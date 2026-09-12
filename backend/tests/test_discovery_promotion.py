"""The promotion candidate store: a candidate persists, a public name can be
claimed by only one candidate at a time, and a stored row cannot become
world-reachable by losing a field.

A discovered Bedrock profile becomes a servable model in two steps: something
decides the model is safe to serve and writes a candidate row for it, and
(separately, later, gated on a probe verdict) that candidate is activated.
This file is about the first step only — the write, and what a stored row
must say about itself once it is written.

Two properties matter enough to get their own tests here. First, every name a
client could address a model by — a short alias, or the raw Bedrock model id,
which is just as callable — must be claimable by at most one candidate, and
the check has to survive two promotions racing for the same name, not merely
look right in the code that runs first. Second, whether a promoted model
requires an entitlement grant is not a per-candidate choice recorded in the
application object; it is a constant the storage schema itself asserts, and
loading a row that lost that assertion (an old restored row, a dropped
column, a hand-edited item) must not quietly hand out the permissive default.

Uses the shared `dynamodb_mock` fixture (`tests/conftest.py`) — no real AWS
call is made or needed.
"""
from __future__ import annotations

import pytest

from dynamo.client import promotion_candidates_table_name
from mvp.discovery.promotion import (
    PromotionCandidate,
    PromotionRefused,
    get_promotion_candidate,
    list_promotion_candidates,
    put_promotion_candidate,
)
from mvp.discovery.records import ObservationScope
from mvp.models import registry_entries

# The closed refusal-reason vocabulary, spelled exactly as the interface fixes
# it. Nothing that raises `PromotionRefused` may use a string outside this
# set, and every one of these strings must be one the class actually accepts.
_ALL_REFUSAL_REASONS = (
    "provider_unsupported",
    "alias_required",
    "pricing_key_required",
    "pricing_key_is_default",
    "jurisdiction_required",
    "identifier_taken",
    "protocol_mismatch",
    "record_not_found",
)


def _observation_scope(**overrides) -> ObservationScope:
    base = dict(account="776010787911", region="us-east-1",
               credentials_fingerprint="abc123", observed_at="2026-01-01T00:00:00+00:00")
    base.update(overrides)
    return ObservationScope(**base)


def _candidate(profile_id: str = "us.anthropic.claude-promo-test", **overrides) -> PromotionCandidate:
    base = dict(
        profile_id=profile_id,
        observation_scope=_observation_scope(),
        state="candidate",
        aliases=("promo-test-alias",),
        pricing_key="opus",
        jurisdiction="us",
        provider="anthropic",
        bedrock_model_id=profile_id,
        bedrock_region="us-east-1",
        wire_protocol="messages",
        model_family="anthropic.claude-promo-test",
        profile_scope="us",
        created_at="2026-01-01T00:00:00+00:00",
        created_by="test-operator",
    )
    base.update(overrides)
    return PromotionCandidate(**base)


def _an_existing_registry_alias() -> str:
    """A real alias from the bundled, code-resident registry — never invented
    — so a test that collides against it is measuring the actual repository
    rather than restating the rule in its own words."""
    for entry in registry_entries():
        if entry.aliases:
            return entry.aliases[0]
    raise AssertionError("the bundled registry has no entry with an alias to test against")


def _an_existing_registry_bedrock_id() -> str:
    """A real Bedrock model id already claimed by the bundled registry."""
    entries = registry_entries()
    assert entries, "the bundled registry has no entries to test against"
    return entries[0].bedrock_model_id


