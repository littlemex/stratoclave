"""Shared helpers for Cognito admin operations (Phase 2).

Centralizes all backend Cognito calls in one place:
- admin_create_user / admin_set_user_password
- admin_delete_user
- admin_update_user_attributes (updates custom:org_id on tenant switch)
- admin_user_global_sign_out (immediately invalidates JWTs after a tenant switch)
- admin_get_user (used to backfill missing email)
"""
from __future__ import annotations

import os
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException


_client: Optional[Any] = None


def get_client():
    global _client
    if _client is None:
        region = os.getenv("COGNITO_REGION") or os.getenv("AWS_REGION", "us-east-1")
        _client = boto3.client("cognito-idp", region_name=region)
    return _client


def require_user_pool_id() -> str:
    pool_id = os.getenv("COGNITO_USER_POOL_ID")
    if not pool_id:
        raise HTTPException(
            status_code=500,
            detail="COGNITO_USER_POOL_ID is not configured",
        )
    return pool_id


def delete_user(email: str) -> None:
    """Delete a user from Cognito. Silently succeeds if the user does not exist."""
    try:
        get_client().admin_delete_user(
            UserPoolId=require_user_pool_id(),
            Username=email,
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "UserNotFoundException":
            return
        raise HTTPException(status_code=502, detail=f"Cognito delete error: {code}")


def update_org_id(sub: str, new_tenant_id: str) -> None:
    """Update the Cognito custom:org_id attribute (called on tenant switch)."""
    try:
        get_client().admin_update_user_attributes(
            UserPoolId=require_user_pool_id(),
            Username=sub,
            UserAttributes=[{"Name": "custom:org_id", "Value": new_tenant_id}],
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        raise HTTPException(
            status_code=502,
            detail=f"Cognito admin_update_user_attributes error: {code}",
        )


def global_sign_out(sub: str) -> None:
    """Force sign-out from all devices (immediately invalidates JWTs on tenant switch)."""
    try:
        get_client().admin_user_global_sign_out(
            UserPoolId=require_user_pool_id(),
            Username=sub,
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "UserNotFoundException":
            return
        raise HTTPException(
            status_code=502,
            detail=f"Cognito admin_user_global_sign_out error: {code}",
        )
