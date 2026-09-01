"""Do the real APIs still speak a grammar this build can read?

The offline tests parse fixtures captured on 2026-08-31, which proves the parsers are
right about what AWS emitted *then*. This is the test that fails when AWS changes it.
It is skipped without an opt-in flag, so the ordinary suite stays offline:

    STRATOCLAVE_LIVE_PRICE_TESTS=1 AWS_PROFILE=... pytest tests/test_pricing_feeds_live_apis.py

What it asserts is deliberately narrow. Not "the price is $5.50" — that is AWS's to
change and `tests/test_pricing_floor.py` is where a move gets noticed — but that the
pipeline still *resolves* the models this deployment serves, and that the names it
could not parse are all names that are not per-token prices in the first place.
"""
from __future__ import annotations

import os
import time

import pytest

from mvp.models import registry_entries
from mvp.pricing_feeds.composite import LivePriceSource
from mvp.pricing_feeds.dimensions import base_model_id
from tests.live_aws import real_session

_FLAG = "STRATOCLAVE_LIVE_PRICE_TESTS"
# Model families whose price this deployment does not need from a feed: the
# self-hosted seam is priced by the operator, and a virtual router pool is never a
# charge of record.
_NOT_FEED_PRICED = {"vllm", "semantic-router"}
# Models known to have no readable price, with the reason. A ratchet in both
# directions: a model outside this set that stops being priced fails the first test,
# and a model inside it that starts being priced fails the second, so the list cannot
# quietly grow into a blanket excuse. Measured 2026-08-31 on account 776010787911;
# the authorization ones are account-scoped and may differ in yours.
EXPECTED_UNPRICED = {
    "anthropic.claude-3-opus-20240229-v1:0":
        "ListFoundationModelAgreementOffers rejects the id as invalid, and the Price "
        "List carries this model only under the AmazonBedrockFoundationModels offer, "
        "whose rows name their model by display name rather than by id",
}


class _NoStore:
    """Reads and writes nothing: a live parse check must not touch the snapshot the
    running deployment charges from."""

    def load(self):
        return None

    def save(self, rates, provenance):
        return None


class _Entry:
    """A minimal stand-in for a `mvp.models.ModelEntry`, carrying only the
    attributes `LivePriceSource` reads (`bedrock_model_id`, `pricing_key`,
    `bedrock_region`, `wire_protocol`, `price_model_id`, `virtual`). `wire_protocol`
    defaults to `"responses"` so `composite._candidate_regions` answers from
    `bedrock_region` alone, without reaching into `routing.chains`' failover policy —
    which is a different subsystem's business, not this one's."""

    def __init__(self, bedrock_model_id: str, pricing_key: str, *,
                bedrock_region: str = "us-east-1", wire_protocol: str = "responses",
                price_model_id=None, virtual: bool = False) -> None:
        self.bedrock_model_id = bedrock_model_id
        self.pricing_key = pricing_key
        self.bedrock_region = bedrock_region
        self.wire_protocol = wire_protocol
        self.price_model_id = price_model_id
        self.virtual = virtual


@pytest.fixture(scope="module")
def report():
    if not os.getenv(_FLAG):
        pytest.skip(f"set {_FLAG}=1 (with AWS credentials) to check the live APIs")
    boto3 = pytest.importorskip("boto3")
    # `tests/conftest.py` plants dummy credentials so no test reaches AWS by accident;
    # a live check opts out of that by naming its profile and handing the clients in.
    from mvp.pricing_feeds.agreement import AgreementFeed
    from mvp.pricing_feeds.price_list import PriceListFeed
    from mvp.pricing_feeds.selfhosted import SelfHostedFeed

    session = real_session(boto3)
    if session is None:
        pytest.skip("no real AWS credentials available")
    region = os.getenv("STRATOCLAVE_REGION") or "us-east-1"
    feeds = (
        AgreementFeed(session.client("bedrock", region_name=region)),
        PriceListFeed(session.client("pricing", region_name="us-east-1")),
        SelfHostedFeed(),
    )
    source = LivePriceSource(feeds, store=_NoStore(), interval_seconds=0)
    result = source.refresh()
    if not result.rates and result.feed_errors:
        pytest.skip(f"price APIs unreachable with these credentials: {result.feed_errors}")
    return result


