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
import re
from typing import Any, Optional

from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ParamValidationError,
    ReadTimeoutError,
)

from core.logging import get_logger

logger = get_logger(__name__)

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


def _classify_httpx(exc: BaseException) -> Optional[str]:
    """`exc` as an outcome state when it is an httpx transport error, else `None`.

    Only the classes whose reading is unambiguous are named. `WriteError` and
    `WriteTimeout` are deliberately NOT here: a partial send may have delivered a
    complete request, so they keep the expensive default. Neither is the base
    `TransportError`, which would swallow the read errors below into whichever
    branch came first.
    """
    try:
        import httpx
    except ImportError:  # pragma: no cover — optional dependency.
        return None
    # Nothing was written: the connection was never obtained, or the URL never
    # resolved to one. Same reading as botocore's `EndpointConnectionError`.
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
                        httpx.UnsupportedProtocol, httpx.InvalidURL)):
        return NOT_SUBMITTED
    # The request left and the answer did not arrive, or arrived broken. This is the
    # measured expensive case, and it is the same one `ReadTimeoutError` names for
    # botocore: the model may have run to completion and been billed for it.
    if isinstance(exc, (httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError)):
        return SUBMITTED_UNSETTLED
    return None


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

    # The same two readings for the OTHER transport. Every route that reaches the
    # Bedrock OpenAI-compatible endpoint raises `httpx` exceptions, and none of them
    # was recognised here: they all fell through to the catch-all and held a full
    # ceiling, including the ones that provably never reached the provider. The
    # catch-all is the right default for something unrecognised and the wrong answer
    # for a TLS handshake that failed, so the recognisable ones are named.
    #
    # Imported lazily and guarded: this module is imported by the Converse paths in
    # environments that do not install `httpx`, and a money classifier must not fail
    # to load because an optional dependency is absent.
    _httpx_state = _classify_httpx(exc)
    if _httpx_state is not None:
        return _httpx_state

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


#: The correlation marker (C13.6): each key, and the HTTP request header that
#: carries it to a self-hosted endpoint. This is the one table of the marker's
#: keys; `MARKER_KEYS`, `RESERVED_METADATA_KEYS` and `attempt_request_metadata`
#: are derived from it. What each key names:
#:   sc_tenant      the tenant id
#:   sc_attempt_id  the id of this attempt's money hold
#:   sc_user        whatever the gateway uses as the principal's `user_id`
#: The gateway sends the headers on every inference call; recording them is the
#: endpoint's job.
MARKER_HEADERS: dict[str, str] = {
    "sc_tenant": "X-Stratoclave-Tenant",
    "sc_attempt_id": "X-Stratoclave-Attempt-Id",
    "sc_user": "X-Stratoclave-User",
}
MARKER_KEYS: tuple[str, ...] = tuple(MARKER_HEADERS)

_MARKER_VALUE_MAX = 256

#: The one rule every marker value passes before it is sent anywhere. Bedrock
#: refuses the WHOLE call (`ValidationException`) when a `requestMetadata` value
#: has a character outside `[a-zA-Z0-9\s:_@$#=/+,-.]` (checked on real Bedrock:
#: "a|b" and non-ASCII refused, "u@x.com/1+2=3,#$:_-." accepted). Tenant ids are
#: chosen by an admin and are not restricted to that set, so an unchecked value
#: would turn every call for that tenant into an error. The rule is that set
#: narrowed to what is also a safe HTTP header value: the only whitespace is an
#: inner space (no tab or line break, nothing leading or trailing). A value that
#: fails is left out, never altered: an altered id would name someone else.
_MARKER_CHAR = r"A-Za-z0-9:_@$#=/+,.\-"
_MARKER_VALUE_RE = re.compile(
    rf"[{_MARKER_CHAR}](?:[{_MARKER_CHAR} ]{{0,{_MARKER_VALUE_MAX - 2}}}[{_MARKER_CHAR}])?")


def marker_value_ok(value: object) -> bool:
    """Whether `value` may be sent as a marker value (see `_MARKER_VALUE_RE`)."""
    return isinstance(value, str) and _MARKER_VALUE_RE.fullmatch(value) is not None


# Keys already reported as dropped by `attempt_request_metadata`, so a tenant
# whose id never fits logs once per process rather than once per call.
_reported_drops: set[str] = set()


