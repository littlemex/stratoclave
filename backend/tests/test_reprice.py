"""Tokens are the record of origin, so a charge can be recomputed at another table.

The point of these tests is the property, not the arithmetic: every terminal money event
carries the token count and the rate for each leg, so "what would this period have cost
at a different rate table" is a multiplication over stored facts. That is what makes a
price correction possible after the fact — and it is why the price-feed store keeps a
version per change rather than only the latest table.

What is deliberately NOT here is a write path. The recompute reports; moving money to
close the difference is a separate decision with its own contract, and a report that
cannot alter the ledger is one that is safe to run against production.
"""
from __future__ import annotations

import json

import pytest

from mvp import reprice
from mvp.rates import Rate

_TENANT = "acme"
_PERIOD = "2026-08"


def _seed(repo, *, hold_id: str, pricing_key: str, tokens: dict[str, int],
         rates: Rate, rounding: str = "ceil") -> int:
    """Write one terminal event the way the settle path does, and return its charge."""
    from dynamo.credit_ledger import EV_SETTLE, ledger_pk, terminal_sk
    from mvp.pricing import mtok_cost_for_rounding

    components = {}
    total = 0
    for leg, count in tokens.items():
        rate = getattr(rates, reprice._RATE_FIELD_BY_COMPONENT[leg])
        cost = mtok_cost_for_rounding(count, int(rate), rounding)
        components[leg] = {"tokens": count, "rate_microusd_per_mtok": int(rate),
                           "cost_microusd": cost, "reported": True}
        total += cost
    repo._table.put_item(Item={
        "pk": ledger_pk(_TENANT, _PERIOD),
        "sk": terminal_sk(hold_id),
        "hold_id": hold_id,
        "event_type": EV_SETTLE,
        "settled_delta_microusd": total,
        "rating": json.dumps({
            "pricing_version": "builtin",
            "pricing_key": pricing_key,
            "rounding": rounding,
            "components": components,
            "total_cost_microusd": total,
        }),
    })
    return total


def _rating_via_rate_usage(*, pricing_key: str, tokens: dict[str, int], rate: Rate,
                           rounding: str = "ceil", version: str = "v1"):
    """The `RatingRecord` `mvp.pricing.rate_usage` -- the settle path's real, single money
    computation -- produces for `tokens` against `rate`.

    Building through it, rather than hand-writing a `{"components": {...}}` dict, is what
    keeps a fixture from drifting from the writer it claims to imitate (M11): production
    `rate_usage` always writes all four `BILLABLE_LEGS`, so a fixture that goes through it
    cannot silently invent a rating with fewer.
    """
    from mvp.pricing import RateSnapshot, rate_usage

    snapshot = RateSnapshot(
        version=version, pricing_key=pricing_key,
        input_per_mtok_microusd=rate.input_per_mtok_microusd,
        output_per_mtok_microusd=rate.output_per_mtok_microusd,
        cache_read_per_mtok_microusd=rate.cache_read_per_mtok_microusd,
        cache_write_per_mtok_microusd=rate.cache_write_per_mtok_microusd,
        rounding=rounding,
    )
    return rate_usage(
        snapshot,
        input_tokens=tokens.get("input", 0),
        output_tokens=tokens.get("output", 0),
        cache_read_tokens=tokens.get("cache_read", 0),
        cache_write_tokens=tokens.get("cache_write", 0),
    )


def _put_settle(repo, *, hold_id: str, rating_dict: dict, settled_delta: int) -> None:
    """Write one SETTLE terminal whose `rating` is `rating_dict` -- the same JSON-encoded
    shape `terminal_event_txn_item` freezes onto a real terminal via
    `RatingRecord.to_ledger_dict()`."""
    from dynamo.credit_ledger import EV_SETTLE, ledger_pk, terminal_sk

    repo._table.put_item(Item={
        "pk": ledger_pk(_TENANT, _PERIOD), "sk": terminal_sk(hold_id),
        "hold_id": hold_id, "event_type": EV_SETTLE,
        "settled_delta_microusd": settled_delta,
        "rating": json.dumps(rating_dict),
    })


@pytest.fixture()
def repo(dynamodb_mock):
    from dynamo.credit_ledger import CreditLedgerRepository

    return CreditLedgerRepository()