def test_the_registry_models_this_deployment_serves_are_still_priced(report):
    expected = {
        base_model_id(e.bedrock_model_id) for e in registry_entries()
        if not getattr(e, "virtual", False)
        and getattr(e, "served_by", "bedrock") not in _NOT_FEED_PRICED
    }
    # A model this account may not read is excluded: that is a permission to grant,
    # and counting it here would make the grammar check cry wolf on every deployment
    # whose account cannot read a legacy model's offer.
    unpriced = ({base_model_id(m) for m in report.unpriced}
                - {base_model_id(m) for m in report.unauthorized}
                - set(EXPECTED_UNPRICED)) & expected
    # A model AWS has not published a price for at all is the operator's problem, not
    # a parser bug — but a model that WAS priced and now is not is exactly the
    # signature this test exists to catch, so the failure names them.
    assert not unpriced, (
        f"the live feeds no longer price {sorted(unpriced)}: {report.unpriced}. "
        f"Either the rate-name grammar changed (see mvp/pricing_feeds/dimensions.py) or the "
        f"model moved offers."
    )


def test_every_unparsed_name_is_a_product_that_is_not_priced_per_token(report):
    """`unparsed` is the change signal. It has to stay empty of token prices, or a
    renamed dimension would hide inside a count that is always nonzero."""
    samples = [s for names in report.unparsed_samples.values() for s in names]
    for name in samples:
        lowered = name.lower()
        assert any(marker in lowered for marker in (
            "reserved", "tpm", "provisioned", "modelunits", "model_units",
            "customization", "customisation", "storage", "unit=",
        )), (
            f"unparsed rate name {name!r} looks like a per-token price this build "
            f"cannot read. Teach mvp/pricing_feeds/dimensions.py the new shape."
        )


def test_a_key_is_never_published_with_a_zero_leg(report):
    for key, rate in report.rates.items():
        assert rate.input_per_mtok_microusd > 0, key
        assert rate.output_per_mtok_microusd > 0, key
        assert rate.cache_read_per_mtok_microusd >= 0, key
        assert rate.cache_write_per_mtok_microusd >= 0, key


def test_the_expected_unpriced_list_has_not_gone_stale(report):
    """The other half of the ratchet: a model AWS has started publishing must come off
    the list, or the list becomes a blanket excuse that hides the next regression."""
    priced = {base_model_id(m) for m in report.rates} if report.rates else set()
    unpriced = {base_model_id(m) for m in report.unpriced}
    stale = sorted(set(EXPECTED_UNPRICED) - unpriced - set(report.unauthorized))
    # `report.rates` is keyed by pricing key, not by model, so absence from `unpriced`
    # is the signal that a model became readable.
    assert not stale, (
        f"these models are priced now and should be removed from EXPECTED_UNPRICED: "
        f"{stale}"
    )
    assert not (set(EXPECTED_UNPRICED) & priced)


# --- M14: the budget clock -----------------------------------------------------
# `out_of_time()` has to be fixed before M4's evidence means anything: the pass
# builds its deadline from an injectable clock (`composite.py:266`,
# `LivePriceSource(clock=...)`), but `FeedRequest.out_of_time()` compares it against
# `time.time()`. Inject `time.monotonic` and every feed expires instantly; inject a
# test's fake clock and the budget is unenforced, because the feed is reading a
# different clock than the one that built the deadline it is checking against.
def test_out_of_time_reads_the_deadlines_own_clock_not_time_time():
    """M14: `FeedRequest.clock` -- a zero-argument callable returning a float on the
    same time source as `deadline`, defaulting to `time.monotonic` per the contract
    -- is what `out_of_time()` must read, never `time.time()` directly, or a test
    (and the composite's own between-feed check) that injects a clock builds a
    deadline the feed cannot read from `time.time()`."""
    import time as time_module

    from mvp.pricing_feeds.base import FeedRequest

    # The contracted default is `time.monotonic`, checked by identity: unlike a
    # string literal, a builtin function is not interned into equality-by-accident.
    assert FeedRequest(model_ids=frozenset({"m"})).clock is time_module.monotonic

    fake_now = [10.0]  # far from wall-clock time.time(), which is on the order of
                        # 1.7e9 in 2026 — chosen so the two clocks cannot be confused.

    def clock():
        return fake_now[0]

    request = FeedRequest(model_ids=frozenset({"m"}), deadline=15.0, clock=clock)
    assert request.out_of_time() is False
    fake_now[0] = 14.999
    assert request.out_of_time() is False
    fake_now[0] = 15.0
    assert request.out_of_time() is True


