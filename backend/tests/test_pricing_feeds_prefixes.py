"""Is the bundled billing-prefix table still what AWS publishes?

`dimensions.BILLING_PREFIXES` maps `USE1` to `us-east-1` and so on, and it is load-bearing:
an agreement rate card names its regions only by that prefix, so a wrong entry would
either drop a region's price or attribute it to the wrong one.

The table is not a guess at the naming convention — it is a projection of AWS's own
data, because every Bedrock product in the Price List carries both `regionCode` and a
`usagetype` beginning with the prefix. This test rebuilds the projection against the
live API and compares. It is skipped without credentials (and in CI by default), so
the offline suite never depends on the network:

    STRATOCLAVE_LIVE_PRICE_TESTS=1 pytest tests/test_pricing_feeds_prefixes.py

The companion live check for the rate-name grammars is
`tests/test_pricing_feeds_live_apis.py`.
"""
from __future__ import annotations

import json
import os
import pathlib
import re

import pytest

from mvp import price_sources
from mvp.models import registry_entries
from mvp.pricing import baseline_rates
from mvp.pricing_feeds.dimensions import BILLING_PREFIXES
from tests.live_aws import real_session

_FLAG = "STRATOCLAVE_LIVE_PRICE_TESTS"
_OFFERS = ("AmazonBedrock", "AmazonBedrockFoundationModels", "AmazonBedrockService")

# backend/tests/test_pricing_feeds_prefixes.py -> parents[1] = backend, .parent = repo root.
_ROOT = pathlib.Path(__file__).resolve().parents[1].parent
_DOC_PATH = _ROOT / "docs" / "design" / "price-feeds.md"


def _live_or_skip():
    if not os.getenv(_FLAG):
        pytest.skip(f"set {_FLAG}=1 to check the prefix table against the live API")
    boto3 = pytest.importorskip("boto3")
    session = real_session(boto3)
    if session is None:
        pytest.skip("no real AWS credentials available")
    return session.client("pricing", region_name="us-east-1")


def test_bundled_prefixes_match_the_live_price_list():
    client = _live_or_skip()
    seen: dict[str, dict[str, int]] = {}
    for service in _OFFERS:
        token = None
        while True:
            kwargs = {"ServiceCode": service, "MaxResults": 100}
            if token:
                kwargs["NextToken"] = token
            response = client.get_products(**kwargs)
            for raw in response.get("PriceList", ()):
                attributes = json.loads(raw)["product"]["attributes"]
                region = attributes.get("regionCode")
                usagetype = attributes.get("usagetype") or ""
                prefix = usagetype.split("-", 1)[0]
                if not region or not prefix.isalnum() or not prefix.isupper():
                    continue
                seen.setdefault(prefix, {}).setdefault(region, 0)
                seen[prefix][region] += 1
            token = response.get("NextToken")
            if not token:
                break

    live = {prefix: max(regions.items(), key=lambda kv: kv[1])[0]
            for prefix, regions in seen.items()}
    # A prefix AWS has added is a gap to fill, not a failure of the existing rows:
    # the bundled table only has to be RIGHT about what it claims, and complete enough
    # to cover the regions this deployment can dispatch to.
    wrong = {p: (BILLING_PREFIXES[p], live[p]) for p in set(BILLING_PREFIXES) & set(live)
             if BILLING_PREFIXES[p] != live[p]}
    assert not wrong, (
        f"bundled prefix -> region disagrees with the live Price List: {wrong}. "
        f"Regenerate with `python -m mvp.pricing_feeds.fetch --print-prefixes`."
    )
    missing = sorted(set(live) - set(BILLING_PREFIXES))
    if missing:
        pytest.skip(f"AWS has prefixes this build does not know yet: {missing} "
                    f"(regenerate the document to price those regions)")


