"""Reach recorded by discovery and carried into an activated entry, so a model that
stays inside an inference boundary can be added through the console with no code
change (`mvp.discovery.reach`, `mvp.discovery.activation`)."""
from __future__ import annotations

import pytest

from mvp import inference_boundary as ib
from mvp.discovery import activation
from mvp.discovery.reach import ReachUnknown, destination_regions
from mvp.discovery.records import destination_regions_from_models


class _Bedrock:
    def __init__(self, arns=None, error=None):
        self.arns, self.error = arns, error

    def get_inference_profile(self, inferenceProfileIdentifier):
        if self.error:
            raise self.error
        return {"models": [{"modelArn": a} for a in self.arns]}


JP_ARNS = ["arn:aws:bedrock:ap-northeast-1::foundation-model/anthropic.claude-opus-5-5",
           "arn:aws:bedrock:ap-northeast-3::foundation-model/anthropic.claude-opus-5-5"]
GLOBAL_ARNS = ["arn:aws:bedrock:::foundation-model/anthropic.claude-opus-5-5",
               "arn:aws:bedrock:ap-northeast-1::foundation-model/anthropic.claude-opus-5-5"]


def test_a_profile_reaches_its_listed_destinations():
    assert destination_regions(_Bedrock(JP_ARNS), "jp.anthropic.claude-opus-5-5", is_profile=True,
                               source_region="ap-northeast-1") == ("ap-northeast-1", "ap-northeast-3")


def test_a_region_less_destination_means_any_region():
    assert destination_regions(_Bedrock(GLOBAL_ARNS), "global.x", is_profile=True,
                               source_region="ap-northeast-1") == (ib.UNBOUNDED,)


def test_a_direct_id_reaches_only_where_it_is_called():
    assert destination_regions(None, "qwen.x", is_profile=False, source_region="ap-northeast-1") == (
        "ap-northeast-1",)


def test_a_profile_bedrock_does_not_know_is_unknown_never_local():
    with pytest.raises(ReachUnknown):
        destination_regions(_Bedrock(error=RuntimeError("not found")), "jp.x", is_profile=True,
                            source_region="us-east-1")
    with pytest.raises(ReachUnknown):
        destination_regions(_Bedrock([]), "jp.x", is_profile=True, source_region="ap-northeast-1")


def test_the_discovered_destinations_become_the_activated_entrys_reach():
    """Discovery keeps the region-less ARN as "" (records.destination_regions_from_models);
    activation turns it into "*"."""
    jp = destination_regions_from_models([{"modelArn": a} for a in JP_ARNS])
    glob = destination_regions_from_models([{"modelArn": a} for a in GLOBAL_ARNS])
    assert activation.inference_regions_from_destinations(jp) == ("ap-northeast-1", "ap-northeast-3")
    assert activation.inference_regions_from_destinations(glob) == (ib.UNBOUNDED,)
    assert activation.inference_regions_from_destinations(()) is None


def test_the_reach_survives_the_activated_entry_store_round_trip():
    from mvp.models import ModelEntry

    entry = ModelEntry(provider="anthropic", bedrock_model_id="jp.anthropic.claude-opus-5-5",
                       bedrock_region="ap-northeast-1", aliases=("claude-opus-5-5",),
                       wire_protocol="messages", pricing_key="opus", profile_scope="jp",
                       model_family="claude-opus-5-5", access="entitlement_required",
                       jurisdiction_bounded=True, jurisdiction="jp",
                       inference_regions=("ap-northeast-1", "ap-northeast-3"))
    assert activation._entry_from_fields(activation._entry_fields(entry)) == entry
    old_row = dict(activation._entry_fields(entry))
    del old_row["inference_regions"]
    assert activation._entry_from_fields(old_row).inference_regions is None


def test_activation_names_the_boundary_as_a_refusal_reason_with_a_client_status():
    from mvp.admin_discovery import _ACTIVATION_REFUSED_STATUS

    assert activation.ActivationRefused.OUTSIDE_BOUNDARY in activation.ActivationRefused.REASONS
    assert _ACTIVATION_REFUSED_STATUS[activation.ActivationRefused.OUTSIDE_BOUNDARY] == 422


def test_discovery_reads_profiles_from_the_deployments_region_under_a_boundary(monkeypatch):
    """A `jp.` profile does not exist in us-east-1 (measured 2026-11-14), so with a
    boundary the pass runs where the gateway calls Bedrock from."""
    from mvp.discovery import reconcile

    seen = {}

    def fake_client(service, *, region, injected=None):
        seen["region"] = region
        raise RuntimeError("stop here")

    monkeypatch.setattr(reconcile, "_client", fake_client)
    monkeypatch.delenv(reconcile.STRATOCLAVE_REGION_ENV, raising=False)
    monkeypatch.setenv(ib.ENV, "ap-northeast-1,ap-northeast-3")
    monkeypatch.setenv("BEDROCK_REGION", "ap-northeast-1")
    pass_fn = next(getattr(reconcile, n) for n in ("run_pass", "reconcile_pass", "discovery_pass")
                   if hasattr(reconcile, n))
    try:
        pass_fn()
    except Exception:  # noqa: BLE001 - only the region matters here
        pass
    assert seen["region"] == "ap-northeast-1"
