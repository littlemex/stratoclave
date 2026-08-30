"""C3.5 — a settle that never commits does not lose the charge.

The settle transaction retries and then gives up. What happened next was that the
hold sat there until the reaper reclaimed it, the reclaim recorded a settled delta
of ZERO — an assertion that nothing was charged — and the usage the provider had
already reported survived only in a `pool_settle_failed` log line. Counter and
ledger both missed it equally, so reconciliation could not see the gap either: the
only signal was an alarm someone had to act on.

The charge is now durable before the reclaim can contradict it. On exhaustion the
settle writes an OWED_SETTLE row saying what was observed, and the reaper honours it
through the same LATE_SETTLE recovery it already uses when it wins the race against a
settle — conditional on the terminal being the RECLAIM it just wrote, and
once-per-hold on its own sort key, so a re-drive cannot double-post.

WHAT THIS DOES NOT CLOSE

A task that dies between learning the usage and writing that row still loses it.
Covering that means a write-ahead on every settle, which is a cost on every request
rather than on a rare one, and the choice is stated rather than hidden.
"""
from __future__ import annotations

import time

import pytest
from botocore.exceptions import ClientError

from dynamo.credit_ledger import CreditLedgerRepository, late_settle_sk, ledger_pk
from dynamo.tenant_budgets import TenantBudgetsRepository, current_period, hold_sk as _hsk
from dynamo.tenants import TenantsRepository
from dynamo.user_tenants import UserTenantsRepository
from mvp import _pipeline
from mvp import provider_outcome as _outcome
from mvp.deps import AuthenticatedUser


TENANT = "owed-settle-tenant"
MODEL = "claude-sonnet-5"


def _user(uid: str = "u-owed") -> AuthenticatedUser:
    UserTenantsRepository().ensure(user_id=uid, tenant_id=TENANT, role="user",
                                   total_credit=10 ** 12)
    return AuthenticatedUser(
        user_id=uid, email="owed@test.example", org_id=TENANT, roles=["user"],
        raw_claims={}, auth_kind="jwt", key_scopes=None, api_key_hash=None)


def _seed() -> str:
    TenantsRepository().create(
        tenant_id=TENANT, name="Owed Settle", team_lead_user_id="admin-owned",
        default_credit=10_000_000, created_by="test")
    period = current_period()
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=TENANT, period=period, pool_limit_microusd=1_000_000_000)
    return period


def _reserve(user):
    return _pipeline.reserve_credit_for_model(
        user, reservation_tokens=2500, model_name=MODEL,
        input_tokens_est=2000, max_output_tokens=400,
        input_bytes=6000, payload_hash="0bad0bad", extra_input_tokens=0)


def _break_the_settle_transaction(monkeypatch):
    """Make every settle transaction fail the way a contention storm does.

    Throttling is the realistic cause: it is transient, so the loop retries, and it
    can outlast the retry budget. The point of the test is what the code does after
    the budget is spent, so the fault is applied to the transaction rather than to
    the process."""
    real = _pipeline._low_level_client

    class _Throttling:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def transact_write_items(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                "TransactWriteItems")

    monkeypatch.setattr(_pipeline, "_low_level_client",
                        lambda *a, **kw: _Throttling(real()))
    # The loop sleeps between attempts; a test does not need to.
    monkeypatch.setattr(_pipeline.time, "sleep", lambda *_a, **_kw: None)


def _settle(user, ctx, *, tokens_in=1800, tokens_out=300):
    return _pipeline.settle_reservation_and_log(
        user=user, tenants_repo=ctx.tenants_repo, reservation=2500,
        actual_input_tokens=tokens_in, actual_output_tokens=tokens_out,
        model_id="us.anthropic.claude-sonnet-5", context=ctx)


def _age_hold_to_sweepable(period, hold_id, hold_sk):
    """Move the hold's expiry far enough into the past that the inline sweep is
    willing to reclaim it, keeping every other attribute."""
    budgets = TenantBudgetsRepository()
    item = budgets._table.get_item(
        Key={"tenant_id": TENANT, "sk": hold_sk}).get("Item")
    assert item is not None, "the abandoned settle should have left the hold in place"
    past = int(time.time()) - 100_000
    budgets._table.delete_item(Key={"tenant_id": TENANT, "sk": hold_sk})
    item["sk"] = _hsk(period, past, hold_id)
    item["expires_at"] = past
    budgets._table.put_item(Item=item)


