"""The audience header that binds a signed GetCallerIdentity to this gateway's own
host (`mvp.sso_sts.AUDIENCE_HEADER`), through the real input validation."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import structlog
from fastapi import HTTPException

from core.logging import get_logger, setup_logging
from mvp import sso_sts

URL = "https://sts.us-east-1.amazonaws.com/"
BODY = "Action=GetCallerIdentity&Version=2011-06-15"


@pytest.fixture
def production_log_lines(caplog, monkeypatch):
    """Configure the REAL production formatter (`core.logging.setup_logging`'s
    JSON branch, the same one a deployed gateway runs) and return a function
    that parses every captured record's MESSAGE as JSON.

    This is the one case where reading a `LogRecord`'s message (not its
    `extra`-derived attributes) is the right tool: structlog's production
    pipeline renders the event dict to a JSON string via `JSONRenderer`
    FIRST, then calls the underlying stdlib logger with that string as the
    sole positional argument -- so `record.getMessage()` IS the line
    production's `"%(message)s"` handler writes to stdout, byte for byte.
    Reading a record attribute instead (the previous version of this file's
    tests did) would have passed even if the production formatter dropped
    the field entirely, which it did: `sso_sts.py` used to log through a
    bare `logging.getLogger(...).info(msg, extra={...})`, whose `extra` dict
    the production handler's `"%(message)s"` format never reads at all.

    structlog's configuration is process-global, so whatever was configured
    before this fixture ran is saved and restored afterwards -- not reset to
    structlog's own library defaults. Resetting to the library default
    instead of restoring the PRIOR config would leave any logger that had not
    yet been used by the time this test ran to resolve, on its own next use,
    against neither this app's "development" config nor its "production" one
    but structlog's bare library default, which `structlog.testing
    .capture_logs()` (used by other tests in this suite) does not reliably
    intercept -- exactly the kind of cross-test pollution a shared, global,
    cached configuration makes easy to introduce without this test file
    appearing to do anything wrong on its own.
    """
    import logging as _logging

    saved_config = structlog.get_config()
    setup_logging(environment="production")
    # Proven BEFORE `caplog.set_level` below gets a chance to force it: if
    # `setup_logging`'s production branch regressed to a level above INFO
    # (silencing `sso_sts_exchange_verified`/`sso_sts_audience_refused`
    # entirely in a real deployment), this fails here. `caplog.set_level`
    # right after is pytest's own capture-harness requirement -- it widens
    # what THIS TEST'S handler receives, which is unrelated to, and must not
    # be mistaken for, what the real production config actually enables.
    assert _logging.getLogger("mvp.sso_sts").isEnabledFor(_logging.INFO), (
        "setup_logging(environment='production') does not enable INFO for "
        "mvp.sso_sts -- the audience-binding rollout signal would be silently "
        "dropped in a real deployment"
    )
    caplog.set_level(_logging.INFO, logger="mvp.sso_sts")
    # `sso_sts._log` is a MODULE-LEVEL singleton, created once at import time.
    # structlog's `cache_logger_on_first_use=True` bakes in whatever
    # processor chain was active at THIS object's first-ever call and never
    # re-resolves it, so a different test that already triggered it earlier
    # in this same process (under a different `setup_logging` call, or
    # structlog's unconfigured default) would otherwise make this fixture's
    # `setup_logging(production)` above a no-op for this specific logger.
    # Swapping in a brand-new `get_logger(...)` call here sidesteps that: it
    # has never been used, so its first call resolves against the config
    # this fixture just set. In the real app this is a non-issue --
    # `main.py` calls `setup_logging` before any `mvp.*` module (and so
    # `sso_sts._log`) is even imported.
    monkeypatch.setattr(sso_sts, "_log", get_logger("mvp.sso_sts"))
    try:
        def _lines():
            lines = [json.loads(r.getMessage()) for r in caplog.records]
            caplog.clear()
            return lines
        yield _lines
    finally:
        structlog.configure(**saved_config)


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


@pytest.mark.parametrize("query", [
    "X-Amz-Algorithm=AWS4-HMAC-SHA256",
    "X-Amz-Credential=AKID%2F20261114%2Fus-east-1%2Fsts%2Faws4_request",
    "X-Amz-SignedHeaders=host",
    "X-Amz-Signature=deadbeef",
    "X-Amz-Security-Token=abc",
    "x-amz-date=20261114T000000Z",  # case-insensitive
])
def test_a_query_string_sigv4_parameter_is_refused(query):
    url = f"{URL}?Action=GetCallerIdentity&Version=2011-06-15&{query}"
    with pytest.raises(HTTPException) as e:
        sso_sts._validate_inputs("POST", url, _headers(["host"]), BODY)
    assert e.value.status_code == 400 and "query string" in e.value.detail


def test_a_plain_query_string_action_is_still_accepted():
    url = f"{URL}?Action=GetCallerIdentity&Version=2011-06-15"
    sso_sts._validate_inputs("POST", url, _headers(["host"]), "")  # must not raise


def test_a_refusal_is_logged_with_the_presented_host_and_reason(production_log_lines):
    with pytest.raises(HTTPException):
        sso_sts._check_audience(_headers(["host", "x-stratoclave-audience"], "evil.example.com"))
    lines = [line for line in production_log_lines() if line.get("event") == "sso_sts_audience_refused"]
    assert lines, "no sso_sts_audience_refused line reached stdout through the real formatter"
    assert lines[0]["presented_host"] == "evil.example.com"
    assert "another host" in lines[0]["reason"]


def test_access_key_id_of_reads_the_credential_scope_string():
    h = _headers(["host", "x-amz-date"])  # authorization set by the default
    assert sso_sts.access_key_id_of(h) == "AKIDEXAMPLE"


def test_access_key_id_of_is_case_insensitive_on_the_header_name():
    h = _headers(["host"])
    h["Authorization"] = h.pop("authorization")
    assert sso_sts.access_key_id_of(h) == "AKIDEXAMPLE"


@pytest.mark.parametrize("authorization", [
    "not sigv4 at all",
    "AWS4-HMAC-SHA256 Credential=AKID/x",  # missing SignedHeaders/Signature
])
def test_access_key_id_of_returns_none_for_a_malformed_header(authorization):
    assert sso_sts.access_key_id_of({"authorization": authorization}) is None


def test_access_key_id_of_returns_none_with_no_authorization_header_at_all():
    assert sso_sts.access_key_id_of({}) is None


def test_an_unrecognised_audience_required_value_fails_startup(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_STS_AUDIENCE_REQUIRED", "enabled")
    with pytest.raises(RuntimeError, match="STRATOCLAVE_STS_AUDIENCE_REQUIRED"):
        sso_sts.audience_required()


def test_validate_audience_config_itself_fails_on_the_same_typo(monkeypatch):
    """The test above calls `audience_required()` directly; this one calls
    the actual startup entry point `main.py`'s lifespan invokes
    (`validate_audience_config`), so a future change that stopped that
    function from calling `audience_required()` at all -- leaving a typo to
    surface only as a 401/500 on the first real sign-in instead of at
    startup -- would be caught here."""
    monkeypatch.delenv("STRATOCLAVE_STS_AUDIENCES", raising=False)
    monkeypatch.setenv("STRATOCLAVE_STS_AUDIENCE_REQUIRED", "enabled")
    with pytest.raises(RuntimeError, match="STRATOCLAVE_STS_AUDIENCE_REQUIRED"):
        sso_sts.validate_audience_config()


@pytest.mark.parametrize("authorization", [
    "AWS4-HMAC-SHA256 Credential=/20261114/us-east-1/sts/aws4_request, SignedHeaders=host;x-amz-date, Signature=abc",
])
def test_an_empty_access_key_id_in_credential_is_refused_at_input_validation(authorization):
    """`Credential=/...` parses as well-formed SigV4 (every parameter name
    present, no duplicates) but names no access key id at all -- refused
    here rather than forwarded to STS or silently skipped by whatever reads
    the access key id out of it."""
    headers = {
        "authorization": authorization,
        "x-amz-date": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    }
    with pytest.raises(HTTPException) as exc:
        sso_sts._validate_inputs("POST", URL, headers, BODY)
    assert exc.value.status_code == 400
    assert "access key id" in exc.value.detail


def test_a_credential_with_a_real_access_key_id_still_passes_input_validation():
    h = _headers(["host"])  # the default Authorization names "AKIDEXAMPLE"
    sso_sts._validate_inputs("POST", URL, h, BODY)  # must not raise


def test_a_malformed_authorization_does_not_trip_the_empty_access_key_id_check():
    """A header that is not SigV4-shaped at all must fall through to the
    general malformed-header path (checked elsewhere, e.g. `_check_audience`)
    rather than being misread as "empty access key id"."""
    headers = {
        "authorization": "not sigv4 at all",
        "x-amz-date": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    }
    sso_sts._validate_inputs("POST", URL, headers, BODY)  # must not raise HERE


def test_every_verified_exchange_is_logged_with_whether_it_was_bound(monkeypatch, production_log_lines):
    """Through verify_and_call_sts, with the STS call and the replay store faked, and
    through the real production JSON formatter -- `audience_bound` must be a real field
    in the emitted line, not only an attribute on a `LogRecord` an operator's actual
    CloudWatch Logs Insights query (docs/DEPLOYMENT.md) could never read.
    """

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
    for signed, aud, bound in ((["host", "x-stratoclave-audience"], "d1.cloudfront.net", True),
                               (["host"], None, False)):
        sso_sts.verify_and_call_sts("POST", URL, _headers(signed, aud), BODY)
        lines = [line for line in production_log_lines() if line.get("event") == "sso_sts_exchange_verified"]
        assert lines, "no sso_sts_exchange_verified line reached stdout through the real formatter"
        assert lines[0]["audience_bound"] is bound


def test_the_documented_logs_insights_filter_matches_the_real_line(monkeypatch, caplog, production_log_lines):
    """Proves the regex docs/DEPLOYMENT.md's runbook tells an operator to
    paste into a CloudWatch Logs Insights query -- `@message like
    /"audience_bound": ?false/` -- against the RAW text this process would
    actually ship, not against the parsed-and-therefore-forgiving JSON
    object the other test in this file compares against. A field-based
    filter (`audience_bound = false`) depends on CloudWatch's own JSON
    auto-discovery agreeing the field is a boolean, which nothing here can
    confirm; a substring/regex match on the literal line does not."""
    import re

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

    sso_sts.verify_and_call_sts("POST", URL, _headers(["host"]), BODY)  # unbound: no audience header

    documented_pattern = re.compile(r'"audience_bound": ?false')
    raw_lines = [r.getMessage() for r in caplog.records if '"event": "sso_sts_exchange_verified"' in r.getMessage()]
    assert raw_lines, "no sso_sts_exchange_verified line captured"
    assert documented_pattern.search(raw_lines[0]), (
        f"the documented Logs Insights pattern does not match the real line: {raw_lines[0]!r}"
    )
