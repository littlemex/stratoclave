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
    AUTH_ADDRESS_RATE_LIMIT,
    AUTH_SIGN_IN_ADDRESS_SCOPE,
    LOGIN_RATE_LIMIT,
    RESPOND_RATE_LIMIT,
    check_identity_rate_limit,
    limiter,
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
@limiter.limit(AUTH_ADDRESS_RATE_LIMIT, scope=AUTH_SIGN_IN_ADDRESS_SCOPE)
def login(request: Request, body: LoginRequest) -> LoginResponse:
    """Two independent limits, as every unauthenticated sign-in route has
    (`core/rate_limit.py`): the decorator above is this address's share of
    the coarse flood guard shared by every sign-in route, and the check below
    is `AUTH_LOGIN_RATE_LIMIT` (default 10/minute) keyed on the email itself --
    credential-stuffing and user-enumeration mitigation that a rotating
    attacker cannot evade by trying a fresh address per guess, nor by trying a
    fresh made-up email per request (the flood guard still bounds the
    address)."""
    _ = request  # the rate limiter reads it from the signature
    check_identity_rate_limit("auth_login_identity", "login", body.email, LOGIN_RATE_LIMIT)
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
@limiter.limit(AUTH_ADDRESS_RATE_LIMIT, scope=AUTH_SIGN_IN_ADDRESS_SCOPE)
def respond_challenge(request: Request, body: RespondRequest) -> LoginResponse:
    """Same two-limit shape as `/login` (see its docstring): the decorator is
    the shared address flood guard, and the check below is
    `AUTH_RESPOND_RATE_LIMIT` keyed on the email, which bounds brute force of
    the one-time `temporary_password` emitted by admin user creation."""
    _ = request
    check_identity_rate_limit("auth_respond_identity", "login", body.email, RESPOND_RATE_LIMIT)
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
