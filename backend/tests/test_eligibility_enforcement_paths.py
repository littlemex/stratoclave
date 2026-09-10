"""C6 -- the eligibility predicate evaluated per concrete candidate,
immediately before that candidate's reservation is attempted, through all
three paths the handoff names: the no-config direct request, chain fallback,
and the hard pin. Plus one HTTP-level C7 collision, proving the real call
site (not just the pure predicate) resolves precedence correctly.

"The bypass is reproduced before it is closed, using an injected test-only
entry against recreated tables -- never the shipped registry" (contract,
Ordering). This file's tests ARE that reproduction: run against this
worktree before `mvp/eligibility.py` exists (or before it is wired into
`_pipeline.py`), every 403-`scope_not_allowed` assertion below fails --
either on collection (`ModuleNotFoundError: mvp.eligibility`) or on getting a
200/other status back, because nothing on the direct-passthrough branch, the
cascade loop, or pin validation currently reads a scope axis at all. That
failure-before-the-fix is the reproduction; passing after the fix lands is
the close.

Rule enforced throughout (C6's own verification requirement):
each scenario makes the model-policy axis and the entitlement axis pass
FIRST -- an empty/unset allowlist (policy admits everything, unchanged by
this PR) and a real grant via the production `grant_entitlement` -- so the
ONLY axis left that can refuse is scope, and every assertion checks for
`scope_not_allowed` BY NAME, never merely "a refusal happened".

Fixture entries' names deliberately avoid the substrings "global"/"us"/"jp"
so this file can assert the OTHER stated rule -- "a refusal ... never echoes
a scope the caller may not use" -- without a false positive from the model's
own alias merely containing the scope word.
"""
from __future__ import annotations

import pytest

from mvp.models import ModelEntry
from tests.eligibility_fixtures import (
    FakeUser,
    build_client,
    ensure_tenant_and_membership,
    grant,
    patch_bedrock_converse,
    post_messages,
    put_tenant_routing,
)
from tests.eligibility_fixtures import install_registry as _install_registry

TENANT = "acme-c6"
USER = "user-c6-1"

# The scope-restricted entry every scenario refuses on. Alias/family chosen to
# NOT contain "global" so the no-scope-echo assertion is meaningful.
PLANETARY_ALIAS = "widget-planetary"
PLANETARY_FAMILY = "widget-planetary-fam"
PLANETARY_ENTRY = ModelEntry(
    provider="anthropic", bedrock_model_id="global.anthropic.widget-planetary",
    bedrock_region="us-east-1", aliases=(PLANETARY_ALIAS,), wire_protocol="messages",
    pricing_key="default", model_family=PLANETARY_FAMILY, profile_scope="global",
    access="entitlement_required", jurisdiction_bounded=False, jurisdiction=None,
)

# A servable, IN-SCOPE, general-access entry used as chain HEAD so its own
# quota can be exhausted to force the cascade onward to PLANETARY_ENTRY.
REGIONAL_ALIAS = "widget-regional"
REGIONAL_FAMILY = "widget-regional-fam"
REGIONAL_ENTRY = ModelEntry(
    provider="anthropic", bedrock_model_id="us.anthropic.widget-regional",
    bedrock_region="us-east-1", aliases=(REGIONAL_ALIAS,), wire_protocol="messages",
    pricing_key="default", model_family=REGIONAL_FAMILY, profile_scope="us",
    access="general", jurisdiction_bounded=True, jurisdiction="us",
)


@pytest.fixture
def user_holder():
    return {"user": FakeUser(user_id=USER, org_id=TENANT)}


@pytest.fixture
def client(dynamodb_mock, monkeypatch, user_holder):
    c = build_client(monkeypatch, current_user_provider=lambda: user_holder["user"])
    patch_bedrock_converse(monkeypatch)
    ensure_tenant_and_membership(TENANT, USER)
    return c


def _refusal_reason(resp):
    body = resp.json()
    return body.get("detail", {}).get("reason")


