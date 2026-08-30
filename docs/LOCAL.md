# Running locally, against your own Bedrock

This is the fastest way to see the gateway do its one job — reserve, call a
real model, settle, and record — without deploying anything to AWS. DynamoDB
runs on your laptop; every inference call still goes to your real Amazon
Bedrock account. There is no mock inference provider anywhere in this setup.

```bash
make up      # DynamoDB Local + gateway, all tables, one seeded API key
make demo    # calls all three inference routes against REAL Bedrock
make prove   # replays the Z3 formal proofs (no network, no AWS)
make down    # stop everything, wipe the local ledger
```

`demo-offline` is a separate, AWS-free path — see [below](#no-aws-account-use-demo-offline).

## What you need before `make up`

- Docker or [Finch](https://github.com/runfinch/finch) with its VM running.
  `docker compose` and `finch compose` both work — the Makefile picks
  whichever binary it finds, preferring `docker`.
- **Python 3 on the host with the backend's runtime dependencies**:

      python3 -m pip install -r backend/requirements.txt

  `make up` and `make demo` run the scripts under `scripts/local/` on the host,
  not inside a container, because they talk to DynamoDB Local directly and
  import the backend's repositories. `make up` checks for this first and prints
  the command above rather than failing with an `ImportError` three steps later.
  (`make prove` additionally needs `backend/requirements-dev.txt` for z3-solver.)
- AWS credentials that can call Bedrock, resolvable via the standard chain
  (`~/.aws/credentials` or an SSO profile in `~/.aws/config`). If you use AWS
  SSO, run `aws sso login` **before** `make up` / `make demo` — the container
  reads your `~/.aws` directory read-only and can use a cached SSO token, but
  it cannot open a browser to refresh an expired one.

  Only Bedrock needs them. Requests to the local store are signed too, but it
  ignores the signature, so if the host has no credentials at all the scripts
  fall back to AWS's published example key pair and say so. That covers table
  creation and seeding on a machine with no AWS account; `make demo` still
  needs real credentials, because Bedrock is real.
- Model access enabled in the Bedrock console for the models `make demo`
  calls (below), in the regions those models use.
- **Bedrock calls are billed by AWS as normal.** `make demo` sends three tiny
  requests (a few tokens each); nothing here waives that cost.

## Environment variables this mode sets, and why

`docker-compose.yml` sets these for the `gateway` service. If you run the
backend some other way (outside compose), set them yourself:

| Variable | Value | Why |
|---|---|---|
| `ENVIRONMENT` | `development` | Anything other than `production` skips the Cognito/OIDC/CORS hard-requirement checks in `backend/main.py` — this mode never uses Cognito, it authenticates with a `sk-stratoclave-*` API key. |
| `STRATOCLAVE_CODEX_ENABLED` | `true` | Off by default in a deployment (`backend/mvp/openai_responses.py`), and set explicitly here because a local demo wants all three routes. Without it, `/openai/v1/responses` returns `503` — `make demo` would silently be a two-route demo instead of three. |
| `AWS_ENDPOINT_URL_DYNAMODB` | `http://dynamodb-local:8000` | The one line that makes DynamoDB local. Read by botocore directly; no application code change. Bedrock has no such override — inference stays real. |
| `AWS_PROFILE` | your shell's `AWS_PROFILE`, else `default` | Which credentials the gateway container uses to call Bedrock. |
| `AWS_REGION` | your shell's `AWS_REGION`, else `us-east-1` | Region for the DynamoDB Local client and any AWS client that doesn't pick its own region per call. |
| `HOME`, `AWS_CONFIG_FILE`, `AWS_SHARED_CREDENTIALS_FILE` | `/app`, `/app/.aws/config`, `/app/.aws/credentials` | Point boto3's config/credentials resolution — and the SSO token cache under `~/.aws/sso/cache`, which only follows `HOME` — at the read-only-mounted `~/.aws`. |

Override `AWS_PROFILE` / `AWS_REGION` on the command line:
`make up AWS_PROFILE=my-bedrock-profile AWS_REGION=us-west-2`.

## What `make demo` actually calls

| Route | Model sent | Notes |
|---|---|---|
| `POST /v1/messages` | `claude-haiku-4-5` | Anthropic Messages API, via Bedrock Converse |
| `POST /v1/chat/completions` | `claude-haiku-4-5` | OpenAI Chat Completions shape, same Bedrock backend |
| `POST /openai/v1/responses` | `openai.gpt-5.6-sol` | OpenAI Responses shape, via `bedrock-runtime` |

These are aliases resolved by `backend/mvp/models.py` (`_ALIAS_MAP`) — not raw
Bedrock model IDs. `GET /v1/models` on your running gateway lists every
accepted alias if you want to try others.

**Enable model access for these specific models** (Bedrock console → Model
access) in whatever region your deployment's model registry entry points at,
before running `make demo`.

### IAM: do not assume `bedrock:InvokeModel` alone is enough

The production task role's actual Bedrock policy
(`iac/lib/ecs-stack.ts`, search `bedrock:InvokeModel`) grants, on the
Anthropic-model ARNs:

```
bedrock:InvokeModel
bedrock:InvokeModelWithResponseStream
bedrock:Converse
bedrock:ConverseStream
```

scoped to `arn:aws:bedrock:*::foundation-model/anthropic.*` and to the
`us./apac./eu./global.anthropic.*` inference-profile ARNs in your account.
Depending on how the model is invoked (streaming vs not, direct
foundation-model ARN vs a cross-region inference profile), you may need more
than a bare `InvokeModel` grant. **This doc does not assert that this list is
either the minimum or the complete set for your own IAM setup** — read
`iac/lib/ecs-stack.ts` directly for the authoritative, current policy, and
adjust your own principal's permissions to match what your models and
invocation style actually require. The IAM story for the `bedrock-runtime`
OpenAI-compatible route (`openai.gpt-5.6-sol`) has not been verified here at
all; if that call fails with an access-denied error, check your account's
`bedrock-runtime` access separately.

## What this setup verifies, and what it does not

**Verifies:** the request lands on real Bedrock; the response is exactly what
the model returned; the effective, resolved model name (not just the alias
you sent) is recorded in your local `UsageLogs` table; the per-user token
quota in `UserTenants` moves by exactly the tokens the response reported;
`GET /v1/messages`, `/v1/chat/completions`, and `/openai/v1/responses` all
work end to end against the real model provider.

**Does not verify** — read this list before treating a green `make demo` as
more than it is:

- **Ledger behaviour under contention.** This is the single most important
  property this project claims (the whole pitch is a proven invariant on a
  budget reservation), and a single-user, single-machine, single-request-at-
  a-time `make demo` run says nothing about it. Concurrent reservations,
  `TransactionConflictException` retries, and DynamoDB throttling under real
  load are not exercised. If you need evidence for that property, see
  `docs/EVIDENCE.md`'s `deployed-live` tier, not this file.
- **The deployed entry path.** No CloudFront, no ALB, no TLS termination, no
  WAF, no Cognito. Everything that "who is allowed to even reach the
  gateway" depends on in production is absent here.
- **API key issuance/revocation at the edge**, and any latency, caching, or
  propagation behaviour of that path — the seeded key here is written
  directly via the same repository code the app uses, not through the normal
  issuance API a real user would hit.
- **DynamoDB-Local-specific transaction semantics.** DynamoDB Local
  approximates, but is not byte-identical to, real DynamoDB's transactional
  guarantees (and, separately, does not actually expire TTL items — see
  below). Passing here is evidence the *wiring* is correct, not that real
  DynamoDB behaves identically under the same load.
- Multi-user contention, tenant isolation, scaling, and anything about
  running this in a way that isn't a single developer on a single laptop.

### If every DynamoDB call hangs, look at the volume's owner

DynamoDB Local answers plain HTTP as soon as the JVM is up, so its container
looks healthy while every *operation* against it times out. Its own log says
what is wrong:

    WARNING: [sqlite] cannot open DB[1]: ... [14] unable to open database file
    WARNING: [sqlite] SQLiteQueue[shared-local-instance.db]: stopped abnormally

The image has no `/home/dynamodblocal/data` directory, so a container runtime
mounting a fresh named volume there creates it as **root**, while the image
runs as uid 1000 and cannot write its database. `docker-compose.yml` runs that
one container as root for this reason. If you change that line, or mount your
own path, this is the failure you get — and the gateway will retry the write
for minutes rather than fail, so `make up` times out waiting for health with
nothing in the gateway's log but a read timeout.

### A known DynamoDB Local limitation

TTL is *enabled* on every table that has one (`create_tables.py` calls
`update_time_to_live`), but DynamoDB Local does not actually expire TTL'd
items — they simply accumulate until you `make down`. This does not affect
`make demo`, but do not use it to test TTL cleanup behaviour.

### Latency here says something, but only about a single caller

Measured on `/v1/chat/completions`, one request at a time, from the gateway's
own `request_timing` line:

| Store | `reserve_ms` | `settle_ms` | `upstream_ms` | `total_ms` |
| --- | --- | --- | --- | --- |
| DynamoDB Local 2.x, `-sharedDb` | 13.7 | 13.3 | 1217.2 | 1255.6 |
| moto, standing in for it | 9485.2 | 3866.3 | 1571.7 | ~14900 |

So with DynamoDB Local the ledger costs about 27 ms of a 1.26 s request and
Bedrock dominates — the same shape as the deployed stack, which took 1112 ms
for the same call. **With a stand-in it is not the same shape at all**: moto
made reserve alone ~690× more expensive and swamped everything else.
`TransactWriteItems` and the conditional-CAS retry loop are exactly where a
stand-in diverges most, so if you substitute one, treat its timings as
meaningless rather than merely inflated.

What no local number can tell you is behaviour under load. These are
single-caller figures against a single-node store with no throttling and no
partition contention; the reserve path's cost under concurrency is a property
of the service, not of this laptop. See the section below.

The split being visible at all is the useful part: `reserve_ms` /
`settle_ms` / `upstream_ms` / `unaccounted_ms` are emitted per request, so a
slow run points at one of the three rather than needing a guess. Note that
only `/v1/chat/completions` emits this line today; the Messages and Responses
routes do not.

### `make prove` proves a model, not this codebase

The Z3 tests replayed by `make prove` prove properties of an **encoding** of
the billing logic (see the axioms stated at the top of
`backend/tests/test_billing_formal_z3.py` and its siblings) — they do not run
against DynamoDB Local, moto, or any live table, and a passing proof says
nothing about whether the Python implementation faithfully matches the
encoding. That correspondence is what the rest of the test suite (and, above
that, `deployed-live` evidence) is for.

## Verification status of this local-mode setup itself

Table creation (23 tables, 11 GSIs), idempotent app-level seeding
(permissions + default tenant, zero `ResourceNotFound` errors), local user +
tenant-membership + API-key seeding, and one full round trip on all three
inference routes against real Bedrock (with the effective model and token
counts read back from the local ledger) have all been run and observed to
work against **DynamoDB Local 2.x itself** (`-sharedDb`), with the backend run
natively from a Python 3.11 venv.

The container path is covered too: `make up` followed by `make demo` has been
run from a clean state through `finch compose` (nerdctl), building
`backend/Dockerfile`, and all three routes returned 200 from real Bedrock with
the ledger read back out of the containerised store. The equivalent under
`docker compose` is what the `compose` job in
`.github/workflows/e2e-nightly.yml` covers; the two runtimes share the compose
file but not the builder, so neither run substitutes for the other.

What no local run says anything about is behaviour under concurrency — a
single-node store with no throttling
cannot. If you hit something that does not match this document when running
`docker compose up` / `finch compose up`, that build path is the more likely
place to look than this file.

## No AWS account? Use `demo-offline`

`make demo-offline` runs the existing offline Savings Report demo
(`bench/savings/demo_offline.py`) over a checked-in, synthetic workload. No
AWS account, no Docker/Finch, no network. It shows the *shape* of the
reproducible-computation claim (`summarize_savings(rows, price=, resolve=)`
taking its price table and resolver as explicit arguments) without touching
a real gateway, a real ledger, or real Bedrock traffic. See
`docs/demo/savings-vs-litellm.md` for what that demo does and does not claim.
