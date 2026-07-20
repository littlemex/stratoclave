"""Auto-issued Savings Certificate store (litellm wedge slice-4).

The Savings Certificate computation (mvp.learning.savings) is a request-time-free
fold; slice-4 turns it into a DURABLE, AUTO-ISSUED artifact so a tenant gets an
audited savings record without an operator running the CLI. This module is the
issue + write-once persistence + retrieval core (the EventBridge schedule + Lambda
that fans this over all tenants is CDK, wired separately).

HONESTY IS THE PRODUCT — the guards here are load-bearing, not decoration
(Fable slice-4 design):

  * WRITE-ONCE. Each (tenant, day, revision) is persisted with an
    attribute_not_exists condition. A re-run is a no-op, never an overwrite — a
    "audited record" a tenant already saw must not silently change. If a later
    recompute genuinely differs (rate change, late-settled usage), it is issued
    as a NEW revision that `supersedes` the old one; the old row is never deleted.
  * NO SYNTHETIC IN THE STORE. The writer refuses any certificate whose
    provenance is not "real" — a seeded/demo (synthetic) certificate can never be
    persisted, even if a future caller reuses this writer.
  * CAVEATS ARE A RUNTIME INVARIANT. The writer refuses a certificate missing the
    honesty caveats (quality-unmeasured, potential-is-upper-bound). A certificate
    that dropped its caveats is not storable — the contract is enforced at write,
    not merely in tests.
  * DATA-ABSENT != ZERO SAVING. "no VSR-acted traffic that day" is NOT a $0
    certificate — issuing $0 would assert "we saved nothing", a lie about a day we
    could not measure. issue_certificate returns a NOT-ISSUED outcome for an empty
    day, and the scheduler records the skip (with a reason) rather than a number.
  * generated_at IS INJECTED. This module never calls a clock; the caller (the
    Lambda handler, the only non-deterministic boundary) passes generated_at_ms,
    so the domain code stays deterministic and testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# Schema version of the persisted certificate envelope. Bump when the stored
# shape changes; it is stamped on every row so a reader knows the contract.
CERTIFICATE_SCHEMA_VERSION = "cert-v1"

# The provenance the store will accept. Synthetic/demo certificates are computed
# the same way but must NEVER be persisted as an audited artifact.
_ALLOWED_PROVENANCE = "real"

# Skip reasons (a NOT-ISSUED outcome carries one). Stable strings: the scheduler
# and its alarm consume them.
#
# NOTE on SKIP_NO_TRAFFIC (Fable slice-4 (d)): this fires when the reconcile shows
# ZERO VSR-acted decisions for the day. It DOES NOT distinguish "the VSR genuinely
# acted on nothing" from "the decision-log ingestion was down" — both look like 0
# decisions here. That is safe for honesty (neither forges a $0 certificate), but
# it is NOT a sufficient operational signal on its own: a fleet-wide ingestion
# outage shows up as every tenant skipping NO_TRAFFIC, which is normal-looking at
# the per-tenant level. The scheduler (CDK leg) MUST therefore alarm on
# "all/most tenants skipped NO_TRAFFIC on the same day" (an outage), separately
# from a single tenant's quiet day (normal). See docs/design/vsr-savings-certificate.md.
SKIP_NO_TRAFFIC = "no_vsr_acted_traffic"          # decision count 0 (quiet OR ingest outage)
SKIP_UNMATCHED_HIGH = "reconcile_unmatched_high"  # too much unsettled to be final
SKIP_NOT_REAL = "provenance_not_real"             # refused synthetic

# Default coverage gate: a day with more than this fraction of its VSR decisions
# unsettled is not final-certifiable. TODO(slice-4 follow-up): tune from the
# observed unmatched distribution once the scheduler has run (Fable slice-4 (c)).
DEFAULT_MAX_UNMATCHED_FRACTION = 0.10


@dataclass(frozen=True)
class IssueOutcome:
    """The result of an issue attempt. Exactly one of `certificate` /
    `skip_reason` is set. `issued` False means the day was deliberately NOT
    certified (honest absence), never a $0 forgery. `already_existed` True means a
    write-once collision occurred and `certificate` is the STORED row (the audit
    record), NOT a fresh recompute — the API never returns something other than
    what is persisted (Fable slice-4 (e)-1)."""

    issued: bool
    tenant_id: str
    day: str
    certificate: Optional[dict[str, Any]] = None
    skip_reason: Optional[str] = None
    already_existed: bool = False


def _has_required_caveats(certificate: dict[str, Any]) -> bool:
    """The certificate MUST still carry its honesty caveats: quality is not
    measured, and the potential (shadow) base is an upper-bound estimate. A
    certificate that lost these is not storable (Fable slice-4 (e))."""
    savings = certificate.get("savings")
    if not isinstance(savings, dict):
        return False
    quality = savings.get("quality")
    if not isinstance(quality, dict) or quality.get("measured") is not False:
        return False
    potential = savings.get("potential")
    # the potential base MUST carry its upper-bound `note` (savings.summarize_savings)
    # and be explicitly not-enacted — that is the honesty caveat for shadow advice.
    if not isinstance(potential, dict) or not potential.get("note"):
        return False
    if potential.get("enacted") is not False:
        return False
    return True


def _reconcile_ok_to_finalize(certificate: dict[str, Any], *,
                              max_unmatched_fraction: float) -> bool:
    """A day is final-certifiable only if its reconcile coverage is high enough:
    too many unmatched (unsettled / dropped-usage) decisions means the realized
    figure would understate — better a documented skip than a low number stamped
    `final` (Fable slice-4 (d) #2)."""
    reconcile = certificate.get("reconcile")
    if not isinstance(reconcile, dict):
        return False
    total = reconcile.get("vsr_acted_count")
    unmatched = reconcile.get("unsettled_count", 0)
    if not isinstance(total, int) or total <= 0:
        # no decisions at all is handled as NO_TRAFFIC upstream, not here.
        return True
    return (int(unmatched) / total) <= max_unmatched_fraction


def issue_certificate(*, tenant_id: str, day: str, generated_at_ms: int,
                      max_unmatched_fraction: float = DEFAULT_MAX_UNMATCHED_FRACTION,
                      revision: int = 0, supersedes_generated_at_ms: Optional[int] = None,
                      supersede_reason: Optional[str] = None,
                      certificate_fn=None) -> IssueOutcome:
    """PURE issue step: compute the (tenant, day) certificate and decide whether it
    is honestly certifiable. Returns an IssueOutcome. Does NOT persist. `certificate_fn`
    is injectable for tests; defaults to savings.savings_certificate with
    traffic="real" (the schedule path NEVER issues synthetic).

    `revision`>0 issues a superseding certificate (a genuine recompute, e.g. after
    late-settled usage): the envelope carries `supersedes` (the prior revision +
    its generated_at + a reason) so the amendment chain is SELF-DESCRIBING, not
    inferred from the sk order (Fable slice-4 (a)). The old row is never mutated.

    NOT-ISSUED (issued=False) is returned — never a $0 certificate — when the day
    has no VSR-acted traffic (SKIP_NO_TRAFFIC) or when reconcile coverage is too
    low to finalize (SKIP_UNMATCHED_HIGH)."""
    # generated_at sanity (Fable slice-4 (e)-3): reject a non-positive stamp; the
    # caller (Lambda) always has a real event time, so 0/negative is a bug.
    if int(generated_at_ms) <= 0:
        raise ValueError(f"generated_at_ms must be positive, got {generated_at_ms!r}")
    if int(revision) < 0:
        raise ValueError(f"revision must be >= 0, got {revision!r}")

    if certificate_fn is None:
        from .savings import savings_certificate as certificate_fn  # noqa: N806

    cert = certificate_fn(tenant_id=tenant_id, day=day, traffic="real")

    # data-absent != zero saving: an empty day is an honest skip.
    reconcile = cert.get("reconcile") or {}
    decision_count = reconcile.get("vsr_acted_count", 0)
    if not isinstance(decision_count, int) or decision_count <= 0:
        return IssueOutcome(issued=False, tenant_id=tenant_id, day=day,
                            skip_reason=SKIP_NO_TRAFFIC)

    if not _reconcile_ok_to_finalize(cert, max_unmatched_fraction=max_unmatched_fraction):
        return IssueOutcome(issued=False, tenant_id=tenant_id, day=day,
                            skip_reason=SKIP_UNMATCHED_HIGH)

    envelope = {
        "record_type": "savings_certificate",
        "schema_version": CERTIFICATE_SCHEMA_VERSION,
        "tenant_id": tenant_id,
        "day": day,
        "generated_at_ms": int(generated_at_ms),
        "status": "final",
        "revision": int(revision),
        "certificate": cert,
    }
    if revision > 0:
        # self-describing amendment: which prior revision this replaces, and why.
        envelope["supersedes"] = {
            "revision": int(revision) - 1,
            "generated_at_ms": (int(supersedes_generated_at_ms)
                                if supersedes_generated_at_ms is not None else None),
            "reason": supersede_reason or "recompute",
        }
    return IssueOutcome(issued=True, tenant_id=tenant_id, day=day, certificate=envelope)


# --------------------------------------------------------------- persistence

def _cert_pk(tenant_id: str) -> str:
    from .decision_log import _safe_key_token
    return f"CERT#{_safe_key_token(tenant_id)}"


def _cert_sk(day: str, revision: int) -> str:
    # r#%04d zero-pads so the sk sorts by revision lexicographically (get_latest
    # relies on this). Revisions >= 10000 would break that order; a day never
    # accrues that many amendments, but assert to fail loud if the assumption dies.
    assert 0 <= int(revision) < 10000, f"revision out of sortable range: {revision}"
    return f"cert#D#{day}#r#{int(revision):04d}"


def _table():
    from dynamo.client import get_dynamodb_resource
    from .decision_log import signals_table_name
    return get_dynamodb_resource().Table(signals_table_name())


def store_certificate(envelope: dict[str, Any]) -> bool:
    """WRITE-ONCE persist of an issued certificate envelope. Returns True on a
    fresh write, False if this (tenant, day, revision) already exists (idempotent
    re-run — never an overwrite). Refuses to persist a non-"real" provenance or a
    certificate that dropped its honesty caveats (Fable slice-4 (b)/(d)/(e))."""
    from botocore.exceptions import ClientError

    cert = envelope.get("certificate") or {}
    if cert.get("traffic") != _ALLOWED_PROVENANCE:
        raise ValueError(f"refusing to persist non-real certificate "
                         f"(traffic={cert.get('traffic')!r})")
    if not _has_required_caveats(cert):
        raise ValueError("refusing to persist a certificate missing honesty "
                         "caveats (quality.measured must be False; potential.note "
                         "required and potential.enacted must be False)")

    item = dict(envelope)
    item["pk"] = _cert_pk(envelope["tenant_id"])
    item["sk"] = _cert_sk(envelope["day"], envelope.get("revision", 0))
    try:
        _table().put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False       # already issued — idempotent no-op, not an error.
        raise


def _strip_keys(item: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Return the stored item without the DynamoDB pk/sk, so a persisted row has
    the SAME shape as a freshly-issued envelope (Fable slice-4 close: the fresh and
    collision paths must not hand callers structurally different objects)."""
    if item is None:
        return None
    return {k: v for k, v in item.items() if k not in ("pk", "sk")}


def get_certificate(*, tenant_id: str, day: str, revision: int = 0,
                    consistent_read: bool = False) -> Optional[dict[str, Any]]:
    """Fetch ONE stored certificate revision, or None if not issued. Keys are
    stripped so the shape matches a freshly-issued envelope."""
    resp = _table().get_item(
        Key={"pk": _cert_pk(tenant_id), "sk": _cert_sk(day, revision)},
        ConsistentRead=consistent_read)
    return _strip_keys(resp.get("Item"))


def get_latest_certificate(*, tenant_id: str, day: str) -> Optional[dict[str, Any]]:
    """Fetch the HIGHEST-revision certificate for a (tenant, day), or None. Since
    older revisions keep status="final" (write-once, never mutated), a reader that
    wants "the current record" must pick the max revision — the sk sorts
    `cert#D#<day>#r#<rev>` so the last one in the day's range is newest (Fable
    slice-4 (a): a query can return multiple 'final' rows; this disambiguates)."""
    from boto3.dynamodb.conditions import Key
    resp = _table().query(
        KeyConditionExpression=(Key("pk").eq(_cert_pk(tenant_id))
                                & Key("sk").begins_with(f"cert#D#{day}#r#")),
        ScanIndexForward=False, Limit=1)
    items = resp.get("Items") or []
    return _strip_keys(items[0]) if items else None


def issue_and_store(*, tenant_id: str, day: str, generated_at_ms: int,
                    max_unmatched_fraction: float = DEFAULT_MAX_UNMATCHED_FRACTION,
                    certificate_fn=None) -> IssueOutcome:
    """Issue + write-once persist in one call. A skipped day is NOT persisted
    (honest absence). On a write-once COLLISION (this revision already exists), the
    returned certificate is the STORED row, NOT the fresh recompute — the audit API
    must never hand back a value different from what is persisted (Fable slice-4
    (e)-1). `already_existed=True` flags the no-op."""
    out = issue_certificate(tenant_id=tenant_id, day=day, generated_at_ms=generated_at_ms,
                            max_unmatched_fraction=max_unmatched_fraction,
                            certificate_fn=certificate_fn)
    if not (out.issued and out.certificate is not None):
        return out
    revision = out.certificate.get("revision", 0)
    fresh = store_certificate(out.certificate)     # write-once
    if fresh:
        return out
    # collision: return the PERSISTED row, not our recompute. Read it strongly
    # consistent — a write-once collision means the row exists NOW, so an eventually
    # consistent read that returned None/stale would let us hand back a wrong or
    # empty certificate (the very audit-API-lie (e)-1 closed). A None here is an
    # invariant violation (row must exist), not a swallow-able miss.
    stored = get_certificate(tenant_id=tenant_id, day=day, revision=revision,
                             consistent_read=True)
    if stored is None:
        raise RuntimeError(
            f"write-once collision for ({tenant_id}, {day}, r{revision}) but the "
            "stored certificate could not be read back — inconsistent store state")
    return IssueOutcome(issued=True, tenant_id=tenant_id, day=day,
                        certificate=stored, already_existed=True)


@dataclass(frozen=True)
class BatchIssueReport:
    """The scheduler's per-run summary. `issued` / `skipped` / `failed` partition
    the tenant list; a non-empty `failed` (or issued < expected) is what the
    "silent skip" alarm keys on (Fable slice-4 (c))."""

    issued: list[str]
    skipped: list[tuple[str, str]]     # (tenant_id, skip_reason)
    failed: list[tuple[str, str]]      # (tenant_id, error_type)


def issue_for_tenants(*, tenant_ids: list[str], day: str, generated_at_ms: int,
                      max_unmatched_fraction: float = DEFAULT_MAX_UNMATCHED_FRACTION,
                      certificate_fn=None) -> BatchIssueReport:
    """Issue+store for every tenant, with PER-TENANT failure isolation: one
    tenant's error is recorded and the loop continues (write-once makes a later
    re-run safe). This is the body the scheduled Lambda calls; the Lambda passes
    generated_at_ms from the EventBridge event `time` (the sole clock boundary)."""
    issued: list[str] = []
    skipped: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    for tenant_id in tenant_ids:
        try:
            out = issue_and_store(tenant_id=tenant_id, day=day,
                                  generated_at_ms=generated_at_ms,
                                  max_unmatched_fraction=max_unmatched_fraction,
                                  certificate_fn=certificate_fn)
            if out.issued:
                issued.append(tenant_id)
            else:
                skipped.append((tenant_id, out.skip_reason or "unknown"))
        except Exception as e:  # noqa: BLE001 — isolate: one tenant never blocks the rest.
            failed.append((tenant_id, type(e).__name__))
    return BatchIssueReport(issued=issued, skipped=skipped, failed=failed)