def _late_settle(period, hold_id):
    return CreditLedgerRepository()._table.get_item(
        Key={"pk": ledger_pk(TENANT, period), "sk": late_settle_sk(hold_id)},
        ConsistentRead=True,
    ).get("Item")


def test_an_abandoned_settle_records_what_it_observed(dynamodb_mock, monkeypatch):
    """The durable half. Nothing has moved any counter yet — what matters is that the
    figure the provider reported is written down somewhere a machine reads."""
    period = _seed()
    user = _user()
    ctx = _reserve(user)
    _break_the_settle_transaction(monkeypatch)
    # The settle does not raise: the pool side gives up loudly and the request has
    # already been answered. That is exactly why the loss was invisible.
    _settle(user, ctx)

    owed = CreditLedgerRepository().get_owed_settle(
        tenant_id=TENANT, period=period, hold_id=ctx.hold_id)
    assert owed is not None, "the observed usage left no durable trace"
    assert int(owed["settled_delta_microusd"]) > 0
    assert owed["event_type"] == "OWED_SETTLE"
    # It records the request it belongs to, or the recovery could not be attributed.
    assert owed["hold_id"] == ctx.hold_id


def test_the_reaper_posts_the_charge_instead_of_asserting_zero(dynamodb_mock,
                                                              monkeypatch):
    """The whole property, end to end: a settle abandoned mid-storm, a reclaim that
    would have said "nothing was charged", and the charge arriving in the ledger and
    on the counter anyway."""
    period = _seed()
    user = _user()
    ctx = _reserve(user)
    _break_the_settle_transaction(monkeypatch)
    # The settle does not raise: the pool side gives up loudly and the request has
    # already been answered. That is exactly why the loss was invisible.
    _settle(user, ctx)
    owed = int(CreditLedgerRepository().get_owed_settle(
        tenant_id=TENANT, period=period, hold_id=ctx.hold_id)["settled_delta_microusd"])
    monkeypatch.undo()

    _age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)
    budgets = TenantBudgetsRepository()
    assert _pipeline._sweep_expired_holds(budgets, TENANT, period) == 1

    terminal = CreditLedgerRepository().get_terminal(
        tenant_id=TENANT, period=period, hold_id=ctx.hold_id)
    assert terminal["event_type"] == "RECLAIM"
    assert int(terminal["settled_delta_microusd"]) == 0  # the reclaim's own record
    recovered = _late_settle(period, ctx.hold_id)
    assert recovered is not None, "the charge was reclaimed to zero and lost"
    assert int(recovered["settled_delta_microusd"]) == owed
    # And the counter agrees, so the ledger and the pool did not part company.
    pool = budgets.pool_summary(TENANT, period)
    assert int(pool["pool_settled_microusd"]) == owed
    assert int(pool["pool_reserved_microusd"]) == 0


def test_a_second_sweep_cannot_post_the_charge_twice(dynamodb_mock, monkeypatch):
    """The owed row is not deleted when it is honoured — the ledger is append-only,
    so there is nothing to delete it with. At-most-once comes from the LATE_SETTLE
    sort key instead, and this is the test that says so rather than the comment."""
    period = _seed()
    user = _user()
    ctx = _reserve(user)
    _break_the_settle_transaction(monkeypatch)
    # The settle does not raise: the pool side gives up loudly and the request has
    # already been answered. That is exactly why the loss was invisible.
    _settle(user, ctx)
    monkeypatch.undo()

    _age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)
    budgets = TenantBudgetsRepository()
    _pipeline._sweep_expired_holds(budgets, TENANT, period)
    once = int(budgets.pool_summary(TENANT, period)["pool_settled_microusd"])

    # Re-drive the recovery directly, as a second sweep of the same hold would.
    _pipeline._recover_owed_settle_after_reclaim(
        client=_pipeline._low_level_client(), budgets=budgets,
        tenant_id=TENANT, period=period, hold_id=ctx.hold_id)
    assert int(budgets.pool_summary(TENANT, period)["pool_settled_microusd"]) == once


def test_a_reclaim_with_nothing_owed_is_unchanged(dynamodb_mock):
    """A hold whose request vanished before any usage was observed still reclaims to
    zero, because that is the truthful record for it. The recovery must not invent a
    charge to have something to post."""
    period = _seed()
    user = _user()
    ctx = _reserve(user)
    _age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)
    budgets = TenantBudgetsRepository()
    assert _pipeline._sweep_expired_holds(budgets, TENANT, period) == 1

    terminal = CreditLedgerRepository().get_terminal(
        tenant_id=TENANT, period=period, hold_id=ctx.hold_id)
    assert terminal["event_type"] == "RECLAIM"
    assert _late_settle(period, ctx.hold_id) is None
    pool = budgets.pool_summary(TENANT, period)
    assert int(pool["pool_settled_microusd"]) == 0
    assert int(pool["pool_reserved_microusd"]) == 0


