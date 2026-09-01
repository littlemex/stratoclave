<!-- Last updated: 2026-08-31 -->
<!-- Applies to: Stratoclave main -->

# Contract: where a rate comes from

The rate table decides what a request is admitted at and what it settles for, so
"where did this number come from" is a question the ledger has to be able to answer.
This document is the normative source for that answer: the layers a rate is resolved
through, what each provider actually publishes (measured, not assumed), and the rules
that keep a missing number from becoming a cheaper one.

Code: [`backend/mvp/pricing_feeds/`](../../backend/mvp/pricing_feeds). Tests:
`test_pricing_feeds_dimensions.py`, `test_pricing_feeds_composite.py`,
`test_pricing_feeds_snapshot.py`, `test_pricing_floor.py`, and the two opt-in live
checks `test_pricing_feeds_live_apis.py` and `test_pricing_feeds_prefixes.py`.

A feed answers one question — "what does the provider publish for these model ids?" —
and takes a single `FeedRequest` (ids, the regions this deployment can dispatch to, a
deadline). It never raises: a network error, a permission error, a renamed field and an
unreadable price all come back as *absence*, because absence is what the layers below
are there to cover. Adding a provider is a new feed and one line in `_default_feeds`.

## 1. The ladder

```
admin override         PricingConfig table, highest precedence          (mvp/pricing.py)
  ^
a fresh fetch          the price APIs, on the feed interval             (pricing_feeds/)
  ^
the current version    DynamoDB, read once when a task starts           (pricing_feeds/snapshot.py)
  ^
the bundled floor      defaults/pricing.json, no network, cannot fail   (mvp/price_sources.py)
```

Each rung exists for a failure the rung below has to absorb, and the direction is
always downward. A feed that blips keeps the last table in memory; a task that
restarts reads the snapshot instead of falling back a release-old document; a
provider that renames a dimension loses that one rate rather than the table; a
model nobody publishes a price for keeps the floor. **Absence never lowers a
price** — that is the single property the whole subsystem is built around, and
`test_pricing_feeds_composite.py` is where each of those paths is exercised.

The bundled floor is not a placeholder, and its provenance is recorded per leg rather
than in one blanket sentence. Every input and output leg in
[`defaults/pricing.json`](../../backend/mvp/defaults/pricing.json) is a measured
in-region list price, pinned with its provenance in `test_pricing_floor.py`. The
exceptions are named in the document's own `notes` rather than left for a reader to
discover: the cache legs of `haiku-3`, `grok`, `gemma`, `nemotron` and `qwen` are a
stated conservative upper bound, because none of those providers publishes a cache
rate at all (read priced at the input rate, write at 1.25x it — the same premium the
Anthropic tiers that do publish one actually charge); `gpt-5` is priced at the
dearest measured GPT-5.6 tier so a cheaper one is never under-charged; `default` is
synthetic, built to dominate every other row rather than to describe a price anyone
publishes; and `vllm` is the operator's own cost-recovery figure, not a list price at
all. So a deployment that never enables a feed still charges real, measured prices
for every provider-published leg, and a named conservative bound rather than an
invented number for the rest.

## 2. What each API actually publishes

Measured on 2026-08-31 against the real APIs. The details matter because three of
them are surprising, and a design that assumed otherwise would be wrong in a way
that shows up as a wrong invoice rather than as an error.

### `bedrock:ListFoundationModelAgreementOffers` — the Marketplace-metered families

This is where **every current Claude price** lives, and it is not where anyone looks
first. It answers for Anthropic (all generations), OpenAI GPT-5.6 sol / terra / luna,
Cohere, Stability, Writer, TwelveLabs and Luma, as a structured rate card:

```
$ aws bedrock list-foundation-model-agreement-offers --model-id anthropic.claude-opus-5
offers[0].termDetails.usageBasedPricingTerm.rateCard[]
  { "dimension": "USW2_input_tokens_global_standard", "price": "5",   "unit": "Units" }
  { "dimension": "USW2_input_tokens_standard",        "price": "5.5", "unit": "Units" }
```

- The card is **region-independent**: called from us-west-2 and from ap-northeast-1 it
  returns the same `offerId` and byte-identical dimensions. Region lives inside the
  dimension names, so one call prices every region.
- It answers `ValidationException: Agreement not supported for this model` for the
  families AWS bills directly. That exception is **information, not failure** — it is
  how the split between this feed and the Price List feed is decided at runtime
  instead of being hardcoded.
- Some **legacy** models answer `Your account is not authorized to invoke this API
  operation` even where the current generation answers fine. That is an account fact,
  reported as `price_feed_not_authorized`, and those models keep the floor.
