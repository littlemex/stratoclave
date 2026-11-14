"""The console path for adding a model under an inference boundary, through the real
promotion, probe and activation code (`mvp.discovery`), not their parts.

A model whose recorded destinations lie inside the boundary is promoted, verified,
activated and served with those destinations; one that can be served outside it is
refused by name at promotion, before the verification probe sends anything, and at
activation, before any write."""
from __future__ import annotations

import pytest

from mvp import inference_boundary as ib

JAPAN = "ap-northeast-1,ap-northeast-3"
JP = ("ap-northeast-1", "ap-northeast-3")


@pytest.fixture(autouse=True)
def _fresh_registry():
    from mvp.models import reset_composed_registry_for_test

    reset_composed_registry_for_test()
    yield
    reset_composed_registry_for_test()


@pytest.fixture
def japan(monkeypatch):
    monkeypatch.setenv(ib.ENV, JAPAN)
    monkeypatch.setenv("BEDROCK_REGION", "ap-northeast-1")


def _scope():
    from mvp.discovery.records import ObservationScope

    return ObservationScope(account="000000000000", region="ap-northeast-1",
                            credentials_fingerprint="t", observed_at="2026-11-14T00:00:00+00:00")


def _record(model_id, destinations, *, scope="jp", region="ap-northeast-1"):
    from mvp.discovery.records import DiscoveredRecord

    return DiscoveredRecord(
        profile_id=model_id, provider="anthropic", profile_scope=scope,
        model_family=model_id.split(".")[-1], jurisdiction_bounded=scope != "global",
        destination_regions=destinations, invocation_region=region, raw_id=model_id,
        raw_payload={}, observation_scope=_scope())


def _candidate(record, alias):
    from mvp.discovery.promotion import derive_candidate

    return derive_candidate(record, aliases=[alias], pricing_key="opus",
                            jurisdiction="jp",
                            probe_wire_protocol="messages", created_by="admin-1")


def _activate(monkeypatch, record, candidate):
    from mvp.discovery.activation import activate_candidate
    from mvp.discovery.promotion import candidate_digest, put_promotion_candidate
    from mvp.discovery.records import put_discovered_record, record_digest
    from mvp.discovery.verdict import ProbeVerdict, put_probe_verdict
    from mvp.deps import AuthenticatedUser
    import mvp.authz as authz

    put_discovered_record(record)
    put_promotion_candidate(candidate)
    verdict = ProbeVerdict(
        profile_id=record.profile_id, observation_scope=record.observation_scope,
        invocation="sync", verified_at="2026-11-14T01:00:00+00:00", verified_by="probe",
        pricing_key_at_verification="opus", wire_protocol_verified="messages", state="verified",
        candidate_digest=candidate_digest(candidate), record_digest=record_digest(record))
    put_probe_verdict(verdict)
    monkeypatch.setattr(authz, "_get_permissions_for_role",
                        lambda r: ["models:promote"] if r == "promoter" else [])
    actor = AuthenticatedUser(user_id="a", email="a@example.com", roles=["promoter"],
                              org_id="default-org", auth_kind="jwt")
    return activate_candidate(record.profile_id, "sync", actor=actor,
                              expected_verified_at=verdict.verified_at)


def test_a_japan_model_is_activated_with_its_destinations_and_served(dynamodb_mock, monkeypatch, japan):
    from mvp.models import registry_entries, resolve_model

    record = _record("jp.anthropic.claude-newmodel-9", ("ap-northeast-3", "ap-northeast-1"))
    _activate(monkeypatch, record, _candidate(record, "claude-newmodel-9"))
    entry = resolve_model("claude-newmodel-9")
    assert entry.inference_regions == JP
    assert entry.bedrock_region == "ap-northeast-1"
    assert entry in registry_entries()


