"""F2 (docs/design/quota-raises.md): R13 — the comment and the client token never
reach a log, a metric, an object key, or an error body.

Two static/shape checks (no DynamoDB, no service call needed to fail today —
they inspect source directly) plus behavioural checks per sink, each driving
`mvp.grants` with a distinctive marker string standing in for the token/
comment and asserting the marker never surfaces verbatim.

docs/design/quota-raises.md Ambiguity note (R13): "object key" is read here as "any
DynamoDB primary key" (F2 has no S3) — the key-builder functions must never
take the token/comment as an input to a pk/sk f-string.
"""
from __future__ import annotations

import inspect
import logging

import boto3
import pytest

from tests.quota_events_fixtures import (
    freeze_grants_clock,
    quota_events_table,
    seed_pool_with_grant_fields,
    seed_tenant,
)

assert quota_events_table  # imported for its pytest-fixture side effect

TENANT = "hygiene-org"
TOKEN_MARKER = "SECRET-CLIENT-TOKEN-zzz999"
COMMENT_MARKER = "SECRET-DECISION-COMMENT-zzz999"


def _table():
    return boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-quota-events")


_SDK_LOGGER_PREFIXES = ("boto3", "botocore", "moto", "urllib3", "s3transfer")


def _app_log_records(caplog):
    """`caplog` at DEBUG/INFO captures every stdlib `logging` record, and that
    includes boto3/botocore/moto's own request/response tracing -- which
    necessarily and correctly echoes every field of a PutItem/UpdateItem
    call, client_token and decision_comment included, because that IS the
    write that stores them. R13 governs THIS application's log lines
    (`stratoclave.audit`, emitted by `mvp/authz.py::log_audit_event`), not
    the AWS SDK's own wire-level debug trace of a write the feature is
    SUPPOSED to make. Filtering by logger name is what makes this test
    check the right layer instead of failing on a false positive the moment
    DEBUG is enabled against the real implementation."""
    return [r for r in caplog.records if not r.name.startswith(_SDK_LOGGER_PREFIXES)]


def _actor(role="admin", user_id="admin-1", org_id=TENANT):
    from mvp.deps import AuthenticatedUser

    # `org_id` defaults to `TENANT`, not `user_id`: `submit_limit_raise`
    # derives the request's tenant from `actor.org_id` (there is no
    # `tenant_id=` parameter -- it is the caller's OWN tenant, read from the
    # session, never a body field), so the requester and the pool seeded at
    # `TENANT` must actually agree, or the request lands under a tenant
    # nothing else in the test seeded.
    return AuthenticatedUser(
        user_id=user_id, email=f"{user_id}@example.com", org_id=org_id,
        roles=[role], raw_claims={}, auth_kind="cognito",
    )


# ---------------------------------------------------------------------------
# Static / shape checks — do not require any moto state
# ---------------------------------------------------------------------------

def test_r13_slot_key_builder_never_takes_client_token_or_comment():
    from dynamo.quota_events import QuotaEventsRepository

    params = set(inspect.signature(QuotaEventsRepository.slot_key).parameters)
    assert "client_token" not in params
    assert "decision_comment" not in params


def test_r13_request_key_builder_never_takes_client_token_or_comment():
    from dynamo.quota_events import QuotaEventsRepository

    params = set(inspect.signature(QuotaEventsRepository.request_key).parameters)
    assert "client_token" not in params
    assert "decision_comment" not in params


def test_r13_grant_key_builder_never_takes_client_token_or_comment():
    from dynamo.quota_events import QuotaEventsRepository

    params = set(inspect.signature(QuotaEventsRepository.grant_key).parameters)
    assert "client_token" not in params
    assert "decision_comment" not in params


# ---------------------------------------------------------------------------
# Behavioural checks — drive the real functions with a marker value
# ---------------------------------------------------------------------------

