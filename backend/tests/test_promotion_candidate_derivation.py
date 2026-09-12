"""Turning one discovered Bedrock inference profile into a promotion
candidate: which fields a machine may fill in, which ones only a human may
decide, and what a candidate has to carry so a later safety mechanism can
find it again.

**A named interface gap, resolved by picking concrete answers.** The design
this file was written against fixes `PromotionCandidate`'s field names, the
storage module (`mvp.discovery.promotion`), and the closed set of refusal
reasons a bad promotion input may raise with, but it does not name the
function that turns a discovered record plus a human's choices into one
candidate — only the lower-level store verbs sit below it. This file commits
to a `derive_candidate` function in that module:

    derive_candidate(
        record: DiscoveredRecord, *, aliases: Sequence[str] | None,
        pricing_key: str | None, jurisdiction: str | None, created_by: str,
        probe_wire_protocol: str, created_at: str | None = None,
    ) -> PromotionCandidate

...raising `PromotionRefused(reason=...)` for a bad input, never touching the
candidate store itself (that belongs to the lower-level write, which also
carries the identifier-collision check this file does not repeat). It also
commits to two small, pure reads of an already-built candidate, because
reporting which names go live is a fact about the candidate's own fields and
does not need to be threaded through validation to be answered:

    newly_live_identifiers(candidate: PromotionCandidate) -> tuple[str, ...]
    default_model_collision_warning(candidate: PromotionCandidate) -> str | None

If the landed interface names any of these differently, that is a naming
difference for whoever reconciles the two sides, not a behavioural one this
file gets to paper over: every assertion below is about which reason a given
input refuses with, and which values a good input produces — checked against
the bundled registry and pricing documents wherever the fact under test is
something this repository already measured, rather than restated by hand.

One reason string this file uses is itself a judgement call rather than
something the design spells out: a record with no real identity (an empty
profile id) is treated as `record_not_found`, on the reading that a profile
id is how this whole package names "which record" — nothing else in the
closed set fits an incomplete identity, and this is flagged as a guess, not
a citation.
"""
from __future__ import annotations

import pytest

from mvp.discovery.promotion import (
    PromotionCandidate,
    PromotionRefused,
    default_model_collision_warning,
    derive_candidate,
    newly_live_identifiers,
)
from mvp.discovery.records import DiscoveredRecord, ObservationScope
from mvp.models import DEFAULT_MODEL, registry_entries


def _scope(**overrides) -> ObservationScope:
    base = dict(account="776010787911", region="us-east-1",
                credentials_fingerprint="abc123",
                observed_at="2026-09-01T00:00:00+00:00")
    base.update(overrides)
    return ObservationScope(**base)


def _record(**overrides) -> DiscoveredRecord:
    """A record shaped like a real, already-served Claude Opus 5 profile
    (`defaults/models.json`'s own `us.anthropic.claude-opus-5` entry) rather
    than an invented shape, so the derivation tests below measure this
    repository's own data instead of a fixture nobody could mistake for a
    live profile."""
    base = dict(
        profile_id="us.anthropic.claude-opus-5",
        provider="anthropic",
        profile_scope="us",
        model_family="claude-opus-5",
        jurisdiction_bounded=True,
        destination_regions=("us-east-1", "us-east-2", "us-west-2"),
        invocation_region="us-east-1",
        raw_id="us.anthropic.claude-opus-5",
        raw_payload={"inferenceProfileId": "us.anthropic.claude-opus-5", "status": "ACTIVE"},
        observation_scope=_scope(),
        blockers=(),
    )
    base.update(overrides)
    return DiscoveredRecord(**base)


def _valid_inputs(**overrides) -> dict:
    """Every argument `derive_candidate` needs to succeed, so a test that
    means to break exactly one of them does not accidentally also trip a
    different, unrelated refusal."""
    base = dict(
        aliases=("claude-opus-5-promoted",),
        pricing_key="opus",
        jurisdiction="us",
        created_by="operator@example.com",
        probe_wire_protocol="messages",
    )
    base.update(overrides)
    return base


# --- the mechanical fields derive from a real record ------------------------

def test_bedrock_id_and_region_derive_from_the_records_own_fields():
    """The two purely mechanical fields are read off the record that was
    actually observed, not re-typed by a human and not looked up in a second
    document: the Bedrock id this profile will be invoked under is the
    record's own `raw_id` (the id `ListInferenceProfiles` reported), and the
    region is where this pass actually reached the account from."""
    record = _record()
    candidate = derive_candidate(record, **_valid_inputs())
    assert candidate.bedrock_model_id == record.raw_id
    assert candidate.bedrock_region == record.invocation_region


def test_the_wire_protocol_a_correctly_verified_provider_gets_is_the_one_the_probe_spoke():
    """Anthropic's own registry entries are all measured, right now, in this
    repository's bundled `defaults/models.json`, to speak the `messages`
    protocol (never `responses`) -- read from `registry_entries()` rather
    than assumed, so this test would notice if that ever stopped being true.
    A promotion for an Anthropic profile whose probe also spoke `messages`
    must agree with that measurement and succeed with it."""
    anthropic_protocols = {e.wire_protocol for e in registry_entries() if e.provider == "anthropic"}
    assert anthropic_protocols == {"messages"}, (
        "fixture assumption broken: the bundled registry no longer measures "
        "every Anthropic entry at the 'messages' protocol"
    )
    candidate = derive_candidate(_record(), **_valid_inputs(probe_wire_protocol="messages"))
    assert candidate.wire_protocol == "messages"