- The response also carries an `offerToken`: the signed blob
  `CreateFoundationModelAgreement` consumes. Reading prices must not become
  subscribing to a paid product, so the token is never stored, logged or returned, and
  the test fixtures have it stripped.

### The Price List API — the AWS-billed families

`pricing:GetProducts` on the **`AmazonBedrock`** offer covers Nova, Titan, Llama,
Mistral, DeepSeek, Gemma, Qwen, NVIDIA, MiniMax, GLM, Kimi, gpt-oss and xAI, as usage
types such as `USE1-xai.grok-4.6-mantle-input-tokens-global-standard`.

Three traps, all handled explicitly in the code:

- The unit here is **per 1K tokens**, while the agreement cards are per 1M. Assuming
  the wrong one is a 1000-fold error, so `dimensions.per_mtok` normalises by the unit string
  and drops a unit it does not recognise.
- The `model` attribute is **not a model id**: it holds a display name for some models
  (`Claude 3 Haiku`) and a raw id for others (`xai.grok-4.6`). The feed keys on the
  usage type, which always embeds the id, and requires the registry's id as the anchor.
- The billed id can carry a segment the registry id does not
  (`qwen.qwen3-next-80b-a3b` is billed as `...-a3b-instruct`).

Bedrock prices are spread across **four** offer codes, which is the reason this was
easy to get wrong: `AmazonBedrock` (legacy on-demand plus the mantle families),
`AmazonBedrockService` (Sonnet 4 / 4.5 / Haiku 4.5 and reserved throughput),
`AmazonBedrockFoundationModels` (the Marketplace-metered rows, where the model is
identifiable only through a `servicename` display name), and `AmazonBedrockAgentCore`.
Commercial-region GPT-5.x appears in none of them; only GovCloud rows exist. The feed
reads `AmazonBedrock` and `AmazonBedrockService` by default — two offers, not one,
because `AmazonBedrock` alone misses Sonnet 4 / 4.5 / Haiku 4.5 — configurable through
`STRATOCLAVE_PRICE_LIST_OFFERS`, and leaves the rest to the agreement API, which covers
the same models with ids instead of display names.

### What no API publishes

**When a promotional price ends.** Every `effectiveDate` in every Bedrock offer reads
as the first of the current month, and there is no end field. A price change is
therefore only visible as a **difference between two fetches**, which is why the
snapshot carries a digest and why `price_table_changed` names the keys that moved.
A deployment that wants the change history keeps its own log of that event; the
gateway does not model a price calendar it cannot see.

**A price on the model card.** `bedrock:GetFoundationModel` carries no price field.
It does carry `modelLifecycle` with `legacyTime` and `endOfLifeTime`, which is a
different useful signal and not one this subsystem consumes today.

## 3. The rules that make it safe in front of money

1. **Input and output must both resolve, or the model is not priced.** Without them
   there is nothing to charge a request with, and taking either from a long-context or
   batch number would price ordinary traffic at another product's rate.
2. **A class the provider prices nowhere falls through, per leg.** Nemotron and Qwen
   publish input and output only; xAI publishes a cache read and no cache write. Those
   legs come from the snapshot, then from the floor — never from zero, because a zero
   leg turns "unpublished" into "free". Provenance records which legs are live:
   `bedrock-price-list(input,output)`. A leg that *was* live and stops being published
   is reported (`price_feed_leg_regression`), because otherwise the stored value —
   possibly a promotional rate that has since ended — is re-published silently for as
   long as the key survives.
3. **A class priced only outside the region or scope asked for is widened, not
   dropped.** Claude Opus 5 publishes its cache legs in-region only, so a deployment
   addressing it through a `global.` profile has no in-scope cache rate; refusing the
   model would send all four legs to the floor, which is the larger error. The dearest
   published number is charged and the widening is reported
   (`price_feed_scope_widened`), because a widened leg means the routing assumption and
   the price list disagree.
4. **Where a choice exists, the dearer number wins.** Across the regions a Converse
   request could fail over to, and across the models that share a pricing key.
4b. **A pass that saw less than the whole picture may raise a rate and never lower one.**
    The published number is a maximum — over the models sharing a key, over the regions a
    request can reach — so a pass that missed a member, could not read a region, or had to
    widen a leg computed that maximum over less than the truth, and is clamped to at least
    what the layer below holds. Completeness is judged **per key from positive evidence per
    member**: every model sharing the key produced a selection, and the feed that answered
    for it did not report its own answer as partial. Not from a flag for the whole pass — a
    pagination limit in an offer that prices nothing we asked about would otherwise freeze a
    model another feed read completely, turning a safety clamp into a permanent over-charge.
    A genuine price drop lands on the next complete pass, which is how Claude Sonnet 5
    listing below Sonnet 4.6 still propagates.
