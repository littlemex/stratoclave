# Gateway capacity

How many requests the gateway can hold at once, what used to limit it, and how to
size it for a target.

## The question

A client in front of this gateway sees one endpoint. Whatever concurrency it
drives lands here first, so the gateway's own ceiling — not the model's — decides
what the caller experiences. That ceiling was measured on 2026-08-24, against the
running service, by sweeping concurrency 1 to 64 and comparing each stage with the
same model called directly.

| Concurrency | Through the gateway | Direct to the same model |
| --- | --- | --- |
| 1 | 1.4 req/s, p50 547 ms | 4.2 req/s, p50 231 ms |
| 8 | 4.2 req/s, p50 1663 ms | 6.0 req/s, p50 287 ms |
| 32 | 4.6 req/s, p50 6223 ms | 28.4 req/s, p50 316 ms |
| 64 | 3.6 req/s, p50 9903 ms | 75.6 req/s, p50 386 ms |

Throughput flattened at about 4.5 req/s while latency grew with concurrency: the
extra load was queueing, not being served. Every request returned 200, so nothing
was rejected — it was simply held. The same ceiling appeared on the Converse
transport, which shares no code with the bedrock-mantle transport, so the limit was
common infrastructure rather than one upstream.

## What limited it

**Two framework defaults, each a hard cap on in-flight requests.**

`POST /v1/chat/completions` is a `def` route, so FastAPI runs it in `anyio`'s
worker threadpool and it holds its worker for the whole upstream call. That pool
defaults to 40 threads, so one task could hold 40 chat requests no matter how much
CPU it had. Separately, `asyncio.to_thread` — which the streaming and accounting
paths use to keep blocking DynamoDB and boto3 calls off the event loop — runs on
the loop's default executor, sized `min(32, cpu_count + 4)`. Inside a Fargate task
`cpu_count` reports the host's cores, not the task's share, so that number was
neither intentional nor predictable.

Both are now set explicitly, per task, in `mvp/_concurrency.py`, from
`GATEWAY_SYNC_ROUTE_THREADS` and `GATEWAY_OFFLOAD_THREADS`.

**Per-request connection setup.** The Bedrock client factory built a fresh
`boto3.client` per call, and the mantle transport built a fresh `httpx` client and
minted a fresh bearer per call. Two clients from one session hold separate
connection pools, so each request paid a new TLS handshake — visible as the 300 ms
gap at concurrency 1, which is not queueing and did not close as load rose. Both
transports now pool per region for the life of the process. The bearer is cached
under its own TTL and passed per request rather than pinned to a client, so the
token and the connection pool rotate independently.

The Bedrock factory's previous per-call construction was there to avoid
snapshotting rotating ECS task-role credentials. That is not a real risk: the
request signer holds the session's credentials *object*, and for a container role
that object refreshes itself when a signature is taken inside its refresh window.
`test_transport_pooling_and_capacity.py` pins that wiring.

Pooling is only as wide as the pool. `botocore` caps a client at 10 connections
by default, and `httpx` at 100: past that, urllib3 opens a connection, uses it and
discards it, which puts the handshake back on every request beyond the cap. Both
ceilings therefore track the per-task request ceiling
(`BEDROCK_MAX_POOL_CONNECTIONS`, `MANTLE_MAX_CONNECTIONS`), and the mantle pool
wait is bounded at 10 s: a connection is acquired *after* the budget reservation
is taken, so an unbounded wait would hold a customer's balance on a queue that is
our own saturation rather than the model's work.

A pooled client that somebody closes is rebuilt rather than trusted. Under
per-request construction any such mistake cost one request; a pooled client would
otherwise fail every request for its region until the task is replaced.

**A task too small to serve what it admitted.** At 256 CPU units (0.25 vCPU) the
per-request work — signing, JSON, TLS — was itself the throughput limit. The
default task is now 1024 CPU units and 2048 MiB, overridable per deployment. That
is four times the per-task cost, and with a floor of two tasks it is what the
gateway costs at idle; a deployment that does not need the concurrency should set
`BACKEND_TASK_CPU` and `BACKEND_TASK_MEMORY` back down rather than inherit it.

## Sizing for a target

Fleet concurrency is the per-task ceiling times the running task count. A target
of 1024 in flight is eight tasks admitting 128 requests each, which is the shipped
default: `GATEWAY_SYNC_ROUTE_THREADS=128` with `BACKEND_MAX_TASKS=8`.

