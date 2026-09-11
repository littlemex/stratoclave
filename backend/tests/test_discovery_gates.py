"""The three data-derived discovery gates, against fixture data — never Bedrock.

Each gate is a named callable and every assertion below calls the production
callable directly (`mvp.discovery.gates.gate_output_is_text`,
`gate_agreement_exists`, `gate_card_prices_tokens`): a test that carried its own
copy of a gate's predicate would keep passing after the production module was
deleted, which is exactly the failure this file exists to rule out.

Fixtures are shaped like the real API responses this change measured against
the live service (recorded in the handoff this PR implements, not repeated
here): a `GetFoundationModel` response has exactly ten fields, of which the
only one a text-output gate reads is `outputModalities`; a rate card is the
row list at `offers[].termDetails.usageBasedPricingTerm.rateCard`, each row
`{dimension, price, description, unit}`. No network call is made anywhere in
this file — `gate_agreement_exists` is always given a fake client, and the
other two gates take plain data and were never going to make one.

Blocker-type assignment is now settled and spelled out per gate — five types
across the three gates plus one minted by the reconciliation itself, not
four across three as an earlier reading of the handoff had it:

- `gate_output_is_text` -> `unsupported_output_modality`
- `gate_agreement_exists` -> `no_agreement_offer` for "Agreement not
  supported for this model", `no_model_access` for the not-authorized text
  (`agreement.py`'s own `_NOT_AUTHORIZED` constant)
- `gate_card_prices_tokens` -> `no_token_pricing`
- an unparseable rate-card dimension -> `price_dimensions_unknown`, minted in
  `reconcile.py` rather than by any of the three gates (see
  `test_discovery_reconcile.py`)

An earlier version of this file guessed `unsupported_output_modality` did not
exist and folded gate 1's failure onto `no_token_pricing`, reasoning from the
design note that the two gates "independently reveal the same fact... from a
different direction" for an image model. That reasoning was about the two
gates catching the SAME MODEL, not about them sharing one blocker TYPE, and
the type list was short a name rather than the two gates being one signal.
Every literal `type` string below is now a stated fact rather than an
inference, but the hedge from that earlier version is kept anyway for
`test_a_readable_offer_passes` and both non-vacuity tests, which only require
that a Blocker come back or not — pinning a string there would not make
those tests stronger.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from mvp.discovery.gates import (
    gate_agreement_exists,
    gate_card_prices_tokens,
    gate_output_is_text,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "discovery"
_PRICING_FIXTURES = Path(__file__).parent / "fixtures" / "pricing_feeds"


def _model_details(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


def _rate_card_rows(fixture_dir: Path, name: str) -> list[dict]:
    doc = json.loads((fixture_dir / name).read_text())
    return doc["offers"][0]["termDetails"]["usageBasedPricingTerm"]["rateCard"]


class _AgreementClient:
    """A fake `bedrock` client exposing only the one call this gate makes.

    Never boto3, never a network socket — the fixed contract this file holds
    `gate_agreement_exists` to, per the handoff's "no network" rule for every
    gate test.
    """

    def __init__(self, *, response=None, error_message: str | None = None):
        self._response = response
        self._error_message = error_message

    def list_foundation_model_agreement_offers(self, modelId):  # noqa: N803 — boto3 name
        if self._error_message is not None:
            raise RuntimeError(self._error_message)
        return self._response


# --- gate_output_is_text ------------------------------------------------------
def test_text_output_passes():
    """Fable 5 measured as TEXT, IMAGE in / TEXT out: TEXT is in the output set,
    so the gate has nothing to block."""
    assert gate_output_is_text(_model_details("model_details_fable5.json")) is None


def test_image_only_output_is_blocked():
    """Stability measured as IMAGE in / IMAGE out: nothing in outputModalities is
    TEXT, so this gate must refuse it, named by its own settled type —
    `unsupported_output_modality` — distinct from `gate_card_prices_tokens`'s
    `no_token_pricing`, even though both gates fire on this same model."""
    blocker = gate_output_is_text(_model_details("model_details_stability.json"))
    assert blocker is not None
    assert blocker.type == "unsupported_output_modality"
    assert blocker.evidence  # some non-empty account of what was seen


def test_gate_output_is_text_evidence_is_derived_not_canned():
    """Non-vacuity for the evidence field specifically: a gate that returns a
    fixed string regardless of what it was handed would pass every assertion
    above while carrying no information a person could act on. Feeding it two
    different non-text models and finding the same fixed evidence back would be
    exactly that bug, so the two evidence strings are required to differ."""
    stability = gate_output_is_text(_model_details("model_details_stability.json"))
    twelvelabs = gate_output_is_text(_model_details("model_details_twelvelabs.json"))
    assert stability is not None and twelvelabs is not None
    assert stability.evidence != twelvelabs.evidence


def test_gate_output_is_text_is_falsifiable():
    """Remove the rule (always return None) and this must fail; remove the
    inverse (always return a Blocker) and the TEXT-output test above must
    fail. Both directions are exercised by the two tests above already; this
    one is the explicit self-check that a stub violating the rule is caught,
    run once here so the property is not merely implied by two tests that
    happen to agree."""
    def _always_pass(_details):
        return None

    def _always_block(_details):
        from mvp.discovery.gates import Blocker
        return Blocker(type="unsupported_output_modality", subtype="x", evidence="x")

    fable5 = _model_details("model_details_fable5.json")
    stability = _model_details("model_details_stability.json")
    assert gate_output_is_text(fable5) != _always_block(fable5)
    assert gate_output_is_text(stability) != _always_pass(stability)


# --- gate_agreement_exists ----------------------------------------------------
def test_a_readable_offer_passes():
    """The mechanism exists, this account has used it, and it answered: nothing
    for this gate to block. Real rows from a live-measured fixture, so the
    "readable" case is not a hand-rolled shape."""
    response = json.loads((_PRICING_FIXTURES / "agreement_opus5.json").read_text())
    blocker = gate_agreement_exists("anthropic.claude-opus-5",
                                    client=_AgreementClient(response=response))
    assert blocker is None


def test_agreement_not_supported_is_no_agreement_offer_not_no_price():
    """Measured on `meta.llama3-70b-instruct-v1:0`: the API answers
    `ValidationException: Agreement not supported for this model`. The handoff
    is explicit that this must not be read as "this model has no price" — it
    means the discovery mechanism used to find a price does not exist for this
    model, which is a different fact with a different evidence string, so the
    blocker must carry the real message rather than a generic one."""
    blocker = gate_agreement_exists(
        "meta.llama3-70b-instruct-v1:0",
        client=_AgreementClient(
            error_message="ValidationException: Agreement not supported for this model"),
    )
    assert blocker is not None
    assert blocker.type == "no_agreement_offer"
    assert "agreement not supported" in blocker.evidence.lower()


def test_not_authorized_is_a_distinct_blocker_from_agreement_not_supported():
    """The two failures this gate can see are adjacent but distinct: one says
    the mechanism does not exist for this model (`no_agreement_offer`), the
    other says the mechanism exists and this account has not used it
    (`no_model_access` — grant model access, don't wait for a different
    model). `agreement.py`'s own `AgreementFeed` already measures and matches
    on this exact substring for the same distinction, which is what makes it
    safe to hold this gate to the same string rather than a guess: "Your
    account is not authorized to invoke this API operation"."""
    blocker = gate_agreement_exists(
        "anthropic.claude-opus-4-1",
        client=_AgreementClient(
            error_message=("AccessDeniedException: Your account is not authorized "
                            "to invoke this API operation")),
    )
    assert blocker is not None
    assert blocker.type == "no_model_access"

    other = gate_agreement_exists(
        "meta.llama3-70b-instruct-v1:0",
        client=_AgreementClient(
            error_message="ValidationException: Agreement not supported for this model"),
    )
    assert other is not None
    assert other.type != blocker.type, (
        "the mechanism-absent and mechanism-unused failures collapsed into one "
        "blocker type, which erases the distinction the handoff calls out by name"
    )