# ---------------------------------------------------------------------------
# M15 -- startup validation must refuse an active pricing document that does
# not price every `pricing_key` the model registry uses.
#
# This change split five new pricing keys out of existing ones (`opus-legacy`,
# `sonnet-5`, `sonnet-3`, `haiku-3-5`, `haiku-3`). `price_sources.py` validates
# shape and the presence of `default` only; `pricing.py` falls back to
# `default` for anything absent. A deployment carrying its own older pricing
# document -- one written before the split -- keeps charging those five
# families at `default` after the upgrade, silently, forever, because nothing
# compares the active source's table against the registry's key set.
#
# Per the Interface section: "`price_sources.validate_configuration()` refuses
# an active pricing document that does not price every `pricing_key` the model
# registry uses, naming each missing key. The bundled document satisfies it."
# ---------------------------------------------------------------------------

class _M15Stub:
    """A registered price source whose table is missing exactly the keys under
    test, so the failure under test is coverage, not shape (every row that IS
    present is a valid `Rate` copied from the floor)."""

    def __init__(self, name, table):
        self.name = name
        self._table = table

    def load(self):
        return self._table


def _table_missing(*missing_keys):
    """The floor's whole table, minus `missing_keys`, still satisfying every
    OTHER shape rule `price_sources` checks (so a failure here can only be
    about registry coverage)."""
    floor = baseline_rates()
    keys = {e.pricing_key for e in registry_entries()} | {price_sources.REQUIRED_KEY}
    return {k: floor[k] for k in keys if k not in missing_keys}


@pytest.fixture(autouse=True)
def _clean_m15_sources(monkeypatch):
    """Isolate the registry mutations these tests perform from every other test
    in the process -- including the live-price test above, which must keep
    seeing the real bundled `json` source."""
    monkeypatch.delenv("STRATOCLAVE_PRICE_SOURCE", raising=False)
    price_sources.reset_registry_for_tests()
    yield
    price_sources.reset_registry_for_tests()


class TestM15StartupValidationCoversRegistryKeys:
    def test_a_document_missing_registry_keys_is_refused_and_names_them(self, monkeypatch):
        """M15: the five split keys are exactly the case the finding describes --
        an operator's older document that predates the split. `default` is left
        in place so the only thing wrong with this table is coverage."""
        missing = {"sonnet-5", "haiku-3"}
        assert missing <= {e.pricing_key for e in registry_entries()}, (
            "fixture assumption broken: the registry no longer uses these keys"
        )
        price_sources.register_price_source(
            _M15Stub("stub-m15-missing", _table_missing(*missing))
        )
        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "stub-m15-missing")

        with pytest.raises(ValueError) as excinfo:
            price_sources.validate_configuration()
        message = str(excinfo.value)
        for key in missing:
            assert key in message, (
                f"validate_configuration() must name every missing key; "
                f"{key!r} is missing from the refusal message: {message!r}"
            )

    def test_a_document_missing_one_of_the_five_split_keys_is_refused(self, monkeypatch):
        """Each of the five keys this change split out is independently a case
        the finding covers, not just the case where several are missing at once."""
        for key in ("opus-legacy", "sonnet-5", "sonnet-3", "haiku-3-5", "haiku-3"):
            price_sources.reset_registry_for_tests()
            price_sources.register_price_source(
                _M15Stub("stub-m15-one", _table_missing(key))
            )
            monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "stub-m15-one")
            with pytest.raises(ValueError, match=key):
                price_sources.validate_configuration()

    def test_a_document_missing_no_registry_key_is_accepted(self, monkeypatch):
        """The negative control for the two tests above: a source whose table is
        merely narrower than the floor (e.g. a live feed that only prices what it
        was asked about) must not be refused for a key it never dropped."""
        price_sources.register_price_source(
            _M15Stub("stub-m15-complete", _table_missing())
        )
        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "stub-m15-complete")
        assert price_sources.validate_configuration() == "stub-m15-complete"

    def test_the_bundled_document_prices_every_registry_key(self):
        """The Interface section's second half: "The bundled document satisfies
        it." No source override -- this is the plain `json` source reading
        `defaults/pricing.json` -- so this is also the deployment's default."""
        assert price_sources.validate_configuration() == price_sources.DEFAULT_SOURCE_NAME

    def test_an_admin_override_supplying_the_missing_key_does_not_rescue_startup_validation(
        self, monkeypatch
    ):
        """Admin overrides are a layer `mvp.pricing`'s cache applies on top of the
        active source at request time (see `price_sources.py`'s module docstring,
        layer 3); `validate_configuration()` resolves and loads the source alone,
        with no repository argument through which an override could reach it.

        This pins the narrower of the two readings the Interface section's
        wording ("an active pricing document") admits: it could mean either "the
        source's own table" or "the effective table after every layer including
        admin overrides". `validate_configuration()`'s existing signature can
        only support the former, and an operator relying on an override to
        paper over a stale document must still see the deployment fail loud --
        the override is not consulted at the point this check runs, deliberately,
        because it exists to catch the document BEFORE traffic ever depends on
        the override having been set correctly too.
        """
        missing = {"opus-legacy"}
        price_sources.register_price_source(
            _M15Stub("stub-m15-admin", _table_missing(*missing))
        )
        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "stub-m15-admin")
        # No PricingConfigRepository is threaded through here on purpose: there is
        # no parameter on `validate_configuration()` to hand one to. An override
        # recorded in that table, however correct, cannot be seen from here.
        with pytest.raises(ValueError, match="opus-legacy"):
            price_sources.validate_configuration()