def test_a_protocol_the_gateway_does_not_speak_refuses():
    """The protocol comes from what the probe actually spoke and succeeded
    with, so this layer has nothing to compare it against and does not try:
    if the probe succeeded on a protocol the provider is not conventionally
    associated with, the probe's evidence is the fact and the convention is
    the guess. What this layer still owes is that the value is one the
    gateway can actually speak -- a string outside the two it implements
    would build an entry that resolves and then fails on the wire.

    The agreement check that does exist compares a candidate against the
    verdict it was verified under, and lives where both are in hand; it can
    only disagree if one of them is edited after the fact."""
    with pytest.raises(PromotionRefused) as exc:
        derive_candidate(_record(), **_valid_inputs(probe_wire_protocol="grpc"))
    assert exc.value.reason == "protocol_mismatch"


def test_a_provider_outside_the_closed_six_refuses():
    """`_PROVIDERS` in `mvp.models` names exactly six providers this gateway
    can invoke at all; a client adapter is what makes a provider callable,
    and admitting a promotion for one with no adapter produces an entry that
    resolves and then fails on the wire. `stability` is a real, live example:
    this repository's own discovery fixtures (`tests/fixtures/discovery/
    model_details_stability.json`) already carry a Stability profile this
    gateway cannot invoke."""
    from mvp.models import _PROVIDERS

    assert "stability" not in _PROVIDERS, (
        "fixture assumption broken: 'stability' is now one of the six "
        "supported providers"
    )
    record = _record(profile_id="stability.sd3-5-large-v1:0", provider="stability",
                      raw_id="stability.sd3-5-large-v1:0",
                      model_family="stability.sd3-5-large-v1:0", profile_scope="us")
    with pytest.raises(PromotionRefused) as exc:
        derive_candidate(record, **_valid_inputs())
    assert exc.value.reason == "provider_unsupported"


def test_a_supported_provider_whose_real_protocol_is_not_the_obvious_guess_still_succeeds():
    """A per-provider table would be tempting to write by pattern-matching
    provider families (Anthropic and OpenAI-shaped providers alike), and
    would be wrong today: this repository's own bundled registry measures
    both `nvidia` and `qwen` at the `messages` protocol, not `responses`,
    despite neither being Anthropic. A promotion whose probe actually spoke
    `messages` for an `nvidia` profile must succeed, which only holds if the
    verification reads the probe's own evidence rather than a family guess."""
    nvidia_protocols = {e.wire_protocol for e in registry_entries() if e.provider == "nvidia"}
    assert nvidia_protocols == {"messages"}, (
        "fixture assumption broken: the bundled registry no longer measures "
        "the nvidia entry at the 'messages' protocol"
    )
    record = _record(profile_id="nvidia.nemotron-super-3-120b", provider="nvidia",
                      raw_id="nvidia.nemotron-super-3-120b",
                      model_family="nvidia.nemotron-super-3-120b", profile_scope="us")
    candidate = derive_candidate(record, **_valid_inputs(probe_wire_protocol="messages"))
    assert candidate.wire_protocol == "messages"


# --- the three human inputs, each required for its own reason ---------------

def test_missing_aliases_refuses_on_its_own_reason():
    with pytest.raises(PromotionRefused) as exc:
        derive_candidate(_record(), **_valid_inputs(aliases=()))
    assert exc.value.reason == "alias_required"


def test_missing_pricing_key_refuses_on_its_own_reason():
    with pytest.raises(PromotionRefused) as exc:
        derive_candidate(_record(), **_valid_inputs(pricing_key=None))
    assert exc.value.reason == "pricing_key_required"


def test_missing_jurisdiction_refuses_on_its_own_reason():
    """Required unconditionally, with no default, because `None` is itself a
    decision (the profile is unbounded) rather than the absence of one -- a
    promotion that never says which reading it means must not silently pick
    the unbounded one."""
    with pytest.raises(PromotionRefused) as exc:
        derive_candidate(_record(), **_valid_inputs(jurisdiction=None))
    assert exc.value.reason == "jurisdiction_required"


def test_pricing_key_of_default_refuses_on_its_own_reason_rather_than_being_accepted():
    with pytest.raises(PromotionRefused) as exc:
        derive_candidate(_record(), **_valid_inputs(pricing_key="default"))
    assert exc.value.reason == "pricing_key_is_default"


