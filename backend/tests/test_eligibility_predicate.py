"""C5/C7 -- the truth table for `mvp.eligibility.refusal_for` and its
deterministic precedence, unit-level (no DynamoDB, no HTTP): `refusal_for` is
specified as "a pure function of its arguments -- no I/O, no DynamoDB, no
registry lookup -- so it can be truth-tabled".

"No registry lookup" is a claim about I/O (no DynamoDB, no network), not
about calling the in-process, pure `mvp.models.resolve_model()` -- the model-
policy axis below calls it via `install_registry`'s fixture registry exactly
as the existing pin-validation code does, and that call is itself pure (an
in-memory dict read).

Signature under test, verbatim from the handoff:

    refusal_for(entry, *, tenant_cfg, user_cfg, grants) -> Optional[str]

Three things in this file were flagged guesses in an earlier round and are
now settled (a correction was sent to both authors so neither is measured
against a contract it never saw):

  1. **`grants` is the tenant's OWN grants, as `list_entitlements()` already
     returns them** -- `Iterable[Entitlement]`, unmodified. CONFIRMED, no
     change from the original guess.
  2. **The `model_not_allowed` axis is membership by IDENTITY, not by string
     equality against the entry's primary alias.** The original version of
     this file tested only "allowlist contains `entry.aliases[0]` verbatim",
     which happens to agree with the correct rule on that one spelling and
     diverges everywhere else. The settled rule, mirroring
     `_pipeline.py`'s existing pin-validation policy-set loop
     (`_resolve_registry(m) is entry`, not `m == entry.aliases[0]`):
       - the policy set is `tenant_cfg.allowlist` when non-empty, ELSE
         `tenant_cfg.chain` (allowlist and chain are never both consulted --
         allowlist, once present, is exhaustive);
       - membership is decided by resolving each configured spelling
         through the registry and comparing the resulting entry to `entry`
         by IDENTITY, so the SAME model configured under a different alias,
         or under its raw `bedrock_model_id`, still matches;
       - the user's own `chain` (when present) narrows the tenant's policy
         set further -- a model the tenant's set admits but the user's own
         chain does not name is still refused.
       Chain ORDER and the breaker tier are availability concerns, not
       permission, and are not part of this predicate at all.
  3. C12's registry-entry fields (provider/wire_protocol/bedrock_region read
     from the shipped `us.` sibling rather than guessed) live in
     `test_eligibility_c12_fable5_global.py`, not this file.

Everything else here is the handoff's own words: the three codes and their
precedence order, `scope_not_allowed` reading the effective intersection via
`mvp.routing.config.effective_profile_scopes` (called directly, never
reimplemented), the grant being keyed EXACTLY `(model_family, profile_scope)`,
and "an absent axis does not restrict."
"""
from __future__ import annotations

import pytest

from mvp.admin_entitlements import Entitlement
from mvp.models import ModelEntry
from mvp.routing.model_resolver import RoutingConfig, UserRoutingConfig
from tests.eligibility_fixtures import install_registry

TENANT = "acme-elig-unit"


def _entry(
    *, alias="widget-a", aliases=None, model_family="widget", profile_scope="us",
    access="general", jurisdiction_bounded=True, jurisdiction="us",
) -> ModelEntry:
    return ModelEntry(
        provider="anthropic",
        bedrock_model_id=f"{profile_scope}.anthropic.{model_family}"
        if profile_scope != "no_profile" else f"anthropic.{model_family}",
        bedrock_region="us-east-1",
        aliases=aliases or (alias,),
        wire_protocol="messages",
        pricing_key="default",
        model_family=model_family,
        profile_scope=profile_scope,
        access=access,
        jurisdiction_bounded=jurisdiction_bounded,
        jurisdiction=jurisdiction if jurisdiction_bounded else None,
    )


def _entitlement(model_family: str, profile_scope: str, tenant_id: str = TENANT) -> Entitlement:
    return Entitlement(
        tenant_id=tenant_id, model_family=model_family, profile_scope=profile_scope,
        granted_at="2026-01-01T00:00:00+00:00", granted_by="admin-1",
    )


