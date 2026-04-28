"""
Logging Configuration

Structured logging with structlog
"""
import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor


def add_app_context(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """Add application context to log entries"""
    event_dict["app"] = "stratoclave"
    event_dict["component"] = event_dict.get("logger", "unknown")
    return event_dict


def mask_sensitive_data(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """Mask sensitive data in log entries.

    Two levels of masking:
      - ``REDACT_KEYS`` — always replaced with ``***REDACTED***`` regardless
        of value length (tokens, passwords, secrets, API keys).
      - ``PII_KEYS`` — hashed to an 8-char marker (``pii:abcd1234``) so log
        correlation across entries for the same user is still possible
        without storing the original value. `email` is the canonical PII
        field; extend this set when new user-level identifiers are added.
    """
    import hashlib

    REDACT_KEYS = {
        "auth_token",
        "token",
        "access_token",
        "id_token",
        "refresh_token",
        "password",
        "secret",
        "api_key",
        "plaintext_key",
    }
    PII_KEYS = {"email", "user_email", "actor_email"}

    def _pii_marker(value: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()[:8]
        return f"pii:{digest}"

    for key in list(event_dict.keys()):
        lowered = key.lower()
        if lowered in REDACT_KEYS:
            event_dict[key] = "***REDACTED***"
            continue
        if lowered in PII_KEYS:
            value = event_dict[key]
            if isinstance(value, str) and value:
                event_dict[key] = _pii_marker(value)
            continue
        if isinstance(event_dict[key], str) and len(event_dict[key]) > 100:
            # Mask long strings that might carry an embedded token.
            if any(s in lowered for s in ["token", "key", "auth"]):
                event_dict[key] = f"{event_dict[key][:10]}...***REDACTED***"

    return event_dict


def setup_logging(environment: str = "development") -> None:
    """
    Setup structured logging

    Args:
        environment: "development" or "production"
    """
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        add_app_context,
        mask_sensitive_data,
    ]

    if environment == "production":
        # JSON output for production
        structlog.configure(
            processors=shared_processors + [
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.stdlib.BoundLogger,
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )

        # Configure stdlib logging for JSON output
        logging.basicConfig(
            format="%(message)s",
            stream=sys.stdout,
            level=logging.INFO,
        )
    else:
        # Console output for development
        structlog.configure(
            processors=shared_processors + [
                structlog.processors.format_exc_info,
                structlog.dev.ConsoleRenderer(colors=True),
            ],
            wrapper_class=structlog.stdlib.BoundLogger,
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )

        # Configure stdlib logging for console output
        logging.basicConfig(
            format="%(message)s",
            stream=sys.stdout,
            level=logging.DEBUG,
        )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    Get a structured logger

    Args:
        name: Logger name (usually __name__)

    Returns:
        Configured structlog logger
    """
    return structlog.get_logger(name)