**Admitting is not serving, and in flight is not per second.** These are two
different quantities and it is worth keeping them apart:

* *In flight* is what the ceilings above bound. A request is in flight for as long
  as its upstream call takes, so 1024 in flight is only a meaningful target when
  requests are slow — long completions and streams. With sub-second requests the
  fleet finishes them faster than a client can accumulate them, and in-flight
  stays low while throughput is high.
* *Throughput* is bounded by CPU, not by the ceilings. Raising a ceiling does not
  create capacity: a thread blocked on an upstream socket costs little, but every
  admitted request still needs its slice of CPU for signing, JSON and TLS.

So the ceiling is set to what the task size can serve, and the autoscaler adds
tasks beyond that. Both live in `iac/bin/iac.ts` next to the task size, and the
Python side reads every one of them at startup and refuses to start on a value
that is set but unusable — the same fail-fast the IaC applies at synth, and the
same rule the price source already follows. A quietly throttled gateway is harder
to notice than one that does not come up.

The configuration is checked against a declared target at synth time.
`BACKEND_CONCURRENCY_TARGET` (1024 by default) is compared with both numbers, and
the synth output states them: a fleet that cannot reach its target with every task
running is a warning, and so is a fleet that may grow but has no signal to grow it.

**Autoscaling answers sustained load, not the first seconds of a burst.** Target
tracking observes a metric, waits for its alarm, then starts a task that needs a
cold start before it serves anything. Whatever arrives in the meantime is served
by the tasks already running, so the floor — `BACKEND_MIN_TASKS`, two by default,
which absorbs 256 in flight — is what decides burst behaviour. A deployment that
must absorb its full target instantly raises the floor to the ceiling and pays for
idle capacity.

| Variable | Default | What it sizes |
| --- | --- | --- |
| `BACKEND_TASK_CPU` | 1024 | CPU units per task |
| `BACKEND_TASK_MEMORY` | 2048 | MiB per task |
| `BACKEND_MIN_TASKS` | 2 | floor, so also the burst the fleet absorbs immediately |
| `BACKEND_MAX_TASKS` | 8 | ceiling the autoscaler may reach |
| `BACKEND_REQUESTS_PER_TARGET` | unset | requests/min/task the scaler holds; see below |
| `GATEWAY_UVICORN_WORKERS` | one per vCPU | server processes per task |
| `GATEWAY_SYNC_ROUTE_THREADS` | 32 | in-flight sync-route requests per PROCESS |
| `GATEWAY_OFFLOAD_THREADS` | 32 | concurrent offloaded blocking calls per process |
| `MANTLE_MAX_CONNECTIONS` | 256 | pooled connections per process to bedrock-mantle |
| `BEDROCK_MAX_POOL_CONNECTIONS` | request ceiling | pooled connections per process to Bedrock |
| `DYNAMODB_MAX_POOL_CONNECTIONS` | request ceiling | pooled connections per process to DynamoDB |

**Beyond the ceiling, requests still queue rather than being refused.** This
change raises the ceiling; it does not add admission control. `anyio`'s limiter has
no wait bound, so offered load above what the fleet admits produces exactly the
original symptom — every request returns 200 and only latency grows. A gateway that
answered 429 or 503 at its limit would tell the caller something it could act on,
and that is not implemented here. Until it is, the ceiling has to be sized above
the load rather than relied on to shed.

The mantle connection pool is the one place with a bound (10 s), because a pool
wait happens inside the reserve/settle window and holding a customer's balance on
our own queue is worse than failing the request.

The two surfaces are bounded differently, which is worth knowing before reading a
saturation graph. Sync routes are limited by the thread ceiling, so admission is
explicit. Async routes — streaming, and the Responses surface — have no per-task
admission limit at all: what bounds them is `MANTLE_MAX_CONNECTIONS` plus that 10 s
pool wait, so beyond the pool they fail with a 502 rather than queue. Same absence
of admission control, different symptom.

Two things that were checked rather than changed: `/health` is an `async` route, so
it is not behind the sync-route limiter and cannot be starved by chat traffic at
the ceiling; and the target group now routes by least outstanding requests rather
than round robin, because with request durations from milliseconds to minutes the
in-flight count is what saturates a task.

## What the change measured

The same sweep, re-run against the deployed change on 2026-08-24 with eight tasks
of 1024 CPU units:

