"""L8 (docs/design/limits.md (C14)): `MAX_POOL_BUDGET_USD_CENTS` and the new
seat figure are validated together so `seats x seat` cannot exceed the pool
maximum silently.

Spec, from the Interface + In-scope table (Interface names the two knobs; the
In-scope row is the only place a check is described):

    L8 Why: "A 500-seat tenant at $200 is $100,000; if that is above the
    accepted maximum the creation path must refuse loudly, not clamp"
    L8 Verified by: "Unit: a tenant whose seat count would exceed the maximum
    is refused with a named error"

`STRATOCLAVE_SEAT_MONTHLY_USD` is an operator-configured figure (default
200); this test drives it to a value that alone — at the 1-seat count every
fresh tenant starts with — already exceeds `MAX_POOL_BUDGET_USD_CENTS`
($10,000,000, `backend/limits.py`), so the refusal must fire at tenant
creation itself, the smallest reproduction of "seats x seat exceeds the
maximum" the interface names.

Today `mvp.admin_tenants.create_tenant` reads no seat figure and writes no
pool row at all, so this override has no effect: creation succeeds (201) with
no oversized pool ever written. The assertion below fails today for that
reason — the endpoint neither refuses nor writes anything to validate against.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from limits import MAX_POOL_BUDGET_USD_CENTS
from mvp.deps import AuthenticatedUser, get_current_user


def _admin_actor() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="admin-1", email="admin@example", org_id="default-org",
        roles=["admin"], raw_claims={}, auth_kind="cognito",
    )


def _client(monkeypatch) -> TestClient:
    from mvp import authz
    from mvp.admin_tenants import router

    monkeypatch.setattr(authz, "user_has_permission", lambda user, scope: True)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = _admin_actor
    return TestClient(app)


def test_oversized_seat_figure_refuses_tenant_creation_loudly(monkeypatch, dynamodb_mock):
    # $200,000,000/seat: 1 seat alone is 20x MAX_POOL_BUDGET_USD_CENTS ($10M).
    oversized_seat_usd = (MAX_POOL_BUDGET_USD_CENTS // 100) * 20
    monkeypatch.setenv("STRATOCLAVE_SEAT_MONTHLY_USD", str(oversized_seat_usd))

    client = _client(monkeypatch)
    resp = client.post(
        "/api/mvp/admin/tenants",
        json={"name": "Too Big Co", "team_lead_user_id": "admin-owned"},
    )

    assert resp.status_code not in (200, 201), (
        f"tenant creation succeeded with a 1-seat pool of "
        f"${oversized_seat_usd}, above the ${MAX_POOL_BUDGET_USD_CENTS // 100} "
        f"maximum — it must refuse loudly, not clamp or silently create it"
    )
    # A named error, not a bare 500: the interface calls for a refusal the
    # caller can act on, not an unhandled exception surfacing as a generic
    # server error.
    assert resp.status_code < 500, (
        f"refused with an unhandled 5xx ({resp.status_code}) rather than a "
        "named validation error"
    )


# NOTE (no test, deliberately): a second test asserting "no oversized pool
# row exists after the refused creation" was considered and dropped. Today
# `create_tenant` writes NO pool row at all (L3 has not landed either), so
# `TenantBudgetsRepository().pool_summary()` is `None` and any such assertion
# would be skipped/vacuously true — it would pass today for having no pool
# mechanism at all, not for refusing loudly. It is subsumed by
# test_oversized_seat_figure_refuses_tenant_creation_loudly above once L3 and
# L8 both land: that test's non-2xx/non-2xx-without-a-body-clamp assertion is
# the real evidence; a value-level check on the row only makes sense to add
# once there is a row to check.
