"""The operator queue: every discovered blocker an operator can actually act
on, and nothing else.

Written from the frozen cross-unit decision alone, split-impl style: the
code author is blind to this file, and this file is blind to their code.
`mvp.discovery.queue` does not exist on the commit this file was written
against, so every test below is expected to be RED until it lands -- that
is the point of writing it first, not a sign this file is wrong.

WHY A QUEUE AT ALL: a blocker nobody reads does not exist. The record
(`DiscoveredRecord.blockers`, already shipped) and the grant refusal
(`mvp.admin_entitlements.GrantFloorRefusal`, already shipped) are the other
two surfaces a blocker can reach; this file is only about the third one --
a place an operator can look and see everything that still needs a human,
without reading every discovered record one at a time.

THE ONE RULE THAT MAKES THE QUEUE TRUSTWORTHY, AND WHY THIS FILE TESTS IT
THE WAY IT DOES: `mvp.discovery.reconcile` already classifies a blocker as
actionable or permanent, by `(type, subtype)` -- an operator can do
something about `no_model_access` (click a console button) or
`price_dimensions_unknown` (a build needs to learn a new dimension), and can
do nothing about `unsupported_output_modality` or a rate card's own
`no_token_pricing` shape. `no_agreement_offer`'s `not_marketplace_metered`
subtype is carved out of that type as PERMANENT for a reason worth
restating here: it is the ordinary, correct, forever shape of every
AWS-billed model family in the account (Nova, Titan, Llama, Mistral,
Anthropic Claude), so a queue that showed it would be permanently full of
things nobody can fix -- which trains an operator to stop reading it, the
exact failure a queue exists to prevent.

An operator reading this queue and an operator reading `--strict`'s exit
code must never get a different answer about which profile needs
attention, because both readings drive the same action (an operator
follows up). The tests below therefore build ONE fixture and read it
through BOTH surfaces -- `mvp.discovery.reconcile.run_pass`/`main`, and
whatever this file names as the queue -- rather than asserting each
surface separately against its own copy of the expectation. Two separate
assertions would not catch the two surfaces disagreeing with EACH OTHER
while each still looks locally correct; comparing them on the same input
is the only form of this test that would.

WHERE THE PREDICATE ACTUALLY LIVES, MEASURED RATHER THAN ASSUMED: the
frozen decision says to consume the reconciliation's actionability
predicate rather than write a second one, and to say so here rather than
duplicate it if it is not importable. It already is:
`mvp.discovery.reconcile.is_actionable_blocker` is a leading-underscore name, but
that is a convention, not an import guard -- `from mvp.discovery.reconcile
import is_actionable_blocker` succeeds today, verified directly against this
checkout, with no repository change. The tests below import it by that
name for exactly that reason, rather than inventing a public re-export this
predicate does not need.

WHAT THIS FILE DOES NOT NAME: the queue's own module and function name
(`mvp.discovery.queue.list_actionable_blockers`) and its return shape
(profile_id paired with the real `Blocker` the record already carries) are
this file's own choice, not a name either frozen document spells -- neither
names an HTTP route, a response body, or a permission-gated surface for it
either, though `backend/mvp/admin_discovery.py` (following the
`admin_<domain>.py` convention every other admin surface in this repo uses)
is the most likely home for whichever endpoint calls it. That gap is
reported rather than guessed at any deeper: this file pins the property
that has to be true of the queue -- same predicate, same input, same
answer as the CLI -- at the layer where that property actually lives,
without also inventing a route path and a JSON body shape nothing named.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mvp.discovery.reconcile import is_actionable_blocker, main, run_pass
from mvp.discovery.records import list_discovered_records

_FIXTURES = Path(__file__).parent / "fixtures" / "discovery"
_PRICING_FIXTURES = Path(__file__).parent / "fixtures" / "pricing_feeds"

# Real Bedrock error text this classifier already matches on
# (`mvp.pricing_feeds.agreement._NOT_MARKETPLACE` / `_NOT_AUTHORIZED`) --
# not reinvented here, just the shape a live exception's message takes.
_NOT_MARKETPLACE_MESSAGE = "ValidationException: Agreement not supported for this model."
_NOT_AUTHORIZED_MESSAGE = (
    "AccessDeniedException: User: arn:aws:iam::776010787911:user/ops is not "
    "authorized to invoke this API operation."
)


class _QueueFakeBedrock:
    """Five profiles, each isolating one cell of the actionable/permanent x
    single/mixed-blocker space this file needs:

    - `us.anthropic.claude-fable-5` -- clean. Reuses the real fixtures
      `test_discovery_reconcile.py` already validates produce zero
      blockers, rather than hand-building a token-priced rate card here.
    - `stability.sd3-5-large-v1:0` -- two PERMANENT blockers
      (`unsupported_output_modality`, `no_token_pricing`), same real
      fixtures as above. Proves a record can carry more than one blocker
      and still owe the queue nothing.
    - `us.meta.llama-family-v1` -- one PERMANENT blocker
      (`no_agreement_offer`/`not_marketplace_metered`), the carve-out this
      whole predicate exists for: the normal, forever shape of an
      AWS-billed family, not a fixable anomaly.
    - `us.acme.widget-v1` -- one ACTIONABLE blocker (`no_model_access`/
      `not_authorized`), and nothing else.
    - `us.acme.imagegen-v1` -- BOTH: a PERMANENT `unsupported_output_
      modality` (its output is `IMAGE`) and an ACTIONABLE `no_model_access`
      on the very same record. This is the pairing that proves the queue
      filters by BLOCKER, not by record: a record with a permanent blocker
      does not get a free pass on an actionable one it also carries.
    """

    def __init__(self):
        self._model_details = {
            "anthropic.claude-fable-5": json.loads(
                (_FIXTURES / "model_details_fable5.json").read_text()),
            "stability.sd3-5-large-v1:0": json.loads(
                (_FIXTURES / "model_details_stability.json").read_text()),
            "meta.llama-family-v1": {"outputModalities": ["TEXT"]},
            "acme.widget-v1": {"outputModalities": ["TEXT"]},
            "acme.imagegen-v1": {"outputModalities": ["IMAGE"]},
        }
        self._agreement_offers = json.loads(
            (_PRICING_FIXTURES / "agreement_sonnet46.json").read_text())
        self._agreement_stability = json.loads(
            (_FIXTURES / "agreement_stability.json").read_text())

    def list_inference_profiles(self, **kwargs):
        def _arn(model_id):
            return {"modelArn": f"arn:aws:bedrock:us-east-1::foundation-model/{model_id}"}

        return {"inferenceProfileSummaries": [
            {"inferenceProfileId": "us.anthropic.claude-fable-5",
             "models": [_arn("anthropic.claude-fable-5")]},
            {"inferenceProfileId": "stability.sd3-5-large-v1:0",
             "models": [_arn("stability.sd3-5-large-v1:0")]},
            {"inferenceProfileId": "us.meta.llama-family-v1",
             "models": [_arn("meta.llama-family-v1")]},
            {"inferenceProfileId": "us.acme.widget-v1",
             "models": [_arn("acme.widget-v1")]},
            {"inferenceProfileId": "us.acme.imagegen-v1",
             "models": [_arn("acme.imagegen-v1")]},
        ]}

    def get_foundation_model(self, modelIdentifier):  # noqa: N803 — boto3 name
        return {"modelDetails": self._model_details[modelIdentifier]}

    def list_foundation_model_agreement_offers(self, modelId):  # noqa: N803
        if modelId == "anthropic.claude-fable-5":
            return self._agreement_offers
        if modelId == "stability.sd3-5-large-v1:0":
            return self._agreement_stability
        if modelId == "meta.llama-family-v1":
            raise Exception(_NOT_MARKETPLACE_MESSAGE)
        if modelId in ("acme.widget-v1", "acme.imagegen-v1"):
            raise Exception(_NOT_AUTHORIZED_MESSAGE)
        raise AssertionError(f"unexpected modelId {modelId!r} in this fixture")


class _QueueFakeSTS:
    def get_caller_identity(self):
        return {"Account": "776010787911",
                "Arn": "arn:aws:sts::776010787911:assumed-role/test/test",
                "UserId": "AIDATEST"}


@pytest.fixture
def queue_fixture_bedrock():
    return _QueueFakeBedrock()


@pytest.fixture
def queue_fixture_sts():
    return _QueueFakeSTS()


# Every ACTIONABLE (profile_id, type, subtype) pair this fixture must
# produce -- the independent, hand-written expectation the rest of this
# file checks both surfaces against. Deliberately does not include
# `us.meta.llama-family-v1` (permanent) or the permanent half of
# `us.acme.imagegen-v1`'s two blockers.
EXPECTED_ACTIONABLE = frozenset({
    ("us.acme.widget-v1", "no_model_access", "not_authorized"),
    ("us.acme.imagegen-v1", "no_model_access", "not_authorized"),
})


def _actionable_pairs_from_records(records) -> frozenset:
    return frozenset(
        (record.profile_id, blocker.type, blocker.subtype)
        for record in records
        for blocker in record.blockers
        if is_actionable_blocker(blocker)
    )


# ---------------------------------------------------------------------------
# Non-vacuity precondition: the fixture itself produces exactly the blocker
# shapes this file's tests need, independent of the queue or the predicate.
# If this fails, every test below would be exercising the wrong scenario.
# ---------------------------------------------------------------------------
def test_fixture_produces_the_five_intended_blocker_shapes(
    queue_fixture_bedrock, queue_fixture_sts,
):
    result = run_pass(bedrock=queue_fixture_bedrock, sts=queue_fixture_sts)
    by_id = {r.profile_id: r for r in result.records}
    assert by_id["us.anthropic.claude-fable-5"].blockers == ()
    assert {(b.type, b.subtype) for b in by_id["stability.sd3-5-large-v1:0"].blockers} == {
        ("unsupported_output_modality", "output_not_text"),
        ("no_token_pricing", "zero_token_priced_dimensions"),
    }
    assert {(b.type, b.subtype) for b in by_id["us.meta.llama-family-v1"].blockers} == {
        ("no_agreement_offer", "not_marketplace_metered"),
    }
    assert {(b.type, b.subtype) for b in by_id["us.acme.widget-v1"].blockers} == {
        ("no_model_access", "not_authorized"),
    }
    assert {(b.type, b.subtype) for b in by_id["us.acme.imagegen-v1"].blockers} == {
        ("unsupported_output_modality", "output_not_text"),
        ("no_model_access", "not_authorized"),
    }


def test_the_hand_written_expectation_matches_the_actionability_predicate(
    queue_fixture_bedrock, queue_fixture_sts,
):
    """Sanity precondition for the comparison below: `EXPECTED_ACTIONABLE`
    was written by reading the frozen actionable/permanent split, not by
    running the predicate and copying its answer. This confirms the two
    agree before either surface is asked to reproduce it."""
    result = run_pass(bedrock=queue_fixture_bedrock, sts=queue_fixture_sts)
    assert _actionable_pairs_from_records(result.records) == EXPECTED_ACTIONABLE


# ---------------------------------------------------------------------------
# The comparison: the queue and the CLI's exit code, read from the SAME
# reconciliation pass, must agree on which profiles need a human.
# ---------------------------------------------------------------------------
def test_strict_exit_code_and_the_hand_written_expectation_agree(
    dynamodb_mock, queue_fixture_bedrock, queue_fixture_sts,
):
    """`--strict` must fire on this fixture (it carries two actionable
    blockers) -- checked before the queue-specific test below so a failure
    here points at the CLI side, not at whatever this file names as the
    queue."""
    code = main(["--apply", "--strict"], bedrock=queue_fixture_bedrock, sts=queue_fixture_sts)
    assert code == 2, (
        "the CLI's own deploy gate did not fire on a fixture carrying two "
        "actionable blockers (no_model_access on two profiles) — either "
        "this fixture stopped producing them, or the CLI's own "
        "actionability classification has changed under it"
    )


def test_queue_and_cli_agree_on_the_same_fixture(
    dynamodb_mock, queue_fixture_bedrock, queue_fixture_sts,
):
    """The strongest form: build the queue and read the CLI's exit code
    from ONE pass over ONE fixture, so a queue that quietly used a
    different rule than `--strict` would show up here even if each,
    tested alone against its own hand-written expectation, looked correct."""
    exit_code = main(["--apply", "--strict"], bedrock=queue_fixture_bedrock, sts=queue_fixture_sts)
    cli_says_actionable_exists = exit_code != 0

    from mvp.discovery.queue import list_actionable_blockers

    queue_pairs = frozenset(
        (profile_id, blocker.type, blocker.subtype)
        for profile_id, blocker in list_actionable_blockers()
    )

    assert queue_pairs == EXPECTED_ACTIONABLE
    assert bool(queue_pairs) == cli_says_actionable_exists, (
        "the queue and --strict's exit code disagree about whether this "
        "account has anything actionable — an operator reading one and an "
        "operator reading the other would be told different things about "
        "the same account"
    )


def test_permanent_only_blockers_never_reach_the_queue(
    dynamodb_mock, queue_fixture_sts,
):
    """`not_marketplace_metered` by name: the normal, forever shape of
    every AWS-billed family, not a fixable anomaly. A queue that included
    it would be permanently full of one entry per family the account can
    see and therefore unread — the exact failure mode a queue exists to
    avoid. Run against a fixture with ONLY the two permanent profiles (no
    `us.acme.widget-v1` / `us.acme.imagegen-v1`), so the queue's emptiness
    cannot be explained by "nothing was discovered" instead of "everything
    discovered was permanent"."""

    class _PermanentOnlyBedrock(_QueueFakeBedrock):
        def list_inference_profiles(self, **kwargs):
            all_profiles = super().list_inference_profiles(**kwargs)
            return {"inferenceProfileSummaries": [
                p for p in all_profiles["inferenceProfileSummaries"]
                if p["inferenceProfileId"] in (
                    "stability.sd3-5-large-v1:0", "us.meta.llama-family-v1",
                )
            ]}

    client = _PermanentOnlyBedrock()
    exit_code = main(["--apply", "--strict"], bedrock=client, sts=queue_fixture_sts)
    assert exit_code == 0, (
        "a pass with only permanent blockers must exit clean under --strict"
    )

    from mvp.discovery.queue import list_actionable_blockers

    assert list(list_actionable_blockers()) == [], (
        "a permanent-only account produced a non-empty queue — "
        "not_marketplace_metered (and the other permanent shapes) must "
        "never reach it"
    )
    # And the store really does hold the permanent blockers — the empty
    # queue above is a filtering decision, not evidence discovery saw
    # nothing.
    stored = {r.profile_id: r.blockers for r in list_discovered_records()}
    assert stored["us.meta.llama-family-v1"], (
        "the not_marketplace_metered record must still be stored and "
        "visible on the record surface — only the queue excludes it"
    )


def test_a_record_with_both_kinds_of_blocker_contributes_only_its_actionable_one(
    dynamodb_mock, queue_fixture_bedrock, queue_fixture_sts,
):
    """`us.acme.imagegen-v1` carries a permanent modality blocker AND an
    actionable access blocker at once. The queue must surface the second
    without also surfacing the first — filtering happens per blocker, not
    per record, so a record is never given a free pass on one blocker for
    also carrying another."""
    main(["--apply"], bedrock=queue_fixture_bedrock, sts=queue_fixture_sts)

    from mvp.discovery.queue import list_actionable_blockers

    entries = [
        (profile_id, blocker.type, blocker.subtype)
        for profile_id, blocker in list_actionable_blockers()
        if profile_id == "us.acme.imagegen-v1"
    ]
    assert entries == [("us.acme.imagegen-v1", "no_model_access", "not_authorized")], (
        f"expected exactly the one actionable blocker for this profile, got {entries!r}"
    )
