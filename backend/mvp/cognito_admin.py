"""Cognito admin 操作の共通ヘルパー (Phase 2).

Backend が Cognito を叩く操作を一箇所にまとめる。
- admin_create_user / admin_set_user_password
- admin_delete_user
- admin_update_user_attributes (Tenant 切替時の custom:org_id 更新)
- admin_user_global_sign_out (Tenant 切替後の JWT 即時失効)
- admin_get_user (email 補填用)
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
    """Cognito から user を削除。存在しない場合は silently ok."""
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
    """Cognito の custom:org_id 属性を更新 (Tenant 切替時)."""
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
    """全デバイスから強制サインアウト (Tenant 切替時の JWT 即時失効)."""
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