class TestDirectRequest:
    """The no-routing-config passthrough branch in `reserve_credit_for_model`
    (`_pipeline.py:2681`: 'no chain, no allowlist, no quotas at all ->
    passthrough on the requested model') calls `reserve_credit` DIRECTLY,
    bypassing `_reserve_over_candidates` entirely -- a fourth C6 enforcement
    path, distinct from the cascade `_reserve_over_candidates` itself walks
    for the requested model as its one candidate: `profile_scopes` and
    entitlement grants are config keys INDEPENDENT of chain/allowlist/quotas,
    so a tenant that configured only a scope or entitlement restriction has
    all three empty, lands here, and would be served with no eligibility
    check at all if this branch does not carry its own.

    Causally verified this class is reaching THIS branch and no other, not
    merely inferred from the fixture: with a temporary call to
    `refusal_for` inserted at `_pipeline.py:2681` (immediately inside the
    `if not chain and not allowlist and not quotas:` guard, before
    `_price(model_name)`), every test in this class -- and
    `TestPrecedenceAtTheRealCallSite` below, which uses the same
    passthrough -- passed, while `TestChainFallback` and `TestHardPin`
    (different branches, untouched by that insert) stayed red. Removing
    only that one inserted call put this class back to red. The insert and
    the temporary `mvp/eligibility.py` it called were both reverted before
    commit; `git status` shows neither production file touched."""

    def _setup(self, monkeypatch, client, *, scopes=("us",)):
        _install_registry(monkeypatch, (PLANETARY_ENTRY,))
        r = put_tenant_routing(client, TENANT, profile_scopes=list(scopes))
        assert r.status_code == 200, r.text
        grant(TENANT, PLANETARY_FAMILY, "global")

    def test_scope_not_allowed_specifically(self, monkeypatch, client):
        self._setup(monkeypatch, client, scopes=("us",))
        resp = post_messages(client, model=PLANETARY_ALIAS)
        assert resp.status_code == 403, resp.text
        assert _refusal_reason(resp) == "scope_not_allowed", resp.text

    def test_refusal_does_not_echo_the_forbidden_scope_value(self, monkeypatch, client):
        self._setup(monkeypatch, client, scopes=("us",))
        resp = post_messages(client, model=PLANETARY_ALIAS)
        assert resp.status_code == 403, resp.text
        assert "global" not in resp.text, (
            f"the refusal must name the axis (scope_not_allowed) but never "
            f"the forbidden scope value itself: {resp.text!r}"
        )

    def test_non_vacuous_succeeds_once_scope_is_widened(self, monkeypatch, client):
        """The other two axes were already made to pass in `_setup`; this
        proves the refusal above was genuinely ABOUT scope -- widening ONLY
        the scope axis (same policy, same grant) flips it to success.

        Widened by OMITTING `profile_scopes` from the PUT (an absent axis
        does not restrict), not by naming `["us", "global"]`: C14 refuses a
        set naming both the unbounded `global` scope and a bounded one at
        write time (`{us, global}` reads as a geography restriction but
        means 'that geography, or anywhere on earth')."""
        self._setup(monkeypatch, client, scopes=("us",))
        assert post_messages(client, model=PLANETARY_ALIAS).status_code == 403

        r = put_tenant_routing(client, TENANT)
        assert r.status_code == 200, r.text
        resp = post_messages(client, model=PLANETARY_ALIAS)
        assert resp.status_code == 200, resp.text

    def test_non_vacuous_without_the_grant_it_is_model_not_entitled_not_scope(
        self, monkeypatch, client,
    ):
        """Companion proving THIS test file's own fixture actually exercises
        the entitlement axis rather than trivially skipping it: with the
        SAME scope restriction but no grant at all, the refusal must be
        `model_not_entitled` (precedence 2), not `scope_not_allowed`."""
        _install_registry(monkeypatch, (PLANETARY_ENTRY,))
        r = put_tenant_routing(client, TENANT, profile_scopes=["us"])
        assert r.status_code == 200, r.text
        # No grant this time.
        resp = post_messages(client, model=PLANETARY_ALIAS)
        assert resp.status_code == 403, resp.text
        assert _refusal_reason(resp) == "model_not_entitled", resp.text


