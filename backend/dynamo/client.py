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


def ui_tickets_table_name() -> str:
    """Short-lived, single-use tickets that hand a CLI session off to
    the web UI without ever placing the access token in a URL.

    The CLI mints a ticket via POST /api/mvp/auth/ui-ticket, the
    browser consumes it via POST /api/mvp/auth/ui-ticket/consume, and
    the record is deleted on first consume. DynamoDB TTL reaps
    unconsumed tickets after ~30 s.
    """
    return table_name("DYNAMODB_UI_TICKETS_TABLE", "stratoclave-ui-tickets")
