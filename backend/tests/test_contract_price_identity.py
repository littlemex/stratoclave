"""Contract 2 — price identity: one price, one snapshot, or no admission.

The contract clause these pin:

  C2.1 The reservation price and the settle price are computed by the same code
       over the same rate document.
  C2.2 The rate document that priced the admission is recorded with the
       reservation, and a live rate edit in between cannot change what this
       request is charged.

Both reviewers of the contract audit arrived at the same inversion independently:
freeze the rate FIRST and price the admission from the frozen value. Today the
hard-ceiling path does that, and two paths do not — the legacy estimate and the
`shadow_mode` amount are priced by a live cache read while the snapshot carried to
settle is a SECOND read, and a snapshot that cannot be taken degrades to a
live-rate settle instead of refusing the request. A reservation priced at a rate
the settle will not use is not a reservation, and a recorded version that priced
nothing is not evidence.

These tests fail before that change and pass after it.
"""
from __future__ import annotations

from dataclasses import dataclass

import boto3
import pytest
from fastapi import HTTPException

from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
from dynamo.user_tenants import UserTenantsRepository
from mvp import _pipeline
from mvp import pricing as _pricing
from mvp.rates import Rate
from mvp.routing import config as _routing_config


TENANT = "price-identity-org"
USER = "user-price-identity-0001"
MODEL = "claude-sonnet-4-6"
PRICING_KEY = "sonnet"

# Two rate documents that disagree by 10x, so any test that prices at the wrong
# one is off by an amount no rounding could explain.
CHEAP = Rate(
    input_per_mtok_microusd=1_000_000,
    output_per_mtok_microusd=1_000_000,
    cache_read_per_mtok_microusd=1_000_000,
    cache_write_per_mtok_microusd=1_000_000,
)
DEAR = Rate(
    input_per_mtok_microusd=10_000_000,
    output_per_mtok_microusd=10_000_000,
    cache_read_per_mtok_microusd=10_000_000,
    cache_write_per_mtok_microusd=10_000_000,
)


@dataclass
class _User:
    user_id: str
    org_id: str
    email: str = "u@example.com"


@pytest.fixture
def env(dynamodb_mock):
    _routing_config.reset_cache()
    _pricing.reset_cache()
    UserTenantsRepository().ensure(
        user_id=USER, tenant_id=TENANT, role="user", total_credit=10**12,
    )
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=TENANT, period=current_period(), pool_limit_microusd=10**11,
    )
    yield
    _routing_config.reset_cache()
    _pricing.reset_cache()


def _reserve(**kwargs):
    return _pipeline.reserve_credit_for_model(
        _User(user_id=USER, org_id=TENANT),
        1000,
        model_name=MODEL,
        input_tokens_est=500_000,
        max_output_tokens=100_000,
        wire_protocol="messages",
        **kwargs,
    )


def _price_from(snapshot, *, input_tokens_est: int, max_output_tokens: int) -> int:
    """What the admission amount must be if it was priced from `snapshot`.

    Written against the same public helpers the estimator uses, but from the
    snapshot object rather than the live cache, so it answers exactly one
    question: which rate document produced the reserved amount.
    """
    from mvp.pricing import (
        INPUT_SIDE,
        OUTPUT_SIDE,
        mtok_cost_for_rounding,
        rounding_slack_microusd,
        worst_rate_in_group,
    )

    return (
        mtok_cost_for_rounding(
            input_tokens_est, worst_rate_in_group(snapshot, INPUT_SIDE), "ceil")
        + mtok_cost_for_rounding(
            max_output_tokens, snapshot.output_per_mtok_microusd, "ceil")
        + rounding_slack_microusd(INPUT_SIDE, input_tokens_est)
        + rounding_slack_microusd(OUTPUT_SIDE, max_output_tokens)
    )


def _split_the_two_reads(monkeypatch):
    """Make the live read and the snapshot read disagree, deterministically.

    A TTL refresh or an admin flip landing between the two reads is a real
    interleaving, but reproducing it by timing would be a flaky test. Patching the
    two cache entry points to different documents reproduces the OUTCOME of that
    interleaving exactly: the live read says CHEAP, the frozen snapshot says DEAR.
    """
    monkeypatch.setattr(_pricing._cache, "get", lambda key, repo=None: CHEAP)
    monkeypatch.setattr(
        _pricing._cache,
        "snapshot_inputs",
        lambda repo=None: (None, {"default": DEAR, PRICING_KEY: DEAR}, set(), set()),
    )