def test_a_feed_sees_out_of_time_flip_at_the_sources_injected_clocks_budget():
    """M14: the source has two time sources, and the budget lives on the second one.
    `LivePriceSource(clock=...)` stays wall time (meaningful against the timestamps
    the store wrote); `LivePriceSource(budget_clock=...)` is elapsed time, default
    `time.monotonic`, and it is what the deadline is built from AND what gets
    threaded to every `FeedRequest` the composite constructs. A source built with a
    `budget_clock` that starts nowhere near wall time must bound its feeds by THAT
    clock: a feed sees `out_of_time()` become true at the budget and not before, on
    the injected budget clock, regardless of what `time.time()` says."""
    from mvp.pricing_feeds.base import FeedRequest, FeedResult

    fake_now = [5_000_000.0]  # nowhere near real wall-clock time.

    def clock():
        return fake_now[0]

    seen: list[bool] = []

    class _ProbeFeed:
        name = "probe"

        def fetch(self, request: FeedRequest) -> FeedResult:
            seen.append(request.out_of_time())  # before the injected clock moves
            fake_now[0] += 10.0  # advance the INJECTED clock well past the budget;
                                 # real wall-clock time barely moves during a test.
            seen.append(request.out_of_time())  # after
            return FeedResult()

    source = LivePriceSource(
        feeds=(_ProbeFeed(),), store=_NoStore(), registry=(), budget_clock=clock,
    )
    source.refresh(budget_seconds=5.0)
    assert seen == [False, True], (
        f"saw {seen}; a feed's out_of_time() must read the source's injected "
        f"budget_clock, not time.time()"
    )


def test_price_lists_own_page_budget_check_goes_through_out_of_time_not_time_time():
    """M14: `PriceListFeed._fetch_region` checks its own page-by-page budget with a
    raw `if deadline is not None and time.time() >= deadline:` -- a second, separate
    bypass of `request.out_of_time()`, distinct from `out_of_time()`'s own bug above.
    With the contracted default (`FeedRequest.clock` defaulting to `time.monotonic`),
    a deadline built on that clock is a small, uptime-scale number; comparing it
    against `time.time()`'s epoch-scale value makes every page look instantly
    overdue, so a fresh pass with a full budget truncates after the very first page
    it reads -- observable without injecting any clock at all. Three pages are
    served so the assertion also fails if only the region-loop's own
    `request.out_of_time()` calls get fixed and this inner, page-level check is left
    reading `time.time()` on its own."""
    import json

    from mvp.pricing_feeds.base import FeedRequest
    from mvp.pricing_feeds.price_list import PriceListFeed

    def _page(term_key: str) -> str:
        return json.dumps({
            "product": {"attributes": {
                "usagetype": "USE1-vendor.model-input-tokens-standard",
                "regionCode": "us-east-1"}},
            "terms": {"OnDemand": {term_key: {"priceDimensions": {"d": {
                "unit": "1K tokens", "pricePerUnit": {"USD": "0.001"}}}}}},
        })

    _PAGES = {
        None: ([_page("p1")], "page2"),
        "page2": ([_page("p2")], "page3"),
        "page3": ([_page("p3")], None),
    }

    class _PagedClient:
        def get_products(self, **kwargs):
            products, next_token = _PAGES[kwargs.get("NextToken")]
            response = {"PriceList": products}
            if next_token:
                response["NextToken"] = next_token
            return response

    request = FeedRequest(
        model_ids=frozenset({"vendor.model"}), regions=frozenset({"us-east-1"}),
        # A full budget, built on the CONTRACTED default clock (time.monotonic).
        deadline=time.monotonic() + 30.0,
    )
    result = PriceListFeed(_PagedClient()).fetch(request)
    assert not result.truncated, (
        "truncated a fresh 30s-budget, 3-page pass; a page-budget check somewhere "
        "in the price list feed is reading time.time() directly instead of "
        "request.out_of_time(), and a monotonic-scale deadline always looks "
        "overdue against epoch-scale time.time()"
    )
    assert "vendor.model" in result.cards


