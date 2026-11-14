"""CLI Cognito User/Pass login helper.

In the MVP environment the CLI does not have IAM permissions to call Cognito InitiateAuth
directly, so the backend acts as a pass-through proxy and returns the JWT.

Endpoints:
  POST /api/mvp/auth/login
    Input: { "email", "password" }
    Returns: Cognito AuthenticationResult (on success) or ChallengeName (first login)

  POST /api/mvp/auth/respond
    Input: { "email", "new_password", "session" } (response to NEW_PASSWORD_REQUIRED challenge)
    Returns: Cognito AuthenticationResult
"""
from __future__ import annotations

import os
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.rate_limit import (
    LOGIN_RATE_LIMIT,
    RESPOND_RATE_LIMIT,
    check_identity_rate_limit,
    limiter,
    sign_in_address,
)


router = APIRouter(prefix="/api/mvp/auth", tags=["mvp-auth"])


def _cognito_client():
    region = os.getenv("COGNITO_REGION") or os.getenv("AWS_REGION", "us-east-1")
    return boto3.client("cognito-idp", region_name=region)


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise HTTPException(status_code=500, detail=f"{name} is not configured")
    return val


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    status: str  # "authenticated" | "new_password_required"
    session: Optional[str] = None
    access_token: Optional[str] = None
    id_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expires_in: Optional[int] = None
    token_type: Optional[str] = None
    challenge_name: Optional[str] = None


class RespondRequest(BaseModel):
    email: str
    new_password: str
    session: str


@router.post("/login", response_model=LoginResponse)
@limiter.limit(LOGIN_RATE_LIMIT)
def login(request: Request, body: LoginRequest) -> LoginResponse:
    """Two independent limits (`core/rate_limit.py`): the decorator above is
    this route's own per-address flood guard, at `AUTH_LOGIN_RATE_LIMIT`
    (default 10/minute) -- it bounds total request volume from one address
    regardless of what email each request names, including a flood of
    made-up emails. The check below is the SAME number, but keyed on the
    email TOGETHER WITH the address: this is what stops an attacker who does
    not control the address from locking the named victim out by guessing
    from elsewhere -- each attacking address only ever spends its own
    (email, that address) bucket, never the victim's own (email, victim's
    address) one."""
    _ = request  # the rate limiter reads it from the signature
    check_identity_rate_limit(
        "auth_login_identity", "login", body.email, sign_in_address(request), LOGIN_RATE_LIMIT
    )
    pool_id = _require_env("COGNITO_USER_POOL_ID")
    client_id = _require_env("COGNITO_CLIENT_ID")
    cognito = _cognito_client()
    try:
        resp = cognito.admin_initiate_auth(
            UserPoolId=pool_id,
            ClientId=client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": body.email,
                "PASSWORD": body.password,
            },
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NotAuthorizedException", "UserNotFoundException"):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        raise HTTPException(status_code=502, detail=f"Cognito error: {code}")

    challenge = resp.get("ChallengeName")
    if challenge == "NEW_PASSWORD_REQUIRED":
        return LoginResponse(
            status="new_password_required",
            session=resp.get("Session"),
            challenge_name=challenge,
        )

    auth = resp.get("AuthenticationResult") or {}
    return _auth_result_to_response(auth)


@router.post("/respond", response_model=LoginResponse)
@limiter.limit(RESPOND_RATE_LIMIT)
def respond_challenge(request: Request, body: RespondRequest) -> LoginResponse:
    """Same two-limit shape as `/login` (see its docstring): the decorator is
    this route's own per-address flood guard, and the check below is
    `AUTH_RESPOND_RATE_LIMIT` keyed on the email TOGETHER WITH the address,
    which bounds brute force of the one-time `temporary_password` emitted by
    admin user creation without letting an attacker lock the named user out
    from an address the user never uses."""
    _ = request
    check_identity_rate_limit(
        "auth_respond_identity", "login", body.email, sign_in_address(request), RESPOND_RATE_LIMIT
    )
    pool_id = _require_env("COGNITO_USER_POOL_ID")
    client_id = _require_env("COGNITO_CLIENT_ID")
    cognito = _cognito_client()
    try:
        resp = cognito.admin_respond_to_auth_challenge(
            UserPoolId=pool_id,
            ClientId=client_id,
            ChallengeName="NEW_PASSWORD_REQUIRED",
            Session=body.session,
            ChallengeResponses={
                "USERNAME": body.email,
                "NEW_PASSWORD": body.new_password,
            },
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "InvalidPasswordException":
            raise HTTPException(
                status_code=400,
                detail="Password does not meet policy (length/upper/lower/digit/symbol required)",
            )
        raise HTTPException(status_code=502, detail=f"Cognito error: {code}")

    auth = resp.get("AuthenticationResult") or {}
    return _auth_result_to_response(auth)


def _auth_result_to_response(auth: dict[str, Any]) -> LoginResponse:
    if not auth:
        raise HTTPException(status_code=502, detail="Cognito returned empty auth result")
    return LoginResponse(
        status="authenticated",
        access_token=auth.get("AccessToken"),
        id_token=auth.get("IdToken"),
        refresh_token=auth.get("RefreshToken"),
        expires_in=auth.get("ExpiresIn"),
        token_type=auth.get("TokenType"),
    )
