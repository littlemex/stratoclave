"""Documents the pre-iteration reservation leak on the streaming path.

When `POST /v1/messages` with `stream: true` reserves credit in the route
handler and hands a generator to `StreamingResponse`, the reservation is
already committed before the generator body runs. If the ASGI server detects
the client is gone and never iterates the generator, neither the body nor its
`finally` executes — so the reservation and the pool hold leak with nothing to
reclaim them except the orphan reaper (bounded, lazy, next-request-driven).

This test pins that leak as a KNOWN GAP. It is marked `xfail(strict=True)`:
it fails today (the leak is real), and the marker keeps the suite green while
making the gap visible. When the streaming path is reworked so the reservation
is owned by the iterated generator (or an explicit never-iterated guard
releases it), this test flips to a genuine pass and the strict marker will then
force us to drop the xfail — turning the documented gap into an enforced fix.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

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


@pytest.mark.xfail(
    strict=True,
    reason="pre-iteration reservation leak: a stream abandoned before the first "
    "byte never runs the generator's finally, so pool_reserved leaks until the "
    "reaper reclaims it. Closed in the budget-flow rework.",
)
def test_stream_abandoned_before_first_byte_does_not_leak(seed_tenant_with_pool):
    """A reserved streaming request whose generator is never iterated must not
    leave pool_reserved outstanding. Today it does — hence xfail.
    """
    seed = seed_tenant_with_pool
    user = _User(user_id=seed["user_id"], org_id=seed["tenant_id"])
    reservation = 4000

    # The route reserves here, before StreamingResponse would iterate anything.
    ctx = reserve_credit(user, reservation, pricing_key="opus", cost_microusd=2_000_000)
    assert _pool(seed)["pool_reserved_microusd"] == 2_000_000

    # Build the generator exactly as the route does, then abandon it WITHOUT
    # iterating a single chunk — the ASGI "client already gone" path.
    gen = anth._stream_messages(
        body=anth.AnthropicMessagesRequest.model_validate(
            {
                "model": "us.anthropic.claude-opus-4-7",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 16,
                "stream": True,
            }
        ),
        model_id="us.anthropic.claude-opus-4-7",
        user=user,
        tenants_repo=ctx,
        reservation=reservation,
    )
    del gen  # never iterated; no finally runs

    # The reservation should have been returned. It has NOT been (leak) — so this
    # assertion fails today and the xfail(strict) records the known gap.
    assert _pool(seed)["pool_reserved_microusd"] == 0
