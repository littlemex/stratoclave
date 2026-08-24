"""bedrock-mantle transport: endpoint, auth, clients, error and usage parsing.

Two routes talk to bedrock-mantle — the OpenAI Responses route and the widened
OpenAI Chat Completions route — and they differ only in which path they POST to
and what they do with the body. Everything below that is transport: the endpoint
template, the short-lived bearer, client construction with the right timeouts,
and reading an error message or a usage block out of a mantle response.

Keeping those here means the endpoint template exists once. A second copy is the
kind of duplication that goes unnoticed until one of them is pointed at the wrong
region or path and only one route breaks.

The bearer TTL is deliberately short: it lives in the ECS task heap as a plain
string, so a smaller window bounds the blast radius after a task compromise. A
SigV4-from-task-role migration is tracked as a follow-up.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

import httpx
from fastapi import HTTPException

from core.error_handler import sanitize_exception_message
from core.logging import get_logger

logger = get_logger(__name__)

# 15 min, against the library default of 1 h (12 h max).
DEFAULT_TOKEN_TTL = timedelta(seconds=900)

# mantle's OpenAI-compatible surface. The `/openai/v1` prefix is part of the
# contract and distinct from the bare `/v1` some models are served under.
_ENDPOINT_TEMPLATE = "https://bedrock-mantle.{region}.api.aws/openai/v1"

# Long read window: a reasoning model can stay silent for a while before its first
# token. Connect stays tight because a slow TLS handshake is never the model.
_DEFAULT_TIMEOUT = httpx.Timeout(600.0, connect=10.0)


def base_url(region: str) -> str:
    """The mantle OpenAI-compatible base URL for `region`."""
    return _ENDPOINT_TEMPLATE.format(region=region)


def mint_bearer_token(region: str) -> str:
    """Mint a short-lived bearer token for mantle in `region`."""
    # Imported lazily so the module loads even where the dependency is absent
    # (e.g. a dev environment running only the Converse tests); the route-time
    # failure is then a 503 rather than a worker that will not start.
    try:
        from aws_bedrock_token_generator import provide_token  # type: ignore
    except ImportError as exc:  # pragma: no cover — covered at deploy time
        raise HTTPException(
            status_code=503,
            detail=(
                "A bedrock-mantle route is enabled but "
                "aws-bedrock-token-generator is not installed. "
                "Add it to backend/requirements.txt."
            ),
        ) from exc
    try:
        return provide_token(region=region, expiry=DEFAULT_TOKEN_TTL)
    except TypeError:
        # Older versions lack the `expiry` kwarg. Staying functional beats failing
        # closed here, but the token then defaults to 1 h.
        logger.warning(
            "bedrock_token_generator_no_expiry",
            region=region,
            note="library version does not support expiry kwarg; using default TTL",
        )
        return provide_token(region=region)


def _auth_headers(region: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_bearer_token(region)}"}


def async_client(region: str, *, timeout: Optional[httpx.Timeout] = None) -> httpx.AsyncClient:
    """An async mantle client for `region`. Use for streaming: a sync client would
    hold a threadpool worker for the life of the stream."""
    return httpx.AsyncClient(
        base_url=base_url(region),
        headers=_auth_headers(region),
        timeout=timeout or _DEFAULT_TIMEOUT,
    )


def sync_client(region: str, *, timeout: Optional[httpx.Timeout] = None) -> httpx.Client:
    """A blocking mantle client for `region`. Use from a sync route that is already
    running in a threadpool."""
    return httpx.Client(
        base_url=base_url(region),
        headers=_auth_headers(region),
        timeout=timeout or _DEFAULT_TIMEOUT,
    )


def format_error(resp: httpx.Response) -> str:
    """A sanitized error message from a non-2xx mantle response."""
    try:
        body = resp.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                msg = err.get("message")
                if isinstance(msg, str) and msg:
                    return sanitize_exception_message(msg)
    except Exception:  # noqa: BLE001 — a non-JSON error body is still an error
        pass
    return sanitize_exception_message(resp.text[:500])


def extract_usage(usage: Any) -> tuple[int, int]:
    """Return `(input_tokens, output_tokens)` from a mantle usage block.

    Accepts both the Responses spelling (`input_tokens`/`output_tokens`) and the
    Chat Completions one (`prompt_tokens`/`completion_tokens`), because the two
    routes share this parser and intermediate proxies normalise inconsistently.
    `output_tokens_details.reasoning_tokens` is a SUBSET of `output_tokens` in the
    Responses contract; it must not be added separately.
    """
    if not isinstance(usage, dict):
        return 0, 0
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens", 0) or 0)
    return input_tokens, output_tokens
