"""Regression guard for sweep-4 C-Critical: the bootstrap admin's
temporary password MUST NOT appear inside the structured logger (which
ships to CloudWatch Logs in production and is commonly ingested by SIEM
and downstream audit stores), and MUST NOT be written to stderr either
(Fargate's awslogs driver forwards stderr to CloudWatch too).

Prior behaviour (sweep-1 C-F regressed): seed.py called
`logger.info("bootstrap_admin_created", ..., temporary_password=pw)`
which writes plaintext into structlog, i.e. CloudWatch.

Sweep-4 round-4 behaviour:
  * plaintext password NEVER in structured logger records
  * plaintext password NEVER in stderr either
  * plaintext password written only to AWS Secrets Manager, which the
    operator retrieves once with get-secret-value and then deletes.

This test monkeypatches `_generate_temp_password` to a fixed sentinel
so we can search for the EXACT string in every output channel, rather
than guessing a password format with regex (the pre-round-4 version
of this test had a regex bug where it matched the sentinel prefix
instead of the real password, rendering the guard useless).
"""
from __future__ import annotations

import io
import logging
import sys

import pytest


SENTINEL_PASSWORD = "TestSentinel!9b7Qx_4fA1zLp-WmNcKj"


def _env(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_ABCDEFGHI")
    monkeypatch.setenv("DEFAULT_ORG_ID", "default-org")
    monkeypatch.setenv("STRATOCLAVE_PREFIX", "stratoclave")


class _CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


class _StashedSecret:
    """Capture what gets stashed into Secrets Manager without touching AWS."""

    last_secret_string: str | None = None
    last_secret_id: str | None = None


class _FakeSecretsClient:
    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

    def put_secret_value(self, SecretId, SecretString):
        _StashedSecret.last_secret_id = SecretId
        _StashedSecret.last_secret_string = SecretString
        return {"ARN": f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{SecretId}"}

    def create_secret(self, Name, Description, SecretString):
        _StashedSecret.last_secret_id = Name
        _StashedSecret.last_secret_string = SecretString
        return {"ARN": f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{Name}"}


def _install_boto_stub(monkeypatch):
    import boto3

    class _FakeCognito:
        def admin_create_user(self, **kw):
            return {"User": {"Attributes": [{"Name": "sub", "Value": "sub-1234"}]}}

        def admin_set_user_password(self, **kw):
            return {}

    fakes = {
        "cognito-idp": _FakeCognito(),
        "secretsmanager": _FakeSecretsClient(),
    }

    def _fake_boto_client(service, **kw):
        return fakes[service]

    monkeypatch.setattr(boto3, "client", _fake_boto_client)


def _install_repo_stubs(monkeypatch):
    from bootstrap import seed

    monkeypatch.setattr(
        seed,
        "UsersRepository",
        type("_U", (), {
            "__init__": lambda self: None,
            "scan_admins": lambda self, limit=1: [],
            "put_user": lambda self, **kw: {},
        }),
    )
    monkeypatch.setattr(
        seed,
        "UserTenantsRepository",
        type("_UT", (), {
            "__init__": lambda self: None,
            "ensure": lambda self, **kw: {},
        }),
    )


def _freeze_password(monkeypatch):
    from bootstrap import seed

    monkeypatch.setattr(seed, "_generate_temp_password", lambda length=20: SENTINEL_PASSWORD)


def test_temporary_password_never_appears_in_logger(monkeypatch):
    """Every stdlib log record produced during seed_bootstrap_admin is
    scanned and must NOT contain the sentinel password anywhere."""
    _env(monkeypatch)
    _install_boto_stub(monkeypatch)
    _install_repo_stubs(monkeypatch)
    _freeze_password(monkeypatch)

    handler = _CapturingHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)

    try:
        from bootstrap import seed

        result = seed.seed_bootstrap_admin()
    finally:
        root.removeHandler(handler)

    assert result.get("created") is True

    for rec in handler.records:
        formatted = rec.getMessage()
        assert SENTINEL_PASSWORD not in formatted, (
            f"Temp password leaked into structured log: event={rec.name} msg={formatted!r}"
        )
        for key, val in vars(rec).items():
            if isinstance(val, str):
                assert SENTINEL_PASSWORD not in val, (
                    f"Temp password leaked into logger extra {key!r} on event={rec.name}"
                )


def test_temporary_password_never_appears_on_stderr(monkeypatch):
    """Sweep-4 round-4 hardening: stderr is captured by the ECS awslogs
    driver, so it is NOT a safe sink. The password must go to Secrets
    Manager only."""
    _env(monkeypatch)
    _install_boto_stub(monkeypatch)
    _install_repo_stubs(monkeypatch)
    _freeze_password(monkeypatch)

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)

    from bootstrap import seed

    seed.seed_bootstrap_admin()
    assert SENTINEL_PASSWORD not in buf.getvalue(), (
        "Temp password must not be written to stderr — stderr is forwarded "
        "to CloudWatch Logs by the Fargate awslogs driver."
    )


def test_temporary_password_stashed_in_secrets_manager(monkeypatch):
    """Positive contract: the password IS written to Secrets Manager so
    operators can retrieve it. Without this the bootstrap flow would be
    unusable."""
    _env(monkeypatch)
    _install_boto_stub(monkeypatch)
    _install_repo_stubs(monkeypatch)
    _freeze_password(monkeypatch)

    _StashedSecret.last_secret_string = None
    _StashedSecret.last_secret_id = None

    from bootstrap import seed

    seed.seed_bootstrap_admin()
    assert _StashedSecret.last_secret_id == "stratoclave/bootstrap-admin-temp-password"
    assert _StashedSecret.last_secret_string is not None
    assert SENTINEL_PASSWORD in _StashedSecret.last_secret_string