5. **Rounding to integer micro-USD rounds up.** Truncation is a discount nobody granted.
6. **A pricing key is a price point, not a family.** A shared key can only be charged
   at its dearest member, so `opus` covering Claude Opus 4.1 ($15/MTok input) and Opus 5
   ($5.50) would have charged every Opus 5 request at nearly three times its rate. The
   registry now splits them, and a live fetch that finds a key whose models disagree
   emits `price_feed_key_spans_prices` naming the models.
7. **The scope follows the inference profile.** A model addressed through `us.` is
   billed at the in-region rate, `global.` at the global rate, and the two differ by ~10%.
   An id carrying a prefix this build does not recognise — AWS keeps adding them — is
   **rejected at registry load** unless the entry declares `price_model_id`. It is not
   stripped on a guess: `xai.grok-4.6` is a bare id whose second dot belongs to a version
   number, so a "strip the first segment" rule mangles it. Both outcomes of guessing end
   with the model quietly on the bundled floor, so the registry is made to state the id the
   price APIs know instead.
8. **A billed id that differs from the invoked one is declared, never guessed.**
   `qwen.qwen3-next-80b-a3b` is billed as `...-a3b-instruct`, and the registry says so
   in `price_model_id`. The alternative — absorbing unexpected segments after a prefix
   match — cannot tell a variant of the same model from a dearer sibling, and would let
   `xai.grok-4` be charged at `xai.grok-4.6` rates.
9. **The default cache TTL is the base product.** Bedrock's prompt cache lives 5
   minutes by default, so `cache_write_tokens_5m_standard` maps to the cache-write leg
   while `_1h_` and `_30m_` (different products, up to double) are excluded. Dropping
   the default-TTL spelling would leave the leg on the floor while the provider was
   publishing it.
10. **A leg with no number behind it drops the whole key, and never charges zero.** If a
    leg resolves neither from the feeds, nor the snapshot, nor the floor — reachable on a
    deploy where the bundled document cannot be read — the key is left to the layer below
    (`price_feed_key_dropped_unfundable_leg`). Zero would make cached tokens free.
11. **An unknown region set refuses to price, rather than narrowing quietly.** The
    selector takes the maximum over the regions a request could be billed in, so a set
    missing members yields a rate that may be too LOW. If the failover set cannot be
    read, the model keeps the layer below and is named in the report — the one place
    where "we could not tell" must not become "here is a cheaper number".
12. **"Recognised but not charged" is not "unparsed".** Reserved and provisioned
    throughput, model customisation, stored models and non-default cache TTLs come back
    as `EXCLUDED` rather than as a parse failure. Every Claude card carries a 1-hour
    cache write and every model carries provisioned-throughput rows, so counting them
    would leave `unparsed` permanently nonzero and destroy the only signal that a
    *token* price changed shape.

## 3b. Versions are cut on change, and a charge can be recomputed at one

A refresh that reads the same prices writes **nothing**. The stored version's id is the
digest of the table, each version row is written once under `attribute_not_exists`, and a
one-line pointer says which is current:

```
pk = CONFIG#pricefeed, sk = CURRENT              -> {active_version, first_seen_at,
                                                     last_seen_at, previous_version}
pk = CONFIG#pricefeed, sk = __ratefeed__<digest>  -> {rates, provenance, live_classes}
```

So hourly polling does not accumulate rows, "how many versions exist" answers "how many
times prices moved", and `first_seen_at` keeps saying when a stable table appeared rather
than when it was last confirmed. It is the same shape as the admin override rows beside it,
for the same reason: an immutable version is what a dispute is answered against.

Both reads are strongly consistent, and the pointer moves under a compare-and-set fenced on
the version the pass STARTED from. Neither is decoration. The pointer is written after the
version row it names, so an eventually-consistent pair can show a reader a pointer at a
version it cannot see yet — and this module's answer to a missing version is "no stored
version", which drops the whole table to the floor. And the fence has to be the pass's
starting point rather than a timestamp or the value read just before writing: a CAS against a
just-read value has the same hole as no CAS at all, because the late writer reads the winner's
version and then satisfies its own condition, while a clock-based guard hands ordering to
whichever task has the worst clock, where a single future-dated write locks the pointer until
wall time catches up. A pass that loses the fence adopts the stored winner instead of serving
a table nothing else can see.

The history exists because **tokens are the record of origin**. Every terminal money event
carries, per leg, the token count and the rate it was charged at, so a charge is arithmetic
over recorded facts rather than an opaque number. Two replays follow from that, and the
difference between them is the point:

