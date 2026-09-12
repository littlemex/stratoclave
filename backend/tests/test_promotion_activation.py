"""Activation: a verified promotion candidate becomes a routable registry entry.

The specification this file is written against fixes the shape of the two
things activation reads (a candidate, and a probe verdict keyed on the same
profile and mode) and the shape of what it produces (a registry entry whose
access is always the restrictive one), and it fixes the three conditions a
verdict must satisfy before a candidate may go live. It deliberately leaves
one thing open: the calling convention a caller actually uses to attempt an
activation. No document in this change names a module, a function, or a
route for it -- only that the action exists, that it is gated by a named
permission, and that its result becomes visible through the registry's one
read accessor.

This file resolves that gap the same way this test suite's own
`test_discovery_records.py` resolves an identical one for the discovered-
record store: by committing to a concrete calling convention and saying so,
rather than refusing to write a test until someone else decides. It commits
to a single function, `mvp.discovery.activation.activate_candidate
(profile_id, mode, *, actor)`, which performs its own permission check and
raises when the actor may not activate, because no component in this launch
owns an admin route for this action and a permission that only gates behind
a route nobody has written yet would be untestable. If the real
implementation instead wraps a permission-agnostic core behind a route's own
dependency injection, that is a calling-convention difference for whoever
integrates the pieces to reconcile -- every assertion below is about what
must be OBSERVABLY true regardless of how the call is shaped: whether the
model becomes visible to something that reads the registry, whether its
access is the restrictive one, whether each of the three verdict conditions
independently refuses it, whether the permission separates a caller that may
do this from one that may not, and whether activating one candidate leaves
every other registry entry untouched.

Two names are load-bearing and are NOT invented here, because both are fixed
by name in the frozen cross-unit specification: the permission string, and
the three per-field sources a produced entry must have come from. Everything
else about how a candidate or a verdict is constructed below is this file's
own test data, built from the shapes both stores are specified to hold.
"""
from __future__ import annotations

import importlib

import pytest

from mvp.deps import AuthenticatedUser
from mvp.discovery.records import ObservationScope


# ---------------------------------------------------------------------------
# Construction helpers. Every field below is required by the frozen shape of
# the store it belongs to; nothing here is optional test convenience.
# ---------------------------------------------------------------------------

def _scope(**overrides) -> ObservationScope:
    base = dict(
        account="776010787911", region="us-east-1",
        credentials_fingerprint="abc123", observed_at="2026-09-01T00:00:00+00:00",
    )
    base.update(overrides)
    return ObservationScope(**base)


#: The profile this whole file promotes and activates. Distinct from every
#: alias already shipped in the bundled registry document, so a test that
#: forgets to check "is this really new" cannot pass by coincidence.
_PROFILE_ID = "us.anthropic.claude-newmodel-9"
_ALIAS = "claude-newmodel-9"
_MODEL_FAMILY = "anthropic.claude-newmodel-9"

#: A pricing key that already has a reviewed rate row in the bundled document
#: (shared with the shipped Opus-tier entries), so a candidate naming it is
#: not refused for a reason this file is not testing.
_PRICING_KEY = "opus"



@pytest.fixture(autouse=True)
def _fresh_composed_registry():
    """Drop the process-wide composed-registry cache around every test.

    That cache is module-global and deliberately outlives a single request; the
    mocked table is per-test. Without this, one test's activation stays visible
    to the next one through the cache after its rows are gone -- which made the
    refusal tests pass for the wrong reason (nothing was ever visible) before the
    writer learned to invalidate locally, and would make them fail for a
    different wrong reason now.
    """
    from mvp.models import invalidate_composed_registry

    invalidate_composed_registry()
    yield
    invalidate_composed_registry()


def _candidate(**overrides):
    from mvp.discovery.promotion import PromotionCandidate

    base = dict(
        profile_id=_PROFILE_ID,
        observation_scope=_scope(),
        state="candidate",
        aliases=(_ALIAS,),
        pricing_key=_PRICING_KEY,
        jurisdiction="us",
        provider="anthropic",
        bedrock_model_id=_PROFILE_ID,
        bedrock_region="us-east-1",
        wire_protocol="messages",
        model_family=_MODEL_FAMILY,
        profile_scope="us",
        created_at="2026-09-01T00:00:00+00:00",
        created_by="admin-1",
    )
    base.update(overrides)
    return PromotionCandidate(**base)