# ---------------------------------------------------------------------------
# Q3 -- `--print-prefixes` must not regenerate a document that drops
# `id_suffix_segments`, and the loader must refuse a document that has none.
# ---------------------------------------------------------------------------


class _FakePricingClient:
    """One page, no `NextToken`: just enough surface for `_print_prefixes` to
    derive a prefix -> region table without an outbound call to AWS. The row
    shape mirrors a real Price List product's `attributes` block."""

    def get_products(self, ServiceCode, MaxResults, NextToken=None):
        del ServiceCode, MaxResults, NextToken  # unused; one canned page is enough.
        rows = [
            {"product": {"attributes": {
                "regionCode": "us-east-1",
                "usagetype": "USE1-fake.model-input-tokens-standard",
            }}},
        ]
        return {"PriceList": [json.dumps(row) for row in rows]}


def _run_print_prefixes(monkeypatch, capsys) -> dict:
    """Runs `fetch._print_prefixes()` against the fake client above and returns
    the printed document, parsed."""
    import boto3

    from mvp.pricing_feeds import fetch as fetch_module

    monkeypatch.setattr(boto3, "client", lambda *a, **k: _FakePricingClient())
    fetch_module._print_prefixes()
    printed = capsys.readouterr().out
    return json.loads(printed)


def test_print_prefixes_output_carries_id_suffix_segments(monkeypatch, capsys):
    """Q3: `--print-prefixes` regenerates a document without `id_suffix_segments`.
    The Interface section is explicit about what the regenerated document has to
    contain: "`fetch --print-prefixes` emits `schema_version`, `prefixes` and
    `id_suffix_segments`." Today it emits only the first two, which means
    following the tool's own instruction (fetch.py's module docstring: "regenerate
    the region table") silently drops every allowed suffix segment (`mantle`) the
    NEXT time the printed document replaces the bundled one, and nothing here
    fails loudly about it."""
    doc = _run_print_prefixes(monkeypatch, capsys)
    assert "id_suffix_segments" in doc, (
        "--print-prefixes must emit id_suffix_segments; without it, regenerating "
        "the bundled document silently drops every allowed suffix (e.g. 'mantle') "
        "and files those Price List rows as unparsed on the next reload"
    )
    assert doc["id_suffix_segments"], "id_suffix_segments must not be emitted empty"


