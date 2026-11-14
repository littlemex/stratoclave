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

The gateway stamps a correlation marker on its provider calls, on both transports:
`requestMetadata` on Converse, the OpenAI-standard `metadata` field on the
OpenAI-compatible surface (#247). Bedrock echoes it into the invocation log.

| Marker key | What it is | When it is absent |
|---|---|---|
| `sc_tenant` | the tenant id | in the case below where the whole marker is dropped |
| `sc_attempt_id` | the gateway's id for this attempt's money hold | for a tenant with no dollar pool, which has no hold |

On the OpenAI-compatible surface, a caller whose own `metadata` already uses 15 or
more of the 16 keys Bedrock accepts gets the request sent without the gateway's
marker entirely rather than refused; that record carries neither key.

**The marker carries no user identifier, and neither does the rest of the record.**
The record's `identity` field names the gateway's task role, which is the same whichever
user sent the request, and the gateway sends no user identifier to the provider.

Nor does the record share a key with a durable gateway record that names a user. The
attempt id is not stored on the usage row or in the credit ledger, and the hold row
that pairs it with a user is deleted when the request settles (no log line was found
that writes the two together; that was a search, not a checked property). So the
record is attributable to a tenant by key (except when the marker was dropped, above)
and not to a user by key.

That is not the same as anonymous. A usage row carries the tenant, the time, the model
and the token counts, and so does the log record; matching on those will often single
out one usage row, and with it one user, for the 90 days the row exists. Do not treat
invocation logs as de-identified per user. Recording the attempt id on the usage row
would make the join exact rather than inferred; that is not built.

## "We need prompt history"

The gateway does not provide it, and adding a place to keep bodies would remove the
property this document states. The capability already exists one layer down:

1. Turn on model invocation logging, with text delivery, in each region the gateway
   calls (see above). This is an account-level setting: it records the bodies of
   each tenant served from that account and region, not of one tenant.
2. Choose the destination's retention, encryption and read permissions as you would
   for any store of customer content.
3. Use `sc_tenant` to split the records by tenant. There is no per-user key in these
   records today, though a user can often be inferred (see the previous section).