- `dynamo/credit_ledger.py::rating_replay_mismatches` replays each event against **its own**
  rate and proves the arithmetic still reproduces. A healthy ledger returns nothing.
- `mvp/reprice.py` replays the same tokens against a **different** table — a stored
  version, the current effective table, or the floor — and reports as-charged against
  as-repriced, broken down per pricing key. As-charged is the ledger's own settled delta,
  not the rating's self-report: they must agree, and when they do not, the money that moved
  is the charge of record and the rating is the thing in doubt. Every money event in the
  period is counted whether or not it carries a rating, and the report says whether it is
  `complete`, because a difference measured over part of a period is a different question
  from the one that was asked:

```bash
python -m mvp.reprice --tenant acme --period 2026-08 --at-version <digest>
```

It writes nothing. The charge of record stands; a correction would be a new idempotent
adjustment event, which is a money-path change with its own contract rather than something
a reporting tool does. That is what makes the tool safe to run against production, and it
is why a price found to have been wrong for a week is a report and a decision instead of an
archaeology exercise.

## 4. Self-hosted capacity travels the same road

The vLLM transport seam has no list price — nobody publishes one — so its rate comes
from a document the operator writes, keyed by `endpoint_key` because cost recovery is
a property of the capacity rather than of the model:

```json
{
  "schema_version": 1,
  "rates": {
    "pool-a": {"input_per_mtok_usd": "0.20", "output_per_mtok_usd": "0.20",
               "notes": "g6e.2xlarge reserved, amortised at the occupancy we measured"}
  }
}
```

Point `STRATOCLAVE_SELFHOSTED_RATES_PATH` at it. It is a feed like the other two, so
the number is parsed once, snapshotted, labelled with its provenance and visible in
the same admin view — one pipeline, three sources. Cache legs are published as an
explicit zero: vLLM reports no Bedrock-style cache split, so those tokens are already
counted as input.

**The gateway will not derive this rate.** It does not turn an hourly cost and an
assumed throughput into a per-token price. Occupancy is a measurement, and a per-token
rate derived from a latency figure is how a cost model ends up an order of magnitude
out; if you want an amortised number, measure the tokens your pool actually serves in
an hour and put the result in this document. That the number is the operator's is not a
gap — it is the same boundary the rest of the gateway keeps: the mechanism is ours, the
measurement is yours.

## 5. Operating it

Nothing is live by default; the source has to be registered and selected. The
commands below run from the repository root. `cd backend` is the first of them
because the pricing-feeds module is not importable from anywhere else — everything
after it is a plain `python -m` invocation, not a path trick. They need AWS
credentials carrying `bedrock:ListFoundationModelAgreementOffers` and
`pricing:GetProducts`, plus read (and, for `--apply`, write) access to the
pricing-config table named by `DYNAMODB_PRICING_CONFIG_TABLE` (falls back to
`stratoclave-pricing-config`), and a region to call through: `AWS_REGION` for the AWS
SDK generally and `STRATOCLAVE_REGION` for the feeds' own endpoint choice, both
falling back to `us-east-1` if unset. In production the credential is the task role's,
not an operator's, because `--apply` runs on whatever schedule the deployment gives it
rather than at a terminal:

```bash
cd backend
export STRATOCLAVE_PRICE_SOURCE=bedrock-live
python -m mvp.pricing_feeds.fetch
python -m mvp.pricing_feeds.fetch --apply
```

The first invocation is a dry run: it fetches, diffs against the stored snapshot, and
stores nothing. Fill the snapshot with `--apply` at deploy time so no task ever races
the feeds on its first request. The task role's own IAM needs the same two price
actions and read/write on the pricing-config table it already has. Both price APIs
are read-only; `bedrock:CreateFoundationModelAgreement` is deliberately **not**
granted, and must not be.

Knobs, all env-tunable and read from the deployment environment:

- `STRATOCLAVE_PRICE_FEED_INTERVAL_SECONDS` (default 3600) — how often the pricing
  cache asks a source for a fresh table. The cache itself polls a source every 60 s,
  and calling two AWS APIs per registered model on that cadence would be a
  self-inflicted throttle for data that moves monthly.
- `STRATOCLAVE_PRICE_FEED_BUDGET_SECONDS` (default 15) — the ceiling on one
  request-path pass. The first fetch after a cold start with no snapshot runs on the
  request path, so it stops at the budget with a partial table rather than making a
  caller wait, and says so.
