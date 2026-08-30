"""C8.3's missing watcher: the exposure a retention creates is reported.

WHAT DEFECT THIS CLOSES

`STRATOCLAVE_UNOBSERVED_HOLDS` defaults on, so a reservation whose provider call departed
and whose outcome was never observed is held rather than returned. Correct — an abandoned
Bedrock call is billed for the full generation — but it moves the failure mode. Retentions
accumulate against a tenant's headroom, and until this shipped the first signal an
operator got was a refusal for an unrelated request. The held amount was reachable (an
admin listing retained holds sums it) and nothing pushed it anywhere.

`charge-loss.md` section 7 names per-tenant and account exposure accounting plus
saturation alarms as the precondition for automatically releasing an unobserved hold. This
is not that release, but it is that accounting.

WHAT IS ASSERTED HERE, AND WHAT IS NOT

The arithmetic and the emission, including the two ways the arithmetic can be quietly
wrong: a zero pool limit must not divide, and a hold with no usable timestamp must not be
read as retained at the epoch and reported as decades old — a staleness alarm calibrated
against that is worthless the first time a field is absent.

Not asserted: that a CloudWatch alarm fires. The alarm lives in `iac/`, is checked by the
CDK tests there, and the seam between them is the field NAMES on the log line. So one test
below pins those names, because renaming a field is how this whole mechanism becomes a log
line nobody reads: the metric filter keeps matching nothing and the alarm sits green
forever on missing data.
"""
from __future__ import annotations

import time

import pytest

from mvp import retention_exposure as rx


@pytest.fixture(autouse=True)
def _clear_throttle():
    rx.reset_for_test()
    yield
    rx.reset_for_test()


class _Budgets:
    """A budgets repository with only what the reporter reads."""

    def __init__(self, holds, limit=1_000_000, explode=False):
        self._holds = holds
        self._limit = limit
        self._explode = explode
        self.list_calls = 0

    def list_retained_holds(self, *, tenant_id, period, limit=100):
        self.list_calls += 1
        if self._explode:
            raise RuntimeError("dynamo said no")
        return list(self._holds)

    def pool_summary(self, tenant_id, period):
        return {"pool_limit_microusd": self._limit}


def _hold(amount, *, retained_at=None, expires_at=None):
    h = {"amount_microusd": amount}
    if retained_at is not None:
        h["retained_at"] = retained_at
    if expires_at is not None:
        h["expires_at"] = expires_at
    return h


# --------------------------------------------------------------- the arithmetic


def test_the_fraction_is_of_the_limit_not_the_amount():
    """The signal. A held amount alone cannot say whether anyone is about to be refused,
    because the same figure is an emergency against one limit and noise against another."""
    small = rx.exposure_figures([_hold(10_000)], pool_limit_microusd=20_000)
    large = rx.exposure_figures([_hold(10_000)], pool_limit_microusd=10_000_000)
    assert small["held_microusd"] == large["held_microusd"] == 10_000
    assert small["held_fraction"] == 0.5
    assert large["held_fraction"] < 0.01, (
        "the same held amount against a limit 500x larger must not read as the same risk")


def test_a_zero_limit_does_not_divide():
    """A tenant with no pool limit cannot be refused for headroom, so the fraction is
    undefined rather than infinite — and reporting it must not raise, because this runs
    inside a sweep that is moving money."""
    figures = rx.exposure_figures([_hold(5_000)], pool_limit_microusd=0)
    assert figures["held_fraction"] == 0.0
    assert figures["held_microusd"] == 5_000, (
        "the amount and the count still have to carry the fact that something is held")
    assert figures["retained_holds"] == 1


def test_a_hold_with_no_usable_timestamp_is_left_out_of_the_age():
    """The quiet failure. Read a missing field as zero and the age becomes the seconds
    since 1970, which makes every staleness threshold fire forever and teaches an operator
    to ignore the alarm."""
    now = int(time.time())
    figures = rx.exposure_figures(
        [_hold(1, retained_at=None, expires_at=None), _hold(1, expires_at=now - 300)],
        pool_limit_microusd=1_000, now_epoch=now)
    assert figures["oldest_retention_age_seconds"] == 300, (
        "the datable hold decides the age; the undatable one must not")
    figures_none = rx.exposure_figures([_hold(1)], pool_limit_microusd=1_000)
    assert figures_none["oldest_retention_age_seconds"] == 0