| Concurrency | Before | After | Direct, same run |
| --- | --- | --- | --- |
| 1 | 1.4 req/s, p50 547 ms | 1.6 req/s, p50 597 ms | 3.4 req/s, p50 306 ms |
| 64 | 4.2 req/s, p50 1663 ms | 61.7 req/s, p50 628 ms | 49.2 req/s, p50 442 ms |
| 256 | 4.6 req/s, p50 6223 ms | 64.4 req/s, p50 1479 ms | 27.5 req/s, p50 525 ms |
| 512 | 3.6 req/s, p50 9903 ms | 48.4 req/s, p50 2741 ms | 8.5 req/s, p50 954 ms |

Fourteen times the throughput, and at 64 concurrent the gateway now serves more
than the direct arm managed in the same window, so at that level it is no longer
what limits the caller. Low concurrency looks unchanged for a reason: with eight
tasks and least-outstanding-requests routing, a handful of requests land on
different tasks and each pays its task's first handshake. Pooling shows up once
there are more requests than tasks.

`BACKEND_REQUESTS_PER_TARGET` follows from those numbers. Per task it is 7.7 req/s
at 64 concurrent with latency still at its unloaded value, 8.0 req/s at 256 with
latency 2.4x higher, and 6.0 req/s at 512 with latency worse again — saturation is
around 8 req/s per task. Holding **300 requests per minute per task** (5 req/s)
keeps a task at about 62% of that, which is where latency is still flat.

## Reaching the target, and what it costs

With the WAF ceiling raised and the caller's budget topped up, the sweep reached
1024 on 2026-08-25: 1017 of 1024 requests returned 200, with no 403 and no 402. Of
the remaining seven, six were the load generator's own socket exhaustion — the
direct arm hit the same six at the same concurrency — and one was a 504 on an
upstream call that took longer than the 30 s CloudFront allows.

| Concurrency | Per task | Gateway | Direct, same run |
| --- | --- | --- | --- |
| 8 | 1 | 10.2 req/s, p50 361 ms | 18.6 req/s, p50 312 ms |
| 64 | 8 | 42.3 req/s, p50 549 ms | 11.7 req/s, p50 332 ms |
| 256 | 32 | 81.1 req/s, p50 1331 ms | 147.9 req/s, p50 511 ms |
| 512 | 64 | 91.9 req/s, p50 2791 ms | 107.8 req/s, p50 958 ms |
| 1024 | 128 | 31.7 req/s, p50 7706 ms | 38.8 req/s, p50 1873 ms |

Peak throughput is 91.9 req/s at 512, twenty times the 4.5 req/s this started at.
But latency tracks *per-task* concurrency, not total: 390 ms at 4 per task, 549 ms
at 8, 1331 ms at 32, 2791 ms at 64, 7706 ms at 128. Admitting a request and serving
it at the upstream's own latency are different things, and the ceiling bounds the
first.

**Where the time went was our own accounting, and the reason was a pool of ten.**
The per-phase timing said reserve p50 1201 ms and p95 3623 ms against an upstream
p50 of 317 ms, while DynamoDB's own UpdateItem latency was 3-4 ms with no
throttling and no conditional-check failures. What closed the gap was the log: 1,010
instances of `Connection pool is full, discarding connection:
dynamodb.us-east-1.amazonaws.com. Connection pool size: 10`.

The shared DynamoDB resource had no `Config`, so it kept botocore's default of ten
connections while up to 128 requests per process used it. Past the tenth concurrent
call, urllib3 opened a connection and discarded it on release, so every reservation
and settlement paid a fresh TLS handshake. Throughput collapsing from 91.9 req/s at
512 concurrent to 31.7 at 1024, rather than plateauing, is that: per-request cost
growing with concurrency. This was the same defect already fixed for the Bedrock and
mantle clients, missed on the client that carries twice the traffic.

An earlier revision of this document blamed the GIL. That was wrong, and worth
recording as wrong: on a one-vCPU task, GIL serialization and CPU saturation are the
same phenomenon and cannot be told apart, and the observation the diagnosis rested
on — latency scaling with threads per process — is equally consistent with a
per-process connection pool being oversubscribed, which is what it was.

With the pools sized, the same sweep on eight 4-vCPU tasks running four workers each
with a 32-request ceiling per process:

