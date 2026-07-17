"""External authorize / capture / void API (P0 — Layer 5 external billing).

Lets a tenant reserve ("authorize") a dollar amount from its pool for a
non-LLM action, then later "capture" (settle) or "void" (release) it from a
SEPARATE HTTP call. The design principle (Fable authcap) is that the MONEY
LOGIC IS NOT FORKED: capture reuses the exact `_settle_pool_side` and void the
exact `release_pool` the inline request path uses; only the ReservationContext's
CONSTRUCTION differs (rehydrated from the ledger rather than held in memory).
So Phase-2's terminal mutual-exclusion and Layer-5's frozen rating carry over
unchanged, and the only new money-adjacent code is the reserve's IDEMP row
(idempotent authorize) and the rehydrate.

Endpoints (all under /api/mvp/billing):
  POST /authorize                         (billing:write, Idempotency-Key header)
  POST /authorizations/{id}/capture       (billing:write)
  POST /authorizations/{id}/void          (billing:write)
  GET  /authorizations/{id}               (billing:read)

authorization_id is an OPAQUE token `auth_<base64url(hold_id|period|hold_sk)>`
(no GSI, no reverse index). It is addressing, not authorization: the ledger PK
is ALWAYS built from the authenticated tenant_id, so a token from another tenant
can only ever address the caller's OWN partition, where the hold does not exist
→ 404. Tampering with a token yields a non-existent hold → the conditional txn
simply fails; no money invariant can break.

Idempotency / double-capture (Fable authcap C): authorize dedupes on the
required `Idempotency-Key` header via an IDEMP ledger row in the reserve txn.
Capture/void need NO new idempotency mechanism — they ride Phase-2's single
TERMINAL sk with `attribute_not_exists`, so at most one of settle/release/reclaim
lands per hold. A loser reads the terminal and maps it to a deterministic
response (see `_terminal_response`).

Expiry (Fable authcap D): a RECLAIM'd (reaper-expired) external hold cannot be
captured — it returns 410, and is deliberately NOT late-settled (the external
capture window is tenant-controlled and unbounded, so late-billing could break
the budget invariant). `_settle_pool_side` raises `ExternalHoldReclaimed` for
that case; this module maps it to 410.
"""
from __future__ import annotations

import base64
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from dynamo import CreditLedgerRepository
from dynamo.tenant_budgets import TenantBudgetsRepository

from . import _pipeline
from .authz import require_permission
from .deps import AuthenticatedUser, get_current_user


router = APIRouter(prefix="/api/mvp/billing", tags=["mvp-billing-authorize"])

# ttl clamp: an authorize must not sit open forever (unbounded reserved leak) nor
# expire before a normal action completes.
_TTL_MIN_SECONDS = 30
_TTL_MAX_SECONDS = 24 * 60 * 60  # 24h
_TTL_DEFAULT_SECONDS = 300

_TOKEN_PREFIX = "auth_"
_TOKEN_SEP = "|"


# ---------------------------------------------------------------------------
# opaque authorization token codec (hold_id | period | hold_sk)
# ---------------------------------------------------------------------------


def encode_authorization_id(*, hold_id: str, period: str, hold_sk: str) -> str:
    """`auth_` + urlsafe-base64(hold_id|period|hold_sk), padding STRIPPED.
    Self-contained addressing so no GSI / reverse index is needed. Not a secret
    — the tenant is always taken from the auth context, so the token only
    addresses the caller's own partition. Padding is stripped so the whole token
    is in the URL-path-safe alphabet [A-Za-z0-9_-] (no `=` to percent-encode).

    Precondition (Fable authcap review-1 codec / review-3 H): the three fields
    must not contain the `|` separator, or decode would mis-split. hold_id is a
    uuid4 and period is YYYY-MM (neither can contain `|`), and hold_sk is
    HOLD#<period>#<expiry>#<hold_id> — none contain `|`. We assert it anyway so a
    future caller with a laxer id cannot silently corrupt addressing."""
    for field in (hold_id, period, hold_sk):
        if _TOKEN_SEP in field:
            raise ValueError(f"authorization token field must not contain {_TOKEN_SEP!r}")
    raw = _TOKEN_SEP.join((hold_id, period, hold_sk)).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return _TOKEN_PREFIX + b64


