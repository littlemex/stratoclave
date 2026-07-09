"""
Common Validation Helpers

Shared validation logic used across multiple endpoints, such as date range checks.
"""
from datetime import datetime
from typing import Optional

from fastapi import HTTPException

from core.constants import MAX_DATE_RANGE_DAYS


def validate_date_range(
    parsed_start: Optional[datetime],
    parsed_end: Optional[datetime],
    max_days: int = MAX_DATE_RANGE_DAYS,
) -> None:
    """Validate that a date range does not exceed the allowed maximum.

    Args:
        parsed_start: Start datetime (may be None).
        parsed_end: End datetime (may be None).
        max_days: Maximum allowed span in days. Defaults to 90.

    Raises:
        HTTPException(422): When the range exceeds max_days.
    """
    if parsed_start and parsed_end:
        if (parsed_end - parsed_start).days > max_days:
            raise HTTPException(
                status_code=422,
                detail=f"Date range cannot exceed {max_days} days",
            )


def parse_iso_date(value: Optional[str], field_name: str) -> Optional[datetime]:
    """Parse an ISO 8601 string into a datetime object.

    Args:
        value: ISO 8601 date string (may be None).
        field_name: Field name used in error messages.

    Returns:
        Parsed datetime, or None.

    Raises:
        HTTPException(422): When the format is invalid.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {field_name} format. Use ISO 8601.",
        )
