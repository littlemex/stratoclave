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

from mvp import _budget_flow, _pipeline
from mvp import anthropic as anth
from mvp._pipeline import reserve_credit, settle_reservation_and_log, release_pool
from mvp._wire import anthropic_wire as wire
from mvp import _converse_types as t


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


class _TestAdapter:
    """Minimal adapter for characterization tests — renders same frames as Anthropic wire."""
    def __init__(self, model="us.anthropic.claude-opus-4-7"):
        self.state = wire.AnthropicStreamState(model=model)

    def prologue(self):
        return wire.stream_prologue(self.state)

    def render_event(self, event):
        return wire.render_stream_event(event, self.state)

    def epilogue(self):
        return wire.stream_epilogue(self.state)

    def error_event(self, message):
        return wire.error_event(message)


def _make_body():
    return anth.AnthropicMessagesRequest.model_validate(
        {
            "model": "us.anthropic.claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16,
            "stream": True,
        }
    )


def _hold(user, ctx, reservation, settle, *, release):
    """The hold the flow reports to, wired to the REAL pool moves."""
    from mvp._money import Hold

    return Hold(
        user=user, tenants_repo=ctx, reservation=reservation,
        model_id="us.anthropic.claude-opus-4-7", settle=settle, release=release,
    )


def _make_settle_counter() -> tuple[dict, callable]:
    """Wrap the real settle so tests can count invocations AND keep real effects
    (pool move + UsageLogs write). Returns (counter_dict, counting_settle).
    """
    counter = {"n": 0}

    def counting_settle(**kwargs):
        counter["n"] += 1
        return settle_reservation_and_log(**kwargs)

    return counter, counting_settle


def _make_release_counter() -> tuple[dict, callable]:
    counter = {"n": 0}

    def counting_release(ctx):
        counter["n"] += 1
        return release_pool(ctx)

    return counter, counting_release


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
@pytest.mark.parametrize("stop_after", [2, 3, 4, 5, 6, 7, None])
def test_settle_runs_exactly_once_on_disconnect_at_any_yield(
    seed_tenant_with_pool, stop_after, monkeypatch
):
    """Disconnecting at ANY yield after the provider call (or running to
    completion, stop_after=None) must settle the reservation exactly once and leave
    pool_reserved at zero.

    `stop_after=1` is excluded and covered by the test below: the first yield is
    the wire prologue, which precedes the provider call, so that ending is a return
    rather than a settle.

    Pinned with `STRATOCLAVE_UNOBSERVED_HOLDS` explicitly OFF, because that is the
    configuration this characterisation is about. With the flag at its shipped
    default an early disconnect — before the provider's usage event arrives — is a
    departed call whose cost was never observed, and it is retained rather than
    settled at zero; the test below covers that. Making the flag explicit here also
    fixes the way this file broke when the default flipped: it depended on a default
    without naming it, so a grep for the flag name did not find it.
    """
    from mvp import provider_outcome as _po

    monkeypatch.setenv(_po.UNOBSERVED_HOLD_ENV, "0")
    seed = seed_tenant_with_pool
    user = _User(user_id=seed["user_id"], org_id=seed["tenant_id"])
    reservation = 4000
    ctx = reserve_credit(user, reservation, pricing_key="opus", cost_microusd=2_000_000)
    assert _pool(seed)["pool_reserved_microusd"] == 2_000_000

    settle_calls, counting_settle = _make_settle_counter()
    fake = _FakeBedrock(stream=_SuccessStream())

    gen = _budget_flow.run_stream(
        body=_make_body(),
        model_id="us.anthropic.claude-opus-4-7",
        hold=_hold(user, ctx, reservation, counting_settle,
                   release=lambda c: release_pool(c)),
        invoke_stream=lambda *, body, model_id: fake.converse_stream(),
        adapter=_TestAdapter(),
    )
    asyncio.run(_drive(gen, stop_after=stop_after))

    assert settle_calls["n"] == 1, (
        f"settle must run exactly once (disconnect after {stop_after} chunks), "
        f"got {settle_calls['n']}"
    )
    summary = _pool(seed)
    assert summary["pool_reserved_microusd"] == 0, "pool_reserved must not go negative"
    assert summary["pool_reserved_microusd"] >= 0