def decode_authorization_id(token: str) -> tuple[str, str, str]:
    """Decode → (hold_id, period, hold_sk). Raises 404 on any malformed token
    (never 400 — a bad token is indistinguishable from a non-existent
    authorization, and we do not want a decoding oracle).

    SECURITY — cross-field binding (Fable authcap review-3 High + codec gap #1):
    the three fields are decoded from ONE token but are used against DIFFERENT
    rows downstream — the C-1 gate reads the RESERVE event by hold_id, while
    rehydrate reads the HOLD row by hold_sk. If they were not bound, a MIXED
    token (hold_id of my legit external hold + hold_sk of a *different* hold)
    would build a context from mismatched rows and drift pool_reserved. We bind
    them here at the single entry point: hold_sk MUST be exactly
    `HOLD#<period>#<something>#<hold_id>`, so all three fields describe the same
    hold. A mismatch is a 404 (a forged/mixed token is not a real authorization).
    """
    if not token or not token.startswith(_TOKEN_PREFIX):
        raise HTTPException(status_code=404, detail="authorization not found")
    try:
        body = token[len(_TOKEN_PREFIX):]
        # Re-add the stripped base64 padding (len must be a multiple of 4).
        pad = (-len(body)) % 4
        raw = base64.urlsafe_b64decode((body + "=" * pad).encode("ascii"))
        hold_id, period, hold_sk = raw.decode("utf-8").split(_TOKEN_SEP, 2)
    except Exception:  # noqa: BLE001 — any decode failure is a 404, not a 400.
        raise HTTPException(status_code=404, detail="authorization not found")
    if not hold_id or not period or not hold_sk:
        raise HTTPException(status_code=404, detail="authorization not found")
    # Cross-field binding: hold_sk must be the sk for THIS (period, hold_id).
    if hold_sk != _expected_hold_sk_shape(period, hold_id, hold_sk):
        raise HTTPException(status_code=404, detail="authorization not found")
    return hold_id, period, hold_sk


def _expected_hold_sk_shape(period: str, hold_id: str, hold_sk: str) -> str:
    """Return `hold_sk` iff it is a well-formed sk binding (period, hold_id):
    `HOLD#<period>#<expiry>#<hold_id>` with a numeric expiry. Otherwise return a
    sentinel that cannot equal hold_sk, so the caller's equality check fails
    (→404). Binding both the prefix (period) and the suffix (hold_id) is what
    defeats the mixed-token attack; the middle segment is the reaper's expiry."""
    prefix = f"HOLD#{period}#"
    suffix = f"#{hold_id}"
    if hold_sk.startswith(prefix) and hold_sk.endswith(suffix):
        middle = hold_sk[len(prefix):len(hold_sk) - len(suffix)]
        if middle.isdigit():
            return hold_sk
    return "\x00invalid-hold-sk-binding"


# ---------------------------------------------------------------------------
# request / response models
# ---------------------------------------------------------------------------


class AuthorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_microusd: int = Field(..., gt=0, description="dollar amount to hold, micro-USD")
    ttl_seconds: Optional[int] = Field(default=None, ge=1)
    description: Optional[str] = Field(default=None, max_length=500)
    workflow_run_id: Optional[str] = Field(default=None, max_length=200)


class AuthorizeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorization_id: str
    amount_microusd: int
    expires_at_epoch: int
    status: str  # "authorized"
    # True when a duplicate Idempotency-Key replayed the ORIGINAL authorization
    # (no new hold created). Surfaced in the body rather than via a 201/200 split
    # so a single-status endpoint stays simple for the CLI/clients.
    replayed: bool = False


class CaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actual_amount_microusd: int = Field(..., ge=0, description="amount to capture, micro-USD")


class CaptureResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorization_id: str
    captured_microusd: int
    terminal: str  # "SETTLE"


class VoidResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorization_id: str
    terminal: str  # "RELEASE"


class AuthorizationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorization_id: str
    tenant_id: str
    amount_microusd: int
    status: str  # authorized | captured | voided | expired
    terminal: Optional[str] = None
    captured_microusd: Optional[int] = None


def _clamp_ttl(ttl_seconds: Optional[int]) -> int:
    if ttl_seconds is None:
        return _TTL_DEFAULT_SECONDS
    return max(_TTL_MIN_SECONDS, min(int(ttl_seconds), _TTL_MAX_SECONDS))


