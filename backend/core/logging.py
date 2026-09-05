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


#: Third-party loggers whose DEBUG output is the wire, not a message this gateway
#: composed. `botocore` at DEBUG prints every request and response body it exchanges
#: with DynamoDB verbatim, so a `development` deployment was putting whole rows from the
#: users, tenants, user-tenants and permissions tables into a log group retained for
#: ninety days — including email addresses, which is the plaintext this project's clause
#: C12.4 forbids. `mask_sensitive_data` cannot help: the address is inside a serialised
#: body in the message position, which is the same blind spot the audit writer had.
#:
#: They are floored at INFO in EVERY environment rather than only in production. The
#: development default exists to make this gateway's own logs verbose, and a third-party
#: library's wire dump is not this gateway's log. An operator who genuinely wants a
#: botocore trace can raise it deliberately after `setup_logging` has run.
NOISY_THIRD_PARTY_LOGGERS: tuple[str, ...] = (
    "botocore",
    "boto3",
    "boto3.resources",
    "urllib3",
    "s3transfer",
)

#: The floor those loggers are held at. WARNING would also stop the leak, but INFO keeps
#: a retry or a throttle visible, which is operationally useful and carries no body.
THIRD_PARTY_LOG_FLOOR = logging.INFO


def _floor_third_party_loggers() -> None:
    """Stop a third-party library's DEBUG wire dump reaching the log sink.

    Called after `basicConfig`, because `basicConfig` sets the ROOT level and these
    loggers inherit it. Setting each one explicitly overrides that inheritance without
    touching the level this gateway's own loggers run at.
    """
    for name in NOISY_THIRD_PARTY_LOGGERS:
        logger = logging.getLogger(name)
        if logger.level == logging.NOTSET or logger.level < THIRD_PARTY_LOG_FLOOR:
            logger.setLevel(THIRD_PARTY_LOG_FLOOR)


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
        _floor_third_party_loggers()
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
        _floor_third_party_loggers()


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    Get a structured logger

    Args:
        name: Logger name (usually __name__)

    Returns:
        Configured structlog logger
    """
    return structlog.get_logger(name)
