"""The server id that binds a signed GetCallerIdentity to this gateway
(`mvp.sso_sts.SERVER_ID_HEADER`), so a request a user signed for another service
using the same pattern cannot be replayed here."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from mvp import sso_sts


def _auth(signed):
    return ("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261114/us-east-1/sts/aws4_request, "
            f"SignedHeaders={';'.join(signed)}, Signature=abc")


@pytest.fixture(autouse=True)
def _server(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_STS_SERVER_ID", "gateway-1")
    monkeypatch.delenv("STRATOCLAVE_STS_SERVER_ID_REQUIRED", raising=False)


def test_the_matching_signed_server_id_passes():
    sso_sts._check_server_id({"authorization": _auth(["host", "x-amz-date", "x-stratoclave-server-id"]),
                              "x-stratoclave-server-id": "gateway-1"})


def test_a_request_signed_for_another_server_is_refused():
    with pytest.raises(HTTPException) as e:
        sso_sts._check_server_id({"authorization": _auth(["host", "x-stratoclave-server-id"]),
                                  "x-stratoclave-server-id": "vault-prod"})
    assert e.value.status_code == 401


def test_an_unsigned_server_id_is_refused():
    """Present but outside SignedHeaders: anyone could have added it after signing."""
    with pytest.raises(HTTPException):
        sso_sts._check_server_id({"authorization": _auth(["host", "x-amz-date"]),
                                  "x-stratoclave-server-id": "gateway-1"})


def test_absent_is_accepted_until_required_then_refused(monkeypatch):
    sso_sts._check_server_id({"authorization": _auth(["host"])})
    monkeypatch.setenv("STRATOCLAVE_STS_SERVER_ID_REQUIRED", "true")
    with pytest.raises(HTTPException) as e:
        sso_sts._check_server_id({"authorization": _auth(["host"])})
    assert "update the CLI" in e.value.detail


def test_the_server_id_defaults_to_the_cognito_client_id(monkeypatch):
    monkeypatch.delenv("STRATOCLAVE_STS_SERVER_ID", raising=False)
    monkeypatch.setenv("COGNITO_CLIENT_ID", "client-abc")
    assert sso_sts.expected_server_id() == "client-abc"


def test_it_is_checked_on_the_validation_path():
    """`_validate_inputs` runs it, so the replay guard and STS call never see an
    unbound request."""
    import inspect

    assert "_check_server_id(lowered)" in inspect.getsource(sso_sts._validate_inputs)