def test_gate_agreement_exists_reads_the_message_not_the_exception_class():
    """Non-vacuity for the branch itself: swapping which of the two error
    strings the client raises must swap which blocker type comes back. A gate
    that classified by exception TYPE alone (every failure here is a plain
    exception in this fixture) rather than by message content would return
    the same type for both and this must fail."""
    a = gate_agreement_exists(
        "model-a", client=_AgreementClient(error_message="Agreement not supported"))
    b = gate_agreement_exists(
        "model-b", client=_AgreementClient(
            error_message="not authorized to invoke this API operation"))
    assert a.type != b.type


# --- the branch ordering amendment A8's actionable/permanent split leans on ---
def test_agreement_not_supported_classifies_as_not_marketplace_metered():
    """This is not a hypothetical message: checked against real Bedrock in
    us-east-1 across five families — Nova, Titan, Llama, Mistral, and
    Anthropic Claude — every one of them raises exactly this exception for a
    model with no marketplace-metered agreement mechanism, which makes it the
    ORDINARY answer for the overwhelming majority of profiles a full-account
    scan sees, not an edge case. Amendment A8 marks this specific subtype
    PERMANENT so `--strict` does not go red on the normal shape of an
    AWS-billed model's entire fleet. Asserted against the real message
    verbatim, capitalisation included, not a paraphrase — the classifier
    matches a substring of what AWS actually sends, and a test that invented
    its own wording would prove nothing about that match."""
    blocker = gate_agreement_exists(
        "amazon.nova-pro-v1:0",
        client=_AgreementClient(error_message="Agreement not supported for this model"))
    assert blocker is not None
    assert blocker.type == "no_agreement_offer"
    assert blocker.subtype == "not_marketplace_metered"


