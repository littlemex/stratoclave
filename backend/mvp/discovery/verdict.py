"""Cross-unit shape 1 (the model-discovery change's frozen interfaces document) —
the probe verdict. E7 writes it, E9 invalidates it; two other units read it blind
to this module, which is why every shape below is spelled exactly as frozen
rather than rederived.

One verdict per `(profile_id, invocation)`, stored as its own item in the SAME
table unit 1's promotion candidates live in — never a new table, and never
folded into `mvp.discovery.records.DiscoveredRecord` (that record carries no
price and no verdict; see that module and the master contract's A16). The
pk/sk scheme is disjoint from the candidate row's own (`CANDIDATE#{profile_id}`
/ `CANDIDATE`) and from an identifier reservation's (`IDENTIFIER#{id}` /
`RESERVATION`), so the three item shapes never collide on one table.

`invocation`, not `mode`: `mvp.pricing_feeds.dimensions.MODES` already names
the PRICING axis (`standard`/`batch`/`flex`/`priority`), and the selector only
ever prices `standard` today, so probing per pricing mode would mint exactly
one verdict and prove nothing. `invocation` is the real axis a probe can fail
independently on — `sync` vs `stream` — because streaming delivers usage in a
trailing metadata event and can pass unary while failing streamed. Reusing
"mode" for both would have two units reading this verdict off two different
axes without either noticing.

**"Current" means `state == "verified"`. There is no clock.** A verdict does
not expire — see `invalidate_verdict` and `on_served_traffic_outcome` for the
only two ways it stops being current. Written once, plainly, because the whole
point of E9 is that nothing here reads a timestamp to decide currency.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from boto3.dynamodb.conditions import Key as boto3_key
from botocore.exceptions import ClientError

from dynamo.client import get_dynamodb_resource, promotion_candidates_table_name

from .records import ObservationScope, _json_safe

SCHEMA_VERSION = 1

_VERDICT_PK_PREFIX = "VERDICT#"
_VERDICT_SK_PREFIX = "INVOCATION#"

#: The invocation axis's closed vocabulary. Not named anywhere in the frozen
#: cross-unit shape (it names only the field, `invocation: str`) — this unit's
#: own decision, ratified: `sync` (a single non-streamed Converse call) and
#: `stream` (a streamed one, whose usage arrives in a trailing metadata event
#: and can therefore fail independently of the unary path — D6's second
#: invalidating signal, "a 200 with absent usage counters", is specifically a
#: streaming failure mode).
INVOCATION_SYNC = "sync"
INVOCATION_STREAM = "stream"
INVOCATION_VALUES = frozenset({INVOCATION_SYNC, INVOCATION_STREAM})

#: `verified_by`'s closed vocabulary, frozen. `"probe"` — E7 minted this verdict
#: by paying for a dedicated Converse call. `"production"` is named in the
#: freeze but nothing in this unit's scope mints it (see the module docstring
#: in `probe.py` for why the production-refresh half of D6 is left unbuilt);
#: kept here because a store that could not even round-trip the value the
#: freeze names would be a narrower store than the one two other units read.
VERIFIED_BY_PROBE = "probe"
VERIFIED_BY_PRODUCTION = "production"
VERIFIED_BY_VALUES = frozenset({VERIFIED_BY_PROBE, VERIFIED_BY_PRODUCTION})

#: `state`'s closed vocabulary, frozen.
STATE_VERIFIED = "verified"
STATE_INVALIDATED = "invalidated"
STATE_VALUES = frozenset({STATE_VERIFIED, STATE_INVALIDATED})

#: Named reasons `invalidate_verdict` is called with. Not part of the frozen
#: cross-unit shape (nothing reads this back off the verdict itself — it is
#: recorded only in the log line, so two call sites can't spell one cause two
#: different ways).
REASON_TYPED_PROTOCOL_FAILURE = "typed_protocol_failure"
REASON_METERING_FAULT = "metering_fault"
REASON_PRICING_KEY_CHANGED = "pricing_key_changed"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ProbeVerdict:
    """Exactly the fields the freeze names, in the order it names them
    (`mode` renamed `invocation` per the ratified correction)."""

    profile_id: str
    observation_scope: ObservationScope
    invocation: str
    verified_at: str
    verified_by: str
    pricing_key_at_verification: str
    wire_protocol_verified: str
    state: str

    def __post_init__(self) -> None:
        if self.invocation not in INVOCATION_VALUES:
            raise ValueError(
                f"unknown invocation {self.invocation!r}; must be one of "
                f"{sorted(INVOCATION_VALUES)}"
            )
        if self.verified_by not in VERIFIED_BY_VALUES:
            raise ValueError(
                f"unknown verified_by {self.verified_by!r}; must be one of "
                f"{sorted(VERIFIED_BY_VALUES)}"
            )
        if self.state not in STATE_VALUES:
            raise ValueError(
                f"unknown state {self.state!r}; must be one of {sorted(STATE_VALUES)}"
            )


class ProbeVerdictStoreUnavailable(Exception):
    """A read or write of the verdict store failed and nothing was read or
    written. Mirrors `mvp.discovery.records.DiscoveredRecordStoreUnavailable`
    exactly, for the same reason that class gives: an unreadable store is not
    evidence a verdict does not exist."""


def _table():
    return get_dynamodb_resource().Table(promotion_candidates_table_name())


def _pk(profile_id: str) -> str:
    return f"{_VERDICT_PK_PREFIX}{profile_id}"


def _sk(invocation: str) -> str:
    return f"{_VERDICT_SK_PREFIX}{invocation}"


def _to_item(verdict: ProbeVerdict) -> dict[str, Any]:
    scope = verdict.observation_scope
    item = {
        "pk": _pk(verdict.profile_id),
        "sk": _sk(verdict.invocation),
        "schema_version": SCHEMA_VERSION,
        "profile_id": verdict.profile_id,
        "invocation": verdict.invocation,
        "verified_at": verdict.verified_at,
        "verified_by": verdict.verified_by,
        "pricing_key_at_verification": verdict.pricing_key_at_verification,
        "wire_protocol_verified": verdict.wire_protocol_verified,
        "state": verdict.state,
        "observation_scope": {
            "account": scope.account,
            "region": scope.region,
            "credentials_fingerprint": scope.credentials_fingerprint,
            "observed_at": scope.observed_at,
        },
    }
    # One conversion for the whole item, not per field — see
    # `mvp.discovery.records._json_safe`'s own docstring for why a per-field
    # fix is a fix to one instance rather than to the defect (A9 in the
    # master contract's amendment log). Reused verbatim rather than
    # reimplemented: a second float-safety function in the same package is a
    # second answer to a question `records.py` already answers.
    return _json_safe(item)


def _from_item(item: Mapping[str, Any]) -> Optional[ProbeVerdict]:
    schema = item.get("schema_version")
    try:
        schema = int(schema)
    except (TypeError, ValueError):
        schema = None
    if schema != SCHEMA_VERSION:
        return None
    scope_raw = item.get("observation_scope") or {}
    if not isinstance(scope_raw, Mapping):
        return None
    try:
        return ProbeVerdict(
            profile_id=str(item.get("profile_id") or ""),
            observation_scope=ObservationScope(
                account=str(scope_raw.get("account") or ""),
                region=str(scope_raw.get("region") or ""),
                credentials_fingerprint=str(scope_raw.get("credentials_fingerprint") or ""),
                observed_at=str(scope_raw.get("observed_at") or ""),
            ),
            invocation=str(item.get("invocation") or ""),
            verified_at=str(item.get("verified_at") or ""),
            verified_by=str(item.get("verified_by") or ""),
            pricing_key_at_verification=str(item.get("pricing_key_at_verification") or ""),
            wire_protocol_verified=str(item.get("wire_protocol_verified") or ""),
            state=str(item.get("state") or ""),
        )
    except ValueError:
        # An unrecognised `invocation`/`verified_by`/`state` is skipped rather
        # than raised — same posture as `records._from_item` on an
        # unrecognised blocker type: a row this build cannot parse is not
        # evidence of absence.
        return None


def get_probe_verdict(profile_id: str, invocation: str) -> Optional[ProbeVerdict]:
    """The one verdict `(profile_id, invocation)` owns, consistently."""
    try:
        resp = _table().get_item(
            Key={"pk": _pk(profile_id), "sk": _sk(invocation)},
            ConsistentRead=True,
        )
    except ClientError as exc:
        raise ProbeVerdictStoreUnavailable(
            f"verdict store unreachable reading profile_id={profile_id!r} "
            f"invocation={invocation!r}: {exc}"
        ) from exc
    item = resp.get("Item")
    return _from_item(item) if item else None


def list_probe_verdicts() -> list[ProbeVerdict]:
    """Every verdict, via the table's `pk` prefix. A `Scan` filtered to the
    `VERDICT#` prefix rather than a GSI query: unlike unit 1's candidates
    (queried by `sk="CANDIDATE"` over a GSI keyed on state) and unlike the
    discovered-record overlay (queried by `tenant_id` over the shared table's
    own GSI), this table's partition key is per-row (`VERDICT#{profile_id}`,
    one partition per profile), so there is no single partition or GSI key to
    query across every verdict — a scan is the only read shape that reaches
    all of them regardless of `profile_id`. Listing every verdict is an
    operator/activation-time operation, not a hot path, so the scan's cost is
    the right trade against building an index this table does not otherwise
    need.
    """
    try:
        verdicts: list[ProbeVerdict] = []
        kwargs: dict[str, Any] = {
            "FilterExpression": boto3_key("pk").begins_with(_VERDICT_PK_PREFIX),
        }
        while True:
            resp = _table().scan(**kwargs)
            for item in resp.get("Items", []):
                parsed = _from_item(item)
                if parsed is not None:
                    verdicts.append(parsed)
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
    except ClientError as exc:
        raise ProbeVerdictStoreUnavailable(
            f"verdict store unreachable listing verdicts: {exc}"
        ) from exc
    return verdicts


def put_probe_verdict(verdict: ProbeVerdict) -> None:
    """Full-replace write of the one verdict `(verdict.profile_id,
    verdict.invocation)` owns. Unconditional: a fresh probe pass, an
    invalidation, and (were it ever built) a production refresh are all "the
    current, complete belief about this invocation", not an append-only log —
    same posture as `records.put_discovered_record`.
    """
    try:
        _table().put_item(Item=_to_item(verdict))
    except ClientError as exc:
        raise ProbeVerdictStoreUnavailable(
            f"verdict store unreachable writing profile_id={verdict.profile_id!r} "
            f"invocation={verdict.invocation!r}: {exc}"
        ) from exc


def invalidate_verdict(profile_id: str, invocation: str, *, reason: str) -> bool:
    """Transition a `"verified"` verdict to `"invalidated"`. Idempotent: a
    verdict that is already invalidated, or does not exist, is left alone and
    this returns `False` — a caller on a money path (see `on_served_traffic_
    outcome`) must never be given a reason to retry or to treat "nothing to
    invalidate" as an error.

    Every other field is carried forward unchanged (`dataclasses.replace`):
    invalidation is a statement about currency, not a new measurement, so it
    must not overwrite `verified_at`/`pricing_key_at_verification`/
    `wire_protocol_verified` with anything — a reader asking "what was this
    verdict's evidence before it stopped being trusted" needs the original
    values still there.

    `reason` is logged, not stored on the verdict: the frozen shape has no
    field for it (cross-unit shape 1 lists exactly eight fields), and adding
    a ninth here would be this unit unilaterally widening a shape two blind
    readers already depend on.
    """
    current = get_probe_verdict(profile_id, invocation)
    if current is None or current.state != STATE_VERIFIED:
        return False
    put_probe_verdict(replace(current, state=STATE_INVALIDATED))
    from core.logging import get_logger

    get_logger(__name__).warning(
        "probe_verdict_invalidated",
        profile_id=profile_id, invocation=invocation, reason=reason,
    )
    return True


#: Service error codes `is_typed_protocol_failure` must NOT treat as a typed
#: protocol failure — named here, once, so the "5xx and a throttle do not
#: invalidate" rule is a set membership test rather than a judgement call
#: repeated at every call site. `ThrottlingException` is deliberately absent
#: from the typed-failure side even though `mvp.provider_outcome` shares its
#: liability bucket (`REJECTED_PRE_INFERENCE`) with `ValidationException` —
#: that module answers "does this cost money", a different question from
#: "does this mean the binding is broken", and the two must not be confused
#: here (seam S2 in the split document).
_NON_INVALIDATING_CODES = frozenset({
    "ThrottlingException",
    "ServiceUnavailableException",
    "InternalServerException",
    "ModelTimeoutException",
    "ModelErrorException",
})

#: Substrings a `ValidationException` message carries when the model or the
#: operation itself is unsupported, as opposed to a malformed parameter on an
#: otherwise-valid call (an over-limit `maxTokens`, an empty text block — see
#: `mvp.provider_outcome`'s own measured example, which is a
#: `REJECTED_PRE_INFERENCE` case that must NOT invalidate a verdict about the
#: binding). No AWS-verified string for "Converse unsupported for this model"
#: was found anywhere in this repository (unlike the agreement-offer
#: classifier's strings, which are real-machine measured) — this is a
#: judgement call on the vocabulary such a message would plausibly use, not a
#: measured fact.
_UNSUPPORTED_OPERATION_MARKERS = (
    "does not support",
    "doesn't support",
    "not supported",
    "unsupported",
)


def is_typed_protocol_failure(exc: BaseException) -> bool:
    """Whether `exc` is D6's first invalidating signal: "a validation error
    naming Converse unsupported, or a 200 with absent usage counters." This
    function answers only the first half — the exception-shaped half; the
    second half (a clean response with no usage) is `metering_fault`, passed
    to `on_served_traffic_outcome` as a bool rather than an exception, because
    `mvp._money.claim_settle` has already made that determination before this
    module is ever consulted (see that call site).

    **The bias, deliberate and stated because the classifier itself is not
    verified against a real message (see `_UNSUPPORTED_OPERATION_MARKERS`):
    an exception this function does not recognise returns `False`, never
    `True`.** Every branch below that is not a confirmed match falls through
    to the same default at the bottom. Unlike the agreement-offer classifier
    this mirrors the shape of (`mvp.pricing_feeds.agreement`), that one has a
    real-machine-verified string to match first and only falls back to
    "actionable" (never silently "safe") for the unmatched case, because being
    wrong there costs a person's attention. Being wrong here costs a working
    verdict: an unrecognised error must leave the verdict standing rather than
    clear it on a hiccup this classifier merely failed to name. So the
    fallback is the SAFE direction here precisely because it is the opposite
    of that module's — a test asserting "unrecognised, therefore not a typed
    failure" is exercising this bias on purpose, not merely a default nobody
    chose.

    Concretely: `True` only for a `ClientError` whose code is
    `ValidationException` and whose message names the model or the operation
    as unsupported. A generic `ValidationException` (a bad parameter on an
    otherwise-servable model) is `False`: it is a fact about this one request,
    not about whether Converse works against this model at all. A
    `ThrottlingException`, any 5xx-shaped failure, anything not a
    `ClientError`, or a `ClientError` this function cannot classify is `False`
    — see `_NON_INVALIDATING_CODES` and the unconditional default below.
    """
    if not isinstance(exc, ClientError):
        return False
    code = str(exc.response.get("Error", {}).get("Code", ""))
    if code in _NON_INVALIDATING_CODES:
        return False
    if code != "ValidationException":
        return False
    message = str(exc.response.get("Error", {}).get("Message", "")).lower()
    if any(marker in message for marker in _UNSUPPORTED_OPERATION_MARKERS):
        return True
    # Recognised code, unrecognised message shape: the safe default, per this
    # function's own stated bias above, not an oversight.
    return False


def on_served_traffic_outcome(
    profile_id: str, invocation: str, *,
    exc: Optional[BaseException] = None,
    metering_fault: bool = False,
) -> bool:
    """The one seam production traffic's outcome calls into. Two callers, both
    in `mvp._money` (see that module for why touching it here is in scope: the
    master contract's scope boundary names "the settle path for the metering
    fault" explicitly):

    - `Hold.claim_settle`, with `metering_fault=True`, when a clean completion
      never reported usage — D8's second signal, already detected there by
      E10; this call is the missing half that makes it also invalidate.
    - `Hold.claim_unobserved`, with `exc=` the exception that ended the
      attempt, whenever the provider was actually reached (S2: a refused
      grant never reaches this function, because it never reaches `reserve_
      credit`/`Hold` at all — refusal happens before a `Hold` exists).

    `metering_fault` takes precedence when both could theoretically be true
    (they cannot be, in practice — `claim_settle` and `claim_unobserved` are
    mutually exclusive endings for one `Hold` — but the precedence is stated
    rather than left to argument order).

    Best-effort by construction at BOTH call sites, never here: this function
    itself still raises on a genuine store fault
    (`ProbeVerdictStoreUnavailable`) rather than swallowing it, because a
    caller that cannot tell "nothing to invalidate" from "the store is down"
    cannot decide whether to also log it. It is the CALLER's job — the money
    path's — to decide that a discovery-store fault must never break a
    settle, and to catch broadly there; see `mvp._money`'s two call sites for
    that catch.
    """
    if metering_fault:
        return invalidate_verdict(profile_id, invocation, reason=REASON_METERING_FAULT)
    if exc is not None and is_typed_protocol_failure(exc):
        return invalidate_verdict(profile_id, invocation, reason=REASON_TYPED_PROTOCOL_FAILURE)
    return False


def invalidate_for_pricing_key_change(
    profile_id: str, invocation: str, new_pricing_key: str,
) -> bool:
    """D6's third signal: the registry `pricing_key` changing invalidates,
    because assertion 4 was made against the old one. Provided as a callable
    mechanism rather than wired to a call site: no unit's scope currently
    mutates an existing candidate's or entry's `pricing_key` after creation
    (G4 sets it once, required, with no update path; unit 5's activation-time
    comparison in cross-unit shape 4 REFUSES a mismatch rather than
    invalidating a stored verdict, which is a different moment — before
    activation, not after). Left unwired rather than invented a call site for;
    see this unit's report for the explicit statement of this gap.

    A no-op (returns `False`) when the current verdict's own `pricing_key_at_
    verification` already equals `new_pricing_key` — nothing changed.
    """
    current = get_probe_verdict(profile_id, invocation)
    if current is None or current.pricing_key_at_verification == new_pricing_key:
        return False
    return invalidate_verdict(profile_id, invocation, reason=REASON_PRICING_KEY_CHANGED)