class TestTheAdmissionAmountComesFromTheFrozenSnapshot:

    def test_legacy_estimate_is_priced_from_the_snapshot_it_records(self, env, monkeypatch):
        """The legacy path (`input_bytes is None`) priced the admission with a live
        cache read and let `reserve_credit` freeze its own snapshot afterwards. The
        two can differ, and settle uses the second one."""
        _split_the_two_reads(monkeypatch)
        ctx = _reserve()
        assert ctx.rate_snapshot is not None, "a priced reservation must carry its rates"
        expected = _price_from(
            ctx.rate_snapshot, input_tokens_est=500_000, max_output_tokens=100_000)
        assert ctx.pool_reserved_microusd == expected, (
            "the amount that gated admission was priced at a different rate "
            "document than the one recorded for the settle")

    def test_shadow_mode_reserves_at_the_rates_it_records(self, env, monkeypatch):
        """`shadow_mode` deliberately reserves the legacy amount rather than the
        bound — that is the operator's measurement discipline and stays. What must
        not differ is the RATE: the legacy amount is priced live while the bound's
        snapshot is frozen, so the reservation and the charge disagree on rates as
        well as on strategy."""
        _split_the_two_reads(monkeypatch)
        ctx = _reserve(input_bytes=2_000_000)
        assert ctx.rate_snapshot is not None
        priced_live = _price_from(
            _pricing._snapshot_from_rate("x", PRICING_KEY, CHEAP),
            input_tokens_est=500_000, max_output_tokens=100_000)
        assert ctx.pool_reserved_microusd != priced_live, (
            "the reserved amount was priced by the live cache read while the "
            "snapshot carried to settle came from a different document")

    def test_a_rate_that_cannot_be_frozen_refuses_the_request(self, env, monkeypatch):
        """There is no honest reservation without the rates it was admitted at.
        The degraded path recorded `snapshot-failed` and settled from the live
        table, which is precisely the edit-in-between C2.2 forbids."""
        def _boom(pricing_key, repo=None):
            raise RuntimeError("rate table unavailable")

        monkeypatch.setattr(_pricing, "snapshot_rates", _boom)
        with pytest.raises(HTTPException) as e:
            _reserve()
        assert e.value.status_code == 503
        assert e.value.detail["reason"] == "pricing_unavailable"

    def test_no_reservation_survives_a_refused_pricing(self, env, monkeypatch):
        """Failing closed must not leave a debit behind."""
        def _boom(pricing_key, repo=None):
            raise RuntimeError("rate table unavailable")

        before = TenantBudgetsRepository().get(TENANT, current_period()) or {}
        monkeypatch.setattr(_pricing, "snapshot_rates", _boom)
        with pytest.raises(HTTPException):
            _reserve()
        after = TenantBudgetsRepository().get(TENANT, current_period()) or {}
        assert int(after.get("pool_reserved_microusd", 0)) == int(
            before.get("pool_reserved_microusd", 0))

    def test_the_snapshot_failed_sentinel_is_no_longer_reachable(self):
        """The sentinel exists only to label a charge rated after admission from a
        live table. With pricing failing closed there is no such charge, so the
        sentinel must not be writable by any path — keeping it as an accepted
        version label would let the class come back silently."""
        from mvp.pricing import SNAPSHOT_FAILED_SENTINEL

        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        writers = []
        for p in (root / "mvp").rglob("*.py"):
            text = p.read_text()
            if "SNAPSHOT_FAILED_SENTINEL" in text and "rate_snapshot_failed" in text:
                writers.append(p.name)
        assert writers == [], (
            f"{SNAPSHOT_FAILED_SENTINEL!r} is still stamped by {writers}; a charge "
            "rated at a rate the admission never saw is a C2.2 violation")


