"""Tests for the P1-A time-bounded admin bootstrap gate.

`authz.admin_creation_allowed()` governs whether
`POST /api/mvp/admin/users` is allowed to create an account whose
role is `admin`. Before P1-A the rule was simply "`ALLOW_ADMIN_CREATION=true`
anywhere", which was a permanent exposure in production once the
flag slipped. The new rule is:

  * development / staging  — `ALLOW_ADMIN_CREATION=true` is still
    sufficient (unchanged), so local smoke tests don't sprout a new
    ceremony.
  * production — `ALLOW_ADMIN_CREATION=true` AND
    `ALLOW_ADMIN_CREATION_UNTIL=<future epoch seconds>` are BOTH
    required. The gate auto-closes the instant `now > epoch`, so even
    an operator who forgets to unset the boolean stops being a
    permanent footgun.

These tests exercise each branch in isolation with monkeypatched
environment variables; they do not hit FastAPI / DynamoDB. The
HTTP-level assertion (handler returns 403) is covered alongside the
existing admin-users integration tests.
"""
from __future__ import annotations

import logging
import time

import pytest

from mvp.authz import (
    admin_creation_allowed,
    warn_if_admin_creation_enabled_in_production,
)


def _set_env(monkeypatch, **values: str | None) -> None:
    """Apply a batch of env vars; None means "delete if set"."""
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


class TestDevelopmentDefaultPath:
    """Development keeps the classic sticky-flag UX."""

    def test_flag_false_default_denied(self, monkeypatch):
        _set_env(
            monkeypatch,
            ENVIRONMENT="development",
            ALLOW_ADMIN_CREATION=None,
            ALLOW_ADMIN_CREATION_UNTIL=None,
        )
        assert admin_creation_allowed() is False

    def test_flag_true_allowed_without_expiry(self, monkeypatch):
        _set_env(
            monkeypatch,
            ENVIRONMENT="development",
            ALLOW_ADMIN_CREATION="true",
            ALLOW_ADMIN_CREATION_UNTIL=None,
        )
        assert admin_creation_allowed() is True

    def test_flag_false_wins_over_expiry(self, monkeypatch):
        _set_env(
            monkeypatch,
            ENVIRONMENT="development",
            ALLOW_ADMIN_CREATION="false",
            ALLOW_ADMIN_CREATION_UNTIL=str(int(time.time()) + 3600),
        )
        assert admin_creation_allowed() is False


class TestProductionRequiresBothFlags:
    """Production mandates a time-bounded window."""

    def test_flag_alone_rejected(self, monkeypatch):
        _set_env(
            monkeypatch,
            ENVIRONMENT="production",
            ALLOW_ADMIN_CREATION="true",
            ALLOW_ADMIN_CREATION_UNTIL=None,
        )
        assert admin_creation_allowed() is False

    def test_flag_plus_future_epoch_allowed(self, monkeypatch):
        _set_env(
            monkeypatch,
            ENVIRONMENT="production",
            ALLOW_ADMIN_CREATION="true",
            ALLOW_ADMIN_CREATION_UNTIL=str(int(time.time()) + 3600),
        )
        assert admin_creation_allowed() is True

    def test_flag_plus_past_epoch_denied(self, monkeypatch):
        _set_env(
            monkeypatch,
            ENVIRONMENT="production",
            ALLOW_ADMIN_CREATION="true",
            ALLOW_ADMIN_CREATION_UNTIL=str(int(time.time()) - 60),
        )
        assert admin_creation_allowed() is False

    def test_flag_plus_zero_epoch_denied(self, monkeypatch):
        _set_env(
            monkeypatch,
            ENVIRONMENT="production",
            ALLOW_ADMIN_CREATION="true",
            ALLOW_ADMIN_CREATION_UNTIL="0",
        )
        assert admin_creation_allowed() is False

    def test_flag_plus_malformed_epoch_denied(self, monkeypatch):
        """A typo in the epoch must fail closed, not silently open."""
        _set_env(
            monkeypatch,
            ENVIRONMENT="production",
            ALLOW_ADMIN_CREATION="true",
            ALLOW_ADMIN_CREATION_UNTIL="soon-please",
        )
        assert admin_creation_allowed() is False

    def test_epoch_alone_rejected(self, monkeypatch):
        """`ALLOW_ADMIN_CREATION_UNTIL` without the boolean flag is ignored."""
        _set_env(
            monkeypatch,
            ENVIRONMENT="production",
            ALLOW_ADMIN_CREATION=None,
            ALLOW_ADMIN_CREATION_UNTIL=str(int(time.time()) + 3600),
        )
        assert admin_creation_allowed() is False