# --- M4: the budget bounds nothing in flight ------------------------------------
def test_every_aws_client_a_feed_builds_carries_a_budget_derived_config(monkeypatch):
    """M4: with no `botocore.config.Config`, a client boto3 builds for itself
    defaults to a 60s read timeout (`Config().read_timeout == 60`), so one in-flight
    call is bounded by botocore's own default rather than by the pass's deadline.
    Every client `AgreementFeed`/`PriceListFeed` build for themselves (no client
    injected) must carry a `Config` whose `connect_timeout` and `read_timeout` are
    DERIVED from the pass's remaining budget -- checked here by giving two very
    different budgets and requiring two different timeouts, so a fixed constant
    unrelated to the budget does not pass by accident."""
    import boto3
    from botocore.config import Config

    from mvp.pricing_feeds.agreement import AgreementFeed
    from mvp.pricing_feeds.base import FeedRequest
    from mvp.pricing_feeds.price_list import PriceListFeed

    class _StubClient:
        def list_foundation_model_agreement_offers(self, modelId):  # noqa: N803
            return {"modelId": modelId, "offers": []}

        def get_products(self, **kwargs):
            return {"PriceList": []}

    def run_with_budget(budget: float) -> list[tuple[str, object]]:
        seen: list[tuple[str, object]] = []

        def fake_client(service, region_name=None, config=None, **kwargs):
            seen.append((service, config))
            return _StubClient()

        monkeypatch.setattr(boto3, "client", fake_client)
        deadline = time.time() + budget
        AgreementFeed().fetch(FeedRequest(
            model_ids=frozenset({"anthropic.claude-opus-5"}), deadline=deadline))
        PriceListFeed().fetch(FeedRequest(
            model_ids=frozenset({"vendor.model"}), regions=frozenset({"us-east-1"}),
            deadline=deadline))
        return seen

    short = run_with_budget(2.0)
    long = run_with_budget(20.0)

    for label, seen in (("2s budget", short), ("20s budget", long)):
        assert len(seen) == 2, f"{label}: expected one client per feed, saw {seen}"
        for service, config in seen:
            assert isinstance(config, Config), (
                f"{label}: the {service!r} client was built with no botocore "
                f"Config at all, so an in-flight call is bounded only by "
                f"botocore's own default read timeout, not by this pass's budget"
            )
            assert config.read_timeout and config.read_timeout > 0
            assert config.connect_timeout and config.connect_timeout > 0
            assert config.retries is not None, (
                f"{label}: {service!r} client has no retry policy; botocore's own "
                f"default retry behaviour applies instead of the contracted one"
            )
            assert config.retries.get("mode") == "standard"
            # botocore's `max_attempts` counts retries AFTER the initial call, so
            # `max_attempts=2` would permit three calls total; "at most two
            # attempts" is `total_max_attempts=2`. Accept either key, but the
            # EFFECTIVE total (initial call plus retries) must be exactly 2.
            total = config.retries.get("total_max_attempts")
            if total is None:
                max_attempts = config.retries.get("max_attempts")
                assert max_attempts is not None, (
                    f"{label}: {service!r} client's retries carry neither "
                    f"'total_max_attempts' nor 'max_attempts': {config.retries}"
                )
                total = max_attempts + 1
            assert total == 2, (
                f"{label}: {service!r} client allows {total} attempt(s) total; "
                f"the interface says at most two"
            )

    short_bedrock = next(c.read_timeout for s, c in short if s == "bedrock")
    long_bedrock = next(c.read_timeout for s, c in long if s == "bedrock")
    assert short_bedrock < long_bedrock, (
        f"read_timeout was {short_bedrock} against a 2s budget and {long_bedrock} "
        f"against a 20s budget: it does not look derived from the pass's remaining "
        f"budget at all"
    )


