"""DynamoDB クライアント / テーブル名解決."""
import os
from functools import lru_cache

import boto3


@lru_cache(maxsize=1)
def get_dynamodb_resource():
    """Process-wide な DynamoDB resource を返す (stringset シリアライズ対応版)."""
    region = os.getenv("AWS_REGION", "us-east-1")
    return boto3.resource("dynamodb", region_name=region)


def table_name(env_var: str, fallback: str) -> str:
    """環境変数優先でテーブル名を返す."""
    return os.getenv(env_var, fallback)


def users_table_name() -> str:
    return table_name("DYNAMODB_USERS_TABLE", "stratoclave-users")


def user_tenants_table_name() -> str:
    return table_name("DYNAMODB_USER_TENANTS_TABLE", "stratoclave-user-tenants")


def usage_logs_table_name() -> str:
    return table_name("DYNAMODB_USAGE_LOGS_TABLE", "stratoclave-usage-logs")


def sse_tokens_table_name() -> str:
    return table_name("DYNAMODB_SSE_TOKENS_TABLE", "stratoclave-sse-tokens")


def sso_nonces_table_name() -> str:
    """Replay-protection nonces for the Vouch-by-STS flow.
    Stores the hash of each successfully verified signed GetCallerIdentity
    request with a short TTL, so an attacker that captures the signed
    payload cannot replay it inside the ±5-minute skew window.
    """
    return table_name("DYNAMODB_SSO_NONCES_TABLE", "stratoclave-sso-nonces")