def test_print_prefixes_output_round_trips_through_the_loader(monkeypatch, capsys, tmp_path):
    """Q3: the whole point of printing rather than writing is that the bundled
    document is reviewed like code (see `_print_prefixes`'s own docstring) and
    then swapped in. That property only holds if the loader can actually read
    what got printed back. This writes the printed document to disk and points
    `dimensions._PREFIX_DOC` at it directly, so the loader functions run against
    exactly the bytes `--print-prefixes` produced -- not a copy of them."""
    from mvp.pricing_feeds import dimensions

    doc = _run_print_prefixes(monkeypatch, capsys)
    assert "id_suffix_segments" in doc, (
        "nothing to round-trip: --print-prefixes did not emit id_suffix_segments "
        "(see test_print_prefixes_output_carries_id_suffix_segments)"
    )

    doc_path = tmp_path / "billing_region_prefixes.json"
    doc_path.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(dimensions, "_PREFIX_DOC", str(doc_path))

    assert dimensions._load_prefixes() == doc["prefixes"]
    assert dimensions._load_id_suffix_segments() == doc["id_suffix_segments"]


def test_loader_refuses_a_document_with_no_id_suffix_segments_key(monkeypatch, tmp_path):
    """Q3, the other half of the Interface line: "`_load_id_suffix_segments` raises
    on a document with no `id_suffix_segments` key." Today the loader treats a
    missing key the same as an empty list (`.get("id_suffix_segments") or []`),
    so a document silently missing the key loads successfully with an empty
    allowlist -- exactly the failure mode `--print-prefixes` can produce, and
    exactly the one nothing here currently fails loudly about."""
    from mvp.pricing_feeds import dimensions

    doc_path = tmp_path / "billing_region_prefixes.json"
    doc_path.write_text(
        json.dumps({"schema_version": 1, "prefixes": {"USE1": "us-east-1"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(dimensions, "_PREFIX_DOC", str(doc_path))

    with pytest.raises(ValueError):
        dimensions._load_id_suffix_segments()


# ---------------------------------------------------------------------------
# Q5 -- docs/design/price-feeds.md drifted from the code it describes: a wrong
# budget default, a Price List offer list short by one, and knobs the document
# never names. Each test reads the document's own sentence AND the code
# constant, so a wrong number OR a deleted sentence both fail, and neither copy
# can drift while the other is trusted blindly.
# ---------------------------------------------------------------------------


def test_document_and_code_agree_on_the_feed_budget_default():
    """Q5: the document says `STRATOCLAVE_PRICE_FEED_BUDGET_SECONDS` defaults to
    30 seconds; `composite.DEFAULT_BUDGET_SECONDS` is 15.0. Reads the document's
    own sentence with a regex anchored on the knob's name, so deleting the
    sentence (rather than fixing the number) still fails -- there is no way to
    satisfy this by removing the claim instead of correcting it."""
    from mvp.pricing_feeds import composite

    text = _DOC_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"STRATOCLAVE_PRICE_FEED_BUDGET_SECONDS`\s*\(default\s+(\d+(?:\.\d+)?)", text
    )
    assert match, (
        "docs/design/price-feeds.md no longer states a default for "
        "STRATOCLAVE_PRICE_FEED_BUDGET_SECONDS in the documented form "
        "'`NAME` (default N ...)' -- deleting the sentence must fail this test, "
        "not satisfy it"
    )
    documented_default = float(match.group(1))
    assert documented_default == composite.DEFAULT_BUDGET_SECONDS, (
        f"docs/design/price-feeds.md says the budget defaults to "
        f"{documented_default}s; mvp/pricing_feeds/composite.DEFAULT_BUDGET_SECONDS "
        f"says {composite.DEFAULT_BUDGET_SECONDS}s. An operator who believes the "
        f"document investigates a setting that does not behave the way they were "
        f"told."
    )


def test_document_and_code_agree_on_which_price_list_offers_the_feed_reads():
    """Q5: "The feed reads `AmazonBedrock` and leaves the rest to the agreement
    API" names one offer. `price_list.DEFAULT_OFFERS` actually iterates two
    (`AmazonBedrock`, `AmazonBedrockService`). Extracts the offer code(s) out of
    the document's own sentence rather than asserting a hardcoded expectation, so
    a document correction that adds the second offer -- or a code change that
    drops one -- both stay caught by comparing against the live constant."""
    from mvp.pricing_feeds import price_list

    text = _DOC_PATH.read_text(encoding="utf-8")
    # The sentence wraps across a source line in the document, so the space
    # between "feed" and "reads" is whitespace (possibly a newline), not
    # necessarily a literal ASCII space.
    match = re.search(r"The feed\s+reads[^.]*\.", text)
    assert match, (
        "docs/design/price-feeds.md no longer has a sentence starting 'The feed "
        "reads ...' naming which Price List offer(s) this feed queries"
    )
    documented_offers = tuple(re.findall(r"`(Amazon\w+)`", match.group()))
    assert documented_offers, (
        f"no backtick-quoted offer code found in {match.group()!r}"
    )
    assert set(documented_offers) == set(price_list.DEFAULT_OFFERS), (
        f"the document's sentence {match.group()!r} names {documented_offers}; "
        f"price_list.DEFAULT_OFFERS is {price_list.DEFAULT_OFFERS}. A reader who "
        f"trusts the document does not know the feed also reads "
        f"AmazonBedrockService."
    )


def test_every_feed_env_knob_is_named_in_the_document():
    """Q5: five knobs the code reads are never mentioned anywhere in
    docs/design/price-feeds.md, which has exactly one paragraph ("Knobs: ...")
    whose entire job is to be that list. `STRATOCLAVE_REGION` is deliberately
    excluded from the set checked here: its problem is Q17 (the same constant
    named in two modules under a comment claiming it is named once), not an
    absence from this document, and it is verified by group A's own test."""
    from mvp.pricing_feeds import agreement, composite, price_list, snapshot

    knob_env_vars = {
        composite.INTERVAL_ENV,
        composite.BUDGET_ENV,
        composite.REFRESH_BUDGET_ENV,
        agreement.WORKERS_ENV,
        price_list.ENDPOINT_REGION_ENV,
        price_list.OFFERS_ENV,
        price_list.MAX_PAGES_ENV,
        snapshot.STALE_AFTER_SECONDS_ENV,
    }
    text = _DOC_PATH.read_text(encoding="utf-8")
    missing = sorted(name for name in knob_env_vars if name not in text)
    assert not missing, (
        f"these env knobs are read by the pricing_feeds code but never named "
        f"anywhere in docs/design/price-feeds.md, so an operator has no way to "
        f"discover they exist: {missing}"
    )


# `--strict`'s full contracted exit-reason set, per CONTRACT.md's Interface
# section (post-amendment: the prose originally left these reasons unnamed,
# which is exactly the gap this test exists to close -- a test that invents its
# own token name for an unnamed reason is checking itself, not the contract).
# These are literal tokens, not phrases: they are meant to appear verbatim in
# the `--help` text, `_exit_code`'s docstring, the design document and what the
# command prints, so all four sources -- and any test reading them -- agree on
# one spelling instead of four independent paraphrases.
_STRICT_REASON_TOKENS = {
    "key_spans_prices",
    "leg_regression",
    "coverage_regression",
    "budget_spent",
    "feed_not_authorized",
    "unpriced_not_allowlisted",
}


def _strict_reasons_named_in(text: str) -> set[str]:
    # Every source here wraps prose across lines -- the document at the source
    # level, argparse's own `--help` formatter at render time -- so a token could
    # in principle straddle a newline if it were ever hyphenated at the wrap
    # point. Collapse whitespace before matching for the same reason the M9
    # per-leg pins do, even though a literal token has no internal whitespace to
    # split on today.
    normalized = re.sub(r"\s+", " ", text)
    return {token for token in _STRICT_REASON_TOKENS if token in normalized}


def test_strict_exit_reasons_are_named_consistently_and_completely(capsys):
    """Q5 + M17. `--strict` is enumerated in three places -- the document's
    "Operating it" section, the CLI's own `--help` text, and `_exit_code`'s
    docstring -- and today all three agree with EACH OTHER on three reasons in
    PROSE only (a pricing key spanning two prices, a leg that stopped being
    published, a spent time budget), naming none of them by the token
    CONTRACT.md's Interface section now fixes: `key_spans_prices`,
    `leg_regression`, `budget_spent`. None of the three names
    `coverage_regression` either, which is ALREADY real in `_exit_code` today
    (`report.coverage_regressions`), and none names M17's two new reasons --
    `feed_not_authorized`, `unpriced_not_allowlisted` -- which are also not yet
    true in the code, since `_exit_code` does not check for them.

    Checking for the exact contracted token rather than a hand-written phrase
    is deliberate: an earlier version of this test matched prose ("key spanning
    two prices") and would have stayed green under a code author's and a
    document author's independently invented token names for the same two new
    reasons, agreeing with each other on nothing. The token is the interface;
    three texts and this test have to reproduce it byte-for-byte or nothing
    else in the change would notice them drifting apart.

    This fails for two independent reasons at once, and is written to say which
    is which: the documentation side (all three texts) is short by tokens
    regardless of what the code does, and the code side (M17's new check) has
    not landed either. Both have to move together for the assertion below to
    pass -- fixing only the words would make the three texts agree with each
    other on a claim the code still doesn't keep."""
    from mvp.pricing_feeds import fetch as fetch_module

    doc_text = _DOC_PATH.read_text(encoding="utf-8")

    with pytest.raises(SystemExit):
        fetch_module.main(["--help"])
    help_text = capsys.readouterr().out

    docstring_text = fetch_module._exit_code.__doc__ or ""

    sources = {
        "docs/design/price-feeds.md": doc_text,
        "--help": help_text,
        "_exit_code's docstring": docstring_text,
    }
    named = {label: _strict_reasons_named_in(text) for label, text in sources.items()}
    full_set = _STRICT_REASON_TOKENS

    disagreements = {
        label: sorted(full_set - reasons)
        for label, reasons in named.items()
        if reasons != full_set
    }
    assert not disagreements, (
        "the three places that enumerate --strict's exit reasons are missing "
        f"tokens from the contracted set {sorted(full_set)}: "
        + "; ".join(f"{label} is missing {missing}"
                    for label, missing in sorted(disagreements.items()))
        + ". coverage_regression is already checked by _exit_code today and "
          "simply unnamed everywhere; feed_not_authorized and "
          "unpriced_not_allowlisted are M17's and are missing from BOTH the "
          "text and _exit_code's actual check -- fixing the words alone would "
          "make the three texts agree with each other on a claim the code "
          "still doesn't keep."
    )


def test_alert_list_names_leg_regression_table_partial_and_fetch_empty():
    """Q5. `docs/design/price-feeds.md`'s "Events worth alerting on" list names
    four events: `price_feed_coverage_regression`, `price_feed_unparsed_names`,
    `price_feed_key_spans_prices` and `price_table_changed`. It omits two events
    `composite.py` already emits today (grounded below rather than asserted by
    name alone, so a rename would fail this for the right reason) --
    `price_feed_leg_regression` and `price_feed_table_partial` -- and per M7 it
    also has to grow `price_feed_fetch_empty`, the new WARNING a pass yielding
    zero keys against a non-empty snapshot is supposed to emit, which is not in
    the code yet either."""
    import inspect

    from mvp.pricing_feeds import composite

    composite_source = inspect.getsource(composite)
    already_emitted = {
        name for name in ("price_feed_leg_regression", "price_feed_table_partial")
        if re.search(rf'"{name}"', composite_source)
    }
    assert already_emitted == {"price_feed_leg_regression", "price_feed_table_partial"}, (
        f"fixture assumption broken: composite.py no longer emits both "
        f"price_feed_leg_regression and price_feed_table_partial (found only "
        f"{sorted(already_emitted)}), so the 'already real, just undocumented' "
        f"half of this test's premise no longer holds"
    )

    text = _DOC_PATH.read_text(encoding="utf-8")
    required = already_emitted | {"price_feed_fetch_empty"}
    missing = sorted(name for name in required if name not in text)
    assert not missing, (
        f"docs/design/price-feeds.md's alert list is missing: {missing}. "
        f"price_feed_leg_regression and price_feed_table_partial are already "
        f"emitted by composite.py today and are simply undocumented; "
        f"price_feed_fetch_empty does not exist in the code yet either (M7) -- "
        f"the document has to name all three, and the code has to grow the "
        f"third."
    )


# ---------------------------------------------------------------------------
# Q21 -- `dimensions.py`'s module docstring and `selfhosted.py`'s `_card_for`
# docstring both say `select()` refuses unless every token class asked for is
# present. It refuses only on a missing input or output, and answers a missing
# cache leg through `Selection.absent` for the caller's per-leg fallback -- the
# two sentences describe the opposite of what the function they sit next to
# does.
# ---------------------------------------------------------------------------


def test_select_refuses_only_on_missing_input_or_output_not_a_cache_leg():
    """Q21, the behavioural half of the pin. A card that prices input and output
    but neither cache class must NOT be refused -- `select()`'s own docstring
    already says as much ("Returns `None` — refuse this model — only when input
    or output does not resolve at all") -- and the missing classes must surface
    through `Selection.absent`, which is the datum `SelfHostedFeed._card_for`
    exists to avoid triggering by publishing an explicit zero instead."""
    from decimal import Decimal

    from mvp.pricing_feeds.dimensions import RateDimension, select

    card = {
        (None, RateDimension("input", "geo")): Decimal(5),
        (None, RateDimension("output", "geo")): Decimal(10),
    }
    selection = select(card, regions=(), scope=None)
    assert selection is not None, (
        "a card missing only cache legs must not be refused -- Selection.absent "
        "exists precisely so the caller can fall back per leg instead"
    )
    assert selection.absent == frozenset({"cache_read", "cache_write"}), selection.absent

    no_output = {(None, RateDimension("input", "geo")): Decimal(5)}
    assert select(no_output, regions=(), scope=None) is None, (
        "a card missing output must still be refused -- that half of the claim "
        "is true and this is the negative control for it"
    )


def test_dimensions_module_docstring_does_not_overclaim_selects_refusal():
    """Q21, the textual half. Reads `dimensions.py`'s own module docstring rather
    than a copy of its wording, so a future edit that keeps the false claim under
    different phrasing does not accidentally satisfy this by no longer matching
    today's exact sentence -- the assertion is on the CLAIM ("select refuses
    unless every class is present"), located by its two most specific anchors."""
    import mvp.pricing_feeds.dimensions as dimensions_module

    doc = dimensions_module.__doc__ or ""
    overclaim = re.search(
        r"select\(\).{0,40}refuses.{0,60}unless every token class", doc, re.DOTALL
    )
    assert overclaim is None, (
        f"dimensions.py's module docstring still claims select() refuses unless "
        f"every token class asked for is present, matched at {overclaim!r}. "
        f"Selection.absent exists because that is false -- it refuses only on a "
        f"missing input or output. Restate the sentence rather than deleting it: "
        f"the module's leniency/strictness split is real documentation, just "
        f"wrong about which side select() is on."
    )


def test_selfhosted_card_for_docstring_does_not_overclaim_selects_refusal():
    """Q21, the second textual anchor. `SelfHostedFeed._card_for` publishes an
    explicit zero for both cache legs and its own docstring gives the reason as
    "omitting them would make `dimensions.select` refuse the model" -- the same
    false claim as the module docstring above, independently written, in the
    file whose only reason to publish a zero would evaporate if the claim were
    true and evaporates anyway because it is not."""
    from mvp.pricing_feeds.selfhosted import _card_for

    doc = _card_for.__doc__ or ""
    overclaim = re.search(r"dimensions\.select`?\s+refuse", doc)
    assert overclaim is None, (
        f"selfhosted.py's _card_for docstring still says omitting a cache leg "
        f"would make dimensions.select refuse the model: {doc!r}. It would not -- "
        f"a missing cache leg lands in Selection.absent, refused only when input "
        f"or output is missing."
    )