def test_the_first_observation_wins(dynamodb_mock, monkeypatch):
    """A second abandoned attempt for the same hold must not overwrite the figure.
    The provider reported once; a later attempt has no new information about what it
    said, so the row is conditional on its own absence."""
    period = _seed()
    user = _user()
    ctx = _reserve(user)
    ledger = CreditLedgerRepository()
    ledger.put_owed_settle(
        tenant_id=TENANT, period=period, hold_id=ctx.hold_id,
        actual_microusd=9_000, run_id="first", run_id_is_fallback=False)
    ledger.put_owed_settle(
        tenant_id=TENANT, period=period, hold_id=ctx.hold_id,
        actual_microusd=1, run_id="second", run_id_is_fallback=False)
    owed = ledger.get_owed_settle(
        tenant_id=TENANT, period=period, hold_id=ctx.hold_id)
    assert int(owed["settled_delta_microusd"]) == 9_000
    assert owed["run_id"] == "first"


# ------------------------------------------------------- C8.3 retaining a reservation


def _hold_row(period, hold_sk):
    return TenantBudgetsRepository()._table.get_item(
        Key={"tenant_id": TENANT, "sk": hold_sk}).get("Item")


def _end_unobserved_through_the_hold(user, ctx):
    """End the reservation the way a read timeout does, through the real `Hold`.

    This is the point of the test rather than a detail of it. The departure fact the
    retention depends on is written by the ENDING, so a test that stamped the hold
    itself would prove the reaper's branch works while the branch stayed unreachable
    from any request — which is exactly what it did prove, until this was rewritten.
    So the fault is injected where a real one arrives (the transport raised a read
    timeout) and everything after that is production code.
    """
    from botocore.exceptions import ReadTimeoutError
    from mvp import _money

    hold = _money.Hold(
        user=user,
        tenants_repo=ctx,
        reservation=2500,
        model_id="us.anthropic.claude-sonnet-5",
        settle=lambda **kw: _pipeline.settle_reservation_and_log(**kw),
        release=lambda c: _pipeline.release_pool(c),
        mark_departed=_money.hold_departure_marker(ctx),
        route="messages",
    )
    ending = hold.claim_unobserved(
        exc=ReadTimeoutError(endpoint_url="https://bedrock.example"))
    assert ending is not None, "the hold should have had an ending to give"
    ending.run()
    return hold


def test_a_departed_call_keeps_its_reservation_when_the_flag_is_on(dynamodb_mock,
                                                                  monkeypatch):
    """C8.3. The reclaim would have returned this budget and recorded that nothing was
    charged. The provider call had already left, and a call abandoned at a read
    timeout was measured being billed 1,493 output tokens, so that record is false —
    the reservation is held instead, and the headroom stays held with no counter
    moving, because the amount was already counted against the limit."""
    monkeypatch.setenv("STRATOCLAVE_UNOBSERVED_HOLDS", "1")
    period = _seed()
    user = _user()
    ctx = _reserve(user)
    before = TenantBudgetsRepository().pool_summary(TENANT, period)
    _end_unobserved_through_the_hold(user, ctx)
    # The ending kept the reservation AND recorded why, both through production code.
    live = TenantBudgetsRepository()._table.get_item(
        Key={"tenant_id": TENANT, "sk": ctx.hold_sk}).get("Item")
    assert live is not None, "an unobserved ending must not delete its hold"
    assert live.get("provider_invoked_at"), (
        "nothing recorded the departure, so the reaper cannot know it happened — "
        "this is the seam that made the whole retention unreachable")
    assert live.get("unobserved_state") == _outcome.SUBMITTED_UNSETTLED

    _age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)
    budgets = TenantBudgetsRepository()
    assert _pipeline._sweep_expired_holds(budgets, TENANT, period) == 0, \
        "a retained hold is not a reclaim"

    after = budgets.pool_summary(TENANT, period)
    assert int(after["pool_reserved_microusd"]) == int(before["pool_reserved_microusd"])
    assert int(after["pool_settled_microusd"]) == int(before["pool_settled_microusd"])
    # No ending was written: the reservation is outstanding, not finished.
    assert CreditLedgerRepository().get_terminal(
        tenant_id=TENANT, period=period, hold_id=ctx.hold_id) is None
    # And the retention explains itself, with the handle the provider's own record
    # can be found by.
    rec = CreditLedgerRepository().get_retained(
        tenant_id=TENANT, period=period, hold_id=ctx.hold_id)
    assert rec is not None and rec["event_type"] == "RETAINED"
    assert int(rec["held_microusd"]) == int(before["pool_reserved_microusd"])
    assert rec["attempt_marker"] == ctx.hold_id

    # A second sweep does not offer to return it either.
    assert _pipeline._sweep_expired_holds(budgets, TENANT, period) == 0
    assert int(budgets.pool_summary(TENANT, period)["pool_reserved_microusd"]) == \
        int(before["pool_reserved_microusd"])


