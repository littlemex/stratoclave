"""Tests for configurable cross-region failover (data-residency control).

STRATOCLAVE_FAILOVER_REGIONS makes the streaming failover region set
operator-controlled instead of the hardcoded us-west-2 + eu-west-1. The
residency-critical property: an EMPTY value disables failover entirely
(single-region — no prompt bytes leave the primary region).
"""
from __future__ import annotations

import pytest

from mvp.routing import chains
from mvp.routing.chains import failover_regions, get_catalog, reset_catalog


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Pin a known primary and clear the knob before each test; rebuild the
    # memoized catalog so env changes take effect.
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv("BEDROCK_REGION", raising=False)
    monkeypatch.delenv("STRATOCLAVE_FAILOVER_REGIONS", raising=False)
    reset_catalog()
    yield
    reset_catalog()


def test_default_filtered_to_primary_jurisdiction(monkeypatch):
    # Primary is us-east-1 (fixture). The built-in defaults are
    # (us-west-2, eu-west-1); eu-west-1 is a DIFFERENT jurisdiction than the
    # us-* primary, so the unset-var default now keeps only us-west-2. This is
    # the residency fix: a non-US primary must never inherit a US failover.
    assert failover_regions() == ["us-west-2"]


def test_default_eu_primary_never_leaks_to_us(monkeypatch):
    # THE headline residency bug (Fable): BEDROCK_REGION=eu-west-1 with the var
    # UNSET must NOT inherit us-west-2 from the built-in defaults. eu-west-1 is
    # also the primary (stripped), so the default set collapses to empty.
    monkeypatch.setenv("BEDROCK_REGION", "eu-west-1")
    assert failover_regions() == []
    reset_catalog()
    catalog = get_catalog()
    assert catalog, "catalog empty — residency assertion would be vacuous"
    all_regions = {t.region for ts in catalog.values() for t in ts}
    assert all_regions == {"eu-west-1"}, all_regions
    assert not any(r.startswith("us-") for r in all_regions), all_regions


def test_default_apac_primary_no_cross_jurisdiction(monkeypatch):
    # An APAC primary with the var unset gets no default failover (neither
    # built-in default is ap-*), so it stays single-region rather than leaking.
    monkeypatch.setenv("BEDROCK_REGION", "ap-northeast-1")
    assert failover_regions() == []


def test_explicit_list_is_honored_verbatim_across_jurisdictions(monkeypatch):
    # An EXPLICIT list is the operator's stated intent and is NOT
    # jurisdiction-filtered (the CDK STRATOCLAVE_RESIDENCY check flags a
    # cross-jurisdiction explicit list separately). EU primary + explicit
    # us-west-2 keeps us-west-2.
    monkeypatch.setenv("BEDROCK_REGION", "eu-west-1")
    monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", "us-west-2,eu-central-1")
    assert failover_regions() == ["us-west-2", "eu-central-1"]


def test_empty_disables_failover(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", "")
    assert failover_regions() == []
    # And the catalog is then single-region for every alias.
    reset_catalog()
    catalog = get_catalog()
    assert catalog, "catalog empty — residency assertion would be vacuous"
    for targets in catalog.values():
        regions = {t.region for t in targets}
        assert regions == {"us-east-1"}, regions


@pytest.mark.parametrize("sentinel", ["none", "disabled", "off", "  Disabled  ", "OFF"])
def test_disable_sentinels(monkeypatch, sentinel):
    # An explicit sentinel disables failover, surviving orchestration that would
    # strip an empty env var (Fable review #1).
    monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", sentinel)
    assert failover_regions() == []


@pytest.mark.parametrize("raw", [",", " , ", ",,", " ,, "])
def test_comma_only_yields_empty_no_default_fallback(monkeypatch, raw):
    # A comma-only value must parse to [] (single-region), NOT fall back to the
    # default us-west-2+eu-west-1 set. The IaC residency analysis relies on this
    # exact behavior (Fable review NEW-12) — a fallback here would be a silent
    # cross-region leak for an operator who thought they disabled failover.
    monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", raw)
    assert failover_regions() == []


def test_custom_list_parsed_trimmed_and_ordered(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", " us-west-2 , us-east-2 ")
    assert failover_regions() == ["us-west-2", "us-east-2"]


def test_primary_region_stripped_from_alts(monkeypatch):
    # The primary must never appear as a failover target (it is always target 0).
    monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", "us-east-1,us-west-2")
    assert failover_regions() == ["us-west-2"]


def test_duplicates_deduped_order_preserved(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", "us-west-2,eu-west-1,us-west-2")
    assert failover_regions() == ["us-west-2", "eu-west-1"]


def test_eu_can_be_excluded_for_residency(monkeypatch):
    # The headline residency use case: keep failover but never touch the EU.
    monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", "us-west-2,us-east-2")
    reset_catalog()
    all_regions = {t.region for ts in get_catalog().values() for t in ts}
    assert "eu-west-1" not in all_regions
    assert all_regions == {"us-east-1", "us-west-2", "us-east-2"}


def test_catalog_regions_follow_config(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", "eu-central-1")
    reset_catalog()
    catalog = get_catalog()
    assert catalog, "catalog empty — assertion would be vacuous"
    for targets in catalog.values():
        assert targets[0].region == "us-east-1"  # primary always first
        assert {t.region for t in targets} == {"us-east-1", "eu-central-1"}


def test_unregistered_alias_fallback_also_honors_config(monkeypatch):
    # The resolve_chain fallback path (alias present-but-empty in catalog) must
    # use the SAME residency setting, not a hardcoded us-west-2. Force the branch
    # by emptying the catalog for a resolvable alias.
    from mvp.routing.chains import resolve_chain

    monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", "")
    monkeypatch.setattr(chains, "_CATALOG", {})
    monkeypatch.setattr(chains, "get_catalog", lambda: {})  # force the fallback
    chain = resolve_chain("claude-opus-4-7")
    regions = {t.region for t in chain.targets}
    assert regions == {"us-east-1"}, regions  # single-region, no hardcoded us-west-2

    # And with a custom list, the fallback follows it.
    monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", "us-west-2")
    chain2 = resolve_chain("claude-opus-4-7")
    assert {t.region for t in chain2.targets} == {"us-east-1", "us-west-2"}