class TestTheTerminalCitesTheRateTheAdmissionSaw:
    """The bridge `docs/EVIDENCE.md` listed as **absent**: Z3 proves that pinning
    is sufficient for the ceiling, and "the code honours the pin" was a trusted
    assumption because no test flipped `CURRENT` between reserve and settle. This
    is that test."""

    def test_a_rate_flip_between_reserve_and_settle_does_not_move_the_charge(
            self, env):
        from dynamo.pricing_config import PricingConfigRepository

        repo = PricingConfigRepository()
        repo.set_rates(version="v1", rates={PRICING_KEY: CHEAP})
        _pricing.reset_cache()
        _pricing.reset_version_cache()

        ctx = _reserve()
        admitted = ctx.rate_snapshot
        assert admitted is not None and admitted.version == "v1"

        # The operator raises prices while the request is in flight.
        repo.set_rates(version="v2", rates={PRICING_KEY: DEAR})
        _pricing.reset_cache()

        _pipeline.settle_reservation_and_log(
            user=_User(user_id=USER, org_id=TENANT), tenants_repo=ctx,
            reservation=1000, actual_input_tokens=1_000_000,
            actual_output_tokens=1_000_000,
            model_id=MODEL, context=ctx,
        )

        events = _ledger_events()
        terminals = [e for e in events if str(e.get("event_type", "")).endswith("SETTLE")]
        assert terminals, f"no SETTLE terminal written; events={[e.get('event_type') for e in events]}"
        settled = terminals[-1]
        assert str(settled.get("pricing_version")) == "v1", (
            "the terminal cites the version in force at settle time, not the one "
            "the request was admitted under")
        rating = _as_obj(settled.get("rating")) or {}
        components = _as_obj(rating.get("components")) or {}
        rates_seen = {
            int(leg.get("rate_microusd_per_mtok", 0))
            for leg in components.values()
            if int(leg.get("tokens", 0)) > 0
        }
        assert rates_seen and rates_seen == {CHEAP.input_per_mtok_microusd}, (
            f"the charge was rated at {rates_seen}, not at the admitted rate "
            f"{CHEAP.input_per_mtok_microusd}")


def _as_obj(v):
    """Ledger attributes arrive as JSON strings for nested shapes."""
    if isinstance(v, str):
        import json
        return json.loads(v)
    return v


def _ledger_events() -> list:
    from dynamo.client import credit_ledger_table_name

    table = boto3.resource("dynamodb", region_name="us-east-1").Table(
        credit_ledger_table_name())
    return table.scan().get("Items", [])


class TestTheRateDocumentIsOneCompleteValidatedValue:
    """C2 rate validity. A rate document is a value, not a bag of coercible
    attributes: a negative leg mints credit, a missing leg silently prices at
    zero, and a version read that stops at the first page is a different document
    than the one the operator wrote."""

    def test_a_negative_rate_is_refused_at_the_write(self, dynamodb_mock):
        from dynamo.pricing_config import PricingConfigRepository

        repo = PricingConfigRepository()
        with pytest.raises(ValueError):
            repo.set_rates(
                version="vneg",
                rates={PRICING_KEY: Rate(
                    input_per_mtok_microusd=-1_000_000,
                    output_per_mtok_microusd=1,
                    cache_read_per_mtok_microusd=1,
                    cache_write_per_mtok_microusd=1,
                )},
            )

    def test_a_negative_rate_cannot_produce_a_charge(self):
        """Even if a row is written out of band, the rating fold must refuse it
        rather than compute credit. `mtok_cost_for_rounding` already refuses an
        unknown rounding policy instead of guessing; a negative rate is the same
        class of input."""
        with pytest.raises(ValueError):
            _pricing.mtok_cost_for_rounding(1000, -5_000_000, "ceil")

    def test_a_version_read_that_paginates_returns_every_row(self, dynamodb_mock, monkeypatch):
        """A single Query response is not a complete result set. The read side
        silently dropping rows means the charge uses the floor rate while the
        operator console reports the version they wrote."""
        from dynamo.pricing_config import PricingConfigRepository

        repo = PricingConfigRepository()
        rates = {f"key-{i}": CHEAP for i in range(5)}
        repo.set_rates(version="vpage", rates=rates)

        real_table = repo._table

        class _OnePageAtATime:
            """Answers like DynamoDB under a 1 MB cap: one item per page."""

            def __init__(self, inner):
                self._inner = inner
                self._all = None

            def query(self, **kwargs):
                if self._all is None:
                    self._all = self._inner.query(**kwargs).get("Items", [])
                start = kwargs.get("ExclusiveStartKey")
                idx = 0 if start is None else int(start["_i"])
                page = self._all[idx:idx + 1]
                out = {"Items": page}
                if idx + 1 < len(self._all):
                    out["LastEvaluatedKey"] = {"_i": idx + 1}
                return out

            def __getattr__(self, name):
                return getattr(self._inner, name)

        monkeypatch.setattr(repo, "_table", _OnePageAtATime(real_table))
        loaded = repo.load_rates("vpage")
        assert set(loaded) == set(rates), (
            "load_rates stopped at the first page; the document it returned is not "
            "the document the operator wrote")
