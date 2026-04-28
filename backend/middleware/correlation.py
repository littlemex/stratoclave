"""Correlation ID middleware.

Reads the ``X-Correlation-ID`` header from the request, validates it
against a conservative pattern, and echoes the (validated or freshly
generated) value back on the response so log lines can be stitched
together across services.

Validation rule (from Team A-3 review): only hex characters, dashes and
underscores, 8–128 chars. Anything else — including CRLF, control
characters, or overly long strings — is replaced with a fresh UUID so
header smuggling and log-injection attacks cannot use this header as a
vehicle.
"""
from __future__ import annotations

import re
import uuid
from typing import Callable

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,128}$")


def _sanitize_correlation_id(raw: str | None) -> str:
    """Return a trusted correlation id: either the caller's (if it passes
    a strict charset + length check) or a fresh UUID4.
    """
    if raw and _CORRELATION_ID_RE.match(raw):
        return raw
    return str(uuid.uuid4())


class CorrelationIDMiddleware(BaseHTTPMiddleware):
    """Generate / propagate / echo a correlation ID on every request."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        correlation_id = _sanitize_correlation_id(
            request.headers.get("X-Correlation-ID")
        )

        # Re-bind contextvars so every log line inside the request gets
        # the validated value.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

        response = await call_next(request)

        # Echo the sanitized value back so clients can correlate.
        response.headers["X-Correlation-ID"] = correlation_id
        return response
