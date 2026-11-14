"""The audience header that binds a signed GetCallerIdentity to this gateway's own
host (`mvp.sso_sts.AUDIENCE_HEADER`), through the real input validation."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from mvp import sso_sts

URL = "https://sts.us-east-1.amazonaws.com/"
BODY = "Action=GetCallerIdentity&Version=2011-06-15"


def _headers(signed, audience=None, authorization=None):
    h = {"x-amz-date": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
         "authorization": authorization or (
             "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261114/us-east-1/sts/aws4_request, "
             f"SignedHeaders={';'.join(signed)}, Signature=abc")}
    if audience is not None:
        h["x-stratoclave-audience"] = audience
    return h


def _validate(headers):
    sso_sts._validate_inputs("POST", URL, headers, BODY)
    return sso_sts._check_audience(headers)


@pytest.fixture(autouse=True)
def _gateway(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_API_ENDPOINT", "https://d1.cloudfront.net")
    monkeypatch.setenv("STRATOCLAVE_STS_AUDIENCES", "https://stratoclave.internal.example.com")
    monkeypatch.delenv("STRATOCLAVE_STS_AUDIENCE_REQUIRED", raising=False)


@pytest.mark.parametrize("host", ["d1.cloudfront.net", "stratoclave.internal.example.com", "D1.CloudFront.net"])
def test_a_request_bound_to_one_of_this_gateways_hosts_passes(host):
    assert _validate(_headers(["host", "x-amz-date", "x-stratoclave-audience"], host)) is True


@pytest.mark.parametrize("value", ["vault.example.com", "evil-stratoclave.example.com", "gätéway", "a b"])
def test_a_request_bound_to_anything_else_is_refused(value):
    with pytest.raises(HTTPException) as e:
        _validate(_headers(["host", "x-stratoclave-audience"], value))
    assert e.value.status_code == 401


def test_an_unsigned_audience_is_refused():
    with pytest.raises(HTTPException) as e:
        _validate(_headers(["host", "x-amz-date"], "d1.cloudfront.net"))
    assert e.value.status_code == 401 and "signed header" in e.value.detail


def test_a_signed_audience_that_was_stripped_is_refused():
    with pytest.raises(HTTPException) as e:
        _validate(_headers(["host", "x-stratoclave-audience"]))
    assert e.value.status_code == 401 and "signed but missing" in e.value.detail


def test_a_decoy_signed_headers_parameter_is_refused():
    auth = ("AWS4-HMAC-SHA256 x=SignedHeaders=x-stratoclave-audience, Credential=AKID/x, "
            "SignedHeaders=host;x-amz-date, Signature=abc")
    with pytest.raises(HTTPException) as e:
        _validate(_headers([], "d1.cloudfront.net", authorization=auth))
    assert e.value.status_code == 400 and "malformed" in e.value.detail


def test_an_unbound_request_is_accepted_until_required_then_refused(monkeypatch):
    assert _validate(_headers(["host", "x-amz-date"])) is False
    monkeypatch.setenv("STRATOCLAVE_STS_AUDIENCE_REQUIRED", "true")
    with pytest.raises(HTTPException) as e:
        _validate(_headers(["host", "x-amz-date"]))
    assert e.value.status_code == 401 and "update the CLI" in e.value.detail


def test_required_without_an_api_url_refuses_to_start(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_STS_AUDIENCE_REQUIRED", "true")
    monkeypatch.delenv("STRATOCLAVE_API_ENDPOINT")
    monkeypatch.delenv("STRATOCLAVE_STS_AUDIENCES")
    with pytest.raises(RuntimeError):
        sso_sts.validate_audience_config()


@pytest.mark.parametrize("endpoint, audience", [
    ("https://gw.example.com:443", "gw.example.com"),
    ("https://GW.example.com/", "gw.example.com"),
    ("http://127.0.0.1:8080", "127.0.0.1:8080"),
])
def test_the_gateway_normalises_its_host_as_the_cli_does(monkeypatch, endpoint, audience):
    monkeypatch.delenv("STRATOCLAVE_STS_AUDIENCES")
    monkeypatch.setenv("STRATOCLAVE_API_ENDPOINT", endpoint)
    assert sso_sts.expected_audiences() == {audience}


def test_extra_audiences_are_configured_separately(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_API_ENDPOINT", "https://d1.cloudfront.net")
    monkeypatch.setenv("STRATOCLAVE_STS_AUDIENCES", "https://private.example.com")
    assert sso_sts.expected_audiences() == {"d1.cloudfront.net", "private.example.com"}
    monkeypatch.setenv("STRATOCLAVE_STS_AUDIENCES", "private.example.com")
    with pytest.raises(RuntimeError, match="not http"):
        sso_sts.validate_audience_config()


@pytest.mark.parametrize("auth", [
    "AWS4-HMAC-SHA256 Credential=A/x, SignedHeaders=host, SignedHeaders=host;x-stratoclave-audience, Signature=a",
    "AWS4-HMAC-SHA256 Credential=A/x, SignedHeaders=host;x-stratoclave-audience",
])
def test_duplicate_or_missing_sigv4_parameters_are_malformed(auth):
    with pytest.raises(HTTPException) as e:
        sso_sts._check_audience(_headers([], "d1.cloudfront.net", authorization=auth))
    assert e.value.status_code == 400 and "malformed" in e.value.detail


def test_header_names_repeating_with_different_case_are_refused():
    h = _headers(["host", "x-stratoclave-audience"], "d1.cloudfront.net")
    h["X-Stratoclave-Audience"] = "evil.example.com"
    with pytest.raises(HTTPException) as e:
        sso_sts._check_audience(h)
    assert e.value.status_code == 400


def test_every_verified_exchange_is_logged_with_whether_it_was_bound(monkeypatch, caplog):
    """Through verify_and_call_sts, with the STS call and the replay store faked."""
    import logging

    class _Resp:
        status_code = 200
        text = ('<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
                "<GetCallerIdentityResult><Arn>arn:aws:sts::123456789012:"
                "assumed-role/r/s</Arn><UserId>AROAEXAMPLE:s</UserId><Account>123456789012</Account>"
                "</GetCallerIdentityResult></GetCallerIdentityResponse>")

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, **k): return _Resp()

    monkeypatch.setattr(sso_sts.httpx, "Client", _Client)
    import dynamo.sso_nonces as nonces
    monkeypatch.setattr(nonces.SsoNoncesRepository, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(nonces.SsoNoncesRepository, "consume", lambda self, **k: None)
    caplog.set_level(logging.INFO, logger="mvp.sso_sts")
    for signed, aud, bound in ((["host", "x-stratoclave-audience"], "d1.cloudfront.net", True),
                               (["host"], None, False)):
        caplog.clear()
        sso_sts.verify_and_call_sts("POST", URL, _headers(signed, aud), BODY)
        rec = [r for r in caplog.records if r.getMessage() == "sso_sts_exchange_verified"]
        assert rec and rec[0].audience_bound is bound
