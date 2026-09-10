"""E10: the metering fault on the SUCCESS settle path.

`Hold.claim_settle` is reached once per request, after the event loop
finished with no exception (`mvp/_budget_flow.py`: `ending = hold.claim_settle(acc)`).
Before this contract item, it snapshotted the accumulator's token counts and
settled with them regardless of whether a usage event ever arrived — so a
stream that completed cleanly without ever delivering Bedrock's `metadata.usage`
event settled at zero, because `usage_from_bedrock` correctly refused to invent
counts nobody reported (see that function's docstring: "zero is a measurement,
absence is not"). A free bill for a served request is the exact defect this
closes.

The contract draws the line on `saw_final_usage`, never on the token value:

  - no usage event ever arrived (`saw_final_usage=False`) -> a metering fault:
    settle at the reservation's own dollar bound (the amount already reserved
    for this call), and mark the row so the fault is discoverable, without
    raising or blocking the response already streamed to the caller.
  - a usage event DID arrive and reported a genuine zero output
    (`saw_final_usage=True`, output=0) -> not a fault: settle at whatever that
    real usage actually costs, and do not mark the row.

These two are written as a pair on purpose (see
`test_reported_zero_output_settles_real_cost_and_is_not_marked`): a suite that
only ever pins the first of them is satisfied by an implementation that
flags every zero, fault or not, which would overcharge every legitimately
empty completion.

Both tests drive the REAL `mvp._pipeline.settle_reservation_and_log` against a
real (moto) tenant pool, rather than a fake `settle` callable, so what is
pinned is the actual dollar amount moved and the actual persisted UsageLogs
row -- not a test's own idea of what those should look like. The row's fault
marker is read back tolerant of its exact attribute name (see
`_metering_fault_marks` below): the handoff commits to the row carrying
*some* attribute naming the `metering_fault` condition and the string
`no_final_usage`, not to a specific key spelling, and pinning one exact
spelling here would make this suite the thing that breaks if the
implementation calls it `metering_fault_reason` instead of a `metering_fault`
bool plus a separate `reason` -- a naming choice this test has no basis to
guess further than the contract text itself commits to.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from boto3.dynamodb.conditions import Attr

from mvp import _converse_types as t
from mvp._money import Hold, run_ending
from mvp._pipeline import reserve_credit, release_pool, settle_reservation_and_log


@dataclass
class _User:
    user_id: str
    org_id: str
    email: str = "u@example.com"


def _pool(seed):
    from dynamo.tenant_budgets import TenantBudgetsRepository

    return TenantBudgetsRepository().pool_summary(seed["tenant_id"], seed["period"])


def _usage_log_items(tenant_id: str) -> list:
    """Every UsageLogs row for this tenant, read straight off the (moto) table --
    not through `aggregate_by_tag`, which folds and drops attributes this test
    needs to inspect verbatim."""
    from dynamo.client import get_dynamodb_resource, usage_logs_table_name

    table = get_dynamodb_resource().Table(usage_logs_table_name())
    resp = table.scan(FilterExpression=Attr("tenant_id").eq(tenant_id))
    return resp.get("Items", [])


def _metering_fault_marks(item: dict) -> bool:
    """Whether this persisted row carries a truthy attribute naming the
    `metering_fault` condition, tolerant of the exact attribute the
    implementation chose (see module docstring)."""
    return any(
        "metering_fault" in str(k) and bool(v) for k, v in item.items()
    )


def _mentions_no_final_usage(item: dict) -> bool:
    return any("no_final_usage" in str(v) for v in item.values())


def _hold(user, ctx, reservation, *, model_id="us.anthropic.claude-opus-4-7"):
    return Hold(
        user=user, tenants_repo=ctx, reservation=reservation, model_id=model_id,
        settle=settle_reservation_and_log, release=release_pool,
    )


def test_no_usage_event_settles_at_reserved_bound_and_marks_the_row(
    seed_tenant_with_pool,
):
    """The pair's first half. `acc` never absorbed a `Usage` event -- exactly
    what a stream that completed cleanly but never delivered Bedrock's
    `metadata.usage` frame leaves behind: `saw_final_usage=False` and an
    accumulator sitting at its zero default. `usage_from_bedrock` refused to
    invent those zeros; this is the one place left that could still turn
    that refusal into "the call was free"."""
    seed = seed_tenant_with_pool
    user = _User(user_id=seed["user_id"], org_id=seed["tenant_id"])
    reservation_tokens = 4000
    reserved_bound_microusd = 2_000_000  # the reservation's own dollar bound
    ctx = reserve_credit(
        user, reservation_tokens, pricing_key="opus", cost_microusd=reserved_bound_microusd,
    )
    assert _pool(seed)["pool_reserved_microusd"] == reserved_bound_microusd

    acc = t.UsageAccumulator()
    assert acc.saw_final_usage is False

    hold = _hold(user, ctx, reservation_tokens)
    ending = hold.claim_settle(acc)
    assert ending is not None, "the success path must win its own claim"
    run_ending(ending)

    summary = _pool(seed)
    assert summary["pool_settled_microusd"] == reserved_bound_microusd, (
        "a stream that never reported usage must be charged its reservation's "
        "own bound, not the zero its empty accumulator would naively settle at"
    )
    assert summary["pool_reserved_microusd"] == 0, (
        "the reservation must be CONSUMED by the fault charge, not refunded back "
        "to the tenant as though nothing had been served"
    )

    rows = _usage_log_items(seed["tenant_id"])
    assert len(rows) == 1
    assert _metering_fault_marks(rows[0]), (
        f"row must carry a truthy metering_fault-named attribute; got keys "
        f"{sorted(rows[0].keys())}"
    )
    assert _mentions_no_final_usage(rows[0]), (
        "the row's fault reason must name the condition that fired "
        "(no_final_usage), not just flag that one did"
    )


def test_reported_zero_output_settles_real_cost_and_is_not_marked(
    seed_tenant_with_pool,
):
    """The pair's second half. `acc` DID absorb a terminal `Usage` event, and
    that event reported a genuine zero output -- a model that produced no
    tokens (e.g. hit a stop sequence immediately) and said so. `saw_final_usage`
    is True here, which is the whole of what must decide this: an
    implementation that flags any zero, measured or not, passes the test above
    on its own but fails this one, because it would overcharge every
    legitimately empty completion at the reservation's bound instead of at the
    real (near-zero, input-only) cost of what actually happened."""
    seed = seed_tenant_with_pool
    user = _User(user_id=seed["user_id"], org_id=seed["tenant_id"])
    reservation_tokens = 4000
    # Reserved generously above what 1000 input / 0 output tokens will actually
    # cost, so a fault-charge (the bound) and a real-cost charge are two
    # different, distinguishable numbers -- the assertion below would be
    # trivially satisfied by either if they coincided.
    reserved_bound_microusd = 2_000_000
    ctx = reserve_credit(
        user, reservation_tokens, pricing_key="opus", cost_microusd=reserved_bound_microusd,
    )

    acc = t.UsageAccumulator()
    acc.absorb(t.Usage(input=1000, output=0))
    assert acc.saw_final_usage is True
    assert acc.output_tokens == 0

    hold = _hold(user, ctx, reservation_tokens)
    ending = hold.claim_settle(acc)
    assert ending is not None
    run_ending(ending)

    from mvp.pricing import rate_usage

    expected = rate_usage(
        ctx.rate_snapshot, input_tokens=1000, output_tokens=0,
        cache_read_tokens=None, cache_write_tokens=None,
    ).total_cost_microusd
    assert expected > 0, "the rate table used by this test must actually price input tokens"
    assert expected != reserved_bound_microusd, (
        "the reservation must have been set generously enough that the real "
        "cost and the bound are distinguishable numbers"
    )

    summary = _pool(seed)
    assert summary["pool_settled_microusd"] == expected, (
        "a reported zero output is a measurement, not an absence: settle at "
        "what was actually measured, not at the reservation's bound"
    )
    assert summary["pool_reserved_microusd"] == 0

    rows = _usage_log_items(seed["tenant_id"])
    assert len(rows) == 1
    assert not _metering_fault_marks(rows[0]), (
        f"a measured zero must not be flagged as a metering fault; got keys "
        f"{sorted(rows[0].keys())}"
    )


def test_no_observation_at_all_is_also_a_metering_fault(seed_tenant_with_pool):
    """`claim_settle()` with no argument at all is the same fact as an
    accumulator that never absorbed anything -- nobody told the settle path
    what the provider did -- and the module's own sibling doctrine
    (`claim_unobserved`: "passing none is not an error -- it is the unknown,
    which is expensive by default") says this must not default to free either.
    Unlike the two tests above, this construction never carries a
    `saw_final_usage` attribute at all (the non-streaming `mvp._money.Usage`
    dataclass has no such field) -- so this also pins that the ABSENCE of the
    concept, not just its being False, reads as a fault.
    """
    seed = seed_tenant_with_pool
    user = _User(user_id=seed["user_id"], org_id=seed["tenant_id"])
    reservation_tokens = 4000
    reserved_bound_microusd = 2_000_000
    ctx = reserve_credit(
        user, reservation_tokens, pricing_key="opus", cost_microusd=reserved_bound_microusd,
    )

    hold = _hold(user, ctx, reservation_tokens)
    ending = hold.claim_settle()
    assert ending is not None
    run_ending(ending)

    summary = _pool(seed)
    assert summary["pool_settled_microusd"] == reserved_bound_microusd
    assert summary["pool_reserved_microusd"] == 0

    rows = _usage_log_items(seed["tenant_id"])
    assert len(rows) == 1
    assert _metering_fault_marks(rows[0])
