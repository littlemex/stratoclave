"""Where a call through a Bedrock model id can be served, as Bedrock itself reports it.

`ModelEntry.inference_regions` is this. Discovery records it for a model an
operator promotes (`records.destination_regions_from_models`); this module reads it
the same way for the release check of the bundled registry
(`scripts/check_inference_regions.py`). The inference boundary
(`mvp.inference_boundary`) decides from it; nothing here decides anything.

How Bedrock reports it (measured 2026-11-14):

* A system inference profile, read with `GetInferenceProfile` in the region it is
  called from, lists one foundation-model ARN per destination region
  (`jp.anthropic.*` from Tokyo or Osaka: ap-northeast-1 and ap-northeast-3). The
  same id is not found from another geography (`jp.*` from us-east-1), so the read
  must be made in the entry's own region.
* A `global.` profile lists the region-less ARN `arn:aws:bedrock:::foundation-model/...`
  besides the source region; that ARN is what "any region" looks like.
* A direct model id (no profile) is served where it is called.
"""
from __future__ import annotations

from typing import Any

from ..inference_boundary import reach_from_destinations
from .records import destination_regions_from_models


class ReachUnknown(Exception):
    """Bedrock did not say where this id is served (not found, or no destinations)."""


def destination_regions(bedrock_control: Any, model_id: str, *, profile_scope: str,
                        source_region: str) -> tuple[str, ...]:
    """The regions a call to `model_id` from `source_region` can be served in, read
    with `GetInferenceProfile` and parsed by the same code discovery uses
    (`records.destination_regions_from_models`, `reach_from_destinations`), so the
    release check and discovery cannot disagree about one profile.

    `bedrock_control` is a boto3 `bedrock` (control-plane) client for
    `source_region`. Raises `ReachUnknown` when Bedrock does not answer with
    destinations."""
    if profile_scope == "no_profile":
        return reach_from_destinations((), profile_scope=profile_scope, source_region=source_region)
    try:
        profile = bedrock_control.get_inference_profile(inferenceProfileIdentifier=model_id)
    except Exception as exc:  # noqa: BLE001 - any failure means "not known", never "local"
        raise ReachUnknown(f"{model_id} from {source_region}: {exc}") from exc
    reach = reach_from_destinations(destination_regions_from_models(profile.get("models")),
                                    profile_scope=profile_scope, source_region=source_region)
    if reach is None:
        raise ReachUnknown(f"{model_id} from {source_region}: no destinations listed")
    return reach