def test_a_period_recomputes_at_another_table_from_the_stored_tokens(repo):
    charged_rate = Rate(3_300_000, 16_500_000, 330_000, 4_125_000)   # sonnet, in-region
    target_rate = Rate(2_200_000, 11_000_000, 220_000, 2_750_000)    # what Sonnet 5 lists at
    charged = _seed(repo, hold_id="h1", pricing_key="sonnet",
                    tokens={"input": 1_000_000, "output": 500_000,
                            "cache_read": 0, "cache_write": 0},
                    rates=charged_rate)

    report = reprice.reprice_period(
        tenant_id=_TENANT, period=_PERIOD,
        target_rates={"sonnet": target_rate}, target_label="test", repo=repo)

    assert report.events_priced == 1
    assert report.as_charged_microusd == charged
    # 1 MTok input + 0.5 MTok output at the target rate.
    assert report.as_repriced_microusd == 2_200_000 + 5_500_000
    assert report.difference_microusd == report.as_repriced_microusd - charged
    assert report.by_pricing_key["sonnet"]["events"] == 1
    assert report.not_repriced == {}


def test_the_cache_legs_are_part_of_the_recompute(repo):
    """A charge that priced four legs cannot be recomputed from two. The ledger stores
    all four, which is exactly why the audit row now records them too."""
    rate = Rate(1_000_000, 2_000_000, 100_000, 1_250_000)
    charged = _seed(repo, hold_id="h1", pricing_key="haiku",
                    tokens={"input": 100_000, "output": 100_000,
                            "cache_read": 1_000_000, "cache_write": 1_000_000},
                    rates=rate)
    doubled = Rate(2_000_000, 4_000_000, 200_000, 2_500_000)
    report = reprice.reprice_period(tenant_id=_TENANT, period=_PERIOD,
                                    target_rates={"haiku": doubled},
                                    target_label="test", repo=repo)
    assert report.as_charged_microusd == charged
    assert report.as_repriced_microusd == charged * 2, (
        "every leg has to move, or the cache legs are being dropped"
    )


def test_the_original_rounding_policy_is_reused_not_todays_default(repo):
    """A recompute that changed how fractions round would report a difference that is
    partly its own doing."""
    rate = Rate(1_000_000, 1_000_000, 1_000_000, 1_000_000)
    _seed(repo, hold_id="h1", pricing_key="haiku",
          tokens={"input": 1, "output": 0, "cache_read": 0, "cache_write": 0},
          rates=rate, rounding="ceil")
    report = reprice.reprice_period(tenant_id=_TENANT, period=_PERIOD,
                                    target_rates={"haiku": rate},
                                    target_label="test", repo=repo)
    # One token at $1/MTok is a fraction of a micro-USD, and ceil is what the charge
    # froze, so the recompute reproduces the charge exactly rather than truncating to 0.
    assert report.as_repriced_microusd == report.as_charged_microusd == 1


def test_a_key_the_target_table_does_not_price_is_reported_not_skipped(repo):
    """A total that silently omits part of a period is worse than no total, because it
    looks like one."""
    rate = Rate(1_000_000, 1_000_000, 100_000, 1_250_000)
    _seed(repo, hold_id="h1", pricing_key="haiku",
          tokens={"input": 1_000_000, "output": 0, "cache_read": 0, "cache_write": 0},
          rates=rate)
    _seed(repo, hold_id="h2", pricing_key="gone",
          tokens={"input": 1_000_000, "output": 0, "cache_read": 0, "cache_write": 0},
          rates=rate)
    report = reprice.reprice_period(tenant_id=_TENANT, period=_PERIOD,
                                    target_rates={"haiku": rate},
                                    target_label="test", repo=repo)
    assert report.events_priced == 1
    assert report.not_repriced == {"pricing_key_absent_from_target": 1}
    assert report.keys_missing_from_target == ["gone"]


