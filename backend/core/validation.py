"""
Common Validation Helpers

日付範囲チェック等、複数エンドポイントで共有するバリデーションロジック。
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
    """日付範囲が上限以内かを検証する。

    Args:
        parsed_start: 開始日時 (None 可)。
        parsed_end: 終了日時 (None 可)。
        max_days: 許容する最大日数。デフォルト 90。

    Raises:
        HTTPException(422): 日付範囲が max_days を超過した場合。
    """
    if parsed_start and parsed_end:
        if (parsed_end - parsed_start).days > max_days:
            raise HTTPException(
                status_code=422,
                detail=f"Date range cannot exceed {max_days} days",
            )


def parse_iso_date(value: Optional[str], field_name: str) -> Optional[datetime]:
    """ISO 8601 文字列を datetime に変換する。

    Args:
        value: ISO 8601 形式の日付文字列 (None 可)。
        field_name: エラーメッセージ用のフィールド名。

    Returns:
        パース済み datetime、または None。

    Raises:
        HTTPException(422): フォーマットが不正な場合。
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