# --- a candidate persists, including a value the store cannot see is illegal
# until it actually tries to write it ----------------------------------------
def test_a_candidate_persists_and_round_trips_across_a_real_table(dynamodb_mock):
    """A candidate saved and reloaded by its profile id must come back with
    every field intact — checked against a mocked DynamoDB table, not an
    in-memory dataclass comparison, because the discovered-record store's own
    history is that a plain Python `float` is accepted everywhere in memory
    and only rejected the moment botocore actually serialises the call
    ('Float types are not supported. Use Decimal types instead.'). An
    in-memory round trip cannot see that failure; only a write through the
    mocked table can. `observation_scope.observed_at` is given a raw float
    here for exactly that reason — the field records.py's own tests exercise
    the identical way — so this test would fail with an unhandled TypeError,
    not a wrong-value assertion, if the store dropped the conversion.
    """
    candidate = _candidate(
        profile_id="us.anthropic.claude-promo-roundtrip",
        observation_scope=_observation_scope(observed_at=1_800_000_000.5),
    )
    put_promotion_candidate(candidate)
    loaded = get_promotion_candidate(candidate.profile_id)
    assert loaded is not None
    assert loaded.profile_id == candidate.profile_id
    assert loaded.state == candidate.state
    assert tuple(loaded.aliases) == candidate.aliases
    assert loaded.pricing_key == candidate.pricing_key
    assert loaded.jurisdiction == candidate.jurisdiction
    assert loaded.provider == candidate.provider
    assert loaded.bedrock_model_id == candidate.bedrock_model_id
    assert loaded.bedrock_region == candidate.bedrock_region
    assert loaded.wire_protocol == candidate.wire_protocol
    assert loaded.model_family == candidate.model_family
    assert loaded.profile_scope == candidate.profile_scope
    assert loaded.created_at == candidate.created_at
    assert loaded.created_by == candidate.created_by
    assert loaded.observation_scope.account == candidate.observation_scope.account
    # Compared through `str()` on both sides deliberately: the value crossed a
    # real DynamoDB item, which never stores a `float` (it comes back as a
    # `Decimal`, or as whatever string form the reader chose to normalise it
    # to) — this test does not pin which, only that the digits the caller
    # wrote are the digits that come back, which a truncating or
    # zeroing conversion would still fail.
    assert str(loaded.observation_scope.observed_at) == str(1_800_000_000.5)


# --- two public identifiers, one candidate at a time -------------------------
def test_two_promotions_racing_for_one_alias_the_second_loses(dynamodb_mock):
    """The reservation-row design this store exists for: uniqueness of a
    public name is a database constraint, not a check-then-write race won by
    whoever reads the listing last. The race is constructed rather than
    inspected — two candidates naming the same alias, written one after the
    other — and the claim is about the outcome (the second write is refused,
    the first is untouched), not about the transaction mechanism that
    produces it.
    """
    shared_alias = "promo-race-shared-alias"
    first = _candidate(profile_id="us.anthropic.claude-promo-race-one", aliases=(shared_alias,))
    second = _candidate(profile_id="us.anthropic.claude-promo-race-two", aliases=(shared_alias,))

    put_promotion_candidate(first)
    with pytest.raises(PromotionRefused) as exc_info:
        put_promotion_candidate(second)
    assert exc_info.value.reason == "identifier_taken"

    # The loser must have left no trace, and the winner must be exactly as it
    # was written — a partially-applied second write would be worse than a
    # clean refusal, because it would be invisible until something else
    # collided with whatever fragment landed.
    assert get_promotion_candidate(second.profile_id) is None
    winner = get_promotion_candidate(first.profile_id)
    assert winner is not None
    assert winner.aliases == (shared_alias,)


