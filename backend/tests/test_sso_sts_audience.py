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
    return sso_sts._validate_inputs("POST", URL, headers, BODY)


@pytest.fixture(autouse=True)
def _gateway(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_API_ENDPOINT", "https://d1.cloudfront.net, https://stratoclave.internal.example.com")
    monkeypatch.delenv("STRATOCLAVE_STS_AUDIENCE_REQUIRED", raising=False)


@pytest.mark.parametrize("host", ["d1.cloudfront.net", "stratoclave.internal.example.com", "D1.CloudFront.net"])
def test_a_request_bound_to_one_of_this_gateways_hosts_passes(host):
    assert _validate(_headers(["host", "x-amz-date", "x-stratoclave-audience"], host)) is True


@pytest.mark.parametrize("value", ["vault.example.com", "evil-stratoclave.example.com", "gätéway", "", "a b"])
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
    assert e.value.status_code == 401


def test_an_unbound_request_is_accepted_until_required_then_refused(monkeypatch):
    assert _validate(_headers(["host", "x-amz-date"])) is False
    monkeypatch.setenv("STRATOCLAVE_STS_AUDIENCE_REQUIRED", "true")
    with pytest.raises(HTTPException) as e:
        _validate(_headers(["host", "x-amz-date"]))
    assert e.value.status_code == 401 and "update the CLI" in e.value.detail


def test_required_without_an_api_url_refuses_to_start(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_STS_AUDIENCE_REQUIRED", "true")
    monkeypatch.delenv("STRATOCLAVE_API_ENDPOINT")
    with pytest.raises(RuntimeError):
        sso_sts.validate_audience_config()