def _refusal_for(entry, *, tenant_cfg=None, user_cfg=None, grants=()):
    from mvp.eligibility import refusal_for

    return refusal_for(
        entry, tenant_cfg=tenant_cfg or RoutingConfig(), user_cfg=user_cfg, grants=grants,
    )


# ---------------------------------------------------------------------------
# Baseline: every axis passes.
# ---------------------------------------------------------------------------
def test_all_axes_pass_returns_none():
    entry = _entry(access="general", profile_scope="us")
    assert _refusal_for(entry, tenant_cfg=RoutingConfig()) is None


# ---------------------------------------------------------------------------
# Each axis, refusing ALONE (the other two held passing) -- C5's truth table.
# ---------------------------------------------------------------------------
class TestEachAxisAlone:
    def test_model_not_allowed_alone(self, monkeypatch):
        entry = _entry(alias="widget-a", access="general", profile_scope="us")
        install_registry(monkeypatch, (entry,))
        tenant_cfg = RoutingConfig(allowlist=("some-other-model",))
        assert _refusal_for(entry, tenant_cfg=tenant_cfg) == "model_not_allowed"

    def test_model_not_allowed_absent_when_allowlist_empty(self):
        """Non-vacuous companion: an EMPTY/absent allowlist must not refuse --
        this is 'existing code, existing meaning', and the existing meaning
        of an empty allowlist is 'everything', never 'nothing' (explicitly
        out of scope to change, per the handoff's 'Explicitly deferred').
        No registry needed: an empty policy set is a pass with nothing to
        resolve."""
        entry = _entry(alias="widget-a", access="general", profile_scope="us")
        assert _refusal_for(entry, tenant_cfg=RoutingConfig(allowlist=())) is None

    def test_model_not_entitled_alone(self):
        entry = _entry(access="entitlement_required", model_family="widget", profile_scope="us")
        assert _refusal_for(entry, tenant_cfg=RoutingConfig(), grants=()) == "model_not_entitled"

    def test_model_not_entitled_resolved_by_a_matching_grant(self):
        """Non-vacuous companion: the SAME entry, the SAME tenant config, and
        a grant keyed exactly to (model_family, profile_scope) flips the
        result to None."""
        entry = _entry(access="entitlement_required", model_family="widget", profile_scope="us")
        grants = (_entitlement("widget", "us"),)
        assert _refusal_for(entry, tenant_cfg=RoutingConfig(), grants=grants) is None

    def test_grant_for_a_different_scope_of_the_same_family_does_not_satisfy(self):
        """'A grant is (tenant_id, model_family, profile_scope) keyed
        exactly' -- a grant for this family at a DIFFERENT scope must not
        satisfy this entry's own scope."""
        entry = _entry(access="entitlement_required", model_family="widget", profile_scope="us")
        grants = (_entitlement("widget", "global"),)
        assert _refusal_for(entry, tenant_cfg=RoutingConfig(), grants=grants) == "model_not_entitled"

    def test_grant_for_a_different_family_at_the_same_scope_does_not_satisfy(self):
        entry = _entry(access="entitlement_required", model_family="widget", profile_scope="us")
        grants = (_entitlement("other-widget", "us"),)
        assert _refusal_for(entry, tenant_cfg=RoutingConfig(), grants=grants) == "model_not_entitled"

    def test_scope_not_allowed_alone(self):
        entry = _entry(access="general", profile_scope="us")
        tenant_cfg = RoutingConfig(profile_scopes=("eu",))
        assert _refusal_for(entry, tenant_cfg=tenant_cfg) == "scope_not_allowed"

    def test_scope_absent_axis_does_not_restrict(self):
        """'An absent axis does not restrict' -- `profile_scopes=None` (never
        persisted as empty; see PR2) must admit every scope."""
        entry = _entry(access="general", profile_scope="us")
        assert _refusal_for(entry, tenant_cfg=RoutingConfig(profile_scopes=None)) is None

    def test_scope_uses_the_effective_tenant_user_intersection(self):
        """`scope_not_allowed` reads `effective_profile_scopes(tenant, user)`
        -- called directly here to build the expectation, never
        reimplemented as a second intersection."""
        from mvp.routing.config import effective_profile_scopes

        entry = _entry(access="general", profile_scope="us")
        tenant_cfg = RoutingConfig(profile_scopes=("us", "eu"))
        user_cfg = UserRoutingConfig(profile_scopes=("eu",))  # narrows AWAY "us"
        effective = effective_profile_scopes(tenant_cfg, user_cfg)
        assert "us" not in effective, "fixture sanity: the user must have narrowed 'us' out"
        assert _refusal_for(entry, tenant_cfg=tenant_cfg, user_cfg=user_cfg) == "scope_not_allowed"

    def test_scope_general_access_is_still_scope_checked(self):
        """The scope axis and the entitlement axis are independent -- a
        `general` entry (no grant needed at all) can still be refused for
        scope. Guards against an implementation that only reads `grants`
        when `access == 'entitlement_required'` and treats `general` as
        unconditionally admitted on every axis."""
        entry = _entry(access="general", profile_scope="jp")
        tenant_cfg = RoutingConfig(profile_scopes=("us",))
        assert _refusal_for(entry, tenant_cfg=tenant_cfg) == "scope_not_allowed"


