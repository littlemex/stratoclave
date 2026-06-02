"""Unauthenticated bootstrap endpoint for the CLI.

In the OSS distribution an admin only has to share one URL — the
CloudFront origin. The CLI is invoked as
`stratoclave setup https://xxx.cloudfront.net` and reads
`GET /.well-known/stratoclave-config` to discover everything else.

Every field returned here is already exposed to the browser through the
Cognito Hosted UI. No secrets are included, so the endpoint is public.

Schema (`schema_version = "1"`):

    {
      "schema_version": "1",
      "api_endpoint":   "https://xxx.cloudfront.net",
      "cognito": {
        "user_pool_id": "us-east-1_XXXX",
        "client_id":    "...",
        "domain":       "https://xxx.auth.us-east-1.amazoncognito.com",
        "region":       "us-east-1"
      },
      "cli": {
        "default_model": "us.anthropic.claude-opus-4-7",
        "callback_port": 18080,
        "codex": {                              // only when CODEX_ENABLED=true
          "default_model":     "openai.gpt-5.4",
          "openai_base_path":  "/openai/v1",
          "supported_regions": ["us-east-2", "us-west-2"]
        }
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
# Constants
# ---------------------------------------------------------------------------

# OAuth2 PKCE callback port — must match the value baked into the Rust CLI.
_DEFAULT_CALLBACK_PORT = 18080

# Default model surfaced to the CLI when DEFAULT_BEDROCK_MODEL is unset.
# Decoupled from the backend-internal DEFAULT_BEDROCK_MODEL_ID so the CLI
# can advertise a different "latest Opus" than the route fallback.
_DEFAULT_CLI_MODEL_FALLBACK = "us.anthropic.claude-opus-4-7"

# Default Codex model surfaced to the CLI when DEFAULT_CODEX_MODEL is unset.
_DEFAULT_CODEX_MODEL_FALLBACK = "openai.gpt-5.4"
_DEFAULT_OPENAI_BASE_PATH = "/openai/v1"
_DEFAULT_OPENAI_SUPPORTED_REGIONS = "us-east-2,us-west-2"

_DEFAULT_REGION_FALLBACK = "us-east-1"

# Cache-Control: configuration changes are rare; 5 min is enough that
# `stratoclave setup` reruns pick up updates promptly.
_CACHE_CONTROL = "public, max-age=300"


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class CognitoInfo(BaseModel):
    user_pool_id: str
    client_id: str
    domain: str  # full URL, e.g. "https://xxx.auth.us-east-1.amazoncognito.com"
    region: str


class CodexHints(BaseModel):
    default_model: str
    openai_base_path: str
    supported_regions: list[str]


class CliHints(BaseModel):
    default_model: str
    callback_port: int
    codex: Optional[CodexHints] = None


class StratoclaveConfig(BaseModel):
    schema_version: str
    api_endpoint: str
    cognito: CognitoInfo
    cli: CliHints


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _derive_api_endpoint(request: Request) -> str:
    """Infer the public origin used to reach this endpoint.

    Resolution order:
      1. `STRATOCLAVE_API_ENDPOINT` env (always wins when set).
      2. `X-Forwarded-Proto` + `X-Forwarded-Host` (CloudFront / ALB).
      3. `request.url.scheme` + host (local development).

    A localhost result triggers a fallback to the env var when set, so a
    direct ALB hit through localhost during dev still returns the public
    URL when one is configured.
    """
    # 1. Explicit env beats everything else.
    explicit = os.getenv("STRATOCLAVE_API_ENDPOINT")
    if explicit:
        return explicit.rstrip("/")

    # 2. Inspect X-Forwarded-* headers.
    headers = request.headers
    forwarded_host = headers.get("x-forwarded-host")
    forwarded_proto = headers.get("x-forwarded-proto")
    if forwarded_host:
        # Take the first hop (RFC 7239 convention).
        host = forwarded_host.split(",")[0].strip()
        proto = (forwarded_proto or "https").split(",")[0].strip()
        return f"{proto}://{host}".rstrip("/")

    # 3. Synthesize from request.url.
    scheme = request.url.scheme or "http"
    host = request.url.hostname or "localhost"
    port = request.url.port
    # Drop standard ports.
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
        # 503: this endpoint depends on ECS-injected env vars; absence is
        # treated as a transient configuration gap (Service Unavailable).
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


def _resolve_codex_hints() -> Optional[CodexHints]:
    """Return Codex CLI hints when CODEX_ENABLED, otherwise None.

    The route handler in `mvp/openai_responses.py` re-checks `CODEX_ENABLED`
    on every request so this discovery omission is purely cosmetic — old
    CLIs that never see `cli.codex` simply never offer the codex
    subcommand bootstrap.
    """
    if os.getenv("CODEX_ENABLED", "false").lower() != "true":
        return None
    raw_regions = os.getenv(
        "OPENAI_BEDROCK_REGIONS", _DEFAULT_OPENAI_SUPPORTED_REGIONS
    )
    regions = [r.strip() for r in raw_regions.split(",") if r.strip()]
    return CodexHints(
        default_model=os.getenv("DEFAULT_CODEX_MODEL", _DEFAULT_CODEX_MODEL_FALLBACK),
        openai_base_path=os.getenv("OPENAI_BASE_PATH", _DEFAULT_OPENAI_BASE_PATH),
        supported_regions=regions,
    )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get(
    "/stratoclave-config",
    response_model=StratoclaveConfig,
    summary="Return CLI bootstrap configuration (unauthenticated)",
    description=(
        "Bootstrap endpoint hit by the Stratoclave CLI on first run. The "
        "response is composed only of values already exposed by the "
        "Cognito Hosted UI; no secrets are included."
    ),
)
def get_stratoclave_config(request: Request, response: Response) -> StratoclaveConfig:
    # 1. Infer api_endpoint from the request.
    api_endpoint = _derive_api_endpoint(request)

    # localhost fallback: when the inference returns localhost, allow env
    # to override (covers dev hits through 127.0.0.1).
    if "localhost" in api_endpoint or "127.0.0.1" in api_endpoint:
        override = os.getenv("STRATOCLAVE_API_ENDPOINT")
        if override:
            api_endpoint = override.rstrip("/")

    # 2. Cognito info (required env).
    user_pool_id = _require_env("COGNITO_USER_POOL_ID")
    client_id = _require_env("COGNITO_CLIENT_ID")
    cognito_domain = _require_env("COGNITO_DOMAIN")
    # COGNITO_DOMAIN is expected to be a full URL. If only a hostname was
    # supplied (e.g. "xxx.auth.us-east-1.amazoncognito.com"), upgrade it.
    if not cognito_domain.startswith(("http://", "https://")):
        cognito_domain = f"https://{cognito_domain}"
    cognito_domain = cognito_domain.rstrip("/")

    region = _resolve_region()

    # 3. CLI hints.
    default_model = os.getenv("DEFAULT_BEDROCK_MODEL") or _DEFAULT_CLI_MODEL_FALLBACK
    codex_hints = _resolve_codex_hints()

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
            codex=codex_hints,
        ),
    )

    # Defensive: regression guard against accidentally exposing a secret.
    _assert_no_secret(config)

    response.headers["Cache-Control"] = _CACHE_CONTROL

    logger.info(
        "well_known_config_served",
        api_endpoint=api_endpoint,
        has_cognito_domain=bool(cognito_domain),
        region=region,
        codex_enabled=codex_hints is not None,
    )

    return config


# ---------------------------------------------------------------------------
# Safety guard
# ---------------------------------------------------------------------------


_SECRET_SUBSTRINGS = ("secret", "password", "private_key", "aws_secret_access_key")


def _assert_no_secret(config: StratoclaveConfig) -> None:
    """Verify the response has no secret-looking fields.

    Redundant given the schema, but kept as a regression net for future
    field additions. Violations raise `RuntimeError` so they surface in
    server logs (a 500 would mask the real cause).
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
