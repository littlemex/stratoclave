"""F2 (CONTRACT-F2-grant.md): R13 — the comment and the client token never
reach a log, a metric, an object key, or an error body.

Two static/shape checks (no DynamoDB, no service call needed to fail today —
they inspect source directly) plus behavioural checks per sink, each driving
`mvp.grants` with a distinctive marker string standing in for the token/
comment and asserting the marker never surfaces verbatim.

design-F2.md Ambiguity note (R13): "object key" is read here as "any
DynamoDB primary key" (F2 has no S3) — the key-builder functions must never
take the token/comment as an input to a pk/sk f-string.
"""
from __future__ import annotations

import inspect
import logging

import boto3
import pytest

from tests.quota_events_fixtures import quota_events_table, seed_pool_with_grant_fields

assert quota_events_table  # imported for its pytest-fixture side effect

TENANT = "hygiene-org"
TOKEN_MARKER = "SECRET-CLIENT-TOKEN-zzz999"
COMMENT_MARKER = "SECRET-DECISION-COMMENT-zzz999"


def _table():
    return boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-quota-events")


def _actor(role="admin", user_id="admin-1"):
    from mvp.deps import AuthenticatedUser

    return AuthenticatedUser(
        user_id=user_id, email=f"{user_id}@example.com", org_id=user_id,
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

def test_r13_client_token_never_reaches_a_log_line_on_submit(dynamodb_mock, quota_events_table, caplog):
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, "2026-09", pool_limit_microusd=10**9,
        pool_granted_microusd=0, grant_cap_microusd=10**8,
    )
    caplog.set_level(logging.DEBUG)
    grants.submit_limit_raise(
        actor=_actor(role="user", user_id="u1"), tenant_id=TENANT,
        requested_amount_microusd=1_000, requested_expires_at=9_999_999_999,
        client_token=TOKEN_MARKER, justification="need more", now_epoch=1_788_307_200,
    )
    for record in caplog.records:
        assert TOKEN_MARKER not in record.getMessage()
        assert TOKEN_MARKER not in str(record.__dict__)


def test_r13_client_token_never_reaches_a_metric_line_from_the_sweeper(
    dynamodb_mock, quota_events_table, caplog,
):
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, "2026-09", pool_limit_microusd=10**9,
        pool_granted_microusd=0, grant_cap_microusd=10**8,
    )
    grants.submit_limit_raise(
        actor=_actor(role="user", user_id="u1"), tenant_id=TENANT,
        requested_amount_microusd=1_000, requested_expires_at=9_999_999_999,
        client_token=TOKEN_MARKER, justification="need more", now_epoch=1_788_307_200,
    )
    caplog.set_level(logging.INFO)
    grants.run_sweep(now_epoch=1_788_307_200)
    for record in caplog.records:
        assert TOKEN_MARKER not in record.getMessage()


def test_r13_decision_comment_never_reaches_a_log_line_on_reject(dynamodb_mock, quota_events_table, caplog):
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, "2026-09", pool_limit_microusd=10**9,
        pool_granted_microusd=0, grant_cap_microusd=10**8,
    )
    submitted = grants.submit_limit_raise(
        actor=_actor(role="user", user_id="u1"), tenant_id=TENANT,
        requested_amount_microusd=1_000, requested_expires_at=9_999_999_999,
        client_token="tok-reject-flow", justification="need more",
        now_epoch=1_788_307_200,
    )
    caplog.set_level(logging.DEBUG)
    grants.reject_limit_raise(
        actor=_actor(), request_id=submitted["request_id"],
        decision_comment=COMMENT_MARKER,
    )
    for record in caplog.records:
        assert COMMENT_MARKER not in record.getMessage()
        assert COMMENT_MARKER not in str(record.__dict__)


def test_r13_decision_comment_never_reaches_an_error_body(dynamodb_mock, quota_events_table):
    """Drive a refusal that carries a comment-shaped input (approve-for-less
    with a too-short window, so `GrantWindowTooShort` fires) and confirm the
    exception's own string form never echoes the comment back."""
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, "2026-09", pool_limit_microusd=10**9,
        pool_granted_microusd=0, grant_cap_microusd=10**8,
    )
    submitted = grants.submit_limit_raise(
        actor=_actor(role="user", user_id="u1"), tenant_id=TENANT,
        requested_amount_microusd=5_000, requested_expires_at=9_999_999_999,
        client_token="tok-error-body", justification="need more",
        now_epoch=1_788_307_200,
    )
    with pytest.raises(grants.GrantWindowTooShort) as exc:
        grants.approve_limit_raise(
            actor=_actor(), request_id=submitted["request_id"],
            approved_amount_microusd=1_000,
            expires_at=1_788_307_200 + 10,  # far short of the 300s minimum
            decision_comment=COMMENT_MARKER,
            now_epoch=1_788_307_200,
        )
    assert COMMENT_MARKER not in str(exc.value)
