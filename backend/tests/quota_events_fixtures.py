"""Shared test-only scaffolding for the F2 quota-events table.

Not a test file (no ``test_`` prefix, not collected by pytest). Mirrors the
pattern already used by ``test_pool_membership_delta_l4.py`` and friends:
extra tables are created LOCALLY by the test file that needs them, layered on
top of the shared ``dynamodb_mock`` fixture in ``conftest.py`` — this module
exists only so eight F2 test files do not each duplicate the same
``create_table`` call and the same raw-item seeding helpers.

Table shape is exactly ``docs/design/quota-raises.md``'s Interface section, plus the
two GSIs it names:

  * base table:  pk (HASH) / sk (RANGE)
  * ``tenant-status-index``:    tenant_id (HASH) / status_created_at (RANGE)
  * ``grant-expiry-index``:     grant_status (HASH) / expires_at (RANGE) — sparse
    by construction (only items that WRITE ``grant_status`` appear in it)

U6 (the journey layer's finding, `docs/design/quota-raises.md`): the table lives in
the SHARED ``dynamodb_mock`` fixture in ``conftest.py``, not a private one —
"a private fixture inside F2's own test files would pass F2 and leave every
cross-part journey unable to seed a grant row at all." ``conftest.py`` already
creates ``stratoclave-quota-events`` (same name, same env var
``DYNAMODB_QUOTA_EVENTS_TABLE``, same two GSIs) as part of that shared
fixture, and ``dynamo/client.py::quota_events_table_name()`` resolves the same
env var. This module's ``quota_events_table`` fixture therefore does NOT
create the table a second time (moto's `CreateTable` on an existing name is a
``ResourceInUseException``, not a no-op) — it depends on ``dynamodb_mock`` for
ordering and hands back the boto3 Table resource already created by it.
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Optional

import boto3
import pytest

TABLE_NAME = "stratoclave-quota-events"
ENV_VAR = "DYNAMODB_QUOTA_EVENTS_TABLE"


@pytest.fixture
def quota_events_table(dynamodb_mock):
    """Yield the boto3 Table resource for the quota-events table that the
    SHARED `dynamodb_mock` fixture (`conftest.py`) already created — this
    fixture's only job is depending on `dynamodb_mock` for ordering and
    resolving the same table name every other repository resolves through
    `dynamo.client.quota_events_table_name()`."""
    table_name = os.environ.get(ENV_VAR, TABLE_NAME)
    yield boto3.resource("dynamodb", region_name="us-east-1").Table(table_name)


def freeze_grants_clock(monkeypatch: "pytest.MonkeyPatch", epoch_seconds: int) -> None:
    """Pin `mvp.grants`'s clock to an exact instant.

    U5 (`docs/design/quota-raises.md`): time in `mvp/grants.py` is read through ONE
    patchable module-level name (`datetime`, imported as a name rather than
    through its package) -- the same convention
    `test_sso_replay_failclosed.py::_freeze_time` already established for
    `mvp/auth/sso_sts.py`. `submit_limit_raise`/`approve_limit_raise`/
    `reject_limit_raise` take no `now_epoch` parameter (only
    `sweep_expired_grants` does, directly) -- the 300-second window and the
    daily slot's date are both computed from this one module symbol, so
    controlling it is the only way a test can drive R11's boundary without
    waiting five minutes.
    """
    import datetime as dt

    from mvp import grants

    fixed = dt.datetime.fromtimestamp(epoch_seconds, tz=dt.timezone.utc)

    class _FixedDt(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.replace(tzinfo=tz or dt.timezone.utc)

    monkeypatch.setattr(grants, "datetime", _FixedDt)


def seed_tenant(
    tenant_id: str, *, team_lead_user_id: str = "admin-owned", name: Optional[str] = None,
) -> None:
    """Seed a real `Tenants` row.

    `approve_limit_raise`'s authority `ConditionCheck` (R6/R31) evaluates
    against this row -- `attribute_exists(tenant_id)` for an admin actor,
    `team_lead_user_id = :actor` for a team-lead one -- so a grant test
    against a tenant with no row here always fails authority, whatever the
    actor. `team_lead_user_id="admin-owned"` (this repository's sentinel,
    `dynamo.tenants.ADMIN_OWNED`) is exempt from the team-lead tenant cap and
    is the right default for admin-actor tests; team-lead-authority tests
    pass their own actor's `user_id` here instead.
    """
    from dynamo.tenants import TenantsRepository

    TenantsRepository().create(
        tenant_id=tenant_id, name=name or f"Tenant {tenant_id}",
        team_lead_user_id=team_lead_user_id, created_by="test-harness",
    )


def slot_pk(user_id: str) -> str:
    return f"USER#{user_id}"


def slot_sk(tenant_id: str, date_str: str) -> str:
    return f"SLOT#{tenant_id}#{date_str}"


def request_pk(request_id: str) -> str:
    return f"REQUEST#{request_id}"


def grant_pk(tenant_id: str) -> str:
    return f"TENANT#{tenant_id}"


def grant_sk(grant_id: str) -> str:
    return f"GRANT#{grant_id}"


def seed_slot(
    table, *, user_id: str, tenant_id: str, date_str: str, client_token: str,
    request_id: str, created_at: str = "2026-09-01T00:00:00+00:00",
) -> None:
    table.put_item(Item={
        "pk": slot_pk(user_id), "sk": slot_sk(tenant_id, date_str),
        "client_token": client_token, "request_id": request_id,
        "created_at": created_at,
    })


def seed_request(
    table, *, request_id: str, tenant_id: str, user_id: str,
    asked_amount_microusd: int,
    status: str = "PENDING", client_token: str = "tok-default",
    reason_code: str = "other", comment: Optional[str] = None,
    created_at: str = "2026-09-01T00:00:00+00:00",
    decision_comment: Optional[str] = None,
    revision: int = 1,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Seed a Request row with the ACTUAL production attribute names
    (`dynamo/quota_events.py::put_request`): `asked_amount_microusd`,
    `reason_code`, `comment`, `limit_kind` -- pinned by the contract's U7
    amendment, superseding this file's original (`requested_amount_microusd`
    / `requested_expires_at`) guess at names the design note drafted before
    U7 landed. There is no `requested_expires_at` on a Request row at all: the
    expiry is the APPROVER's decision, made at approval time, never asked for
    by the requester. `client_token` also does NOT live here in production
    (it lives on the slot row only, by R13's own design) but is accepted and
    stored here anyway for tests that seed a request+slot pair together and
    want the token visible on both without a second lookup -- harmless extra
    state no production code path reads back off this row.
    """
    from mvp.grants import POOL_WALL

    item: dict[str, Any] = {
        "pk": request_pk(request_id), "sk": "REQUEST",
        "request_id": request_id, "tenant_id": tenant_id, "user_id": user_id,
        "asked_amount_microusd": Decimal(asked_amount_microusd),
        "reason_code": reason_code, "limit_kind": POOL_WALL,
        "status": status, "client_token": client_token,
        "created_at": created_at, "revision": Decimal(revision),
        "status_created_at": f"{status}#{created_at}",
    }
    if comment is not None:
        item["comment"] = comment
    if decision_comment is not None:
        item["decision_comment"] = decision_comment
    if extra:
        item.update(extra)
    table.put_item(Item=item)