def test_a_call_that_stalls_past_the_deadline_does_not_run_to_botocores_default(
    monkeypatch,
):
    """M4: measured on `c39be4c` at 356 ms against a 50 ms budget, still reporting
    `truncated=False`. With no `Config`, the deadline is only checked BETWEEN calls,
    so one in-flight call is free to run to whatever timeout applies by default —
    which, with no `Config` at all, is botocore's own (60s). The stub client below
    sleeps for whatever `read_timeout` its `Config` carries (falling back to a much
    longer stand-in duration when it was built with none, so this test does not
    itself wait out botocore's real default) and then raises, the way a real socket
    read timeout does."""
    import boto3

    _NO_CONFIG_STANDIN_SECONDS = 4.0

    class _StalledCallClient:
        def __init__(self, config=None):
            self._timeout = (
                config.read_timeout
                if config is not None and config.read_timeout
                else _NO_CONFIG_STANDIN_SECONDS
            )

        def list_foundation_model_agreement_offers(self, modelId):  # noqa: N803
            time.sleep(self._timeout + 0.05)
            raise TimeoutError("simulated botocore read timeout")

    def fake_client(service, region_name=None, config=None, **kwargs):
        return _StalledCallClient(config)

    monkeypatch.setattr(boto3, "client", fake_client)

    from mvp.pricing_feeds.agreement import AgreementFeed

    entry = _Entry("anthropic.claude-opus-5", "opus")
    source = LivePriceSource(feeds=(AgreementFeed(),), store=_NoStore(),
                             registry=(entry,))

    started = time.time()
    report = source.refresh(budget_seconds=1.0)
    elapsed = time.time() - started

    assert elapsed < 3.0, (
        f"the pass ran {elapsed:.2f}s against a 1s budget; a Config-less client's "
        f"in-flight call is not bounded by the pass's deadline at all"
    )
    assert report.truncated is True, (
        "a pass that had to give up on an in-flight call because it overran its "
        "own deadline must report truncated=True, same as skipping a feed outright"
    )


# --- M17: --strict is a gate, not a suggestion ----------------------------------
def _run_strict(monkeypatch, report):
    """Drive `fetch.main(["--strict"])` against a canned `FetchReport`, touching
    neither AWS nor a real snapshot store."""
    import mvp.pricing_feeds.fetch as fetch_mod

    class _FakeStore:
        def load(self):
            return None

        def save(self, *args, **kwargs):
            return None

    class _FakeSource:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def refresh(self, budget_seconds=None):
            return report

    monkeypatch.setattr(fetch_mod, "LivePriceSource", _FakeSource)
    monkeypatch.setattr(fetch_mod, "SnapshotStore", lambda *a, **kw: _FakeStore())
    return fetch_mod.main(["--strict"])


def test_strict_exits_2_when_a_feed_reported_an_authorisation_failure(
    monkeypatch, capsys,
):
    """M17: `--strict` is offered as the deploy gate. A pass where a whole feed was
    denied -- no `bedrock:ListFoundationModelAgreementOffers`, every Claude and GPT
    key left at the floor -- exits 0 today as long as one other pricing key was
    read. That is the half of the catalogue that most needed a live price getting
    none, and a gate that passes through it is not a gate. The reason is named
    `feed_not_authorized`, and every reason `--strict` exits on is printed."""
    from mvp.pricing_feeds.composite import FetchReport
    from mvp.rates import Rate

    report = FetchReport(
        rates={"self-hosted-pool": Rate(1, 1, 0, 0)},
        unauthorized={
            "anthropic.claude-opus-5": "bedrock-agreement",
            "openai.gpt-5.6-sol": "bedrock-agreement",
        },
    )
    exit_code = _run_strict(monkeypatch, report)
    out = capsys.readouterr().out
    assert exit_code == 2
    assert "feed_not_authorized" in out, (
        f"the reason it exited on was not printed under its contracted name:\n{out}"
    )


def test_strict_exits_2_for_an_unpriced_model_not_on_the_declared_allowlist(
    monkeypatch, capsys,
):
    """M17: an unpriced model with no allowlist entry is exactly what the gate
    exists to catch -- nobody has said this model's absence from a live price is
    acceptable. Named `unpriced_not_allowlisted`."""
    from mvp.pricing_feeds.composite import FetchReport
    from mvp.rates import Rate

    report = FetchReport(
        rates={"opus": Rate(1, 1, 0, 0)},
        unpriced={
            "vendor.experimental-model":
                "bedrock-price-list: card has no standard-tier input and output "
                "rate for ['us-east-1']",
        },
    )
    monkeypatch.delenv("STRATOCLAVE_PRICE_FEED_UNPRICED_ALLOWLIST", raising=False)
    exit_code = _run_strict(monkeypatch, report)
    out = capsys.readouterr().out
    assert exit_code == 2
    assert "unpriced_not_allowlisted" in out, (
        f"the reason it exited on was not printed under its contracted name:\n{out}"
    )


