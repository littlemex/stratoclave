# Where request and response bodies are held

Customers ask where their prompts go. This document answers it for this gateway as
built today: what the gateway itself keeps, where a body can be found if anywhere,
and what it takes to keep prompt history. Clause C12.7 in
[CONTRACTS.md](CONTRACTS.md) states the property; this document explains it.

## What the gateway keeps

None of the tables the deployment creates, and none of the audit log, is written with
prompt or completion text. Only the usage row's attribute set is checked by a test
(C12.7); the rest is how the code is written, and is listed here so that it can be
read and compared against the code. Every table the IaC creates is named below, and
`backend/tests/test_usage_row_attributes_are_pinned.py` fails if one is added
without being named here.

**Tables written per request.**

| Table | Lifetime | What it carries about the request |
|---|---|---|
| `<prefix>-usage-logs` | 90 days (`ttl`) | token counts, model ids, the settled cost and its bound, the gateway's request id, its user id, a one-way digest of the user's email, a caller-asserted task tag of at most 64 characters, and reason codes. The attribute list is pinned by the test above. |
| `<prefix>-tenant-budgets` (hold rows) | until the request settles, releases or is reclaimed | the reserved amount, the model, the user id, the payload's size in bytes and a SHA-256 digest of the payload. The digest does not reveal the text; a party that already holds a candidate text could check whether it matches. The external authorize API (`POST /api/mvp/billing/authorize`) also stores the free-text `description` its caller supplies; that is text sent to the billing API, not an inference body. |
| `<prefix>-credit-ledger` | no expiry set by the gateway | amounts, ids and timestamps. |
| `<prefix>-observability` | expires by TTL | request ids, the model and region that served it, token counts, status and timing. |
| `<prefix>-routing-signals` | expires by TTL | the routing decision (models considered and chosen, a rule or reason code, a category label), a digest of the session key, token and latency figures. The local shadow router reads only whether the request has tools or images and its token estimate, not its text. |
| `<prefix>-saar-memory` | expires by TTL | per session: the last model, a phase, turn and switch counts, and cache timing. The session key comes from a request header (`x-sc-session-id`) or a server-minted run id, not from the body. |
| `<prefix>-quota-events` | no expiry set by the gateway | limit-raise requests and grants; a requester's own free-text comment on a raise request is stored, which is text the user typed into that form, not an inference body. |
| `<prefix>-rate-limits`, `<prefix>-model-quotas` | expire by TTL | counts per key and window. |

**Tables that hold no per-request content:** `<prefix>-users`, `<prefix>-user-tenants`,
`<prefix>-tenants`, `<prefix>-permissions`, `<prefix>-api-keys`,
`<prefix>-trusted-accounts`, `<prefix>-sso-pre-registrations`, `<prefix>-sso-nonces`,
`<prefix>-ui-tickets`, `<prefix>-sse-tokens`, `<prefix>-pricing-config` and
`<prefix>-promotion-candidates` hold identities, configuration, one-time
authentication artifacts and model onboarding state.

**Provisioned and not written by the backend:** `<prefix>-sessions`,
`<prefix>-messages`, `<prefix>-app-settings` and `<prefix>-tags` are created by the
IaC, and no backend code reads or writes them.

**The audit log** records administrative and sign-in events (tenant, user, key, limit
and routing changes; SSO and console hand-offs), not inference requests.

**Boundary.** The application's own logs are not swept: no inference handler passes a
request or response body to a logging call, but that is how the code is written today
rather than a checked property, as with C12.4.

**When a body does leave the gateway to somewhere other than Bedrock.** A model
registered as served by a self-hosted endpoint (`served_by` `vllm` or
`semantic-router`, behind `HYBRID_SERVING_ENABLED`, off by default) is called with the
request itself, so the body goes to that endpoint. What that endpoint keeps is decided
by whoever runs it.

## Where a body can be found

Bedrock's model invocation logging can deliver a record of each call to CloudWatch
Logs or S3. The record carries token counts and the marker described below; it
carries the request and response text only when text delivery
(`textDataDeliveryEnabled`) is turned on. Both settings belong to the AWS account
and region, not to the gateway.

| Route | Transport | Held by the gateway | Where a body can be found |
|---|---|---|---|
| `POST /v1/messages` | Bedrock Converse | no | the invocation-log destination, when logging and text delivery are on for that account and region |
| `POST /v1/chat/completions` | Converse for Bedrock-native models; the OpenAI-compatible surface on `bedrock-runtime` for OpenAI-family models | no | same |
| `POST /openai/v1/responses` | the OpenAI-compatible surface on `bedrock-runtime` (C13.5) | no | same |

"Found" means delivered, not queryable. Whether a body can be retrieved later depends
on the destination: its retention, its encryption key, who may read it, and whether
delivery succeeded. Bedrock offers no API that returns a past request's text.

The region that matters is the region the gateway calls. A cross-region inference
profile can serve a request in a different region: a record measured on 2026-08-31
was written for a call to `us-east-1` whose `inferenceRegion` was `us-east-2`. A deployment that calls Bedrock in more than one region (its `BEDROCK_REGION`,
each failover region, and the region an OpenAI-family model is registered in) needs
logging configured in each.

