"""Tests for the per-tenant SR mode resolution (mvp.sr.adapter) and its parse
through the routing config (SR migration stage 2 groundwork).

Money is fail-closed in every mode; these tests only pin the routing-mode
resolution (four-state + kill-switch + global default + fail-open) and the config
round-trip. The SR HTTP consult itself is a fail-open no-op until a later
sub-step, so `decide()` returns NO_DECISION here regardless of mode.
"""
from __future__ import annotations

import pytest

from mvp.sr import adapter as sr
from mvp.sr.port import NO_DECISION, RouteDecision, SwitchCostHint


# --------------------------------------------------------------- kill-switch
def test_force_off_outranks_everything(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_SR_FORCE_OFF", "true")
    monkeypatch.setenv("STRATOCLAVE_SR_MODE_DEFAULT", "decide_route")
    assert sr.sr_globally_forced_off() is True
    # even with a permissive global default, force-off wins.
    assert sr._global_sr_mode_default() == "decide_route"
    # (force-off is applied in sr_mode_for, which needs a tenant config read;
    #  the unit-level guarantee here is that the kill-switch flag is honored.)


def test_force_off_spellings(monkeypatch):
    for val in ("true", "1", "yes", "on", "TRUE", "On"):
        monkeypatch.setenv("STRATOCLAVE_SR_FORCE_OFF", val)
        assert sr.sr_globally_forced_off() is True, val
    for val in ("false", "0", "no", "off", ""):
        monkeypatch.setenv("STRATOCLAVE_SR_FORCE_OFF", val)
        assert sr.sr_globally_forced_off() is False, val


# --------------------------------------------------------------- global default
def test_global_default_dark_by_default(monkeypatch):
    monkeypatch.delenv("STRATOCLAVE_SR_MODE_DEFAULT", raising=False)
    assert sr._global_sr_mode_default() == "off"


def test_global_default_garbage_falls_back_to_off(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_SR_MODE_DEFAULT", "nonsense")
    assert sr._global_sr_mode_default() == "off"


def test_global_default_valid_modes(monkeypatch):
    for m in ("off", "shadow", "route", "decide_route"):
        monkeypatch.setenv("STRATOCLAVE_SR_MODE_DEFAULT", m)
        assert sr._global_sr_mode_default() == m


# --------------------------------------------------------------- per-tenant resolve
def test_mode_for_forced_off(monkeypatch, dynamodb_mock):
    monkeypatch.setenv("STRATOCLAVE_SR_FORCE_OFF", "true")
    assert sr.sr_mode_for("acme") == "off"
    assert sr.sr_active_for("acme") is False


def test_mode_for_follows_global_when_tenant_none(monkeypatch, dynamodb_mock):
    monkeypatch.delenv("STRATOCLAVE_SR_FORCE_OFF", raising=False)
    monkeypatch.setenv("STRATOCLAVE_SR_MODE_DEFAULT", "route")
    from mvp.routing import config as rc
    rc._cache.clear()
    # a tenant with no routing-config item resolves sr_mode=None -> global default.
    assert sr.sr_mode_for("never-touched") == "route"


def test_mode_for_tenant_explicit_wins(monkeypatch, dynamodb_mock):
    monkeypatch.delenv("STRATOCLAVE_SR_FORCE_OFF", raising=False)
    monkeypatch.setenv("STRATOCLAVE_SR_MODE_DEFAULT", "off")
    from mvp.routing import config as rc
    rc._cache.clear()
    from mvp import admin_routing as ar
    ar._table().put_item(Item=ar.tenant_config_to_item(
        "acme", ar.TenantRoutingConfigRequest(), updated_by="op") | {"sr_mode": "decide_route"})
    rc.invalidate_routing_cache("acme")
    assert sr.sr_mode_for("acme") == "decide_route"
    assert sr.sr_active_for("acme") is True


def test_mode_for_failopen_on_config_error(monkeypatch):
    # Force the routing-config read to raise; sr_mode_for must swallow it and
    # resolve to the global default (fail-open — never break the request).
    monkeypatch.delenv("STRATOCLAVE_SR_FORCE_OFF", raising=False)
    monkeypatch.setenv("STRATOCLAVE_SR_MODE_DEFAULT", "route")
    from mvp.routing import config as rc

    def _boom(_tenant):
        raise RuntimeError("dynamodb unreachable")
    monkeypatch.setattr(rc, "get_tenant_routing_config", _boom)
    assert sr.sr_mode_for("acme") == "route"


# --------------------------------------------------------------- decide no-op
def test_decide_is_failopen_noop_for_now(monkeypatch, dynamodb_mock):
    monkeypatch.setenv("STRATOCLAVE_SR_MODE_DEFAULT", "decide_route")
    from mvp.routing import config as rc
    rc._cache.clear()
    d = sr.decide(tenant_id="acme", session_key="s1",
                  requested_model="claude-opus-4-7", has_tool_result=False)
    assert d is NO_DECISION
    assert d.acts is False


def test_decide_off_tenant_returns_no_decision(monkeypatch, dynamodb_mock):
    monkeypatch.setenv("STRATOCLAVE_SR_FORCE_OFF", "true")
    d = sr.decide(tenant_id="acme", session_key=None,
                  requested_model="claude-opus-4-7", has_tool_result=True)
    assert d is NO_DECISION


# --------------------------------------------------------------- port extension
def test_route_decision_candidate_pool_defaults_empty():
    assert RouteDecision().candidate_pool == ()
    d = RouteDecision(hard_model="a", candidate_pool=("a", "b"), origin="semantic-router")
    assert d.candidate_pool == ("a", "b")
    assert d.acts is True
