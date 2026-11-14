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
    """The region the gateway calls Bedrock's Converse API from. The same expression
    `mvp._bedrock_clients.deployment_client` and `mvp.routing.clients.default_region`
    use."""
    return os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION") or "us-east-1"


def require_region(region: str) -> None:
    """Refuse a Bedrock runtime client for a region outside the boundary. Called by
    every factory that builds one, so no code path can reach a region the boundary
    does not allow, whatever entry or setting led it there."""
    boundary = configured()
    if boundary is not None and region not in boundary:
        raise BoundaryConfigError(
            f"refusing a Bedrock client for {region}: outside {ENV} {sorted(boundary)}")


@dataclass(frozen=True)
class Exclusion:
    """An entry left out of the composed registry, and why."""

    bedrock_model_id: str
    reason: str


def exclusion_reason(entry, *, boundary: Optional[frozenset[str]], primary: str) -> Optional[str]:
    """Why `entry` is not part of this deployment, or None when it is.

    With a boundary: the entry's recorded reach must be known, bounded and inside it,
    and a Converse entry must have been recorded for the region the gateway calls
    from (a profile's destinations depend on the source region).

    Without a boundary nothing is left out here; `compose` only chooses among
    variants of one model that share a name."""
    if getattr(entry, "served_by", "bedrock") != "bedrock":
        return None  # a self-hosted endpoint is not Bedrock inference
    reach = getattr(entry, "inference_regions", None)
    converse = entry.wire_protocol == "messages"
    if boundary is None:
        return None
    if not reach:
        return "its destination regions were never recorded"
    if UNBOUNDED in reach:
        return "it can be served in any region"
    outside = sorted(set(reach) - boundary)
    if outside:
        return f"it can be served in {outside}, outside the boundary"
    if converse and entry.bedrock_region != primary:
        return (f"it was recorded for calls from {entry.bedrock_region}; this deployment "
                f"calls Bedrock from {primary}")
    if not converse and entry.bedrock_region not in boundary:
        return f"it is called in {entry.bedrock_region}, outside the boundary"
    return None


def reach_from_destinations(destinations, *, profile_scope: str,
                            source_region: str) -> Optional[tuple[str, ...]]:
    """`ModelEntry.inference_regions` from a discovered record's destination regions
    (`mvp.discovery.records.destination_regions_from_models`), which keep the
    region-less ARN of a `global.` profile as "" (any region). A direct model id
    is served where it is called. None when nothing was recorded."""
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
    """`exclusion_reason` for a model that is still a discovered record or a
    candidate, so promotion and the verification probe refuse it before anything
    is written or any prompt is sent, rather than only at activation."""
    from types import SimpleNamespace

    probe = SimpleNamespace(
        served_by="bedrock", bedrock_region=bedrock_region, wire_protocol=wire_protocol,
        profile_scope=profile_scope,
        inference_regions=reach_from_destinations(destination_regions, profile_scope=profile_scope,
                                                  source_region=bedrock_region))
    return exclusion_reason(probe, boundary=configured(), primary=primary_region())


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
    that shares no name is never left out, so a deployment with no boundary and no
    shared names serves exactly what it served before."""
    entries = tuple(entries)
    preferred: dict[str, object] = {}
    if boundary is None:
        def rank(item):
            index, e = item
            reach = getattr(e, "inference_regions", None) or ()
            return (0 if primary in reach else 1, index)

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
        reason = exclusion_reason(entry, boundary=boundary, primary=primary)
        if reason is None:
            winner = next((preferred[a] for a in entry.aliases
                           if a in preferred and preferred[a] is not entry), None)
            if winner is not None:
                reason = (f"another variant of this model, {winner.bedrock_model_id}, serves "
                          f"its name from {primary}")
        if reason is None:
            clash = next((a for a in entry.aliases if a in owner), None)
            if clash is not None:
                reason = f"{ALIAS_CONFLICT}: alias {clash!r} is already served by {owner[clash]}"
        if reason is not None:
            excluded.append(Exclusion(entry.bedrock_model_id, reason))
            continue
        for alias in entry.aliases:
            owner[alias] = entry.bedrock_model_id
        kept.append(entry)
    return tuple(kept), tuple(excluded)


ALIAS_CONFLICT = "alias conflict"


def check_startup(*, entries: Iterable, exclusions: Iterable[Exclusion],
                  default_model: str) -> None:
    """Refuse a deployment whose boundary contradicts the rest of its settings.
    Called once at startup, before traffic. Does nothing when no boundary is set."""
    boundary = configured()
    if boundary is None:
        return
    primary = primary_region()
    if primary not in boundary:
        raise BoundaryConfigError(
            f"BEDROCK_REGION {primary} is outside {ENV} {sorted(boundary)}")
    entries = tuple(entries)
    if not entries:
        raise BoundaryConfigError(
            f"no model in the registry can be served inside {ENV} {sorted(boundary)}")
    conflicts = [e for e in exclusions if e.reason.startswith(ALIAS_CONFLICT)]
    if conflicts:
        raise BoundaryConfigError(
            "two models inside the boundary share a name: "
            + "; ".join(f"{e.bedrock_model_id} ({e.reason})" for e in conflicts))
    if default_model not in {e.bedrock_model_id for e in entries}:
        why = next((e.reason for e in exclusions if e.bedrock_model_id == default_model),
                   "it is not in the model registry")
        raise BoundaryConfigError(
            f"DEFAULT_BEDROCK_MODEL {default_model} cannot be served inside {ENV}: {why}; "
            f"set it to a model that can")
