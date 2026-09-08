"""Persona journey: the engineer who tags her work, and the engineer whose
tag never arrives.

These are NOT requirement tests. Each walks a sequence a real person walks
and asks the only question that matters at that altitude: **does she find
out what her work cost, and is what she is told true?** Every step below is
satisfiable by unit tests that pass in isolation while she is still
misinformed at the end of the month -- that is precisely the interval these
tests occupy.

Drives the REAL edge resolution (`mvp.observability.context.
build_request_context`, the function `mvp.deps.get_request_context` calls
for every live request carrying `x-sc-task-tag`), carried onto the REAL
reserve/settle pipeline (`mvp._pipeline.reserve_credit` /
`settle_reservation_and_log` -- the same pair the codebase's own ledger
tests, e.g. `tests/test_credit_ledger.py`, drive directly rather than going
through a mocked model invocation), stamped with the resolved tag exactly
the way `reserve_credit_for_model`'s own chokepoint stamps it
(`ctx.task_tag = task_tag or SENTINEL`), and read back through the REAL
`GET /api/mvp/me/usage/by-tag` router on moto DynamoDB.

The journeys:
  A. She tags her work all month, typed three different ways, and asks what
     it cost.
  B. A header that never arrives -- a stray space her teammate told her to
     add for clarity -- and what her own report lets her believe about the
     work that carried it.
  C. Her own project happens to be named the one word this feature reserves
     for work nobody tagged.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dynamo.tenant_budgets import TenantBudgetsRepository
from dynamo.tenants import TenantsRepository
from dynamo.user_tenants import UserTenantsRepository
from mvp._pipeline import reserve_credit, settle_reservation_and_log
from mvp.deps import AuthenticatedUser, get_current_user
from mvp.observability.context import build_request_context

TENANT = "tagjourney-eng"
HER = "engineer-tagger-1"
LEAD = "lead-tagger-1"


@dataclass
class _PipelineUser:
    """The shape `reserve_credit`/`settle_reservation_and_log` take."""

    user_id: str
    org_id: str
    email: str = "her@journey"


def _seed_permissions(dynamodb_mock) -> None:
    """The real role -> permission table (`permissions.json`, the file the
    deployment itself seeds from), not a monkeypatched `user_has_permission`
    -- so `require_permission("usage:read-self")` on `/me/usage/by-tag` is
    the actual gate a live "user" role clears, not a bypass."""
    import pathlib as _p

    dynamodb_mock.create_table(
        TableName="stratoclave-permissions",
        KeySchema=[{"AttributeName": "role", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "role", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    from dynamo.permissions import PermissionsRepository

    PermissionsRepository().seed_from_file(
        _p.Path(__file__).resolve().parent.parent / "permissions.json"
    )
    import mvp.authz as authz

    authz._clear_permissions_cache()


def _seed_tenant(monkeypatch) -> str:
    """A tenant with real money in its pool and her as a provisioned member,
    seeded the way `_seed_tenant_with_spent_pool` seeds it in the existing
    grant journeys -- a period pinned so every call below and every read
    afterwards agree on which month is "now"."""
    import dynamo.tenant_budgets as _tb
    import mvp._pipeline as _pl

    period = _tb.current_period()
    monkeypatch.setattr(_pl, "current_period", lambda: period)

    TenantsRepository().create(
        tenant_id=TENANT, name="Tag Journey", team_lead_user_id=LEAD,
        default_credit=1_000_000, created_by=LEAD,
    )
    UserTenantsRepository().ensure(
        user_id=HER, tenant_id=TENANT, role="user", total_credit=1_000_000_000,
    )
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=TENANT, period=period, manual_limit_microusd=1_000_000_000,
    )
    return period


def _send(
    user: _PipelineUser, *, header: Optional[str], cost_micro: int,
    input_tokens: int = 100, output_tokens: int = 200,
    model: str = "us.anthropic.claude-opus-4-7",
) -> None:
    """One admitted-and-settled request that arrived carrying `header` under
    `x-sc-task-tag`.

    The (tag, source) pair is resolved by the REAL edge function
    `build_request_context` -- the same one `mvp.deps.get_request_context`
    calls on every live request -- so this is never a hand-computed guess
    about what `mvp.task_tag.resolve` would have done to a given header
    value. It is then stamped onto the reservation exactly the way
    `reserve_credit_for_model`'s own chokepoint stamps it
    (`ctx.task_tag = task_tag or SENTINEL`) rather than driving that
    chokepoint directly, which needs a tenant routing config this journey
    is not about; `reserve_credit` / `settle_reservation_and_log` below is
    the same pair `tests/test_credit_ledger.py` drives for the money path.
    """
    ctx_headers = build_request_context(
        tenant_id=user.org_id, group_id_header=None,
        workflow_run_id_header=None, task_tag_header=header,
    )
    ctx = reserve_credit(user, 1000, pricing_key="opus", cost_microusd=cost_micro)
    ctx.task_tag = ctx_headers.task_tag
    ctx.task_tag_source = ctx_headers.task_tag_source
    settle_reservation_and_log(
        user=user, tenants_repo=ctx, reservation=1000,
        actual_input_tokens=input_tokens, actual_output_tokens=output_tokens,
        model_id=model, context=ctx, actual_cost_microusd=cost_micro,
    )


def _me_client(user_id: str) -> TestClient:
    """A client over the one router this journey needs: her own usage-by-tag
    read. `require_permission("usage:read-self")` is left un-bypassed --
    her role really does hold it -- rather than monkeypatched around, so a
    future change that revokes it from `user` would fail here too."""
    from mvp.me import router as me_router

    app = FastAPI()
    app.include_router(me_router)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id=user_id, email=f"{user_id}@journey", org_id=TENANT,
        roles=["user"], raw_claims={}, auth_kind="cognito",
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# Journey A -- she tags all month and finds out what it cost
# ---------------------------------------------------------------------------


def test_journey_she_tags_all_month_and_finds_out_what_it_cost(
    monkeypatch, dynamodb_mock
):
    """She tags one project all month, typing the header three different
    ways -- capitalised at the start, plain lowercase mid-month, with a
    trailing space from a copy-paste on the last day -- and has one
    genuinely untagged, unrelated call. At month end she opens her own
    report and asks: does it tell her what she actually spent, or something
    close to it?

    Walks: three calls under the same project, spelled three different ways,
    plus one deliberately untagged call, then her own `GET
    /me/usage/by-tag`. Asserts the three spellings fold to ONE row with the
    EXACT sum (canonicalisation -- NFKC, casefold, strip -- protecting her
    from her own inconsistency, not merely tolerating it), that her total
    across every row equals her real total, and that this figure is not
    just self-consistent but matches the pool's own settled counter -- the
    number the money side of the gateway independently agrees she spent.
    """
    _seed_permissions(dynamodb_mock)
    period = _seed_tenant(monkeypatch)
    her = _PipelineUser(user_id=HER, org_id=TENANT)

    _send(her, header="Migration-42", cost_micro=10_000_000)
    _send(her, header="migration-42", cost_micro=20_000_000)
    _send(her, header="migration-42 ", cost_micro=5_000_000)
    _send(her, header=None, cost_micro=1_000_000)

    client = _me_client(HER)
    resp = client.get(f"/api/mvp/me/usage/by-tag?period={period}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    rows = {r["task_tag"]: r for r in body["rows"]}

    assert "migration-42" in rows, (
        f"her three differently-typed headers did not fold into one row: {body}"
    )
    migration = rows["migration-42"]
    assert migration["requests"] == 3
    assert migration["cost_microusd"] == 35_000_000, (
        "the total under her own tag must be the exact sum of what she spent "
        f"under it: {migration}"
    )
    assert migration["user_id"] == HER

    assert "unlabelled" in rows, (
        "her one deliberately untagged call must still be legible, not "
        f"silently dropped from the report: {body}"
    )
    assert rows["unlabelled"]["cost_microusd"] == 1_000_000

    total = sum(r["cost_microusd"] for r in body["rows"])
    assert total == 36_000_000
    pool_settled = TenantBudgetsRepository().pool_summary(TENANT, period)[
        "pool_settled_microusd"
    ]
    assert total == pool_settled, (
        "the by-tag report is not just internally consistent -- it must "
        f"match the pool's own settled counter: report={total} pool={pool_settled}"
    )

    # The two facts a consumer of this JSON would otherwise have to assume.
    assert body["tag_is_caller_asserted"] is True
    assert body["retention_policy_days"] == 90
    assert body["truncated"] is False


# ---------------------------------------------------------------------------
# Journey B -- the tag that never arrives, and the one that never was
# ---------------------------------------------------------------------------


def test_journey_a_dropped_tag_and_a_never_sent_one_look_identical_to_her(
    monkeypatch, dynamodb_mock
):
    """She tags six calls "release-9". On two more, a teammate's shell alias
    appends " (staging)" for clarity -- a space and parentheses are not in
    the grammar this header is checked against (`mvp.task_tag.GRAMMAR`, the
    same `[A-Za-z0-9._:-]` pattern the correlation headers use). The
    request is never refused, by design (`mvp.task_tag`'s whole reason to
    exist): both calls are simply recorded as `unlabelled`. She also runs
    one genuinely, deliberately untagged lookup that same month.

    What she would be told wrongly, reading her own report: that
    "unlabelled" means "work she never meant to tag" -- when two-thirds of
    that row's requests are the release she WAS tracking, mislabelled next
    to a lookup she never intended to track at all, and there is no error,
    warning, or count anywhere in the response that distinguishes a header
    that failed from a header that was never sent.

    Walks: six tagged calls, two calls whose header fails the grammar, one
    genuinely untagged call, then her own report. Asserts the fold that
    actually happens and that nothing on the row -- or the two disclosure
    counters that sound like they might cover this
    (`legacy_rows`/`malformed_rows`, which count something else entirely --
    see `dynamo.usage_logs.UsageLogsRepository.record`) -- tells her apart.
    """
    _seed_permissions(dynamodb_mock)
    period = _seed_tenant(monkeypatch)
    her = _PipelineUser(user_id=HER, org_id=TENANT)

    for _ in range(6):
        _send(her, header="release-9", cost_micro=2_000_000)
    _send(her, header="release-9 (staging)", cost_micro=3_000_000)
    _send(her, header="release-9 (staging)", cost_micro=4_000_000)
    _send(her, header=None, cost_micro=500_000)

    client = _me_client(HER)
    resp = client.get(f"/api/mvp/me/usage/by-tag?period={period}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    rows = {r["task_tag"]: r for r in body["rows"]}

    assert rows["release-9"]["requests"] == 6
    assert rows["release-9"]["cost_microusd"] == 12_000_000

    # The point of the journey: two grammar-dropped "release-9" calls and one
    # genuinely untagged call are ONE row, one count, one total.
    unlabelled = rows["unlabelled"]
    assert unlabelled["requests"] == 3, (
        "two grammar-dropped 'release-9' calls and one genuinely untagged "
        f"call must fold together if she cannot tell them apart: {body}"
    )
    assert unlabelled["cost_microusd"] == 500_000 + 3_000_000 + 4_000_000

    assert body["legacy_rows"] == 0, (
        "these rows all carry the task_tag pair -- 'legacy' means a row "
        "written before this feature existed, not a tag that failed grammar"
    )
    assert body["malformed_rows"] == 0, (
        "these rows all wrote both halves of the pair together -- "
        "'malformed' means exactly one of the two was ever written, not a "
        "grammar-dropped assertion"
    )
    assert set(unlabelled.keys()) == {
        "user_id", "task_tag", "requests", "cost_microusd",
        "input_tokens", "output_tokens",
    }, (
        "no field on this row breaks the fold apart; if one is ever added, "
        "this assertion is the one that should start failing"
    )
    # She would have to go find the two calls herself, off some record other
    # than this one, and already suspect her header had a space in it, to
    # ever learn two-thirds of "unlabelled" was a release she meant to track.


def test_journey_the_project_named_after_the_reserved_word_vanishes(
    monkeypatch, dynamodb_mock
):
    """A teammate names an internal cleanup project "Unlabelled" -- an
    ordinary, if ironic, choice, made with no idea that the string is the
    exact word this feature reserves for work nobody tagged
    (`mvp.task_tag.SENTINEL`, matched case-insensitively so any casing of it
    is grammar-dropped -- see the module's own docstring on why: "an
    asserted tag that canonicalises onto it is grammar-dropped rather than
    accepted, so genuinely untagged spend can never be confused with a
    caller who typed the sentinel's spelling"). She tags five calls under
    it. The request is never refused -- nothing on the wire tells her
    anything went wrong -- and her label is dropped for being the reserved
    word, exactly as if she had sent a header with a stray space in it.

    What she would be told wrongly: that her named project generated zero
    tracked spend, when it generated five requests' worth, now
    indistinguishable from every other untagged call in the tenant.

    Walks: five calls tagged "Unlabelled" (her own casing), one call tagged
    "sprint-lookup" as a control that DOES survive, one genuinely untagged
    call, then her report. Asserts her named project's total is nowhere
    under its own name and lands, unannounced, in the sentinel bucket with
    everything else.
    """
    _seed_permissions(dynamodb_mock)
    period = _seed_tenant(monkeypatch)
    her = _PipelineUser(user_id=HER, org_id=TENANT)

    for _ in range(5):
        _send(her, header="Unlabelled", cost_micro=1_000_000)
    _send(her, header="sprint-lookup", cost_micro=7_000_000)
    _send(her, header=None, cost_micro=500_000)

    client = _me_client(HER)
    resp = client.get(f"/api/mvp/me/usage/by-tag?period={period}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    rows = {r["task_tag"]: r for r in body["rows"]}

    assert rows["sprint-lookup"]["cost_microusd"] == 7_000_000
    assert len(rows) == 2, (
        f"a row keyed on her project's own spelling must not exist: {body}"
    )
    unlabelled = rows["unlabelled"]
    assert unlabelled["requests"] == 6, (
        "her five 'Unlabelled'-tagged calls and her one genuinely untagged "
        f"call are indistinguishable in the one place she would look: {body}"
    )
    assert unlabelled["cost_microusd"] == 5_000_000 + 500_000