class TestChainFallback:
    """The bypass finding, verbatim: 'checking only the requested entry
    leaves chain fallback open, because a us-restricted request can fall
    through to a configured global candidate.' REGIONAL_ENTRY is fully
    eligible (in-scope, general access, admitted by an empty allowlist) but
    its quota is set to 1 micro-USD -- functionally exhausted on the first
    attempt -- forcing the cascade onward to PLANETARY_ENTRY, which must be
    refused for scope BEFORE its own reservation is attempted (never
    silently skipped past to a 402, and never served)."""

    def _setup(self, monkeypatch, client):
        _install_registry(monkeypatch, (REGIONAL_ENTRY, PLANETARY_ENTRY))
        r = put_tenant_routing(
            client, TENANT,
            chain=[REGIONAL_ALIAS, PLANETARY_ALIAS],
            quotas={REGIONAL_ALIAS: {"limit": 1}},
            fallback_default="on",
            profile_scopes=["us"],
        )
        assert r.status_code == 200, r.text
        grant(TENANT, PLANETARY_FAMILY, "global")

    def test_scope_not_allowed_specifically_reached_via_fallback(self, monkeypatch, client):
        self._setup(monkeypatch, client)
        resp = post_messages(client, model=REGIONAL_ALIAS)
        assert resp.status_code == 403, resp.text
        assert _refusal_reason(resp) == "scope_not_allowed", (
            f"the cascade must refuse the fallen-through candidate for scope "
            f"specifically, not silently exhaust to a generic quota 402: {resp.text!r}"
        )

    def test_refusal_does_not_echo_the_forbidden_scope_value(self, monkeypatch, client):
        self._setup(monkeypatch, client)
        resp = post_messages(client, model=REGIONAL_ALIAS)
        # Non-vacuity: this must be the SAME 403 scope_not_allowed refusal
        # asserted above, not merely "whatever this response is happens not
        # to contain the word global" -- a 200 success response would pass
        # the substring check just as easily.
        assert resp.status_code == 403, resp.text
        assert _refusal_reason(resp) == "scope_not_allowed", resp.text
        assert "global" not in resp.text, resp.text

    def test_non_vacuous_succeeds_once_scope_is_widened(self, monkeypatch, client):
        """Same waterfall proof as the direct-request case: widen ONLY the
        scope axis and the cascade actually lands on PLANETARY_ENTRY and
        settles (200) -- proving the fallback mechanics themselves (quota
        exhaustion -> advance) were never the thing under test.

        Widened by OMITTING `profile_scopes` (absent = unrestricted), not by
        naming `["us", "global"]`: this scenario needs BOTH REGIONAL_ENTRY's
        own scope ("us") and PLANETARY_ENTRY's ("global") admitted at once --
        REGIONAL must still be reachable long enough to hit its exhausted
        quota and advance, not itself refuse on scope first -- and C14
        refuses any set naming both a bounded scope and the unbounded
        `global` in the same write, so no explicit two-member list can
        express this; only an absent axis can."""
        self._setup(monkeypatch, client)
        assert post_messages(client, model=REGIONAL_ALIAS).status_code == 403

        r = put_tenant_routing(
            client, TENANT,
            chain=[REGIONAL_ALIAS, PLANETARY_ALIAS],
            quotas={REGIONAL_ALIAS: {"limit": 1}},
            fallback_default="on",
        )
        assert r.status_code == 200, r.text
        resp = post_messages(client, model=REGIONAL_ALIAS)
        assert resp.status_code == 200, resp.text


