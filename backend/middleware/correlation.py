"""
Correlation ID Middleware

Generates and propagates correlation IDs across requests
"""
import uuid
from typing import Callable

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


class CorrelationIDMiddleware(BaseHTTPMiddleware):
    """
    Middleware to generate and propagate correlation IDs

    Reads X-Correlation-ID header from request, or generates new UUID
    Adds correlation ID to structlog context and response headers
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """
        Process request and add correlation ID

        Args:
            request: Incoming request
            call_next: Next middleware/handler

        Returns:
            Response with X-Correlation-ID header
        """
        # Get or generate correlation ID
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())

        # Add to structlog context
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

        # Process request
        response = await call_next(request)

        # Add correlation ID to response headers
        response.headers["X-Correlation-ID"] = correlation_id

        return response
