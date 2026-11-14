"""The inference boundary (`mvp.inference_boundary`, STRATOCLAVE_INFERENCE_REGIONS).

With a boundary, a model entry is part of the deployment only if every region a
call through it can be served in lies inside the boundary and the gateway calls it
from inside. Without one, everything is as before, except that an entry which
could never be called from this deployment's region is not listed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from mvp import inference_boundary as ib
from mvp import models
from mvp.models import ModelEntry

JAPAN = "ap-northeast-1,ap-northeast-3"
JP_REACH = ("ap-northeast-1", "ap-northeast-3")
US_REACH = ("us-east-1", "us-east-2", "us-west-2")


def _entry(model_id, region, reach, *, scope="jp", aliases=None, wire="messages", family=None):
    return ModelEntry(
        provider="anthropic", bedrock_model_id=model_id, bedrock_region=region,
        aliases=tuple(aliases or (model_id,)), wire_protocol=wire, pricing_key="opus",
        profile_scope=scope, model_family=family or model_id.split(".")[-1],
        inference_regions=reach)


@pytest.fixture
def japan(monkeypatch):
    monkeypatch.setenv(ib.ENV, JAPAN)
    monkeypatch.setenv("BEDROCK_REGION", "ap-northeast-1")
    models.reset_composed_registry_for_test()
    yield
    models.reset_composed_registry_for_test()


# --- parsing -------------------------------------------------------------------

def test_unset_means_no_boundary():
    assert ib.parse(None) is None


@pytest.mark.parametrize("raw", ["", " ", "ap-northeast-1,", "ap-northeast-1,,us-east-1",
                                 "tokyo", "ap-northeast-1,ap-northeast-1", "AP-NORTHEAST-1"])
def test_a_set_but_unusable_value_is_an_error_never_no_boundary(raw):
    with pytest.raises(ib.BoundaryConfigError):
        ib.parse(raw)


def test_a_region_list_parses_with_spaces():
    assert ib.parse(" ap-northeast-1 , ap-northeast-3 ") == frozenset(JP_REACH)


# --- the rule ------------------------------------------------------------------

FIXTURE = [
    _entry("us.anthropic.m-a", "us-east-1", US_REACH, scope="us", aliases=["m-a"], family="m-a"),
    _entry("jp.anthropic.m-a", "ap-northeast-1", JP_REACH, aliases=["m-a"], family="m-a"),
    _entry("apac.anthropic.m-b", "ap-northeast-1", ("ap-northeast-1", "ap-southeast-2"), scope="apac"),
    _entry("global.anthropic.m-c", "ap-northeast-1", (ib.UNBOUNDED,), scope="global"),
    _entry("qwen.direct-tokyo", "ap-northeast-1", ("ap-northeast-1",), scope="no_profile"),
    _entry("qwen.direct-virginia", "us-east-1", ("us-east-1",), scope="no_profile"),
    _entry("jp.anthropic.never-recorded", "ap-northeast-1", None),
    _entry("jp.anthropic.recorded-from-osaka", "ap-northeast-3", JP_REACH),
]


def test_with_japan_only_entries_served_inside_japan_from_tokyo_remain():
    kept, excluded = ib.compose(FIXTURE, boundary=frozenset(JP_REACH), primary="ap-northeast-1")
    assert [e.bedrock_model_id for e in kept] == ["jp.anthropic.m-a", "qwen.direct-tokyo"]
    reasons = {x.bedrock_model_id: x.reason for x in excluded}
    assert "outside the boundary" in reasons["us.anthropic.m-a"]
    assert "ap-southeast-2" in reasons["apac.anthropic.m-b"]
    assert "any region" in reasons["global.anthropic.m-c"]
    assert "outside the boundary" in reasons["qwen.direct-virginia"]
    assert "never recorded" in reasons["jp.anthropic.never-recorded"]
    assert "ap-northeast-3" in reasons["jp.anthropic.recorded-from-osaka"]


def test_without_a_boundary_only_uncallable_profiles_are_hidden():
    kept, excluded = ib.compose(FIXTURE, boundary=None, primary="us-east-1")
    ids = [e.bedrock_model_id for e in kept]
    # the jp. variant of m-a is hidden (it cannot be called from us-east-1), so the
    # us. one keeps the shared name with no conflict
    assert "us.anthropic.m-a" in ids and "jp.anthropic.m-a" not in ids
    # everything else is served exactly as before, including unknown reach and
    # direct ids whatever their region
    for model_id in ("global.anthropic.m-c", "qwen.direct-tokyo", "qwen.direct-virginia",
                     "jp.anthropic.never-recorded"):
        assert model_id in ids
    assert not any(x.reason.startswith(ib.ALIAS_CONFLICT) for x in excluded)


def test_without_a_boundary_a_tokyo_deployment_gets_the_japan_variant():
    kept, _ = ib.compose(FIXTURE, boundary=None, primary="ap-northeast-1")
    owners = {a: e.bedrock_model_id for e in kept for a in e.aliases}
    assert owners["m-a"] == "jp.anthropic.m-a"


def test_a_self_hosted_entry_is_not_bedrock_inference_and_is_untouched():
    vllm = ModelEntry(provider="qwen", bedrock_model_id="local-model", bedrock_region="self-hosted",
                      aliases=("local-model",), wire_protocol="messages", pricing_key="opus",
                      served_by="vllm", endpoint_key="k", profile_scope="no_profile",
                      model_family="local-model")
    kept, _ = ib.compose([vllm], boundary=frozenset(JP_REACH), primary="ap-northeast-1")
    assert kept == (vllm,)


# --- the registry, end to end ----------------------------------------------------

def test_the_bundled_registry_under_a_japan_boundary_serves_only_japan(japan):
    entries = models.registry_entries()
    assert entries and all(e.bedrock_model_id.startswith("jp.") for e in entries)
    assert models.resolve_model("claude-opus-4-7").bedrock_model_id == "jp.anthropic.claude-opus-4-7"
    with pytest.raises(ValueError):
        models.resolve_model("claude-opus-5")  # US-only model: not available here
    with pytest.raises(ValueError):
        models.resolve_model("us.anthropic.claude-opus-4-7")


@pytest.mark.parametrize("region", ["us-east-1", "us-east-2", "us-west-2"])
def test_without_a_boundary_a_us_deployment_serves_exactly_the_bundled_us_list(monkeypatch, region):
    """The golden check: what a US deployment serves is unchanged by the Japan
    entries, which are declared after the US ones and hidden from it."""
    monkeypatch.delenv(ib.ENV, raising=False)
    monkeypatch.setenv("BEDROCK_REGION", region)
    models.reset_composed_registry_for_test()
    served = [e.bedrock_model_id for e in models.registry_entries()]
    bundled_non_jp = [e.bedrock_model_id for e in models._REGISTRY if not e.bedrock_model_id.startswith("jp.")]
    assert served == bundled_non_jp
    models.reset_composed_registry_for_test()


def test_every_japan_entry_is_declared_after_its_us_variant():
    order = {e.bedrock_model_id: i for i, e in enumerate(models._REGISTRY)}
    for e in models._REGISTRY:
        if e.bedrock_model_id.startswith("jp."):
            twin = "us." + e.bedrock_model_id[len("jp."):]
            if twin in order:
                assert order[twin] < order[e.bedrock_model_id]


def test_an_activated_entry_outside_the_boundary_is_not_served(japan, monkeypatch):
    from mvp.discovery import activation

    outside = _entry("us.anthropic.activated", "us-east-1", US_REACH, scope="us")
    inside = _entry("jp.anthropic.activated", "ap-northeast-1", JP_REACH)
    monkeypatch.setattr(activation, "list_activated_entries",
                        lambda: [SimpleNamespace(entry=outside), SimpleNamespace(entry=inside)])
    models.reset_composed_registry_for_test()
    ids = {e.bedrock_model_id for e in models.registry_entries()}
    assert "jp.anthropic.activated" in ids and "us.anthropic.activated" not in ids


# --- the call path -----------------------------------------------------------------

def test_no_failover_region_under_a_boundary(japan, monkeypatch):
    from mvp.routing import chains

    monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", "ap-northeast-3,us-west-2")
    assert chains.failover_regions() == []
    chains.reset_catalog()
    targets = chains.resolve_chain("claude-opus-4-7").targets
    assert [(t.model_id, t.region) for t in targets] == [("jp.anthropic.claude-opus-4-7", "ap-northeast-1")]
    chains.reset_catalog()


def test_no_bedrock_client_is_built_for_a_region_outside_the_boundary(japan):
    from mvp import _bedrock_clients, _openai_transport

    _bedrock_clients.reset_client_cache()
    with pytest.raises(ib.BoundaryConfigError):
        _bedrock_clients.bedrock_runtime_client("us-east-1")
    with pytest.raises(ib.BoundaryConfigError):
        _openai_transport.sync_client("us-east-2")
    with pytest.raises(ib.BoundaryConfigError):
        _openai_transport.async_client("us-east-2")
    assert _bedrock_clients.bedrock_runtime_client("ap-northeast-1") is not None
    _bedrock_clients.reset_client_cache()


# --- startup -------------------------------------------------------------------------

def _startup(default_model):
    ib.check_startup(entries=models.registry_entries(), exclusions=models.registry_exclusions(),
                     default_model=default_model)


def test_startup_accepts_a_coherent_japan_deployment(japan):
    _startup("jp.anthropic.claude-opus-4-7")


def test_startup_refuses_a_default_model_outside_the_boundary(japan):
    with pytest.raises(ib.BoundaryConfigError, match="DEFAULT_BEDROCK_MODEL"):
        _startup("us.anthropic.claude-opus-4-7")


def test_startup_refuses_a_bedrock_region_outside_the_boundary(japan, monkeypatch):
    monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
    models.reset_composed_registry_for_test()
    with pytest.raises(ib.BoundaryConfigError, match="BEDROCK_REGION"):
        _startup("jp.anthropic.claude-opus-4-7")


def test_startup_refuses_when_nothing_can_be_served(monkeypatch):
    monkeypatch.setenv(ib.ENV, "eu-west-3")
    monkeypatch.setenv("BEDROCK_REGION", "eu-west-3")
    models.reset_composed_registry_for_test()
    with pytest.raises(ib.BoundaryConfigError, match="no model"):
        _startup("jp.anthropic.claude-opus-4-7")
    models.reset_composed_registry_for_test()


def test_startup_refuses_two_models_sharing_a_name_inside_the_boundary(monkeypatch):
    monkeypatch.setenv(ib.ENV, JAPAN)
    monkeypatch.setenv("BEDROCK_REGION", "ap-northeast-1")
    a = _entry("jp.anthropic.x", "ap-northeast-1", JP_REACH, aliases=["same"], family="x")
    b = _entry("qwen.y", "ap-northeast-1", ("ap-northeast-1",), scope="no_profile", aliases=["same"], family="y")
    kept, excluded = ib.compose([a, b], boundary=frozenset(JP_REACH), primary="ap-northeast-1")
    with pytest.raises(ib.BoundaryConfigError, match="share a name"):
        ib.check_startup(entries=kept, exclusions=excluded, default_model="jp.anthropic.x")


def test_startup_does_nothing_without_a_boundary(monkeypatch):
    monkeypatch.delenv(ib.ENV, raising=False)
    ib.check_startup(entries=(), exclusions=(), default_model="anything")


def test_startup_is_called_from_the_real_lifespan_before_the_seed(monkeypatch):
    import asyncio

    import main
    import mvp.price_sources as price_sources

    monkeypatch.setenv(ib.ENV, JAPAN)
    monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
    models.reset_composed_registry_for_test()
    monkeypatch.setattr(price_sources, "validate_configuration", lambda: "bundled")
    reached = []
    monkeypatch.setattr("bootstrap.seed_all", lambda: reached.append(True))

    async def once():
        async with main.lifespan(main.app):
            pass

    with pytest.raises(ib.BoundaryConfigError, match="BEDROCK_REGION"):
        asyncio.run(once())
    assert not reached
    models.reset_composed_registry_for_test()


# --- the bundled data ------------------------------------------------------------------

# Bundled entries whose profile Bedrock no longer knows (retired models), so their
# destinations cannot be recorded; with a boundary they are left out.
_UNRECORDABLE = {
    "us.anthropic.claude-opus-4-20250514-v1:0", "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
    "us.anthropic.claude-3-5-haiku-20241022-v1:0", "us.anthropic.claude-3-opus-20240229-v1:0",
}


def test_every_bundled_bedrock_entry_records_its_destinations():
    missing = {e.bedrock_model_id for e in models._REGISTRY
               if e.served_by == "bedrock" and e.inference_regions is None}
    assert missing == _UNRECORDABLE


def test_a_direct_id_records_exactly_the_region_it_is_called_in():
    for e in models._REGISTRY:
        if e.profile_scope == models.NO_PROFILE_SCOPE and e.inference_regions is not None:
            assert e.inference_regions == (e.bedrock_region,), e.bedrock_model_id


def test_only_variants_of_one_model_may_share_a_name(tmp_path):
    import json

    doc = json.load(open(models.registry_path(), encoding="utf-8"))
    first = dict(doc["models"][0])
    other = dict(first, bedrock_model_id="jp.anthropic.other-model", model_family="other-model",
                 profile_scope="jp", jurisdiction="jp", bedrock_region="ap-northeast-1",
                 inference_regions=list(JP_REACH))
    doc["models"].append(other)
    path = tmp_path / "models.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="is claimed by"):
        models.load_registry(str(path))