class TestEnvironmentDefault:
    """Unset ENVIRONMENT defaults to production (fail-safe)."""

    def test_unset_environment_behaves_as_production(self, monkeypatch):
        _set_env(
            monkeypatch,
            ENVIRONMENT=None,
            ALLOW_ADMIN_CREATION="true",
            ALLOW_ADMIN_CREATION_UNTIL=None,
        )
        # production path requires the UNTIL epoch → denied.
        assert admin_creation_allowed() is False


class TestWarnOnOpenGate:
    """`warn_if_admin_creation_enabled_in_production` should log a
    structured warning *only* when the gate is actively open in prod."""

    def test_warn_fires_with_expected_extras(self, monkeypatch, caplog):
        future = int(time.time()) + 600
        _set_env(
            monkeypatch,
            ENVIRONMENT="production",
            ALLOW_ADMIN_CREATION="true",
            ALLOW_ADMIN_CREATION_UNTIL=str(future),
        )
        logger = logging.getLogger("stratoclave.test.admin_creation_gate")
        caplog.set_level(logging.WARNING, logger=logger.name)
        warn_if_admin_creation_enabled_in_production(logger)
        records = [r for r in caplog.records if r.name == logger.name]
        assert records, "expected a warning record"
        record = records[-1]
        assert record.levelno == logging.WARNING
        assert record.message == "allow_admin_creation_enabled_in_production"
        assert record.__dict__.get("expires_at") == future
        assert record.__dict__.get("environment") == "production"

    def test_no_warn_in_development_even_with_flag(self, monkeypatch, caplog):
        _set_env(
            monkeypatch,
            ENVIRONMENT="development",
            ALLOW_ADMIN_CREATION="true",
            ALLOW_ADMIN_CREATION_UNTIL=None,
        )
        logger = logging.getLogger("stratoclave.test.admin_creation_gate")
        caplog.set_level(logging.WARNING, logger=logger.name)
        warn_if_admin_creation_enabled_in_production(logger)
        assert not [r for r in caplog.records if r.name == logger.name]

    def test_no_warn_in_production_when_gate_closed(self, monkeypatch, caplog):
        _set_env(
            monkeypatch,
            ENVIRONMENT="production",
            ALLOW_ADMIN_CREATION="false",
            ALLOW_ADMIN_CREATION_UNTIL=None,
        )
        logger = logging.getLogger("stratoclave.test.admin_creation_gate")
        caplog.set_level(logging.WARNING, logger=logger.name)
        warn_if_admin_creation_enabled_in_production(logger)
        assert not [r for r in caplog.records if r.name == logger.name]


@pytest.mark.parametrize(
    "env_value,flag,until_offset,expected",
    [
        ("production", "true", 60, True),
        ("production", "true", -60, False),
        ("production", "false", 60, False),
        ("development", "true", None, True),
        ("staging", "true", None, True),  # non-production keeps sticky flag
    ],
)
def test_matrix(monkeypatch, env_value, flag, until_offset, expected):
    _set_env(
        monkeypatch,
        ENVIRONMENT=env_value,
        ALLOW_ADMIN_CREATION=flag,
        ALLOW_ADMIN_CREATION_UNTIL=(
            str(int(time.time()) + until_offset) if until_offset is not None else None
        ),
    )
    assert admin_creation_allowed() is expected