#: The mode a probe verified against. Its content is opaque to this file --
#: only that a candidate's activation attempt and the verdict it is checked
#: against name the SAME one, which is what the store's own key scheme
#: (`sk = f"MODE#{mode}"`) requires for the read to find anything at all.
_INVOCATION = "sync"


def _verdict(**overrides):
    from mvp.discovery.verdict import ProbeVerdict

    base = dict(
        profile_id=_PROFILE_ID,
        observation_scope=_scope(),
        invocation=_INVOCATION,
        verified_at="2026-09-01T01:00:00+00:00",
        verified_by="probe",
        pricing_key_at_verification=_PRICING_KEY,
        wire_protocol_verified="messages",
        state="verified",
    )
    base.update(overrides)
    return ProbeVerdict(**base)


def _actor(roles: list[str], *, user_id: str = "actor-1") -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id, email=f"{user_id}@example.com", roles=roles,
        org_id="default-org", auth_kind="jwt",
    )


#: The literal permission string the frozen specification names for both
#: creating a candidate and activating one. Quoted once, here, so a typo in
#: one test cannot silently agree with a typo in another.
_PROMOTE_SCOPE = "models:promote"


def _grant_only(monkeypatch, role: str, scopes: frozenset[str]) -> None:
    """Make `role` hold exactly `scopes`, independent of the real permission
    document. The real document's role assignments are a different unit's
    job to seed; this file needs only a controllable, structural way to give
    one synthetic role a permission and withhold it from another, the same
    technique `test_entitlements_permission.py`'s separation tests already
    use for an unrelated pair of scopes."""
    import mvp.authz as authz

    monkeypatch.setattr(
        authz, "_get_permissions_for_role",
        lambda r: sorted(scopes) if r == role else [],
    )



def _discovered_record(candidate):
    """The record activation reads back, matching the candidate under test."""
    from mvp.discovery.records import DiscoveredRecord, ObservationScope

    return DiscoveredRecord(
        profile_id=candidate.profile_id,
        provider=candidate.provider,
        profile_scope=candidate.profile_scope,
        model_family=candidate.model_family,
        jurisdiction_bounded=True,
        destination_regions=("us-east-1",),
        invocation_region="us-east-1",
        raw_id=candidate.bedrock_model_id,
        raw_payload={},
        observation_scope=ObservationScope(
            account="000000000000", region="us-east-1",
            credentials_fingerprint="test", observed_at="2026-09-12T00:00:00+00:00",
        ),
    )


def _seed_and_activate(dynamodb_mock, monkeypatch, *, candidate=None, verdict=None,
                        actor=None):
    """Arrange a candidate and (optionally) a verdict, then attempt
    activation. Returns the actor used, so a caller can look it up again
    through a consumer afterward."""
    from mvp.discovery.promotion import put_promotion_candidate
    from mvp.discovery.records import put_discovered_record
    from mvp.discovery.verdict import put_probe_verdict
    from mvp.discovery.activation import activate_candidate

    cand = candidate or _candidate()
    # Activation reads the discovered record for one field it refuses to infer:
    # whether the profile is geographically bounded. A candidate always carries a
    # jurisdiction because a human supplied one, but "a jurisdiction was given" is
    # not the same fact as "this profile is bounded", and for a global profile the
    # inferred answer would be wrong. So the record has to be present.
    put_discovered_record(_discovered_record(cand))
    put_promotion_candidate(cand)
    if verdict is not None:
        put_probe_verdict(verdict)
    if actor is None:
        _grant_only(monkeypatch, "promoter", frozenset({_PROMOTE_SCOPE}))
        actor = _actor(["promoter"])
    activate_candidate(_PROFILE_ID, _INVOCATION, actor=actor)
    return actor


