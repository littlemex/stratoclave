"""CLI 用 Cognito User/Pass ログインヘルパー.

MVP では CLI が Cognito InitiateAuth を直接叩ける環境権限を持たないため、
Backend がパススルーで Cognito を叩き、JWT を返す仕組みを用意する.

エンドポイント:
  POST /api/mvp/auth/login
    入力: { "email", "password" }
    返却: Cognito の AuthenticationResult (成功時) または ChallengeName (初回)

  POST /api/mvp/auth/respond
    入力: { "email", "new_password", "session" } (NEW_PASSWORD_REQUIRED チャレンジへの応答)
    返却: Cognito の AuthenticationResult
"""
from __future__ import annotations

import os
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.rate_limit import LOGIN_RATE_LIMIT, RESPOND_RATE_LIMIT, limiter


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
    """P0-3: limited to `AUTH_LOGIN_RATE_LIMIT` per source IP (default
    10/minute). Credential stuffing + user enumeration mitigation."""
    _ = request  # slowapi requires it to be in the signature
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
    """P0-3: same per-IP rate limit as /login. Prevents brute force of
    the one-time `temporary_password` emitted by admin user creation."""
    _ = request
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
