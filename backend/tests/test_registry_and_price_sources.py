"""The model registry and the rate table are data, not Python literals.

That moves a class of mistake from "syntax error at import" to "a field an operator
typed slightly wrong". These tests pin the validation that catches it, and the
three-layer price resolution (bundled floor -> active source -> admin overrides).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mvp import price_sources
from mvp.models import ModelEntry, load_registry, registry_path
from mvp.pricing import Rate, baseline_rates


def _write(tmp_path, doc, name="models.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


def _entry(**over):
    base = {
        "provider": "anthropic",
        "bedrock_model_id": "us.anthropic.claude-opus-5",
        "bedrock_region": "us-east-1",
        "aliases": ["claude-opus-5"],
        "wire_protocol": "messages",
        "pricing_key": "opus",
    }
    base.update(over)
    return base


def _doc(*entries):
    return {"schema_version": 1, "models": list(entries)}


# ---------------------------------------------------------------------------
# Registry document
# ---------------------------------------------------------------------------

class TestRegistryLoader:
    @pytest.fixture(autouse=True)
    def _no_path_override(self, monkeypatch):
        """The shipped-document assertions are about the DEFAULT path, so an
        environment that points elsewhere must not silently invert them."""
        monkeypatch.delenv("STRATOCLAVE_MODEL_REGISTRY_PATH", raising=False)

    def test_the_shipped_document_loads_and_is_the_one_in_effect(self):
        entries = load_registry()
        assert entries, "the shipped registry must not be empty"
        assert Path(registry_path()).parts[-2:] == ("defaults", "models.json")
        assert all(isinstance(e, ModelEntry) for e in entries)

    def test_notes_are_documentation_and_do_not_reach_the_entry(self, tmp_path):
        """`notes` carries the rationale that used to live in Python comments. It
        must be accepted (so it can be written) and inert (so it cannot be read as
        behaviour)."""
        path = _write(tmp_path, _doc(_entry(notes="why this entry looks like this")))
        (entry,) = load_registry(path)
        assert not hasattr(entry, "notes")

    def test_unknown_field_is_rejected_rather_than_ignored(self, tmp_path):
        """A typo like "region" for "bedrock_region" would otherwise be a silently
        dropped setting."""
        path = _write(tmp_path, _doc(_entry(region="us-west-2")))
        with pytest.raises(ValueError, match="unknown field"):
            load_registry(path)

    @pytest.mark.parametrize("field", ["provider", "bedrock_model_id", "bedrock_region",
                                       "aliases", "wire_protocol"])
    def test_missing_required_field_is_rejected(self, tmp_path, field):
        raw = _entry()
        raw.pop(field)
        path = _write(tmp_path, _doc(raw))
        with pytest.raises(ValueError, match="missing required field"):
            load_registry(path)

    @pytest.mark.parametrize("bad", [
        {"provider": "acme"},
        {"wire_protocol": "grpc"},
        {"served_by": "lambda"},
    ])
    def test_unknown_enum_values_are_rejected(self, tmp_path, bad):
        path = _write(tmp_path, _doc(_entry(**bad)))
        with pytest.raises(ValueError):
            load_registry(path)

    def test_empty_or_non_string_aliases_are_rejected(self, tmp_path):
        for aliases in ([], [""], ["ok", 3]):
            path = _write(tmp_path, _doc(_entry(aliases=aliases)))
            with pytest.raises(ValueError):
                load_registry(path)

    def test_duplicate_alias_across_entries_is_rejected(self, tmp_path):
        """Two entries claiming one alias makes resolution depend on document
        order, which is not a contract anyone should rely on."""
        path = _write(tmp_path, _doc(
            _entry(),
            _entry(bedrock_model_id="us.anthropic.claude-sonnet-5", pricing_key="sonnet"),
        ))
        with pytest.raises(ValueError, match="claimed by"):
            load_registry(path)

    def test_alias_colliding_with_another_entrys_bedrock_id_is_rejected(self, tmp_path):
        """Bedrock ids resolve too, so an alias shadowing one is the same ambiguity
        as a duplicate alias."""
        path = _write(tmp_path, _doc(
            _entry(),
            _entry(bedrock_model_id="us.anthropic.claude-sonnet-5",
                   aliases=["us.anthropic.claude-opus-5"], pricing_key="sonnet"),
        ))
        with pytest.raises(ValueError, match="claimed by"):
            load_registry(path)

    def test_repeating_the_same_entrys_id_as_its_own_alias_is_allowed(self, tmp_path):
        """Several shipped entries list the Bedrock id among their aliases; that is
        redundant, not ambiguous, and must keep working."""
        path = _write(tmp_path, _doc(_entry(
            aliases=["claude-opus-5", "us.anthropic.claude-opus-5"])))
        (entry,) = load_registry(path)
        assert "us.anthropic.claude-opus-5" in entry.aliases

    def test_unsupported_schema_version_is_rejected(self, tmp_path):
        path = _write(tmp_path, {"schema_version": 99, "models": [_entry()]})
        with pytest.raises(ValueError, match="unsupported schema_version"):
            load_registry(path)

    def test_missing_file_and_invalid_json_are_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="file not found"):
            load_registry(str(tmp_path / "nope.json"))
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="invalid JSON"):
            load_registry(str(bad))

    def test_empty_model_list_is_rejected(self, tmp_path):
        path = _write(tmp_path, {"schema_version": 1, "models": []})
        with pytest.raises(ValueError, match="non-empty array"):
            load_registry(path)

    def test_vllm_entry_without_an_endpoint_key_is_rejected(self, tmp_path):
        """The pre-existing hybrid-serving invariant must survive the move to data:
        a vLLM entry names an allowlist token, never a URL, and without one it would
        be routed as if it were Bedrock."""
        path = _write(tmp_path, _doc(_entry(served_by="vllm")))
        with pytest.raises(ValueError, match="endpoint_key"):
            load_registry(path)


# ---------------------------------------------------------------------------
# Rate document
# ---------------------------------------------------------------------------

def _rate_doc(rates):
    return {"schema_version": 1, "rates": rates}


_OK_RATE = {
    "input_per_mtok_microusd": 1,
    "output_per_mtok_microusd": 2,
    "cache_read_per_mtok_microusd": 0,
    "cache_write_per_mtok_microusd": 0,
}


class TestRateDocument:
    @pytest.fixture(autouse=True)
    def _no_path_override(self, monkeypatch):
        monkeypatch.delenv("STRATOCLAVE_PRICING_PATH", raising=False)

    def test_the_shipped_document_loads_and_matches_the_baseline(self):
        rates = price_sources.load_rate_document()
        assert rates["opus"] == baseline_rates()["opus"]
        assert Path(price_sources.pricing_path()).parts[-2:] == ("defaults", "pricing.json")

    def test_missing_default_key_is_rejected(self, tmp_path):
        """Every layer degrades to `default`; without it an unpriced model cannot be
        priced at all."""
        path = _write(tmp_path, _rate_doc({"opus": dict(_OK_RATE)}), "pricing.json")
        with pytest.raises(ValueError, match="default"):
            price_sources.load_rate_document(path)

    @pytest.mark.parametrize("value", [None, "5", 1.5, -1, True])
    def test_a_non_integer_or_negative_rate_is_rejected(self, tmp_path, value):
        """Defaulting a bad field to zero would silently stop charging for that
        token class."""
        rate = dict(_OK_RATE)
        rate["output_per_mtok_microusd"] = value
        path = _write(tmp_path, _rate_doc({"default": rate}), "pricing.json")
        with pytest.raises(ValueError, match="output_per_mtok_microusd"):
            price_sources.load_rate_document(path)

    def test_unsupported_schema_version_is_rejected(self, tmp_path):
        path = _write(tmp_path, {"schema_version": 7, "rates": {"default": dict(_OK_RATE)}},
                      "pricing.json")
        with pytest.raises(ValueError, match="unsupported schema_version"):
            price_sources.load_rate_document(path)


# ---------------------------------------------------------------------------
# Price source plugin
# ---------------------------------------------------------------------------

class _StubSource:
    name = "stub"

    def __init__(self, table=None, raises=False):
        self._table, self._raises = table or {}, raises
        self.calls = 0

    def load(self):
        self.calls += 1
        if self._raises:
            raise RuntimeError("rate service unreachable")
        return self._table


@pytest.fixture(autouse=True)
def _clean_sources(monkeypatch):
    """Swap sources in isolation.

    The rate cache holds a last-good source table and a 60 s TTL, and the routing
    tiers are memoised, so without a reset a stub's table leaks into later tests.
    """
    from mvp.pricing import reset_cache

    monkeypatch.delenv("STRATOCLAVE_PRICE_SOURCE", raising=False)
    price_sources.reset_registry_for_tests()
    reset_cache()
    yield
    price_sources.reset_registry_for_tests()
    reset_cache()


class _NoOverrideRepo:
    """A PricingConfigRepository stand-in with no admin overrides."""

    def current_version(self):
        return None

    def load_rates(self, version):  # pragma: no cover - not reached without a version
        return {}


def _effective(cache, key="opus"):
    """The rate `key` is actually charged at, through the path requests use.

    Deliberately not `_baseline()`: the source fetch happens in `_ensure_fresh` (off
    the lock), so calling the merge step alone would test a half of the flow that no
    request ever exercises on its own — the mistake an earlier round of these tests
    made.
    """
    return cache.get(key, _NoOverrideRepo())


def _effective_table(cache):
    cache.get("opus", _NoOverrideRepo())
    version, rates, _overrides, _source_keys = cache.snapshot_inputs(_NoOverrideRepo())
    return rates


class TestPriceSourceRegistry:
    def test_json_source_is_registered_and_active_by_default(self):
        assert price_sources.active_source_name() == "json"
        assert "json" in price_sources.registered_source_names()

    def test_registering_and_selecting_a_source(self, monkeypatch):
        price_sources.register_price_source(_StubSource())
        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "stub")
        assert price_sources.active_source().name == "stub"

    def test_duplicate_registration_is_an_error_unless_replacing(self):
        price_sources.register_price_source(_StubSource())
        with pytest.raises(ValueError, match="already registered"):
            price_sources.register_price_source(_StubSource())
        price_sources.register_price_source(_StubSource(), replace=True)

    @pytest.mark.parametrize("bad", [object(), type("N", (), {"name": ""})()])
    def test_a_source_without_a_name_or_load_is_rejected(self, bad):
        with pytest.raises(ValueError):
            price_sources.register_price_source(bad)

    def test_an_unknown_active_source_raises_instead_of_falling_back(self, monkeypatch):
        """Quietly charging the bundled floor when the operator asked for live
        prices is a billing error, not a warning."""
        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "not-registered")
        with pytest.raises(ValueError, match="unknown price source"):
            price_sources.active_source()


class TestThreeLayerResolution:
    def _cache(self):
        from mvp.pricing import _RateCache

        return _RateCache()

    def test_a_source_table_layers_over_the_floor(self, monkeypatch):
        """A source supplies the keys it knows; the floor still answers for the
        rest, so a partial source cannot make a model unpriceable."""
        cheap = Rate(1, 2, 0, 0)
        price_sources.register_price_source(_StubSource({"opus": cheap}))
        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "stub")
        table = _effective_table(self._cache())
        assert table["opus"] == cheap
        assert table["sonnet"] == baseline_rates()["sonnet"]

    def test_a_source_that_never_succeeded_degrades_to_the_floor(self, monkeypatch):
        price_sources.register_price_source(_StubSource(raises=True))
        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "stub")
        assert _effective_table(self._cache()) == baseline_rates()

    def test_admin_overrides_win_over_the_source(self, monkeypatch):
        """Precedence is floor < source < admin override. An operator's explicit
        override must not be undone by a live feed."""
        from mvp.pricing import _RateCache

        source_rate, override_rate = Rate(1, 2, 0, 0), Rate(9, 9, 9, 9)
        price_sources.register_price_source(_StubSource({"opus": source_rate}))
        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "stub")

        class _Repo:
            def current_version(self):
                return "v1"

            def load_rates(self, version):
                return {"opus": override_rate}

        cache = _RateCache()
        cache._refresh_locked(_Repo())
        assert cache.get("opus") == override_rate

    def test_the_source_is_not_consulted_on_the_request_path(self, monkeypatch):
        """It is called on the cache's refresh interval only — a per-request fetch
        would put a network call in front of every invocation."""
        from mvp.pricing import _RateCache

        stub = _StubSource({"opus": Rate(1, 2, 0, 0)})
        price_sources.register_price_source(stub)
        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "stub")

        class _Repo:
            def current_version(self):
                return None

            def load_rates(self, version):  # pragma: no cover - not reached
                return {}

        cache = _RateCache()
        cache._refresh_locked(_Repo())
        after_refresh = stub.calls
        for _ in range(5):
            cache.get("opus", _Repo())
        assert stub.calls == after_refresh


class TestBundledDocumentsAreShipped:
    """The bundled documents are the allowlist and the price floor: if they are not
    in the image, the process cannot start. A repo-wide `data/` ignore rule already
    swallowed them once, so pin that they are tracked rather than trusting that the
    directory name stays lucky."""

    def test_the_bundled_documents_are_tracked_by_git(self):
        import subprocess
        from pathlib import Path

        repo = Path(__file__).resolve().parents[2]
        try:
            tracked = subprocess.run(
                ["git", "-C", str(repo), "ls-files", "--error-unmatch",
                 "backend/mvp/defaults/models.json", "backend/mvp/defaults/pricing.json"],
                capture_output=True, text=True, timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pytest.skip("git unavailable")
        if tracked.returncode == 128:
            # 128 is "not a git repository" — a container test run, where the useful
            # assertion is simply that the files shipped.
            from mvp import models as models_module

            assert Path(models_module.registry_path()).is_file()
            assert Path(price_sources.pricing_path()).is_file()
            return
        assert tracked.returncode == 0, (
            "the bundled defaults are not tracked by git — they would be missing from "
            f"the image and the process would fail to start: {tracked.stderr.strip()}"
        )


class TestFailureSeparation:
    """A misconfiguration and a transient are not the same event, and the difference
    is money: charging the bundled floor because someone typo'd an env var is a
    billing error, while riding out a rate service blip is correct."""

    def _cache(self):
        from mvp.pricing import _RateCache

        return _RateCache()

    def test_unknown_source_name_reaches_the_charging_path_as_an_error(self, monkeypatch):
        """The earlier test only called `active_source()` directly, so it could not
        see that the cache swallowed the very error that function exists to raise."""
        from mvp.price_sources import PriceSourceConfigError

        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "not-registered")
        with pytest.raises(PriceSourceConfigError):
            _effective(self._cache())

    def test_last_good_source_table_survives_a_later_failure(self, monkeypatch):
        """Regressing to the floor would under-charge for every key where the source
        supplied a higher real-world price than the bundled default."""
        dear = Rate(9_000_000, 90_000_000, 0, 0)

        class _Flaky:
            name = "flaky"

            def __init__(self):
                self.fail = False

            def load(self):
                if self.fail:
                    raise RuntimeError("rate service down")
                return {"opus": dear}

        source = _Flaky()
        price_sources.register_price_source(source)
        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "flaky")
        cache = self._cache()
        assert _effective(cache) == dear
        source.fail = True
        cache._loaded_at = 0.0  # force the next call to refresh
        assert _effective(cache) == dear, "a blip must not reprice downwards"

    def test_a_malformed_source_table_is_treated_as_a_load_failure(self, monkeypatch):
        """A source is third-party code from the gateway's point of view; an
        unvalidated table would first show up as a wrong invoice or a 500."""
        for bad in ({"opus": "cheap"}, {"opus": None}, {"opus": Rate(1, -2, 0, 0)}, ["opus"]):
            price_sources.reset_registry_for_tests()

            class _Bad:
                name = "bad"

                def load(self):
                    return bad

            price_sources.register_price_source(_Bad())
            monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "bad")
            assert _effective_table(self._cache()) == baseline_rates()

    def test_a_source_may_not_lower_the_default_key(self, monkeypatch):
        """`default` is what an unregistered pricing key is charged at, and the
        standing rule is that an unpriced model over-charges rather than under."""
        price_sources.register_price_source(_StubSource({"default": Rate(1, 1, 0, 0)}))
        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "stub")
        assert _effective(self._cache(), "default") == baseline_rates()["default"]

    def test_a_source_may_raise_the_default_key_field_by_field(self, monkeypatch):
        """The clamp is per field, not all-or-nothing: a source may raise the fields
        it knows about while the floor still protects the ones it left at zero."""
        from mvp.rates import RATE_FIELDS

        dearer = Rate(99_000_000, 99_000_000, 0, 0)
        price_sources.register_price_source(_StubSource({"default": dearer}))
        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "stub")
        got = _effective(self._cache(), "default")
        floor = baseline_rates()["default"]
        for field in RATE_FIELDS:
            assert getattr(got, field) == max(getattr(dearer, field), getattr(floor, field))
        assert got.output_per_mtok_microusd == 99_000_000

    def test_lowering_only_one_field_of_default_is_still_floored(self, monkeypatch):
        """A source matching the floor's output rate while lowering input would slip
        past a single-field comparison and under-charge every unpriced model."""
        floor = baseline_rates()["default"]
        sneaky = Rate(1, floor.output_per_mtok_microusd,
                      floor.cache_read_per_mtok_microusd, floor.cache_write_per_mtok_microusd)
        price_sources.register_price_source(_StubSource({"default": sneaky}))
        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "stub")
        assert _effective(self._cache(), "default") == floor

    def test_the_source_is_reconsulted_even_when_the_override_version_is_stable(self, monkeypatch):
        """Gating the source read on the override version froze live prices for as
        long as an operator left the override set alone."""
        from mvp.pricing import _RateCache

        stub = _StubSource({"opus": Rate(1, 2, 0, 0)})
        price_sources.register_price_source(stub)
        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "stub")

        class _Repo:
            def current_version(self):
                return "v1"

            def load_rates(self, version):
                return {"sonnet": Rate(3, 4, 0, 0)}

        cache = _RateCache()
        cache.get("opus", _Repo())
        first = stub.calls
        cache._loaded_at = 0.0  # TTL elapsed
        cache.get("opus", _Repo())
        assert stub.calls > first, "the source must be re-read on every refresh"
        # The override survives the re-merge even though its version did not move.
        assert cache.get("sonnet", _Repo()) == Rate(3, 4, 0, 0)


class TestStrictFieldTypes:
    @pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
    def test_virtual_must_be_a_json_boolean(self, tmp_path, value):
        """`bool("false")` is True, and `virtual` is the flag that keeps an entry
        from ever becoming a charge-of-record model."""
        path = _write(tmp_path, _doc(_entry(virtual=value)))
        with pytest.raises(ValueError, match="virtual"):
            load_registry(path)

    @pytest.mark.parametrize("field", ["bedrock_region", "bedrock_model_id", "pricing_key"])
    def test_string_fields_must_be_strings(self, tmp_path, field):
        path = _write(tmp_path, _doc(_entry(**{field: 123})))
        with pytest.raises(ValueError, match="must be a string"):
            load_registry(path)

    def test_a_missing_key_and_an_empty_value_report_differently(self, tmp_path):
        raw = _entry()
        raw.pop("pricing_key")
        with pytest.raises(ValueError, match="missing required field"):
            load_registry(_write(tmp_path, _doc(raw)))
        with pytest.raises(ValueError, match="empty required field"):
            load_registry(_write(tmp_path, _doc(_entry(pricing_key=""))))


class TestPricingKeyIntegrity:
    @pytest.fixture(autouse=True)
    def _no_path_override(self, monkeypatch):
        monkeypatch.delenv("STRATOCLAVE_PRICING_PATH", raising=False)
        monkeypatch.delenv("STRATOCLAVE_MODEL_REGISTRY_PATH", raising=False)

    def test_every_shipped_pricing_key_has_a_rate_row(self):
        """The likeliest transcription mistake in this migration. A key with no row
        is charged at `default`, and `default` is not an upper bound — the fable tier
        sits above it — so a typo can under-charge."""
        from mvp.models import registry_entries

        known = set(price_sources.load_rate_document())
        missing = sorted({e.pricing_key for e in registry_entries()} - known)
        assert not missing, f"pricing keys with no rate row: {missing}"

    def test_an_unknown_pricing_key_is_rejected_at_load(self, tmp_path):
        path = _write(tmp_path, _doc(_entry(pricing_key="fabel")))
        with pytest.raises(ValueError, match="no rate row"):
            load_registry(path)


class TestUniqueness:
    def test_duplicate_bedrock_model_id_across_entries_is_rejected(self, tmp_path):
        """Two entries for one model id left `_BEDROCK_ID_MAP` last-writer-wins, so
        which pricing key got charged depended on document order."""
        path = _write(tmp_path, _doc(
            _entry(aliases=["a"], pricing_key="opus"),
            _entry(aliases=["b"], pricing_key="haiku"),
        ))
        with pytest.raises(ValueError, match="claimed by"):
            load_registry(path)


class TestResponsesRegionIsAuthoritative:
    def _openai_entry(self, region):
        return _entry(provider="google", bedrock_model_id="google.gemma-4-31b",
                      aliases=["gemma-4"], wire_protocol="responses",
                      pricing_key="gemma", bedrock_region=region)

    def test_a_region_openai_does_not_have_is_rejected(self, tmp_path):
        """For a responses entry the region is where the prompt goes, so a typo must
        not reach the transport."""
        path = _write(tmp_path, _doc(self._openai_entry("ap-northeast-1")))
        with pytest.raises(ValueError, match="is not a region where the OpenAI-compatible endpoint serves"):
            load_registry(path)

    def test_a_real_openai_region_is_accepted(self, tmp_path):
        path = _write(tmp_path, _doc(self._openai_entry("eu-central-1")))
        (entry,) = load_registry(path)
        assert entry.bedrock_region == "eu-central-1"

    def test_the_check_does_not_read_the_display_only_env_hint(self, tmp_path, monkeypatch):
        """`OPENAI_BEDROCK_REGIONS` must stay display-only: the IaC residency analysis
        reads the registry and ignores that variable, so making it load-bearing here
        would silently invalidate the analysis. Pinned by
        tests/test_openai_region_residency_contract.py as well; this is the loader
        half of the same contract."""
        monkeypatch.setenv("OPENAI_BEDROCK_REGIONS", "eu-west-1")
        path = _write(tmp_path, _doc(self._openai_entry("us-east-2")))
        (entry,) = load_registry(path)
        assert entry.bedrock_region == "us-east-2"


class TestPathOverrides:
    def test_registry_path_env_override_is_honoured(self, tmp_path, monkeypatch):
        path = _write(tmp_path, _doc(_entry()))
        monkeypatch.setenv("STRATOCLAVE_MODEL_REGISTRY_PATH", path)
        from mvp.models import registry_path as rp

        assert rp() == path
        assert len(load_registry()) == 1

    def test_pricing_path_env_override_is_honoured(self, tmp_path, monkeypatch):
        path = _write(tmp_path, _rate_doc({"default": dict(_OK_RATE)}), "alt-pricing.json")
        monkeypatch.setenv("STRATOCLAVE_PRICING_PATH", path)
        assert price_sources.pricing_path() == path
        assert set(price_sources.load_rate_document()) == {"default"}


class TestRateDocumentFieldAllowlist:
    def test_unknown_rate_field_is_rejected_but_notes_is_allowed(self, tmp_path):
        ok = dict(_OK_RATE, notes="why this rate")
        path = _write(tmp_path, _rate_doc({"default": ok}), "pricing.json")
        assert price_sources.load_rate_document(path)["default"].output_per_mtok_microusd == 2

        bad = dict(_OK_RATE, input_per_mtok="typo")
        path = _write(tmp_path, _rate_doc({"default": bad}), "pricing.json")
        with pytest.raises(ValueError, match="unknown field"):
            price_sources.load_rate_document(path)


class TestRegionAsymmetryIsPinnedOnBothSides:
    """`bedrock_region` is authoritative for `responses` and advisory for `messages`.
    An asymmetry that is only documented drifts; both halves get a test."""

    def test_messages_invoke_uses_the_deployment_region_not_the_entry(self, monkeypatch):
        from mvp import _bedrock_clients

        monkeypatch.setenv("BEDROCK_REGION", "eu-west-1")
        seen = {}

        def _spy(region, **kw):
            seen["region"] = region
            return object()

        monkeypatch.setattr(_bedrock_clients, "bedrock_runtime_client", _spy)
        _bedrock_clients.deployment_client()
        assert seen["region"] == "eu-west-1"

    def test_client_for_model_still_honours_the_entry_region(self, monkeypatch):
        """The OpenAI-compatible endpoint side of the asymmetry: per-entry region stays authoritative."""
        from mvp import _bedrock_clients
        from mvp.models import resolve_model

        seen = {}
        monkeypatch.setattr(_bedrock_clients, "bedrock_runtime_client",
                            lambda region, **kw: seen.setdefault("region", region))
        _bedrock_clients.client_for_model(resolve_model("grok-4.6"))
        assert seen["region"] == resolve_model("grok-4.6").bedrock_region


class TestMigrationEquivalence:
    """The registry moved from a Python literal to JSON. This is the full name->id
    mapping the literal resolved, extracted from it mechanically, so a dropped alias
    or a changed id fails here rather than as a 400 for a model that used to work.

    A subset would not do: the entries most likely to be lost in transcription are
    the old dated aliases nobody exercises day to day.
    """

    GOLDEN = {
        "claude-3-5-haiku": "us.anthropic.claude-3-5-haiku-20241022-v1:0",
        "claude-3-5-haiku-20241022": "us.anthropic.claude-3-5-haiku-20241022-v1:0",
        "claude-3-7-sonnet": "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        "claude-3-7-sonnet-20250219": "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        "claude-3-haiku": "us.anthropic.claude-3-haiku-20240307-v1:0",
        "claude-3-opus": "us.anthropic.claude-3-opus-20240229-v1:0",
        "claude-3-sonnet": "us.anthropic.claude-3-sonnet-20240229-v1:0",
        "claude-fable-5": "us.anthropic.claude-fable-5",
        "claude-haiku-4-5": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "claude-haiku-4-5-20251001": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "claude-opus-4": "us.anthropic.claude-opus-4-20250514-v1:0",
        "claude-opus-4-1": "us.anthropic.claude-opus-4-1-20250805-v1:0",
        "claude-opus-4-1-20250805": "us.anthropic.claude-opus-4-1-20250805-v1:0",
        "claude-opus-4-20250514": "us.anthropic.claude-opus-4-20250514-v1:0",
        "claude-opus-4-5": "us.anthropic.claude-opus-4-5-20251101-v1:0",
        "claude-opus-4-5-20251101": "us.anthropic.claude-opus-4-5-20251101-v1:0",
        "claude-opus-4-6": "us.anthropic.claude-opus-4-6-v1",
        "claude-opus-4-7": "us.anthropic.claude-opus-4-7",
        "claude-opus-5": "us.anthropic.claude-opus-5",
        "claude-sonnet-4-5": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "claude-sonnet-4-5-20250929": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "claude-sonnet-4-6": "us.anthropic.claude-sonnet-4-6",
        "claude-sonnet-5": "us.anthropic.claude-sonnet-5",
        "gpt-5.6-sol": "us.openai.gpt-5.6-sol",
        "gpt-5.6-terra": "us.openai.gpt-5.6-terra",
        "grok-4.6": "us.xai.grok-4.6",
        "nemotron-super-3-120b": "nvidia.nemotron-super-3-120b",
        "nvidia.nemotron-super-3-120b": "nvidia.nemotron-super-3-120b",
        "openai.gpt-5.6-sol": "us.openai.gpt-5.6-sol",
        "openai.gpt-5.6-terra": "us.openai.gpt-5.6-terra",
        "qwen.qwen3-next-80b-a3b": "qwen.qwen3-next-80b-a3b",
        "qwen3-next-80b": "qwen.qwen3-next-80b-a3b",
        "us.anthropic.claude-3-5-haiku-20241022-v1:0": "us.anthropic.claude-3-5-haiku-20241022-v1:0",
        "us.anthropic.claude-3-7-sonnet-20250219-v1:0": "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        "us.anthropic.claude-3-haiku-20240307-v1:0": "us.anthropic.claude-3-haiku-20240307-v1:0",
        "us.anthropic.claude-3-opus-20240229-v1:0": "us.anthropic.claude-3-opus-20240229-v1:0",
        "us.anthropic.claude-3-sonnet-20240229-v1:0": "us.anthropic.claude-3-sonnet-20240229-v1:0",
        "us.anthropic.claude-fable-5": "us.anthropic.claude-fable-5",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "us.anthropic.claude-opus-4-1-20250805-v1:0": "us.anthropic.claude-opus-4-1-20250805-v1:0",
        "us.anthropic.claude-opus-4-20250514-v1:0": "us.anthropic.claude-opus-4-20250514-v1:0",
        "us.anthropic.claude-opus-4-5-20251101-v1:0": "us.anthropic.claude-opus-4-5-20251101-v1:0",
        "us.anthropic.claude-opus-4-6-v1": "us.anthropic.claude-opus-4-6-v1",
        "us.anthropic.claude-opus-4-7": "us.anthropic.claude-opus-4-7",
        "us.anthropic.claude-opus-5": "us.anthropic.claude-opus-5",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "us.anthropic.claude-sonnet-4-6": "us.anthropic.claude-sonnet-4-6",
        "us.anthropic.claude-sonnet-5": "us.anthropic.claude-sonnet-5",
        "xai.grok-4.6": "us.xai.grok-4.6"
    }

    def test_every_name_the_literal_resolved_still_resolves_to_the_same_model(self):
        from mvp.models import resolve_model

        wrong = {}
        for name, model_id in self.GOLDEN.items():
            try:
                got = resolve_model(name).bedrock_model_id
            except ValueError:
                wrong[name] = "unresolvable"
                continue
            if got != model_id:
                wrong[name] = f"{got} (expected {model_id})"
        assert not wrong, f"registry migration changed resolution: {wrong}"

    def test_the_golden_mapping_covers_every_shipped_entry(self):
        """Guards the guard: if a future entry is added without extending this table,
        say so rather than quietly checking less than the whole registry."""
        from mvp.models import registry_entries

        shipped = {e.bedrock_model_id for e in registry_entries()}
        assert shipped <= set(self.GOLDEN.values()), (
            "new entries are not covered by the golden mapping: "
            f"{sorted(shipped - set(self.GOLDEN.values()))}"
        )


class TestConfigErrorReachesTheCaller:
    """The first fix moved the re-raise into `_baseline`, but the refresh above it
    still had a catch-all that absorbs `ValueError` — so the silent floor fallback
    came back one layer down. These go through the paths charging actually uses."""

    def test_refresh_propagates_an_unknown_source_name(self, monkeypatch):
        from mvp.price_sources import PriceSourceConfigError
        from mvp.pricing import _RateCache

        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "not-registered")

        class _Repo:
            def current_version(self):
                return None

            def load_rates(self, version):  # pragma: no cover - not reached
                return {}

        with pytest.raises(PriceSourceConfigError):
            _RateCache().get("opus", _Repo())

    def test_get_propagates_an_unknown_source_name(self, monkeypatch):
        from mvp.price_sources import PriceSourceConfigError
        from mvp.pricing import _RateCache

        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "not-registered")

        class _Repo:
            def current_version(self):
                return None

            def load_rates(self, version):  # pragma: no cover - not reached
                return {}

        with pytest.raises(PriceSourceConfigError):
            _RateCache().get("opus", _Repo())

    def test_a_transient_load_failure_is_still_absorbed(self, monkeypatch):
        """The separation has to cut both ways: a raising source must NOT take
        charging down."""
        from mvp.pricing import _RateCache

        price_sources.register_price_source(_StubSource(raises=True))
        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "stub")

        class _Repo:
            def current_version(self):
                return None

            def load_rates(self, version):  # pragma: no cover - not reached
                return {}

        cache = _RateCache()
        assert cache.get("opus", _Repo()) == baseline_rates()["opus"]


class TestDuplicateJsonKeys:
    """`json.load` keeps the last occurrence of a repeated key, so a botched merge
    leaving two `"opus"` rows would make the charged rate depend on document order —
    the ambiguity the registry already rejects for duplicate model ids."""

    def test_a_duplicate_rate_key_is_rejected(self, tmp_path):
        raw = ('{"schema_version": 1, "rates": {'
               '"default": {"input_per_mtok_microusd": 5, "output_per_mtok_microusd": 5,'
               ' "cache_read_per_mtok_microusd": 0, "cache_write_per_mtok_microusd": 0},'
               '"default": {"input_per_mtok_microusd": 1, "output_per_mtok_microusd": 1,'
               ' "cache_read_per_mtok_microusd": 0, "cache_write_per_mtok_microusd": 0}}}')
        p = tmp_path / "pricing.json"
        p.write_text(raw, encoding="utf-8")
        with pytest.raises(ValueError, match="duplicate key"):
            price_sources.load_rate_document(str(p))

    def test_a_duplicate_field_in_a_model_entry_is_rejected(self, tmp_path):
        raw = ('{"schema_version": 1, "models": [{'
               '"provider": "anthropic", "bedrock_model_id": "us.anthropic.claude-opus-5",'
               '"bedrock_region": "us-east-1", "aliases": ["claude-opus-5"],'
               '"wire_protocol": "messages", "pricing_key": "opus",'
               '"pricing_key": "haiku"}]}')
        p = tmp_path / "models.json"
        p.write_text(raw, encoding="utf-8")
        with pytest.raises(ValueError, match="duplicate key"):
            load_registry(str(p))


class TestVllmCacheRateInvariantSurvivesRefresh:
    def test_a_source_cannot_reintroduce_a_nonzero_vllm_cache_rate(self, monkeypatch):
        """The registry checks this for the bundled floor, but a source is re-read on
        every refresh and could add it back after that check. vLLM reports no Bedrock
        cache-token split, so a nonzero rate is dead pricing that also skews SAAR's
        warm-prefix delta."""
        from mvp import models as models_module
        from mvp.pricing import _RateCache

        # The shipped registry has no vLLM entry (hybrid serving is not enabled in
        # the bundled data), so the entry that makes the invariant apply is injected.
        vllm_entry = ModelEntry(
            provider="qwen", bedrock_model_id="selfhosted.qwen", bedrock_region="us-east-1",
            aliases=("selfhosted-qwen",), wire_protocol="messages", pricing_key="vllm",
            served_by="vllm", endpoint_key="pool-a",
        )
        monkeypatch.setattr(models_module, "registry_entries", lambda: (vllm_entry,))
        price_sources.register_price_source(
            _StubSource({"vllm": Rate(1_000, 2_000, 500, 700)}))
        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "stub")
        rate = _effective(_RateCache(), "vllm")
        assert (rate.cache_read_per_mtok_microusd, rate.cache_write_per_mtok_microusd) == (0, 0)
        # The non-cache fields the source supplied are kept.
        assert rate.input_per_mtok_microusd == 1_000


class TestVirtualEntryCoherence:
    """`virtual` is the only thing keeping a pool placeholder from becoming a charge
    of record, so an entry that claims it must actually look like one."""

    def test_virtual_requires_the_semantic_router_serving_mode(self, tmp_path):
        path = _write(tmp_path, _doc(_entry(virtual=True, sr_pool_ref="pool-a")))
        with pytest.raises(ValueError, match="semantic-router"):
            load_registry(path)

    def test_virtual_requires_the_pool_it_stands_for(self, tmp_path):
        path = _write(tmp_path, _doc(_entry(virtual=True, served_by="semantic-router")))
        with pytest.raises(ValueError, match="sr_pool_ref"):
            load_registry(path)

    def test_a_pool_ref_without_virtual_is_rejected(self, tmp_path):
        path = _write(tmp_path, _doc(_entry(sr_pool_ref="pool-a")))
        with pytest.raises(ValueError, match="not virtual"):
            load_registry(path)

    def test_a_coherent_virtual_entry_loads(self, tmp_path):
        path = _write(tmp_path, _doc(_entry(
            virtual=True, served_by="semantic-router", sr_pool_ref="pool-a")))
        (entry,) = load_registry(path)
        assert entry.virtual and entry.sr_pool_ref == "pool-a"


class TestRepeatedAliasWithinOneEntry:
    def test_an_entry_repeating_its_own_alias_is_rejected(self, tmp_path):
        path = _write(tmp_path, _doc(_entry(aliases=["a", "a"])))
        with pytest.raises(ValueError, match="repeats an alias"):
            load_registry(path)


class TestBuiltinSourceNameIsReserved:
    def test_the_bundled_name_cannot_be_taken_over(self):
        """Taking the name `json` would stamp a live feed's prices as `builtin` on the
        ledger — the dispute ambiguity the sentinels exist to prevent."""
        from mvp.price_sources import PriceSourceConfigError

        class _Impostor:
            name = "json"

            def load(self):  # pragma: no cover - never reached
                return {}

        with pytest.raises(PriceSourceConfigError, match="may not be replaced"):
            price_sources.register_price_source(_Impostor(), replace=True)


class TestUnreadableDocuments:
    def test_a_directory_in_place_of_a_document_is_a_registry_error(self, tmp_path):
        """Not a raw traceback: the message has to name the file that is wrong."""
        d = tmp_path / "models.json"
        d.mkdir()
        with pytest.raises(ValueError, match="cannot read"):
            load_registry(str(d))

    def test_a_directory_in_place_of_a_rate_document_is_a_pricing_error(self, tmp_path):
        d = tmp_path / "pricing.json"
        d.mkdir()
        with pytest.raises(ValueError, match="cannot read"):
            price_sources.load_rate_document(str(d))


class TestConfigErrorDoesNotDecayWithTheTtl:
    """The re-raise alone was not enough: the refresh's `finally` advanced the TTL, so
    only the first request per interval errored and the rest charged whatever table was
    held — the initial floor on a cold cache. The silent fallback had moved onto the
    time axis."""

    def test_every_call_keeps_raising_not_just_the_first(self, monkeypatch):
        from mvp.price_sources import PriceSourceConfigError
        from mvp.pricing import _RateCache

        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "not-registered")
        cache = _RateCache()
        for attempt in range(3):
            with pytest.raises(PriceSourceConfigError):
                cache.get("opus", _NoOverrideRepo())
            assert cache._loaded_at == 0.0, (
                f"attempt {attempt}: the TTL advanced despite the config error, so the "
                "next 60 s of requests would silently charge the held table"
            )


class TestTopLevelFieldAllowlist:
    def test_an_unknown_top_level_field_in_the_registry_is_rejected(self, tmp_path):
        doc = _doc(_entry())
        doc["modles"] = []
        with pytest.raises(ValueError, match="unknown top-level field"):
            load_registry(_write(tmp_path, doc))

    def test_an_unknown_top_level_field_in_the_rate_document_is_rejected(self, tmp_path):
        doc = _rate_doc({"default": dict(_OK_RATE)})
        doc["rate"] = {}
        with pytest.raises(ValueError, match="unknown top-level field"):
            price_sources.load_rate_document(_write(tmp_path, doc, "pricing.json"))

    def test_the_comment_field_is_allowed_in_both(self, tmp_path):
        doc = _doc(_entry())
        doc["$comment"] = ["prose"]
        assert len(load_registry(_write(tmp_path, doc))) == 1
        rdoc = _rate_doc({"default": dict(_OK_RATE)})
        rdoc["$comment"] = ["prose"]
        assert price_sources.load_rate_document(_write(tmp_path, rdoc, "pricing.json"))


class TestVllmEndpointKeyDiagnostic:
    def test_the_error_names_the_file_and_the_entry(self, tmp_path):
        """A self-hosted entry with no endpoint key would be routed as if it were
        Bedrock, so the message has to say which entry to fix."""
        path = _write(tmp_path, _doc(_entry(served_by="vllm")))
        with pytest.raises(ValueError) as ei:
            load_registry(path)
        assert "models[0]" in str(ei.value) and path in str(ei.value)


class TestUnknownKeyFallbackLabel:
    def test_a_source_supplied_default_is_labelled_as_a_feed(self, monkeypatch):
        """An unknown pricing key is charged at `default`, so the dispute label must
        describe where `default` came from — not the key nobody priced."""
        from mvp.pricing import PRICE_SOURCE_SENTINEL, reset_cache, snapshot_rates

        floor = baseline_rates()["default"]
        dearer = Rate(floor.input_per_mtok_microusd * 2, floor.output_per_mtok_microusd * 2,
                      floor.cache_read_per_mtok_microusd, floor.cache_write_per_mtok_microusd)
        price_sources.register_price_source(_StubSource({"default": dearer}))
        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "stub")
        reset_cache()
        try:
            snap = snapshot_rates("a-key-nobody-priced", _NoOverrideRepo())
            assert snap.output_per_mtok_microusd == dearer.output_per_mtok_microusd
            assert snap.version == PRICE_SOURCE_SENTINEL
        finally:
            reset_cache()


class TestVllmClampSurvivesAdminOverrides:
    """The clamp ran before the override merge, so an admin row could put a nonzero
    cache rate back after the source's was removed. The invariant is about what is
    actually charged, so it has to be the last word."""

    def _vllm_registry(self, monkeypatch):
        from mvp import models as models_module

        entry = ModelEntry(
            provider="qwen", bedrock_model_id="selfhosted.qwen", bedrock_region="us-east-1",
            aliases=("selfhosted-qwen",), wire_protocol="messages", pricing_key="vllm",
            served_by="vllm", endpoint_key="pool-a",
        )
        monkeypatch.setattr(models_module, "registry_entries", lambda: (entry,))

    def test_an_admin_override_cannot_reintroduce_a_nonzero_cache_rate(self, monkeypatch):
        from mvp.pricing import _RateCache

        self._vllm_registry(monkeypatch)

        class _Repo:
            def current_version(self):
                return "v1"

            def load_rates(self, version):
                return {"vllm": Rate(1_000, 2_000, 400, 900)}

        rate = _RateCache().get("vllm", _Repo())
        assert (rate.cache_read_per_mtok_microusd, rate.cache_write_per_mtok_microusd) == (0, 0)
        assert rate.input_per_mtok_microusd == 1_000

    def test_the_clamp_also_applies_with_no_override_version(self, monkeypatch):
        from mvp.pricing import _RateCache

        self._vllm_registry(monkeypatch)
        price_sources.register_price_source(
            _StubSource({"vllm": Rate(1_000, 2_000, 500, 700)}))
        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "stub")
        rate = _RateCache().get("vllm", _NoOverrideRepo())
        assert (rate.cache_read_per_mtok_microusd, rate.cache_write_per_mtok_microusd) == (0, 0)


class TestStartupValidation:
    """A misconfigured price source must fail the deployment, because the request path
    degrades on pricing failures by design (`snapshot-failed`) rather than breaking
    admission — so per-request errors would never stop the floor being charged."""

    def test_an_unknown_source_fails_startup_validation(self, monkeypatch):
        from mvp.price_sources import PriceSourceConfigError, validate_configuration

        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "not-registered")
        with pytest.raises(PriceSourceConfigError):
            validate_configuration()

    def test_a_source_returning_a_malformed_table_fails_startup_validation(self, monkeypatch):
        from mvp.price_sources import validate_configuration

        class _Bad:
            name = "bad"

            def load(self):
                return {"opus": "cheap"}

        price_sources.register_price_source(_Bad())
        monkeypatch.setenv("STRATOCLAVE_PRICE_SOURCE", "bad")
        with pytest.raises(ValueError):
            validate_configuration()

    def test_the_bundled_configuration_validates(self):
        from mvp.price_sources import DEFAULT_SOURCE_NAME, validate_configuration

        assert validate_configuration() == DEFAULT_SOURCE_NAME