## Attribution: what a logged record says about who sent it

The gateway stamps a correlation marker on each provider call, streaming or not, on
each transport (C13.6). Bedrock echoes it into the invocation log.

| Marker key | What it is | When it is absent |
|---|---|---|
| `sc_tenant` | the tenant id | when the value does not pass the value rule below; in the case below where the whole marker is dropped |
| `sc_user` | the principal's `user_id`, the id the console and the usage records use | when the principal has no user id; when the value does not pass the value rule; after `sc_task_tag`, when the OpenAI-compatible key limit below leaves no room for it |
| `sc_attempt_id` | the gateway's id for this attempt's money hold | for a tenant with no dollar pool, which has no hold; in the case below where the whole marker is dropped |
| `sc_task_tag` | the task tag the caller sent in `x-sc-task-tag`, the same one the usage records group by | when the caller sent none or it was dropped (an untagged request is not sent as `unlabelled`); first of all, when the OpenAI-compatible key limit below leaves no room for it |

**The value rule.** Bedrock refuses the whole call when a `requestMetadata` value has
a character outside letters, digits, whitespace and `:_@$#=/+,-.`. So each value is
checked before it is sent, against that set with whitespace narrowed to an inner
space, at most 256 characters, and a value that does not pass is left out, never
altered. The call goes out with the other parts. The ids the gateway mints
or receives from Cognito (`tenant-<uuid>`, a Cognito `sub`, a hold id) and every task
tag its grammar accepts pass. The default tenant's id is chosen in
`DEFAULT_ORG_ID`, so the gateway refuses to start when that id does not pass, and no
tenant row is written with an id that does not. A tenant row written before this
check may not pass; its calls go out without `sc_tenant`, logged once per process.

| Transport | Where the marker goes |
|---|---|
| Bedrock Converse (`/v1/messages`, and `/v1/chat/completions` for Bedrock-native models), streaming or not | `requestMetadata` |
| The OpenAI-compatible surface on `bedrock-runtime` | the OpenAI-standard `metadata` field (#247) |
| A self-hosted endpoint | HTTP request headers, see [Self-hosted endpoints](#self-hosted-endpoints) |

On the OpenAI-compatible surface, Bedrock accepts 16 `metadata` keys. When a caller's
own keys leave no room for the full marker, `sc_task_tag` is left out first and then
`sc_user`, and the attempt and tenant pair is still sent (up to 14 of the caller's keys).
A caller with 15 or more keys
gets the request sent without the gateway's marker entirely rather than refused; that
record carries none of the keys.

So a logged record is attributable to a tenant, a user and a task by key when the key
is present: `sc_user` joins it to the usage records and the console's user list,
and `sc_task_tag` to the usage records' task grouping. The marker is values the
gateway already holds and the caller's task tag; it carries no email and nothing from
the request body.

**What this means for whoever can read the logs.** With logging on, anyone who can
read the destination can see, per user id, which model was called when and with how
many tokens, and with text delivery on, what was sent. Grant read access to the
destination as you would to per-user usage data.

## Self-hosted endpoints

A model registered as served by a self-hosted endpoint (`served_by` `vllm`, behind
`HYBRID_SERVING_ENABLED`) gets the same marker, as HTTP request headers on every
inference call:

| Header | Value |
|---|---|
| `X-Stratoclave-Tenant` | `sc_tenant` |
| `X-Stratoclave-User` | `sc_user` |
| `X-Stratoclave-Attempt-Id` | `sc_attempt_id` |
| `X-Stratoclave-Task-Tag` | `sc_task_tag` |

The responsibilities split at the wire:

- **The gateway** sends the headers on each inference call, with the same values and
  the same absence rules as the Bedrock marker (the value rule above also keeps every
  value a valid header value), and never puts them in the request body, so the
  endpoint's model never sees them and they cost no tokens. The gateway stores nothing
  about the call beyond what it stores for a Bedrock call, and reads nothing back from
  the endpoint's logs.
- **The endpoint's operator** decides what to keep. A reverse proxy in front of the
  model server that writes these headers into its access log, together with the
  token counts or the response status, gives the same per-tenant, per-user and per-task record
  Bedrock's invocation log gives. Whether the bodies are kept there is equally the
  operator's choice.

The semantic-router transport (`served_by` `semantic-router`) is not wired to a real
endpoint yet; when it is, it sends the same headers (C13.6).

## "We need prompt history"

The gateway does not provide it, and adding a place to keep bodies would remove the
property this document states. The capability already exists one layer down:

1. Turn on model invocation logging, with text delivery, in each region the gateway
   calls (see above). This is an account-level setting: it records the bodies of
   each tenant served from that account and region, not of one tenant.
2. Choose the destination's retention, encryption and read permissions as you would
   for any store of customer content.
3. Use `sc_tenant` and `sc_user` to split the records by tenant and by user (see
   [Attribution](#attribution-what-a-logged-record-says-about-who-sent-it)).