def attempt_request_metadata(
    hold_id: Optional[str], tenant_id: Optional[str] = None, user_id: Optional[str] = None,
) -> dict[str, str]:
    """The correlation handle to stamp on a provider call (C13.6).

    Bedrock echoes `requestMetadata` into model invocation log records, verified
    on real Bedrock: a record for a call abandoned on read timeout was retrieved
    by this marker alone and carried the exact token counts. That matters because
    an abandoned caller never learns the provider's own request id, and the log
    record's `identity` is the gateway's single task role, identical for every
    tenant. So this marker is the ONLY thing that can attribute a charge the
    gateway did not observe, which makes stamping it a correctness requirement
    rather than an optimisation.

    Values are ids the gateway minted or holds: the hold id, the tenant id, and
    the principal's `user_id`. Nothing derived from request content. A value that
    is absent is left out; a value that is longer than Bedrock's 256-character
    limit or outside `_MARKER_VALUE_RE` is left out too, and logged once per
    process by key (never the value), so the call is never refused because of its
    marker and a lost attribution is still visible. The other parts are sent.
    """
    given = {"sc_attempt_id": hold_id, "sc_tenant": tenant_id, "sc_user": user_id}
    assert given.keys() == MARKER_HEADERS.keys()
    md: dict[str, str] = {}
    for key, value in given.items():
        if value is None or value == "":
            continue
        value = str(value)
        if marker_value_ok(value):
            md[key] = value
        elif key not in _reported_drops:
            _reported_drops.add(key)
            logger.warning("correlation_marker_value_dropped", key=key,
                           reason="too_long" if len(value) > _MARKER_VALUE_MAX else "characters")
    return md


def marker_headers(marker: Optional[dict]) -> dict[str, str]:
    """The marker as the HTTP request headers a self-hosted endpoint receives.

    Only the gateway's own keys are carried, under their fixed header names, and
    by the same value rule as the Bedrock marker (`marker_value_ok`), so a
    self-hosted endpoint sees exactly the values a Bedrock log record would."""
    marker = marker or {}
    return {header: marker[key] for key, header in MARKER_HEADERS.items()
            if marker_value_ok(marker.get(key))}


# ---------------------------------------------------------------------------
# The OpenAI-compatible routes' correlation marker (issue #247)
# ---------------------------------------------------------------------------
# Converse carries the marker in a dedicated `requestMetadata` kwarg that is
# never part of the content Bedrock tokenises. The OpenAI-compatible routes
# have no such side channel: the OpenAI-standard `metadata` body field is the
# ONLY mechanism Bedrock echoes into the invocation log on these routes
# (measured 2026-09-03, see the issue) and it is also an ordinary field in the
# canonical payload the hard-ceiling bound is computed over
# (docs/design/hard-ceiling.md section 3a: "the byte count over the canonical
# payload the gateway will send ... serialised"). So, unlike Converse, this
# marker's bytes ARE counted toward the bound, and section 3a is explicit:
# "If any code path can modify the payload after the bound is computed,
# either move the computation after it or make that path fail."
#
# The marker's values are not all knowable before the bound is computed,
# though: `sc_tenant` is (`user.org_id`, read at request entry), but
# `sc_attempt_id` is the ledger's own idempotency token, minted inside
# `reserve_credit`/`reserve_credit_for_model` AFTER the bound has gated
# admission (`mvp._pipeline`'s `hold_id = _fresh_idempotency_token()`) —
# threading a caller-chosen id through that money-path primitive instead
# would touch its CCF-retry/idempotency semantics, which is a materially
# bigger and riskier change than this issue calls for.
#
# The fix is `ATTEMPT_ID_PLACEHOLDER`: a same-length stand-in minted into the
# payload BEFORE the survey (by `stamp_openai_metadata_marker`, called from
# inside the payload builder), then swapped for the real hold id AFTER
# reserve (`stamp_attempt_id`). Both are exactly `len(str(uuid.uuid4()))`
# characters, so the swap changes a value, not a length — the payload's
# serialised byte count stays IDENTICAL before and after it, and the bound
# therefore still describes exactly the payload that is sent. This is
# "inside the same computation" in the sense section 3a asks for: the
# metadata key was already part of what the survey measured, nothing is
# appended afterward.

