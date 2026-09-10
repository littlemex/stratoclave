"""C15 -- the two residency controls (a tenant's jurisdiction restriction, and
the deployment's cross-region failover list) must not contradict each other.

PR2 handoff: "A named callable that fails when a jurisdiction-restricted
tenant is served by a deployment whose `failover_regions()` leaves that
jurisdiction. Name it `mvp.registry_checks.check_tenant_jurisdiction_against_
failover(...)` and give it the same contract as PR1's checks: returns `None`,
raises `ValueError` naming the tenant, the jurisdiction and the offending
regions."

The handoff named the callable, not its signature (a literal ellipsis stood
in for the parameters). The contract has since specified it exactly:

    check_tenant_jurisdiction_against_failover(*, tenant_id, jurisdiction,
                                                failover_regions=None)

keyword-only, with `failover_regions=None` resolving to the real
`mvp.routing.chains.failover_regions()` -- the same "None means read the real
source" convention PR1's checks use for `entries=None`. Both driving styles
this file uses are therefore legitimate and kept:

  - omitting `failover_regions` and driving the real source through
    `BEDROCK_REGION` / `STRATOCLAVE_FAILOVER_REGIONS` -- the only way to
    prove the default genuinely reads the real dependency rather than a
    hardcoded stand-in;
  - passing `failover_regions=[...]` explicitly for a pure case that wants a
    specific list without touching the environment, which also pins that the
    parameter is honoured when supplied rather than silently ignored.
"""
from __future__ import annotations

import re

import pytest


@pytest.fixture
def check_tenant_jurisdiction_against_failover():
    # Imported lazily (inside a fixture, not at module top level) so that
    # this callable not existing yet fails only the tests in this file, one
    # at a time -- an unguarded top-level import would turn a single missing
    # name into a pytest "error during collection" that, by default (no
    # --continue-on-collection-errors in this repo's pytest config), aborts
    # collection of every OTHER test file in the same invocation too.
    from mvp.registry_checks import check_tenant_jurisdiction_against_failover as fn
    return fn


@pytest.fixture(autouse=True)
def _clean_failover_env(monkeypatch):
    # Start every test from a known primary region with the override var
    # UNSET, so each test states its own intent explicitly instead of
    # inheriting whatever a previous test (or conftest's module-level
    # AWS_REGION="us-east-1") left behind.
    monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
    monkeypatch.delenv("STRATOCLAVE_FAILOVER_REGIONS", raising=False)


def _word(token: str, text: str) -> bool:
    """Whole-word containment -- "eu" must not match inside "requirement"."""
    return re.search(rf"\b{re.escape(token)}\b", text) is not None


def test_same_jurisdiction_explicit_failover_is_accepted(monkeypatch, check_tenant_jurisdiction_against_failover):
    monkeypatch.setenv("BEDROCK_REGION", "eu-west-1")
    monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", "eu-west-2")
    # Must not raise. failover_regions omitted -> reads the real source,
    # driven through the env vars above.
    assert check_tenant_jurisdiction_against_failover(
        tenant_id="tenant-eu", jurisdiction="eu") is None


def test_cross_jurisdiction_explicit_failover_is_refused(monkeypatch, check_tenant_jurisdiction_against_failover):
    """The headline case: an operator has explicitly pointed failover at a
    different jurisdiction than the tenant is restricted to. Non-vacuous
    companion to the accept-case above -- same call shape, only the env
    differs, and the two tests disagree on the outcome."""
    monkeypatch.setenv("BEDROCK_REGION", "eu-west-1")
    monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", "us-east-1")

    with pytest.raises(ValueError) as ei:
        check_tenant_jurisdiction_against_failover(tenant_id="tenant-eu", jurisdiction="eu")

    message = str(ei.value)
    assert "tenant-eu" in message, f"message does not name the tenant: {message!r}"
    assert _word("eu", message), f"message does not name the jurisdiction: {message!r}"
    assert "us-east-1" in message, f"message does not name the offending region: {message!r}"


def test_multiple_offending_regions_are_all_named(monkeypatch, check_tenant_jurisdiction_against_failover):
    monkeypatch.setenv("BEDROCK_REGION", "eu-west-1")
    monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", "us-east-1,ap-northeast-1")

    with pytest.raises(ValueError) as ei:
        check_tenant_jurisdiction_against_failover(tenant_id="tenant-eu", jurisdiction="eu")

    message = str(ei.value)
    assert "us-east-1" in message
    assert "ap-northeast-1" in message


def test_failover_explicitly_disabled_is_accepted(monkeypatch, check_tenant_jurisdiction_against_failover):
    """`failover_regions()` returns [] for the disable sentinels; an empty
    region list can never leave any jurisdiction, so this must pass even
    though the tenant is restricted."""
    monkeypatch.setenv("BEDROCK_REGION", "eu-west-1")
    monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", "none")
    assert check_tenant_jurisdiction_against_failover(
        tenant_id="tenant-eu", jurisdiction="eu") is None


def test_unset_failover_env_default_is_already_same_jurisdiction(monkeypatch, check_tenant_jurisdiction_against_failover):
    """Pins the rule this check would otherwise only ever meet 'by accident':
    `failover_regions()`'s OWN filtering (mvp/routing/chains.py) already keeps
    the unset-env default within the primary's jurisdiction, so with no
    override configured at all the check must never fire, for any primary.
    This is exactly the kind of rule the handoff warns can be met only
    because it happens not to break a fixture -- pinned here directly rather
    than left to be caught by accident (or not) elsewhere."""
    monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
    assert check_tenant_jurisdiction_against_failover(tenant_id="tenant-us", jurisdiction="us") is None

    monkeypatch.setenv("BEDROCK_REGION", "eu-west-1")
    assert check_tenant_jurisdiction_against_failover(tenant_id="tenant-eu", jurisdiction="eu") is None


def test_explicit_failover_regions_argument_is_honoured_over_the_environment(
        monkeypatch, check_tenant_jurisdiction_against_failover):
    """The pure case: pass `failover_regions=[...]` directly, with the
    environment left at a same-jurisdiction default that would otherwise
    accept. If the parameter were silently ignored in favour of the real
    `failover_regions()`, this would pass when it must fail -- proving the
    argument is actually read, not merely accepted and dropped."""
    # BEDROCK_REGION=us-east-1, STRATOCLAVE_FAILOVER_REGIONS unset (from the
    # autouse fixture) -- the REAL failover_regions() here is same-jurisdiction
    # and would accept. The explicit argument must be what decides instead.
    with pytest.raises(ValueError) as ei:
        check_tenant_jurisdiction_against_failover(
            tenant_id="tenant-us", jurisdiction="us", failover_regions=["eu-west-1"])

    message = str(ei.value)
    assert "tenant-us" in message
    assert "eu-west-1" in message

    # And the accepting side of the same pure call shape, so a check that
    # simply always raises when given an explicit list cannot pass either.
    assert check_tenant_jurisdiction_against_failover(
        tenant_id="tenant-us", jurisdiction="us", failover_regions=["us-west-2"]) is None