@pytest.mark.parametrize("stop_after", [2, 3, 4])
def test_a_disconnect_before_the_usage_event_is_retained_by_default(
    seed_tenant_with_pool, stop_after
):
    """The shipped default, on the same disconnects the test above pins with the flag
    off. The provider call departed and the client stopped listening before the usage
    event arrived, so what it cost is exactly the thing nobody observed — an abandoned
    Bedrock call is billed for the full generation. Settling zero would record that the
    call was free. The reservation is retained instead, and an operator ends it at the
    figure the provider's own record shows.

    Later disconnects (5, 6, 7, and running to completion) are NOT parametrized here on
    purpose: by then the usage event has arrived, the cost IS observed, and those settle
    normally at their default. That the split falls exactly at the usage event is the
    property worth pinning — retention is scoped to what was not seen, not to every
    failure."""
    seed = seed_tenant_with_pool
    user = _User(user_id=seed["user_id"], org_id=seed["tenant_id"])
    reservation = 4000
    ctx = reserve_credit(user, reservation, pricing_key="opus", cost_microusd=2_000_000)

    settle_calls, counting_settle = _make_settle_counter()
    fake = _FakeBedrock(stream=_SuccessStream())

    gen = _budget_flow.run_stream(
        body=_make_body(),
        model_id="us.anthropic.claude-opus-4-7",
        hold=_hold(user, ctx, reservation, counting_settle,
                   release=lambda c: release_pool(c)),
        invoke_stream=lambda *, body, model_id: fake.converse_stream(),
        adapter=_TestAdapter(),
    )
    asyncio.run(_drive(gen, stop_after=stop_after))

    assert settle_calls["n"] == 0, (
        f"a disconnect after {stop_after} chunks observed no usage, so settling it "
        f"records a cost nobody measured")
    assert _pool(seed)["pool_reserved_microusd"] == 2_000_000, (
        "the reservation must still be held: the money may have been spent")


def test_a_disconnect_before_the_provider_call_returns_the_reservation(
    seed_tenant_with_pool,
):
    """Nothing was sent, so nothing could be billed — and the pool must come back
    to exactly where it started, with no spend recorded against it."""
    seed = seed_tenant_with_pool
    user = _User(user_id=seed["user_id"], org_id=seed["tenant_id"])
    reservation = 4000
    ctx = reserve_credit(user, reservation, pricing_key="opus", cost_microusd=2_000_000)
    assert _pool(seed)["pool_reserved_microusd"] == 2_000_000

    settle_calls, counting_settle = _make_settle_counter()
    fake = _FakeBedrock(stream=_SuccessStream())

    gen = _budget_flow.run_stream(
        body=_make_body(),
        model_id="us.anthropic.claude-opus-4-7",
        hold=_hold(user, ctx, reservation, counting_settle,
                   release=lambda c: release_pool(c)),
        invoke_stream=lambda *, body, model_id: fake.converse_stream(),
        adapter=_TestAdapter(),
    )
    asyncio.run(_drive(gen, stop_after=1))

    assert settle_calls["n"] == 0, "a request that never reached the provider settled"
    summary = _pool(seed)
    assert summary["pool_reserved_microusd"] == 0
    assert summary["pool_settled_microusd"] == 0, "spend was recorded for no request"
    assert summary["remaining_microusd"] == seed["pool_limit_microusd"]