#: The nil UUID — 36 characters, same as `str(uuid.uuid4())` (32 hex digits +
#: 4 hyphens), and already a recognised "unset" convention (`uuid.UUID(int=0)`),
#: so a reader grepping an invocation log for this exact string sees "no
#: attempt id was available" rather than a value that could pass for a real
#: one. Kept as a named constant (not re-derived from `uuid`) so a change to
#: how hold ids are minted is forced to touch this file rather than silently
#: making the swap in `stamp_attempt_id` a length-changing — and therefore
#: skipped — one.
ATTEMPT_ID_PLACEHOLDER = "00000000-0000-0000-0000-000000000000"

#: OpenAI's documented limits on the `metadata` request field: at most 16
#: key/value pairs, keys up to 64 characters, values up to 512. Checked here
#: (issue #247 acceptance criterion) because the gateway's own two keys must
#: fit with headroom for a caller's, not be discovered at a 400. The 16-key
#: ceiling is also Bedrock's OWN measured, enforced limit, not only OpenAI's
#: documentation: sent 17 keys directly against the real `us-east-2` endpoint
#: and got back `HTTP 400 "Request metadata cannot contain more than 16
#: key/value pairs"` — full real-machine verification log in the PR that
#: closed this issue.
OPENAI_METADATA_MAX_KEYS = 16
OPENAI_METADATA_MAX_KEY_LEN = 64
OPENAI_METADATA_MAX_VALUE_LEN = 512

# The gateway's own keys are far inside both per-pair limits (longest key is
# "sc_attempt_id", 13 chars; `attempt_request_metadata` sends no value longer
# than 256 characters, itself inside OpenAI's 512-char cap),
# so only the 16-key ceiling can actually be violated by adding them.
assert len(ATTEMPT_ID_PLACEHOLDER) <= OPENAI_METADATA_MAX_VALUE_LEN
assert marker_value_ok(ATTEMPT_ID_PLACEHOLDER)

#: The `sc_*` names this module ever puts in `metadata` are RESERVED — a
#: caller's own value under one of these names is never forwarded, whether or
#: not the gateway's marker ends up fitting this particular request. Without
#: this, a caller who pads `metadata` with filler keys up to the 16-key
#: ceiling plus a same-named `sc_tenant`/`sc_attempt_id` of their own choosing
#: could force `merge_openai_metadata` onto its "drop the whole marker, return
#: the caller's metadata unchanged" path and have THEIR value — a different
#: tenant's id, another hold's id — reach the invocation log looking exactly
#: like the gateway's own correlation marker. Found in review (issue #247);
#: pinned by `TestMergeOpenAIMetadata::test_a_caller_cannot_forge_the_reserved_keys_by_padding_metadata_to_the_ceiling`.
#:
#: Derived from `MARKER_HEADERS`, the same table `attempt_request_metadata`
#: is checked against, so the names a caller may not use and the names the
#: gateway sends cannot drift apart.
RESERVED_METADATA_KEYS = frozenset(MARKER_HEADERS)


