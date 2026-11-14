"""Facts about how this process is deployed, read in one place so every module
answers them the same way: whether it runs locally, how a boolean setting is
spelled, and which host the gateway's own API URL names."""
from __future__ import annotations

import os
from typing import Optional
from urllib.parse import urlsplit

#: `ENVIRONMENT` values for a developer machine or a test run, where the gateway
#: may infer its own URL from request headers. Anything else is a deployment.
LOCAL_ENVIRONMENTS = frozenset({"development", "local", "test"})

_TRUE = frozenset({"1", "true", "yes", "on"})
_DEFAULT_PORTS = {"https": 443, "http": 80}


def is_local_environment() -> bool:
    return os.getenv("ENVIRONMENT", "").strip().lower() in LOCAL_ENVIRONMENTS


def flag(name: str) -> bool:
    """A boolean setting: on for 1/true/yes/on (any case), off otherwise."""
    return os.getenv(name, "").strip().lower() in _TRUE


def host_of(url: str) -> Optional[str]:
    """The host a URL names, lowercased, with the port only when it is not the
    scheme's default (the same normalisation the CLI applies), or None when the
    value is not an absolute http(s) URL."""
    parts = urlsplit(url.strip())
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname.lower()
    return host if port in (None, _DEFAULT_PORTS[parts.scheme]) else f"{host}:{port}"


def api_endpoint() -> Optional[str]:
    """The gateway's own public API URL (`STRATOCLAVE_API_ENDPOINT`), or None."""
    value = os.getenv("STRATOCLAVE_API_ENDPOINT", "").strip().rstrip("/")
    return value or None


def validate_api_endpoint() -> None:
    """Startup check: a deployment must name its own API URL, as an absolute
    http(s) URL. The CLI is told this URL, and signed sign-in requests are bound
    to its host (`mvp.sso_sts`)."""
    value = api_endpoint()
    if value is None:
        if is_local_environment():
            return
        raise RuntimeError("STRATOCLAVE_API_ENDPOINT must be set outside a local environment")
    if host_of(value) is None:
        raise RuntimeError(f"STRATOCLAVE_API_ENDPOINT is not an absolute http(s) URL: {value!r}")