def seed_grant(
    table, *, grant_id: str, tenant_id: str, request_id: str,
    approver_user_id: str, approved_amount_microusd: int,
    expires_at_epoch: int, target_pk: str, target_sk: str, period: str,
    status: str = "ACTIVE", created_at: str = "2026-09-01T00:00:00+00:00",
    revoke_attempts: int = 0, revision: int = 1,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    item: dict[str, Any] = {
        "pk": grant_pk(tenant_id), "sk": grant_sk(grant_id),
        "grant_id": grant_id, "tenant_id": tenant_id, "request_id": request_id,
        "approver_user_id": approver_user_id,
        "approved_amount_microusd": Decimal(approved_amount_microusd),
        "expires_at": Decimal(expires_at_epoch),
        "target_pk": target_pk, "target_sk": target_sk, "period": period,
        "status": status, "created_at": created_at,
        "revoke_attempts": Decimal(revoke_attempts),
        # `grant_terminal_txn_item`'s revoke SETs `revision = revision + :one`
        # (`dynamo/quota_events.py`) -- an arithmetic SET, not a plain
        # assignment, so a grant row with no `revision` attribute at all
        # fails that expression outright ("refers to an attribute that does
        # not exist in the item"), not merely leaves it unincremented.
        "revision": Decimal(revision),
        "status_created_at": f"{status}#{created_at}",
    }
    if status == "ACTIVE":
        item["grant_status"] = "ACTIVE"
    if extra:
        item.update(extra)
    table.put_item(Item=item)


def seed_pool_with_grant_fields(
    tenant_id: str, period: str, *, pool_limit_microusd: int,
    pool_granted_microusd: int = 0, grant_cap_microusd: Optional[int] = None,
) -> None:
    """Seed a `TenantBudgets` pool row at the FINAL, already-consistent state
    `pool_limit_microusd = baseline + pool_granted_microusd` (F1's identity,
    which `dynamo.tenant_budgets.baseline_microusd`/`effective_grant_cap_for_row`
    both assume holds), so a caller choosing `pool_limit_microusd=` gets a row
    where that is genuinely the CURRENT ceiling, grants included -- not a row
    whose stored `pool_limit_microusd` silently excludes the very
    `pool_granted_microusd` this function was asked to seed alongside it.

    F1's setter is `set_manual_limit` (this repository's rename of the
    contract-era `set_pool_limit`, adapted repo-wide by `adapt/quota-suite`),
    and it has no `pool_granted_microusd` parameter at all -- it is F1's own
    `baseline_microusd`/`is_seat_tracked` (manual_limit if present, else
    seat_count x rate) that `effective_grant_cap_for_row` derives the cap
    from, NOT a `pool_limit_microusd - pool_granted_microusd` subtraction
    computed fresh by this fixture (docs/design/quota-raises.md's original, F1-unaware plan
    for a same-named `TenantBudgetsRepository.baseline_microusd(tenant_id,
    period)` method -- superseded once F1 landed with the pure, row-level
    function of that name already in place). So the baseline this seeds
    THROUGH `set_manual_limit` is `pool_limit_microusd - pool_granted_microusd`
    -- the figure that, once `pool_granted_microusd` is layered on top by the
    raw overlay below, reconstructs the caller's requested
    `pool_limit_microusd` exactly, and reads back as that baseline through
    F1's own real function rather than a second, competing one."""
    from dynamo.tenant_budgets import TenantBudgetsRepository, budget_sk

    repo = TenantBudgetsRepository()
    baseline = int(pool_limit_microusd) - int(pool_granted_microusd)
    repo.set_manual_limit(
        tenant_id=tenant_id, period=period, manual_limit_microusd=baseline,
    )
    # Absolute SETs, not ADDs: idempotent under a second seed call against the
    # same row (a fresh ADD would double-count), and exact regardless of what
    # `set_manual_limit` left in `pool_headroom_microusd` (equal to the
    # baseline on a fresh row, since nothing has reserved or settled yet).
    update_expr = (
        "SET pool_granted_microusd = :g, pool_limit_microusd = :lim, "
        "pool_headroom_microusd = :lim"
    )
    values: dict[str, Any] = {
        ":g": Decimal(pool_granted_microusd), ":lim": Decimal(pool_limit_microusd),
    }
    if grant_cap_microusd is not None:
        update_expr += ", grant_cap_microusd = :c"
        values[":c"] = Decimal(grant_cap_microusd)
    repo._table.update_item(
        Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
        UpdateExpression=update_expr,
        ExpressionAttributeValues=values,
    )