def test_an_unreadable_rating_is_counted_rather_than_dropped(repo):
    from dynamo.credit_ledger import EV_SETTLE, ledger_pk, terminal_sk

    repo._table.put_item(Item={
        "pk": ledger_pk(_TENANT, _PERIOD), "sk": terminal_sk("h1"),
        "hold_id": "h1", "event_type": EV_SETTLE, "settled_delta_microusd": 7,
        "rating": "{not json",
    })
    report = reprice.reprice_period(tenant_id=_TENANT, period=_PERIOD,
                                    target_rates={}, target_label="test", repo=repo)
    assert report.not_repriced == {"unparseable_rating": 1}
    assert report.events_priced == 0
    # The money still moved, so it is in the as-charged total and the report says it is
    # not complete. A difference measured against a smaller period than the one asked
    # about is a wrong number that looks like a right one.
    assert report.as_charged_microusd == 7
    assert report.events_seen == 1
    assert report.complete is False


def test_as_charged_is_the_settled_delta_not_the_rating_s_self_report(repo):
    """The charge of record is the ledger's money move. When a rating disagrees with it —
    a bad total, a `true` where a number belongs — the rating is the thing in doubt, and
    its token counts stop being evidence of what the charge was for."""
    from dynamo.credit_ledger import EV_SETTLE, ledger_pk, terminal_sk

    repo._table.put_item(Item={
        "pk": ledger_pk(_TENANT, _PERIOD), "sk": terminal_sk("h1"),
        "hold_id": "h1", "event_type": EV_SETTLE,
        "settled_delta_microusd": 11_000_000,
        "rating": json.dumps({
            "pricing_key": "haiku", "rounding": "ceil",
            # `int(True)` is 1: a bool must be refused rather than read as one micro-USD.
            "total_cost_microusd": True,
            "components": {"input": {"tokens": 1_000_000,
                                     "rate_microusd_per_mtok": 1_000_000,
                                     "cost_microusd": 1_000_000, "reported": True}},
        }),
    })
    report = reprice.reprice_period(
        tenant_id=_TENANT, period=_PERIOD,
        target_rates={"haiku": Rate(1_000_000, 1, 1, 1)}, target_label="test", repo=repo)
    assert report.as_charged_microusd == 11_000_000
    assert report.as_repriced_microusd == 0
    assert report.not_repriced == {"rating_disagrees_with_settled_delta": 1}
    assert report.complete is False


def test_a_terminal_with_no_rating_is_counted_and_marks_the_report_incomplete(repo):
    from dynamo.credit_ledger import EV_SETTLE, ledger_pk, terminal_sk

    repo._table.put_item(Item={
        "pk": ledger_pk(_TENANT, _PERIOD), "sk": terminal_sk("h1"),
        "hold_id": "h1", "event_type": EV_SETTLE,
        "settled_delta_microusd": 5_000_000,
    })
    report = reprice.reprice_period(tenant_id=_TENANT, period=_PERIOD,
                                    target_rates={}, target_label="test", repo=repo)
    assert report.as_charged_microusd == 5_000_000
    assert report.not_repriced == {"missing_rating": 1}
    assert report.complete is False


def test_the_effective_target_neither_fetches_nor_persists(dynamodb_mock, monkeypatch):
    """A tool that prints "read-only" must not write the state charging depends on.
    Resolving the active price source would run a live fetch on a cold process and store a
    new version, so the layers are read directly instead."""
    from mvp.pricing_feeds import composite

    def _explode(*_args, **_kwargs):
        raise AssertionError("the effective target must not resolve the live source")

    monkeypatch.setattr(composite.LivePriceSource, "load", _explode)
    monkeypatch.setattr(composite.LivePriceSource, "refresh", _explode)
    rates, label = reprice.target_from_effective()
    assert rates, "the floor at least must answer"
    # Named for what it can see: a task that failed to persist a fresh fetch charges from
    # its own memory, and no report can read another process's memory.
    assert label.startswith("durable-effective(")


def test_the_recompute_writes_nothing(repo):
    """The charge of record stands. A correction would be a new idempotent adjustment
    event, which is a separate decision with its own contract — not an edit made by a
    reporting tool."""
    from dynamo.credit_ledger import ledger_pk

    rate = Rate(1_000_000, 1_000_000, 100_000, 1_250_000)
    _seed(repo, hold_id="h1", pricing_key="haiku",
          tokens={"input": 1_000_000, "output": 0, "cache_read": 0, "cache_write": 0},
          rates=rate)
    from boto3.dynamodb.conditions import Key

    before = repo._table.query(
        KeyConditionExpression=Key("pk").eq(ledger_pk(_TENANT, _PERIOD)))["Items"]
    reprice.reprice_period(tenant_id=_TENANT, period=_PERIOD,
                           target_rates={"haiku": Rate(9, 9, 9, 9)},
                           target_label="test", repo=repo)
    after = repo._table.query(
        KeyConditionExpression=Key("pk").eq(ledger_pk(_TENANT, _PERIOD)))["Items"]
    assert before == after


