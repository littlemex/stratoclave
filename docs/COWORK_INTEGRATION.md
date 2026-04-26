# Using Stratoclave with Claude Desktop Cowork

Claude Desktop's **Cowork** feature runs long-form, agentic tasks and can be
configured to route its inference requests through a custom gateway instead
of calling Anthropic directly. This guide shows how to use a Stratoclave
deployment as that gateway, so that Cowork requests are authenticated,
credit-accounted, and audit-logged per user and per tenant — while the
model calls themselves continue to run on Amazon Bedrock.

Cowork is a headless, long-running client, so the one-hour lifetime of a
Cognito `access_token` is inconvenient. Stratoclave therefore issues
**long-lived API keys** (`sk-stratoclave-...`) that Cowork can use as bearer
tokens for as long as the administrator allows.

<!-- TODO(docs): Insert high-level diagram showing Cowork → CloudFront → ALB → FastAPI → Bedrock -->

## Table of contents

- [Prerequisites](#prerequisites)
- [Step 1. Issue a long-lived API key](#step-1-issue-a-long-lived-api-key)
- [Step 2. Configure Claude Desktop](#step-2-configure-claude-desktop)
- [Step 3. Verify the connection](#step-3-verify-the-connection)
- [Step 4. Start using Cowork](#step-4-start-using-cowork)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)
- [Internals (for the curious)](#internals-for-the-curious)

---

## Prerequisites

Before starting, confirm the following:

- A Stratoclave deployment is reachable over HTTPS at a CloudFront URL of
  the form `https://<subdomain>.cloudfront.net`. If you don't have one yet,
  follow [GETTING_STARTED.md](GETTING_STARTED.md) first.
- You can log in to the deployment either through the Web UI or via the
  CLI (`stratoclave auth login` or `stratoclave auth sso`). Any role —
  `user`, `team_lead`, or `admin` — can issue its own API keys.
- Claude Desktop is installed on your workstation. The Cowork gateway
  feature requires **Developer Mode** (see Step 2).
- Your Stratoclave user has the `messages:send` permission. All three
  default roles include it; if you are running a customized permissions
  table, confirm the value is present.

If you plan to call models other than the defaults, confirm with your
administrator that the Bedrock inference profiles you want are enabled on
the deployment's region.

---

## Step 1. Issue a long-lived API key

Stratoclave exposes two issuance paths. Both produce the same kind of key;
pick whichever is more convenient.

### Option A — Web UI

1. Log in to the Stratoclave Web UI.
2. From the header, click **API keys** (or navigate directly to
   `/me/api-keys`).
3. Click **New key** and fill in the dialog:

    | Field | Suggested value | Notes |
    |-------|-----------------|-------|
    | Label | `cowork on <machine-name>` | Up to 64 characters. Shown in the list so you can revoke the right key later. |
    | Scopes | `messages:send`, `usage:read-self` (default) | Scopes are narrowed by your own roles; you cannot issue a key more powerful than your account. |
    | Expiration | `30 days` (recommended) | Options: 7 / 30 / 90 / 180 / 365 days, or no expiration. |

4. Confirm. The key's **plaintext** is displayed **once**, in a modal, with
   a copy-to-clipboard control. Copy it into your password manager
   immediately — the server stores only a SHA-256 hash and cannot show the
   plaintext again.

<!-- TODO(docs): screenshot of the /me/api-keys list page -->
<!-- TODO(docs): screenshot of the "New key" dialog and the one-shot plaintext modal -->

### Option B — CLI

```bash
stratoclave api-key create \
  --name "cowork on laptop" \
  --scope messages:send --scope usage:read-self \
  --expires-days 30
```

The last line of the command's output is the plaintext key, prefixed with
`sk-stratoclave-`. Capture it:

```text
sk-stratoclave-4f8c9b2a1d7e6c0f3a5b8c9d2e1f4a7b
```

The CLI does not persist the plaintext anywhere on disk — it is your
responsibility to store it securely.

---

## Step 2. Configure Claude Desktop

### 2.1 Enable Developer Mode

1. Open Claude Desktop.
2. In the application menu, choose **Help → Troubleshooting → Enable
   Developer Mode**.
3. A **Developer** menu appears in the menu bar.

### 2.2 Open the gateway configuration

From the **Developer** menu, select **Configure Third-Party Inference**
and switch the mode to **Gateway**.

### 2.3 Fill in the gateway fields

Use the CloudFront URL of your Stratoclave deployment and the API key you
issued in Step 1.

| Field | Value |
|-------|-------|
| Gateway base URL | `https://<your-deployment>.cloudfront.net` |
| Gateway auth scheme | `Bearer` |
| Gateway API key | `sk-stratoclave-...` (the plaintext from Step 1) |
| Gateway extra headers | *(leave empty)* |
| Model list | `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5` (or leave empty to auto-discover) |
| Organization UUID | *(optional, leave empty)* |
| Credential helper script | *(leave empty; unnecessary for long-lived keys)* |

> **Important.** The gateway base URL must *not* contain `/v1`. Cowork
> automatically prepends `/v1/models` and `/v1/messages` to the base URL.
> If you enter `https://<host>/v1`, Cowork will request
> `https://<host>/v1/v1/models`, which the backend does not route and
> which will return a `404`. This is the single most common configuration
> error; if anything fails, check this first.

### 2.4 Save and restart

Click **Save locally** and then **restart Claude Desktop**. Cowork reads
its gateway configuration only at startup; a restart is required for any
field change to take effect.

<!-- TODO(docs): screenshot of the Developer → Configure Third-Party Inference dialog -->

---

## Step 3. Verify the connection

From the **Developer** menu, choose **Test Third-Party Inference**. A
successful test emits:

```text
Gateway API key was accepted.
```

On the Stratoclave side, a `GET /v1/models` and/or `POST /v1/messages`
call will appear in CloudWatch Logs, tagged with your `user_id`. If you
have `usage:read-self`, you can also see it by running:

```bash
stratoclave usage show
```

---

## Step 4. Start using Cowork

With the test green, use Cowork normally. Each inference request goes
through the following steps:

1. Cowork sends `POST /v1/messages` to your CloudFront URL with
   `Authorization: Bearer sk-stratoclave-...`.
2. CloudFront forwards the request to the ALB, which routes it to the
   backend task.
3. The backend validates the key (hash lookup, revocation check, expiration
   check) and resolves the owner's current roles. The request is admitted
   only if the requested permission is held by **both** the owner's roles
   **and** the key's scopes.
4. The backend calls Bedrock `converse` / `converseStream`, streams the
   response back to Cowork, and writes a usage log with the exact
   `input_tokens` and `output_tokens` reported by Bedrock.
5. The credit balance for `(user_id, tenant_id)` is decremented by the
   total token count using a conditional DynamoDB update.

Revoking the key takes effect on the *next* request; there is no in-process
caching. You can revoke from the Web UI or via
`stratoclave api-key revoke <key_id>`.

---

## Troubleshooting

### `Gateway returned HTTP 404` with an endpoint like `https://<host>/` or `https://<host>/v1/v1/models`

The gateway base URL almost certainly contains `/v1`. Remove it so the URL
is the bare `https://<host>`. Restart Claude Desktop after saving.

### `Gateway API key was rejected` / `401 Unauthorized`

Possible causes, in rough order of likelihood:

- The key was copy-pasted with surrounding whitespace or a missing
  character. Re-issue the key and paste with care.
- The key has expired. Inspect it via the Web UI or
  `stratoclave api-key list`.
- The key has been revoked. The `revoked_at` column in the list view will
  be populated.
- The owning user has been deleted. The API key path requires the owner's
  `Users` record to still exist.

### `403 Forbidden` on `POST /v1/messages` even though the test request succeeded

The key passed authentication (the test request exercises authentication
only) but failed authorization on `messages:send`. Two common causes:

- The key was issued with `--scope usage:read-self` but *not*
  `--scope messages:send`. Re-issue with both scopes, or re-issue with
  the defaults.
- The owner's current role no longer contains `messages:send`. All three
  default roles do, but a customized permissions table could change
  this. Ask an administrator.

### Cowork shows *No models* with an empty model list

With an empty **Model list**, Cowork calls `GET /v1/models` and expects a
list of model IDs. Stratoclave's `/v1/models` requires a valid
`Authorization: Bearer` header — if the key is accepted but your scopes
don't cover `messages:send`, the auto-discovery probe will return `403`
and Cowork will render "No models". Either add `messages:send` to the key
or list model IDs explicitly (`claude-opus-4-7`, `claude-sonnet-4-6`,
`claude-haiku-4-5`, …).

### Streaming appears to hang

Cowork uses server-sent events (`Accept: text/event-stream`). Stratoclave
sets `Cache-Control: no-cache`, `Connection: keep-alive`, and
`X-Accel-Buffering: no` so that CloudFront does not buffer the stream. If
your deployment has fronted the CloudFront distribution with another CDN,
make sure SSE pass-through is enabled there as well.

### I want to rotate the key without downtime

Issue a new key first, update Claude Desktop's configuration with the new
key (and restart Claude Desktop), then revoke the old key. Because
Stratoclave does not cache revocations, the old key is invalid
immediately after revoke.

---

## Security notes

- **Plaintext is never retained server-side.** Only a SHA-256 hash is
  stored. A leaked database does not leak any API keys.
- **Revocation is immediate.** The next request to arrive after a revoke
  is rejected with `401`.
- **Scope narrowing.** Issue keys with the minimum scopes you need
  (`messages:send` is usually enough; add `usage:read-self` only if the
  client needs to query its own usage). Scope narrowing also means that if
  the owning user is demoted (e.g. admin → user), the key immediately
  stops being able to do anything the owner can't do anymore.
- **Per-user active-key limit.** Each user is capped at 5 active (not
  revoked, not expired) keys. Revoke before re-issuing if you're near the
  limit.
- **Blast radius.** A leaked key can only burn the owning user's credit,
  bounded by the tenant's default credit and any per-user override. The
  audit log (`api_key_created`, `api_key_revoked`) preserves the full
  issuance and revocation history for forensic review.
- **Delegated issuance.** Admins can issue keys on behalf of another user
  via `POST /api/mvp/admin/users/{user_id}/api-keys`. The audit log
  records both the actor and the `on_behalf_of` user.

---

## Internals (for the curious)

### How the backend distinguishes API keys from Cognito tokens

`backend/mvp/deps.py` inspects the incoming bearer token and dispatches on
its prefix:

- `sk-stratoclave-...` → API key path: SHA-256 hash, DynamoDB `ApiKeys`
  lookup, revocation and expiration checks, owner load from `Users`.
- Anything else → Cognito `access_token` path: JWKS key fetch, `RS256`
  verify, `token_use == "access"` assertion, `client_id` claim check, then
  `Users` lookup.

In both cases the handler produces the same `AuthenticatedUser` dataclass.
For API-key callers, `AuthenticatedUser.auth_kind = "api_key"` and
`AuthenticatedUser.key_scopes` is populated from the key's scopes. The
authorization helper `user_has_permission(user, permission)` then enforces
the AND of the owner's roles and the key's scopes.

### The `/v1/models` endpoint

Cowork probes `GET /v1/models` when its configured model list is empty.
The endpoint requires authentication (so that the list is not publicly
enumerable) and returns the set of model IDs that Stratoclave currently
knows how to translate to Bedrock inference profiles, in a shape compatible
with the Anthropic Models API.

### Why the gateway base URL must not include `/v1`

Cowork hard-codes the `/v1` prefix when it constructs the final URL. This
mirrors Anthropic's API surface, where the base URL is the origin and the
version lives in the path. If you include `/v1` in the base URL, every
request becomes `/v1/v1/...` and the backend returns `404` because only
`/v1/messages`, `/v1/models`, `/api/*`, and `/.well-known/*` are routed.

### What gets audit-logged

Each issuance emits `api_key_created` with the scopes, label, and target
user; each revoke emits `api_key_revoked` with the actor and the owner.
Admin-initiated issuance also records `on_behalf_of` so that the actor's
identity is recoverable from the log.
