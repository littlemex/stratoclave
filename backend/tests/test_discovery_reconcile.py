"""E2 — the reconciliation CLI, following `mvp.pricing_feeds.fetch`'s own
conventions: dry run by default, `--apply` writes, `--strict` exits non-zero on
a finding.

**A named interface gap, settled after one round of correction.** The handoff
fixes the CLI's module path (`python -m mvp.discovery.reconcile`) and its
argparse-level behaviour, but not the object model underneath. An earlier
version of this file guessed a `Reconciler(client=...).run(apply=...)` class,
reasoning that `main(argv)` alone gives no seam for a fake client and moto
implements none of the Bedrock discovery APIs (`list_inference_profiles`,
`get_foundation_model`, `list_foundation_model_agreement_offers` all raise
`NotImplementedError`/`AttributeError` under moto, checked directly against
this environment). That reasoning about the GAP was right — it is the reason
the apply path was unreachable from any test before this file existed — but
the shape was a guess and the real one is narrower: the code author built
`run_pass(*, bedrock=None, sts=None, region=None)`, which never writes, with
writing gated on `--apply` inside `main(argv)` — and threaded the injection
straight through `main` rather than wrapping it in a class. This file now
calls exactly that:

    main([], bedrock=fake, sts=fake)              # dry run
    main(["--apply"], bedrock=fake, sts=fake)      # apply
    main(["--strict"], bedrock=fake, sts=fake)     # the deploy-gate exit code

`sts` exists because `observation_scope.account` (the record's own account
fact) comes from `sts:GetCallerIdentity`, not from Bedrock — a second
service, and so a second fake, distinct from `bedrock`.

Because `main()` returns only an exit code, not a report object, "what was
found" is now checked two ways depending on what the test is pinning: through
the **exit code** for `--strict` (what an unattended deploy gate actually
reads — a report nobody parses is not a gate), and by reading the record back
out of the store after an `--apply` run for tests that need to see WHICH
blocker fired and on which profile.

The fake client below returns fixed, realistic Bedrock shapes for exactly two
profiles — a well-formed text model and a Stability image model — so a
reconciliation pass over it produces one clean record and one blocked one, and
that split is what the dry-run/apply behaviour is checked against below.

`--strict`, as of amendment A8, is NOT tested against that split, because the
split alone no longer decides the exit code. A8 narrowed "any blocker fails
--strict" to a distinction between ACTIONABLE blockers (a human can do
something about it, so the gate must go red) and PERMANENT ones (the normal,
correct, forever state of that model, so a gate that reddened on it would be
red forever and get ignored). Stability's two blockers here —
`unsupported_output_modality` and `no_token_pricing` — are both PERMANENT: an
image-only model will never grow a text output or a per-token price, so
`--strict` must exit ZERO on this exact fixture, not non-zero. The tests below
that need `--strict` to go non-zero use separate fixtures built around an
ACTIONABLE blocker instead (`price_dimensions_unknown`, or `no_agreement_offer`
outside its `not_marketplace_metered` subtype) — because after A8 it is a
blocker's ACTIONABLE/PERMANENT status, not merely whether one exists, that the
gate reads. The gate is keyed on `(type, subtype)` alone, never on whether the
blocker is newly seen: one of the tests below re-observes an old, already-seen
actionable blocker and confirms `--strict` still fails on it.

A pass whose Bedrock client cannot be built no longer crashes `main()` with an
uncaught exception (an earlier version of this file could not test
`client_unavailable` for exactly that reason). It now returns a PASS-LEVEL
`no_agreement_offer`/`client_unavailable` blocker — the same two tokens the
gate already uses, just with nowhere to attach a per-model record, since no
model was ever listed. That blocker feeds the same actionable-blocker
`--strict` reason as any other (`--strict` exits 2, no new reason token), and
without `--strict` it exits 1 while printing that the pass could not observe
the account at all. A genuinely empty account — the call succeeds and lists
zero profiles — ALSO exits 1 without `--strict`, with no error at all, so
exit code 1 alone cannot tell the two apart; the printed line is the only
thing that does, and one of the tests below asserts on exactly that
distinction rather than only on the shared exit code. An STS client that
cannot be built, by contrast, does not block the pass at all — STS is
metadata about the pass's own identity, not a dependency of the pass — and
the pass degrades `observation_scope.account` to `""` instead.

`price_dimensions_unknown` — "a dimension appearing... that discovery cannot
classify" — is minted HERE, by the reconciliation reading a rate-card row that
`dimensions.parse_agreement_dimension` returns `None` for, not by
`mvp.discovery.pricing_key.key_for_selection`. A `Selection` carries no memory
of which raw row fed it, so the pricing-key module cannot be the one to
notice a row it never saw; only the layer that still holds the raw card can.
`test_an_unparseable_rate_card_row_is_quarantined_...` below exercises that
against a third fixture profile whose card mixes one unreadable dimension
into an otherwise well-formed one, so it is blocked for that reason alone and
not because it also fails a gate.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from mvp.discovery.reconcile import main
from mvp.discovery.records import get_discovered_record, put_discovered_record

_FIXTURES = Path(__file__).parent / "fixtures" / "discovery"
_PRICING_FIXTURES = Path(__file__).parent / "fixtures" / "pricing_feeds"


class _FakeBedrock:
    """Every Bedrock control-plane call this reconciliation needs, from fixed
    fixture data — never a real socket. Bedrock's own APIs are not
    moto-covered (see the module docstring), so this fake is the only way to
    exercise this file without contacting AWS."""

    def __init__(self):
        self._model_details = {
            "anthropic.claude-fable-5": json.loads(
                (_FIXTURES / "model_details_fable5.json").read_text()),
            "stability.sd3-5-large-v1:0": json.loads(
                (_FIXTURES / "model_details_stability.json").read_text()),
        }
        self._agreement_offers = {
            "anthropic.claude-fable-5": json.loads(
                (_PRICING_FIXTURES / "agreement_sonnet46.json").read_text()),
            "stability.sd3-5-large-v1:0": json.loads(
                (_FIXTURES / "agreement_stability.json").read_text()),
        }

    def list_inference_profiles(self, **kwargs):
        return {"inferenceProfileSummaries": [
            {
                "inferenceProfileId": "us.anthropic.claude-fable-5",
                "models": [
                    {"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/"
                                "anthropic.claude-fable-5"},
                ],
            },
            {
                "inferenceProfileId": "stability.sd3-5-large-v1:0",
                "models": [
                    {"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/"
                                "stability.sd3-5-large-v1:0"},
                ],
            },
        ]}

    def get_foundation_model(self, modelIdentifier):  # noqa: N803 — boto3 name
        return {"modelDetails": self._model_details[modelIdentifier]}

    def list_foundation_model_agreement_offers(self, modelId):  # noqa: N803
        return self._agreement_offers[modelId]


class _FakeSTS:
    """The one call `observation_scope.account` needs — real STS shape, never
    a real socket."""

    def get_caller_identity(self):
        return {"Account": "776010787911",
                "Arn": "arn:aws:sts::776010787911:assumed-role/test/test",
                "UserId": "AIDATEST"}


@pytest.fixture
def fake_client():
    return _FakeBedrock()


@pytest.fixture
def fake_sts():
    return _FakeSTS()


def _discovered_items(dynamodb_mock):
    from dynamo.client import user_tenants_table_name

    table = dynamodb_mock.Table(user_tenants_table_name())
    scanned = table.scan().get("Items", [])
    return [i for i in scanned if str(i.get("user_id", "")).startswith("DISCOVERED#")]


# --- dry run vs --apply --------------------------------------------------------
def test_a_dry_run_writes_nothing(dynamodb_mock, fake_client, fake_sts):
    """"dry run by default and safe against production credentials" — the same
    property `fetch.py --apply`-less runs hold. Checked against the actual
    table, not against any in-memory report, so a pass that computed
    everything faithfully but wrote anyway would be caught."""
    main([], bedrock=fake_client, sts=fake_sts)
    discovered = _discovered_items(dynamodb_mock)
    assert discovered == [], (
        f"a dry run wrote {len(discovered)} discovered record(s) — dry run must "
        f"write nothing so it stays safe against production credentials"
    )


def test_apply_writes(dynamodb_mock, fake_client, fake_sts):
    """The other half of the same contract: `--apply` must actually write, or
    the whole point of the flag — filling the store once so later runs don't
    race discovery — silently does nothing."""
    main(["--apply"], bedrock=fake_client, sts=fake_sts)
    discovered = _discovered_items(dynamodb_mock)
    assert len(discovered) == 2, (
        f"expected one record per discovered profile (2), found {len(discovered)}"
    )


def test_dry_run_and_apply_agree_on_the_strict_finding(dynamodb_mock, fake_sts):
    """Non-vacuity for the write/no-write split specifically: a pass that only
    fails to persist under a dry run but otherwise computes nothing (an early
    return before classification) would pass the "writes nothing" test above
    for the wrong reason. What was FOUND — here, observed through the
    `--strict` exit code, since that is the one thing this CLI's caller
    actually reads — must be the same whether or not `--apply` is set; only
    persistence may differ.

    Built on the vendor fixture rather than the shared `fake_client`, because
    since A8 that split's own blockers are both PERMANENT and `--strict` exits
    zero on it either way — a dry-run/apply pair that agreed on zero would
    satisfy the `==` half of this assertion without ever exercising the
    `!= 0` half. The vendor fixture's `price_dimensions_unknown` blocker is
    ACTIONABLE, so this is the one place a break in that agreement would
    actually show up as a failing assertion rather than two zeros trivially
    matching."""
    client = _vendor_client_with_unparseable_row()
    dry_code = main(["--strict"], bedrock=client, sts=fake_sts)
    applied_code = main(["--strict", "--apply"], bedrock=client, sts=fake_sts)
    assert dry_code == applied_code != 0


# --- the fixture split: one clean, one blocked ---------------------------------
def test_the_text_model_records_clean_and_the_image_model_is_blocked(
    dynamodb_mock, fake_client, fake_sts,
):
    """Sets up the scenario `--strict` is tested against below: of the two
    profiles this fake client answers for, Fable 5 passes every gate and
    Stability fails the token-pricing gate (real measured shape: 4
    dimensions, zero token-priced) — so the reconciliation must produce
    exactly one record with no blockers and one with at least one. Checked by
    reading the store back after `--apply`, since `main()` returns only an
    exit code."""
    main(["--apply"], bedrock=fake_client, sts=fake_sts)
    fable5 = get_discovered_record("us.anthropic.claude-fable-5")
    stability = get_discovered_record("stability.sd3-5-large-v1:0")
    assert fable5 is not None and fable5.blockers == ()
    assert stability is not None and stability.blockers != ()


# --- --strict -------------------------------------------------------------------
def test_strict_is_non_zero_when_a_profile_is_blocked(dynamodb_mock, fake_sts):
    """"`--strict` exits non-zero on an ACTIONABLE finding and is the unattended
    deploy gate" — mirrored from `fetch.py --strict`'s own contract, and
    asserted on the EXIT CODE rather than on any report field, because the
    exit code is the one thing an unattended deploy step actually reads.

    Built on the vendor fixture (`price_dimensions_unknown`), not the
    Stability profile in the shared `fake_client`. Before amendment A8, any
    blocker on any profile failed `--strict`, and Stability's blockers were
    "exactly one such finding". A8 narrowed the rule to ACTIONABLE blockers
    only — a human can do something about a card with an unreadable dimension
    (file it against the rate-card parser), so that blocker must still fail
    the gate — while Stability's blockers are the normal, permanent shape of
    an image-only model and must NOT fail it (see
    `test_strict_exits_zero_when_the_only_blockers_are_permanent` below, which
    is the fixture this test used to be built on)."""
    client = _vendor_client_with_unparseable_row()
    code = main(["--strict"], bedrock=client, sts=fake_sts)
    assert code != 0


def test_strict_exits_zero_when_the_only_blockers_are_permanent(
    dynamodb_mock, fake_client, fake_sts,
):
    """The regression amendment A8 exists to close: a gate that fails
    `--strict` on ANY blocker, actionable or not, goes red on Stability here
    and stays red forever, because an image-only model will never grow a text
    output or a per-token price — the exact "gate becomes permanently red and
    gets ignored" failure the amendment's own rationale names. Both of
    Stability's blockers are PERMANENT (`unsupported_output_modality`,
    `no_token_pricing`), so a compliant `--strict` must exit zero on this
    fixture even though a profile IS blocked. Without this test, a future
    edit that reverted the actionable/permanent distinction — failing
    `--strict` on every AWS-billed, non-token-metered, or non-text-output
    family — would pass every other test in this file and still be wrong."""
    code = main(["--strict"], bedrock=fake_client, sts=fake_sts)
    assert code == 0


def test_without_strict_the_same_run_exits_zero(dynamodb_mock, fake_client, fake_sts):
    """The same blocked profile must NOT fail the run when `--strict` is not
    passed — a candidate that cannot be served yet is the ordinary, expected
    output of discovery, not a failure of the reconciliation itself. Checked
    against the identical fixture client as the test above so the only
    variable is the flag."""
    code = main([], bedrock=fake_client, sts=fake_sts)
    assert code == 0


def test_strict_is_falsifiable_by_a_clean_run(dynamodb_mock, fake_sts):
    """Non-vacuity for `--strict` specifically: it must be possible for the
    SAME flag to exit zero when nothing is blocked, or the assertion above
    would be equally satisfied by a pass that exits non-zero unconditionally
    whenever `--strict` is passed."""
    clean_client = _FakeBedrock()
    # Only the well-formed profile — no Stability, so nothing should block.
    clean_client.list_inference_profiles = lambda **_: {"inferenceProfileSummaries": [
        {"inferenceProfileId": "us.anthropic.claude-fable-5",
         "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/"
                                "anthropic.claude-fable-5"}]},
    ]}
    code = main(["--strict"], bedrock=clean_client, sts=fake_sts)
    assert code == 0


# --- the A8 exception: no_agreement_offer's one PERMANENT subtype -------------
class _AgreementFailureBedrock:
    """A fourth, single-purpose fake client: one text, token-priced-eligible
    profile whose ONLY interesting call is `list_foundation_model_agreement_offers`,
    which always raises. Never the shared `fake_client` or the vendor client,
    because this scenario is about which EXCEPTION MESSAGE arrives, not about
    a rate card — giving it a rate card at all would let a gate-3
    `no_token_pricing` blocker sneak in and confound which blocker made
    `--strict` decide what it decided."""

    def __init__(self, model_details: dict, agreement_error: str):
        self._model_id = model_details["modelId"]
        self._model_details = model_details
        self._agreement_error = agreement_error

    def list_inference_profiles(self, **kwargs):
        return {"inferenceProfileSummaries": [
            {"inferenceProfileId": self._model_id,
             "models": [{"modelArn": self._model_details["modelArn"]}]},
        ]}

    def get_foundation_model(self, modelIdentifier):  # noqa: N803
        return {"modelDetails": self._model_details}

    def list_foundation_model_agreement_offers(self, modelId):  # noqa: N803
        raise RuntimeError(self._agreement_error)


def test_no_agreement_offer_is_permanent_only_for_its_not_marketplace_metered_subtype(
    dynamodb_mock, fake_sts,
):
    """A8's one exception, and the pair a future edit is most likely to get
    wrong because both arrive from the SAME call raising the SAME exception
    type: "Agreement not supported for this model" is the ordinary, forever
    answer for every AWS-billed family that is not sold through the
    marketplace-metered mechanism (Nova, Titan, Llama, Mistral) — so THAT
    specific message must leave `--strict` at zero. A different failure
    reaching the same call (here, an unrelated backend error) is not that
    story at all; classified as `no_agreement_offer`/`call_failed`, it is
    ACTIONABLE — something a human needs to look at — and must still fail
    `--strict`. The two fixtures differ ONLY in the exception message the
    fake's `list_foundation_model_agreement_offers` raises, so the message is
    the one variable this test isolates."""
    llama_details = json.loads((_FIXTURES / "model_details_llama3_70b.json").read_text())
    permanent_client = _AgreementFailureBedrock(
        llama_details, "Agreement not supported for this model")
    permanent_code = main(["--strict"], bedrock=permanent_client, sts=fake_sts)
    assert permanent_code == 0, (
        "no_agreement_offer/not_marketplace_metered failed --strict — this is "
        "the normal, forever shape of every AWS-billed non-marketplace-metered "
        "family and must not be treated as actionable"
    )

    outage_details = json.loads(
        (_FIXTURES / "model_details_agreement_outage.json").read_text())
    actionable_client = _AgreementFailureBedrock(
        outage_details, "ServiceUnavailableException: internal error, try again")
    actionable_code = main(["--strict"], bedrock=actionable_client, sts=fake_sts)
    assert actionable_code != 0, (
        "no_agreement_offer/call_failed passed --strict — a genuinely failing "
        "call must not be swallowed by the same exception applying to the "
        "permanent not_marketplace_metered subtype"
    )


class _AgreementSuccessBedrock(_AgreementFailureBedrock):
    """A5th fake client, sharing `_AgreementFailureBedrock`'s one-profile
    shape but overriding the one call that matters here to SUCCEED rather
    than raise. `empty_rate_card` is the one ACTIONABLE `no_agreement_offer`
    subtype that is not an exception at all — the call answers normally with
    a well-formed offer, just one whose rate card has no rows — so it needs
    a client that returns a response, not one that raises. Inheriting from
    `_AgreementFailureBedrock` rather than duplicating `list_inference_profiles`
    / `get_foundation_model` keeps the "one profile, agreement call is the
    only interesting part" shape the two share explicit, instead of two
    fixture clients quietly drifting apart from a copy-paste."""

    def __init__(self, model_details: dict, agreement_response: dict):
        super().__init__(model_details, agreement_error="unused — this client returns, never raises")
        self._agreement_response = agreement_response

    def list_foundation_model_agreement_offers(self, modelId):  # noqa: N803
        return self._agreement_response


def test_no_agreement_offer_empty_rate_card_is_actionable_not_permanent(
    dynamodb_mock, fake_sts,
):
    """The ACTIONABLE `no_agreement_offer` subtype most exposed to being
    miscoded as PERMANENT by a future edit, because nothing about it LOOKS
    like a failure at the exit-code level either: the call to
    `list_foundation_model_agreement_offers` succeeds, and the fixture is a
    well-formed, real-shaped response (`offers[].termDetails
    .usageBasedPricingTerm.rateCard`, all present) — it simply carries zero
    rows. That is an anomaly a human should look at (a marketplace-metered
    offer with nothing priced makes no sense), not the ordinary shape
    `not_marketplace_metered` describes, so A8 keeps it ACTIONABLE and
    `--strict` must still exit non-zero on it."""
    details = json.loads(
        (_FIXTURES / "model_details_empty_agreement.json").read_text())
    response = json.loads(
        (_FIXTURES / "agreement_empty_rate_card.json").read_text())
    client = _AgreementSuccessBedrock(details, response)
    code = main(["--strict"], bedrock=client, sts=fake_sts)
    assert code != 0, (
        "no_agreement_offer/empty_rate_card passed --strict — a successful "
        "call that priced nothing is an anomaly, not the permanent shape "
        "not_marketplace_metered describes, and must not be waved through"
    )


# --- a pass-level client_unavailable, and telling it apart from "found nothing" -
def test_pass_level_client_unavailable_fails_strict(dynamodb_mock, fake_sts):
    """The case an earlier version of this file could not write: `bedrock`
    left unset so the pass must build its own client, with construction
    itself made to fail. This used to escape `main()` as an uncaught
    exception; now it is a PASS-LEVEL `no_agreement_offer`/`client_unavailable`
    blocker feeding the SAME actionable-blocker `--strict` reason every other
    ACTIONABLE blocker in this file feeds — no new exit code, no new reason —
    so `--strict` exits 2, exactly as it would for any other actionable
    finding."""
    with patch("boto3.client", side_effect=RuntimeError(
            "EndpointConnectionError: could not connect to the endpoint URL")):
        code = main(["--strict"], sts=fake_sts)
    assert code == 2


def test_a_pass_that_could_not_observe_and_a_pass_that_observed_nothing_are_distinguishable(
    dynamodb_mock, fake_sts, capsys,
):
    """The pair this fix exists to make distinguishable, and the one test in
    this file most likely to pass against a broken version by accident if it
    only checked the exit code: WITHOUT `--strict`, a pass that could not
    even build a client and a pass that built one, asked, and genuinely found
    zero profiles BOTH exit 1 — there is no error in the second case, nothing
    to elevate the exit code over, so 1 is the correct answer for a run that
    accomplished nothing either way. A test that stopped at `assert code ==
    1` for both would pass just as well against a version that reconflated
    the two everywhere else, which is exactly the defect this fix closes. The
    printed line is the one place they differ, so that is what is asserted
    on: the client-unavailable pass says it could not observe the account at
    all; the genuinely-empty pass says no such thing, because it isn't true
    of it."""
    with patch("boto3.client", side_effect=RuntimeError(
            "EndpointConnectionError: could not connect to the endpoint URL")):
        could_not_observe_code = main([], sts=fake_sts)
    could_not_observe_output = capsys.readouterr().out

    empty_client = _FakeBedrock()
    empty_client.list_inference_profiles = lambda **_: {"inferenceProfileSummaries": []}
    observed_nothing_code = main([], bedrock=empty_client, sts=fake_sts)
    observed_nothing_output = capsys.readouterr().out

    assert could_not_observe_code == 1
    assert observed_nothing_code == 1
    assert "could not observe the account" in could_not_observe_output, (
        "a pass whose client could not be built printed nothing saying so — "
        "the one signal that distinguishes it from a pass that genuinely "
        "found nothing"
    )
    assert "could not observe the account" not in observed_nothing_output, (
        "a pass that genuinely found zero profiles claimed it could not "
        "observe the account — that claim is false of it and would mislead "
        "an operator into looking for a client problem that does not exist"
    )


def test_a_genuine_zero_profile_pass_carries_no_strict_reason_either(
    dynamodb_mock, fake_sts,
):
    """Non-vacuity for the "no strict reason" half of the same distinction:
    a genuinely empty account is not an ACTIONABLE finding — there is nothing
    for an operator to act on — so `--strict` must not escalate it to exit 2
    the way it does for the pass-level `client_unavailable` blocker above.
    It stays at exit 1 (the same "this run found nothing" code as without
    `--strict`), never reaching 0, but also never reaching 2."""
    empty_client = _FakeBedrock()
    empty_client.list_inference_profiles = lambda **_: {"inferenceProfileSummaries": []}
    code = main(["--strict"], bedrock=empty_client, sts=fake_sts)
    assert code == 1


def test_sts_construction_failure_does_not_block_the_pass(dynamodb_mock):
    """The asymmetry between the two services this pass depends on, pinned so
    a future edit does not give STS the same treatment as Bedrock by analogy:
    Bedrock IS the pass (no client, no profiles, no gates, nothing to
    reconcile), but STS only supplies metadata ABOUT the pass — whose account
    ran it — so a pass that cannot resolve its own identity can still
    discover every profile and run every gate. `bedrock` is injected and
    working; only the (unused) default STS construction is made to fail, and
    the pass must still complete successfully and store a record — degraded
    to an empty account, never blocked."""
    client = _FakeBedrock()
    client.list_inference_profiles = lambda **_: {"inferenceProfileSummaries": [
        {"inferenceProfileId": "us.anthropic.claude-fable-5",
         "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/"
                                "anthropic.claude-fable-5"}]},
    ]}
    with patch("boto3.client", side_effect=RuntimeError("cannot construct sts")):
        code = main(["--strict", "--apply"], bedrock=client)
    assert code == 0, (
        "an STS construction failure blocked the pass — STS is metadata "
        "about the pass's own identity, not a dependency the pass needs to "
        "discover profiles or run gates"
    )
    record = get_discovered_record("us.anthropic.claude-fable-5")
    assert record is not None
    assert record.observation_scope.account == "", (
        "observation_scope.account was not degraded to the empty string when "
        "STS could not be reached — the record should still say WHAT it "
        "could not determine, not fabricate an account"
    )


# --- rate_card_api_unavailable: one fact about the environment, not N ----------
class _ClientMissingAgreementOffersMethod:
    """The multi-profile hazard this PR fixes, reproduced without needing the
    actual old botocore installed: `list_inference_profiles` and
    `get_foundation_model` work normally for TWO text-output, otherwise
    clean profiles (Fable 5 and Llama 3 70B — both pass gate 1 outright, so
    nothing about their own data blocks either of them), and
    `list_foundation_model_agreement_offers` simply does not exist on this
    object at all — measured for real on `/usr/bin/python3`'s botocore
    1.35.99, whose `Bedrock` client raises `AttributeError` for this exact
    name; `backend/.venv`'s botocore 1.43.92 has it. Two profiles, not one,
    because "reported once, not once per model" is meaningless to assert
    against a fixture that only has one model to begin with."""

    def __init__(self):
        self._model_details = {
            "anthropic.claude-fable-5": json.loads(
                (_FIXTURES / "model_details_fable5.json").read_text()),
            "meta.llama3-70b-instruct-v1:0": json.loads(
                (_FIXTURES / "model_details_llama3_70b.json").read_text()),
        }

    def list_inference_profiles(self, **kwargs):
        return {"inferenceProfileSummaries": [
            {"inferenceProfileId": "us.anthropic.claude-fable-5",
             "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/"
                                    "anthropic.claude-fable-5"}]},
            {"inferenceProfileId": "us.meta.llama3-70b-instruct-v1:0",
             "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/"
                                    "meta.llama3-70b-instruct-v1:0"}]},
        ]}

    def get_foundation_model(self, modelIdentifier):  # noqa: N803 — boto3 name
        return {"modelDetails": self._model_details[modelIdentifier]}

    # Deliberately no `list_foundation_model_agreement_offers` — this is the
    # entire point of the fixture, not an omission to fill in later.


def test_rate_card_api_unavailable_is_reported_once_not_per_model(
    dynamodb_mock, fake_sts, capsys,
):
    """The consequence this PR closes, stated as a test: with the old-
    botocore fixture above and TWO discovered profiles, the pass must name
    the missing method exactly ONCE (`PassResult.rate_card_api_unavailable`,
    surfaced in the JSON report's own top-level field of that name) — not
    twice, and not as a `no_agreement_offer`/`method_unavailable` blocker
    attached to either record. A version of this fix that only added the
    new subtype to `gates.fetch_rate_card` without `reconcile.py` also
    deduplicating it at the pass level would still call that gate once per
    profile and attach the SAME blocker to both records — which is exactly
    the "dozens of broken models" presentation this fix exists to prevent —
    so this asserts on BOTH halves: the pass-level fact is present, and
    neither record carries a per-record copy of it."""
    client = _ClientMissingAgreementOffersMethod()
    main(["--apply", "--json"], bedrock=client, sts=fake_sts)
    # `run_pass` also logs a `discovery_agreement_offers_method_missing`
    # warning line to the SAME stdout `--json` prints its report to (the
    # module's structlog is configured with `stream=sys.stdout`); the report
    # itself is the one well-formed JSON object in the captured output, so
    # slicing from its opening brace is enough to isolate it.
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])

    assert len(payload["records"]) == 2, (
        f"expected both profiles to still be discovered and recorded even "
        f"though neither could be priced; got {len(payload['records'])}"
    )
    assert payload["rate_card_api_unavailable"] is not None, (
        "the pass-level fact was not reported at all"
    )
    assert payload["rate_card_api_unavailable"]["type"] == "no_agreement_offer"
    assert payload["rate_card_api_unavailable"]["subtype"] == "method_unavailable"

    for record in payload["records"]:
        assert not any(
            b["subtype"] == "method_unavailable" for b in record["blockers"]
        ), (
            f"profile {record['profile_id']!r} carries its own "
            f"method_unavailable blocker — the fact was reported once at "
            f"the pass level AND once per model, recreating the exact "
            f"'dozens of broken models' presentation this fix exists to "
            f"prevent"
        )
        assert record["pricing_key"] is None, (
            f"profile {record['profile_id']!r} got a pricing key despite "
            f"the rate-card API being unavailable for this whole pass"
        )


def test_rate_card_api_unavailable_fails_strict(dynamodb_mock, fake_sts):
    """Whatever else this fix changes, the one thing an unattended deploy
    step reads is the exit code, so it is asserted directly here: an
    account whose botocore has no `list_foundation_model_agreement_offers`
    method can price NOTHING for ANY model, which is exactly the kind of
    fact an operator can act on (upgrade boto3/botocore) — ACTIONABLE, not
    PERMANENT — so `--strict` must exit non-zero on it, the same as it
    already does for `client_unavailable` and every other actionable
    `no_agreement_offer` subtype in this file."""
    client = _ClientMissingAgreementOffersMethod()
    code = main(["--strict"], bedrock=client, sts=fake_sts)
    assert code != 0, (
        "no_agreement_offer/method_unavailable passed --strict — an "
        "operator can upgrade boto3/botocore to clear this, so it must keep "
        "failing an unattended deploy gate until they do"
    )


# --- the gate is keyed on (type, subtype), never on first-seen ----------------
def test_an_old_actionable_blocker_still_fails_strict(dynamodb_mock, fake_sts):
    """The gate reads `(type, subtype)` alone, never whether the blocker is
    newly observed — a deploy gate that only fired on a blocker's FIRST
    appearance would silently go green while the underlying problem persisted
    unfixed for weeks, which defeats the point of an unattended gate more
    thoroughly than never gating at all. Simulates "weeks" by writing the
    stored record back with the SAME blocker's `first_seen` moved to a date
    long before its `last_seen`, then re-running `--strict` without
    `--apply` — the dry run still merges against the stored record (this is
    what keeps `[new]` tags accurate across runs), so this exercises exactly
    the read path `--strict` uses, not a hand-rolled shortcut around it."""
    client = _vendor_client_with_unparseable_row()
    main(["--apply"], bedrock=client, sts=fake_sts)
    record = get_discovered_record("vendor.custom-model-v1")
    assert record is not None and len(record.blockers) == 1
    aged_blocker = dataclasses.replace(record.blockers[0], first_seen="2020-01-01T00:00:00+00:00")
    put_discovered_record(dataclasses.replace(record, blockers=(aged_blocker,)))

    reread = get_discovered_record("vendor.custom-model-v1")
    assert reread.blockers[0].first_seen != reread.blockers[0].last_seen, (
        "the fixture setup did not actually create a first_seen/last_seen gap "
        "— this test would pass vacuously without one"
    )

    code = main(["--strict"], bedrock=client, sts=fake_sts)
    assert code != 0, (
        "an actionable blocker stopped failing --strict once it was no longer "
        "newly seen — the gate must key on (type, subtype), not on novelty"
    )


# --- an unparseable dimension is quarantined, not silently dropped -------------
def _vendor_client_with_unparseable_row() -> _FakeBedrock:
    """A THIRD, separate fixture client (not the two-profile `fake_client`
    used everywhere above) so this scenario does not disturb any assertion
    elsewhere that counts exactly two discovered profiles. The vendor model
    here passes gate 1 (text output) and gate 3 (it has two real token-priced
    rows) cleanly — the only thing wrong with its card is the one row neither
    grammar in `dimensions.py` can read."""
    client = _FakeBedrock()
    client._model_details["vendor.custom-model-v1"] = json.loads(
        (_FIXTURES / "model_details_vendor_custom.json").read_text())
    client._agreement_offers["vendor.custom-model-v1"] = json.loads(
        (_FIXTURES / "agreement_vendor_with_unparseable_row.json").read_text())
    client.list_inference_profiles = lambda **_: {"inferenceProfileSummaries": [
        {"inferenceProfileId": "vendor.custom-model-v1",
         "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/"
                                "vendor.custom-model-v1"}]},
    ]}
    return client


def test_an_unparseable_rate_card_row_is_quarantined_not_silently_dropped(
    dynamodb_mock, fake_sts,
):
    """"Never silently ignore a dimension." A card can have plenty of usable
    token pricing (two real rows here) and still carry one row this build's
    parser cannot read at all — a future rate-card grammar the parser has not
    met yet. That row must produce its own `price_dimensions_unknown` blocker
    on the record — checked both in the stored record's content and, since
    that is what an unattended `--strict` run actually reads, in the exit
    code — passing every gate is not the same as the card being fully
    understood, and a deploy gate that only read gate failures would miss
    this one."""
    client = _vendor_client_with_unparseable_row()
    main(["--apply"], bedrock=client, sts=fake_sts)
    record = get_discovered_record("vendor.custom-model-v1")
    assert record is not None
    assert any(b.type == "price_dimensions_unknown" for b in record.blockers), (
        f"the unreadable row produced no price_dimensions_unknown blocker; "
        f"got blockers={record.blockers!r}"
    )
    strict_code = main(["--strict"], bedrock=client, sts=fake_sts)
    assert strict_code != 0, (
        "a record quarantined only for an unparseable dimension did not fail "
        "--strict — the one place an unattended deploy actually reads this"
    )


def test_the_unparseable_row_is_not_confused_with_no_token_pricing(dynamodb_mock, fake_sts):
    """Non-vacuity in the specific direction this scenario is built to check:
    the vendor card DOES have token-priced dimensions, so `gate_card_prices_tokens`
    must not ALSO fire — a reconciliation that only ever produced `no_token_pricing`
    for any card imperfection (never learning the new type at all) would still
    block this record, and only checking the blocker's type name here catches
    that it blocked it for the wrong reason."""
    client = _vendor_client_with_unparseable_row()
    main(["--apply"], bedrock=client, sts=fake_sts)
    record = get_discovered_record("vendor.custom-model-v1")
    assert record is not None
    assert not any(b.type == "no_token_pricing" for b in record.blockers)
    assert not any(b.type == "unsupported_output_modality" for b in record.blockers)
