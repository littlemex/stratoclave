"""The inference boundary: the set of AWS regions Bedrock inference may run in.

An operator sets `STRATOCLAVE_INFERENCE_REGIONS` to a comma-separated region list
(Japan: `ap-northeast-1,ap-northeast-3`; US: `us-east-1,us-east-2,us-west-2`).
Unset means no boundary, and everything below reduces to today's behaviour.

The one rule: with a boundary set, a model entry is part of this deployment only if
every region a call through it can be served in (`ModelEntry.inference_regions`,
read from Bedrock when the entry was recorded) lies inside the boundary, and the
gateway calls it from inside the boundary. Everything else is the same rule applied
where a region or a model enters: the composed registry drops entries that fail it,
the client factories refuse a region outside it, and startup refuses a deployment
that contradicts it.

Who enforces what:

* AWS. The serving role's Bedrock permissions are generated from the same list by
  the IaC. AWS authorises a geo inference profile against every destination
  region's foundation-model ARN before the call (measured 2026-11-14: with the
  Osaka grant removed, `jp.*` was denied 60 of 60 times), so a model reaching
  outside the boundary is refused by AWS even if the data here were wrong.
* The gateway. Lists only what it can serve, and refuses before money is reserved,
  so a tenant sees "model not available" rather than a provider error.
* The operator. Where a call was actually served is reported only afterwards, as
  `inferenceRegion` in Bedrock's model invocation log; auditing that is the
  operator's (docs/DEPLOYMENT.md gives the query).

This module holds the rule and the parsing. It knows no geography by name: a
boundary is a list of regions and reach is data recorded per entry.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable, Optional

ENV = "STRATOCLAVE_INFERENCE_REGIONS"

#: In `ModelEntry.inference_regions`, marks an entry whose calls may be served in
#: any region (a `global.` profile: Bedrock lists its destination as the
#: region-less foundation-model ARN).
UNBOUNDED = "*"

_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")


class BoundaryConfigError(RuntimeError):
    """The boundary, or the deployment around it, is not a coherent setting."""


def region_is_valid(value: object) -> bool:
    return isinstance(value, str) and _REGION_RE.fullmatch(value) is not None


def parse(raw: Optional[str]) -> Optional[frozenset[str]]:
    """The boundary from its environment value. None when unset. A value that is set
    but empty, has a blank item, a malformed region or a repeat is an error, never
    "no boundary": a typo must not silently run unbounded."""
    if raw is None:
        return None
    items = [part.strip() for part in raw.split(",")]
    if not raw.strip() or any(not part for part in items):
        raise BoundaryConfigError(f"{ENV} is set but has an empty item: {raw!r}")
    bad = [part for part in items if not region_is_valid(part)]
    if bad:
        raise BoundaryConfigError(f"{ENV} has values that are not AWS region ids: {bad}")
    if len(set(items)) != len(items):
        raise BoundaryConfigError(f"{ENV} repeats a region: {raw!r}")
    return frozenset(items)


def configured() -> Optional[frozenset[str]]:
    """The deployment's boundary, read from the environment on every call (tests and
    an operator flipping the variable see the change without a reload)."""
    return parse(os.getenv(ENV))


def primary_region() -> str:
    """The region the gateway calls Bedrock's Converse API from (`BEDROCK_REGION`,
    else `AWS_REGION`, else us-east-1). The one definition:
    `mvp._bedrock_clients.deployment_client` and
    `mvp.routing.clients.default_region` call this."""
    return os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION") or "us-east-1"


class RegionOutsideBoundary(RuntimeError):
    """A Bedrock client was asked for a region outside the boundary."""


def require_region(region: str) -> None:
    """Refuse a Bedrock runtime client for a region outside the boundary. Called by
    every factory that builds one, so no code path can reach a region the boundary
    does not allow, whatever entry or setting led it there."""
    boundary = configured()
    if boundary is not None and region not in boundary:
        raise RegionOutsideBoundary(
            f"refusing a Bedrock client for {region}: outside {ENV} {sorted(boundary)}")


#: Why an entry is left out (`Exclusion.kind`).
OUTSIDE = "outside_boundary"          # the boundary rule
VARIANT = "other_variant"             # another variant of the model serves the name
ALIAS_CONFLICT = "alias_conflict"     # two in-boundary entries claim one name


@dataclass(frozen=True)
class Exclusion:
    """An entry left out of the composed registry, and why."""

    bedrock_model_id: str
    kind: str
    reason: str


def calls_from_primary(wire_protocol: str) -> bool:
    """Whether the gateway calls an entry from `primary_region()` rather than from
    the entry's own `bedrock_region`: true for Converse (`messages`, routed by
    `mvp.routing.chains` from the deployment's region), false for the
    OpenAI-compatible surface (`responses`, called in each entry's region by
    `mvp._openai_transport`)."""
    return wire_protocol == "messages"


def reach_reason(*, reach: Optional[tuple[str, ...]], bedrock_region: str,
                 from_primary: bool, boundary: Optional[frozenset[str]],
                 primary: str) -> Optional[str]:
    """The one rule, over plain values. Why a model with recorded destinations
    `reach`, recorded for calls from `bedrock_region`, is outside `boundary`, or
    None when it is inside (always None without a boundary).

    The destinations must be known, bounded and inside the boundary, and the
    gateway must call the model from the region its destinations were recorded
    for (a profile's destinations depend on the region it is called from)."""
    if boundary is None:
        return None
    if not reach:
        return "its destination regions were never recorded"
    if UNBOUNDED in reach:
        return "it can be served in any region"
    outside = sorted(set(reach) - boundary)
    if outside:
        return f"it can be served in {outside}, outside the boundary"
    called_in = primary if from_primary else bedrock_region
    if called_in != bedrock_region:
        return (f"its destinations were recorded for calls from {bedrock_region}; "
                f"this deployment calls it from {called_in}")
    if called_in not in boundary:
        return f"it is called in {called_in}, outside the boundary"
    return None


def exclusion_reason(entry, *, boundary: Optional[frozenset[str]], primary: str) -> Optional[str]:
    """`reach_reason` for a registry entry. A self-hosted entry is not Bedrock
    inference and is never left out by the boundary."""
    if entry.served_by != "bedrock":
        return None
    return reach_reason(reach=entry.inference_regions, bedrock_region=entry.bedrock_region,
                        from_primary=calls_from_primary(entry.wire_protocol),
                        boundary=boundary, primary=primary)


def reach_from_destinations(destinations, *, profile_scope: str,
                            source_region: str) -> Optional[tuple[str, ...]]:
    """`ModelEntry.inference_regions` from destination regions as
    `mvp.discovery.records.destination_regions_from_models` reads them from Bedrock,
    which keeps the region-less ARN of a `global.` profile as "" (any region). A
    direct model id is served where it is called. None when nothing was recorded."""
    if profile_scope == "no_profile":
        return (source_region,)
    regions = tuple(destinations or ())
    if not regions:
        return None
    if "" in regions:
        return (UNBOUNDED,)
    return tuple(sorted(set(regions)))


def discovered_exclusion_reason(*, bedrock_region: str, wire_protocol: str, profile_scope: str,
                                destination_regions) -> Optional[str]:
    """`reach_reason` for a model that is still a discovered record or a candidate,
    so promotion and the verification probe refuse it before anything is written or
    any prompt is sent, rather than only at activation."""
    return reach_reason(
        reach=reach_from_destinations(destination_regions, profile_scope=profile_scope,
                                      source_region=bedrock_region),
        bedrock_region=bedrock_region, from_primary=calls_from_primary(wire_protocol),
        boundary=configured(), primary=primary_region())


def compose(entries: Iterable, *, boundary: Optional[frozenset[str]], primary: str,
            ) -> tuple[tuple, tuple[Exclusion, ...]]:
    """The entries that are part of this deployment, in their original order, and
    the ones left out with the reason.

    Also settles entries that share an alias (the `us.` and `jp.` variants of one
    model are both named `claude-opus-4-7`, so Claude Code keeps working unchanged).
    With a boundary the rule above leaves at most the in-boundary variants, and if
    two remain the later one is left out as an `ALIAS_CONFLICT`, which startup
    refuses. Without a boundary, among the variants sharing a name the one whose
    destinations include `primary` is served, else the first declared; an entry
    that shares no name is never left out."""
    entries = tuple(entries)
    preferred: dict[str, object] = {}
    if boundary is None:
        def rank(item):
            index, e = item
            return (0 if primary in (e.inference_regions or ()) else 1, index)

        claims: dict[str, list] = {}
        for index, e in enumerate(entries):
            for alias in e.aliases:
                claims.setdefault(alias, []).append((index, e))
        preferred = {alias: min(group, key=rank)[1]
                     for alias, group in claims.items() if len(group) > 1}
    kept: list = []
    excluded: list[Exclusion] = []
    owner: dict[str, str] = {}
    for entry in entries:
        found: Optional[Exclusion] = None
        reason = exclusion_reason(entry, boundary=boundary, primary=primary)
        if reason is not None:
            found = Exclusion(entry.bedrock_model_id, OUTSIDE, reason)
        if found is None:
            winner = next((preferred[a] for a in entry.aliases
                           if a in preferred and preferred[a] is not entry), None)
            if winner is not None:
                found = Exclusion(entry.bedrock_model_id, VARIANT,
                                  f"another variant of this model, {winner.bedrock_model_id}, "
                                  f"serves its name from {primary}")
        if found is None:
            clash = next((a for a in entry.aliases if a in owner), None)
            if clash is not None:
                found = Exclusion(entry.bedrock_model_id, ALIAS_CONFLICT,
                                  f"alias {clash!r} is already served by {owner[clash]}")
        if found is not None:
            excluded.append(found)
            continue
        for alias in entry.aliases:
            owner[alias] = entry.bedrock_model_id
        kept.append(entry)
    return tuple(kept), tuple(excluded)


def check_startup(*, entries: Iterable, exclusions: Iterable[Exclusion],
                  default_model: str) -> None:
    """Refuse a deployment whose boundary contradicts the rest of its settings.
    Called once at startup, before traffic. Without a boundary it only warns when
    the default model is a variant this region does not serve."""
    from core.logging import get_logger

    boundary = configured()
    exclusions = tuple(exclusions)
    entries = tuple(entries)
    served = {e.bedrock_model_id for e in entries}
    if boundary is None:
        if default_model not in served and any(
                e.bedrock_model_id == default_model for e in exclusions):
            get_logger(__name__).warning("default_model_not_served_here", default_model=default_model)
        return
    primary = primary_region()
    if primary not in boundary:
        raise BoundaryConfigError(
            f"the region the gateway calls Bedrock from ({primary}: BEDROCK_REGION, else "
            f"AWS_REGION) is outside {ENV} {sorted(boundary)}")
    if not entries:
        raise BoundaryConfigError(
            f"no model in the registry can be served inside {ENV} {sorted(boundary)}")
    conflicts = [e for e in exclusions if e.kind == ALIAS_CONFLICT]
    if conflicts:
        raise BoundaryConfigError(
            "two models inside the boundary share a name: "
            + "; ".join(f"{e.bedrock_model_id} ({e.reason})" for e in conflicts))
    if default_model not in served:
        why = next((e.reason for e in exclusions if e.bedrock_model_id == default_model),
                   "it is not in the model registry")
        raise BoundaryConfigError(
            f"DEFAULT_BEDROCK_MODEL {default_model} cannot be served inside {ENV}: {why}; "
            f"set it to a model that can")