def test_a_bedrock_id_collision_refuses_exactly_as_an_alias_collision_does(dynamodb_mock):
    """A Bedrock model id is just as callable by a client as an alias is —
    nothing about `resolve_model` distinguishes the two — so promotion makes
    a candidate's Bedrock id public whether or not it also chose an alias.
    The two candidates below share NO alias (proving the alias map alone
    would not have caught this) but do share a Bedrock model id, and the
    second write must be refused with the identical exception class and
    reason as an alias collision, not some second, weaker check.
    """
    shared_bedrock_id = "us.anthropic.claude-promo-shared-bedrock-id"
    first = _candidate(profile_id="us.anthropic.claude-promo-bedrock-one",
                       bedrock_model_id=shared_bedrock_id, aliases=("promo-bedrock-alias-one",))
    second = _candidate(profile_id="us.anthropic.claude-promo-bedrock-two",
                        bedrock_model_id=shared_bedrock_id, aliases=("promo-bedrock-alias-two",))

    put_promotion_candidate(first)
    with pytest.raises(PromotionRefused) as exc_info:
        put_promotion_candidate(second)
    assert exc_info.value.reason == "identifier_taken"
    assert get_promotion_candidate(second.profile_id) is None


def test_a_collision_against_the_bundled_registrys_alias_refuses_too(dynamodb_mock):
    """Collision is checked against every public name already in service, not
    only against other candidates in this store — `_ALIAS_MAP` is the flat,
    silently-last-writer-wins dict a duplicate today does not raise on, and
    this is the check that closes it for a NEW candidate. A real alias is
    used, pulled from the bundled registry itself, rather than an invented
    string that merely asserts the rule back at itself: this test fails if
    the registry ever stops shipping any aliased entry, which is the
    correct failure — it means the premise this test measures no longer
    holds, not that it should be faked.
    """
    existing_alias = _an_existing_registry_alias()
    candidate = _candidate(profile_id="us.anthropic.claude-promo-registry-alias-clash",
                           aliases=(existing_alias,))
    with pytest.raises(PromotionRefused) as exc_info:
        put_promotion_candidate(candidate)
    assert exc_info.value.reason == "identifier_taken"
    assert get_promotion_candidate(candidate.profile_id) is None


def test_a_collision_against_the_bundled_registrys_bedrock_id_refuses_too(dynamodb_mock):
    """The same check, against `_BEDROCK_ID_MAP` rather than `_ALIAS_MAP` — a
    candidate whose alias is brand new but whose Bedrock model id already
    belongs to a shipped registry entry must be refused just the same,
    because that id is already a way a client can reach a model today.
    """
    existing_bedrock_id = _an_existing_registry_bedrock_id()
    candidate = _candidate(profile_id="us.anthropic.claude-promo-registry-bedrock-clash",
                           bedrock_model_id=existing_bedrock_id,
                           aliases=("promo-brand-new-nonclashing-alias",))
    with pytest.raises(PromotionRefused) as exc_info:
        put_promotion_candidate(candidate)
    assert exc_info.value.reason == "identifier_taken"
    assert get_promotion_candidate(candidate.profile_id) is None


# --- the refusal vocabulary is closed, and every member of it is real -------
def test_the_eight_closed_refusal_reasons_are_pairwise_distinct_strings():
    """A sanity check on the vocabulary itself before anything tries to raise
    it: eight reasons that are supposed to be individually diagnosable must
    actually be eight different strings, not two spellings of the same idea
    or a copy-paste duplicate."""
    assert len(set(_ALL_REFUSAL_REASONS)) == len(_ALL_REFUSAL_REASONS), _ALL_REFUSAL_REASONS


@pytest.mark.parametrize("reason", _ALL_REFUSAL_REASONS)
def test_each_closed_refusal_reason_can_actually_be_raised(reason):
    """Every one of the eight reasons this change promises can happen must be
    a reason `PromotionRefused` will actually accept — a reason string
    nothing can construct is a promise the code cannot keep. This is checked
    at the exception's own construction, not by driving every write-time
    check that would use it in production (most of those checks belong to
    later work not built yet); what is verified here is the narrower but
    still load-bearing fact that the closed set the class enforces is
    exactly this set, with no member silently unreachable.
    """
    refusal = PromotionRefused(reason=reason)
    assert refusal.reason == reason