def test_a_stored_feed_version_can_be_the_target(dynamodb_mock, repo):
    """The version history's purpose: the table that WAS in force is readable, so a
    recompute at it is a lookup rather than a reconstruction."""
    from mvp.pricing_feeds.snapshot import SnapshotStore, digest_of

    was_in_force = {"haiku": Rate(1_100_000, 5_500_000, 110_000, 1_375_000)}
    store = SnapshotStore()
    store.save(was_in_force, {"haiku": "bedrock-agreement(input,output)"}, fenced_on=None)
    rates, label = reprice.target_from_feed_version(digest_of(was_in_force))
    assert rates == was_in_force
    assert digest_of(was_in_force) in label

    with pytest.raises(ValueError):
        reprice.target_from_feed_version("no-such-version")


def test_the_usage_row_records_every_leg_it_was_charged_on(dynamodb_mock):
    """The audit row's stated purpose is that spend be re-derivable from it. Two legs out
    of four could only re-derive a request that used no prompt cache."""
    from dynamo import UsageLogsRepository

    item = UsageLogsRepository().record(
        tenant_id=_TENANT, user_id="u1", user_email="a@b.c",
        model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        input_tokens=10, output_tokens=20,
        cache_read_tokens=30, cache_write_tokens=40,
        request_id="req-1", cost_microusd=123,
    )
    assert int(item["cache_read_tokens"]) == 30
    assert int(item["cache_write_tokens"]) == 40

    # Absent, not zero, when the provider reported nothing: "this model does not cache"
    # and "this request did not cache" are different facts.
    bare = UsageLogsRepository().record(
        tenant_id=_TENANT, user_id="u1", user_email="a@b.c",
        model_id="us.anthropic.claude-3-haiku-20240307-v1:0",
        input_tokens=10, output_tokens=20, request_id="req-2",
    )
    assert "cache_read_tokens" not in bare
    assert "cache_write_tokens" not in bare


def test_a_real_settle_event_is_counted(repo):
    """The event type comes from the ledger's own constants. This module once read
    `"TERMINAL"` — the name of a SORT KEY, not of any event the writer emits — so every
    real charge was invisible and the report still said `complete`.

    (M11) The fixture used to write a rating with ONE component by hand under a docstring
    claiming it "writes what the settle path writes" — but production `rate_usage` always
    writes all four `BILLABLE_LEGS`, so that hand-written shape was never the shape a real
    settle produces, and nothing here checked the difference. Built through
    `_rating_via_rate_usage` (the real writer) instead, so this fixture cannot drift from
    the writer it claims to imitate again."""
    rate = Rate(1_000_000, 1_000_000, 1_000_000, 1_000_000)
    record = _rating_via_rate_usage(pricing_key="haiku", tokens={"input": 5_000_000},
                                    rate=rate)
    assert set(record.components) == {"input", "output", "cache_read", "cache_write"}, (
        "the real writer always writes all four legs; if this fails, BILLABLE_LEGS moved"
    )
    _put_settle(repo, hold_id="h1", rating_dict=record.to_ledger_dict(),
               settled_delta=record.total_cost_microusd)

    report = reprice.reprice_period(tenant_id=_TENANT, period=_PERIOD,
                                    target_rates={"haiku": rate}, target_label="test",
                                    repo=repo)
    assert report.events_seen == report.events_priced == 1
    assert report.as_charged_microusd == record.total_cost_microusd == 5_000_000
    assert report.as_repriced_microusd == 5_000_000
    assert report.complete is True


