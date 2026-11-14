# The perigraph record on `/v1/chat/completions`

A successful response from `POST /v1/chat/completions` says which harness the request carried, as a
[perigraph](https://github.com/littlemex/perigraph) spec version 3 record. A different system prompt or request template
gives a different harness identity, so a caller that groups its observations by the model name it asked for can see
when two of its requests under one name were different harnesses. Different `tools`, `response_format` or decoding
fields change part digests in the whole record, not the identity.

The gateway returns the record and nothing else. It does not group, compare, store or refuse anything on the strength
of it. What a caller measures under a harness identity, and whether it pools two identities, is the caller's decision.

## What the response carries

| header | sent | value |
|---|---|---|
| `x-sc-perigraph-status` | every successful response | `complete`, or `collector_failed` when the gateway could not build the record |
| `x-sc-perigraph-harness-identity` | when the record has one | `perigraph.harness_identity`, 24 lowercase hex characters |
| `x-sc-perigraph-record` | when the request sends `x-sc-perigraph-return: record` | the whole record: the JSON object of the perigraph JSON Lines binding, ASCII-escaped, base64url without padding |

The whole record is sent on request because it is far larger than the other two headers (about 2.3 KB for a request
with a system message and a template). It is also the only header that names the provider model the gateway invoked.
A caller that writes each decoded record as one line of a `.jsonl` file has a perigraph JSON Lines file.

The headers are added once the provider model is final, just before the provider call. A request rejected before
then (validation, budget, an unknown model) and a provider error returned as an HTTP error carry none of them. A
streamed response sends them with its first bytes, so a stream that fails part-way has already sent them; the record
describes the request, not its outcome.

## What the record says

The parts follow perigraph's `openai-chat-completions` binding, read from the request body as the caller sent it,
decoded from JSON and before validation coerces any value.

| part | read from | boundary | keys the identity |
|---|---|---|---|
| `instruction` | the `system` messages' contents, joined with a newline | `model_visible` | yes |
| `request_template` | `perigraph_template`, verified against the messages | `model_visible` | refines an identity `instruction` establishes |
| `tool_schemas` | `tools`: each function's name and parameters, in any order | `parsed` | no |
| `readout` | `response_format` | `parsed` | no |
| `decoding` | `temperature`, `top_p`, `top_k`, `seed`, then `response_format` | `parsed` | no |

`loop`, `turn_budget`, `retry_policy` and `context_partitioning` have no field in a request body and are absent as
`not_provided`. `tool_extension` is `not_observable`. `tool_trace` is `not_provided`; earlier tool calls in `messages`
are not read as a trace.

A request with no system text has no harness identity, because only `instruction` can establish one;
`request_template` refines one and cannot stand alone. A system message whose `content` is not a string, or a
`perigraph_template` that does not verify, is `extraction_failed` with a detail naming the field and the check, and
withdraws the identity. No detail quotes request content.

The four model parts say the gateway did not observe the model. Each is `not_observable`, so the record has no model
identity. The `checkpoint` part's label is the provider model id the gateway invoked, after any quota cascade, and keys
nothing; an id that is an ARN is left out, because it names the gateway's AWS account. A receiver admits such a
record; its claims are not bound to model bytes. A provider's model id can stay the same while the model behind it
changes, which is why it is a label and not an identity.

## The request as the caller sent it

The record describes the request the caller sent to the gateway. The gateway then builds a new provider request from
it rather than sending the body on, so the binding's Forwarding rule applies to the template only: `perigraph_template`
is removed before the provider request is sent, on both transports. The OpenAI-compatible endpoint rejects it as an
unknown parameter.

The Converse transport caps `temperature` at 1.0, so the `decoding` digest describes the value the caller sent, not the
value invoked. The other translations touch fields no part reads. None of them changes the system text or the message
text a request template names, the two parts that can enter the identity.

## Scope

Only `/v1/chat/completions`. perigraph has no binding yet for the Anthropic Messages API or the OpenAI Responses API,
so `/v1/messages` and `/openai/v1/responses` do not collect a record, and they ignore `x-sc-perigraph-return`.

## Implementation

[`backend/mvp/perigraph_record.py`](../../backend/mvp/perigraph_record.py) is a self-contained collector with no
perigraph package at runtime. The part lists and the identity rules are read from a vendored copy of perigraph's
`spec/vocabularies.json`; absence reasons, statuses and boundaries are literals in the module. A copy whose sha256 is
not the one the module implements is not read, and every response then says `collector_failed` and carries no record.
A failure inside the collector also produces `collector_failed`, and never fails the request.

Conformance can be checked with perigraph's runner, from a perigraph checkout:

```bash
python tools/conform.py --role collector --adapter "python /path/to/stratoclave/backend/tests/perigraph_adapter.py"
```

[`backend/tests/test_perigraph_record.py`](../../backend/tests/test_perigraph_record.py) runs the collector over a
vendored copy of perigraph's identity fixtures, and drives both transports through the route.