def test_r13_client_token_never_reaches_a_log_line_on_submit(
    dynamodb_mock, quota_events_table, caplog, monkeypatch,
):
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, "2026-09", pool_limit_microusd=10**9,
        pool_granted_microusd=0, grant_cap_microusd=10**8,
    )
    caplog.set_level(logging.DEBUG)
    freeze_grants_clock(monkeypatch, 1_788_307_200)
    grants.submit_limit_raise(
        actor=_actor(role="user", user_id="u1"),
        asked_amount_microusd=1_000, reason_code="usage_spike",
        client_token=TOKEN_MARKER, comment="need more",
    )
    for record in _app_log_records(caplog):
        assert TOKEN_MARKER not in record.getMessage()
        assert TOKEN_MARKER not in str(record.__dict__)


def test_r13_client_token_never_reaches_a_metric_line_from_the_sweeper(
    dynamodb_mock, quota_events_table, capsys, monkeypatch,
):
    """The sweeper's metrics are EMF lines `print()`ed to stdout
    (`mvp/grants.py::_emit_sweep_metrics`), not routed through the stdlib
    `logging` module at all -- `caplog` cannot see them, so asserting
    against it here would pass vacuously (never having looked at the real
    sink) rather than actually proving the marker absent. `capsys` reads the
    real channel CloudWatch's log agent parses the EMF block from."""
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, "2026-09", pool_limit_microusd=10**9,
        pool_granted_microusd=0, grant_cap_microusd=10**8,
    )
    freeze_grants_clock(monkeypatch, 1_788_307_200)
    grants.submit_limit_raise(
        actor=_actor(role="user", user_id="u1"),
        asked_amount_microusd=1_000, reason_code="usage_spike",
        client_token=TOKEN_MARKER, comment="need more",
    )
    capsys.readouterr()  # discard anything printed by setup above
    grants.sweep_expired_grants(now_epoch=1_788_307_200)
    out = capsys.readouterr().out
    assert TOKEN_MARKER not in out


def test_r13_decision_comment_never_reaches_a_log_line_on_reject(
    dynamodb_mock, quota_events_table, caplog, monkeypatch,
):
    from mvp import grants

    seed_tenant(TENANT)  # reject_limit_raise's authority ConditionCheck reads this row
    seed_pool_with_grant_fields(
        TENANT, "2026-09", pool_limit_microusd=10**9,
        pool_granted_microusd=0, grant_cap_microusd=10**8,
    )
    freeze_grants_clock(monkeypatch, 1_788_307_200)
    submitted = grants.submit_limit_raise(
        actor=_actor(role="user", user_id="u1"),
        asked_amount_microusd=1_000, reason_code="usage_spike",
        client_token="tok-reject-flow", comment="need more",
    )
    caplog.set_level(logging.DEBUG)
    grants.reject_limit_raise(
        actor=_actor(), request_id=submitted["request_id"],
        decision_comment=COMMENT_MARKER,
    )
    for record in _app_log_records(caplog):
        assert COMMENT_MARKER not in record.getMessage()
        assert COMMENT_MARKER not in str(record.__dict__)


def test_r13_decision_comment_never_reaches_an_error_body(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    """Drive a refusal that carries a comment-shaped input (approve-for-less
    with a too-short window, so `GrantWindowTooShort` fires) and confirm the
    exception's own string form never echoes the comment back."""
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, "2026-09", pool_limit_microusd=10**9,
        pool_granted_microusd=0, grant_cap_microusd=10**8,
    )
    freeze_grants_clock(monkeypatch, 1_788_307_200)
    submitted = grants.submit_limit_raise(
        actor=_actor(role="user", user_id="u1"),
        asked_amount_microusd=5_000, reason_code="usage_spike",
        client_token="tok-error-body", comment="need more",
    )
    with pytest.raises(grants.GrantWindowTooShort) as exc:
        grants.approve_limit_raise(
            actor=_actor(), request_id=submitted["request_id"],
            approved_amount_microusd=1_000,
            expires_at=1_788_307_200 + 10,  # far short of the 300s minimum
            decision_comment=COMMENT_MARKER,
        )
    assert COMMENT_MARKER not in str(exc.value)
