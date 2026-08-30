"""Tests for the Layer 5-d rating read surface (mvp.billing_read).

Focus (per Fable design E): the risk here is LEAKAGE (redaction) and access
(cross-tenant), not arithmetic. So:
  - P1: a tenant response NEVER contains provider_cost / margin (recursively).
  - cross-tenant / unknown run → 404 (no existence oracle).
  - admin response DOES carry provider_cost / margin.
  - golden fixtures (tenant.json + admin.json) are emitted for the 3-layer
    contract-drift gate (CLI + UI parse the SAME files).
"""
from __future__ import annotations

import json
import pathlib
import re
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.deps import AuthenticatedUser, get_current_user

# Golden fixtures shared across backend / CLI / UI (the contract-drift gate).
FIXTURE_DIR = Path(__file__).resolve().parents[2] / "contracts" / "billing"

TENANT = "acme-billing"
RUN_ID = "run-abc123"
COST_VERSION = "v-billing-cost"


def _user(roles, org=TENANT) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="u-bill", email="u@example", org_id=org, roles=roles,
        raw_claims={}, auth_kind="cognito",
    )


def _patch_authz(monkeypatch, allow: set[str]) -> None:
    from mvp import authz

    monkeypatch.setattr(authz, "user_has_permission", lambda user, scope: scope in allow)


def _app(monkeypatch, allow: set[str], user: AuthenticatedUser) -> TestClient:
    _patch_authz(monkeypatch, allow=allow)
    from mvp.billing_read import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _seed_run_with_cost(tenant=TENANT, run_id=RUN_ID):
    """Drive a real reserve+settle on a cost-bearing pricing version so the run's
    SETTLE terminal carries a frozen rating WITH provider_cost/margin."""
    from dynamo.pricing_config import PricingConfigRepository
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
    from dynamo.user_tenants import UserTenantsRepository
    from mvp import pricing
    from mvp._pipeline import reserve_credit, settle_reservation_and_log

    period = current_period()
    PricingConfigRepository().set_rates(
        version=COST_VERSION,
        rates={"opus": pricing.Rate(5_000_000, 25_000_000, 0, 0)},
        costs={"opus": pricing.Rate(2_000_000, 10_000_000, 0, 0)},
    )
    pricing.reset_cache()
    pricing.reset_version_cache()

    class _U:
        user_id = "worker"
        org_id = tenant
        email = "w@example.com"
        roles = ("user",)

    UserTenantsRepository().ensure(
        user_id="worker", tenant_id=tenant, role="user", total_credit=10**12
    )
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=tenant, period=period, pool_limit_microusd=10**12
    )
    ctx = reserve_credit(_U(), 4000, pricing_key="opus", cost_microusd=2_000_000)
    # settle with an explicit run_id so events_for_run(run_id) finds it.
    settle_reservation_and_log(
        user=_U(), tenants_repo=ctx, reservation=ctx.reservation_tokens,
        actual_input_tokens=1_000_000, actual_output_tokens=1_000_000,
        model_id="us.anthropic.claude-opus-4-7", context=ctx,
        actual_cost_microusd=None,
    )
    # The settle used the hold_id as the run_id fallback (no real run header on
    # this internal path), so query by that.
    return ctx.hold_id