def _request_fingerprint(body: "AuthorizeRequest") -> str:
    """A stable hash of the money-bearing request fields, stored on the IDEMP row.
    On a duplicate Idempotency-Key the reserve compares the incoming fingerprint
    to the stored one and 422s a mismatch (Fable authcap review-1 H-1) — so a key
    reused for a DIFFERENT request never silently replays the wrong hold. ttl is
    EXCLUDED (it does not change what is charged, only the expiry, and clients may
    legitimately retry with a fresher ttl); amount / description / run_id are the
    identity of the authorization."""
    import hashlib
    import json as _json

    payload = _json.dumps(
        {
            # capture_mode is pinned so a future units-mode request cannot replay
            # onto an amount-mode authorization that happens to share amount/desc
            # (Fable authcap review-4 L-A). Today it is always "amount".
            "capture_mode": "amount",
            "amount_microusd": int(body.amount_microusd),
            "description": body.description or "",
            "workflow_run_id": body.workflow_run_id or "",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# POST /authorize
# ---------------------------------------------------------------------------


@router.post("/authorize", response_model=AuthorizeResponse)
def authorize(
    body: AuthorizeRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=200),
    user: AuthenticatedUser = Depends(get_current_user),
    _perm: AuthenticatedUser = Depends(require_permission("billing:write")),
) -> AuthorizeResponse:
    """Reserve `amount_microusd` from the caller's tenant pool. `Idempotency-Key`
    is REQUIRED — a retry with the same key REPLAYS the original authorization
    (no second hold; `replayed=true` in the body), while the same key with a
    DIFFERENT body is 422 `idempotency_key_reuse`. Always 200 (the replay flag is
    in the body, not a 201/200 split). amount-mode only for now (a pricing_key/
    units mode is a documented future extension).

    The authorization_id is a pure function of the hold identity the reserve
    mints, so it is stored in the IDEMP row at write time and a duplicate-key
    replay reconstructs the SAME token deterministically."""
    ttl = _clamp_ttl(body.ttl_seconds)
    fingerprint = _request_fingerprint(body)
    try:
        result = _pipeline.reserve_external_authorization(
            tenant_id=user.org_id,
            amount_microusd=body.amount_microusd,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            # The id is a pure function of the hold identity (all minted inside
            # the reserve), so it is stored in the IDEMP row at write time and a
            # duplicate-key replay recomputes the SAME id — no placeholder rewrite.
            authorization_id_factory=lambda hold_id, period, hold_sk: (
                encode_authorization_id(hold_id=hold_id, period=period, hold_sk=hold_sk)
            ),
            ttl_seconds=ttl,
            description=body.description,
            workflow_run_id=body.workflow_run_id,
        )
    except _pipeline.ExternalAuthorizeNoPool:
        # A tenant with no pool cannot authorize; 404 so pool existence is not an
        # oracle to an external caller (matches the run-billing read surface).
        raise HTTPException(status_code=404, detail="no pool budget for tenant/period")
    except _pipeline.IdempotencyKeyReuse:
        # Same key, DIFFERENT request body (or a key collision) — never a silent
        # wrong-authorization replay (Fable authcap review-1 H-1).
        raise HTTPException(
            status_code=422,
            detail={"type": "idempotency_key_reuse",
                    "message": "Idempotency-Key already used for a different request"},
        )
    return AuthorizeResponse(
        authorization_id=result.authorization_id,
        amount_microusd=result.amount_microusd,
        expires_at_epoch=result.expires_at_epoch,
        status="authorized",
        replayed=result.replayed,
    )


# ---------------------------------------------------------------------------
# POST /authorizations/{id}/capture
# ---------------------------------------------------------------------------


@router.post("/authorizations/{authorization_id}/capture", response_model=CaptureResponse)
def capture(
    authorization_id: str,
    body: CaptureRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    _perm: AuthenticatedUser = Depends(require_permission("billing:write")),
) -> CaptureResponse:
    """Settle an open authorization for `actual_amount_microusd` (≤ authorized).

    Rehydrates the ReservationContext from the ledger and calls the UNMODIFIED
    `_settle_pool_side` — so this is byte-identically the inline settle. Idempotency
    and all races are Phase-2's terminal mutual-exclusion: a second capture, a
    capture-vs-void, or a capture-vs-reaper all resolve by reading the terminal."""
    hold_id, period, hold_sk = decode_authorization_id(authorization_id)
    tenant_id = user.org_id
    # SECURITY (C-1): confirm this token names an EXTERNAL authorization before
    # ANY terminal read or state change — an inline LLM hold's (forgeable) token
    # must 404 on every path, never be captured or have its state mapped.
    _require_external(tenant_id, period, hold_id)
    try:
        ctx = _pipeline.rehydrate_reservation_context(
            tenant_id=tenant_id, period=period, hold_id=hold_id, hold_sk=hold_sk
        )
    except _pipeline.ExternalHoldInconsistent:
        # H-A: the hold's two durable amounts disagree — refuse to settle an
        # inconsistent hold (an alarm is logged in the pipeline). 409, not 500,
        # so a client sees a definitive "this authorization is unsettleable".
        raise HTTPException(
            status_code=409,
            detail={"type": "authorization_inconsistent",
                    "message": "Authorization amounts are inconsistent; contact support."},
        )
    if ctx is None:
        # Hold gone → already captured/voided/reclaimed. Read the terminal to map
        # a deterministic response (200 replay / 409 / 410).
        return _capture_terminal_response(
            tenant_id, period, hold_id, hold_sk, authorization_id,
            body.actual_amount_microusd,
        )

    actual = int(body.actual_amount_microusd)
    if actual > ctx.pool_reserved_microusd:
        # captured ≤ authorized (Fable authcap E). Over-capture would spend beyond
        # the hold and break pool accounting → 422.
        raise HTTPException(
            status_code=422,
            detail={
                "type": "capture_exceeds_authorization",
                "authorized_microusd": ctx.pool_reserved_microusd,
                "requested_microusd": actual,
            },
        )

    try:
        _settle_external(ctx, actual)
    except _pipeline.ExternalHoldReclaimed:
        # The reaper reclaimed this hold before we captured (D-2): 410, NOT
        # late-settled. The counters are untouched.
        raise HTTPException(status_code=410, detail="authorization expired")
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        # H-B (Fable authcap review-4): ANY settle failure — a ClientError from a
        # terminal clash (concurrent void), OR a non-ClientError like a
        # ReadTimeout on a settle that may ALREADY have committed — falls back to
        # reading the terminal. If the settle actually landed, the terminal is a
        # SETTLE and the client gets a truthful 200 replay / 409; if it truly
        # failed and the hold is still live, _no_terminal_error returns 503
        # (retryable). This closes the "bare 500 after a committed settle" hole:
        # the client never sees an opaque 500 that hides a real charge.
        return _capture_terminal_response(
            tenant_id, period, hold_id, hold_sk, authorization_id, actual
        )
    return CaptureResponse(
        authorization_id=authorization_id,
        captured_microusd=actual,
        terminal="SETTLE",
    )


def _settle_external(ctx, actual_microusd: int) -> None:
    """Call the UNMODIFIED `_settle_pool_side` for an external capture.

    Builds the same `ledger_facts` the inline settle builds. amount-mode is the
    only mode today: the captured figure is a client-declared fixed amount, NOT
    derived from any rate — so we stamp the DISTINCT `EXTERNAL_AMOUNT_SENTINEL`
    (never a real version) and pass `rating=None`, exactly the honesty rule the
    inline path follows for a snapshot-less charge (Fable authcap review-1 M-4).
    A future units-mode would freeze a rating and pass its real version through
    this same facts dict; there is deliberately NO version-stamping branch here
    until that rating exists (so we cannot label an amount with a version it was
    not derived from)."""
    from .pricing import EXTERNAL_AMOUNT_SENTINEL

    # Defense-in-depth for captured ≤ authorized (Fable authcap review-4 money
    # Gap 2): the endpoint already 422s an over-capture, but that guard is a
    # single Python `if` at one call site. Re-assert the bound HERE, at the money
    # entry point every external capture funnels through, so a future second
    # caller or a refactor that bypasses the endpoint can never push pool_settled
    # past what was authorized. This is a hard invariant, not a user error, so it
    # raises (mapped to 409 by the endpoint) rather than silently clamping.
    actual = int(actual_microusd)
    if actual > int(ctx.pool_reserved_microusd):
        raise _pipeline.ExternalHoldInconsistent(ctx.hold_id or "")

    facts = {
        "model_id": None,
        "pricing_version": EXTERNAL_AMOUNT_SENTINEL,
        "pricing_key": ctx.pricing_key,
        "rating": None,
        "settle_reason": "external_capture",
        "run_id": ctx.workflow_run_id,
        "source": "external",
    }
    _pipeline._settle_pool_side(
        _ExternalUser(ctx.tenant_id), ctx, actual, ledger_facts=facts
    )


class _ExternalUser:
    """Minimal shim for `_settle_pool_side`, which reads ONLY `user.org_id`
    (verified: it never touches user_id/email — UsageLogs is written by the inline
    `settle_reservation_and_log`, which the external capture deliberately does NOT
    call). An external capture is a tenant-level action with no acting end-user,
    so org_id is the tenant. user_id/email are placeholders that never reach a
    money write or a user-keyed side effect (Fable review-1 Low)."""

    def __init__(self, tenant_id: str):
        self.org_id = tenant_id
        self.user_id = ""
        self.email = ""


# ---------------------------------------------------------------------------
# POST /authorizations/{id}/void
# ---------------------------------------------------------------------------


@router.post("/authorizations/{authorization_id}/void", response_model=VoidResponse)
def void(
    authorization_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    _perm: AuthenticatedUser = Depends(require_permission("billing:write")),
) -> VoidResponse:
    """Release an open authorization without charge (RELEASE terminal). Reuses the
    UNMODIFIED `ReservationContext.release_pool`."""
    hold_id, period, hold_sk = decode_authorization_id(authorization_id)
    tenant_id = user.org_id
    _require_external(tenant_id, period, hold_id)  # C-1: external-only
    try:
        ctx = _pipeline.rehydrate_reservation_context(
            tenant_id=tenant_id, period=period, hold_id=hold_id, hold_sk=hold_sk
        )
    except _pipeline.ExternalHoldInconsistent:
        # H-A: releasing an inconsistent hold by HOLD.amount would drift the
        # ledger's +reserved just as a capture would — refuse (409), don't move it.
        raise HTTPException(
            status_code=409,
            detail={"type": "authorization_inconsistent",
                    "message": "Authorization amounts are inconsistent; contact support."},
        )
    if ctx is None:
        return _void_terminal_response(tenant_id, period, hold_id, hold_sk, authorization_id)
    # release_pool is idempotent + best-effort; it flips the terminal to RELEASE
    # (or no-ops if the reaper reclaimed). Then read the terminal for the honest
    # response — a reclaimed void is 410 (already expired, nothing to release).
    ctx.release_pool()
    return _void_terminal_response(tenant_id, period, hold_id, hold_sk, authorization_id)


# ---------------------------------------------------------------------------
# GET /authorizations/{id}
# ---------------------------------------------------------------------------


@router.get("/authorizations/{authorization_id}", response_model=AuthorizationStatus)
def get_authorization(
    authorization_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    _perm: AuthenticatedUser = Depends(require_permission("billing:read")),
) -> AuthorizationStatus:
    """Status of one authorization: authorized (hold open) / captured / voided /
    expired. tenant_id is pinned from the auth context."""
    hold_id, period, hold_sk = decode_authorization_id(authorization_id)
    tenant_id = user.org_id
    budgets = TenantBudgetsRepository()
    ledger = CreditLedgerRepository()

    reserve_evt = ledger.get_reserve(tenant_id=tenant_id, period=period, hold_id=hold_id)
    # SECURITY (C-1): a token is only an external authorization if its RESERVE
    # event was minted by this API (source=external). An inline LLM hold's token
    # (forgeable, same sk shape) must 404 here too, never expose its state.
    if reserve_evt is None or reserve_evt.get("source") != "external":
        raise HTTPException(status_code=404, detail="authorization not found")
    amount = int(reserve_evt.get("reserved_delta_microusd", 0))

    hold = budgets.get_hold(tenant_id=tenant_id, sk=hold_sk)
    if hold is not None:
        return AuthorizationStatus(
            authorization_id=authorization_id, tenant_id=tenant_id,
            amount_microusd=amount, status="authorized",
        )
    terminal = ledger.get_terminal(tenant_id=tenant_id, period=period, hold_id=hold_id)
    et = (terminal or {}).get("event_type")
    if et == "SETTLE":
        return AuthorizationStatus(
            authorization_id=authorization_id, tenant_id=tenant_id,
            amount_microusd=amount, status="captured", terminal="SETTLE",
            captured_microusd=int(terminal.get("settled_delta_microusd", 0)),
        )
    if et == "RELEASE":
        return AuthorizationStatus(
            authorization_id=authorization_id, tenant_id=tenant_id,
            amount_microusd=amount, status="voided", terminal="RELEASE",
        )
    if et == "RECLAIM":
        return AuthorizationStatus(
            authorization_id=authorization_id, tenant_id=tenant_id,
            amount_microusd=amount, status="expired", terminal="RECLAIM",
        )
    # Hold gone AND no terminal: a legacy/edge state — report expired (the hold no
    # longer holds budget), which is the safe external-facing answer.
    return AuthorizationStatus(
        authorization_id=authorization_id, tenant_id=tenant_id,
        amount_microusd=amount, status="expired",
    )


# ---------------------------------------------------------------------------
# deterministic terminal → response mapping (Fable authcap C table)
# ---------------------------------------------------------------------------


def _require_external(tenant_id: str, period: str, hold_id: str) -> None:
    """Raise 404 unless the hold's RESERVE event was minted by this API
    (source=external). The single C-1 gate for capture/void — it must run BEFORE
    any terminal read so an inline hold's token can neither be acted on nor have
    its state observed. get_reserve is a ConsistentRead so a capture immediately
    after authorize sees its own RESERVE."""
    reserve_evt = CreditLedgerRepository().get_reserve(
        tenant_id=tenant_id, period=period, hold_id=hold_id
    )
    if reserve_evt is None or reserve_evt.get("source") != "external":
        raise HTTPException(status_code=404, detail="authorization not found")


def _no_terminal_error(tenant_id: str, period: str, hold_sk: str) -> HTTPException:
    """Decide the response when a hold has NO terminal (Fable authcap review-1
    M-1/M-2). Two distinct causes:
      * the hold row is STILL PRESENT → a preceding settle/release silently
        failed (throttle) — the money is still frozen, so return 503 (retryable),
        NOT a misleading 404 that would make the client abandon a live hold;
      * the hold row is GONE with no terminal → the reaper writes hold-delete +
        RECLAIM in ONE txn, so this state should be unreachable. Log it as an
        invariant violation and return 404 (nothing left to act on)."""
    hold = TenantBudgetsRepository().get_hold(tenant_id=tenant_id, sk=hold_sk)
    if hold is not None:
        return HTTPException(
            status_code=503,
            detail={"type": "authorization_action_unavailable",
                    "message": "Temporarily unavailable; retry shortly."},
        )
    logging.getLogger(__name__).error(
        "external_hold_gone_no_terminal", extra={"tenant_id": tenant_id, "period": period}
    )
    return HTTPException(status_code=404, detail="authorization not found")


def _capture_terminal_response(
    tenant_id: str, period: str, hold_id: str, hold_sk: str,
    authorization_id: str, actual: int,
) -> CaptureResponse:
    """Map an already-terminal hold to a capture response (Fable authcap C):
      SETTLE, same actual  → 200 replay
      SETTLE, diff actual  → 409 already_captured (first-writer amount wins)
      RELEASE              → 409 already_voided
      RECLAIM              → 410 expired
      none                 → 503 if hold still live, else 404 (M-1/M-2)."""
    ledger = CreditLedgerRepository()
    terminal = ledger.get_terminal(tenant_id=tenant_id, period=period, hold_id=hold_id)
    et = (terminal or {}).get("event_type")
    if et == "SETTLE":
        settled = int(terminal.get("settled_delta_microusd", 0))
        if settled == int(actual):
            return CaptureResponse(
                authorization_id=authorization_id,
                captured_microusd=settled, terminal="SETTLE",
            )
        raise HTTPException(
            status_code=409,
            detail={"type": "already_captured", "captured_microusd": settled},
        )
    if et == "RELEASE":
        raise HTTPException(status_code=409, detail={"type": "already_voided"})
    if et == "RECLAIM":
        raise HTTPException(status_code=410, detail="authorization expired")
    raise _no_terminal_error(tenant_id, period, hold_sk)


def _void_terminal_response(
    tenant_id: str, period: str, hold_id: str, hold_sk: str, authorization_id: str
) -> VoidResponse:
    """Map a terminal hold to a void response:
      RELEASE → 200 replay      SETTLE → 409 already_captured
      RECLAIM → 410 expired     none   → 503 if hold live, else 404 (M-1)."""
    ledger = CreditLedgerRepository()
    terminal = ledger.get_terminal(tenant_id=tenant_id, period=period, hold_id=hold_id)
    et = (terminal or {}).get("event_type")
    if et == "RELEASE":
        return VoidResponse(authorization_id=authorization_id, terminal="RELEASE")
    if et == "SETTLE":
        raise HTTPException(status_code=409, detail={"type": "already_captured"})
    if et == "RECLAIM":
        raise HTTPException(status_code=410, detail="authorization expired")
    raise _no_terminal_error(tenant_id, period, hold_sk)
