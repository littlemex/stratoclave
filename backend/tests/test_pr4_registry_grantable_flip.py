"""HANDOFF-PR4 P4.1 (`user_dollar_quota` becomes `grantable=True`, and the
raise path actually admits a filing against it in the same PR) and P4.9 (the
`UnknownLimitKind` message stops naming `POOL_WALL` by hand and instead
derives the grantable set from `RESERVE_LIMITS`, `sorted(k.name for k in
RESERVE_LIMITS if k.grantable)`).

Both are cheap, fully-specified checks -- I1 gives the exact registry field
and the exact derivation -- so they are pinned directly rather than folded
into a larger scenario file: a regression in either is a one-line diff this
file catches at the source, before it can hide behind a bigger test's own
setup.
"""
from __future__ import annotations

import pytest

from tests.quota_events_fixtures import (
    freeze_grants_clock,
    quota_events_table,
    seed_tenant,
)

assert quota_events_table  # imported for its pytest-fixture side effect

TENANT = "pr4-registry-org"
T0 = 1_788_307_200  # 2026-09-02T00:00:00Z


def _actor(user_id: str) -> "object":
    from mvp.deps import AuthenticatedUser

    return AuthenticatedUser(
        user_id=user_id, email=f"{user_id}@example.com", org_id=TENANT,
        roles=["user"], raw_claims={}, auth_kind="cognito",
    )


def test_p41_user_dollar_quota_wall_is_declared_grantable():
    """I1: the registry entry's OWN `grantable` field flips. Read directly
    from the declaration `submit_limit_raise`/`is_grantable_wall` both trust,
    so a stale `False` here means every downstream check that reads the
    registry is still refusing a raise this PR exists to allow, regardless
    of what `submit_limit_raise` itself does."""
    from mvp.reserve_limits import limit_kind

    assert limit_kind("user_dollar_quota").grantable is True, (
        "P4.1 flips this wall's own grantable flag in RESERVE_LIMITS; the "
        "registry entry is the single source of truth, and every other "
        "component in this PR (submit_limit_raise's validity check, "
        "blocker_for_wall, the 402's headline ordering) reads it rather than "
        "deciding for itself"
    )
    # I4/P4.9's own reason this is testable at all: the pool wall must stay
    # grantable too, so there are genuinely TWO grantable walls for P4.9's
    # derived set to name, not a flip that silently dropped the first one.
    assert limit_kind("tenant_dollar_pool").grantable is True


def test_p41_filing_against_the_per_user_wall_is_admitted_end_to_end(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    """The end-to-end form of the same flip: on `origin/main`,
    `submit_limit_raise(limit_kind="user_dollar_quota", ...)` raises
    `UnknownLimitKind` (the wall is known but not grantable, and
    `submit_limit_raise` refuses both cases identically). After P4.1 it must
    be admitted -- a PENDING request created and returned, under its own
    `limit_kind`, not silently coerced onto the pool wall."""
    from mvp import grants

    seed_tenant(TENANT, team_lead_user_id="admin-owned")
    freeze_grants_clock(monkeypatch, T0)

    result = grants.submit_limit_raise(
        actor=_actor("u-flip"), asked_amount_microusd=1_000_000,
        reason_code="usage_spike", client_token="tok-flip",
        limit_kind="user_dollar_quota", comment="need more of my own room",
    )
    assert result["status"] == "PENDING"
    assert result["limit_kind"] == "user_dollar_quota", (
        f"the request must be recorded under the wall it was actually filed "
        f"against; got limit_kind={result['limit_kind']!r}"
    )


def test_p49_unknown_limit_kind_message_names_both_grantable_walls_dynamically(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    """P4.9, verbatim: the message is BUILT from
    `sorted(k.name for k in RESERVE_LIMITS if k.grantable)` rather than a
    literal naming only `POOL_WALL`. Filed against a wall that is real but
    NOT grantable (`user_token_quota`, unaffected by this PR) so the refusal
    fires for the same reason on `origin/main` and after -- what must change
    is the MESSAGE TEXT, which on `origin/main` says only `tenant_dollar_pool`
    is grantable (`grants.py:920-930`'s old hardcoded sentence) and after
    P4.1/P4.9 must also say `user_dollar_quota` -- the exact defect P4.9
    exists to close, now that a second wall genuinely is grantable."""
    from mvp import grants

    seed_tenant(TENANT, team_lead_user_id="admin-owned")
    freeze_grants_clock(monkeypatch, T0)

    with pytest.raises(grants.UnknownLimitKind) as ei:
        grants.submit_limit_raise(
            actor=_actor("u-msg"), asked_amount_microusd=1_000,
            reason_code="other", client_token="tok-msg",
            limit_kind="user_token_quota",
        )
    exc = ei.value
    assert exc.extra.get("grantable") is False, (
        "user_token_quota itself must still read as non-grantable -- this "
        "test is about the MESSAGE listing the grantable set, not about "
        "flipping a second wall by mistake"
    )
    message = str(exc)
    for wall_name in ("tenant_dollar_pool", "user_dollar_quota"):
        assert wall_name in message, (
            f"the refusal derives its grantable set from RESERVE_LIMITS, "
            f"which now has TWO grantable walls -- {wall_name!r} must appear "
            f"in the message text. A message still naming only the pool "
            f"wall is P4.9's own defect, unfixed. Got message={message!r}"
        )