def test_a_rating_missing_a_billable_leg_is_not_repriced_as_complete(repo):
    """M11: `reprice_period` never checks a rating's component set against
    `BILLABLE_LEGS`. Before this fix, a rating short a leg was repriced as though it priced
    everything — the missing leg's cost simply never entered the sum, `events_priced`
    still counted it, and `complete` stayed true over a period that quietly lost a leg's
    worth of money.

    Starts from the real writer (`_rating_via_rate_usage`) so every other fact about the
    rating is exactly what the settle path produces; the one deliberate departure from it —
    deleting `cache_write` — is the shape M11 says this module must catch rather than
    silently price at three legs out of four."""
    charged_rate = Rate(1_000_000, 1_000_000, 1_000_000, 1_000_000)
    target_rate = Rate(2_000_000, 2_000_000, 2_000_000, 2_000_000)
    record = _rating_via_rate_usage(
        pricing_key="haiku",
        tokens={"input": 1_000_000, "output": 500_000, "cache_read": 200_000,
                "cache_write": 2_000_000},
        rate=charged_rate)
    rating_dict = record.to_ledger_dict()
    del rating_dict["components"]["cache_write"]
    _put_settle(repo, hold_id="h1", rating_dict=rating_dict,
               settled_delta=record.total_cost_microusd)

    report = reprice.reprice_period(tenant_id=_TENANT, period=_PERIOD,
                                    target_rates={"haiku": target_rate}, target_label="test",
                                    repo=repo)
    assert report.events_seen == 1
    # Period totals still cover the event even though it could not be repriced.
    assert report.as_charged_microusd == record.total_cost_microusd
    assert report.events_priced == 0
    assert report.as_repriced_microusd == 0
    assert report.complete is False
    assert report.not_repriced, (
        "a rating short a billable leg must be refused, not silently priced at three "
        "legs out of four"
    )


def test_an_owed_settle_is_not_counted_twice_after_the_late_settle_lands(repo):
    """`OWED_SETTLE` is evidence that a charge is owed, not money that moved. Counting it
    doubles a period the moment the reaper posts the real one."""
    from dynamo.credit_ledger import (EV_LATE_SETTLE, ledger_pk, late_settle_sk,
                                      owed_settle_sk)

    rating = json.dumps({
        "pricing_key": "haiku", "rounding": "ceil", "total_cost_microusd": 1_000_000,
        "components": {"input": {"tokens": 1_000_000,
                                 "rate_microusd_per_mtok": 1_000_000,
                                 "cost_microusd": 1_000_000, "reported": True}},
    })
    repo._table.put_item(Item={
        "pk": ledger_pk(_TENANT, _PERIOD), "sk": owed_settle_sk("h1"),
        "hold_id": "h1", "event_type": "OWED_SETTLE",
        "settled_delta_microusd": 1_000_000, "rating": rating,
    })
    repo._table.put_item(Item={
        "pk": ledger_pk(_TENANT, _PERIOD), "sk": late_settle_sk("h1"),
        "hold_id": "h1", "event_type": EV_LATE_SETTLE,
        "settled_delta_microusd": 1_000_000, "rating": rating,
    })
    report = reprice.reprice_period(
        tenant_id=_TENANT, period=_PERIOD,
        target_rates={"haiku": Rate(1_000_000, 1, 1, 1)}, target_label="test", repo=repo)
    assert report.events_seen == 1
    assert report.as_charged_microusd == 1_000_000


# ---------------------------------------------------------------------------
# M6 — the headline difference must be over the repriced population, while the
# period totals still cover every event.
# ---------------------------------------------------------------------------


def test_the_contracts_reproduction_case_charged_1e6_difference_0(repo):
    """M6's own reproduction: one settled event whose pricing key is absent from the
    target, charged 1,000,000. Nothing was repriced, so the honest headline difference is
    `0` over the (empty) comparable population — not `-1,000,000`, which is what
    subtracting the whole period's as-charged total from a zero as-repriced total says
    today."""
    rate = Rate(1_000_000, 1_000_000, 1_000_000, 1_000_000)
    charged = _seed(repo, hold_id="h1", pricing_key="gone",
                    tokens={"input": 1_000_000, "output": 0, "cache_read": 0,
                            "cache_write": 0},
                    rates=rate)
    assert charged == 1_000_000

    report = reprice.reprice_period(tenant_id=_TENANT, period=_PERIOD,
                                    target_rates={}, target_label="test", repo=repo)

    assert report.as_charged_microusd == 1_000_000
    assert report.comparable_as_charged_microusd == 0
    assert report.difference_microusd == 0
    assert report.complete is False