# ---------------------------------------------------------------------------
# The model-policy axis's own mechanics: identity, not string equality;
# allowlist-else-chain; user-chain narrowing. Corrected per the settled
# contract -- see this file's module docstring, item 2.
# ---------------------------------------------------------------------------
class TestModelPolicyAxisMechanics:
    def test_matches_by_identity_via_a_non_primary_alias(self, monkeypatch):
        """The distinguishing case between the correct rule and a string-
        equality reading: the SAME entry, configured in the allowlist under
        an alias that is NOT `entry.aliases[0]`, must still be admitted."""
        entry = _entry(alias="widget-a", aliases=("widget-a", "widget-a-alt"),
                        access="general", profile_scope="us")
        install_registry(monkeypatch, (entry,))
        tenant_cfg = RoutingConfig(allowlist=("widget-a-alt",))
        assert _refusal_for(entry, tenant_cfg=tenant_cfg) is None, (
            "configuring the same entry under a non-primary alias must "
            "still admit it by identity, not fail a literal string match "
            "against aliases[0]"
        )

    def test_matches_by_identity_via_the_raw_bedrock_model_id(self, monkeypatch):
        entry = _entry(alias="widget-a", access="general", profile_scope="us")
        install_registry(monkeypatch, (entry,))
        tenant_cfg = RoutingConfig(allowlist=(entry.bedrock_model_id,))
        assert _refusal_for(entry, tenant_cfg=tenant_cfg) is None

    def test_falls_back_to_chain_when_allowlist_is_absent(self, monkeypatch):
        entry = _entry(alias="widget-a", access="general", profile_scope="us")
        install_registry(monkeypatch, (entry,))
        tenant_cfg = RoutingConfig(allowlist=(), chain=("widget-a",))
        assert _refusal_for(entry, tenant_cfg=tenant_cfg) is None

    def test_chain_fallback_also_refuses_when_the_entry_is_absent_from_it(self, monkeypatch):
        entry = _entry(alias="widget-a", access="general", profile_scope="us")
        other = _entry(alias="widget-other", model_family="other-widget",
                        access="general", profile_scope="us")
        install_registry(monkeypatch, (entry, other))
        tenant_cfg = RoutingConfig(allowlist=(), chain=("widget-other",))
        assert _refusal_for(entry, tenant_cfg=tenant_cfg) == "model_not_allowed"

    def test_a_present_allowlist_is_exhaustive_chain_is_not_also_consulted(self, monkeypatch):
        """allowlist and chain are never BOTH consulted for permission --
        once the allowlist is non-empty it alone decides, even when the
        chain would have admitted the entry."""
        entry = _entry(alias="widget-a", access="general", profile_scope="us")
        other = _entry(alias="widget-other", model_family="other-widget",
                        access="general", profile_scope="us")
        install_registry(monkeypatch, (entry, other))
        tenant_cfg = RoutingConfig(allowlist=("widget-other",), chain=("widget-a",))
        assert _refusal_for(entry, tenant_cfg=tenant_cfg) == "model_not_allowed", (
            "a non-empty allowlist that omits the entry must refuse it even "
            "though the chain names it -- chain is not a second, wider "
            "policy set to fall back to once an allowlist exists"
        )

    def test_user_chain_narrows_the_tenant_policy_set(self, monkeypatch):
        entry = _entry(alias="widget-a", access="general", profile_scope="us")
        other = _entry(alias="widget-other", model_family="other-widget",
                        access="general", profile_scope="us")
        install_registry(monkeypatch, (entry, other))
        tenant_cfg = RoutingConfig(allowlist=("widget-a", "widget-other"))  # tenant admits both
        user_cfg = UserRoutingConfig(chain=("widget-other",))  # user's own chain narrows AWAY widget-a
        assert _refusal_for(entry, tenant_cfg=tenant_cfg, user_cfg=user_cfg) == "model_not_allowed", (
            "the user's own chain narrows the tenant's policy set -- a "
            "model the TENANT admits but the user's own chain does not "
            "name must still be refused"
        )

    def test_user_chain_admits_when_it_names_the_entry(self, monkeypatch):
        """Non-vacuous companion to the narrowing test above: the SAME
        tenant config, with the user's chain widened (within the tenant's
        own set) to include the entry, admits it."""
        entry = _entry(alias="widget-a", access="general", profile_scope="us")
        other = _entry(alias="widget-other", model_family="other-widget",
                        access="general", profile_scope="us")
        install_registry(monkeypatch, (entry, other))
        tenant_cfg = RoutingConfig(allowlist=("widget-a", "widget-other"))
        user_cfg = UserRoutingConfig(chain=("widget-a",))
        assert _refusal_for(entry, tenant_cfg=tenant_cfg, user_cfg=user_cfg) is None

    def test_absent_user_chain_does_not_narrow(self, monkeypatch):
        """'An absent axis does not restrict' applies here too: a user with
        no chain override at all (`chain=None`, the model-resolver's own
        'inherit tenant chain' convention) leaves the tenant's own policy
        set fully in force."""
        entry = _entry(alias="widget-a", access="general", profile_scope="us")
        install_registry(monkeypatch, (entry,))
        tenant_cfg = RoutingConfig(allowlist=("widget-a",))
        user_cfg = UserRoutingConfig(chain=None)
        assert _refusal_for(entry, tenant_cfg=tenant_cfg, user_cfg=user_cfg) is None


