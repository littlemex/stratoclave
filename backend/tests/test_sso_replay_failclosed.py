"""Regression guard for sweep-4 C-Critical: SSO replay-nonce consumption
must fail CLOSED.

Before sweep-2 X-3, verify_and_call_sts wrapped
`SsoNoncesRepository().consume(...)` in `except Exception: log.warning`,
which silently degraded to "no replay protection" when the nonces table
was absent, mis-permissioned, throttled, or any other transient
DynamoDB error occurred. That is fail-OPEN: an attacker who triggers
the failure condition bypasses replay-protection for the whole fleet.

Sweep-4 restores the correct behaviour:
  * NonceReplayError → 401 (the detection path)
  * any OTHER exception → 401 (fail-closed)

This test exists purely to make the next server-side squash LOUDLY
break CI if it ever drops the fail-closed branch again.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException


def _headers(auth: str = "AWS4-HMAC-SHA256 Credential=AKIAxxxx/..."):
    return {
        "Authorization": auth,
        "X-Amz-Date": "20260430T000000Z",
        "Host": "sts.amazonaws.com",
    }


def _freeze_time(monkeypatch, iso="2026-04-30T00:00:05+00:00"):
    # Keep the skew check from rejecting our fixed-date fixture.
    import datetime as dt

    class _FixedDt(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime.fromisoformat(iso).replace(tzinfo=tz or dt.timezone.utc)

    from mvp import sso_sts

    monkeypatch.setattr(sso_sts, "datetime", _FixedDt)


def test_replay_detected_raises_401(monkeypatch):
    """NonceReplayError must become a 401 — the replay-detection path."""
    from mvp import sso_sts

    _freeze_time(monkeypatch)

    class _ReplayingRepo:
        def consume(self, **_):
            from dynamo.sso_nonces import NonceReplayError

            raise NonceReplayError("already seen")

    # Replace the repository at the imported name
    import dynamo.sso_nonces as nonce_mod

    monkeypatch.setattr(nonce_mod, "SsoNoncesRepository", _ReplayingRepo)

    with pytest.raises(HTTPException) as exc:
        sso_sts.verify_and_call_sts(
            method="POST",
            url="https://sts.amazonaws.com/",
            headers=_headers(),
            body="Action=GetCallerIdentity&Version=2011-06-15",
        )
    assert exc.value.status_code == 401
    assert "replay" in str(exc.value.detail).lower()


def test_nonce_storage_unavailable_fails_closed(monkeypatch):
    """Any non-NonceReplayError from the nonces repo MUST be fail-closed:
    the request is refused rather than silently bypassing replay defence."""
    from mvp import sso_sts

    _freeze_time(monkeypatch)

    class _BrokenRepo:
        def consume(self, **_):
            raise RuntimeError("dynamodb unavailable")

    import dynamo.sso_nonces as nonce_mod

    monkeypatch.setattr(nonce_mod, "SsoNoncesRepository", _BrokenRepo)

    # If fail-closed: we raise HTTPException (401/503). If fail-OPEN: we
    # fall through and try to talk to real STS (httpx), which will time
    # out / DNS-fail — either way we MUST NOT reach the STS call. We
    # assert that the response is an HTTPException, not a downstream
    # httpx error bubbling up through the generic except at the STS
    # layer.
    with pytest.raises(HTTPException) as exc:
        sso_sts.verify_and_call_sts(
            method="POST",
            url="https://sts.amazonaws.com/",
            headers=_headers(),
            body="Action=GetCallerIdentity&Version=2011-06-15",
        )
    # Accept either 401 (auth refused) or 503 (dependency unavailable).
    # Both are fail-closed; the original bug turned this into 200 + STS
    # reply because we swallowed the exception.
    assert exc.value.status_code in (401, 503)


def test_happy_path_consumes_and_proceeds(monkeypatch):
    """When the nonce is new and storage is healthy, we must reach the
    STS round trip. We stub httpx.Client so no network hit happens."""
    from mvp import sso_sts

    _freeze_time(monkeypatch)

    class _OkRepo:
        def consume(self, **_):
            return None

    import dynamo.sso_nonces as nonce_mod

    monkeypatch.setattr(nonce_mod, "SsoNoncesRepository", _OkRepo)

    sample_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
        '<GetCallerIdentityResult>'
        '<Arn>arn:aws:iam::123456789012:user/alice</Arn>'
        '<UserId>ROLLE-EXAMPLE-USER-ID-001</UserId>'
        '<Account>123456789012</Account>'
        '</GetCallerIdentityResult>'
        '</GetCallerIdentityResponse>'
    )

    class _FakeResp:
        status_code = 200
        text = sample_xml

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def request(self, **_):
            return _FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    ident = sso_sts.verify_and_call_sts(
        method="POST",
        url="https://sts.amazonaws.com/",
        headers=_headers(),
        body="Action=GetCallerIdentity&Version=2011-06-15",
    )
    assert ident.account_id == "123456789012"
    assert ident.identity_type == "iam_user"