def test_an_unrelated_agreement_failure_classifies_as_call_failed():
    """The branch that must stay ACTIONABLE: a message that matches neither
    the not-marketplace-metered case above nor the not-authorized case below
    is a genuinely failing call — a throttle, an outage, a shape the gate has
    not met — and a human needs to look at it. If this ever fell into
    `not_marketplace_metered` by a substring match too loose, or by the
    branches being tried in the wrong order, every one of these real failures
    would go PERMANENT and `--strict` would stop catching them."""
    blocker = gate_agreement_exists(
        "vendor.some-model-v1",
        client=_AgreementClient(
            error_message="ServiceUnavailableException: internal error, try again"))
    assert blocker is not None
    assert blocker.type == "no_agreement_offer"
    assert blocker.subtype == "call_failed"


def test_not_authorized_classifies_as_no_model_access_not_no_agreement_offer():
    """The other half of the same load-bearing ordering: the not-authorized
    message must land on a different TYPE entirely (`no_model_access`), never
    on `no_agreement_offer` under some subtype. Both `no_agreement_offer`
    subtypes above and this type answer the same call raising an exception,
    so if the not-authorized check were ever tried after (and shadowed by) a
    looser not-marketplace-metered match, this would silently become
    `no_agreement_offer` and both A8 branches on it — PERMANENT and
    ACTIONABLE — would be reading the wrong signal for it. Not_authorized
    means "grant model access", not "this model has no agreement mechanism";
    collapsing it into `no_agreement_offer` would erase that distinction."""
    blocker = gate_agreement_exists(
        "anthropic.claude-opus-4-1",
        client=_AgreementClient(
            error_message="Your account is not authorized to invoke this API operation"))
    assert blocker is not None
    assert blocker.type == "no_model_access"
    assert blocker.type != "no_agreement_offer"


def test_client_construction_failure_classifies_as_client_unavailable():
    """A8 names three ACTIONABLE `no_agreement_offer` subtypes besides
    `not_marketplace_metered`: `client_unavailable`, `call_failed`, and
    `empty_rate_card`. `client_unavailable` is a DIFFERENT failure surface
    from every other test in this section: those all give this gate a client
    object and fail INSIDE the call the client makes; this one fails before
    any call happens, because the client itself cannot be built (no region
    configured, no route to the endpoint, credentials missing). No
    `_AgreementClient` is passed at all — `client` is left at its default
    (`None`), so the gate must attempt to construct its own, and this patches
    THAT construction to fail, not the API call."""
    with patch("boto3.client", side_effect=RuntimeError(
            "EndpointConnectionError: could not connect to the endpoint URL")):
        blocker = gate_agreement_exists("some.model")
    assert blocker is not None
    assert blocker.type == "no_agreement_offer"
    assert blocker.subtype == "client_unavailable"


def test_an_unrelated_agreement_failure_classifies_as_call_failed_not_client_unavailable():
    """Non-vacuity for the distinction the test above draws: the SAME kind of
    message ("could not connect", "no region", "no credentials") must NOT
    classify as `client_unavailable` when it arrives from a client that WAS
    successfully constructed and handed to the gate — only failing to BUILD
    the client does. If the gate classified by matching this message text
    regardless of which try/except caught it, a client-side outage on an
    already-built client would be silently relabelled as a construction
    failure, and `test_an_unrelated_agreement_failure_classifies_as_call_failed`
    above and this one would collapse into the same case."""
    blocker = gate_agreement_exists(
        "some.model",
        client=_AgreementClient(
            error_message="EndpointConnectionError: could not connect to the endpoint URL"))
    assert blocker is not None
    assert blocker.type == "no_agreement_offer"
    assert blocker.subtype == "call_failed"


