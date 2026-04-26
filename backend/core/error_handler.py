"""
Error Handler

エラーレスポンスのサニタイズとセキュアなエラーハンドリング。
AWS アカウント情報、ファイルパス、認証情報などの漏洩を防ぐ。
"""
import re
from typing import Optional, Dict, Any

from fastapi.responses import JSONResponse

from core.logging import get_logger

logger = get_logger(__name__)


def sanitize_exception_message(message: str) -> str:
    """例外メッセージから機密情報を除去する。

    以下の情報をサニタイズします：
    - AWS アカウント ID (12桁の数字)
    - AWS ARN (arn:aws:...)
    - ファイルパス (/home/, /usr/, /var/, C:\\, etc.)
    - API キー、トークン (sk-, pk-, Bearer, etc.)
    - IP アドレス

    Args:
        message: 元の例外メッセージ

    Returns:
        サニタイズされたメッセージ
    """
    # AWS ARN を除去（アカウント ID を含む）
    message = re.sub(r'arn:aws:[a-z0-9\-]+:[a-z0-9\-]*:\d{12}:[a-zA-Z0-9\-/_]+', '[ARN]', message)

    # AWS アカウント ID (12桁の数字) を除去
    # SEC-07: この正規表現は 12 桁の数字全般にマッチするため、
    # 電話番号やタイムスタンプなど AWS アカウント ID 以外の値も置換される可能性がある。
    # 現時点ではセキュリティ優先でこの挙動を維持する（false positive は安全側に倒れる）。
    # より厳密な判定が必要な場合は、ARN コンテキストやプレフィックスでの判定を検討する。
    message = re.sub(r'\b\d{12}\b', '[ACCOUNT_ID]', message)

    # ファイルパス（絶対パス）を除去
    message = re.sub(r'(?:/[a-zA-Z0-9_\-\.]+)+/[a-zA-Z0-9_\-\.]+\.[a-z]+', '[FILE_PATH]', message)
    message = re.sub(r'[A-Z]:\\(?:[a-zA-Z0-9_\-\.]+\\)+[a-zA-Z0-9_\-\.]+\.[a-z]+', '[FILE_PATH]', message)

    # API キー、トークンを除去
    message = re.sub(r'(sk|pk|Bearer|token)[-_][a-zA-Z0-9\-]{15,}', '[CREDENTIALS]', message, flags=re.IGNORECASE)

    # IP アドレスを除去
    message = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[IP_ADDRESS]', message)

    return message


def create_error_response(
    status_code: int,
    message: str,
    correlation_id: Optional[str] = None,
    log_details: Optional[Dict[str, Any]] = None
) -> JSONResponse:
    """セキュアなエラーレスポンスを作成する。

    ユーザーには汎用的なエラーメッセージを返し、
    詳細情報はログにのみ記録する。

    Args:
        status_code: HTTP ステータスコード
        message: ユーザーに表示するエラーメッセージ（汎用的なもの）
        correlation_id: リクエスト追跡用の相関 ID（オプション）
        log_details: ログに記録する詳細情報（オプション）

    Returns:
        JSONResponse オブジェクト

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
    # 詳細情報をログに記録（ユーザーには返さない）
    if log_details:
        logger.error(
            "api_error",
            status_code=status_code,
            message=message,
            correlation_id=correlation_id,
            **log_details
        )

    # ユーザーには汎用的なメッセージのみ返す
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
    """500 Internal Server Error レスポンスを作成する。

    Args:
        correlation_id: リクエスト追跡用の相関 ID
        log_details: ログに記録する詳細情報

    Returns:
        JSONResponse (500 エラー)
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
    """400 Bad Request レスポンスを作成する。

    Args:
        message: エラーメッセージ（汎用的なもの）
        correlation_id: リクエスト追跡用の相関 ID
        log_details: ログに記録する詳細情報

    Returns:
        JSONResponse (400 エラー)
    """
    return create_error_response(
        status_code=400,
        message=message,
        correlation_id=correlation_id,
        log_details=log_details
    )