def test_a_reason_outside_the_closed_set_is_itself_refused():
    """The other half of "closed": a string that is not one of the eight must
    be rejected by the exception's own constructor, not accepted and carried
    through as if the vocabulary were open. Without this, a future typo in a
    call site would silently mint a ninth reason nothing here promised."""
    with pytest.raises(ValueError):
        PromotionRefused(reason="not_a_real_promotion_refusal_reason")


# --- access is a schema fact, not a per-candidate choice, and load enforces it
def test_promotion_always_writes_entitlement_required_access_in_the_stored_row(dynamodb_mock):
    """`PromotionCandidate` carries no `access` field at all — there is no
    per-candidate choice to make — because every promoted candidate is
    `entitlement_required` by construction. Asserted directly against the
    stored item at its documented key, not through the reader, so a writer
    that got this right only by accident of what the reader happens to
    default to would still be caught here.
    """
    candidate = _candidate(profile_id="us.anthropic.claude-promo-access-write")
    put_promotion_candidate(candidate)
    table = dynamodb_mock.Table(promotion_candidates_table_name())
    item = table.get_item(Key={
        "pk": f"CANDIDATE#{candidate.profile_id}", "sk": "CANDIDATE",
    }).get("Item")
    assert item is not None, (
        "no item was written at pk='CANDIDATE#{profile_id}', sk='CANDIDATE' — "
        "the exact key scheme the interface fixes for a candidate row"
    )
    assert item.get("access") == "entitlement_required", (
        f"expected the stored row's access column to read 'entitlement_required' "
        f"unconditionally, got {item.get('access')!r}"
    )


def test_a_stored_row_with_no_access_field_is_not_returned_as_a_valid_candidate(dynamodb_mock):
    """`access` defaults to the permissive `'general'` on `ModelEntry`, so a
    row that lost the column for any reason not caused by promotion choosing
    it — a dropped attribute, an older restored row, a hand edit — must not
    quietly read back as a normal, servable candidate. Constructed by
    writing a real candidate through the writer and then stripping the
    column directly on the mocked table, bypassing `put_promotion_candidate`
    entirely: a test that only ever wrote through the writer could prove the
    writer is careful, never that the loader is.
    """
    candidate = _candidate(profile_id="us.anthropic.claude-promo-access-missing")
    put_promotion_candidate(candidate)
    table = dynamodb_mock.Table(promotion_candidates_table_name())
    table.update_item(
        Key={"pk": f"CANDIDATE#{candidate.profile_id}", "sk": "CANDIDATE"},
        UpdateExpression="REMOVE access",
    )
    assert get_promotion_candidate(candidate.profile_id) is None, (
        "a stored row with no access column at all was returned as if it were "
        "an ordinary candidate"
    )
    assert candidate.profile_id not in {c.profile_id for c in list_promotion_candidates()}, (
        "a stored row with no access column at all appeared in the listing"
    )


def test_a_stored_row_saying_general_access_is_not_returned_as_a_valid_candidate(dynamodb_mock):
    """The other half of the same defence: a row that explicitly says
    `access='general'` — the exact value `ModelEntry.access` defaults to —
    must be refused identically to a missing column, not treated as a
    weaker or different case. If load only checked for the column's
    presence, this row would sail through with the permissive value intact.
    """
    candidate = _candidate(profile_id="us.anthropic.claude-promo-access-general")
    put_promotion_candidate(candidate)
    table = dynamodb_mock.Table(promotion_candidates_table_name())
    table.update_item(
        Key={"pk": f"CANDIDATE#{candidate.profile_id}", "sk": "CANDIDATE"},
        UpdateExpression="SET access = :general",
        ExpressionAttributeValues={":general": "general"},
    )
    assert get_promotion_candidate(candidate.profile_id) is None, (
        "a stored row explicitly saying access='general' was returned as if "
        "it were an ordinary candidate"
    )
    assert candidate.profile_id not in {c.profile_id for c in list_promotion_candidates()}, (
        "a stored row saying access='general' appeared in the listing"
    )
