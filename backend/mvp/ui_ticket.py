"""UI handoff ticket endpoints.

Two routes:

* ``POST /api/mvp/auth/ui-ticket``
      Authenticated. The caller's current bearer / refresh tokens are
      persisted against a fresh opaque nonce, and the nonce is returned
      to the CLI. The CLI opens the browser with
      ``https://<host>/?ui_ticket=<nonce>``.

* ``POST /api/mvp/auth/ui-ticket/consume``
      Unauthenticated. The SPA submits the nonce it pulled out of the
      URL. The nonce is atomically deleted (single-use) and the tokens
      are returned so the SPA can put them in sessionStorage.

Why an opaque nonce instead of just letting the SPA accept
``?token=<access_token>``? See P0-8 in the 2026-04 review: a URL-borne
token is a session-fixation primitive. A nonce is:

* transport-only (no API authority of its own),
* single-use (delete-and-return semantics),
* short-lived (30 s TTL in DynamoDB), and
* bound to the minting user at mint time (so the consumer cannot
  escalate to a different account).

The nonce itself is still sensitive — anyone who sees the plaintext
within the 30 s window and wins the race gets the tokens. The SPA
therefore strips ``?ui_ticket=`` from ``window.location`` before any
third-party script has a chance to observe it.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from core.rate_limit import SSO_EXCHANGE_RATE_LIMIT, limiter

from dynamo import TicketNotFoundError, UiTicketsRepository

from .authz import log_audit_event
from .deps import AuthenticatedUser, _decode_cognito_access_token, get_current_user


router = APIRouter(prefix="/api/mvp/auth", tags=["mvp-ui-ticket"])
_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Mint
# ---------------------------------------------------------------------
class MintUiTicketRequest(BaseModel):
    """CLI → backend. The CLI must forward its *own* bearer so the
    backend can bind the ticket to the caller; it may optionally
    surface the full token bundle (access / id / refresh / expires_in)
    if the CLI already holds those — the SPA needs them together to
    drive refresh correctly on its side.
    """

    model_config = ConfigDict(extra="forbid")

    access_token: str = Field(min_length=8, max_length=8192)
    id_token: Optional[str] = Field(default=None, max_length=8192)
    refresh_token: Optional[str] = Field(default=None, max_length=8192)
    expires_in: Optional[int] = Field(default=None, ge=0, le=86400 * 30)
    token_type: Optional[str] = Field(default="Bearer", max_length=32)


class MintUiTicketResponse(BaseModel):
    ticket: str
    expires_at: int
    expires_in: int


@router.post("/ui-ticket", response_model=MintUiTicketResponse)
def mint_ui_ticket(
    body: MintUiTicketRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> MintUiTicketResponse:
    """Mint a single-use UI handoff ticket for the authenticated user.

    The caller must present a valid bearer via the normal
    `get_current_user` dependency. We then bind the supplied token
    bundle to a fresh nonce and hand the nonce back.

    P0-8 / sweep-4 C-Critical-B1: the body's access_token is a
    full-authority credential that the SPA will sessionStorage-adopt
    on consume. Trusting the caller to put THEIR OWN token here is
    not enough — a malicious CLI could mint a ticket carrying Bob's
    access_token while authenticated as Alice, giving Alice a
    session-fixation primitive for Bob's account. We therefore
    JWKS-verify the body.access_token and require sub(body) ==
    caller.user_id before binding it to the nonce.
    """
    # Re-verify body.access_token against Cognito JWKS and bind it to
    # the caller. `_decode_cognito_access_token` raises HTTPException
    # 401 on any verification failure (signature, issuer, audience,
    # expiry, or non-access token_use), so we inherit the full set of
    # JWT-level defences already exercised by test_jwt_verify.py.
    #
    # Sweep-4 round-5 hardening: the JWKS fetch layer can raise non-
    # PyJWTError exceptions (URLError / JSONDecodeError / SSL cert
    # errors) on transient infrastructure failures. We must NOT echo
    # those exception strings back to the caller — they leak internal
    # OIDC issuer URLs, proxy error hints, and DNS topology.
    # Instead we log the detail server-side and return a generic 401.
    try:
        body_claims = _decode_cognito_access_token(body.access_token)
    except HTTPException:
        raise
    except Exception as e:  # pragma: no cover — defensive
        _log.error(
            "ui_ticket_body_verify_unexpected_error",
            extra={"error": str(e), "error_type": type(e).__name__},
        )
        raise HTTPException(
            status_code=401,
            detail="UI ticket body access_token could not be verified.",
        )
    body_sub = str(body_claims.get("sub") or "")
    if not body_sub or body_sub != user.user_id:
        # Do not leak either sub back to the caller. Audit log below is
        # intentionally terse.
        log_audit_event(
            event="ui_ticket_mint_subject_mismatch",
            actor_id=user.user_id,
            actor_email=user.email,
            target_id="(ui-handoff)",
            target_type="ui_ticket",
            details={"reason": "body_sub_does_not_match_caller"},
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "UI ticket body subject mismatch: "
                "the access_token must belong to the authenticated caller."
            ),
        )

    repo = UiTicketsRepository()
    plaintext, expires_at = repo.mint(
        user_id=user.user_id,
        access_token=body.access_token,
        id_token=body.id_token,
        refresh_token=body.refresh_token,
        expires_in=body.expires_in,
        token_type=body.token_type or "Bearer",
    )

    # Audit: we log that a ticket was minted, but never the plaintext —
    # that is sensitive on the same order as the access token itself.
    log_audit_event(
        event="ui_ticket_minted",
        actor_id=user.user_id,
        actor_email=user.email,
        target_id="(ui-handoff)",
        target_type="ui_ticket",
        details={"expires_at": expires_at},
    )

    now_to_exp = max(0, int(expires_at) - int(body.expires_in or 0))
    _ = now_to_exp  # silence unused if future trimming needs it
    return MintUiTicketResponse(
        ticket=plaintext,
        expires_at=expires_at,
        # expires_in is relative to "now" so the SPA can wait-and-retry
        # gracefully; callers have no reason to trust our clock.
        expires_in=max(0, expires_at - int(_epoch_now())),
    )


# ---------------------------------------------------------------------
# Consume
# ---------------------------------------------------------------------
class ConsumeUiTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Plaintext tickets are CSPRNG-base64url (~43 chars) + "stt_"
    # prefix. We keep the accepted length generous to be future-proof
    # but refuse anything that obviously cannot be one.
    ticket: str = Field(min_length=16, max_length=256, pattern=r"^stt_[A-Za-z0-9_\-]+$")


class ConsumeUiTicketResponse(BaseModel):
    access_token: str
    id_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expires_in: Optional[int] = None
    token_type: Optional[str] = None


@router.post("/ui-ticket/consume", response_model=ConsumeUiTicketResponse)
@limiter.limit(SSO_EXCHANGE_RATE_LIMIT)
def consume_ui_ticket(
    request: Request, body: ConsumeUiTicketRequest
) -> ConsumeUiTicketResponse:
    """Redeem a UI handoff ticket. Unauthenticated by design — the
    nonce IS the credential for this exchange — but rate-limited per
    IP to slow down blind guessing. 256-bit ticket entropy already
    makes guessing uneconomic; the limiter is belt-and-braces.
    """
    _ = request  # slowapi reads it from the signature
    try:
        attrs = UiTicketsRepository().consume(body.ticket)
    except TicketNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="UI ticket not found, already consumed, or expired",
        )

    return ConsumeUiTicketResponse(
        access_token=str(attrs.get("access_token") or ""),
        id_token=str(attrs.get("id_token")) if attrs.get("id_token") else None,
        refresh_token=str(attrs.get("refresh_token"))
        if attrs.get("refresh_token")
        else None,
        expires_in=int(attrs["expires_in"]) if "expires_in" in attrs else None,
        token_type=str(attrs.get("token_type")) if attrs.get("token_type") else None,
    )


def _epoch_now() -> int:
    import time

    return int(time.time())
