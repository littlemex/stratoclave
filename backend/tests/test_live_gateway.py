"""Opt-in driver for the GATEWAY-PATH live verification (scenarios/usage/small-team
/live_gateway.py). Drives /v1/messages through the in-process gateway to REAL
Bedrock, with the moto ledger, and writes results/live-gateway-<run_id>.json.

    SC_GW_LIVE=1 AWS_REGION=us-east-1 [AWS_LIVE_PROFILE=claude-code] \
        python -m pytest backend/tests/test_live_gateway.py -s -q

NEVER runs in CI (skipped unless SC_GW_LIVE=1). Costs real (tiny) money, bounded by
the $1 hard cap in live_gateway.py.
"""
from __future__ import annotations

import importlib.util
import os
import time as _t
from dataclasses import dataclass
from pathlib import Path

import pytest

if os.getenv("SC_GW_LIVE") != "1":
    pytest.skip("gateway-live driver: set SC_GW_LIVE=1", allow_module_level=True)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.anthropic import router as anthropic_router
from mvp.authz import _PERMS_CACHE
from mvp.deps import get_current_user

_SCEN = Path(__file__).resolve().parents[2] / "scenarios" / "usage" / "small-team"


def _load_live_gateway():
    spec = importlib.util.spec_from_file_location(
        "live_gateway", _SCEN / "live_gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@dataclass
class _FakeUser:
    user_id: str = "user-22222222-2222-2222-2222-222222222222"
    org_id: str = "acme-eng"
    email: str = "workshop@example.com"
    roles: list = None
    auth_kind: str = "jwt"
    key_scopes: list = None

    def __post_init__(self):
        if self.roles is None:
            self.roles = ["user", "admin"]


@pytest.fixture
def real_bedrock_before_moto():
    """Real bedrock-runtime client built BEFORE moto starts (fixture order) and
    from an explicit profile, so moto's global botocore patch and conftest's dummy
    env creds do not capture it."""
    import boto3 as _b3
    profile = os.getenv("AWS_LIVE_PROFILE", "claude-code")
    session = _b3.Session(profile_name=profile, region_name="us-east-1")
    return session.client("bedrock-runtime", region_name="us-east-1")


def test_run_gateway_verification(real_bedrock_before_moto, dynamodb_mock,
                                  seed_tenant_with_pool, monkeypatch):
    _PERMS_CACHE["user"] = (["messages:send", "usage:read-self"], _t.time() + 3600)
    _PERMS_CACHE["admin"] = (["messages:send", "usage:read-self"], _t.time() + 3600)
    # Non-streaming converse() resolves via mvp.anthropic._bedrock_client; the
    # STREAMING path routes through mvp.routing.infrarouter -> clients.bedrock_client
    # (region-arg). Patch BOTH so real Bedrock is used on both paths (else moto
    # answers converse_stream with "not implemented").
    monkeypatch.setattr("mvp.anthropic._bedrock_client",
                        lambda: real_bedrock_before_moto)
    monkeypatch.setattr("mvp.routing.infrarouter.bedrock_client",
                        lambda region=None: real_bedrock_before_moto)

    app = FastAPI()
    app.include_router(anthropic_router)
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()
    client = TestClient(app)

    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
    repo = TenantBudgetsRepository()
    period = current_period()

    def pool_summary():
        return repo.pool_summary(tenant_id="acme-eng", period=period) or {}

    lg = _load_live_gateway()
    now_iso = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
    report = lg.run_gateway_verification(
        client=client, bedrock=real_bedrock_before_moto,
        pool_summary=pool_summary, run_id="gw1", now_iso=now_iso, region="us-east-1")

    print("\n" + lg.format_report(report))

    out_dir = _SCEN / "results"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / "live-gateway-gw1.json"
    import json
    out_file.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(f"\nresults -> {out_file}")

    # sanity: the gateway path actually charged the ledger and answered
    assert report["cost"]["charge_of_record_microusd"] > 0, "ledger settled nothing"
    assert report["quality"]["graded"] == 10
    assert report["provenance"]["gateway_in_path"] is True