# ---------------------------------------------------------------------------
# C7 -- deterministic precedence through COLLISION cases (not three isolated
# codes): an entry failing two or three axes at once.
# ---------------------------------------------------------------------------
class TestPrecedence:
    def test_model_not_allowed_beats_model_not_entitled(self, monkeypatch):
        entry = _entry(alias="widget-a", access="entitlement_required",
                        model_family="widget", profile_scope="us")
        install_registry(monkeypatch, (entry,))
        tenant_cfg = RoutingConfig(allowlist=("some-other-model",))  # policy fails
        # entitlement also fails: no grant.
        assert _refusal_for(entry, tenant_cfg=tenant_cfg, grants=()) == "model_not_allowed"

    def test_model_not_allowed_beats_scope_not_allowed(self, monkeypatch):
        entry = _entry(alias="widget-a", access="general", profile_scope="us")
        install_registry(monkeypatch, (entry,))
        tenant_cfg = RoutingConfig(allowlist=("some-other-model",), profile_scopes=("eu",))
        assert _refusal_for(entry, tenant_cfg=tenant_cfg) == "model_not_allowed"

    def test_model_not_entitled_beats_scope_not_allowed_when_policy_passes(self, monkeypatch):
        entry = _entry(alias="widget-a", access="entitlement_required",
                        model_family="widget", profile_scope="us")
        install_registry(monkeypatch, (entry,))
        tenant_cfg = RoutingConfig(allowlist=(), profile_scopes=("eu",))  # policy passes, scope fails
        assert _refusal_for(entry, tenant_cfg=tenant_cfg, grants=()) == "model_not_entitled"

    def test_all_three_fail_at_once_yields_model_not_allowed(self, monkeypatch):
        entry = _entry(alias="widget-a", access="entitlement_required",
                        model_family="widget", profile_scope="us")
        install_registry(monkeypatch, (entry,))
        tenant_cfg = RoutingConfig(allowlist=("some-other-model",), profile_scopes=("eu",))
        assert _refusal_for(entry, tenant_cfg=tenant_cfg, grants=()) == "model_not_allowed"

    def test_precedence_waterfall_fixing_one_axis_at_a_time(self, monkeypatch):
        """The sharpest non-vacuity proof available for a precedence order:
        one entry, one growing tenant config, fixing exactly one failing
        axis at each step and re-asserting that the NEXT-highest-precedence
        failure is what surfaces -- never a stale code left over from a
        prior step, and never 'any refusal' standing in for 'the right
        one'."""
        entry = _entry(alias="widget-a", access="entitlement_required",
                        model_family="widget", profile_scope="us")
        install_registry(monkeypatch, (entry,))

        # Step 0: all three fail -> model_not_allowed wins.
        tenant_cfg = RoutingConfig(allowlist=("some-other-model",), profile_scopes=("eu",))
        assert _refusal_for(entry, tenant_cfg=tenant_cfg, grants=()) == "model_not_allowed"

        # Step 1: widen the allowlist to admit the entry (by identity: this
        # names its own primary alias, which is the simplest admitting
        # spelling) -> policy now passes; entitlement + scope still fail ->
        # model_not_entitled wins.
        tenant_cfg = RoutingConfig(allowlist=("widget-a",), profile_scopes=("eu",))
        assert _refusal_for(entry, tenant_cfg=tenant_cfg, grants=()) == "model_not_entitled"

        # Step 2: grant the entitlement -> only scope still fails ->
        # scope_not_allowed, not "no refusal" and not the code from step 1.
        grants = (_entitlement("widget", "us"),)
        assert _refusal_for(entry, tenant_cfg=tenant_cfg, grants=grants) == "scope_not_allowed"

        # Step 3: widen the scope axis to admit "us" -> every axis passes.
        tenant_cfg = RoutingConfig(allowlist=("widget-a",), profile_scopes=("us", "eu"))
        assert _refusal_for(entry, tenant_cfg=tenant_cfg, grants=grants) is None


# ---------------------------------------------------------------------------
# Failure paths table item: "Model and scope axes are each non-empty but
# disjoint -> scope_not_allowed, naming both axes" is a CONFIG-write-time
# concern (admin_routing's validator), not this pure function's -- not
# retested here to avoid duplicating that suite's own assertions.
# ---------------------------------------------------------------------------
def test_no_profile_scope_entry_is_not_exempt_from_the_scope_axis():
    """A bare-foundation-model entry (`NO_PROFILE_SCOPE`) still has a
    declared `profile_scope` value and is not a special case this predicate
    is allowed to skip."""
    from mvp.models import NO_PROFILE_SCOPE

    entry = _entry(access="general", profile_scope=NO_PROFILE_SCOPE,
                    jurisdiction_bounded=False, jurisdiction=None)
    tenant_cfg = RoutingConfig(profile_scopes=("us",))  # does not include NO_PROFILE_SCOPE
    assert _refusal_for(entry, tenant_cfg=tenant_cfg) == "scope_not_allowed"
