"""CLI 用の未認証 bootstrap エンドポイント.

OSS 版では Admin が CloudFront URL 1 つだけをユーザーに共有し、
CLI は `stratoclave setup https://xxx.cloudfront.net` で指定された URL に対して
`GET /.well-known/stratoclave-config` を叩いて残りの設定を自動取得する.

返却するフィールドはすべて「Cognito Hosted UI でブラウザに既に露出している値」であり
secret を含まない. 従って本エンドポイントは未認証で公開する.

スキーマ (schema_version = "1"):
  {
    "schema_version": "1",
    "api_endpoint": "https://xxx.cloudfront.net",
    "cognito": {
      "user_pool_id": "us-east-1_XXXX",
      "client_id": "...",
      "domain": "https://xxx.auth.us-east-1.amazoncognito.com",
      "region": "us-east-1"
    },
    "cli": {
      "default_model": "us.anthropic.claude-opus-4-7",
      "callback_port": 18080
    }
  }
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from core.logging import get_logger


logger = get_logger(__name__)

router = APIRouter(prefix="/.well-known", tags=["well-known"])


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

# CLI 側 (Rust 実装) と揃える OAuth2 PKCE callback 用ポート
_DEFAULT_CALLBACK_PORT = 18080

# CLI が未指定時に使う Bedrock モデル.
# (backend 全体のデフォルト DEFAULT_BEDROCK_MODEL_ID とは別軸で、CLI 向けの
#  "最新世代 Opus" を案内する目的で独立させている)
_DEFAULT_CLI_MODEL_FALLBACK = "us.anthropic.claude-opus-4-7"

_DEFAULT_REGION_FALLBACK = "us-east-1"

# Cache-Control: 設定は頻繁に変わらないが、変更時に CLI 再実行で 5 分以内に
# 解決される範囲で cache 可能に.
_CACHE_CONTROL = "public, max-age=300"


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class CognitoInfo(BaseModel):
    user_pool_id: str
    client_id: str
    domain: str  # full URL, e.g. "https://xxx.auth.us-east-1.amazoncognito.com"
    region: str


class CliHints(BaseModel):
    default_model: str
    callback_port: int


class StratoclaveConfig(BaseModel):
    schema_version: str
    api_endpoint: str
    cognito: CognitoInfo
    cli: CliHints


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _derive_api_endpoint(request: Request) -> str:
    """Request ヘッダから呼ばれた URL の origin を推定する.

    優先順:
      1. env `STRATOCLAVE_API_ENDPOINT` が明示指定されていればそれを使う
         (ただし request 推定が localhost を含む場合のフォールバックとしても使う)
      2. X-Forwarded-Proto + X-Forwarded-Host (CloudFront / ALB 経由)
      3. request.url の scheme + host (ローカル実行時)

    推定結果が localhost を含む場合、`STRATOCLAVE_API_ENDPOINT` env が
    設定されていればそちらで上書きする (CloudFront 経由のアクセスを想定).
    """
    # 1. 明示指定の env が最優先
    explicit = os.getenv("STRATOCLAVE_API_ENDPOINT")
    if explicit:
        return explicit.rstrip("/")

    # 2. X-Forwarded-* を見る
    headers = request.headers
    forwarded_host = headers.get("x-forwarded-host")
    forwarded_proto = headers.get("x-forwarded-proto")
    if forwarded_host:
        # 複数 hop を経た場合は先頭を使う (RFC 7239 の慣行)
        host = forwarded_host.split(",")[0].strip()
        proto = (forwarded_proto or "https").split(",")[0].strip()
        return f"{proto}://{host}".rstrip("/")

    # 3. request.url から組み立て
    scheme = request.url.scheme or "http"
    host = request.url.hostname or "localhost"
    port = request.url.port
    # 標準ポートはあえて省略
    if port and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def _resolve_region() -> str:
    return (
        os.getenv("COGNITO_REGION")
        or os.getenv("AWS_REGION")
        or _DEFAULT_REGION_FALLBACK
    )


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        # 503: 本エンドポイントが機能するには ECS 側で env var が設定されている必要があり、
        # 未設定は一時的な構成不備 (Service Unavailable) として扱う.
        logger.warning(
            "well_known_config_unavailable",
            missing_env=name,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                f"Server is not fully configured: {name} is not set. "
                "Contact your administrator."
            ),
        )
    return value


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get(
    "/stratoclave-config",
    response_model=StratoclaveConfig,
    summary="CLI 用の bootstrap 設定を返す (未認証)",
    description=(
        "Stratoclave CLI が初回起動時に叩く bootstrap エンドポイント. "
        "返却値は Cognito Hosted UI でブラウザに露出している値のみで構成され、"
        "secret は含まない."
    ),
)
def get_stratoclave_config(request: Request, response: Response) -> StratoclaveConfig:
    # 1. api_endpoint を Request から推定
    api_endpoint = _derive_api_endpoint(request)

    # localhost フォールバック: 推定が localhost だった場合、env で上書き可能
    if "localhost" in api_endpoint or "127.0.0.1" in api_endpoint:
        override = os.getenv("STRATOCLAVE_API_ENDPOINT")
        if override:
            api_endpoint = override.rstrip("/")

    # 2. Cognito 情報 (必須 env)
    user_pool_id = _require_env("COGNITO_USER_POOL_ID")
    client_id = _require_env("COGNITO_CLIENT_ID")
    cognito_domain = _require_env("COGNITO_DOMAIN")
    # COGNITO_DOMAIN は full URL 想定.
    # 運用で "xxx.auth.us-east-1.amazoncognito.com" のようなホストのみ入って
    # しまった場合に備えて、スキーマが無ければ https:// を付ける.
    if not cognito_domain.startswith(("http://", "https://")):
        cognito_domain = f"https://{cognito_domain}"
    cognito_domain = cognito_domain.rstrip("/")

    region = _resolve_region()

    # 3. CLI hints
    default_model = os.getenv("DEFAULT_BEDROCK_MODEL") or _DEFAULT_CLI_MODEL_FALLBACK

    config = StratoclaveConfig(
        schema_version="1",
        api_endpoint=api_endpoint,
        cognito=CognitoInfo(
            user_pool_id=user_pool_id,
            client_id=client_id,
            domain=cognito_domain,
            region=region,
        ),
        cli=CliHints(
            default_model=default_model,
            callback_port=_DEFAULT_CALLBACK_PORT,
        ),
    )

    # secret を含めない保証 (念のため, 開発中の regression 検知用)
    _assert_no_secret(config)

    # Cache-Control を付与 (5 分)
    response.headers["Cache-Control"] = _CACHE_CONTROL

    logger.info(
        "well_known_config_served",
        api_endpoint=api_endpoint,
        has_cognito_domain=bool(cognito_domain),
        region=region,
    )

    return config


# ---------------------------------------------------------------------------
# Safety guard
# ---------------------------------------------------------------------------


_SECRET_SUBSTRINGS = ("secret", "password", "private_key", "aws_secret_access_key")


def _assert_no_secret(config: StratoclaveConfig) -> None:
    """レスポンスに secret と疑われるフィールドが混入していないことを検証する.

    冗長な guard だが、将来 field 追加時の regression を防ぐ目的で残す.
    違反は 500 ではなく RuntimeError とし、サーバー側のログで検知する.
    """
    dumped = config.model_dump()

    def _walk(obj: object, path: str = "") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                key_lower = str(k).lower()
                for marker in _SECRET_SUBSTRINGS:
                    if marker in key_lower:
                        raise RuntimeError(
                            f"well_known config contains a secret-like field: {path}.{k}"
                        )
                _walk(v, f"{path}.{k}")

    _walk(dumped)
