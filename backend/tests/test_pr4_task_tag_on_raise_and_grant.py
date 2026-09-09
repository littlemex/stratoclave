"""HANDOFF-PR4 I3/P4.5: a raise carries a task tag, resolved through the ONE
existing canonicaliser (`mvp.task_tag.resolve`) -- "that call and no other
validation" -- stored on the request row as `task_tag`/`task_tag_source`, and
COPIED onto the grant row by the approval.

Two raw tags are exercised on purpose, per this split's own priority list:
one that survives but CHANGES shape under canonicalisation (case-folded), and
one that is DROPPED (its canonical form IS the reserved sentinel,
`mvp.task_tag.SENTINEL`). A test that only tried an already-canonical tag
could not tell "resolve() actually ran" apart from "the raw string is stored
verbatim" -- those two behaviours agree on an already-canonical input, and
would agree right up until the first caller who does not happen to type a tag
that was already in canonical form.
"""
from __future__ import annotations

from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
from tests.quota_events_fixtures import (
    freeze_grants_clock,
    quota_events_table,
    seed_tenant,
)

assert quota_events_table  # imported for its pytest-fixture side effect

TENANT = "pr4-tag-org"
MEMBER = "member-1"
APPROVER = "admin-1"
T0 = 1_788_307_200  # 2026-09-02T00:00:00Z


def _member():
    from mvp.deps import AuthenticatedUser

    return AuthenticatedUser(
        user_id=MEMBER, email="m@example.com", org_id=TENANT,
        roles=["user"], raw_claims={}, auth_kind="cognito",
    )


def _approver():
    from mvp.deps import AuthenticatedUser

    return AuthenticatedUser(
        user_id=APPROVER, email="a@example.com", org_id=APPROVER,
        roles=["admin"], raw_claims={}, auth_kind="cognito",
    )


def _seed_pool(period: str) -> None:
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=TENANT, period=period, manual_limit_microusd=10**9)


def _approve(request_id: str, amount: int):
    from mvp import grants

    return grants.approve_limit_raise(
        actor=_approver(), request_id=request_id,
        approved_amount_microusd=amount, expires_at=T0 + 3600,
    )


def test_a_tag_that_changes_under_canonicalisation_lands_canonical_on_both_rows(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    from dynamo.quota_events import QuotaEventsRepository
    from mvp import grants, task_tag

    seed_tenant(TENANT, team_lead_user_id="admin-owned")
    _seed_pool(current_period())
    freeze_grants_clock(monkeypatch, T0)

    raw = "Team-ROCKET"
    canonical = task_tag.canonical(raw)
    assert canonical != raw, (
        "the example itself must genuinely change shape under "
        "canonicalisation, or this test cannot distinguish resolve() "
        "actually running from the raw string being stored untouched"
    )

    filed = grants.submit_limit_raise(
        actor=_member(), asked_amount_microusd=1_000_000, reason_code="usage_spike",
        client_token="tok-tag-changed", limit_kind="user_dollar_quota",
        comment="tagged raise", task_tag=raw,
    )
    repo = QuotaEventsRepository()
    request_row = repo.get_request(filed["request_id"])
    assert request_row.get("task_tag") == canonical, (
        f"the request must store the CANONICAL form ({canonical!r}), not the "
        f"raw string as typed ({raw!r}); got "
        f"{request_row.get('task_tag')!r}"
    )
    assert request_row.get("task_tag_source") == task_tag.Source.ASSERTED.value, (
        f"a grammar-valid, non-reserved tag resolves to ASSERTED; got "
        f"{request_row.get('task_tag_source')!r}"
    )

    result = _approve(filed["request_id"], 1_000_000)
    grant_row = repo.get_grant(
        tenant_id=TENANT, grant_id=result["grant"]["grant_id"])
    assert grant_row.get("task_tag") == canonical, (
        "the grant must carry the SAME canonical tag the request was filed "
        "under -- COPIED, not re-resolved on the grant's own path (there is "
        "nothing on that path to resolve it FROM if it were)"
    )
    assert grant_row.get("task_tag_source") == task_tag.Source.ASSERTED.value, (
        "I3: 'the resolved PAIR is stored... and copied onto the grant "
        "row' -- the pair, not the tag alone. A grant that carries the "
        "canonical string but a different or absent source cannot tell an "
        "asserted tag apart from one that landed here some other way"
    )


def test_a_tag_that_canonicalises_onto_the_reserved_sentinel_is_dropped_on_both_rows(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    from dynamo.quota_events import QuotaEventsRepository
    from mvp import grants, task_tag

    seed_tenant(TENANT, team_lead_user_id="admin-owned")
    _seed_pool(current_period())
    freeze_grants_clock(monkeypatch, T0)

    raw = "UnLabelled"  # canonicalises onto task_tag.SENTINEL itself
    assert task_tag.canonical(raw) == task_tag.SENTINEL

    filed = grants.submit_limit_raise(
        actor=_member(), asked_amount_microusd=1_000_000, reason_code="usage_spike",
        client_token="tok-tag-dropped", limit_kind="user_dollar_quota",
        comment="a caller who typed the sentinel's own spelling",
        task_tag=raw,
    )
    repo = QuotaEventsRepository()
    request_row = repo.get_request(filed["request_id"])
    assert request_row.get("task_tag") == task_tag.SENTINEL
    assert request_row.get("task_tag_source") == task_tag.Source.DROPPED_RESERVED.value, (
        f"a caller-typed tag that IS the sentinel (any casing) must record "
        f"as DROPPED_RESERVED, never ASSERTED -- an asserted sentinel would "
        f"be indistinguishable from genuinely untagged spend, exactly what "
        f"the reservation exists to prevent. Got "
        f"{request_row.get('task_tag_source')!r}"
    )

    result = _approve(filed["request_id"], 1_000_000)
    grant_row = repo.get_grant(
        tenant_id=TENANT, grant_id=result["grant"]["grant_id"])
    assert grant_row.get("task_tag") == task_tag.SENTINEL, (
        "the dropped tag's SENTINEL form must copy onto the grant too, not "
        "just onto the request"
    )
    assert grant_row.get("task_tag_source") == task_tag.Source.DROPPED_RESERVED.value


def test_an_unspecified_tag_is_absent_not_dropped_reserved(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    """A caller who never asserts a tag at all must read as ABSENT, distinct
    from a caller who explicitly typed the sentinel's own spelling --
    `mvp.task_tag.Source` keeps the two apart for exactly this reason, and a
    raise's default (`task_tag=None`) must resolve through the same
    `resolve()` call as an explicit one rather than skip it."""
    from dynamo.quota_events import QuotaEventsRepository
    from mvp import grants, task_tag

    seed_tenant(TENANT, team_lead_user_id="admin-owned")
    _seed_pool(current_period())
    freeze_grants_clock(monkeypatch, T0)

    filed = grants.submit_limit_raise(
        actor=_member(), asked_amount_microusd=1_000_000, reason_code="usage_spike",
        client_token="tok-tag-absent", limit_kind="user_dollar_quota",
        comment="no tag given",
    )
    request_row = QuotaEventsRepository().get_request(filed["request_id"])
    assert request_row.get("task_tag") == task_tag.SENTINEL
    assert request_row.get("task_tag_source") == task_tag.Source.ABSENT.value, (
        f"an omitted task_tag must resolve to ABSENT, not DROPPED_RESERVED "
        f"or any other source -- got "
        f"{request_row.get('task_tag_source')!r}"
    )
