"""A blocker nobody reads does not exist -- and neither does a refusal that
only says "no".

The grant refusal is the highest-value place a blocker can surface, because
the human is already there: an operator granting a tenant access to a
model is a person mid-decision, not a batch job reading a report later.
`mvp.admin_entitlements.GrantFloorRefusal` is that surface, and it already
shipped (the floor comparison at grant, its own closed reason vocabulary of
`floor_disagreement` / `floor_row_unreviewed`, and the bulk of its own test
coverage all predate this file -- see `test_grant_floor_e6.py`). What that
file pins is the STRUCTURE of a refusal: `.reason`, `.pricing_key`, `.leg`,
`.floor_micro`, `.live_micro`, `.notes`. None of its tests read the
refusal's own message -- the string a caller who does nothing but
`str(exc)` the exception actually sees -- which is the specific gap this
file closes: a refusal that carries perfect structured fields but a bare
"refused" message would still pass every existing test and give an
operator nothing to act on.

**Where this file stops rather than guesses.** The two closed reasons this
exception already carries are about a PRICE disagreeing with its floor --
neither names any of the five discovered-record blocker types
(`no_model_access`, `price_dimensions_unknown`, ...). Whether granting
entitlement to a model that ALSO carries an open, actionable discovery
blocker should refuse -- and if so, through this same exception, a new
reason on it, or an entirely separate one -- is not settled by either
frozen document: `GrantFloorRefusal.REASONS` is a closed, validated set
(constructing it with anything outside `{floor_disagreement,
floor_row_unreviewed}` raises), so widening it is a decision this file
does not make for the code author. This file therefore pins only what IS
settled -- that whichever of the two shipped reasons fires, the refusal's
own message names an action, not merely a fact -- and reports the wider
question rather than picking an answer for it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pytest

FAMILY = "acme-discovery-e13"
SCOPE = "us"
PRICING_KEY = "opus"  # bundled floor: 5_500_000 / 27_500_000 / 550_000 / 6_875_000

UNREVIEWED_FAMILY = "acme-discovery-e13-unreviewed"
UNREVIEWED_SCOPE = "us"
UNREVIEWED_PRICING_KEY = "acme-e13-unreviewed-key-xyz"

TENANT = "acme-e13-tenant"


@dataclass
class _Actor:
    """Matches `mvp.deps.AuthenticatedUser`'s fields `grant_entitlement`
    reads -- the same minimal local stand-in `test_grant_floor_e6.py` and
    `test_entitlement_store.py` each already build rather than share,
    since it is three fields wide and a shared fixture for it would be a
    fixture for the dataclass import alone."""

    user_id: str = "admin-1"
    email: str = "admin@example.com"
    org_id: str = "ops"
    roles: list = field(default_factory=lambda: ["admin"])
    auth_kind: str = "jwt"
    key_scopes: Optional[list] = None


def _fixture_registry():
    from mvp.models import ModelEntry

    def _entry(family, scope, pricing_key):
        return ModelEntry(
            provider="anthropic", bedrock_model_id=f"us.anthropic.{family}",
            bedrock_region="us-east-1", aliases=(f"{family}-{scope}",),
            wire_protocol="messages", pricing_key=pricing_key,
            model_family=family, profile_scope=scope,
            access="entitlement_required", jurisdiction_bounded=True, jurisdiction="us",
        )

    return (
        _entry(FAMILY, SCOPE, PRICING_KEY),
        _entry(UNREVIEWED_FAMILY, UNREVIEWED_SCOPE, UNREVIEWED_PRICING_KEY),
    )


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr("mvp.models._REGISTRY", _fixture_registry())


@pytest.fixture(autouse=True)
def _reset_pricing_cache():
    from mvp import pricing

    pricing.reset_cache()
    yield
    pricing.reset_cache()


def _seed_discovered_record(model_family: str, profile_scope: str) -> None:
    from mvp.discovery.records import DiscoveredRecord, ObservationScope, put_discovered_record

    pid = f"us.anthropic.{model_family}"
    put_discovered_record(DiscoveredRecord(
        profile_id=pid, provider="anthropic", profile_scope=profile_scope,
        model_family=model_family, jurisdiction_bounded=True,
        destination_regions=("us-east-1",), invocation_region="us-east-1",
        raw_id=pid, raw_payload={"inferenceProfileId": pid},
        observation_scope=ObservationScope(
            account="776010787911", region="us-east-1",
            credentials_fingerprint="test-fingerprint",
            observed_at="2026-09-11T00:00:00+00:00",
        ),
    ))


def _install_disagreeing_override(pricing_key: str):
    from dynamo.pricing_config import PricingConfigRepository
    from mvp import pricing
    from mvp.rates import Rate

    floor = pricing.baseline_rates()[pricing_key]
    below = Rate(
        floor.input_per_mtok_microusd, floor.output_per_mtok_microusd,
        floor.cache_read_per_mtok_microusd, floor.cache_write_per_mtok_microusd - 1,
    )
    PricingConfigRepository().set_rates(
        version=f"test-e13-{pricing_key}", rates={pricing_key: below},
    )
    pricing.reset_cache()


def test_a_disagreement_refusal_names_the_direction_and_the_two_readings(
    dynamodb_mock, registry,
):
    """A caller who does nothing but print the exception must still learn
    something usable: which reading is suspect and that ONE of the two is
    wrong, not merely that a grant did not happen. A refusal whose message
    were just "refused" or the reason token alone would pass every test in
    `test_grant_floor_e6.py` (none of them reads `str(exc)`) and tell an
    operator nothing beyond a boolean."""
    from mvp.admin_entitlements import GrantFloorRefusal, grant_entitlement

    _install_disagreeing_override(PRICING_KEY)
    _seed_discovered_record(FAMILY, SCOPE)

    with pytest.raises(GrantFloorRefusal) as exc_info:
        grant_entitlement(tenant_id=TENANT, model_family=FAMILY, profile_scope=SCOPE,
                          actor=_Actor())
    message = str(exc_info.value)
    assert PRICING_KEY in message, (
        f"the refusal's own message must name which pricing_key disagreed; "
        f"got {message!r}"
    )
    assert "below" in message and "floor" in message, (
        f"the message must state the DIRECTION of the disagreement (below "
        f"the floor), not just that a mismatch occurred; got {message!r}"
    )
    assert "wrong" in message, (
        f"the message must tell the reader that one of the two numbers is "
        f"wrong -- the action a human takes next (work out which) -- not "
        f"merely report that they differ; got {message!r}"
    )


def test_an_unreviewed_row_refusal_names_the_action_add_a_row(dynamodb_mock, registry):
    """The companion reason's message must name a DIFFERENT action than the
    disagreement case -- "add a reviewed row" is not "work out which
    reading is wrong" -- because the two reasons exist precisely so an
    operator is told which of two different jobs to do."""
    from mvp.admin_entitlements import GrantFloorRefusal, grant_entitlement

    _seed_discovered_record(UNREVIEWED_FAMILY, UNREVIEWED_SCOPE)

    with pytest.raises(GrantFloorRefusal) as exc_info:
        grant_entitlement(
            tenant_id=TENANT, model_family=UNREVIEWED_FAMILY, profile_scope=UNREVIEWED_SCOPE,
            actor=_Actor(),
        )
    message = str(exc_info.value)
    assert UNREVIEWED_PRICING_KEY in message
    assert "review" in message.lower(), (
        f"the message must tell the reader the action is to get this "
        f"pricing_key reviewed -- distinct from the disagreement case's "
        f"'work out which reading is wrong'; got {message!r}"
    )


def test_the_two_reasons_produce_distinguishable_messages(dynamodb_mock, registry):
    """Non-vacuity for the pair above: if both reasons produced the same
    templated sentence, each test could pass while the two actions were
    actually identical text, which is not what the closed, two-reason
    vocabulary is for."""
    from mvp.admin_entitlements import GrantFloorRefusal, grant_entitlement

    _install_disagreeing_override(PRICING_KEY)
    _seed_discovered_record(FAMILY, SCOPE)
    _seed_discovered_record(UNREVIEWED_FAMILY, UNREVIEWED_SCOPE)

    with pytest.raises(GrantFloorRefusal) as disagreement_exc:
        grant_entitlement(tenant_id=TENANT, model_family=FAMILY, profile_scope=SCOPE,
                          actor=_Actor())
    with pytest.raises(GrantFloorRefusal) as unreviewed_exc:
        grant_entitlement(
            tenant_id=TENANT, model_family=UNREVIEWED_FAMILY, profile_scope=UNREVIEWED_SCOPE,
            actor=_Actor(),
        )
    assert str(disagreement_exc.value) != str(unreviewed_exc.value)