def test_the_headline_difference_is_over_the_repriced_population_only(repo):
    """M6: `reprice`'s headline difference used to subtract two different populations —
    `as_repriced_microusd` summed over the events that COULD be repriced, subtracted from
    `as_charged_microusd` summed over every event in the period. One unrepriceable event
    then reports a refund that did not happen. The period totals (`as_charged_microusd`)
    must still cover every event; the headline (`difference_microusd`) must be computed
    against `comparable_as_charged_microusd`, the as-charged total of the repriced
    population only."""
    charged_rate = Rate(1_000_000, 1_000_000, 1_000_000, 1_000_000)
    target_rate = Rate(2_000_000, 2_000_000, 2_000_000, 2_000_000)
    priced = _seed(repo, hold_id="h-priced", pricing_key="haiku",
                   tokens={"input": 1_000_000, "output": 0, "cache_read": 0,
                           "cache_write": 0},
                   rates=charged_rate)
    unrepriceable = _seed(repo, hold_id="h-gone", pricing_key="gone",
                          tokens={"input": 2_000_000, "output": 0, "cache_read": 0,
                                  "cache_write": 0},
                          rates=charged_rate)

    report = reprice.reprice_period(tenant_id=_TENANT, period=_PERIOD,
                                    target_rates={"haiku": target_rate}, target_label="test",
                                    repo=repo)

    # Period totals cover every event, repriceable or not.
    assert report.as_charged_microusd == priced + unrepriceable
    # 1 MTok of input at the doubled target rate — the repriced population is one event.
    repriced_total = 2_000_000
    assert report.as_repriced_microusd == repriced_total
    assert report.comparable_as_charged_microusd == priced
    assert report.difference_microusd == repriced_total - priced
    assert report.complete is False


def test_the_cli_exits_2_when_the_report_is_incomplete(repo, capsys):
    """M6 (CLI): the tool exists to answer a dispute, so a caller reading only the exit
    code must be able to tell a complete report from a partial one. Reproduced: charged
    1,000,000, nothing repriceable, and the CLI must not exit 0."""
    rate = Rate(1_000_000, 1_000_000, 1_000_000, 1_000_000)
    _seed(repo, hold_id="h1", pricing_key="not-on-the-target-table",
          tokens={"input": 1_000_000, "output": 0, "cache_read": 0, "cache_write": 0},
          rates=rate)

    code = reprice.main(["--tenant", _TENANT, "--period", _PERIOD, "--at", "floor",
                         "--json"])
    out = json.loads(capsys.readouterr().out)

    assert out["complete"] is False
    assert out["difference_microusd"] == 0
    assert out["as_charged_microusd"] == 1_000_000
    assert code == 2


# ---------------------------------------------------------------------------
# M10 — a charge-type event with no readable settled delta must be counted, not
# filtered out before it is ever seen.
# ---------------------------------------------------------------------------


def test_a_charge_event_with_no_settled_delta_is_counted_not_dropped(repo):
    """M10: `_charge_events` used to skip a charge-type event whose
    `settled_delta_microusd` is missing BEFORE yielding it, so it entered neither
    `events_seen` nor `not_repriced` and `complete` could read `True` over a period that
    silently lost an event. C2.10 says every money event is counted whether or not it
    carries a rating; this is the one case that hid the event before that rule ever ran."""
    from dynamo.credit_ledger import EV_SETTLE, ledger_pk, terminal_sk

    repo._table.put_item(Item={
        "pk": ledger_pk(_TENANT, _PERIOD), "sk": terminal_sk("h1"),
        "hold_id": "h1", "event_type": EV_SETTLE,
        # No settled_delta_microusd at all: the case this module must not make invisible.
    })

    report = reprice.reprice_period(tenant_id=_TENANT, period=_PERIOD,
                                    target_rates={}, target_label="test", repo=repo)

    assert report.events_seen == 1
    assert report.not_repriced == {"settled_delta_unreadable": 1}
    assert report.complete is False