def test_retention_is_off_by_default(dynamodb_mock, monkeypatch):
    """The flag ships off, so merging this changes no deployment's behaviour: the same
    unobserved ending returns the reservation immediately and leaves nothing behind —
    not even the departure marker, because there is no retention for it to serve."""
    monkeypatch.delenv("STRATOCLAVE_UNOBSERVED_HOLDS", raising=False)
    period = _seed()
    user = _user()
    ctx = _reserve(user)
    _end_unobserved_through_the_hold(user, ctx)

    budgets = TenantBudgetsRepository()
    assert int(budgets.pool_summary(TENANT, period)["pool_reserved_microusd"]) == 0
    assert budgets._table.get_item(
        Key={"tenant_id": TENANT, "sk": ctx.hold_sk}).get("Item") is None


def test_a_hold_whose_call_never_departed_is_still_returned(dynamodb_mock, monkeypatch):
    """The distinction the retention rests on. Nothing left this process, so no
    provider can have billed it, and holding the budget would be inventing a
    liability rather than recording one."""
    monkeypatch.setenv("STRATOCLAVE_UNOBSERVED_HOLDS", "1")
    period = _seed()
    user = _user()
    ctx = _reserve(user)  # no provider_invoked_at stamped
    _age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)
    budgets = TenantBudgetsRepository()
    assert _pipeline._sweep_expired_holds(budgets, TENANT, period) == 1
    assert int(budgets.pool_summary(TENANT, period)["pool_reserved_microusd"]) == 0


def test_an_external_authorization_is_never_retained(dynamodb_mock, monkeypatch):
    """An external authorization makes no provider call, so it cannot have been
    billed by one — returning it is simply correct, and this is the same distinction
    the exposure figure in the reclaim log already draws."""
    monkeypatch.setenv("STRATOCLAVE_UNOBSERVED_HOLDS", "1")
    period = _seed()
    user = _user()
    ctx = _reserve(user)
    _end_unobserved_through_the_hold(user, ctx)
    TenantBudgetsRepository()._table.update_item(
        Key={"tenant_id": TENANT, "sk": ctx.hold_sk},
        UpdateExpression="SET #s = :src",
        ExpressionAttributeNames={"#s": "source"},
        ExpressionAttributeValues={":src": "external"},
    )
    _age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)
    budgets = TenantBudgetsRepository()
    assert _pipeline._sweep_expired_holds(budgets, TENANT, period) == 1
    assert int(budgets.pool_summary(TENANT, period)["pool_reserved_microusd"]) == 0


def test_a_retention_resolves_at_the_figure_an_operator_supplies(dynamodb_mock,
                                                                 monkeypatch):
    """The other half of retaining: something has to be able to end it. The figure
    comes from the provider's own record, which is the only place it exists, and it
    lands through the same settle the request path uses."""
    monkeypatch.setenv("STRATOCLAVE_UNOBSERVED_HOLDS", "1")
    period = _seed()
    user = _user()
    ctx = _reserve(user)
    _end_unobserved_through_the_hold(user, ctx)
    _age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)
    budgets = TenantBudgetsRepository()
    _pipeline._sweep_expired_holds(budgets, TENANT, period)
    held = int(budgets.pool_summary(TENANT, period)["pool_reserved_microusd"])
    assert held > 0

    listed = _pipeline.list_retained_holds(TENANT, period)
    assert [h["hold_id"] for h in listed] == [ctx.hold_id]

    terminal, settled = _pipeline.resolve_retained_hold(
        TENANT, period, ctx.hold_id, charge_microusd=1_234)
    assert (terminal, settled) == ("SETTLE", 1_234)
    pool = budgets.pool_summary(TENANT, period)
    assert int(pool["pool_settled_microusd"]) == 1_234
    assert int(pool["pool_reserved_microusd"]) == 0
    ev = CreditLedgerRepository().get_terminal(
        tenant_id=TENANT, period=period, hold_id=ctx.hold_id)
    assert ev["event_type"] == "SETTLE"
    assert int(ev["settled_delta_microusd"]) == 1_234
    # The ledger says WHY a charge arrived late at a figure no request computed.
    assert ev["settle_reason"] == "retention_resolved"
    assert _pipeline.list_retained_holds(TENANT, period) == []


