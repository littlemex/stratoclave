"""core.deployment: the facts every module reads the same way.

Covers the host normalisation shared with the CLI (contract:C6.11), the
strict/lenient boolean readers, and the startup checks `main.py`'s lifespan
calls before the app serves a request.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from core import deployment

ROOT = pathlib.Path(__file__).resolve().parents[2]
VECTORS = json.loads((ROOT / "contracts" / "host_normalization_vectors.json").read_text())["cases"]


@pytest.mark.parametrize("case", VECTORS, ids=[c["url"] for c in VECTORS])
def test_host_of_matches_the_shared_vectors(case):
    assert deployment.host_of(case["url"]) == case["host"]


@pytest.mark.parametrize("url", [
    "https://[::1",
    "https://[::1:8080",
    "not a url",
    "ftp://example.com",
    "https://",
    "https://example.com?a=1",
    "https://example.com#frag",
    "https://user:pass@example.com",
    "https://user@example.com",
])
def test_host_of_refuses_rather_than_raises(url):
    # A malformed bracketed IPv6 literal used to raise a bare ValueError out of
    # `urlsplit` itself, before `validate_api_endpoint`'s own try/except could
    # turn it into the documented RuntimeError. Every one of these must return
    # None, never raise.
    assert deployment.host_of(url) is None


def test_host_of_rejects_a_comma_joined_value():
    # A configured list value handed to the single-URL reader by mistake must
    # not resolve to a host at all.
    assert deployment.host_of("https://a, https://b") is None


class TestStrictFlag:
    NAME = "TEST_STRATOCLAVE_STRICT_FLAG"

    @pytest.mark.parametrize("value", ["1", "true", "True", "YES", "on"])
    def test_true_spellings(self, monkeypatch, value):
        monkeypatch.setenv(self.NAME, value)
        assert deployment.strict_flag(self.NAME) is True

    @pytest.mark.parametrize("value", ["", "0", "false", "False", "no", "off"])
    def test_false_spellings(self, monkeypatch, value):
        monkeypatch.setenv(self.NAME, value)
        assert deployment.strict_flag(self.NAME) is False

    def test_unset_is_false(self, monkeypatch):
        monkeypatch.delenv(self.NAME, raising=False)
        assert deployment.strict_flag(self.NAME) is False

    @pytest.mark.parametrize("value", ["enabled", "y", "required", "TRU", "2"])
    def test_an_unrecognised_spelling_raises_rather_than_reading_as_off(self, monkeypatch, value):
        monkeypatch.setenv(self.NAME, value)
        with pytest.raises(RuntimeError, match=self.NAME):
            deployment.strict_flag(self.NAME)

    def test_the_lenient_reader_still_reads_an_unrecognised_spelling_as_off(self, monkeypatch):
        # `flag` is unaffected by `strict_flag` existing: a caller that wants
        # silent-off-on-typo still has that option.
        monkeypatch.setenv(self.NAME, "enabled")
        assert deployment.flag(self.NAME) is False


class TestValidateApiEndpoint:
    def test_unset_outside_a_local_environment_raises(self, monkeypatch):
        monkeypatch.delenv("STRATOCLAVE_API_ENDPOINT", raising=False)
        monkeypatch.setenv("ENVIRONMENT", "production")
        with pytest.raises(RuntimeError, match="STRATOCLAVE_API_ENDPOINT"):
            deployment.validate_api_endpoint()

    def test_unset_with_no_environment_set_at_all_also_raises(self, monkeypatch):
        # `ENVIRONMENT` unset entirely (not just non-local) must not be read as
        # permissive: `is_local_environment()` is False for an empty value, so
        # this is the same refusal as an explicit "production".
        monkeypatch.delenv("STRATOCLAVE_API_ENDPOINT", raising=False)
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        with pytest.raises(RuntimeError, match="STRATOCLAVE_API_ENDPOINT"):
            deployment.validate_api_endpoint()

    def test_a_scheme_less_endpoint_raises(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("STRATOCLAVE_API_ENDPOINT", "d1.cloudfront.net")
        with pytest.raises(RuntimeError, match="absolute http"):
            deployment.validate_api_endpoint()

    def test_a_malformed_bracketed_ipv6_endpoint_raises_the_documented_error(self, monkeypatch):
        # Pins the fix for the bare ValueError `urlsplit` used to throw here.
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("STRATOCLAVE_API_ENDPOINT", "https://[::1")
        with pytest.raises(RuntimeError, match="absolute http"):
            deployment.validate_api_endpoint()

    def test_a_local_environment_with_no_endpoint_passes(self, monkeypatch):
        monkeypatch.delenv("STRATOCLAVE_API_ENDPOINT", raising=False)
        monkeypatch.setenv("ENVIRONMENT", "development")
        deployment.validate_api_endpoint()  # must not raise

    def test_a_valid_endpoint_passes_in_any_environment(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("STRATOCLAVE_API_ENDPOINT", "https://d1.cloudfront.net")
        deployment.validate_api_endpoint()  # must not raise


class TestValidateApiEndpointPrivate:
    """`STRATOCLAVE_API_ENDPOINT_PRIVATE` — set only by `iac/bin/iac.ts` under
    `STRATOCLAVE_ENTRY=both`. Unlike `STRATOCLAVE_API_ENDPOINT`, unset is the
    normal case (every entry mode except `both`), so only a PRESENT,
    malformed value is a startup failure."""

    def test_unset_passes_in_any_environment(self, monkeypatch):
        monkeypatch.delenv("STRATOCLAVE_API_ENDPOINT_PRIVATE", raising=False)
        monkeypatch.setenv("ENVIRONMENT", "production")
        deployment.validate_api_endpoint_private()  # must not raise
        assert deployment.api_endpoint_private() is None

    def test_a_scheme_less_value_raises(self, monkeypatch):
        monkeypatch.setenv("STRATOCLAVE_API_ENDPOINT_PRIVATE", "stratoclave.internal.example.com")
        with pytest.raises(RuntimeError, match="absolute http"):
            deployment.validate_api_endpoint_private()

    def test_a_valid_value_passes_and_is_returned_without_a_trailing_slash(self, monkeypatch):
        monkeypatch.setenv(
            "STRATOCLAVE_API_ENDPOINT_PRIVATE", "https://stratoclave.internal.example.com/"
        )
        deployment.validate_api_endpoint_private()  # must not raise
        assert deployment.api_endpoint_private() == "https://stratoclave.internal.example.com"


def test_lifespan_calls_both_startup_validators(monkeypatch):
    """`main.py`'s lifespan calls `validate_api_endpoint` and
    `validate_audience_config`, in that order, before it seeds the database --
    reading the source rather than driving the ASGI lifespan, which would need
    a live DynamoDB/Cognito to construct the app at all."""
    main_source = (ROOT / "backend" / "main.py").read_text()
    lifespan_body = main_source.split("async def lifespan(", 1)[1]
    endpoint_at = lifespan_body.index("validate_api_endpoint()")
    audience_at = lifespan_body.index("validate_audience_config()")
    seed_at = lifespan_body.index("seed_all()")
    assert endpoint_at < audience_at < seed_at, (
        "both startup validators must run, in this order, before the seed -- "
        "a misconfigured gateway must refuse to start rather than seed data "
        "it will not serve correctly"
    )