# ---------------------------------------------------------------------------
# invoke-time failure: full refund + release, NO settle.
# ---------------------------------------------------------------------------
def test_invoke_time_failure_releases_pool_and_does_not_settle(
    seed_tenant_with_pool,
):
    seed = seed_tenant_with_pool
    user = _User(user_id=seed["user_id"], org_id=seed["tenant_id"])
    reservation = 4000
    ctx = reserve_credit(user, reservation, pricing_key="opus", cost_microusd=2_000_000)
    assert _pool(seed)["pool_reserved_microusd"] == 2_000_000

    settle_calls, counting_settle = _make_settle_counter()
    release_calls, counting_release = _make_release_counter()
    err = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "bad request"}},
        "ConverseStream",
    )

    def raising_invoke(*, body, model_id):
        raise err

    gen = _budget_flow.run_stream(
        body=_make_body(),
        model_id="us.anthropic.claude-opus-4-7",
        hold=_hold(user, ctx, reservation, counting_settle, release=counting_release),
        invoke_stream=raising_invoke,
        adapter=_TestAdapter(),
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
    seed_tenant_with_pool, monkeypatch,
):
    """Pinned with retention explicitly OFF: what this characterises is that the
    ending runs once and settle owns the hold, which is a property of the flow rather
    than of the flag. The stub raises before delivering any event, so nothing was
    observed and the shipped default retains instead — covered by the test below."""
    from mvp import provider_outcome as _po

    monkeypatch.setenv(_po.UNOBSERVED_HOLD_ENV, "0")
    seed = seed_tenant_with_pool
    user = _User(user_id=seed["user_id"], org_id=seed["tenant_id"])
    reservation = 4000
    ctx = reserve_credit(user, reservation, pricing_key="opus", cost_microusd=2_000_000)

    settle_calls, counting_settle = _make_settle_counter()
    release_calls, counting_release = _make_release_counter()

    gen = _budget_flow.run_stream(
        body=_make_body(),
        model_id="us.anthropic.claude-opus-4-7",
        hold=_hold(user, ctx, reservation, counting_settle, release=counting_release),
        invoke_stream=lambda *, body, model_id: {"stream": _RaisingMidStream()},
        adapter=_TestAdapter(),
    )
    chunks = asyncio.run(_drive(gen))

    assert settle_calls["n"] == 1, "mid-stream failure must settle the partial usage once"
    assert release_calls["n"] == 0, "mid-stream failure must NOT release (settle owns the hold)"
    summary = _pool(seed)
    assert summary["pool_reserved_microusd"] == 0
    assert any(b"error" in c for c in chunks), "an error event must be emitted"


def test_a_mid_stream_break_with_nothing_observed_is_retained_by_default(
    seed_tenant_with_pool,
):
    """The shipped default on the same break. The call returned, so the request left
    and the model may have run to completion; the stream then died before a single
    event, so its cost was never observed. Settling zero here is the record this
    retention exists to stop making. Note the exception is a bare `RuntimeError`,
    which `classify_exception` cannot distinguish from a departed call — the departure
    evidence is what carries that judgement, not the exception type."""
    seed = seed_tenant_with_pool
    user = _User(user_id=seed["user_id"], org_id=seed["tenant_id"])
    reservation = 4000
    ctx = reserve_credit(user, reservation, pricing_key="opus", cost_microusd=2_000_000)

    settle_calls, counting_settle = _make_settle_counter()
    release_calls, counting_release = _make_release_counter()

    gen = _budget_flow.run_stream(
        body=_make_body(),
        model_id="us.anthropic.claude-opus-4-7",
        hold=_hold(user, ctx, reservation, counting_settle, release=counting_release),
        invoke_stream=lambda *, body, model_id: {"stream": _RaisingMidStream()},
        adapter=_TestAdapter(),
    )
    chunks = asyncio.run(_drive(gen))

    assert settle_calls["n"] == 0, "nothing was observed, so nothing may be settled"
    assert release_calls["n"] == 0, "the reservation is retained, not released"
    assert _pool(seed)["pool_reserved_microusd"] == 2_000_000, (
        "the reservation must still be held: the money may have been spent")
    assert any(b"error" in c for c in chunks), "an error event must be emitted"
