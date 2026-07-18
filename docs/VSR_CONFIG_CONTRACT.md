# Per-Tenant VSR Configuration Contract

Stratoclave stores and edits per-tenant configuration for the **external,
version-pinned VSR** (Value/Session Router) without ever parsing that
configuration. This document is the contract between Stratoclave and the VSR.
It exists so the two can evolve independently: the VSR's config schema may
change with every VSR release, and Stratoclave must need **zero** changes for
that.

## Principles

1. **Loose coupling.** The VSR config (a YAML whose schema is the VSR's own —
   e.g. `routing.decisions[].algorithm.session_aware.*`) is an **opaque blob**
   to Stratoclave. Stratoclave stores it as bytes and never inspects fields.
2. **One shared VSR task, multi-tenant.** There is exactly one VSR service. It
   reads the right tenant's config per request. No per-tenant task is spun up.
3. **Blast-radius isolation.** A broken config for tenant `T` must never crash
   the shared task and must never affect another tenant. Combined with the
   consult being fail-open, a broken `T` config degrades **only** `T` to normal
   Bedrock routing.
4. **Version-pin is a separate axis.** The VSR container image is pinned by
   digest/semver with a startup handshake (see `mvp/vsr/client.py`). Config is
   independent of the image pin.

## Storage (Stratoclave-owned)

- S3 bucket `${prefix}-vsr-config-${account}`, **versioning ON**, private,
  TLS-enforced, SSE-managed. Created only when `EXTERNAL_VSR_ENABLED=true`.
- Keys: `vsr-config/default.yaml` and `vsr-config/<tenant_id>.yaml`.
- The backend task role is granted `s3:GetObject/PutObject/DeleteObject` **only**
  on `vsr-config/*` — never bucket-wide, never `ListBucket`.
- Blobs are capped at **256 KiB** (enforced at PUT and expected at the VSR).

## Stratoclave admin API