| Concurrency | Before pools | After pools | Direct, same run |
| --- | --- | --- | --- |
| 64 | 42.3 req/s, p50 549 ms | 80.6 req/s, p50 435 ms | 114.4 req/s, p50 344 ms |
| 256 | 81.1 req/s, p50 1331 ms | 183.9 req/s, p50 811 ms | 36.1 req/s, p50 605 ms |
| 1024 | 31.7 req/s, p50 7706 ms | 226.6 req/s, p50 3326 ms | 151.1 req/s, p50 1853 ms |

1018 of 1024 requests returned 200 at the target, no pool-full warnings remain, and
throughput is fifty times where this started. Per phase: total p50 705 ms, reserve
341 ms, upstream 266 ms, settle 38 ms, unaccounted 0.6 ms.

Two things stay true from the wrong diagnosis, because they were measured rather
than inferred. Latency does track requests in flight per process, so the ceiling
sits near the knee of that curve — 32, which is below anyio's own default of 40 —
and the fleet grows by processes rather than by threads: `GATEWAY_UVICORN_WORKERS`,
one per vCPU, because workers beyond the core count escape nothing. And reserve at
341 ms is still two orders above DynamoDB's service time, so it is where to look
next.

## Three limits that are not capacity

The sweep could not reach 1024, and what stopped it was policy rather than
capacity — all three are worth knowing before sizing anything against this
gateway.

**The WAF rate rule, since fixed.** It allowed 300 requests per five minutes per
source IP — one per second sustained — for all traffic. The 1024 stage returned
403 for 1018 of its requests while the service itself was serving 60 req/s
comfortably. The reasoning behind 300 was that an LLM request takes seconds, so
1 req/s per IP could not impede normal use; that is false for the clients this
gateway exists to serve, because an aggregator in front of it — a semantic router,
a benchmark harness, a CI fleet behind one NAT — is one address carrying many
users' traffic.

A per-IP rate rule is the wrong instrument for that traffic, and the gateway
already has the right one: per-user token quotas and per-tenant dollar pools bound
cost per identity rather than per address. So the rule is now two rules.
Authenticated requests get a ceiling derived from the concurrency target
(`impliedRatePer5Min`: a client holding the target in flight, each request no
faster than the fastest measured p50, produces at most that many per window), whose
remaining job is to bound a flood. Requests with no usable `Authorization` header
keep the tight 300, because with no user to charge the address is the only key
available.

**The caller's token budget, and how it bounds concurrency.** Reservations are held
concurrently and each one is at least 1024 tokens, so a single user cannot have more
than `remaining_credit / 1024` requests in flight — regardless of what the fleet
can serve. Driving 1024 concurrent from one identity needs roughly 1.05M tokens of
headroom held at the peak, even though the settled cost of those requests is a few
tens of thousands. The 256 and 512 stages returned 402 `personal_budget_exhausted`
for exactly this reason. Settlement itself is correct: one request measured 37
tokens of usage and 37 of credit.

**Neither says anything about how many requests a task can hold**, which is what
the ceilings above are about.

## Why scaling tracks requests, not CPU

The service already had CPU target tracking at 70% and it never fired during the
sweep. A request here spends most of its life waiting on Bedrock, so a task can be
saturated — every worker held, latency climbing — while average CPU stays low. It
measured 25-29% average while throughput had already flattened and p50 latency had
grown eighteen-fold.

Requests per target moves with offered load, so it sees the queue that CPU cannot.
It is now the primary policy, with the CPU policy kept as a second signal for work
that is genuinely compute-bound. Scale-in cooldown is longer than scale-out:
removing a task while load is still arriving costs a cold start before the
replacement serves anything.

`BACKEND_REQUESTS_PER_TARGET` ships unset, and while it is unset the request
policy is not registered at all — CPU tracking is the only signal. The only
defensible value comes from a sweep against the deployed task size and request
path, and the measurement that exists predates both the larger task and connection
pooling. Setting it from that number would size the fleet from a figure this design
invalidated. Derive it from a fresh sweep, keep it below the per-task saturation
point so tasks are added while latency is still flat, and re-derive it whenever the
task size or the request path changes.

**A rate signal does not see long-lived streams.** A thousand open streams with a
low arrival rate keep every worker busy while `RequestCountPerTarget` reads low, so
the scaler will not react. Neither predefined metric — requests per target or
average CPU — measures in-flight work. A deployment whose traffic is mostly
streaming needs a custom metric that does: the per-task in-flight count published
from the application, tracked with `scaleToTrackCustomMetric`. That is not in this
change, and until it is, streaming-dominated load has to be sized by the floor
rather than left to the scaler.