def merge_openai_metadata(
    caller_metadata: object, marker: dict[str, str]
) -> dict[str, str]:
    """Merge the gateway's `marker` (namespaced `sc_*`) into a caller's own
    OpenAI-standard `metadata` field.

    Collision rule (issue #247, "decide and document the collision rule"):
    **the gateway's keys win, unconditionally.** Every key the caller sent
    under one of `RESERVED_METADATA_KEYS` is dropped FIRST, before anything
    else — not overwritten in place, dropped, so there is no path (including
    the key-count-ceiling path below) on which a caller-supplied value can
    end up stored under a name the gateway reserves for its own correlation
    marker. Every OTHER key the caller sent is kept exactly as sent.

    Count rule: a caller already at OpenAI's `OPENAI_METADATA_MAX_KEYS`
    ceiling (counting only the caller's own, non-reserved keys) must not be
    pushed over it by a field THIS gateway adds — "never break a valid caller
    request" (issue #247's acceptance criteria). The marker is added only as
    a whole: a half-stamped marker (tenant without an attempt id, or the
    reverse) is not a smaller correlation handle, it is a misleading one, so
    when the two gateway keys cannot both fit without exceeding the ceiling,
    NEITHER is added — the caller's own (reserved-keys-stripped) metadata is
    returned as is, and the attempt is not reconcilable by this marker. This
    is logged, since it is otherwise a silent gap in the reconciliation claim.

    `caller_metadata` is accepted as `object`, not `Optional[dict]`: a caller
    may send any JSON value there (or none), and this function's job is to
    decide what the GATEWAY does with it, not to validate the caller's
    request — a non-dict value is treated as absent here and is left for
    Bedrock's own validation to accept or reject, unchanged, byte-for-byte.
    """
    base: dict[str, str] = (
        dict(caller_metadata) if isinstance(caller_metadata, dict) else {}
    )
    # Strip the reserved names BEFORE counting or merging, regardless of
    # whether `marker` happens to use all of them this call (e.g. `tenant_id`
    # was falsy so `marker` has only `sc_attempt_id`) — the namespace is
    # reserved in full, not key-by-key per call. Matched case- and
    # whitespace-insensitively (review finding): a caller sending `SC_TENANT`
    # or `sc_tenant ` must not survive as a decoy that a case-insensitive log
    # query could mistake for the gateway's own field, even though it would
    # not literally collide with the lowercase, untrimmed key the marker
    # itself uses.
    _reserved_normalised = {k.casefold() for k in RESERVED_METADATA_KEYS}
    scrubbed = {
        k: v for k, v in base.items()
        if k.strip().casefold() not in _reserved_normalised
    }
    if not marker:
        return scrubbed
    # Every key in `marker` is now guaranteed to be a NEW addition to
    # `scrubbed` (its own keys are drawn from `RESERVED_METADATA_KEYS`, all of
    # which were just removed, case- and whitespace-insensitively), so the
    # count check below has no collision case to special-case — exactly what
    # closes the forgery path: a caller cannot "pre-occupy" a reserved slot
    # to make it free.
    if len(scrubbed) + len(marker) > OPENAI_METADATA_MAX_KEYS:
        # `info`, not `warning`: a caller choosing to use a lot of `metadata`
        # keys is an ordinary, caller-triggerable condition, not a gateway
        # anomaly -- reserving `warning` for the shapes that actually
        # indicate something is wrong (see `stamp_attempt_id`'s `error` on a
        # hold id of the wrong shape, below).
        logger.info(
            "openai_metadata_marker_dropped_key_ceiling",
            caller_key_count=len(scrubbed),
            marker_key_count=len(marker),
            max_keys=OPENAI_METADATA_MAX_KEYS,
        )
        return scrubbed
    merged = dict(scrubbed)
    merged.update(marker)
    return merged


def stamp_openai_metadata_marker(
    payload: dict, tenant_id: Optional[str], *, user_id: Optional[str],
) -> bool:
    """Merge the gateway's correlation keys into `payload["metadata"]`,
    BEFORE the hard-ceiling survey runs over `payload` — see the module-level
    note above for why `sc_attempt_id` is a same-length placeholder here
    rather than the real hold id (which does not exist yet).

    Returns whether the marker was added (`False` exactly when
    `merge_openai_metadata` dropped it for being over OpenAI's key-count
    ceiling) — the caller uses this to decide whether `stamp_attempt_id`
    later has anything to swap.

    When the full marker does not fit under the ceiling, the attempt and tenant
    pair is tried without `sc_user` before giving up: that pair is what fitted
    before `sc_user` existed, and it is the part reconciliation needs. The pair
    itself is still all or nothing (`merge_openai_metadata`).
    """
    # Built from `attempt_request_metadata` — the SAME function Converse
    # calls — rather than re-typing `"sc_attempt_id"`/`"sc_tenant"` here, so
    # the two routes' key names cannot drift apart ("the same identifiers
    # Converse carries" is then true by construction, not by two call sites
    # agreeing on two string literals).
    marker = attempt_request_metadata(ATTEMPT_ID_PLACEHOLDER, tenant_id, user_id)
    merged = merge_openai_metadata(payload.get("metadata"), marker)
    if merged.get("sc_attempt_id") != ATTEMPT_ID_PLACEHOLDER and "sc_user" in marker:
        without_user = {k: v for k, v in marker.items() if k != "sc_user"}
        merged = merge_openai_metadata(payload.get("metadata"), without_user)
    if merged.get("sc_attempt_id") != ATTEMPT_ID_PLACEHOLDER:
        # The key-count ceiling made `merge_openai_metadata` drop the whole
        # marker (the caller's own `sc_attempt_id`, if any, was stripped by
        # `RESERVED_METADATA_KEYS` on the way in, so this is never a
        # caller-supplied value masquerading as the placeholder). There is
        # nothing of ours to swap later.
        if merged:
            payload["metadata"] = merged
        return False
    payload["metadata"] = merged
    return True