All routes are feature-gated (`EXTERNAL_VSR_ENABLED=true` + a configured
bucket); otherwise they return 404. `{tenant_id}` may be a real tenant id or the
reserved literal `default`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/mvp/admin/tenants/{tenant_id}/vsr-config` | Raw config text (404 if unset) |
| PUT | `/api/mvp/admin/tenants/{tenant_id}/vsr-config` | Validate-via-VSR, then store; returns S3 version id |
| DELETE | `/api/mvp/admin/tenants/{tenant_id}/vsr-config` | Remove override (revert to `default`) |
| POST | `/api/mvp/admin/tenants/{tenant_id}/vsr-config/validate` | Dry-run "Check" (validate, do not store) |

Request bodies are **raw text** (`application/yaml` / `text/plain`), not a
per-field JSON model. Authorization:

- `default` — **admin only** (org-wide fallback).
- a real tenant id — the tenant **owner** (its `team_lead`) or an admin. An
  unknown tenant or a non-owner receives a unified `404` (enumeration defense).

## VSR responsibilities (the other side of the contract)

The VSR MUST implement:

### 1. `POST /v1/config/validate`

- Body: the raw config blob (`application/yaml`).
- Response: `200 {"valid": true}` when the config is valid for the running,
  pinned VSR version; `422 {"valid": false, "errors": [...]}` (or `400`/`413`)
  otherwise. `errors` is relayed verbatim to the admin UI.
- This is the **only** place config-schema knowledge lives. Stratoclave
  interprets only the valid/invalid verdict.

Save semantics in Stratoclave:
- validator returns valid → the blob is written to S3;
- validator rejects → **422**, nothing is stored;
- validator is unreachable / unhealthy → **503**, the save fails loudly and
  nothing is stored (an unvalidated blob is never persisted).

### 2. Lazy, last-known-good load model

- The VSR MUST load a tenant's config **lazily** (on first consult for that
  tenant), NOT eagerly at startup — so a save never requires a restart, and a
  restart never re-validates all tenants at once.
- Per-tenant cache with a short TTL (e.g. 30 s) refreshed via S3 ETag
  conditional GET. A freshly saved config is picked up within the TTL without a
  task restart. An optional `POST /v1/config/reload {tenant_id}` may be honored
  for instant pickup, but the poll is the correctness mechanism.
- If tenant `T`'s config is missing/broken/unloadable, the VSR MUST serve `T`
  with its **previous good config (last-known-good)** or the `default`, and MUST
  NOT crash and MUST NOT affect other tenants. All fetch/parse work is wrapped
  in try/except with hard size (256 KiB) and parse-time caps, and a YAML **safe
  loader** (reject anchors/alias bombs).
- Ship a compiled-in baseline default so a total S3 outage still leaves the VSR
  answering (or cleanly returning non-200 → Stratoclave fail-open).

### 3. Consult path (unchanged)

`POST /v1/route {tenant_id, session_key, requested_model}` — 150 ms, no retry,
fail-open. The VSR resolves the tenant's config internally. Stratoclave passes
**only** `tenant_id`; it never sends or reads config on the hot path. Any
suggestion the VSR returns is re-checked against the tenant allowlist by
Stratoclave exactly as a client `x-sc-model-pin` is, so a config can never
expand a tenant's model access or touch the money path.

### 4. Effective-config echo (observability contract addition)

On the `/v1/route` response the VSR SHOULD echo the header
`x-vsr-config-version: <opaque id>` — the id (e.g. the S3 `VersionId`, an etag,
or a content hash) of the tenant config blob it **actually served** this
consult. It is OPTIONAL: an older VSR that omits it simply leaves skew
undetected (never an error).

Stratoclave records the echoed id on the reserve-time decision record
(`vsr.config_version`) and on the `vsr_consult_decision` log line. Compared
offline against the S3 version Stratoclave wrote at PUT, a mismatch reveals
**validate/serve skew** — the running VSR lazy-loaded an older blob or fell back
to last-known-good/default while the admin believes their newest config is live.
Detecting the skew is Stratoclave's job (only the writer knows what it wrote);
resolving it (reload / re-validation sweep) is the VSR's. Stratoclave never
parses the id — it is an opaque equality token, bounded to 128 chars.

## Observability boundary and offline billing reconciliation

Stratoclave does NOT re-implement the VSR's routing-quality metrics (request
counts, routing latency, TTFT/TPOT, cache hit rate). Those are the VSR's own —
it already ships a Prometheus endpoint and a Grafana dashboard, and Stratoclave
co-locates an ADOT sidecar in the VSR task to converge those series onto the
same CloudWatch pane as the gateway (one dashboard, dark-safe: absent when the
VSR is off). The division is: **how the advice was made = the VSR; what happened
to that advice at Stratoclave's trust boundary, and what it billed = Stratoclave.**

Stratoclave therefore keeps only the boundary-owned records, each keyed by the
request's `span_id`:

- the **decision record** (`vsr` block: decision, suggested_model, mode,
  config_version) — written at reserve;
- the **usage row** (effective billed model + `cost_microusd`) — written at
  settle in the UsageLogs table.

An INTERNAL offline job (`mvp.learning.vsr_reconcile` / `vsr_reconcile_cli`)
joins the two per (tenant, day) — no admin API, no new table, no request-path
code — to answer the three questions Stratoclave owns:

1. **billing reconciliation** — for every VSR-acted request, what did it cost
   (summed over matched rows only — an honest partial sum, unsettled requests
   surfaced as a coverage gap, never counted as 0);
2. **enforcement integrity** — was a `hard` pin actually honored (advised alias
   == committed alias, both recorded on the decision item), or did a hard
   decision commit a different model — a trust-boundary **violation** to surface;
3. **coverage** — VSR decisions with no matching usage row (request failed
   before settle, or a dropped best-effort write).

A `prefer` suggestion is advisory (a local SAAR prefer may legitimately override
it), so a `prefer`/`no-advice`/`timeout` decision is `n/a` for enforcement — only
a `hard` pin is held to the billed-model equality check.

## Failure matrix

| Failure | Admin/user sees | Running VSR task | Blast radius |
|---|---|---|---|
| No `<tenant>.yaml` | normal | serves `default` | none — tenant gets default |
| Broken YAML in S3 (e.g. direct write) | metric `vsr_config_load_failure` | LKG, else default; no crash | that tenant only |
| Valid YAML, VSR rejects semantics | save **rejected at PUT** with errors | never sees it | zero |
| VSR `/validate` down at save | PUT fails `503`; retry later | keeps serving last stored config | zero |
| VSR can't reach S3 at consult | none, or latency blip | cached/LKG; cold miss → compiled default; worst case 5xx → Stratoclave fail-open | tenant(s) degrade to normal Bedrock routing — never crash, never wrong billing |
| S3 stale within poll window | save "succeeded", applies within TTL | serves previous **valid** config | that tenant, briefly |

Every load path terminates in one of {tenant config, LKG, default, compiled
default, non-200 → fail-open}. All are per-request and per-tenant-keyed and
exception-wrapped, so there is no crash and no cross-tenant state.