def test_pricing_key_of_default_would_undercharge_every_other_priced_model_if_accepted():
    """The refusal above is not pedantry about a magic string -- it is
    protecting a real number. This repository's own bundled floor
    (`defaults/pricing.json`, read here through `mvp.pricing.baseline_rates`)
    prices `default` at or above every other real pricing key on every
    billed leg (input, output, cache read, cache write) precisely so an
    unpriced model over-charges rather than under-charges. If a promotion
    were allowed to land on `default`, that guarantee would still hold for
    THIS model, but only because `default` happens to be the dearest key in
    the table -- for every model that shares a cheaper key today, an
    operator who later notices the mistake and moves it off `default` would
    find it had been over-charged in the meantime, and there is no floor row
    reviewed specifically for what this model should cost. Measured against
    the repository's own data rather than restated as a rule, because a test
    that only re-asserts "default is refused" would still pass if the floor
    document's own domination property silently broke."""
    from mvp.pricing import baseline_rates
    from mvp.rates import RATE_FIELDS

    floor = baseline_rates()
    default = floor["default"]
    for key, rate in floor.items():
        if key in {"default", "vllm"}:
            continue
        for leg in RATE_FIELDS:
            assert getattr(rate, leg) <= getattr(default, leg), (
                f"fixture assumption broken: {key}.{leg} out-prices 'default', "
                f"so refusing 'default' would no longer be the conservative "
                f"side of the mistake"
            )


# --- the identity a later safety mechanism will need to find this row again -

def test_the_source_records_identity_survives_into_the_candidate():
    """A candidate that forgets which record and which observation it rests
    on cannot be found again by anything that watches for a reason to pull
    it back out of service -- the profile id and the full observation scope
    (which account, which region, whose credentials, and when) have to
    survive derivation exactly, not merely "some profile", "some scope"."""
    record = _record()
    candidate = derive_candidate(record, **_valid_inputs())
    assert candidate.profile_id == record.profile_id
    assert candidate.observation_scope == record.observation_scope
    assert candidate.state == "candidate"


def test_a_record_with_no_real_identity_refuses_rather_than_producing_an_unfindable_candidate():
    """A blank profile id names nothing a later safety mechanism could ever
    match against -- there is no record to point back to, so this is read as
    the closed set's `record_not_found` rather than let through to produce a
    candidate nothing could ever pull back out of service. (This mapping is
    this file's own judgement call, not something the design names -- see
    the module docstring.)"""
    record = _record(profile_id="", raw_id="")
    with pytest.raises(PromotionRefused) as exc:
        derive_candidate(record, **_valid_inputs())
    assert exc.value.reason == "record_not_found"


# --- every identifier a promotion makes live is reported, including the ----
# --- one nobody asked for: the deployment's own default model name --------

def test_the_reported_identifiers_include_both_the_alias_and_the_bedrock_id():
    candidate = derive_candidate(_record(), **_valid_inputs(aliases=("claude-opus-5-promoted",)))
    identifiers = newly_live_identifiers(candidate)
    assert "claude-opus-5-promoted" in identifiers
    assert candidate.bedrock_model_id in identifiers


def test_promoting_the_configured_default_bedrock_id_is_reported_as_a_new_way_to_reach_it():
    """`resolve_model` substitutes the deployment's own configured default
    (`DEFAULT_MODEL`, read here rather than assumed) whenever a request names
    no model at all. Promoting an id that happens to equal that default turns
    a request with an empty or absent model name from a failure into a
    success -- a real behaviour change for every such caller, not merely a
    new model becoming reachable by name. Built from the CURRENT value of
    `DEFAULT_MODEL` rather than a literal copied from today's config, so this
    test survives that value changing."""
    record = _record(profile_id=DEFAULT_MODEL, raw_id=DEFAULT_MODEL,
                      model_family="promoted-default-model")
    candidate = derive_candidate(record, **_valid_inputs(aliases=("promoted-default-alias",)))
    assert candidate.bedrock_model_id == DEFAULT_MODEL
    warning = default_model_collision_warning(candidate)
    assert warning is not None, (
        "promoting the id that equals DEFAULT_MODEL produced no warning -- "
        "a request naming no model at all will now silently resolve here"
    )
    assert DEFAULT_MODEL in warning


def test_promoting_the_default_via_an_alias_is_reported_the_same_way():
    """The consequence is about which NAME resolves, not specifically about
    `bedrock_model_id` -- `resolve_model` checks the alias map first, so an
    alias that happens to equal `DEFAULT_MODEL` has exactly the same effect
    on an empty-model request as the bedrock id having it."""
    record = _record(profile_id="some.other.profile-id",
                      raw_id="some.other.profile-id",
                      model_family="unrelated-model")
    candidate = derive_candidate(record, **_valid_inputs(aliases=(DEFAULT_MODEL,)))
    warning = default_model_collision_warning(candidate)
    assert warning is not None
    assert DEFAULT_MODEL in warning


def test_promoting_an_ordinary_new_identifier_carries_no_default_model_warning():
    """The negative control: nothing about an ordinary promotion should ever
    mention the deployment's default model, so a warning that always fires
    would pass every test above by accident."""
    candidate = derive_candidate(_record(), **_valid_inputs(
        aliases=("a-perfectly-ordinary-new-alias",)))
    assert DEFAULT_MODEL not in newly_live_identifiers(candidate)
    assert default_model_collision_warning(candidate) is None
