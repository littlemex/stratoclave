"""Characterization tests pinning the streaming reserve/settle control flow.

These lock the money-critical behaviour of the Anthropic streaming path
(`mvp.anthropic._stream_messages`) BEFORE it is refactored behind a shared
budget-flow layer. They are written against the observable contract, not the
internal structure, so they must stay green across the move:

  - the reservation is settled EXACTLY ONCE regardless of where the client
    disconnects, and the tenant pool's outstanding reservation never goes
    negative (a double-settle would drive `pool_reserved_microusd` below zero);
  - an invoke-time failure (Bedrock rejects the call before any tokens are
    produced) refunds the whole reservation and releases the pool hold, and
    does NOT record spend;
  - a mid-stream failure (the event stream breaks after the call succeeded)
    settles the partial usage once and does NOT release the hold a second time.

The disconnect case is exercised by injecting `GeneratorExit` at every yield
point via `aclose()`: this is exactly what an ASGI server does when the client
goes away mid-response, and it is the scenario where a settle/`settled=True`
ordering slip would double-count.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from botocore.exceptions import ClientError

from mvp import _pipeline
from mvp import anthropic as anth
from mvp._pipeline import reserve_credit


@dataclass
class _User:
    user_id: str
    org_id: str
    email: str = "u@example.com"


def _pool(seed):
    from dynamo.tenant_budgets import TenantBudgetsRepository

    return TenantBudgetsRepository().pool_summary(seed["tenant_id"], seed["period"])


class _SuccessStream:
    """A well-formed Bedrock converse_stream: two text deltas, a stop, usage."""

    def __init__(self):
        self._events = iter(
            [
                {"contentBlockDelta": {"delta": {"text": "he"}}},
                {"contentBlockDelta": {"delta": {"text": "llo"}}},
                {"messageStop": {"stopReason": "end_turn"}},
                {"metadata": {"usage": {"inputTokens": 12, "outputTokens": 3}}},
            ]
        )

    def __iter__(self):
        return self._events


class _RaisingMidStream:
    """A stream that breaks after the call returned — a mid-stream failure."""

    def __iter__(self):
        return self

    def __next__(self):
        raise RuntimeError("bedrock stream broke mid-flight")


class _FakeBedrock:
    def __init__(self, *, stream=None, raise_on_call=None):
        self._stream = stream
        self._raise_on_call = raise_on_call

    def converse_stream(self, **kwargs):
        if self._raise_on_call is not None:
            raise self._raise_on_call
        return {"stream": self._stream}


def _make_body():
    return anth.AnthropicMessagesRequest.model_validate(
        {
            "model": "us.anthropic.claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16,
            "stream": True,
        }
    )


def _install_settle_counter(monkeypatch) -> dict:
    """Wrap the real settle so tests can count invocations AND keep real effects
    (pool move + UsageLogs write). Returns a mutable counter dict.
    """
    counter = {"n": 0}
    real_settle = _pipeline.settle_reservation_and_log

    def counting_settle(**kwargs):
        counter["n"] += 1
        return real_settle(**kwargs)

    monkeypatch.setattr(anth, "_settle_reservation_and_log", counting_settle)
    return counter


def _install_release_counter(monkeypatch) -> dict:
    counter = {"n": 0}
    real_release = _pipeline.release_pool

    def counting_release(ctx):
        counter["n"] += 1
        return real_release(ctx)

    monkeypatch.setattr(anth, "_release_pool", counting_release)
    return counter


async def _drive(gen, *, stop_after=None) -> list:
    """Iterate `gen`. If `stop_after` is set, `aclose()` after that many chunks
    (injecting GeneratorExit at that yield). Otherwise exhaust fully.
    """
    agen = gen.__aiter__()
    got: list = []
    try:
        while True:
            if stop_after is not None and len(got) >= stop_after:
                await agen.aclose()
                break
            got.append(await agen.__anext__())
    except StopAsyncIteration:
        pass
    return got


# ---------------------------------------------------------------------------
# settle happens exactly once, and the pool never goes negative, no matter
# where the client disconnects.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stop_after", [1, 2, 3, 4, 5, 6, 7, None])
def test_settle_runs_exactly_once_on_disconnect_at_any_yield(
    seed_tenant_with_pool, monkeypatch, stop_after
):
    """Disconnecting at ANY yield (or running to completion, stop_after=None)
    must settle the reservation exactly once and leave pool_reserved at zero.
    """
    seed = seed_tenant_with_pool
    user = _User(user_id=seed["user_id"], org_id=seed["tenant_id"])
    reservation = 4000
    ctx = reserve_credit(user, reservation, pricing_key="opus", cost_microusd=2_000_000)
    assert _pool(seed)["pool_reserved_microusd"] == 2_000_000

    settle_calls = _install_settle_counter(monkeypatch)
    monkeypatch.setattr(anth, "_bedrock_client", lambda: _FakeBedrock(stream=_SuccessStream()))

    gen = anth._stream_messages(
        body=_make_body(),
        model_id="us.anthropic.claude-opus-4-7",
        user=user,
        tenants_repo=ctx,
        reservation=reservation,
    )
    asyncio.run(_drive(gen, stop_after=stop_after))

    assert settle_calls["n"] == 1, (
        f"settle must run exactly once (disconnect after {stop_after} chunks), "
        f"got {settle_calls['n']}"
    )
    summary = _pool(seed)
    assert summary["pool_reserved_microusd"] == 0, "pool_reserved must not go negative"
    assert summary["pool_reserved_microusd"] >= 0


# ---------------------------------------------------------------------------
# invoke-time failure: full refund + release, NO settle.
# ---------------------------------------------------------------------------
def test_invoke_time_failure_releases_pool_and_does_not_settle(
    seed_tenant_with_pool, monkeypatch
):
    seed = seed_tenant_with_pool
    user = _User(user_id=seed["user_id"], org_id=seed["tenant_id"])
    reservation = 4000
    ctx = reserve_credit(user, reservation, pricing_key="opus", cost_microusd=2_000_000)
    assert _pool(seed)["pool_reserved_microusd"] == 2_000_000

    settle_calls = _install_settle_counter(monkeypatch)
    release_calls = _install_release_counter(monkeypatch)
    err = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "bad request"}},
        "ConverseStream",
    )
    monkeypatch.setattr(anth, "_bedrock_client", lambda: _FakeBedrock(raise_on_call=err))

    gen = anth._stream_messages(
        body=_make_body(),
        model_id="us.anthropic.claude-opus-4-7",
        user=user,
        tenants_repo=ctx,
        reservation=reservation,
    )
    chunks = asyncio.run(_drive(gen))

    assert settle_calls["n"] == 0, "invoke-time failure must NOT record spend"
    assert release_calls["n"] == 1, "invoke-time failure must release the pool hold"
    summary = _pool(seed)
    assert summary["pool_reserved_microusd"] == 0, "reservation must be returned"
    assert summary["pool_settled_microusd"] == 0, "nothing was spent"
    assert summary["remaining_microusd"] == seed["pool_limit_microusd"]
    assert any(b"error" in c for c in chunks), "an error event must be emitted"


# ---------------------------------------------------------------------------
# mid-stream failure: partial settle once, NO release.
# ---------------------------------------------------------------------------
def test_mid_stream_failure_settles_once_and_does_not_release(
    seed_tenant_with_pool, monkeypatch
):
    seed = seed_tenant_with_pool
    user = _User(user_id=seed["user_id"], org_id=seed["tenant_id"])
    reservation = 4000
    ctx = reserve_credit(user, reservation, pricing_key="opus", cost_microusd=2_000_000)

    settle_calls = _install_settle_counter(monkeypatch)
    release_calls = _install_release_counter(monkeypatch)
    monkeypatch.setattr(
        anth, "_bedrock_client", lambda: _FakeBedrock(stream=_RaisingMidStream())
    )

    gen = anth._stream_messages(
        body=_make_body(),
        model_id="us.anthropic.claude-opus-4-7",
        user=user,
        tenants_repo=ctx,
        reservation=reservation,
    )
    chunks = asyncio.run(_drive(gen))

    assert settle_calls["n"] == 1, "mid-stream failure must settle the partial usage once"
    assert release_calls["n"] == 0, "mid-stream failure must NOT release (settle owns the hold)"
    summary = _pool(seed)
    assert summary["pool_reserved_microusd"] == 0
    assert any(b"error" in c for c in chunks), "an error event must be emitted"