- `STRATOCLAVE_PRICE_FEED_REFRESH_BUDGET_SECONDS` (default 300) — the ceiling on a
  background `refresh()`, off the request path, where a caller waiting is not the
  constraint and a fuller table is worth the extra time. **This is the one the CLI below
  obeys**, since the CLI is a refresh; the request-path budget above has no effect on it.
- `STRATOCLAVE_PRICE_FEED_STALE_AFTER_SECONDS` (default 24 h) — a label, never an
  expiry; expiring a snapshot would change the amount charged with nobody deciding to.
- `STRATOCLAVE_PRICE_FEED_WORKERS` (default 8) — the agreement feed's thread pool.
  One call per model, and a registry of twenty models took roughly 40 s run in
  sequence — long enough to spend the request-path budget with most models unasked.
- `STRATOCLAVE_REGION` — the deployment's own region, read by both price feeds:
  the fallback catalogue region for the Price List feed when no candidate regions are
  passed in, and the endpoint `agreement.py` calls (that API is region-independent, so
  this is an operational choice, not a pricing one). Defaults to `us-east-1`.
- `STRATOCLAVE_PRICE_LIST_REGION` (default `us-east-1`) — which regional endpoint the
  Price List feed calls. The Price List API is served from only a few regions and is
  a global catalogue, so this is an availability choice and never changes the answer.
- `STRATOCLAVE_PRICE_LIST_OFFERS` (default `AmazonBedrock,AmazonBedrockService`) —
  which offer codes the Price List feed reads.
- `STRATOCLAVE_PRICE_LIST_MAX_PAGES` (default 40 per region) — the pagination scan is
  bounded so a catalogue that grows by an order of magnitude cannot turn a price
  refresh into an unbounded scan.
- `STRATOCLAVE_PRICE_FEED_UNPRICED_ALLOWLIST` — a comma-separated list of model ids an
  operator has accepted as unpriced. A model on it does not trip `--strict`; empty or
  unset allows none.

The fetch never runs while the source's lock is held, so a slow feed cannot stall
concurrent readers, who go on serving the table already in memory rather than waiting
on it. `--strict` on the
CLI exits 2 on exactly these reasons, printed by name: `key_spans_prices`,
`leg_regression`, `coverage_regression`, `budget_spent`, `feed_not_authorized` and
`unpriced_not_allowlisted`. The first four are a pass that found something a person
has to act on; the last two are new: a whole feed the account could not read at all,
and a model left unpriced that is not on the accepted allowlist — so `--strict` is a
gate against a half-denied pass as well as a partially-stale one.

Events worth alerting on: `price_feed_coverage_regression` (a key that used to be
readable is not any more — the signature of a renamed API), `price_feed_unparsed_names`
(a grammar this build cannot read), `price_feed_key_spans_prices` (split the key),
`price_feed_leg_regression` (a leg that was live and stopped being published, so the
stored value is now serving on trust rather than on a fresh answer),
`price_feed_table_partial` (a pass that stopped before it had asked about everything,
so the table it produced is partial and every key it did not reach is answered by the
layer below — this is the one that fires when a fetch runs out of time, which is why
it is the event an operator sees when a knob needs changing rather than a provider),
`price_feed_fetch_empty` (a pass that read nothing against a snapshot that is not
empty, naming the stored key count and the snapshot's age), and `price_table_changed`
(a price moved).

## 6. What this does not guarantee

- **Automatic following of market prices for every model.** A model no feed covers, or
  whose offer this account may not read, keeps the floor. The fetch report names those
  models rather than leaving it to be discovered in an invoice.
- **Detecting the end of a promotional price on the day it ends.** No API publishes an
  end date; the change is seen at the next fetch, within the feed interval.
- **That the published list price is what your account pays.** Private pricing,
  credits and commitments live outside the Price List. The ledger's charge of record is
  what the gateway computed from these rates; reconciling it against the bill is the
  operator's job, and `docs/design/charge-loss.md` covers the related question of
  attempts the gateway did not observe.
- **Tiers other than standard.** `flex`, `priority` and `batch` are different products
  a caller has to ask Bedrock for explicitly, and this gateway does not. Their rate
  names are recognised and excluded rather than averaged in.
- **The long-context rate band.** Bedrock charges a request past a model's long-context
  threshold at a higher rate per leg — for Sonnet 4.6, double the standard input rate —
  and the rate table holds one rate per leg, so such a request is charged at the standard
  rate. It is the only systematic UNDER-charge here, it is deliberate (folding the
  long-context number into the standard slot would over-charge every ordinary request
  instead), and it is on the open-items list in
  [`CONTRACTS.md`](CONTRACTS.md) with the change named: a leg per context band through the
  rate type, the estimator and the settle path.