def test_the_cli_exits_2_when_a_settled_delta_is_unreadable(repo, capsys):
    """M10 (CLI): the same event, through the CLI entry point the contract names. An
    event this invisible used to let the CLI exit 0; it must exit 2 instead."""
    from dynamo.credit_ledger import EV_SETTLE, ledger_pk, terminal_sk

    repo._table.put_item(Item={
        "pk": ledger_pk(_TENANT, _PERIOD), "sk": terminal_sk("h1"),
        "hold_id": "h1", "event_type": EV_SETTLE,
    })

    code = reprice.main(["--tenant", _TENANT, "--period", _PERIOD, "--at", "floor",
                         "--json"])
    out = json.loads(capsys.readouterr().out)

    assert out["events_seen"] == 1
    assert out["not_repriced"] == {"settled_delta_unreadable": 1}
    assert out["complete"] is False
    assert code == 2


# ---------------------------------------------------------------------------
# Q19 — a rating with no frozen rounding is refused, not priced at today's ceil.
# ---------------------------------------------------------------------------


def test_a_rating_with_no_frozen_rounding_is_refused(repo):
    """Q19 (resolved: refuse). `rate_usage` raises unless the snapshot's rounding policy
    is `ceil`, so a rating with no `rounding` field at all never came from the current
    writer — it is legacy or corrupt. Replaying it under an assumed policy is the same
    defect as M6 and M10 one field lower: filling in a fact instead of reading it. The
    event stays inside `as_charged_microusd` (period totals still cover it) but is refused
    as `rating_without_rounding`, not silently priced at today's default."""
    from dynamo.credit_ledger import EV_SETTLE, ledger_pk, terminal_sk

    repo._table.put_item(Item={
        "pk": ledger_pk(_TENANT, _PERIOD), "sk": terminal_sk("h1"),
        "hold_id": "h1", "event_type": EV_SETTLE,
        "settled_delta_microusd": 5_000_000,
        "rating": json.dumps({
            "pricing_key": "haiku",
            # No "rounding" key at all — the shape `rate_usage` never writes, because it
            # raises rather than freeze an unsupported policy.
            "total_cost_microusd": 5_000_000,
            "components": {
                "input": {"tokens": 5_000_000, "rate_microusd_per_mtok": 1_000_000,
                          "cost_microusd": 5_000_000, "reported": True},
                "output": {"tokens": 0, "rate_microusd_per_mtok": 1_000_000,
                          "cost_microusd": 0, "reported": True},
                "cache_read": {"tokens": 0, "rate_microusd_per_mtok": 1_000_000,
                              "cost_microusd": 0, "reported": True},
                "cache_write": {"tokens": 0, "rate_microusd_per_mtok": 1_000_000,
                               "cost_microusd": 0, "reported": True},
            },
        }),
    })

    rate = Rate(1_000_000, 1_000_000, 1_000_000, 1_000_000)
    report = reprice.reprice_period(tenant_id=_TENANT, period=_PERIOD,
                                    target_rates={"haiku": rate}, target_label="test",
                                    repo=repo)

    assert report.events_seen == 1
    assert report.as_charged_microusd == 5_000_000
    assert report.not_repriced == {"rating_without_rounding": 1}
    assert report.complete is False


# ---------------------------------------------------------------------------
# Q23 — the documented machine-readable output must parse as JSON on its own.
# ---------------------------------------------------------------------------


def test_the_json_cli_output_is_one_document_with_no_preamble(repo, capsys):
    """Q23: `main(["--json"])` used to emit the `reprice_report` structlog line on stdout
    immediately before the JSON print, so the output documented as machine-readable did
    not parse as JSON by itself. `mvp/pricing_feeds/fetch.py` already solved exactly this
    (`contextlib.redirect_stdout` around the work, so logging lands elsewhere and stdout
    carries only the printed document). The point of this test is that no stripping
    should be necessary: a plain `json.loads` on the whole of stdout must succeed."""
    rate = Rate(1_000_000, 1_000_000, 1_000_000, 1_000_000)
    _seed(repo, hold_id="h1", pricing_key="haiku",
          tokens={"input": 1_000_000, "output": 0, "cache_read": 0, "cache_write": 0},
          rates=rate)

    reprice.main(["--tenant", _TENANT, "--period", _PERIOD, "--at", "floor", "--json"])
    out = capsys.readouterr().out

    parsed = json.loads(out)  # must not raise: no preamble to strip
    assert parsed["tenant_id"] == _TENANT
    assert parsed["period"] == _PERIOD

