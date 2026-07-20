"""Unit tests for the minimal LOCAL shadow VSR judge (mvp.vsr.shadow).

The judge is PURE and injectable: given a requested model + cheap request
features, it advises a cheaper same-family model (or nothing), never steering
execution. These tests use fake resolvers so no registry/pricing import is needed;
one test exercises the real registry-backed defaults.
"""
from __future__ import annotations

from mvp.vsr import shadow

# fake tier map: model id -> {pricing_key, bedrock_model_id}
_FAKE = {
    "opus": {"pricing_key": "opus", "bedrock_model_id": "bid-opus"},
    "sonnet": {"pricing_key": "sonnet", "bedrock_model_id": "bid-sonnet"},
    "haiku": {"pricing_key": "haiku", "bedrock_model_id": "bid-haiku"},
    "mystery": {"pricing_key": "weird", "bedrock_model_id": "bid-mystery"},
}


def _resolve(m):
    return _FAKE.get(m)


def _cheapest(_tier):
    return "haiku"


def _simple(tokens=100):
    return shadow.RequestFeatures(approx_input_tokens=tokens, has_tools=False, has_images=False)


def _propose(model, features):
    return shadow.propose(requested_model=model, features=features,
                          resolve=_resolve, cheapest_model_for_tier=_cheapest)


def test_simple_expensive_request_is_advised_down_to_cheapest():
    s = _propose("opus", _simple())
    assert s is not None and s.model == "haiku" and s.rule_id == "R2-simple-downgrade"


def test_already_cheapest_is_not_advised():
    assert _propose("haiku", _simple()) is None


def test_tools_are_never_downgraded():
    f = shadow.RequestFeatures(approx_input_tokens=100, has_tools=True, has_images=False)
    assert _propose("opus", f) is None


def test_images_are_never_downgraded():
    f = shadow.RequestFeatures(approx_input_tokens=100, has_tools=False, has_images=True)
    assert _propose("opus", f) is None


def test_large_prompt_keeps_the_stronger_model():
    big = shadow.RequestFeatures(approx_input_tokens=999_999, has_tools=False, has_images=False)
    assert _propose("opus", big) is None


def test_unknown_or_unpriced_model_is_out_of_scope():
    assert _propose("nope", _simple()) is None            # not resolvable
    assert _propose("mystery", _simple()) is None          # resolvable but non-tier


def test_never_advises_the_same_model():
    # if the cheapest-for-tier resolves to the SAME bedrock id as requested, no advice.
    s = shadow.propose(requested_model="haiku", features=_simple(),
                       resolve=_resolve, cheapest_model_for_tier=lambda _t: "haiku")
    assert s is None


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("STRATOCLAVE_SHADOW_VSR", raising=False)
    assert shadow.shadow_enabled() is False
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR", "true")
    assert shadow.shadow_enabled() is True


def test_real_registry_backed_defaults_downgrade_opus_to_haiku():
    # exercises the real resolver + cheapest-for-tier against the code registry.
    s = shadow.propose(requested_model="claude-opus-4-7", features=_simple())
    assert s is not None and s.rule_id == "R2-simple-downgrade"
    # the advised model resolves to the haiku tier.
    from mvp.models import resolve_model
    assert resolve_model(s.model).pricing_key == "haiku"