def test_the_age_comes_from_retained_at_when_it_is_there():
    """`hold_retain` writes an ISO string, so the age has to parse one; `expires_at` is
    the fallback and is a bounded proxy, since a hold is retained BECAUSE it expired."""
    now = int(time.time())
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 7200))
    figures = rx.exposure_figures(
        [_hold(1, retained_at=iso, expires_at=now - 60)],
        pool_limit_microusd=1_000, now_epoch=now)
    assert 7100 <= figures["oldest_retention_age_seconds"] <= 7300, (
        "retained_at must win over expires_at; a two-hour-old retention read as one "
        "minute old is an operator who stopped looking, reported as healthy")


def test_the_oldest_wins_not_the_newest():
    now = int(time.time())
    figures = rx.exposure_figures(
        [_hold(1, expires_at=now - 60), _hold(1, expires_at=now - 86_400)],
        pool_limit_microusd=1_000, now_epoch=now)
    assert figures["oldest_retention_age_seconds"] == 86_400


# ----------------------------------------------------------------- the emission


def test_emitting_reports_the_figures_and_reads_the_table_once():
    budgets = _Budgets([_hold(2_500, expires_at=int(time.time()) - 10)], limit=10_000)
    figures = rx.emit_exposure(budgets, "acme", "2026-08", reason="test", force=True)
    assert figures is not None
    assert figures["held_microusd"] == 2_500
    assert figures["held_fraction"] == 0.25
    assert budgets.list_calls == 1, "one bounded query, not one per hold"


def test_a_sweep_is_throttled_but_an_edge_is_not():
    """Cost control that must not cost a signal. A sweep runs as often as traffic does, so
    re-reading the table each time would be a query per request for a tenant that has
    retentions. The moments exposure CHANGES are forced through, because those are the
    edges — an outage is a burst of them inside one throttle window."""
    budgets = _Budgets([_hold(1_000, expires_at=int(time.time()))])
    assert rx.emit_exposure(budgets, "acme", "2026-08", reason="sweep") is not None
    assert rx.emit_exposure(budgets, "acme", "2026-08", reason="sweep") is None, (
        "the second sweep inside the interval must not re-read the table")
    assert budgets.list_calls == 1
    assert rx.emit_exposure(
        budgets, "acme", "2026-08", reason="retained", force=True) is not None
    assert budgets.list_calls == 2


def test_a_resolution_that_empties_the_list_still_reports_zero():
    """The metric has to come back DOWN. A forced emission with nothing retained reports
    zero rather than staying silent, or an operator who resolved everything watches a
    stale high-water mark and cannot tell it from an unresolved one."""
    budgets = _Budgets([])
    figures = rx.emit_exposure(
        budgets, "acme", "2026-08", reason="resolved_release", force=True)
    assert figures is not None
    assert figures["held_microusd"] == 0 and figures["retained_holds"] == 0


def test_an_unthrottled_sweep_with_nothing_retained_stays_quiet():
    """The other side of that. Emitting a zero every interval for every tenant that has
    no retentions is how a signal gets buried in the absence of one."""
    budgets = _Budgets([])
    assert rx.emit_exposure(budgets, "acme", "2026-08", reason="sweep") is None


def test_a_failure_to_read_exposure_never_raises():
    """This runs inside a sweep and inside a resolution, both of which are moving money. A
    reporter that raises would turn an observability gap into a money failure. Logged, not
    swallowed silently — but never raised."""
    budgets = _Budgets([_hold(1)], explode=True)
    assert rx.emit_exposure(budgets, "acme", "2026-08", reason="sweep", force=True) is None


# ------------------------------------------------- the seam with the CDK alarms


def test_the_field_names_the_alarms_read_are_pinned():
    """The seam. `iac/` builds metric filters on `$.held_fraction`,
    `$.oldest_retention_age_seconds` and `$.held_microusd` from the
    `retention_exposure` line. Rename one here and the filter keeps matching nothing: the
    alarm never fires, and it sits green on missing data rather than going red. That
    failure is invisible from both sides, so the names are asserted from this one."""
    figures = rx.exposure_figures([_hold(1)], pool_limit_microusd=10)
    for field in ("held_microusd", "held_fraction", "oldest_retention_age_seconds",
                  "retained_holds", "pool_limit_microusd"):
        assert field in figures, (
            f"{field} is read by a metric filter in iac/lib/ecs-stack.ts; removing it "
            f"silently disables an alarm")


# ------------------------------------------ the reaper actually emits it, end to end


