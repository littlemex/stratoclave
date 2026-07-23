"""S1 seam tests: served_by='semantic-router' virtual entry + sr_is_servable stub.

S1 makes the SR transport *type-legal but never selectable*: the registry gains
the "semantic-router" served_by value and virtual/sr_pool_ref fields, and
serving/semantic_router.py exists with sr_is_servable() hard-wired to False. The
hot path is byte-identical (no registry entry uses SR; the flag defaults off).
"""
from __future__ import annotations

from mvp.models import ModelEntry, _validate_registry, registry_entries
from mvp.serving import semantic_router as sr_serving


def test_semantic_router_served_by_is_type_legal():
    e = ModelEntry(
        provider="anthropic", bedrock_model_id="sr:pool/default",
        bedrock_region="us-east-1", aliases=("sr-pool",), wire_protocol="messages",
        served_by="semantic-router", virtual=True, sr_pool_ref="default",
    )
    assert e.served_by == "semantic-router"
    assert e.virtual is True and e.sr_pool_ref == "default"


def test_default_entry_is_not_virtual():
    # existing entries default to non-virtual bedrock — unchanged.
    for e in registry_entries():
        assert e.virtual is False
        assert e.served_by in ("bedrock", "vllm")  # no SR entry ships yet


def test_sr_is_servable_stub_always_false():
    e = ModelEntry(
        provider="anthropic", bedrock_model_id="sr:pool/default",
        bedrock_region="us-east-1", aliases=("sr-pool",), wire_protocol="messages",
        served_by="semantic-router", virtual=True, sr_pool_ref="default",
    )
    # S1 stub: never servable, regardless of tenant/time — SR is unreachable.
    assert sr_serving.sr_is_servable(e, "acme", 0.0) is False
    assert sr_serving.sr_is_servable(e, "other", 1e12) is False


def test_semantic_router_dark_by_default(monkeypatch):
    monkeypatch.delenv("SEMANTIC_ROUTER_ENABLED", raising=False)
    assert sr_serving.semantic_router_enabled() is False
    monkeypatch.setenv("SEMANTIC_ROUTER_ENABLED", "true")
    assert sr_serving.semantic_router_enabled() is True


def test_registry_still_validates():
    # adding the fields + Literal value must not break the shipped registry's
    # load-time validation (no SR/virtual entries present).
    _validate_registry(tuple(registry_entries()))