def stamp_attempt_id(payload: dict, hold_id: Optional[str]) -> None:
    """Replace `ATTEMPT_ID_PLACEHOLDER` in `payload["metadata"]` with the
    real hold id, now that reserve has minted one. Called once, right after
    the hold is opened and before the payload is sent.

    Relies on one invariant held by every call site today: ONE hold is opened
    per payload object (docs/design/hard-ceiling.md section 3a's retry rule —
    a retry reuses its hold rather than opening a second one against the same
    payload). If that ever stops holding, restore the placeholder before
    opening a second hold against a payload this function has already
    touched, or rebuild the payload instead of reusing it.

    Checked (review finding), not merely argued: this swap changes
    `envelope_bytes(payload)` by zero, but does it change the SURVEYED HASH
    too? No — `survey_and_hash_openai_responses_payload` /
    `survey_and_hash_openai_chat_payload` walk `input`/`instructions`/`tools`
    only (same reasoning as `mvp.anthropic`'s Converse surveyor); `metadata`
    is never part of either function's text/hash accumulation. The hash
    `reserve_credit_for_model` pins to the hold (`payload_hash`) is therefore
    identical whether it is taken before or after this swap, and recording it
    as "over the placeholder form" would be describing a dependency that does
    not exist. If a future change ever makes a surveyor hash `metadata` too,
    this comment is the thing that stops being true first — update it there.

    A no-op (leaves `metadata` exactly as `stamp_openai_metadata_marker` left
    it) if that function never added the placeholder in the first place
    (dropped for the key-count ceiling, or `payload` was built by a path that
    does not call it at all — a standalone unit test, say). The swap is keyed
    on the CURRENT VALUE being exactly the placeholder — never on the key
    merely being present — so a caller-forged `sc_attempt_id` (already
    impossible via `merge_openai_metadata`'s reserved-key stripping, but
    checked again here independently) can never be mistaken for ours and
    overwritten in place, and this function never INSERTS the key either.

    If `hold_id` is falsy, or its UTF-8 byte length ever stops matching
    `ATTEMPT_ID_PLACEHOLDER`'s (defensive: every `hold_id` in this codebase is
    `str(uuid.uuid4())`, always 36 ASCII bytes, pinned by
    `test_provider_outcome.py`'s check against `mvp._pipeline`'s actual
    minting function), the placeholder is DELETED rather than left in place
    (review finding: leaving it would put the same 36 zero characters on
    every such request — a marker that looks real, joins to nothing, and is
    indistinguishable from every other request this ever happens to, which is
    worse than sending no marker). Deleting only ever SHRINKS the payload, so
    it cannot move the serialised byte count above what the bound was
    computed over — shrinking after the bound was computed is always sound,
    only growing is the case section 3a forbids.
    """
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return
    if metadata.get("sc_attempt_id") != ATTEMPT_ID_PLACEHOLDER:
        return
    value = str(hold_id)[:256] if hold_id else ""
    if value and len(value.encode("utf-8")) == len(ATTEMPT_ID_PLACEHOLDER.encode("utf-8")):
        metadata["sc_attempt_id"] = value
        return
    # No usable id: remove `sc_attempt_id` rather than ship a placeholder
    # that would be indistinguishable from every other request's. `sc_tenant`
    # (if present) is left alone — unlike the attempt id, it was already
    # resolved to its real value at build time (`stamp_openai_metadata_marker`
    # reads `tenant_id` directly, with no placeholder stage), so it is true
    # information, not a stand-in, and discarding it would throw away a
    # correct, available correlation handle for no reason. This also matches
    # what Converse already does on the SAME condition — a hold with no
    # `hold_id` (an unpooled "accounting" tenant per hard-ceiling.md section
    # 7a: no dollar pool means no hold row, hence no hold id at all, equally
    # on both transports) still sends `attempt_request_metadata`'s
    # `sc_tenant` alone; there is no reason for this route to be stricter.
    metadata.pop("sc_attempt_id", None)
    if not metadata:
        payload.pop("metadata", None)
    if not hold_id:
        # The ordinary, expected case: an unpooled ("accounting") tenant has
        # no hold id at all (see the docstring above) -- not a gateway
        # anomaly, so `info`, not `warning`.
        logger.info("openai_metadata_marker_dropped_no_hold_id")
    else:
        # `hold_id` was truthy but the wrong shape/length -- THIS is the case
        # that actually indicates something is wrong (a change to how hold
        # ids are minted that this file's assumptions did not keep up with),
        # so it is logged louder.
        logger.error(
            "openai_metadata_marker_dropped_hold_id_wrong_shape",
            hold_id_length=len(str(hold_id)),
        )
