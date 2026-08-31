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
    from dynamo.credit_ledger import ledger_pk, terminal_sk
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
        "event_type": "TERMINAL",
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
    from dynamo.credit_ledger import ledger_pk, terminal_sk

    repo._table.put_item(Item={
        "pk": ledger_pk(_TENANT, _PERIOD), "sk": terminal_sk("h1"),
        "hold_id": "h1", "event_type": "TERMINAL", "rating": "{not json",
    })
    report = reprice.reprice_period(tenant_id=_TENANT, period=_PERIOD,
                                    target_rates={}, target_label="test", repo=repo)
    assert report.not_repriced == {"unparseable_rating": 1}
    assert report.events_priced == 0


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
    store.save(was_in_force, {"haiku": "bedrock-agreement(input,output)"})
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
