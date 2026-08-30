"""The classification and the liability policy, one test per branch.

`docs/design/charge-loss.md` acceptance criterion 4 asks for a test per state and,
specifically, for an unrecognised error to land in the expensive state. That last
one is the point of the whole module: the defect being fixed was a generic
`except Exception` that refunded, so a test that only covers the errors somebody
thought of would pass while the real hole stayed open.
"""
from __future__ import annotations

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ParamValidationError,
    ReadTimeoutError,
)

from mvp import provider_outcome as po


def _client_error(code: str, status: int = 400, op: str = "Converse") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "x"},
         "ResponseMetadata": {"HTTPStatusCode": status}},
        op,
    )


# --- the states ------------------------------------------------------------

def test_serialiser_refusal_never_reached_the_wire():
    exc = ParamValidationError(report="maxTokens must be positive")
    assert po.classify_exception(exc) == po.NOT_SUBMITTED
    assert po.refunds_immediately(po.NOT_SUBMITTED)


@pytest.mark.parametrize("exc", [
    ConnectTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"),
    EndpointConnectionError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"),
])
def test_never_connected_is_not_submitted(exc):
    assert po.classify_exception(exc) == po.NOT_SUBMITTED


def test_service_rejection_is_its_own_state():
    assert po.classify_exception(_client_error("ValidationException")) == (
        po.REJECTED_PRE_INFERENCE
    )
    assert po.refunds_immediately(po.REJECTED_PRE_INFERENCE)


def test_throttle_is_a_rejection_so_throttled_traffic_does_not_eat_budget():
    assert po.classify_exception(_client_error("ThrottlingException", 429)) == (
        po.REJECTED_PRE_INFERENCE
    )


def test_read_timeout_holds_the_ceiling():
    """The measured case: abandoned at the client, billed at the provider."""
    exc = ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")
    assert po.classify_exception(exc) == po.SUBMITTED_UNSETTLED
    assert not po.refunds_immediately(po.SUBMITTED_UNSETTLED)
    assert po.liability_for(po.SUBMITTED_UNSETTLED) == po.LIABILITY_FULL_CEILING


def test_server_error_is_unsettled_because_the_model_may_have_run():
    for code, status in (("InternalServerException", 500),
                         ("ServiceUnavailableException", 503),
                         ("ModelErrorException", 424)):
        assert po.classify_exception(_client_error(code, status)) == (
            po.SUBMITTED_UNSETTLED
        ), code


def test_an_unlisted_4xx_does_not_get_the_cheap_row():
    """A rejection by shape is not a rejection by evidence.

    Reading the status code and concluding "4xx, therefore free" is the same kind
    of assumption this module exists to avoid; a code nobody measured stays
    expensive until someone measures it.
    """
    assert po.classify_exception(_client_error("SomeNewBedrockRefusal", 400)) == (
        po.SUBMITTED_UNSETTLED
    )


def test_unrecognised_exception_lands_in_the_expensive_state():
    class Weird(Exception):
        pass

    assert po.classify_exception(Weird("no idea")) == po.SUBMITTED_UNSETTLED
    assert not po.refunds_immediately(po.SUBMITTED_UNSETTLED)


def test_unknown_state_is_expensive_not_free():
    """A programming error on a money path must fail toward holding."""
    assert po.liability_for("state_that_does_not_exist") == po.LIABILITY_FULL_CEILING
    assert not po.refunds_immediately("state_that_does_not_exist")


# --- the policy table ------------------------------------------------------

def test_every_state_has_a_policy_row_with_cited_evidence():
    for state in po.STATES:
        row = po.LIABILITY_POLICY[state]
        assert row["liability"] in (
            po.LIABILITY_NONE, po.LIABILITY_FULL_CEILING, po.LIABILITY_OBSERVED
        )
        assert row["evidence"] and len(row["evidence"]) > 40, (
            f"{state} needs the measurement that justifies its liability, so a "
            f"reader can tell a measured zero from an assumed one"
        )


def test_a_zero_liability_row_names_its_accepted_risk():
    """Except the one row that is a property of our own transport.

    A zero that costs nothing to be wrong about needs no risk statement; a zero
    that asserts something about the provider does.
    """
    for state, row in po.LIABILITY_POLICY.items():
        if row["liability"] != po.LIABILITY_NONE:
            continue
        if state == po.NOT_SUBMITTED:
            continue
        assert row["accepted_risk"], (
            f"{state} assigns zero liability from provider behaviour, so it must "
            f"say what happens when that behaviour changes"
        )


def test_measured_and_by_shape_rejection_codes_are_distinguished():
    """So nobody later reads the union and believes all of it was measured."""
    assert po._REJECTION_CODES_MEASURED
    assert po._REJECTION_CODES_BY_SHAPE
    assert not (po._REJECTION_CODES_MEASURED & po._REJECTION_CODES_BY_SHAPE)
    assert "ValidationException" in po._REJECTION_CODES_MEASURED
    assert "measured" in po.LIABILITY_POLICY[po.REJECTED_PRE_INFERENCE]["accepted_risk"]


def test_policy_version_is_pinned():
    assert po.LIABILITY_POLICY_VERSION


# --- the switch and the correlation handle ---------------------------------

def test_enforcement_is_on_unless_explicitly_turned_off(monkeypatch):
    monkeypatch.delenv(po.UNOBSERVED_HOLD_ENV, raising=False)
    assert po.unobserved_holds_enforced() is True
    monkeypatch.setenv(po.UNOBSERVED_HOLD_ENV, "1")
    assert po.unobserved_holds_enforced() is True
    monkeypatch.setenv(po.UNOBSERVED_HOLD_ENV, "no")
    assert po.unobserved_holds_enforced() is False


def test_request_metadata_carries_ids_and_nothing_else():
    md = po.attempt_request_metadata("hold-abc", "tenant-1")
    assert md == {"sc_attempt_id": "hold-abc", "sc_tenant": "tenant-1"}
    assert po.attempt_request_metadata(None) == {}
    long = po.attempt_request_metadata("x" * 400, "y" * 400)
    assert all(len(v) <= 256 for v in long.values()), (
        "Bedrock bounds requestMetadata values; an over-long id must not turn a "
        "billable call into a validation error"
    )
