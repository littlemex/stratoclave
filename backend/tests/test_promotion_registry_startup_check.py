"""The check that runs once, at process start, over every stored promotion
candidate together with the code-resident model registry -- the half of the
uniqueness guarantee a single candidate's own write can never see for
itself, because the thing it collides with does not exist yet at write time:
a name added to the registry by a later code deploy. Also the place a row
this build can no longer read has to be handled without either hiding it or
holding the whole fleet hostage to it.

**A named interface gap, resolved by picking a concrete answer.** The design
this file was written against fixes the candidate store's key scheme (`pk`,
`sk`, a candidate row at `pk="CANDIDATE#<profile_id>"`/`sk="CANDIDATE"`, and a
`schema_version` matching `mvp.discovery.records.SCHEMA_VERSION`'s own
convention) but does not name the function that re-reads the table at start
and compares it against the registry. This file commits to a
`check_registry_at_start` function living in `mvp.discovery.promotion`
alongside the store:

    check_registry_at_start(*, registry: tuple[ModelEntry, ...] | None = None,
                             ) -> tuple[str, ...]

...returning every profile id it had to quarantine (a stored row it could not
re-parse), and raising `ValueError` naming the colliding identifier when a
live candidate's alias or Bedrock id collides with the registry. `registry`
defaults to the real `mvp.models.registry_entries()`; the tests below pass a
small, direct-constructed one instead of editing the bundled document, the
same direct-construction pattern `mvp.models.ModelEntry`'s own module already
documents for callers that predate a field. If the landed function reads the
table through a different name, or through the store's own listing function
instead of its own read, that is a naming difference for whoever reconciles
the two sides to fix, not a behavioural one this file gets to paper over --
every assertion below is about what a person or a deploy sees when this
check runs, not about which line of code produced it.

Uses the shared `dynamodb_mock` fixture (`tests/conftest.py`), extended with
the promotion-candidates table this design adds, the same convention the
discovery-record store's own tests already use.
"""
from __future__ import annotations

import pytest

from dynamo.client import get_dynamodb_resource, table_name
from mvp.discovery.promotion import PromotionCandidate, check_registry_at_start, put_promotion_candidate
from mvp.discovery.records import ObservationScope
from mvp.models import ModelEntry, registry_entries

_TABLE_ENV = "PROMOTION_CANDIDATES_TABLE"
_TABLE_FALLBACK = "stratoclave-promotion-candidates"


def _table():
    return get_dynamodb_resource().Table(table_name(_TABLE_ENV, _TABLE_FALLBACK))


def _scope(**overrides) -> ObservationScope:
    base = dict(account="776010787911", region="us-east-1",
                credentials_fingerprint="abc123",
                observed_at="2026-09-01T00:00:00+00:00")
    base.update(overrides)
    return ObservationScope(**base)


def _candidate(profile_id: str, *, alias: str, bedrock_model_id: str,
               **overrides) -> PromotionCandidate:
    base = dict(
        profile_id=profile_id,
        observation_scope=_scope(),
        state="candidate",
        aliases=(alias,),
        pricing_key="opus",
        jurisdiction="us",
        provider="anthropic",
        bedrock_model_id=bedrock_model_id,
        bedrock_region="us-east-1",
        wire_protocol="messages",
        model_family=profile_id,
        profile_scope="us",
        created_at="2026-09-01T00:00:00+00:00",
        created_by="operator@example.com",
    )
    base.update(overrides)
    return PromotionCandidate(**base)


def _write_unparseable_row(profile_id: str, dynamodb_mock) -> None:
    """A row this build cannot make sense of: same key scheme as a real
    candidate, but a `schema_version` no reader of this table understands --
    the same convention `mvp.discovery.records` already uses to recognise a
    row it must not guess at. What else the row carries deliberately does not
    matter; a reader that correctly refuses to guess at an unknown schema
    would quarantine this no matter what other fields are (or are not)
    present."""
    _table().put_item(Item={
        "pk": f"CANDIDATE#{profile_id}", "sk": "CANDIDATE",
        "schema_version": 999, "profile_id": profile_id,
    })


# --- a row this build cannot re-parse is quarantined, not skipped, not fatal -

def test_an_unparseable_stored_row_does_not_crash_process_start(dynamodb_mock):
    _write_unparseable_row("unparseable.profile-1", dynamodb_mock)
    check_registry_at_start()  # must not raise


