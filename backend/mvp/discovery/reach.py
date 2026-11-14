"""Where a call through a Bedrock model id can be served, as Bedrock itself reports it.

`ModelEntry.inference_regions` is this, recorded once per entry: by the release
check for the bundled registry (`scripts/check_inference_regions.py`) and by
discovery for a model an operator promotes. The inference boundary
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

from ..inference_boundary import UNBOUNDED


class ReachUnknown(Exception):
    """Bedrock did not say where this id is served (not found, or no destinations)."""


def _region_of(model_arn: str) -> str:
    # arn:<partition>:bedrock:<region>::foundation-model/<id>
    parts = model_arn.split(":")
    if len(parts) < 6 or parts[2] != "bedrock":
        raise ReachUnknown(f"not a Bedrock model ARN: {model_arn!r}")
    return parts[3]


def destination_regions(bedrock_control: Any, model_id: str, *, is_profile: bool,
                        source_region: str) -> tuple[str, ...]:
    """The regions a call to `model_id` from `source_region` can be served in.

    `bedrock_control` is a boto3 `bedrock` (control-plane) client for
    `source_region`. Returns `("*",)` for a profile with a region-less destination.
    Raises `ReachUnknown` when Bedrock does not answer with destinations."""
    if not is_profile:
        return (source_region,)
    try:
        profile = bedrock_control.get_inference_profile(inferenceProfileIdentifier=model_id)
    except Exception as exc:  # noqa: BLE001 - any failure means "not known", never "local"
        raise ReachUnknown(f"{model_id} from {source_region}: {exc}") from exc
    regions = {_region_of(m.get("modelArn", "")) for m in profile.get("models") or []}
    if not regions:
        raise ReachUnknown(f"{model_id} from {source_region}: no destinations listed")
    if "" in regions:
        return (UNBOUNDED,)
    return tuple(sorted(regions))
