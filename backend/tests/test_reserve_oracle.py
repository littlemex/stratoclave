"""Tests for the production reserve differential oracle (mvp.reserve_oracle) and its
wiring into the pending commit (mvp._pipeline._pending_commit_transact).

Proves: the golden write-set prediction mirrors the transaction admission gate; the
oracle is a pure detector (never changes control flow); it is flag-gated (zero read
when off); and a real mismatch is logged, not raised.
"""
from __future__ import annotations

import pytest

from mvp import reserve_oracle as ro


# ------------------------------------------------------------ pure predictions

def _pool(limit, reserved=0, settled=0, status="active"):
    return {"pool_limit_microusd": limit, "pool_reserved_microusd": reserved,
            "pool_settled_microusd": settled, "status": status}


def test_golden_admits_within_ceiling():
    ws = ro.golden_predicted_writeset(amount_microusd=40, pool_row=_pool(100, 30, 20))
    assert ws.verdict == ro.VERDICT_ADMIT and ws.reserved_delta_int == 40   # 30+20+40<=100


def test_golden_rejects_at_ceiling_overflow():
    ws = ro.golden_predicted_writeset(amount_microusd=51, pool_row=_pool(100, 30, 20))
    assert ws.verdict == ro.VERDICT_REJECT and ws.reserved_delta_int == 0    # 30+20+51>100


def test_golden_rejects_exact_boundary_plus_one():
    # 50+50+1 > 100 -> reject; 50+50+0 (amount must be >=1) — use 1 over.
    assert ro.golden_predicted_writeset(amount_microusd=1, pool_row=_pool(100, 50, 50)
                                        ).verdict == ro.VERDICT_REJECT


def test_golden_admits_exact_boundary():
    assert ro.golden_predicted_writeset(amount_microusd=50, pool_row=_pool(100, 30, 20)
                                        ).verdict == ro.VERDICT_ADMIT    # 30+20+50==100


def test_golden_rejects_suspended_pool():
    assert ro.golden_predicted_writeset(amount_microusd=1, pool_row=_pool(100, 0, 0, "suspended")
                                        ).verdict == ro.VERDICT_REJECT


def test_golden_rejects_missing_pool():
    assert ro.golden_predicted_writeset(amount_microusd=1, pool_row=None
                                        ).verdict == ro.VERDICT_REJECT


def test_pending_applied_and_exhausted_map_to_verdicts():
    admit = ro.pending_actual_writeset(amount_microusd=40, outcome="applied",
                                       exhausted_sentinel="exhausted", applied_sentinel="applied")
    assert admit.verdict == ro.VERDICT_ADMIT and admit.reserved_delta_int == 40
    rej = ro.pending_actual_writeset(amount_microusd=40, outcome="exhausted",
                                     exhausted_sentinel="exhausted", applied_sentinel="applied")
    assert rej.verdict == ro.VERDICT_REJECT and rej.reserved_delta_int == 0


def test_pending_writeset_rejects_replay_outcome():
    # ALREADY (replay) must NOT be mapped here — the caller skips it (dead-branch guard).
    with pytest.raises(ValueError):
        ro.pending_actual_writeset(amount_microusd=40, outcome="already",
                                   exhausted_sentinel="exhausted", applied_sentinel="applied")


def test_compare_match_does_not_reread():
    a = ro.ReserveWriteSet(ro.VERDICT_ADMIT, 40)
    calls = {"n": 0}

    def _reread():
        calls["n"] += 1
        return _pool(100, 40)
    assert ro.compare_and_log(tenant_id="t", period="p", hold_id="h", golden=a, pending=a,
                              pool_before=_pool(100, 0), reread=_reread) == "match"
    assert calls["n"] == 0          # a match pays NO extra read (lazy reread)


def test_compare_race_and_mismatch_via_reread():
    a = ro.ReserveWriteSet(ro.VERDICT_ADMIT, 40)   # golden predicts admit +40
    b = ro.ReserveWriteSet(ro.VERDICT_REJECT, 0)   # pending actually rejected (disagree)
    before = _pool(100, 60)
    # reread shows the pool moved by MORE than pending's own delta (0) -> concurrent
    # release raced -> benign race.
    moved = _pool(100, 10)
    assert ro.compare_and_log(tenant_id="t", period="p", hold_id="h", golden=a, pending=b,
                              pool_before=before, reread=lambda: moved) == "race"
    # reread shows the pool unchanged (pending's own delta is 0) -> genuine mismatch.
    assert ro.compare_and_log(tenant_id="t", period="p", hold_id="h", golden=a, pending=b,
                              pool_before=before, reread=lambda: _pool(100, 60)) == "mismatch"