def test_an_unparseable_stored_row_is_named_rather_than_disappearing(dynamodb_mock):
    """The alternative to crashing is not silence: a row nobody can read is
    not evidence that nothing is wrong, and a check that only avoided raising
    would leave that fact undiscoverable forever."""
    _write_unparseable_row("unparseable.profile-2", dynamodb_mock)
    quarantined = check_registry_at_start()
    assert "unparseable.profile-2" in {q.profile_id for q in quarantined}


def test_a_good_row_beside_an_unparseable_one_is_not_swallowed_by_the_same_check(dynamodb_mock):
    """The two outcomes (readable, quarantined) have to be distinguishable in
    the same pass -- a check that quarantined everything the moment anything
    was unreadable would pass the two tests above by accident."""
    _write_unparseable_row("unparseable.profile-3", dynamodb_mock)
    put_promotion_candidate(_candidate(
        "readable.profile-1", alias="readable-profile-one",
        bedrock_model_id="us.anthropic.readable-profile-one",
    ))
    quarantined = check_registry_at_start()
    ids = {q.profile_id for q in quarantined}
    assert "unparseable.profile-3" in ids
    assert "readable.profile-1" not in ids


# --- a collision only visible once the candidate and the registry are ------
# --- composed together fails the process, naming the colliding identifier --

def test_a_candidates_alias_colliding_with_a_registry_alias_fails_with_the_name_in_the_message(
    dynamodb_mock,
):
    """The direction a single candidate's own write can never see: nothing
    was wrong when this candidate was written, and nothing about writing it
    again would be wrong either -- the collision only exists once it is read
    back together with a registry that, in this test, stands in for one a
    later code deploy changed. A fabricated `ModelEntry` (direct
    construction, not an edit to the bundled document) stands in for that
    deploy rather than this test mutating `defaults/models.json`."""
    put_promotion_candidate(_candidate(
        "collision.profile-alias", alias="totally-new-test-alias-e2",
        bedrock_model_id="us.anthropic.collision-profile-alias",
    ))
    colliding_registry = (
        ModelEntry(
            provider="anthropic", bedrock_model_id="us.anthropic.some-other-model",
            bedrock_region="us-east-1", aliases=("totally-new-test-alias-e2",),
            wire_protocol="messages", pricing_key="opus", profile_scope="us",
            model_family="some-other-model", access="general",
            jurisdiction_bounded=True, jurisdiction="us",
        ),
    )
    with pytest.raises(ValueError) as exc:
        check_registry_at_start(registry=colliding_registry)
    assert "totally-new-test-alias-e2" in str(exc.value)


def test_a_candidates_bedrock_id_colliding_with_a_registry_bedrock_id_fails_with_the_name_in_the_message(
    dynamodb_mock,
):
    put_promotion_candidate(_candidate(
        "collision.profile-bedrock-id", alias="totally-new-test-alias-e3",
        bedrock_model_id="us.anthropic.shared-with-registry",
    ))
    colliding_registry = (
        ModelEntry(
            provider="anthropic", bedrock_model_id="us.anthropic.shared-with-registry",
            bedrock_region="us-east-1", aliases=("some-other-alias-e3",),
            wire_protocol="messages", pricing_key="opus", profile_scope="us",
            model_family="some-other-model-e3", access="general",
            jurisdiction_bounded=True, jurisdiction="us",
        ),
    )
    with pytest.raises(ValueError) as exc:
        check_registry_at_start(registry=colliding_registry)
    assert "us.anthropic.shared-with-registry" in str(exc.value)


def test_a_candidate_with_no_colliding_name_passes_against_the_real_bundled_registry(
    dynamodb_mock,
):
    """The negative control against the real registry rather than a
    fabricated one: an ordinary promotion, checked against
    `registry_entries()` exactly as process start would, must not fail."""
    put_promotion_candidate(_candidate(
        "no-collision.profile-1", alias="totally-unique-test-alias-e4",
        bedrock_model_id="us.anthropic.totally-unique-test-model-e4",
    ))
    check_registry_at_start(registry=registry_entries())  # must not raise


def test_an_empty_candidate_store_passes_at_start(dynamodb_mock):
    """The base case: no candidate has ever been written, so there is
    nothing to compose and nothing to quarantine."""
    assert check_registry_at_start() == ()
