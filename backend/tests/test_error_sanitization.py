"""sanitize_exception_message regression tests (P1-7 usage site).

These guard the existing sanitizer rules so that future rule changes
cannot silently expose the AWS account ID, ARNs, file paths, or tokens
that the 502 response on `/v1/messages` forwards to the client.
"""
from __future__ import annotations

from core.error_handler import sanitize_exception_message


def test_redacts_arn_with_account_id() -> None:
    arn = "arn:aws:bedrock:us-east-1:123456789012:inference-profile/foo"
    out = sanitize_exception_message(f"AccessDenied on {arn}")
    assert arn not in out
    assert "[ARN]" in out


def test_redacts_bare_12_digit_account_id() -> None:
    out = sanitize_exception_message("operation forbidden for 123456789012")
    assert "123456789012" not in out
    assert "[ACCOUNT_ID]" in out


def test_redacts_file_paths() -> None:
    out = sanitize_exception_message(
        "Import failed: /usr/local/lib/python/site-packages/mod.py missing"
    )
    assert "/usr/local/lib/python/site-packages/mod.py" not in out
    assert "[FILE_PATH]" in out


def test_redacts_credential_prefixes() -> None:
    out = sanitize_exception_message(
        "Bad credential: sk-stratoclave-abcdef1234567890xyz"
    )
    assert "sk-stratoclave-abcdef1234567890xyz" not in out
    assert "[CREDENTIALS]" in out


def test_redacts_ip_addresses() -> None:
    out = sanitize_exception_message("connection refused 10.0.12.34")
    assert "10.0.12.34" not in out
    assert "[IP_ADDRESS]" in out


def test_passthrough_when_nothing_sensitive() -> None:
    msg = "ValidationException: Input must be shorter than 8192 characters"
    assert sanitize_exception_message(msg) == msg
