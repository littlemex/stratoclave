"""
Error Handler

Sanitization of error responses and secure error handling.
Prevents leakage of AWS account information, file paths, credentials, and similar sensitive data.
"""
import re
from typing import Optional, Dict, Any

from fastapi.responses import JSONResponse

from core.logging import get_logger

logger = get_logger(__name__)


def sanitize_exception_message(message: str) -> str:
    """Strip sensitive information from an exception message.

    Sanitizes the following:
    - AWS account IDs (12-digit numbers)
    - AWS ARNs (arn:aws:...)
    - File paths (/home/, /usr/, /var/, C:\\, etc.)
    - API keys and tokens (sk-, pk-, Bearer, etc.)
    - IP addresses

    Args:
        message: The original exception message.

    Returns:
        The sanitized message.
    """
    # Remove AWS ARNs (which contain account IDs).
    message = re.sub(r'arn:aws:[a-z0-9\-]+:[a-z0-9\-]*:\d{12}:[a-zA-Z0-9\-/_]+', '[ARN]', message)

    # Remove AWS account IDs (12-digit numbers).
    # SEC-07: This regex matches any 12-digit sequence, so values such as
    # phone numbers or timestamps may also be replaced. We accept this
    # trade-off in favour of security (false positives are safe-side errors).
    # If more precise matching is needed, consider restricting to ARN context
    # or known prefixes.
    message = re.sub(r'\b\d{12}\b', '[ACCOUNT_ID]', message)

    # Remove absolute file paths.
    message = re.sub(r'(?:/[a-zA-Z0-9_\-\.]+)+/[a-zA-Z0-9_\-\.]+\.[a-z]+', '[FILE_PATH]', message)
    message = re.sub(r'[A-Z]:\\(?:[a-zA-Z0-9_\-\.]+\\)+[a-zA-Z0-9_\-\.]+\.[a-z]+', '[FILE_PATH]', message)

    # Remove API keys and tokens.
    message = re.sub(r'(sk|pk|Bearer|token)[-_][a-zA-Z0-9\-]{15,}', '[CREDENTIALS]', message, flags=re.IGNORECASE)

    # Remove IP addresses.
    message = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[IP_ADDRESS]', message)

    return message


def create_error_response(
    status_code: int,
    message: str,
    correlation_id: Optional[str] = None,
    log_details: Optional[Dict[str, Any]] = None
) -> JSONResponse:
    """Build a secure error response.

    Returns a generic error message to the caller while recording
    the full details only in the log.

    Args:
        status_code: HTTP status code.
        message: Generic error message shown to the user.
        correlation_id: Request correlation ID for tracing (optional).
        log_details: Detailed information recorded only in the log (optional).

    Returns:
        A JSONResponse object.

    Example:
        ```python
        try:
            # AWS API call
        except Exception as e:
            return create_error_response(
                500,
                "Internal server error",
                correlation_id=request_id,
                log_details={"exception": str(e), "traceback": traceback.format_exc()}
            )
        ```
    """
    # Log details internally — do not include them in the response body.
    if log_details:
        logger.error(
            "api_error",
            status_code=status_code,
            message=message,
            correlation_id=correlation_id,
            **log_details
        )

    # Return only the generic message to the caller.
    response_body = {
        "detail": message,
    }

    if correlation_id:
        response_body["correlation_id"] = correlation_id

    return JSONResponse(
        status_code=status_code,
        content=response_body
    )


def internal_server_error(
    correlation_id: Optional[str] = None,
    log_details: Optional[Dict[str, Any]] = None
) -> JSONResponse:
    """Build a 500 Internal Server Error response.

    Args:
        correlation_id: Request correlation ID for tracing.
        log_details: Detailed information recorded only in the log.

    Returns:
        JSONResponse (500 error).
    """
    return create_error_response(
        status_code=500,
        message="Internal server error",
        correlation_id=correlation_id,
        log_details=log_details
    )


def bad_request_error(
    message: str,
    correlation_id: Optional[str] = None,
    log_details: Optional[Dict[str, Any]] = None
) -> JSONResponse:
    """Build a 400 Bad Request response.

    Args:
        message: Generic error message shown to the user.
        correlation_id: Request correlation ID for tracing.
        log_details: Detailed information recorded only in the log.

    Returns:
        JSONResponse (400 error).
    """
    return create_error_response(
        status_code=400,
        message=message,
        correlation_id=correlation_id,
        log_details=log_details
    )
