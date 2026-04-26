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
    """Mask sensitive data in log entries"""
    sensitive_keys = {"auth_token", "token", "password", "secret", "api_key"}

    for key in list(event_dict.keys()):
        if key.lower() in sensitive_keys:
            event_dict[key] = "***REDACTED***"
        elif isinstance(event_dict[key], str) and len(event_dict[key]) > 100:
            # Mask long strings that might contain tokens
            if any(s in key.lower() for s in ["token", "key", "auth"]):
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
