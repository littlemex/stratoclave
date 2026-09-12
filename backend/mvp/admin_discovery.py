"""The discovery operator surfaces: the queue (below), and everything an
operator needs to read a discovered record, review and create a promotion
candidate, probe it, and activate it -- the read path and the three write
surfaces the mechanism has had, unreachable, since promotion and activation
merged. See this package's own change history for why the read path exists
at all: three write surfaces with no way to learn what to probe, no way to
see a blocker's evidence, and no way to see whether a candidate is verified
are unusable by the only person who would use them.

A discovered record can carry a blocker that is PERMANENT -- most concretely,
`no_agreement_offer`'s `not_marketplace_metered` subtype, which is the normal
shape for every AWS-billed family this account can see (measured against a
real account: 15 of the 75 discovered profiles). Listing every blocker in the
QUEUE, unfiltered, would make it permanently full of profiles nobody can do
anything about, and a queue nobody can clear is a queue nobody reads -- the
same failure mode `mvp.discovery.reconcile`'s own `--strict` gate exists to
avoid. So the queue is scoped to TASK blockers only, deciding "task" by
importing `mvp.discovery.reconcile.is_actionable_blocker` -- the exact
predicate `--strict` already classifies `actionable_blocker` findings with --
rather than writing a second classifier here. An operator staring at this
queue and an operator staring at that command's exit code must never
disagree about whether a given blocker is a task, and the only way to
guarantee that is to share the one function that decides it.

The record surfaces below (`GET /records`, `GET /records/{profile_id}`) are
deliberately NOT scoped that way: a permanent blocker is not a task, but an
operator asking "why can I not promote this" needs the answer, and the queue
filtered it out by design. Both surfaces list every blocker a record carries.

**One error vocabulary.** Every refusal below reuses the typed
`{"type": ..., "message": ...}` shape (widened with a `field` or a `blocker`
key where the contract calls for one) that the discovery/promotion/
activation/entitlement layers already return as `PromotionRefused.reason`,
`ActivationRefused.reason`, `ProbeAttemptRefused.reason`, and
`mvp.discovery.records.Blocker`'s own three fields. No second vocabulary is
invented here; this module only ever picks the HTTP status a reason maps to.

**Permissions.** `models:discover` gates every read below; `models:promote`
gates all three writes, including the probe -- a discover-only principal
who could mint a probe verdict would hold half the promote capability, since
the verdict is activation's own input.

**The probe targets a candidate, never the raw record.** A raw record has no
pricing key and no declared wire protocol, so a route that probed a record
directly would need the human to type the price twice (once now, once at
candidate creation) and activation's agreement check would then be comparing
two human inputs instead of a claim against a measurement. `POST .../probe`
below reads the candidate, takes ITS pricing key and ITS declared protocol,
and probes with those.

**A failed assertion is a completed operation.** The probe route answers 200
whether or not the four assertions passed -- a failed verdict is not an
error, it is the operation's actual result. Only an operation that could not
even be attempted (a permanent blocker, a stale candidate revision, a ledger
refusal, an indeterminate provider timeout) answers something other than 200.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .authz import require_permission
from .deps import AuthenticatedUser
from .discovery.activation import ActivationRefused, ActivationStoreUnavailable, activate_candidate
from .discovery.ledger import ProbeAttemptRefused
from .discovery.probe import INDETERMINATE_SUBTYPE, probe
from .discovery.promotion import (
    PromotionCandidate,
    PromotionRefused,
    PromotionStoreUnavailable,
    default_model_collision_warning,
    derive_candidate,
    get_promotion_candidate,
    list_promotion_candidates,
    newly_live_identifiers,
    put_promotion_candidate,
)
from .discovery.queue import actionable_blockers_of
from .discovery.reconcile import is_actionable_blocker
from .discovery.records import (
    Blocker,
    DiscoveredRecord,
    DiscoveredRecordStoreUnavailable,
    get_discovered_record,
    list_discovered_records,
)
from .discovery.verdict import (
    INVOCATION_VALUES,
    ProbeVerdict,
    ProbeVerdictStoreUnavailable,
    get_probe_verdict,
)

router = APIRouter(prefix="/api/mvp/admin/discovery", tags=["admin-discovery"])


class QueueBlocker(BaseModel):
    type: str
    subtype: str
    evidence: str
    first_seen: str
    last_seen: str


class QueueEntry(BaseModel):
    profile_id: str
    provider: str
    profile_scope: str
    model_family: str
    # Only the blockers on this profile that are actionable -- a permanent
    # blocker riding alongside an actionable one on the same profile is left
    # off this list, for the same reason it never earns the profile a place
    # in `entries` on its own.
    blockers: list[QueueBlocker]


class QueueResponse(BaseModel):
    entries: list[QueueEntry]


def _err_503_store_unavailable() -> HTTPException:
    # Same shape/convention as `admin_entitlements._err_503_store_unavailable`:
    # a `type` a client's retry logic can key on.
    return HTTPException(
        status_code=503,
        detail={
            "type": "discovered_record_store_unavailable",
            "message": "The discovered-record store is temporarily unavailable. Retry shortly.",
        },
    )


@router.get("/queue", response_model=QueueResponse)
def get_discovery_queue(
    actor: AuthenticatedUser = Depends(require_permission("models:discover")),
) -> QueueResponse:
    """Every discovered profile that, as of the last reconciliation pass,
    carries at least one actionable blocker -- the queue an operator works
    from, not a dump of every profile discovery has ever seen.
    """
    try:
        records = list_discovered_records()
    except DiscoveredRecordStoreUnavailable:
        raise _err_503_store_unavailable()
    entries: list[QueueEntry] = []
    for record in records:
        actionable = actionable_blockers_of(record)
        if not actionable:
            continue
        entries.append(QueueEntry(
            profile_id=record.profile_id,
            provider=record.provider,
            profile_scope=record.profile_scope,
            model_family=record.model_family,
            blockers=[
                QueueBlocker(
                    type=b.type, subtype=b.subtype, evidence=b.evidence,
                    first_seen=b.first_seen, last_seen=b.last_seen,
                )
                for b in actionable
            ],
        ))
    return QueueResponse(entries=entries)


# =============================================================================
# Shared error helpers
# =============================================================================
def _err_503(error_type: str, message: str) -> HTTPException:
    """Same shape/convention as `_err_503_store_unavailable` above and
    `mvp.admin_entitlements._err_503_store_unavailable`: a `type` a client's
    retry logic can key on. Generalised to a caller-supplied type/message so
    every store this module reads from (promotion candidates, probe
    verdicts, activated entries) gets the same disposition without four
    near-identical helpers."""
    return HTTPException(status_code=503, detail={"type": error_type, "message": message})


def _blocker_model(blocker: Blocker) -> "QueueBlocker":
    return QueueBlocker(
        type=blocker.type, subtype=blocker.subtype, evidence=blocker.evidence,
        first_seen=blocker.first_seen, last_seen=blocker.last_seen,
    )


# =============================================================================
# Discovered records -- listing, and one record by profile_id
# =============================================================================
class RecordResponse(BaseModel):
    profile_id: str
    provider: str
    profile_scope: str
    model_family: str
    jurisdiction_bounded: bool
    invocation_region: str
    # The record's own revision, for the stale-revision guard on candidate
    # creation, below: the discovered record dataclass carries no dedicated
    # version field, and its blockers'
    # `first_seen`/`last_seen` are per-blocker, not a fact about the whole
    # record. `observation_scope.observed_at` IS a fact about the whole
    # record -- every field on one record comes from the SAME reconciliation
    # pass, stamped once per pass (`mvp.discovery.reconcile.run_pass` reads
    # the clock exactly once, before iterating profiles) and overwritten in
    # full on every subsequent pass (`put_discovered_record` is an
    # unconditional full replace) -- so a caller comparing this value across
    # two reads is comparing exactly "has this record been touched by a
    # reconciliation pass since I last read it", which is what a revision is
    # for here.
    revision: str
    blockers: list[QueueBlocker]


class RecordListResponse(BaseModel):
    records: list[RecordResponse]


def _record_response(record: DiscoveredRecord) -> RecordResponse:
    return RecordResponse(
        profile_id=record.profile_id,
        provider=record.provider,
        profile_scope=record.profile_scope,
        model_family=record.model_family,
        jurisdiction_bounded=record.jurisdiction_bounded,
        invocation_region=record.invocation_region,
        revision=record.observation_scope.observed_at,
        blockers=[_blocker_model(b) for b in record.blockers],
    )


@router.get("/records", response_model=RecordListResponse)
def list_discovery_records(
    actor: AuthenticatedUser = Depends(require_permission("models:discover")),
) -> RecordListResponse:
    """Every discovered record, blockers UNFILTERED -- unlike `/queue`, a
    permanent blocker is listed here rather than omitted: it is not a task,
    but it is the answer to "why can this not be promoted"."""
    try:
        records = list_discovered_records()
    except DiscoveredRecordStoreUnavailable:
        raise _err_503_store_unavailable()
    return RecordListResponse(records=[_record_response(r) for r in records])


@router.get("/records/{profile_id}", response_model=RecordResponse)
def get_discovery_record(
    profile_id: str,
    actor: AuthenticatedUser = Depends(require_permission("models:discover")),
) -> RecordResponse:
    """One discovered record -- the input an operator needs before creating
    a candidate or probing one: its revision, its blockers and their
    evidence (permanent or actionable, both, unlike the queue)."""
    try:
        record = get_discovered_record(profile_id)
    except DiscoveredRecordStoreUnavailable:
        raise _err_503_store_unavailable()
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "record_not_found", "message": f"no discovered record for profile_id={profile_id!r}"},
        )
    return _record_response(record)


# =============================================================================
# Promotion candidates, with their verdicts
# =============================================================================
class VerdictView(BaseModel):
    invocation: str
    # "unverified" when no probe has ever recorded a verdict for this
    # (profile_id, invocation) pair -- a candidate with no verdict reads as
    # unverified rather than as an absent key, so a client never has to
    # special-case "this invocation is simply missing from the map".
    state: str
    verified_at: Optional[str] = None
    verified_by: Optional[str] = None
    pricing_key_at_verification: Optional[str] = None
    wire_protocol_verified: Optional[str] = None


def _verdict_view(invocation: str, verdict: Optional[ProbeVerdict]) -> VerdictView:
    if verdict is None:
        return VerdictView(invocation=invocation, state="unverified")
    return VerdictView(
        invocation=invocation, state=verdict.state, verified_at=verdict.verified_at,
        verified_by=verdict.verified_by,
        pricing_key_at_verification=verdict.pricing_key_at_verification,
        wire_protocol_verified=verdict.wire_protocol_verified,
    )


class CandidateResponse(BaseModel):
    profile_id: str
    state: str
    aliases: list[str]
    bedrock_model_id: str
    bedrock_region: str
    pricing_key: str
    jurisdiction: Optional[str] = None
    provider: str
    wire_protocol: str
    model_family: str
    profile_scope: str
    created_at: str
    created_by: str
    # Aliases plus the Bedrock id, deduplicated -- `PromotionCandidate.
    # identifiers()`'s own answer to "every public identifier this candidate
    # would make reachable".
    identifiers: list[str]
    # Keyed by invocation, not a list: a caller asks "is the sync path
    # verified", and a list makes them search for the answer they already
    # named. Every invocation the gateway knows is present, so a candidate
    # never probed reads as unverified rather than as missing.
    verdicts: dict[str, VerdictView]


class CandidateListResponse(BaseModel):
    candidates: list[CandidateResponse]


def _candidate_response(candidate: PromotionCandidate) -> CandidateResponse:
    verdicts: dict[str, VerdictView] = {}
    for invocation in sorted(INVOCATION_VALUES):
        try:
            verdict = get_probe_verdict(candidate.profile_id, invocation)
        except ProbeVerdictStoreUnavailable:
            raise _err_503(
                "probe_verdict_store_unavailable",
                "The probe verdict store is temporarily unavailable. Retry shortly.",
            )
        verdicts[invocation] = _verdict_view(invocation, verdict)
    return CandidateResponse(
        profile_id=candidate.profile_id, state=candidate.state,
        aliases=list(candidate.aliases), bedrock_model_id=candidate.bedrock_model_id,
        bedrock_region=candidate.bedrock_region, pricing_key=candidate.pricing_key,
        jurisdiction=candidate.jurisdiction, provider=candidate.provider,
        wire_protocol=candidate.wire_protocol, model_family=candidate.model_family,
        profile_scope=candidate.profile_scope, created_at=candidate.created_at,
        created_by=candidate.created_by, identifiers=list(candidate.identifiers()),
        verdicts=verdicts,
    )


def _err_promotion_store_unavailable() -> HTTPException:
    return _err_503(
        "promotion_candidate_store_unavailable",
        "The promotion candidate store is temporarily unavailable. Retry shortly.",
    )


@router.get("/candidates", response_model=CandidateListResponse)
def list_discovery_candidates(
    actor: AuthenticatedUser = Depends(require_permission("models:discover")),
) -> CandidateListResponse:
    """Every promotion candidate, each with the verdict for every invocation
    -- `"unverified"`, never an absent key, when no probe has run yet."""
    try:
        candidates = list_promotion_candidates()
    except PromotionStoreUnavailable:
        raise _err_promotion_store_unavailable()
    return CandidateListResponse(candidates=[_candidate_response(c) for c in candidates])


@router.get("/candidates/{profile_id}", response_model=CandidateResponse)
def get_discovery_candidate(
    profile_id: str,
    actor: AuthenticatedUser = Depends(require_permission("models:discover")),
) -> CandidateResponse:
    try:
        candidate = get_promotion_candidate(profile_id)
    except PromotionStoreUnavailable:
        raise _err_promotion_store_unavailable()
    if candidate is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "candidate_not_found", "message": f"no promotion candidate for profile_id={profile_id!r}"},
        )
    return _candidate_response(candidate)


# =============================================================================
# Create a promotion candidate
# =============================================================================
class CreateCandidateRequest(BaseModel):
    profile_id: str
    # The record's own `revision` (see `RecordResponse.revision`), read by the
    # operator before this call. Required, no default: a stale revision must
    # refuse rather than silently derive from whatever the record has since
    # become.
    revision: str
    # All three of the human decisions are Optional at the wire, and required by
    # the domain. Not because they are optional -- they are not -- but because a
    # required Pydantic field is rejected by the framework with its own error
    # shape, before the one refusal vocabulary this surface promises can name the
    # field in the way every other refusal here names it. Making them Optional
    # moves all three into the same layer, so `aliases` missing and
    # `jurisdiction` missing read alike to whoever is on the other end.
    aliases: Optional[list[str]] = None
    pricing_key: Optional[str] = None
    jurisdiction: Optional[str] = None
    # The human's declared wire protocol -- `mvp.discovery.promotion.
    # derive_candidate`'s own `probe_wire_protocol` parameter, required with
    # no default and validated against the registry's closed
    # `{"messages", "responses"}` set. Not folded into "the three human
    # decisions" `validate_human_inputs` names (aliases, pricing_key,
    # jurisdiction): `derive_candidate` checks this one itself, with its own
    # closed reason (`PROTOCOL_MISMATCH`), because it is a fourth thing
    # nothing about the record can answer. `derive_candidate` takes it as a
    # plain string precisely so this layer never has to import the probe
    # module's storage to supply it (see that function's own docstring); the
    # probe run against the resulting candidate (`POST .../probe`, below)
    # verifies THIS exact value, and activation refuses if what the probe
    # actually verified disagrees with what the candidate still declares.
    # Optional at the wire, required by the domain -- the same reason the three
    # human decisions above are Optional. Measured against a running gateway:
    # omitting this field returned the framework's own error list, so a caller
    # got `[{"type": "missing", "loc": [...]}]` where every other refusal on
    # this surface returns `{"type", "field", "message"}`. The closed-set check
    # below already refuses `None`, and it names the field.
    wire_protocol: Optional[str] = None


class CreateCandidateResponse(BaseModel):
    candidate: CandidateResponse
    newly_live_identifiers: list[str]
    default_model_collision_warning: Optional[str] = None


#: `PromotionRefused.reason` values that name an invalid HUMAN-supplied field
#: on this request, mapped to the field name the 422 body must carry per the
#: contract's own status-code rule ("422 for an invalid human field with the
#: field named"). Every other `PromotionRefused` reason is a fact about the
#: RECORD or the STORE, not about a field the operator typed, and is handled
#: separately below.
_HUMAN_FIELD_REASONS: dict[str, str] = {
    PromotionRefused.ALIAS_REQUIRED: "aliases",
    PromotionRefused.PRICING_KEY_REQUIRED: "pricing_key",
    PromotionRefused.PRICING_KEY_IS_DEFAULT: "pricing_key",
    PromotionRefused.JURISDICTION_REQUIRED: "jurisdiction",
    PromotionRefused.PROTOCOL_MISMATCH: "wire_protocol",
}


def _promotion_refused_to_http(exc: PromotionRefused) -> HTTPException:
    if exc.reason in _HUMAN_FIELD_REASONS:
        return HTTPException(
            status_code=422,
            detail={"type": exc.reason, "field": _HUMAN_FIELD_REASONS[exc.reason], "message": str(exc)},
        )
    if exc.reason == PromotionRefused.RECORD_NOT_FOUND:
        return HTTPException(status_code=404, detail={"type": exc.reason, "message": str(exc)})
    # IDENTIFIER_TAKEN (a state conflict against the store/registry) and
    # PROVIDER_UNSUPPORTED (a fact about the record, not the operator's
    # input) both land here, at 409: neither is a malformed field, and both
    # are the record or the store disagreeing with what this write asked for.
    return HTTPException(status_code=409, detail={"type": exc.reason, "message": str(exc)})


@router.post("/candidates", response_model=CreateCandidateResponse, status_code=201)
def create_promotion_candidate(
    body: CreateCandidateRequest,
    actor: AuthenticatedUser = Depends(require_permission("models:promote")),
) -> CreateCandidateResponse:
    """Create a candidate from a discovered record plus the human decisions
    `derive_candidate` requires. Refuses a stale `revision` before deriving
    anything, so a re-discovery between reading the record and promoting it
    cannot silently change what the candidate derives from."""
    try:
        record = get_discovered_record(body.profile_id)
    except DiscoveredRecordStoreUnavailable:
        raise _err_503_store_unavailable()
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "record_not_found", "message": f"no discovered record for profile_id={body.profile_id!r}"},
        )

    current_revision = record.observation_scope.observed_at
    if current_revision != body.revision:
        raise HTTPException(
            status_code=409,
            detail={
                "type": "stale_revision",
                "submitted_revision": body.revision,
                "current_revision": current_revision,
                "message": (
                    "the discovered record has been re-observed since this "
                    "revision was read; re-fetch the record and retry with its "
                    "current revision"
                ),
            },
        )

    try:
        candidate = derive_candidate(
            record, aliases=body.aliases, pricing_key=body.pricing_key,
            jurisdiction=body.jurisdiction, probe_wire_protocol=body.wire_protocol,
            created_by=actor.user_id,
        )
    except PromotionRefused as exc:
        raise _promotion_refused_to_http(exc)

    warning = default_model_collision_warning(candidate)

    try:
        put_promotion_candidate(candidate)
    except PromotionRefused as exc:
        raise _promotion_refused_to_http(exc)
    except PromotionStoreUnavailable:
        raise _err_promotion_store_unavailable()

    return CreateCandidateResponse(
        candidate=_candidate_response(candidate),
        newly_live_identifiers=list(newly_live_identifiers(candidate)),
        default_model_collision_warning=warning,
    )


# =============================================================================
# Probe a promotion candidate
# =============================================================================
class ProbeRequest(BaseModel):
    # Optional at the wire, required by the domain -- the same reason the three
    # human decisions above are Optional. Measured against a running gateway:
    # omitting this field returned the framework's own error list, so a caller
    # got `[{"type": "missing", "loc": [...]}]` where every other refusal on
    # this surface returns `{"type", "field", "message"}`. The closed-set check
    # below already refuses `None`, and it names the field.
    invocation: Optional[str] = None


class ProbeResponseBody(BaseModel):
    passed: bool
    invocation: str
    charged_microusd: Optional[int] = None
    verdict: Optional[VerdictView] = None
    blocker: Optional[QueueBlocker] = None


@router.post("/candidates/{profile_id}/probe", response_model=ProbeResponseBody)
def probe_promotion_candidate(
    profile_id: str, body: ProbeRequest,
    actor: AuthenticatedUser = Depends(require_permission("models:promote")),
) -> ProbeResponseBody:
    """Probe the CANDIDATE, never the raw record: this reads the candidate's
    own pricing key and declared wire protocol and probes with those, so the
    verdict is a claim about the exact thing that would be activated, not two
    independent human-typed prices to compare later.

    The admission rule reads the CURRENT discovered record, not any snapshot
    -- a `PromotionCandidate` carries no cached blocker list of its own, so
    there is no stale view to consult even in principle. This is what makes
    both wrong outcomes unreachable: a record that has become blocked since
    the candidate was made is caught (the current record decides, and it now
    says blocked), and a record that has since been CORRECTED is not wrongly
    refused for a blocker the candidate's era no longer reflects (the current
    record decides, and it now says clear). Refuses, before spending
    anything, only when the current record carries a permanent blocker --
    money must not be spent verifying a binding that cannot be promoted.

    A completed probe -- passed or not -- answers 200; a failed assertion is
    a result, not an error. The one exception is an INDETERMINATE outcome (a
    provider timeout after bytes were already sent, so whether the call ran
    is genuinely unknown): that answers 409, because reporting it as a
    definite pass or fail would claim more than anyone watching from here
    can support.
    """
    try:
        candidate = get_promotion_candidate(profile_id)
    except PromotionStoreUnavailable:
        raise _err_promotion_store_unavailable()
    if candidate is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "candidate_not_found", "message": f"no promotion candidate for profile_id={profile_id!r}"},
        )

    try:
        record = get_discovered_record(profile_id)
    except DiscoveredRecordStoreUnavailable:
        raise _err_503_store_unavailable()
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={
                "type": "record_not_found",
                "message": f"no discovered record for profile_id={profile_id!r}; cannot probe without it",
            },
        )

    permanent = [b for b in record.blockers if not is_actionable_blocker(b)]
    if permanent:
        raise HTTPException(
            status_code=409,
            detail={
                "type": "permanent_blocker",
                "blocker": _blocker_model(permanent[0]).model_dump(),
                "message": (
                    f"profile_id={profile_id!r} carries a permanent blocker; probing "
                    f"would spend real money verifying a binding that is not promotable"
                ),
            },
        )

    if body.invocation not in INVOCATION_VALUES:
        raise HTTPException(
            status_code=422,
            detail={
                "type": "invalid_invocation", "field": "invocation",
                "message": f"invocation must be one of {sorted(INVOCATION_VALUES)}",
            },
        )

    try:
        result = probe(
            record, invocation=body.invocation, pricing_key=candidate.pricing_key,
            wire_protocol=candidate.wire_protocol,
        )
    except ProbeAttemptRefused as exc:
        raise HTTPException(status_code=409, detail={"type": exc.reason, "message": exc.detail})

    if not result.passed and result.blocker is not None and result.blocker.subtype == INDETERMINATE_SUBTYPE:
        raise HTTPException(
            status_code=409,
            detail={
                "type": "probe_indeterminate",
                "blocker": _blocker_model(result.blocker).model_dump(),
                "message": (
                    "the provider call timed out after the request was already sent; "
                    "whether it completed is unknown, so this did not run to a definite "
                    "result -- read the verdict rather than retrying blindly"
                ),
            },
        )

    return ProbeResponseBody(
        passed=result.passed,
        invocation=result.invocation,
        charged_microusd=result.charged_microusd,
        verdict=(_verdict_view(result.invocation, result.verdict) if result.verdict is not None else None),
        blocker=(_blocker_model(result.blocker) if result.blocker is not None else None),
    )


# =============================================================================
# Activate a promotion candidate
# =============================================================================
class ActivateRequest(BaseModel):
    # Optional at the wire, required by the domain -- the same reason the three
    # human decisions above are Optional. Measured against a running gateway:
    # omitting this field returned the framework's own error list, so a caller
    # got `[{"type": "missing", "loc": [...]}]` where every other refusal on
    # this surface returns `{"type", "field", "message"}`. The closed-set check
    # below already refuses `None`, and it names the field.
    invocation: Optional[str] = None
    # The verdict identity the operator saw (`VerdictView.verified_at` from
    # `GET /candidates/{profile_id}`) -- the compare-and-set. Required, no
    # default: activation binds to the SPECIFIC verified moment the operator
    # reviewed, not to "whatever is currently verified", so an activation
    # racing an invalidation or a fresher re-probe refuses rather than
    # silently activating something never verified in the form the operator
    # saw.
    # Optional at the wire, required by the domain -- the same reason the three
    # human decisions above are Optional. Measured against a running gateway:
    # omitting this field returned the framework's own error list, so a caller
    # got `[{"type": "missing", "loc": [...]}]` where every other refusal on
    # this surface returns `{"type", "field", "message"}`. The closed-set check
    # below already refuses `None`, and it names the field.
    verified_at: Optional[str] = None


class ActivateResponse(BaseModel):
    profile_id: str
    invocation: str
    verified_at: str
    provider: str
    bedrock_model_id: str
    bedrock_region: str
    aliases: list[str]
    wire_protocol: str
    pricing_key: str
    profile_scope: str
    model_family: str
    access: str
    jurisdiction_bounded: bool
    jurisdiction: Optional[str] = None
    identifiers: list[str]


#: `ActivationRefused.reason` -> HTTP status. `NOT_PERMITTED` is 403 even
#: though the router's own `require_permission` dependency already gates
#: `models:promote` before this handler runs -- `activate_candidate` re-checks
#: the SAME permission itself (its own docstring: any future caller must go
#: through it rather than re-implementing the gate), so this mapping stays
#: correct even if a future caller reaches the domain function some other
#: way. Every reason not listed (there is none, today) would fall back to 409
#: -- a conflict is the safer default for a reason this mapping does not yet
#: know, since nothing here should ever answer a bare, unmapped 500.
_ACTIVATION_REFUSED_STATUS: dict[str, int] = {
    ActivationRefused.NOT_PERMITTED: 403,
    ActivationRefused.CANDIDATE_NOT_FOUND: 404,
    ActivationRefused.RECORD_NOT_FOUND: 404,
    ActivationRefused.VERDICT_NOT_FOUND: 409,
    ActivationRefused.VERDICT_NOT_VERIFIED: 409,
    ActivationRefused.VERDICT_IDENTITY_MISMATCH: 409,
    ActivationRefused.PRICING_KEY_MISMATCH: 409,
    ActivationRefused.WIRE_PROTOCOL_MISMATCH: 409,
    ActivationRefused.IDENTIFIER_TAKEN: 409,
}


def _activation_refused_to_http(exc: ActivationRefused) -> HTTPException:
    status_code = _ACTIVATION_REFUSED_STATUS.get(exc.reason, 409)
    return HTTPException(status_code=status_code, detail={"type": exc.reason, "message": str(exc)})


@router.post("/candidates/{profile_id}/activate", response_model=ActivateResponse)
def activate_promotion_candidate(
    profile_id: str, body: ActivateRequest,
    actor: AuthenticatedUser = Depends(require_permission("models:promote")),
) -> ActivateResponse:
    """Activate, taking the verdict identity the operator saw
    (`body.verified_at`) as a compare-and-set: `activate_candidate` refuses
    when the CURRENT verdict for `(profile_id, body.invocation)` is not
    verified as exactly that identity, checked again, atomically, inside the
    transaction that commits the activation -- see `mvp.discovery.activation
    ._commit_activation`. Idempotent: re-activating an already-active
    candidate against the SAME verdict identity succeeds again rather than
    refusing on "already active", since nothing here tracks that as a
    distinct state (see that module's own docstring)."""
    if body.invocation not in INVOCATION_VALUES:
        raise HTTPException(
            status_code=422,
            detail={
                "type": "invalid_invocation", "field": "invocation",
                "message": f"invocation must be one of {sorted(INVOCATION_VALUES)}",
            },
        )
    if not body.verified_at:
        # The identity is what makes this a compare-and-set rather than "activate
        # whatever is currently verified", so an absent one is refused here in
        # this surface's own vocabulary rather than passed down as an empty
        # string that would refuse further in with a less specific reason.
        raise HTTPException(
            status_code=422,
            detail={
                "type": "verdict_identity_required", "field": "verified_at",
                "message": (
                    "the verdict identity the operator saw is required; read "
                    "VerdictView.verified_at from GET /candidates/{profile_id}"
                ),
            },
        )
    try:
        entry = activate_candidate(
            profile_id, body.invocation, actor=actor, expected_verified_at=body.verified_at,
        )
    except ActivationRefused as exc:
        raise _activation_refused_to_http(exc)
    except ActivationStoreUnavailable:
        raise _err_503(
            "activated_entry_store_unavailable",
            "The activated-entry store is temporarily unavailable. Retry shortly.",
        )

    identifiers: list[str] = []
    for identifier in (*entry.aliases, entry.bedrock_model_id):
        if identifier not in identifiers:
            identifiers.append(identifier)

    return ActivateResponse(
        profile_id=profile_id, invocation=body.invocation, verified_at=body.verified_at,
        provider=entry.provider, bedrock_model_id=entry.bedrock_model_id,
        bedrock_region=entry.bedrock_region, aliases=list(entry.aliases),
        wire_protocol=entry.wire_protocol, pricing_key=entry.pricing_key,
        profile_scope=entry.profile_scope, model_family=entry.model_family,
        access=entry.access, jurisdiction_bounded=entry.jurisdiction_bounded,
        jurisdiction=entry.jurisdiction, identifiers=identifiers,
    )