def _keys_recursive(obj):
    """Every dict key anywhere in a nested JSON structure."""
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            out |= _keys_recursive(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _keys_recursive(v)
    return out


FORBIDDEN_TENANT_KEYS = {
    "provider_cost_microusd", "margin_microusd",
    "total_provider_cost_microusd", "total_margin_microusd",
}


def test_tenant_response_never_contains_cost_or_margin(dynamodb_mock, monkeypatch):
    """P1: the redaction is by TYPE — no cost/margin key appears anywhere in a
    tenant response, recursively."""
    run_id = _seed_run_with_cost()
    client = _app(monkeypatch, allow={"usage:read-self"}, user=_user(["user"]))
    resp = client.get(f"/api/mvp/me/billing/runs/{run_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    leaked = _keys_recursive(body) & FORBIDDEN_TENANT_KEYS
    assert not leaked, f"tenant response leaked cost/margin keys: {leaked}"
    # But the charge IS present and correct.
    assert body["total_settled_microusd"] == 30_000_000
    assert body["events"][0]["settled_microusd"] == 30_000_000


def test_admin_response_carries_cost_and_margin(dynamodb_mock, monkeypatch):
    run_id = _seed_run_with_cost()
    client = _app(monkeypatch, allow={"usage:read-all"}, user=_user(["admin"]))
    resp = client.get(f"/api/mvp/admin/billing/runs/{run_id}?tenant_id={TENANT}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_provider_cost_microusd"] == 12_000_000
    assert body["total_margin_microusd"] == 18_000_000
    assert body["events"][0]["provider_cost_microusd"] == 12_000_000
    assert body["events"][0]["margin_microusd"] == 18_000_000


def test_partial_cost_run_reports_null_total_margin(dynamodb_mock, monkeypatch):
    """M4 (Fable L5-d review): a run whose events don't all carry a provider cost
    must NOT report a run-total margin (it would treat unknown-cost events as
    free and overstate margin). Per-event cost still shows; the TOTAL is null."""
    import json as _json

    from dynamo import CreditLedgerRepository
    from dynamo.tenant_budgets import current_period

    run_id = _seed_run_with_cost()  # one SETTLE terminal WITH cost
    period = current_period()
    # Inject a second terminal for the SAME run (via gsi) WITHOUT provider cost.
    led = CreditLedgerRepository()
    led._table.put_item(Item={
        "pk": f"TENANT#{TENANT}#P#{period}",
        "sk": "EV#HOLD#nocost#TERMINAL",
        "event_type": "SETTLE", "hold_id": "nocost",
        "reserved_delta_microusd": 0, "settled_delta_microusd": 1_000_000,
        "gsi1pk": f"TENANT#{TENANT}#RUN#{run_id}", "gsi1sk": "9999999999999#x",
        "rating": _json.dumps({
            "pricing_version": "v", "pricing_key": "opus", "rounding": "ceil",
            "components": {"input": {"tokens": 1_000_000, "rate_microusd_per_mtok": 1_000_000, "cost_microusd": 1_000_000}},
            "total_cost_microusd": 1_000_000,  # no provider_cost/margin
        }),
    })
    client = _app(monkeypatch, allow={"usage:read-all"}, user=_user(["admin"]))
    body = client.get(f"/api/mvp/admin/billing/runs/{run_id}?tenant_id={TENANT}").json()
    # Two events, total settled includes both.
    assert body["total_settled_microusd"] == 31_000_000
    # Mixed coverage → totals are null (not an overstated margin).
    assert body["total_provider_cost_microusd"] is None
    assert body["total_margin_microusd"] is None


def test_legacy_terminal_without_rating_still_counts_settled(dynamodb_mock, monkeypatch):
    """M-1 (Fable L5-d review-2): a pre-Layer-5 SETTLE terminal has no `rating`
    attribute. It must NOT be dropped from the run total (fail-open on money);
    it appears as a `missing_rating` line whose settled is still counted."""
    import json as _json

    from dynamo import CreditLedgerRepository
    from dynamo.tenant_budgets import current_period

    run_id = "legacy-run-xyz"
    period = current_period()
    led = CreditLedgerRepository()
    # A legacy SETTLE terminal: settled recorded, NO rating attribute.
    led._table.put_item(Item={
        "pk": f"TENANT#{TENANT}#P#{period}",
        "sk": "EV#HOLD#legacy1#TERMINAL",
        "event_type": "SETTLE", "hold_id": "legacy1",
        "reserved_delta_microusd": -1, "settled_delta_microusd": 7_000_000,
        "gsi1pk": f"TENANT#{TENANT}#RUN#{run_id}", "gsi1sk": "1#a",
    })
    client = _app(monkeypatch, allow={"usage:read-self"}, user=_user(["user"]))
    resp = client.get(f"/api/mvp/me/billing/runs/{run_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_settled_microusd"] == 7_000_000  # NOT dropped
    assert body["events"][0]["settle_reason"] == "missing_rating"
    # Still redacted.
    assert not (_keys_recursive(body) & FORBIDDEN_TENANT_KEYS)


def test_cross_tenant_run_is_404_not_403(dynamodb_mock, monkeypatch):
    """A caller from another tenant gets 404 (no existence oracle), because the
    me endpoint pins tenant_id from the auth context."""
    run_id = _seed_run_with_cost()
    other = _app(monkeypatch, allow={"usage:read-self"}, user=_user(["user"], org="other-tenant"))
    resp = other.get(f"/api/mvp/me/billing/runs/{run_id}")
    assert resp.status_code == 404


def test_unknown_run_is_404(dynamodb_mock, monkeypatch):
    _seed_run_with_cost()
    client = _app(monkeypatch, allow={"usage:read-self"}, user=_user(["user"]))
    resp = client.get("/api/mvp/me/billing/runs/does-not-exist")
    assert resp.status_code == 404


def test_me_billing_requires_usage_read_self(dynamodb_mock, monkeypatch):
    run_id = _seed_run_with_cost()
    client = _app(monkeypatch, allow=set(), user=_user(["user"]))
    resp = client.get(f"/api/mvp/me/billing/runs/{run_id}")
    assert resp.status_code == 403


def _norm(b):
    """Normalize volatile fields (ts_ms, run_id) so the golden files are stable
    across runs — the CONTRACT (shape + redaction) is what is pinned."""
    b = json.loads(json.dumps(b))  # deep copy
    b["run_id"] = "RUN"
    for e in b["events"]:
        e["ts_ms"] = 0
    return b


def test_the_rust_reader_accepts_every_field_the_fixture_carries():
    """The other half of the cross-layer gate, from this side.

    The committed fixtures are parsed by a Rust struct that uses
    `deny_unknown_fields`, so ADDING a field to the API response and regenerating
    the fixtures breaks the CLI build — and the failure surfaces in another
    language's test suite, after the fixture is already committed. That is exactly
    what happened when `reported` was added to a rating component.

    So the field sets are compared here, where the change is made. The Rust struct
    is read as text rather than compiled: this test must run in a Python-only
    environment, and what it needs to know is which names the reader accepts.
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    rust = (root / "cli" / "src" / "mvp" / "billing.rs").read_text()
    block = rust.split("pub struct RatingComponent {", 1)[1].split("}", 1)[0]
    accepted = set(re.findall(r"pub (\w+):", block))

    in_fixtures: set[str] = set()
    for name in ("run_tenant.json", "run_admin.json"):
        body = json.loads((FIXTURE_DIR / name).read_text())
        for event in body.get("events", []):
            for component in (event.get("components") or {}).values():
                in_fixtures |= set(component)

    assert in_fixtures, "fixtures carry no rating components to check"
    missing = in_fixtures - accepted
    assert not missing, (
        f"the Rust CLI's RatingComponent does not accept {sorted(missing)}; it uses "
        "deny_unknown_fields, so the committed fixture would fail to deserialize. "
        "Add the field to cli/src/mvp/billing.rs in the same change."
    )


def test_golden_fixtures_match_committed_contract(dynamodb_mock, monkeypatch):
    """The cross-layer contract-drift gate (backend half). The current API
    response must EQUAL the committed golden fixtures (contracts/billing/*.json)
    that the Rust CLI and React UI parse. If the API shape changes, this test
    FAILS (it does not silently rewrite the fixtures) — forcing an explicit,
    reviewed fixture update that then re-triggers the CLI + UI fixture tests.

    Set REGEN_BILLING_FIXTURES=1 to regenerate after an intended contract change.
    """
    run_id = _seed_run_with_cost()
    tenant_client = _app(monkeypatch, allow={"usage:read-self"}, user=_user(["user"]))
    tenant_body = _norm(tenant_client.get(f"/api/mvp/me/billing/runs/{run_id}").json())
    admin_client = _app(monkeypatch, allow={"usage:read-all"}, user=_user(["admin"]))
    admin_body = _norm(
        admin_client.get(f"/api/mvp/admin/billing/runs/{run_id}?tenant_id={TENANT}").json()
    )

    # Redaction sanity regardless of fixture state.
    assert not (_keys_recursive(tenant_body) & FORBIDDEN_TENANT_KEYS)
    assert "total_provider_cost_microusd" in admin_body

    pairs = [("run_tenant.json", tenant_body), ("run_admin.json", admin_body)]
    if os.getenv("REGEN_BILLING_FIXTURES") == "1":
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        for name, body in pairs:
            (FIXTURE_DIR / name).write_text(
                json.dumps(body, indent=2, sort_keys=True) + "\n"
            )
        return
    # Assert the committed fixtures still match the live contract.
    for name, body in pairs:
        path = FIXTURE_DIR / name
        assert path.exists(), (
            f"missing golden fixture {path}; run with REGEN_BILLING_FIXTURES=1"
        )
        committed = json.loads(path.read_text())
        assert committed == body, (
            f"{name} drifted from the API contract — review and regenerate with "
            f"REGEN_BILLING_FIXTURES=1 (this also breaks the CLI/UI fixture tests)"
        )


def test_the_three_misses_are_byte_identical(dynamodb_mock, monkeypatch):
    """The no-existence-oracle invariant, pinned.

    An unknown run, another tenant's run, and a run that exists but has no
    dollar charge lines must be indistinguishable — same status AND same body.
    The 404 detail was made explanatory in 2026-08-27 after a live verification
    spent half an hour deciding whether a bare "run not found" meant a wrong id
    or a tenant without a dollar pool; this test is what stops a future author
    from making the wording branch per case.
    """
    run_id = _seed_run_with_cost()

    mine = _app(monkeypatch, allow={"usage:read-self"}, user=_user(["user"]))
    other = _app(monkeypatch, allow={"usage:read-self"},
                 user=_user(["user"], org="other-tenant"))

    unknown = mine.get("/api/mvp/me/billing/runs/does-not-exist")
    cross = other.get(f"/api/mvp/me/billing/runs/{run_id}")
    # A run id that exists nowhere is the closest reachable stand-in for
    # "exists but has no rated events": both take the `full is None` branch.
    no_lines = mine.get("/api/mvp/me/billing/runs/run-with-no-rated-events")

    assert unknown.status_code == cross.status_code == no_lines.status_code == 404
    assert unknown.text == cross.text == no_lines.text
    # And it still explains itself rather than saying only "run not found".
    assert "dollar" in unknown.json()["detail"]