def test_a_global_model_is_refused_at_activation_before_any_write(dynamodb_mock, monkeypatch, japan):
    """Promotion refuses it too; built here without promotion's check to show the
    activation refusal stands on its own."""
    from mvp.discovery import activation
    from mvp.discovery.promotion import PromotionCandidate

    record = _record("global.anthropic.claude-newmodel-9", ("", "ap-northeast-1"), scope="global")
    candidate = PromotionCandidate(
        profile_id=record.profile_id, observation_scope=record.observation_scope, state="candidate",
        aliases=("claude-newmodel-9-global",), pricing_key="opus", jurisdiction=None,
        provider="anthropic", bedrock_model_id=record.raw_id, bedrock_region="ap-northeast-1",
        wire_protocol="messages", model_family=record.model_family, profile_scope="global",
        created_at="2026-11-14T00:00:00+00:00", created_by="admin-1")
    commits = []
    monkeypatch.setattr(activation, "_commit_activation", lambda *a, **k: commits.append(a))
    with pytest.raises(activation.ActivationRefused) as refused:
        _activate(monkeypatch, record, candidate)
    assert refused.value.reason == activation.ActivationRefused.OUTSIDE_BOUNDARY
    assert "any region" in str(refused.value)
    assert commits == []


def test_without_a_boundary_activation_is_unchanged(dynamodb_mock, monkeypatch):
    from mvp.models import resolve_model

    monkeypatch.delenv(ib.ENV, raising=False)
    monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
    record = _record("us.anthropic.claude-newmodel-9", ("us-east-1", "us-east-2", "us-west-2"),
                     scope="us", region="us-east-1")
    _activate(monkeypatch, record, _candidate(record, "claude-newmodel-9"))
    assert resolve_model("claude-newmodel-9").inference_regions == ("us-east-1", "us-east-2", "us-west-2")


@pytest.mark.parametrize("model_id, destinations, scope", [
    ("global.anthropic.claude-newmodel-9", ("", "ap-northeast-1"), "global"),
    ("apac.anthropic.claude-newmodel-9", ("ap-northeast-1", "ap-southeast-2"), "apac"),
    ("jp.anthropic.claude-newmodel-9", (), "jp"),
])
def test_promotion_refuses_a_model_that_can_be_served_outside(japan, model_id, destinations, scope):
    from mvp.discovery.promotion import PromotionRefused

    with pytest.raises(PromotionRefused) as refused:
        _candidate(_record(model_id, destinations, scope=scope), "claude-newmodel-9")
    assert refused.value.reason == PromotionRefused.OUTSIDE_BOUNDARY


def test_the_probe_sends_nothing_for_a_model_outside_the_boundary(monkeypatch, japan):
    """A candidate made before the boundary was set (or by any other path) must not
    be called even once: the probe's own prompt would be served outside."""
    from mvp.discovery import probe as probe_mod
    from mvp.discovery.ledger import ProbeAttemptRefused
    from mvp.discovery.promotion import PromotionCandidate

    record = _record("global.anthropic.claude-newmodel-9", ("", "ap-northeast-1"), scope="global")
    candidate = PromotionCandidate(
        profile_id=record.profile_id, observation_scope=record.observation_scope, state="candidate",
        aliases=("x",), pricing_key="opus", jurisdiction=None, provider="anthropic",
        bedrock_model_id=record.raw_id, bedrock_region="ap-northeast-1", wire_protocol="messages",
        model_family=record.model_family, profile_scope="global",
        created_at="2026-11-14T00:00:00+00:00", created_by="admin-1")

    class _Bedrock:
        calls = 0

        def __getattr__(self, name):
            _Bedrock.calls += 1
            raise AssertionError(f"Bedrock was called: {name}")

    with pytest.raises(ProbeAttemptRefused, match="outside the inference boundary"):
        probe_mod.probe(record, candidate, invocation="sync", bedrock=_Bedrock())
    assert _Bedrock.calls == 0


def test_a_direct_model_is_recorded_as_served_where_it_is_called(japan):
    assert ib.reach_from_destinations((), profile_scope="no_profile",
                                      source_region="ap-northeast-1") == ("ap-northeast-1",)
    assert ib.discovered_exclusion_reason(
        bedrock_region="ap-northeast-1", wire_protocol="messages", profile_scope="no_profile",
        destination_regions=()) is None