def _pricing_config_via_the_real_route(monkeypatch, dynamodb_mock):
    """Call the admin pricing surface exactly the way an HTTP request would
    reach it -- mounted router, real permission evaluation, a caller that
    holds `usage:read-all` and nothing else -- rather than importing its
    handler and calling it as a bare function. This is the SAME shape
    `test_admin_pool_budget.py` already uses for a different admin route,
    reused rather than re-invented, because a shortcut that skips FastAPI's
    own request handling could pass while the real route -- serialising
    through its declared response model -- would not.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mvp.admin_pricing import router
    from mvp.deps import get_current_user
    import mvp.authz as authz

    def fake_user_has_permission(user, scope: str) -> bool:
        return scope == "usage:read-all"

    monkeypatch.setattr(authz, "user_has_permission", fake_user_has_permission)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: _actor(["reader"])
    client = TestClient(app)
    resp = client.get("/api/mvp/admin/pricing-config")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _models_for_pricing_key(pricing_config: dict, pricing_key: str) -> list[str]:
    for row in pricing_config["rates"]:
        if row["pricing_key"] == pricing_key:
            return row["models"]
    return []


# ---------------------------------------------------------------------------
# A component that reads the registry sees the activated model.
# ---------------------------------------------------------------------------
def test_a_pricing_reader_sees_the_activated_model_under_its_declared_rate_key(
    dynamodb_mock, monkeypatch,
):
    """The strongest form of "an activated entry is reachable": not that the
    registry's own accessor returns it (that would only prove activation
    wrote to the place it reads from), but that a DIFFERENT, real component
    which prices the registry -- reached the same way an HTTP request would
    reach it -- lists the newly activated alias under the pricing key the
    candidate declared. The defect this guards against is a model that a
    chat request could resolve to while the component that prices it has
    never heard of it, which is exactly the shape of bug a second, private
    read path would produce.
    """
    _seed_and_activate(dynamodb_mock, monkeypatch, verdict=_verdict())
    config = _pricing_config_via_the_real_route(monkeypatch, dynamodb_mock)
    assert _ALIAS in _models_for_pricing_key(config, _PRICING_KEY), (
        f"{_ALIAS!r} did not appear among the models the pricing reader lists "
        f"for pricing_key={_PRICING_KEY!r} after activation -- the pricing "
        f"surface and the registry's own accessor disagree about what exists"
    )


# ---------------------------------------------------------------------------
# access is always the restrictive value, asserted on the stored/resolved
# entry directly rather than trusted from the writer's intent.
# ---------------------------------------------------------------------------
def test_activated_entry_access_is_entitlement_required_never_general(
    dynamodb_mock, monkeypatch,
):
    """`ModelEntry.access` defaults to `"general"` -- reachable by every
    tenant -- so a promoted entry that merely FORGOT to set it would still
    construct, still validate, and would fail open. Checked on the actual
    entry the registry now resolves, not on an argument this test passed in,
    because the defect this pins is exactly a writer that intended
    `entitlement_required` and produced `general` regardless.
    """
    from mvp.models import registry_entries

    _seed_and_activate(dynamodb_mock, monkeypatch, verdict=_verdict())
    matches = [e for e in registry_entries() if _ALIAS in e.aliases]
    assert len(matches) == 1, (
        f"expected exactly one registry entry for {_ALIAS!r} after "
        f"activation, found {len(matches)}"
    )
    assert matches[0].access == "entitlement_required", (
        f"activated entry {_ALIAS!r} has access={matches[0].access!r}; every "
        f"tenant can already reach a general entry, which is precisely the "
        f"unchosen widening this field exists to prevent"
    )


# ---------------------------------------------------------------------------
# Each of the three verification conditions is independently load-bearing.
# Each test satisfies the OTHER two so a combined check could not pass by
# only ever exercising one of them.
# ---------------------------------------------------------------------------
def test_an_unverified_verdict_refuses_activation(dynamodb_mock, monkeypatch):
    """A verdict that exists but has been invalidated (rather than being
    absent) must still refuse -- absence and invalidation are different
    facts, and a check that only asked "is there a row" would pass this one
    by accident."""
    from mvp.models import registry_entries

    verdict = _verdict(state="invalidated")
    with pytest.raises(Exception):
        _seed_and_activate(dynamodb_mock, monkeypatch, verdict=verdict)
    assert not any(_ALIAS in e.aliases for e in registry_entries()), (
        "the model became visible despite an invalidated verdict"
    )


def test_a_verdict_verified_at_a_different_pricing_key_refuses_activation(
    dynamodb_mock, monkeypatch,
):
    """The verdict is current and speaks the right protocol, but it was
    earned by a candidate priced differently -- the thing verified is not
    the thing being activated, even though every other field lines up."""
    from mvp.models import registry_entries

    verdict = _verdict(pricing_key_at_verification="sonnet")
    with pytest.raises(Exception):
        _seed_and_activate(dynamodb_mock, monkeypatch, verdict=verdict)
    assert not any(_ALIAS in e.aliases for e in registry_entries()), (
        "the model became visible despite a pricing-key mismatch between the "
        "verdict and the candidate"
    )


def test_a_verdict_verified_over_a_different_wire_protocol_refuses_activation(
    dynamodb_mock, monkeypatch,
):
    """The verdict is current and priced correctly, but the probe that
    produced it spoke a different wire protocol than the candidate declares
    -- a real risk once a provider ever ships on both, and the reason this
    field is verified rather than trusted from a table at all."""
    from mvp.models import registry_entries

    verdict = _verdict(wire_protocol_verified="responses")
    with pytest.raises(Exception):
        _seed_and_activate(dynamodb_mock, monkeypatch, verdict=verdict)
    assert not any(_ALIAS in e.aliases for e in registry_entries()), (
        "the model became visible despite a wire-protocol mismatch between "
        "the verdict and the candidate"
    )


def test_a_verdict_satisfying_all_three_conditions_activates(dynamodb_mock, monkeypatch):
    """Non-vacuity for the three tests above: the SAME candidate, checked
    against a verdict that agrees on state, pricing key and wire protocol,
    activates without raising. Without this, the three refusal tests could
    all be passing against a version of activation that refuses everything.
    """
    from mvp.models import registry_entries

    _seed_and_activate(dynamodb_mock, monkeypatch, verdict=_verdict())
    assert any(_ALIAS in e.aliases for e in registry_entries())


# ---------------------------------------------------------------------------
# The permission gates it. Both directions, against the frozen literal
# scope string, exercised structurally so this holds regardless of which
# real role the permission document eventually assigns it to.
# ---------------------------------------------------------------------------
def test_a_caller_without_the_promotion_permission_cannot_activate(
    dynamodb_mock, monkeypatch,
):
    """Non-vacuity for the permission gate: an actor holding an unrelated
    permission, but not `models:promote`, must be refused. The positive case
    alone (below) would also pass against a version that checks nothing at
    all -- only this negative half tells the two apart."""
    from mvp.models import registry_entries

    _grant_only(monkeypatch, "bystander", frozenset({"messages:send"}))
    actor = _actor(["bystander"])
    with pytest.raises(Exception):
        _seed_and_activate(dynamodb_mock, monkeypatch, verdict=_verdict(), actor=actor)
    assert not any(_ALIAS in e.aliases for e in registry_entries()), (
        "the model became visible despite the actor lacking the promotion "
        "permission"
    )


def test_a_caller_holding_the_promotion_permission_can_activate(dynamodb_mock, monkeypatch):
    """The positive half: an actor holding exactly `models:promote` (and
    nothing broader) succeeds. Exactly the permission named, not a role that
    happens to also hold it in the real deployment -- the real role
    assignment belongs to a different part of this change and must not leak
    into whether THIS check works."""
    from mvp.models import registry_entries

    _grant_only(monkeypatch, "promoter", frozenset({_PROMOTE_SCOPE}))
    actor = _actor(["promoter"])
    _seed_and_activate(dynamodb_mock, monkeypatch, verdict=_verdict(), actor=actor)
    assert any(_ALIAS in e.aliases for e in registry_entries())


# ---------------------------------------------------------------------------
# Activation of one candidate must not widen an unrelated, already-shipped
# entry -- the regression the task most wants pinned because it is the one
# a diff would not show.
# ---------------------------------------------------------------------------
def test_activating_one_candidate_leaves_every_other_registry_entry_untouched(
    dynamodb_mock, monkeypatch,
):
    """Snapshots every entry the bundled document ships BEFORE activation,
    then re-reads the registry after and requires each one to still be
    exactly itself: same aliases, same access, same pricing key. A regression
    that widened an unrelated entry's access, or merged its aliases with the
    new candidate's, would leave the new model correctly activated and this
    test would be the only one to notice."""
    from mvp.models import registry_entries

    before = {
        entry.bedrock_model_id: (entry.aliases, entry.access, entry.pricing_key)
        for entry in registry_entries()
    }
    assert before, "the bundled registry document has no entries to compare against"
    assert _PROFILE_ID not in before, "the profile this file promotes must not pre-exist"

    _seed_and_activate(dynamodb_mock, monkeypatch, verdict=_verdict())

    after = {
        entry.bedrock_model_id: (entry.aliases, entry.access, entry.pricing_key)
        for entry in registry_entries()
    }
    unchanged = {k: v for k, v in after.items() if k in before}
    assert unchanged == before, (
        "activating a new candidate changed one or more pre-existing registry "
        "entries -- expected every entry present before activation to be "
        "byte-for-byte identical after it"
    )


# ---------------------------------------------------------------------------
# Store behaviour when the candidate store is unreachable at process start.
# The frozen specification leaves this decision to whichever way the real
# implementation went; this test observes and records the actual behaviour
# rather than assuming one, and pins the one invariant that has to hold no
# matter which way it went: a caller must get either a clean startup failure
# naming the store, or a running process that simply does not resolve the
# promoted model -- never a partial, wrongly-priced, or silently-general
# entry.
# ---------------------------------------------------------------------------
def test_registry_composition_behaviour_when_the_candidate_store_is_unreachable_at_start(
    dynamodb_mock, monkeypatch,
):
    import mvp.models as models

    _seed_and_activate(dynamodb_mock, monkeypatch, verdict=_verdict())
    assert any(_ALIAS in e.aliases for e in models.registry_entries()), (
        "setup failed: the model was not active before the store was made "
        "unreachable, so this test cannot tell 'vanished' from 'never there'"
    )

    # `delete_table` is a client/Table operation, not a ServiceResource one.
    dynamodb_mock.meta.client.delete_table(TableName="stratoclave-promotion-candidates")
    try:
        try:
            importlib.reload(models)
        except Exception as exc:
            # Observed outcome: the process refuses to start. Recorded for
            # whoever reads this file's report -- the specification left
            # picking one of the two outcomes to the implementation, and
            # this is which one it turned out to be.
            failure_names_the_store = any(
                token in str(exc).lower()
                for token in ("promotion", "candidate", "unreachable", "dynamodb", "table")
            )
            assert failure_names_the_store, (
                f"process start refused, but the failure does not name the "
                f"unreachable store -- an operator reading this crash would "
                f"not know what to fix: {exc!r}"
            )
        else:
            # Observed outcome: the process starts and simply does not
            # resolve the promoted model. Recorded for the same reason.
            assert not any(_ALIAS in e.aliases for e in models.registry_entries()), (
                "the candidate store was unreachable, yet the promoted model "
                "is still resolvable -- this can only be true if the entry "
                "was cached somewhere other than the store, which is a wrong "
                "answer even under the 'serve code-only' choice: the model "
                "should be ABSENT, not stale-but-present"
            )
            assert len(models.registry_entries()) >= 22, (
                "the bundled, code-shipped registry document must still load "
                "in full even when the promoted-candidate store cannot be "
                "reached -- 'serve code-only' means the code-only half keeps "
                "working, not that it degrades too"
            )
    finally:
        dynamodb_mock.create_table(
            TableName="stratoclave-promotion-candidates",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        importlib.reload(models)