def test_enabled_flag(monkeypatch):
    monkeypatch.delenv("STRATOCLAVE_RESERVE_ORACLE", raising=False)
    assert ro.oracle_enabled() is True                 # default ON
    monkeypatch.setenv("STRATOCLAVE_RESERVE_ORACLE", "false")
    assert ro.oracle_enabled() is False
    monkeypatch.setenv("STRATOCLAVE_RESERVE_ORACLE", "true")
    assert ro.oracle_enabled() is True


# ------------------------------------------------- wiring into the pending commit

def _seed(tenant, limit=10_000_000):
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
    from dynamo.user_tenants import UserTenantsRepository
    period = current_period()
    UserTenantsRepository().ensure(user_id=f"u-{tenant}", tenant_id=tenant, role="user",
                                   total_credit=1_000_000_000)
    TenantBudgetsRepository().set_pool_limit(tenant_id=tenant, period=period,
                                             pool_limit_microusd=limit)
    return tenant, period


def test_oracle_on_agrees_on_normal_commit(dynamodb_mock, monkeypatch):
    from structlog.testing import capture_logs

    from mvp import _pipeline
    from dynamo.tenant_budgets import TenantBudgetsRepository
    monkeypatch.setenv("STRATOCLAVE_RESERVE_ORACLE", "true")
    _pipeline._reset_low_level_client()
    tenant, period = _seed("oracle-ok")
    b = TenantBudgetsRepository()
    # capture_logs intercepts events at the structlog processor layer, independent
    # of the global logging config (some suite ordering rebinds stdlib logging away
    # from the live sys.stdout that capsys replaces — capsys is therefore flaky here).
    with capture_logs() as caps:
        out = _pipeline._pending_commit_transact(
            b, tenant_id=tenant, period=period, hold_id="h1", amount=100_000)
    assert out == b.RESERVE_APPLIED
    # a matching write-set logs reserve_oracle_match, NEVER reserve_oracle_mismatch.
    events = [c.get("event") for c in caps]
    assert "reserve_oracle_match" in events
    assert "reserve_oracle_mismatch" not in events
    assert b.pool_summary(tenant, period)["pool_reserved_microusd"] == 100_000


def test_oracle_off_skips_the_extra_read(dynamodb_mock, monkeypatch):
    from mvp import _pipeline
    from dynamo.tenant_budgets import TenantBudgetsRepository
    monkeypatch.setenv("STRATOCLAVE_RESERVE_ORACLE", "false")
    _pipeline._reset_low_level_client()
    tenant, period = _seed("oracle-off")
    b = TenantBudgetsRepository()
    calls = {"get": 0}
    real_get = b.get

    def _counting_get(*a, **k):
        calls["get"] += 1
        return real_get(*a, **k)
    monkeypatch.setattr(b, "get", _counting_get)
    # commit_transact does NOT read the pool when the oracle is off.
    out = _pipeline._pending_commit_transact(
        b, tenant_id=tenant, period=period, hold_id="h1", amount=100_000)
    assert out == b.RESERVE_APPLIED
    assert calls["get"] == 0                           # no oracle read


def test_oracle_mismatch_is_logged_not_raised(dynamodb_mock, monkeypatch):
    """Inject a golden prediction that disagrees with pending; the commit still
    succeeds (fail-open) and a reserve_oracle_mismatch is logged."""
    from structlog.testing import capture_logs

    from mvp import _pipeline, reserve_oracle
    from dynamo.tenant_budgets import TenantBudgetsRepository
    monkeypatch.setenv("STRATOCLAVE_RESERVE_ORACLE", "true")
    _pipeline._reset_low_level_client()
    tenant, period = _seed("oracle-mismatch")
    b = TenantBudgetsRepository()
    # force the golden to predict REJECT while pending will APPLY (a real divergence).
    monkeypatch.setattr(reserve_oracle, "golden_predicted_writeset",
                        lambda **kw: reserve_oracle.ReserveWriteSet(reserve_oracle.VERDICT_REJECT, 0))
    # capture_logs is config-independent (see test_oracle_on_agrees_on_normal_commit).
    with capture_logs() as caps:
        out = _pipeline._pending_commit_transact(
            b, tenant_id=tenant, period=period, hold_id="h1", amount=100_000)
    # control flow UNCHANGED: the debit still committed.
    assert out == b.RESERVE_APPLIED
    assert b.pool_summary(tenant, period)["pool_reserved_microusd"] == 100_000
    assert "reserve_oracle_mismatch" in [c.get("event") for c in caps]