def test_strict_exits_0_when_the_only_unpriced_entries_are_on_the_allowlist(
    monkeypatch,
):
    """M17: `STRATOCLAVE_PRICE_FEED_UNPRICED_ALLOWLIST` is the operator's declared
    acceptance of a specific model's absence from a live price. The gate must not
    fire on exactly the case an operator has already signed off on, or the
    allowlist is decorative — and an empty or unset list allows none, so this is
    checked against the positive case as well as the negative one above."""
    from mvp.pricing_feeds.composite import FetchReport
    from mvp.rates import Rate

    report = FetchReport(
        rates={"opus": Rate(1, 1, 0, 0)},
        unpriced={
            "vendor.experimental-model":
                "bedrock-price-list: card has no standard-tier input and output "
                "rate for ['us-east-1']",
        },
    )
    monkeypatch.setenv(
        "STRATOCLAVE_PRICE_FEED_UNPRICED_ALLOWLIST", "vendor.experimental-model")
    exit_code = _run_strict(monkeypatch, report)
    assert exit_code == 0


def test_strict_exits_2_for_feed_not_authorized_even_when_every_model_is_allowlisted(
    monkeypatch, capsys,
):
    """M17: settled overlap — `feed_not_authorized` is unconditional. A feed that
    could not read its source at all means the pass did not see a whole catalogue,
    which is categorically different from a model an operator has looked at and
    accepted as unpriced. The allowlist covers individual model ids only and never
    suppresses a feed-level authorisation failure, even when every model the failed
    feed named happens to also be on the allowlist."""
    from mvp.pricing_feeds.composite import FetchReport
    from mvp.rates import Rate

    report = FetchReport(
        rates={"self-hosted-pool": Rate(1, 1, 0, 0)},
        unauthorized={
            "anthropic.claude-opus-5": "bedrock-agreement",
            "openai.gpt-5.6-sol": "bedrock-agreement",
        },
    )
    monkeypatch.setenv(
        "STRATOCLAVE_PRICE_FEED_UNPRICED_ALLOWLIST",
        "anthropic.claude-opus-5,openai.gpt-5.6-sol",
    )
    exit_code = _run_strict(monkeypatch, report)
    out = capsys.readouterr().out
    assert exit_code == 2, (
        "a feed-level authorisation failure must exit 2 regardless of the "
        "allowlist; the allowlist is not a way to suppress a whole feed going dark"
    )
    assert "feed_not_authorized" in out


def test_strict_help_names_the_two_new_reasons():
    """M17: the reasons `--strict` exits on are named with fixed tokens "in the
    `--help` text, in `_exit_code`'s docstring, in the design document and in what
    the command prints" — so a reader of `--help` alone can look one up. Checked for
    the two new ones; the pre-existing four are the other authors' finding, not
    this one's."""
    import io
    from contextlib import redirect_stdout

    import mvp.pricing_feeds.fetch as fetch_mod

    # `main(["--help"])` exercises the real parser argparse builds (rather than a
    # hand-rolled second one that could drift from it) and exits via SystemExit,
    # which argparse does on `--help` regardless of this module's own return-code
    # convention.
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit):
        fetch_mod.main(["--help"])
    text = buf.getvalue()
    for token in ("feed_not_authorized", "unpriced_not_allowlisted"):
        assert token in text, f"--help does not name {token!r}:\n{text}"


# --- Q17: one region constant, not two ------------------------------------------
def test_stratoclave_region_env_name_is_defined_in_exactly_one_module():
    """Q17: `price_list.py` declares `REGION_ENV = "STRATOCLAVE_REGION"` under the
    comment "named once. `agreement.py` reads the same variable" -- but
    `agreement.py` declares its OWN `_REGION_ENV = "STRATOCLAVE_REGION"` rather than
    importing `price_list`'s, so the name is written twice and the comment is false.

    Checked on the source text rather than on `is` identity of the two constants:
    CPython interns identifier-shaped string literals across modules, so
    `agreement._REGION_ENV is price_list.REGION_ENV` is already `True` today for two
    INDEPENDENTLY-declared constants that merely happen to hold an equal string, and
    would not catch the duplication the comment denies."""
    import inspect

    import mvp.pricing_feeds.agreement as agreement
    import mvp.pricing_feeds.price_list as price_list

    literal = '"STRATOCLAVE_REGION"'
    counts = {
        "agreement.py": inspect.getsource(agreement).count(literal),
        "price_list.py": inspect.getsource(price_list).count(literal),
    }
    assert sum(counts.values()) <= 1, (
        f"'STRATOCLAVE_REGION' is written as a literal in {counts}; it must be "
        f"assigned in at most one module, with the other importing that name, or "
        f"a future rename has two places to remember instead of one"
    )
    assert agreement._REGION_ENV == price_list.REGION_ENV == "STRATOCLAVE_REGION"