def test_a_successful_empty_rate_card_classifies_as_empty_rate_card():
    """The one ACTIONABLE `no_agreement_offer` subtype that arrives WITHOUT an
    exception: the call succeeds and answers with a well-formed offer, but
    `offers[].termDetails.usageBasedPricingTerm.rateCard` carries no rows —
    an anomaly (the mechanism exists and answered, but with nothing to
    price), not the permanent shape `not_marketplace_metered` describes,
    which is why A8 keeps it ACTIONABLE. This is exactly the shape most
    likely to be miscoded as PERMANENT by a future edit, since nothing here
    LOOKS like a failure — no raised exception, no error message — so this
    is checked against the returned blocker's subtype directly, the same way
    `not_marketplace_metered` is pinned above, not merely against "a blocker
    came back"."""
    response = json.loads(
        (_FIXTURES / "agreement_empty_rate_card.json").read_text())
    blocker = gate_agreement_exists(
        "vendor.empty-agreement-v1", client=_AgreementClient(response=response))
    assert blocker is not None
    assert blocker.type == "no_agreement_offer"
    assert blocker.subtype == "empty_rate_card"


def test_empty_rate_card_is_not_confused_with_no_token_pricing():
    """Non-vacuity in the direction this case is built to catch: an empty
    rate card and a POPULATED rate card that simply prices nothing in tokens
    (the Stability case, `gate_card_prices_tokens`'s own `no_token_pricing`)
    are different mechanisms firing at different gates. A reconciliation that
    only ever produced `no_token_pricing` for "nothing to price" — never
    learning `empty_rate_card` at gate 2 — would still block this record, but
    for the wrong reason, and the two carry OPPOSITE verdicts under A8
    (`no_token_pricing` is PERMANENT; `empty_rate_card` is ACTIONABLE), so
    confusing them would silently flip which one `--strict` is allowed to
    ignore."""
    response = json.loads(
        (_FIXTURES / "agreement_empty_rate_card.json").read_text())
    blocker = gate_agreement_exists(
        "vendor.empty-agreement-v1", client=_AgreementClient(response=response))
    assert blocker is not None
    assert blocker.type != "no_token_pricing"


# --- gate_card_prices_tokens ---------------------------------------------------
def test_a_card_with_token_dimensions_passes():
    """Real Claude Opus 5 rows, which the existing dimension parser already
    resolves to token classes (tested in test_pricing_feeds_dimensions.py) —
    reused here rather than re-asserted, since this gate's job is only to ask
    whether at least one such row exists."""
    rows = _rate_card_rows(_PRICING_FIXTURES, "agreement_opus5.json")
    assert gate_card_prices_tokens(rows) is None


def test_a_card_with_no_token_dimensions_is_blocked():
    """Stability measured with 4 dimensions, zero token-priced — the exact
    shape the handoff records. An image-per-unit card is real, recognised
    pricing data; it simply prices nothing this gateway can meter in tokens."""
    rows = _rate_card_rows(_FIXTURES, "agreement_stability.json")
    blocker = gate_card_prices_tokens(rows)
    assert blocker is not None
    assert blocker.type == "no_token_pricing"


def test_an_empty_card_is_also_blocked():
    """The degenerate case of "no token-priced dimension": zero rows at all.
    Written separately from the Stability case because a gate that only
    checked "the card is non-empty and I'll assume it prices tokens" would
    pass the populated-Stability-card test for the wrong reason and only show
    itself here."""
    assert gate_card_prices_tokens([]) is not None


def test_gate_card_prices_tokens_is_falsifiable():
    """Remove the check (always pass) and the Stability test above must fail;
    that removal is exercised directly here so the property does not rest on
    one test alone matching one stub by coincidence."""
    def _always_pass(_rows):
        return None

    stability_rows = _rate_card_rows(_FIXTURES, "agreement_stability.json")
    assert gate_card_prices_tokens(stability_rows) != _always_pass(stability_rows)


# --- the two signals that catch the same model, named separately --------------
def test_the_two_signals_that_catch_the_same_model_are_named_separately():
    """Gate 1 (text output) and gate 3 (token pricing) "independently reveal
    the same fact... from a different direction" for an image model — read
    correctly, that is about the two gates independently CATCHING the same
    model, not about them sharing one blocker type. Stability fails both, and
    the settled taxonomy gives the two failures different names
    (`unsupported_output_modality` and `no_token_pricing`) because they are
    different mechanisms an operator would act on differently, even though
    they agree on which model is unpublishable. An earlier version of this
    test asserted the two types were equal; that assertion is now known to be
    wrong rather than merely unproven, so this test asserts the two gates
    both fire and are named distinctly instead."""
    details = _model_details("model_details_stability.json")
    rows = _rate_card_rows(_FIXTURES, "agreement_stability.json")
    text_blocker = gate_output_is_text(details)
    price_blocker = gate_card_prices_tokens(rows)
    assert text_blocker is not None and price_blocker is not None
    assert text_blocker.type == "unsupported_output_modality"
    assert price_blocker.type == "no_token_pricing"
    assert text_blocker.type != price_blocker.type