# ---------------------------------------- log <-> CDK metric-filter contract
#
# Fable review-1 closure blocker: the ecs-stack.ts metric filters key on the
# structlog `event` field (filterPattern `$.event = '<name>'`). If the oracle
# ever renames an event, or the production JSON renderer stops emitting `event`
# as a top-level key, the filter would silently match nothing and — because the
# filter carries `defaultValue: 0` and the alarm is `treatMissingData:
# NOT_BREACHING` — the mismatch alarm would go permanently deaf with NO visible
# failure. These tests are the tripwire: rename either side and CI goes red.

# The exact event strings the CDK metric filters depend on. Keep in lockstep
# with iac/lib/ecs-stack.ts mkFilter('<event>', 'ReserveOracle<...>') calls.
_ORACLE_EVENTS = ("reserve_oracle_match", "reserve_oracle_race",
                  "reserve_oracle_mismatch")


def _render_production_json(event_dict):
    """Serialize one event dict through the PRODUCTION structlog processor chain
    (the JSONRenderer that ships to CloudWatch), WITHOUT going through a cached
    BoundLogger. structlog uses cache_logger_on_first_use=True, so a logger bound
    earlier in the suite keeps the dev ConsoleRenderer even after a production
    reconfigure — driving the cached logger would render console text, not JSON
    (this test itself hit that failure under full-suite ordering). Running the
    configured processor list directly is cache-independent and restores global
    config afterwards so this test never pollutes suite-wide logging state."""
    import json

    import structlog

    from core.logging import setup_logging

    saved_cfg = structlog.get_config()
    try:
        setup_logging(environment="production")
        processors = structlog.get_config()["processors"]

        class _StubLogger:
            name = "mvp.reserve_oracle"

        out = dict(event_dict)
        for proc in processors:
            out = proc(_StubLogger(), "error", out)
    finally:
        structlog.configure(**saved_cfg)
    assert isinstance(out, str), (
        f"production chain did not render to a JSON string (last proc output {out!r})")
    return json.loads(out)


def test_production_json_emits_exact_oracle_event_names():
    """Each oracle outcome must render a top-level JSON `event` equal to the exact
    string the CDK filter matches — the contract CloudWatch depends on. Drive the
    REAL oracle code via capture_logs to get the event dicts it actually emits,
    then serialize each through the production JSONRenderer."""
    from structlog.testing import capture_logs

    g = ro.ReserveWriteSet(ro.VERDICT_ADMIT, 40)
    rej = ro.ReserveWriteSet(ro.VERDICT_REJECT, 0)
    with capture_logs() as caps:
        # match: verdicts + deltas agree (no reread).
        ro.compare_and_log(tenant_id="t", period="p", hold_id="h", golden=g, pending=g,
                           pool_before=_pool(100, 0))
        # race: disagree AND pool moved beyond own delta (concurrent op).
        ro.compare_and_log(tenant_id="t", period="p", hold_id="h", golden=g, pending=rej,
                           pool_before=_pool(100, 60), reread=lambda: _pool(100, 10))
        # mismatch: disagree AND pool did NOT move -> genuine divergence.
        ro.compare_and_log(tenant_id="t", period="p", hold_id="h", golden=g, pending=rej,
                           pool_before=_pool(100, 60), reread=lambda: _pool(100, 60))

    # the oracle code emits exactly the three contract event names, in order.
    assert [c.get("event") for c in caps] == list(_ORACLE_EVENTS), (
        f"oracle event names drifted from the CDK metric-filter contract: {caps}")
    # and each, once serialized by the production JSONRenderer, keeps `event` as a
    # top-level JSON key equal to that exact string (the filter is `$.event`).
    for cap in caps:
        # capture_logs adds log_level; mirror the real record shape for rendering.
        rendered = _render_production_json({k: v for k, v in cap.items()
                                            if k != "log_level"})
        assert rendered.get("event") == cap["event"], (
            f"production JSON dropped/renamed the event key: {rendered}")


def test_cdk_stack_filters_on_the_exact_oracle_event_names():
    """The other half of the contract: ecs-stack.ts must declare a metric filter
    for each oracle event string. Rename in the CDK and this goes red."""
    import pathlib

    stack = (pathlib.Path(__file__).resolve().parent.parent.parent
             / "iac" / "lib" / "ecs-stack.ts")
    assert stack.exists(), f"CDK stack not found at {stack}"
    src = stack.read_text(encoding="utf-8")
    for event in _ORACLE_EVENTS:
        assert f"'{event}'" in src, (
            f"ecs-stack.ts has no metric filter for oracle event {event!r} — "
            "the log<->filter contract is broken; the alarm would be deaf.")
    # the mismatch filter must feed an alarm (not just a counter).
    assert "ReserveOracleMismatch" in src and "cloudwatch.Alarm" in src
