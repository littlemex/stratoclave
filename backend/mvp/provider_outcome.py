"""How far did a provider call get, and what does that oblige us to hold?

`docs/design/charge-loss.md` is the contract; `docs/MEASUREMENTS.md` publishes the
measurements it rests on. The gateway cannot observe what the provider charged; it
can only observe how far its own request got. Everything here follows from that, and from one measured fact: **"I did not receive a response" does not mean
"I was not billed."** A Converse call abandoned on a 2 s client read timeout was
still executed and billed 1,493 output tokens, measured against CloudWatch's own
`AWS/Bedrock` token counters on a model the account otherwise never invokes.

Two ideas keep this correct when the provider's billing behaviour changes, which
it may, and which is only partly observable from here:

1. **The state is a fact; the liability is policy.** `classify_exception` reports
   only what the gateway saw — the request never left, the service rejected it, or
   bytes went out and no terminal evidence came back. What each state costs lives
   in `LIABILITY_POLICY`, a versioned table whose rows cite the measurement that
   justifies them. If Bedrock starts billing rejected requests, one row changes;
   the states, the ledger schema and the invariant do not.
2. **The unknown is expensive by default.** Anything unrecognised classifies as
   `SUBMITTED_UNSETTLED` and holds a full ceiling. Being wrong in that direction
   costs a tenant headroom; being wrong the other way breaks the ceiling this
   project sells. Those are not symmetric, so the default is not symmetric.

What this module deliberately does NOT contain is a table of which error codes
are billed. That is a provider behaviour, not a property of our code, and
encoding it as fact is the defect this replaces.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ParamValidationError,
    ReadTimeoutError,
)

# --- the states, which are observations ------------------------------------

#: The request never left this process: serialisation, DNS, TLS, connect.
NOT_SUBMITTED = "not_submitted"
#: The service answered with an explicit rejection. It received the request.
REJECTED_PRE_INFERENCE = "rejected_pre_inference"
#: Bytes went out and no terminal evidence came back. The default for the unknown.
SUBMITTED_UNSETTLED = "submitted_unsettled"
#: Usage observed in a successful response.
SETTLED_FINAL = "settled_final"

STATES = (NOT_SUBMITTED, REJECTED_PRE_INFERENCE, SUBMITTED_UNSETTLED, SETTLED_FINAL)

# --- the policy table, which is a decision --------------------------------

#: What a state obliges the pool to keep held.
LIABILITY_NONE = "none"
LIABILITY_FULL_CEILING = "full_ceiling"
LIABILITY_OBSERVED = "observed_amount"

#: Bump when a row changes, so a ledger event can record which policy priced it.
LIABILITY_POLICY_VERSION = "2026-08-29.2"

#: scope: what this row was measured against. Narrow it rather than generalising.
_SCOPE = "aws-bedrock/converse+converse_stream/on-demand-token-pricing"

LIABILITY_POLICY: dict[str, dict[str, Any]] = {
    NOT_SUBMITTED: {
        "liability": LIABILITY_NONE,
        "scope": "transport",
        "evidence": (
            "The request was never written, so no provider-side work can exist. "
            "This is a property of our own transport, not of the provider, and is "
            "the one row that needs no measurement. Measured corroboration: a "
            "botocore-side ParamValidationError produced no provider counters at "
            "all, not even an invocation."
        ),
        "accepted_risk": None,
    },
    REJECTED_PRE_INFERENCE: {
        "liability": LIABILITY_NONE,
        "scope": _SCOPE,
        "evidence": (
            "Measured 2026-08-29, us-east-1: a service ValidationException (HTTP "
            "400, both an over-limit maxTokens and an empty text block) produced "
            "Invocations=1 and InvocationClientErrors=1 but NO token counters, and "
            "the invocation log record carried no token counts. On-demand pricing "
            "is per token, so no tokens is no charge. Note Invocations alone counts "
            "rejections and is not a billing proxy — reading it instead of the token "
            "counters gives the opposite answer."
        ),
        "accepted_risk": (
            "This zero is an operating assumption about today's provider, not a "
            "guarantee. If rejections begin to bill, this row under-holds until the "
            "canary notices. Change the row, not the code. Narrower still: only "
            "ValidationException was measured; the other codes in "
            "`_REJECTION_CODES_BY_SHAPE` inherit this zero by argument, not by "
            "counter, and a canary should promote them one at a time. The same "
            "applies to the HTTP statuses in `_REJECTION_STATUSES_BY_SHAPE`: the "
            "OpenAI-compatible endpoint reports a refusal as a status rather than "
            "as a modelled error code, and none of those statuses has a counter "
            "behind it here."
        ),
    },
    SUBMITTED_UNSETTLED: {
        "liability": LIABILITY_FULL_CEILING,
        "scope": _SCOPE,
        "evidence": (
            "Measured 2026-08-29, us-east-1: a Converse call abandoned at a 2 s "
            "client read timeout, with exactly one SDK attempt, was billed 1,493 "
            "output tokens; the invocation log record for it carried in=22/out=1,493. "
            "The caller received nothing. Holding the ceiling is the only choice that "
            "keeps the pool limit binding when the amount is unknown."
        ),
        "accepted_risk": None,
    },
    SETTLED_FINAL: {
        "liability": LIABILITY_OBSERVED,
        "scope": _SCOPE,
        "evidence": (
            "The provider's own usage block. Measured to agree exactly with the "
            "CloudWatch token counters and with the invocation log record."
        ),
        "accepted_risk": None,
    },
}

#: Codes MEASURED to produce an invocation with no token counters at all.
_REJECTION_CODES_MEASURED = frozenset({
    "ValidationException",
})

#: Codes included as rejections by shape: the service answers them before a model
#: can run, so the same zero applies by the same reasoning — but nobody has put a
#: counter behind them here, and the policy row says so rather than implying they
#: were measured. Promote a code to the set above when a canary covers it.
_REJECTION_CODES_BY_SHAPE = frozenset({
    "AccessDeniedException",
    "ResourceNotFoundException",
    "ThrottlingException",
    "ServiceQuotaExceededException",
    "UnrecognizedClientException",
    "IncompleteSignature",
    "MissingAuthenticationToken",
})

#: Service error codes that are rejections rather than results. Membership decides
#: which STATE a failure is in, never how much money it costs — that stays in the
#: table above. A code in neither set is not assumed cheap: it lands in
#: `SUBMITTED_UNSETTLED`.
_REJECTION_CODES = _REJECTION_CODES_MEASURED | _REJECTION_CODES_BY_SHAPE


#: HTTP statuses that mean the service refused the request before a model could
#: run. The OpenAI-compatible endpoint answers with a status where Converse raises
#: a modelled error code, so this is the same argument as
#: `_REJECTION_CODES_BY_SHAPE` in the other alphabet — and, like that set, nobody
#: has put a token counter behind it. The set is a MAPPING of that one, not a
#: generalisation of it: 400/422 are Validation, 401/403 are AccessDenied and the
#: signature codes, 404 is ResourceNotFound, 429 is Throttling and
#: ServiceQuotaExceeded, and 405/413/415 are refusals the service makes on the
#: shape of the request before it can route it to a model. Anything without a
#: counterpart in that set is deliberately absent — 409 was dropped for exactly
#: that reason — as are 408 and every 5xx, where the measured expensive case lives:
#: a timeout or a server-side failure may follow a model that ran to completion.
_REJECTION_STATUSES_BY_SHAPE = frozenset({
    400, 401, 403, 404, 405, 413, 415, 422, 429,
})


def classify_http_status(status: int) -> str:
    """Which state an HTTP status from an OpenAI-compatible provider leaves us in.

    The status-shaped sibling of `classify_exception`, for the routes that read a
    response instead of catching an exception. A 2xx is not a classification: a
    route that got one observed usage and must settle it, so asking here is a
    programming error and gets the expensive answer rather than a free one.
    """
    if status in _REJECTION_STATUSES_BY_SHAPE:
        return REJECTED_PRE_INFERENCE
    return SUBMITTED_UNSETTLED


def liability_for(state: str) -> str:
    """What `state` obliges the pool to hold.

    An unknown state is a programming error, and the safe reading of a
    programming error on a money path is the expensive one.
    """
    row = LIABILITY_POLICY.get(state)
    if row is None:
        return LIABILITY_FULL_CEILING
    return str(row["liability"])


def refunds_immediately(state: str) -> bool:
    """Whether a reservation in `state` may be returned to the pool now.

    The single question asked of this module, and it is asked from exactly one
    place: the write that `mvp._money.Hold.claim_unobserved` hands back. Routes report
    what they saw and never answer this themselves — nine hand-written endings gave
    five different answers to it before the hold owned the decision.
    """
    return liability_for(state) == LIABILITY_NONE


def classify_exception(exc: BaseException) -> str:
    """Which state an exception from a provider call leaves the attempt in.

    Ordered by how far the request got, not by exception type hierarchy, and it
    ends in `SUBMITTED_UNSETTLED` rather than in a guess.
    """
    # Never written: our own serialiser refused it.
    if isinstance(exc, ParamValidationError):
        return NOT_SUBMITTED
    # Never connected: no socket to the service was established.
    if isinstance(exc, (ConnectTimeoutError, EndpointConnectionError)):
        return NOT_SUBMITTED

    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in _REJECTION_CODES:
            return REJECTED_PRE_INFERENCE
        # A 5xx means the service took the request and then failed somewhere we
        # cannot see. Whether the model had already run is exactly the thing that
        # is unobservable, so it is not a rejection.
        if isinstance(status, int) and 400 <= status < 500 and code:
            # An unlisted 4xx is still a rejection by shape, but it has not been
            # measured, so it does not get the cheap row: it is treated as
            # submitted until someone measures it and adds the code above.
            return SUBMITTED_UNSETTLED
        return SUBMITTED_UNSETTLED

    # Read timeout is the measured expensive case: the request left, the model may
    # have run to completion, and the client simply stopped waiting.
    if isinstance(exc, (ReadTimeoutError, ConnectionClosedError)):
        return SUBMITTED_UNSETTLED

    return SUBMITTED_UNSETTLED


# --- the opt-in switch -----------------------------------------------------

#: On by default. Every classification still runs and is recorded regardless of
#: this flag — the states and the ledger events are the point, and they are
#: useful on their own — but with the flag on, a `SUBMITTED_UNSETTLED` attempt
#: keeps its reservation until an operator settles or releases it, rather than
#: being handed back as though the call were free. It was not free: an abandoned
#: Bedrock call is billed for the full generation (see the module docstring's
#: measured 1,493-token example). The hard-ceiling work once shipped a gate that
#: defaulted ON and began refusing every pooled tenant the moment it merged; this
#: flag existed to apply that lesson by shipping OFF first and observing the
#: effect in production before it could withhold anyone's budget. That
#: observation period is over: the default is now ON, and an operator who needs
#: the old byte-for-byte refund behaviour sets this variable to a falsy value.
UNOBSERVED_HOLD_ENV = "STRATOCLAVE_UNOBSERVED_HOLDS"

_TRUTHY = ("1", "true", "yes", "on")


def unobserved_holds_enforced() -> bool:
    """Whether a `SUBMITTED_UNSETTLED` attempt keeps its reservation.

    True unless an operator explicitly sets `STRATOCLAVE_UNOBSERVED_HOLDS` to a
    falsy value. With it off, every classification still runs and is recorded —
    the states and the ledger events are the point, and they are useful on their
    own — but the refund behaviour reverts to byte-for-byte what it was before
    this module existed: the reservation is handed back as though the call were
    free, which it was not.
    """
    raw = os.getenv(UNOBSERVED_HOLD_ENV)
    if raw is None:
        return True
    return raw.strip().lower() in _TRUTHY


def attempt_request_metadata(
    hold_id: Optional[str], tenant_id: Optional[str] = None
) -> dict[str, str]:
    """The correlation handle to stamp on a provider call.

    Bedrock echoes `requestMetadata` into model invocation log records, verified
    on real Bedrock: a record for a call abandoned on read timeout was retrieved
    by this marker alone and carried the exact token counts. That matters because
    an abandoned caller never learns the provider's own request id, and the log
    record's `identity` is the gateway's single task role, identical for every
    tenant. So this marker is the ONLY thing that can attribute a charge the
    gateway did not observe, which makes stamping it a correctness requirement
    rather than an optimisation.

    Values are ids the gateway minted. No prompt, no user identifier, nothing
    derived from request content.
    """
    md: dict[str, str] = {}
    if hold_id:
        md["sc_attempt_id"] = str(hold_id)[:256]
    if tenant_id:
        md["sc_tenant"] = str(tenant_id)[:256]
    return md