def test_a_real_retention_emits_exposure_through_the_reaper(dynamodb_mock, monkeypatch):
    """The wiring, driven through production code rather than by calling the reporter.

    The unit tests above prove the reporter works; this proves something calls it. Those
    are different failures, and the second is the one that ships quietly: a reporter
    nobody invokes looks exactly like a reporter with nothing to report.

    The spy is on `mvp.retention_exposure.emit_exposure` rather than on the log output,
    because the assertion is about the CALL. Asserting on captured log records would also
    be asserting that this project's logger propagates to pytest's handler, which is a
    property of the logging setup and not of this wiring — a true statement failing for an
    unrelated reason is a test that gets deleted later.
    """
    from dynamo.tenant_budgets import TenantBudgetsRepository
    from tests import test_reaper_counter_giveback as harness

    monkeypatch.setenv("STRATOCLAVE_UNOBSERVED_HOLDS", "1")
    rx.reset_for_test()

    seen: list[dict] = []
    real = rx.emit_exposure

    def _spy(budgets, tenant_id, period, *, reason, force=False):
        figures = real(budgets, tenant_id, period, reason=reason, force=force)
        seen.append({"reason": reason, "force": force, "figures": figures})
        return figures

    monkeypatch.setattr(rx, "emit_exposure", _spy)

    period = harness._seed()
    user = harness._user()
    ctx = harness._reserve(user)
    budgets = TenantBudgetsRepository()

    # Mark the departure the way an ending does, so the reaper retains rather than
    # reclaims, then age the hold into the reaper's range.
    budgets.hold_mark_departed(tenant_id=harness.TENANT, sk=ctx.hold_sk,
                               state="submitted_unsettled")
    harness._age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)

    harness._sweep(period)

    reasons = [s["reason"] for s in seen]
    assert "retained" in reasons, (
        f"the reaper retained a hold and nothing reported the exposure it created; "
        f"reasons seen: {reasons}")
    retained_call = next(s for s in seen if s["reason"] == "retained")
    assert retained_call["force"] is True, (
        "the moment exposure rises must not be throttled away")
    figures = retained_call["figures"]
    assert figures and figures["held_microusd"] > 0, (
        "the exposure line carried no held amount, so a metric filter reading "
        "$.held_microusd has nothing to match")
    assert figures["retained_holds"] >= 1


def test_a_sweep_keeps_reporting_a_retention_nobody_resolved(dynamodb_mock, monkeypatch):
    """The case the retention edge does NOT cover, and the reason it matters.

    Exposure is reported when it rises. If that were the only report, a retention nobody
    resolves would produce one datapoint and then silence — and a CloudWatch alarm with no
    datapoints treats the exposure as gone. The high-water mark would clear itself while
    the money was still held, which is the exact failure this whole mechanism exists to
    prevent, arriving through the monitoring instead of through the ledger.

    So a sweep reports the STANDING exposure even when it retained nothing new. Driven
    here through a second sweep, after the retention already happened and the throttle has
    been cleared, so the only thing that can produce the emission is the sweep itself.
    """
    from dynamo.tenant_budgets import TenantBudgetsRepository
    from tests import test_reaper_counter_giveback as harness

    monkeypatch.setenv("STRATOCLAVE_UNOBSERVED_HOLDS", "1")
    rx.reset_for_test()

    period = harness._seed()
    user = harness._user()
    ctx = harness._reserve(user)
    budgets = TenantBudgetsRepository()
    budgets.hold_mark_departed(tenant_id=harness.TENANT, sk=ctx.hold_sk,
                               state="submitted_unsettled")
    harness._age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)
    harness._sweep(period)          # takes the retention
    rx.reset_for_test()             # a later interval, in the same second

    seen: list[dict] = []
    real = rx.emit_exposure

    def _spy(b, tenant_id, p, *, reason, force=False):
        figures = real(b, tenant_id, p, reason=reason, force=force)
        seen.append({"reason": reason, "figures": figures})
        return figures

    monkeypatch.setattr(rx, "emit_exposure", _spy)
    harness._sweep(period)          # retains nothing new: the hold is already RETAINED

    sweeps = [s for s in seen if s["reason"] == "sweep" and s["figures"] is not None]
    assert sweeps, (
        f"a sweep over a tenant with an unresolved retention reported nothing, so the "
        f"metric goes silent and the alarm clears itself; saw {[s['reason'] for s in seen]}")
    assert sweeps[0]["figures"]["held_microusd"] > 0, (
        "the standing exposure has to carry the amount still held, not just the fact of a "
        "sweep")
    assert sweeps[0]["figures"]["retained_holds"] >= 1
