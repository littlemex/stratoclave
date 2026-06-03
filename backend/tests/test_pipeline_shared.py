"""Regression guard for the credit-pipeline extraction.

`mvp.anthropic` previously owned `_reserve_credit` and
`_settle_reservation_and_log` directly. They moved to `mvp._pipeline` so
the OpenAI Responses route can share them. These tests stub the Dynamo
repositories so the settlement logic is exercised without depending on
the moto fixture chain (the existing `seed_active_tenant` fixture is
incompatible with the cached `boto3.resource` in `dynamo/client.py`,
which is a pre-existing issue tracked outside this change).

What is verified:
  - actual ≤ reservation → diff is refunded
  - actual > reservation → overrun is best-effort re-reserved
  - UsageLogs always appends the actual usage (never the reservation)
"""
from __future__ import annotations

from typing import Any

import pytest

from dynamo.user_tenants import CreditExhaustedError
from mvp._pipeline import settle_reservation_and_log
from mvp.deps import AuthenticatedUser


def _user(uid: str, tid: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uid,
        email="settle@test.example",
        org_id=tid,
        roles=["user"],
        raw_claims={},
        auth_kind="jwt",
        key_scopes=None,
        api_key_hash=None,
    )


class _StubTenantsRepo:
    """In-memory stand-in for `UserTenantsRepository` for unit tests.

    Records every reserve/refund call and lets test code inspect the
    final state without going near boto3 / moto.
    """

    def __init__(
        self, total_credit: int = 10_000, used: int = 0, reserve_fails: int = 0
    ) -> None:
        self.total_credit = total_credit
        self.used = used
        self._reserve_fails = reserve_fails
        self.refund_calls: list[int] = []
        self.reserve_calls: list[int] = []

    def refund(self, *, user_id: str, tenant_id: str, tokens: int) -> int:
        self.refund_calls.append(tokens)
        self.used = max(self.used - tokens, 0)
        return self.total_credit - self.used

    def reserve(self, *, user_id: str, tenant_id: str, tokens: int) -> int:
        self.reserve_calls.append(tokens)
        if self._reserve_fails > 0:
            self._reserve_fails -= 1
            raise CreditExhaustedError(
                "stub: simulated balance exhaustion"
            )
        self.used += tokens
        return self.total_credit - self.used

    def get(self, user_id: str, tenant_id: str) -> dict[str, Any]:
        return {"total_credit": self.total_credit, "credit_used": self.used}


@pytest.fixture
def stub_usage_logs(monkeypatch: pytest.MonkeyPatch):
    """Capture the single `UsageLogsRepository().record(...)` call."""
    captured: list[dict[str, Any]] = []

    class _StubUsageLogs:
        def record(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(
        "mvp._pipeline.UsageLogsRepository", lambda: _StubUsageLogs()
    )
    return captured


def test_pipeline_settle_refund_path(stub_usage_logs):
    """When actual < reservation the diff is refunded into the same row."""
    repo = _StubTenantsRepo(total_credit=10_000, used=5_000)

    settle_reservation_and_log(
        user=_user("u-1", "default-org"),
        tenants_repo=repo,  # type: ignore[arg-type]
        reservation=5_000,
        actual_input_tokens=1_000,
        actual_output_tokens=500,
        model_id="us.anthropic.claude-opus-4-7",
    )

    # 5_000 reserved, 1_500 used → 3_500 refunded → balance back to 6_500.
    assert repo.refund_calls == [3_500]
    assert repo.reserve_calls == []
    assert repo.used == 1_500

    assert len(stub_usage_logs) == 1
    log = stub_usage_logs[0]
    assert log["input_tokens"] == 1_000
    assert log["output_tokens"] == 500
    assert log["model_id"] == "us.anthropic.claude-opus-4-7"


def test_pipeline_settle_overrun_path(stub_usage_logs):
    """When actual > reservation the overrun triggers an additional reserve.

    UsageLogs still records the *actual* usage even when the additional
    reserve clamps against an exhausted balance.
    """
    repo = _StubTenantsRepo(total_credit=10_000, used=2_000)

    settle_reservation_and_log(
        user=_user("u-1", "default-org"),
        tenants_repo=repo,  # type: ignore[arg-type]
        reservation=2_000,
        actual_input_tokens=2_500,
        actual_output_tokens=500,
        model_id="openai.gpt-5.4",
    )

    # 2_000 reserved, 3_000 used → +1_000 additional reserve.
    assert repo.refund_calls == []
    assert repo.reserve_calls == [1_000]
    assert repo.used == 3_000

    assert len(stub_usage_logs) == 1
    log = stub_usage_logs[0]
    assert log["input_tokens"] == 2_500
    assert log["output_tokens"] == 500


def test_pipeline_settle_zero_actual_refunds_full(stub_usage_logs):
    """A streaming early-exit (zero observed tokens) must refund everything."""
    repo = _StubTenantsRepo(total_credit=10_000, used=8_192)

    settle_reservation_and_log(
        user=_user("u-1", "default-org"),
        tenants_repo=repo,  # type: ignore[arg-type]
        reservation=8_192,
        actual_input_tokens=0,
        actual_output_tokens=0,
        model_id="openai.gpt-5.4",
    )

    assert repo.refund_calls == [8_192]
    assert repo.reserve_calls == []
    assert repo.used == 0

    assert len(stub_usage_logs) == 1
    log = stub_usage_logs[0]
    assert log["input_tokens"] == 0
    assert log["output_tokens"] == 0


def test_pipeline_overrun_clamps_when_balance_exhausted(stub_usage_logs):
    """Overrun + insufficient balance: clamp to remaining, log the gap."""
    # Reserved 2k, used 2k. total_credit 10k. Now actual is 12k → overrun
    # = 10k, but balance is 8k. Reserve(10k) must fail; clamp to 8k and
    # mark uncovered = 2k. UsageLogs still records actual = 12k.
    repo = _StubTenantsRepo(total_credit=10_000, used=2_000, reserve_fails=1)

    settle_reservation_and_log(
        user=_user("u-1", "default-org"),
        tenants_repo=repo,  # type: ignore[arg-type]
        reservation=2_000,
        actual_input_tokens=10_000,
        actual_output_tokens=2_000,
        model_id="openai.gpt-5.4",
    )

    # First reserve(10000) fails, then clamp reserve(8000) succeeds.
    assert repo.reserve_calls == [10_000, 8_000]
    assert repo.refund_calls == []
    assert repo.used == 10_000

    assert len(stub_usage_logs) == 1
    log = stub_usage_logs[0]
    assert log["input_tokens"] == 10_000
    assert log["output_tokens"] == 2_000