def test_a_retention_releases_when_the_provider_shows_no_charge(dynamodb_mock,
                                                                monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_UNOBSERVED_HOLDS", "1")
    period = _seed()
    user = _user()
    ctx = _reserve(user)
    _end_unobserved_through_the_hold(user, ctx)
    _age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)
    budgets = TenantBudgetsRepository()
    _pipeline._sweep_expired_holds(budgets, TENANT, period)

    terminal, settled = _pipeline.resolve_retained_hold(
        TENANT, period, ctx.hold_id, release=True)
    assert (terminal, settled) == ("RELEASE", 0)
    pool = budgets.pool_summary(TENANT, period)
    assert int(pool["pool_reserved_microusd"]) == 0
    assert int(pool["pool_settled_microusd"]) == 0
    assert CreditLedgerRepository().get_terminal(
        tenant_id=TENANT, period=period, hold_id=ctx.hold_id)["event_type"] == "RELEASE"


def test_a_resolution_refuses_a_figure_above_the_reservation(dynamodb_mock,
                                                            monkeypatch):
    """Settling above the reservation would push the settled side past what admission
    ever checked. An operator with a larger figure from the provider is reporting an
    overrun, which is a different record than resolving a retention."""
    import fastapi

    monkeypatch.setenv("STRATOCLAVE_UNOBSERVED_HOLDS", "1")
    period = _seed()
    user = _user()
    ctx = _reserve(user)
    _end_unobserved_through_the_hold(user, ctx)
    _age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)
    budgets = TenantBudgetsRepository()
    _pipeline._sweep_expired_holds(budgets, TENANT, period)
    held = int(budgets.pool_summary(TENANT, period)["pool_reserved_microusd"])

    with pytest.raises(fastapi.HTTPException) as ei:
        _pipeline.resolve_retained_hold(
            TENANT, period, ctx.hold_id, charge_microusd=held + 1)
    assert ei.value.status_code == 400
    # Refused means nothing moved and the retention is still there to resolve.
    assert int(budgets.pool_summary(TENANT, period)["pool_reserved_microusd"]) == held
    assert [h["hold_id"] for h in _pipeline.list_retained_holds(TENANT, period)] == \
        [ctx.hold_id]


def test_two_resolutions_of_one_retention_cannot_both_land(dynamodb_mock, monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_UNOBSERVED_HOLDS", "1")
    period = _seed()
    user = _user()
    ctx = _reserve(user)
    _end_unobserved_through_the_hold(user, ctx)
    _age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)
    budgets = TenantBudgetsRepository()
    _pipeline._sweep_expired_holds(budgets, TENANT, period)

    _pipeline.resolve_retained_hold(TENANT, period, ctx.hold_id, charge_microusd=500)
    with pytest.raises((_pipeline.RetainedHoldNotFound,
                        _pipeline.RetainedResolutionRaced)):
        _pipeline.resolve_retained_hold(
            TENANT, period, ctx.hold_id, charge_microusd=500)
    assert int(budgets.pool_summary(TENANT, period)["pool_settled_microusd"]) == 500


def test_a_resolution_needs_exactly_one_of_the_two_claims(dynamodb_mock, monkeypatch):
    """A figure and a release are different assertions about the provider's record,
    and there is no default because the gateway cannot make either."""
    import fastapi

    monkeypatch.setenv("STRATOCLAVE_UNOBSERVED_HOLDS", "1")
    period = _seed()
    for kwargs in ({}, {"charge_microusd": 5, "release": True}):
        with pytest.raises(fastapi.HTTPException) as ei:
            _pipeline.resolve_retained_hold(TENANT, period, "whatever", **kwargs)
        assert ei.value.status_code == 400