class TestHardPin:
    """`_validate_model_pin` is 'defence in depth, not the boundary' -- the
    handoff's own words -- so this test asserts the FINAL observable result
    (403 scope_not_allowed) without caring whether the catch happens inside
    `_validate_model_pin` itself or inside `_reserve_over_candidates`'s
    per-candidate loop once the pin becomes its one-element candidate list.

    Tenant has neither `allowlist` nor `chain` (pure passthrough), so the
    pin's own model-policy check ('only a tenant with neither allowlist nor
    chain accepts an arbitrary servable pin') is satisfied trivially and
    admits the pin -- the model-policy axis 'passes first' exactly as C6
    requires."""

    def _setup(self, monkeypatch, client, *, scopes=("us",)):
        _install_registry(monkeypatch, (PLANETARY_ENTRY,))
        r = put_tenant_routing(client, TENANT, profile_scopes=list(scopes))
        assert r.status_code == 200, r.text
        grant(TENANT, PLANETARY_FAMILY, "global")

    def test_scope_not_allowed_specifically(self, monkeypatch, client):
        self._setup(monkeypatch, client)
        # body.model is an ordinary, real, already-servable model; the pin
        # header is what actually names the candidate under test.
        resp = post_messages(client, model="claude-haiku-4-5", pin=PLANETARY_ALIAS)
        assert resp.status_code == 403, resp.text
        assert _refusal_reason(resp) == "scope_not_allowed", resp.text

    def test_refusal_is_403_not_400_the_pin_is_servable_and_known(self, monkeypatch, client):
        """Distinguishes this from an UNSERVABLE pin: servability is checked
        BEFORE eligibility and stays a 400 so a refusal never leaks that a
        model exists but is not permitted -- this pin resolves and speaks
        the route's protocol, so its refusal must be the POLICY status (403),
        not the servability one (400)."""
        self._setup(monkeypatch, client)
        resp = post_messages(client, model="claude-haiku-4-5", pin=PLANETARY_ALIAS)
        assert resp.status_code == 403, (
            f"a servable, known pin refused on policy grounds must be 403, "
            f"not 400: {resp.status_code} {resp.text!r}"
        )

    def test_non_vacuous_succeeds_once_scope_is_widened(self, monkeypatch, client):
        """Widened by OMITTING `profile_scopes`, not by naming
        `["us", "global"]` -- see the direct-request test's own comment for
        why C14 forbids that combination."""
        self._setup(monkeypatch, client, scopes=("us",))
        assert post_messages(
            client, model="claude-haiku-4-5", pin=PLANETARY_ALIAS).status_code == 403

        r = put_tenant_routing(client, TENANT)
        assert r.status_code == 200, r.text
        resp = post_messages(client, model="claude-haiku-4-5", pin=PLANETARY_ALIAS)
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# C7, once, at the HTTP boundary: the real call site must resolve precedence
# the same way the pure predicate does (test_eligibility_predicate.py already
# covers the collision matrix per the contract's own "unit: the collision
# cases" verification plan for C7 -- this is the wiring check, not a repeat
# of the matrix).
# ---------------------------------------------------------------------------
class TestPrecedenceAtTheRealCallSite:
    def test_model_not_entitled_beats_scope_not_allowed_then_flips_when_granted(
        self, monkeypatch, client,
    ):
        _install_registry(monkeypatch, (PLANETARY_ENTRY,))
        r = put_tenant_routing(client, TENANT, profile_scopes=["us"])  # scope will fail
        assert r.status_code == 200, r.text
        # No grant yet -> entitlement fails too. Policy (empty allowlist) passes.
        resp = post_messages(client, model=PLANETARY_ALIAS)
        assert resp.status_code == 403, resp.text
        assert _refusal_reason(resp) == "model_not_entitled", (
            f"entitlement (precedence 2) must beat scope (precedence 3) when "
            f"both fail and policy passes: {resp.text!r}"
        )

        grant(TENANT, PLANETARY_FAMILY, "global")
        resp2 = post_messages(client, model=PLANETARY_ALIAS)
        assert resp2.status_code == 403, resp2.text
        assert _refusal_reason(resp2) == "scope_not_allowed", (
            f"granting the entitlement must surface the NEXT-precedence "
            f"failure (scope), not leave the stale code behind: {resp2.text!r}"
        )
