"""Two offline registry checks (C10, C13). No AWS credentials, no network.

Both are plain functions over the in-memory registry (or an explicitly-passed
one), so a test can call them directly instead of only observing them through
`load_registry`'s side effects. Each raises `ValueError` naming the offending
entries on failure and returns `None` on success — there is no partial result.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .models import ModelEntry, NO_PROFILE_SCOPE, registry_entries


# ---------------------------------------------------------------------------
# C10 — scope is a price point: two entries of one family at different scopes
# must not share a pricing_key.
# ---------------------------------------------------------------------------

def check_family_scope_pricing_keys_distinct(
    entries: Optional[Iterable[ModelEntry]] = None,
) -> None:
    """Raise if two entries share a `model_family` but differ in `profile_scope`
    while carrying the SAME `pricing_key`.

    Scope is a price point (measured: Claude Fable 5 is 11/55 in-region, 10/50
    global) — sharing a key across scopes would charge one scope at the other's
    rate. Two entries of one family at the SAME scope are already rejected by
    the registry loader itself (a `(model_family, profile_scope)` pair names at
    most one entry), so this only has something to check once a family spans
    more than one scope.
    """
    entries = tuple(entries) if entries is not None else registry_entries()
    by_family: dict[str, list[ModelEntry]] = {}
    for entry in entries:
        by_family.setdefault(entry.model_family, []).append(entry)

    for family, group in by_family.items():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if a.profile_scope == b.profile_scope:
                    continue  # same-scope duplicates are the loader's problem, not this check's.
                if a.pricing_key == b.pricing_key:
                    raise ValueError(
                        f"model_family {family!r} has entries at profile_scope "
                        f"{a.profile_scope!r} ({a.bedrock_model_id!r}) and "
                        f"{b.profile_scope!r} ({b.bedrock_model_id!r}) sharing "
                        f"pricing_key {a.pricing_key!r}; scope is a price point, "
                        f"so they need distinct keys"
                    )


# ---------------------------------------------------------------------------
# C13 — every declared profile_scope must be granted by the task role's Bedrock
# IAM policy in iac/lib/ecs-stack.ts, for that entry's vendor.
# ---------------------------------------------------------------------------

# `iac/lib/ecs-stack.ts` is READ ONLY for this check: it is parsed for the ARN
# patterns the CDK stack already declares, never edited and never re-derived
# from a second, hand-maintained map in Python (a hardcoded per-vendor
# allowlist would pass a test that never actually reads the file).
_DEFAULT_ECS_STACK_PATH = (
    Path(__file__).resolve().parent.parent.parent / "iac" / "lib" / "ecs-stack.ts"
)

# A backtick template literal containing a Bedrock ARN, e.g.
# `` `arn:aws:bedrock:*:${account}:inference-profile/us.anthropic.*` ``.
_ARN_TEMPLATE_RE = re.compile(r"`([^`]*arn:aws:bedrock[^`]*)`")
# `inference-profile/<scope>.<vendor>.*` — the scope/vendor pair an
# inference-profile grant names. The trailing `.*` is the model-name wildcard;
# it is not itself a scope or a vendor.
_INFERENCE_PROFILE_RE = re.compile(r"inference-profile/([^./]+)\.([^./]+)\.\*$")
# `<vendor>.*` remainder of a `foundation-model/<vendor>.*` literal (the
# "foundation-model/" prefix is already stripped before this is matched) —
# every model AWS publishes for that vendor.
_FOUNDATION_MODEL_WILDCARD_RE = re.compile(r"^([^./]+)\.\*$")
# `<arrayName>.map((modelId) => ...)` — the shape `ecs-stack.ts` uses to grant a
# LIST of exact foundation-model ids (nvidia, qwen) rather than a vendor
# wildcard. Matched so the exact ids can be read out of the array literal
# itself instead of being retyped in Python.
_MAP_CALL_RE = re.compile(r"(\w+)\.map\(\s*\(\s*modelId\s*\)\s*=>")


@dataclass(frozen=True)
class IamGrants:
    """What the ECS task role's Bedrock policy grants, parsed from `ecs-stack.ts`.

    `profile_scopes[vendor]` — profile_scope prefixes granted an
    inference-profile ARN for that vendor (e.g. `{"us", "global"}`).
    `foundation_model_wildcard_vendors` — vendors granted every foundation-model
    id under their prefix (`<vendor>.*`).
    `foundation_model_exact_ids[vendor]` — EXACT foundation-model ids granted
    for that vendor (the nvidia/qwen style grant, listed by id rather than by
    wildcard so onboarding a new one is a deliberate IaC edit).
    """

    profile_scopes: dict[str, frozenset[str]] = field(default_factory=dict)
    foundation_model_wildcard_vendors: frozenset[str] = frozenset()
    foundation_model_exact_ids: dict[str, frozenset[str]] = field(default_factory=dict)


def parse_ecs_stack_grants(source: str) -> IamGrants:
    """Extract `IamGrants` from the TEXT of `ecs-stack.ts`.

    Deliberately a text scan, not a TypeScript parser: the CDK file's ARN
    literals are backtick template strings with a small, stable shape (a
    resource-type segment, then either a `<scope>.<vendor>.*` or `<vendor>.*`
    wildcard, or `${modelId}` filled from a nearby array literal). Reading
    those shapes out of the file is what keeps this check from becoming a
    second, hand-maintained copy of the policy it is meant to verify against.
    """
    profile_scopes: dict[str, set[str]] = {}
    wildcard_vendors: set[str] = set()
    exact_ids: dict[str, set[str]] = {}

    for match in _ARN_TEMPLATE_RE.finditer(source):
        literal = match.group(1)
        if "inference-profile/" in literal:
            ip_match = _INFERENCE_PROFILE_RE.search(literal)
            if ip_match:
                scope, vendor = ip_match.group(1), ip_match.group(2)
                profile_scopes.setdefault(vendor, set()).add(scope)
            continue
        if "foundation-model/" in literal:
            remainder = literal.split("foundation-model/", 1)[1]
            if "${" in remainder:
                # A per-id template (`${modelId}`); resolved below from the
                # array literal its enclosing `.map()` call iterates.
                continue
            wc_match = _FOUNDATION_MODEL_WILDCARD_RE.search(remainder)
            if wc_match:
                wildcard_vendors.add(wc_match.group(1))
                continue
            # An exact id written directly as a literal (no `${...}` template).
            vendor = remainder.split(".", 1)[0]
            exact_ids.setdefault(vendor, set()).add(remainder)

    # Resolve every `<arrayName>.map((modelId) => ...foundation-model/${modelId}...)`
    # call by reading the array literal it maps over, so the exact ids granted
    # come from the IaC file's own data, not a Python restatement of it.
    for map_match in _MAP_CALL_RE.finditer(source):
        array_name = map_match.group(1)
        array_match = re.search(
            rf"\b{re.escape(array_name)}\s*(?::[^=]*)?=\s*\[(.*?)\]",
            source,
            re.DOTALL,
        )
        if not array_match:
            continue
        for id_match in re.finditer(r"['\"]([^'\"]+)['\"]", array_match.group(1)):
            model_id = id_match.group(1)
            vendor = model_id.split(".", 1)[0]
            exact_ids.setdefault(vendor, set()).add(model_id)

    return IamGrants(
        profile_scopes={vendor: frozenset(scopes) for vendor, scopes in profile_scopes.items()},
        foundation_model_wildcard_vendors=frozenset(wildcard_vendors),
        foundation_model_exact_ids={vendor: frozenset(ids) for vendor, ids in exact_ids.items()},
    )


def load_ecs_stack_grants(path: Optional[str] = None) -> IamGrants:
    """`parse_ecs_stack_grants` over the file at `path` (default: the real
    `iac/lib/ecs-stack.ts`, resolved relative to this module — `iac/` is read
    only, this never writes to it)."""
    resolved = Path(path) if path is not None else _DEFAULT_ECS_STACK_PATH
    if not resolved.exists():
        raise ValueError(f"ecs-stack.ts not found at {resolved}")
    return parse_ecs_stack_grants(resolved.read_text(encoding="utf-8"))


def check_profile_scopes_granted_by_iam(
    entries: Optional[Iterable[ModelEntry]] = None,
    *,
    grants: Optional[IamGrants] = None,
    ecs_stack_path: Optional[str] = None,
) -> None:
    """Raise if any entry's declared `profile_scope` is not actually grantable.

    An entry naming an inference profile is checked against
    `grants.profile_scopes[entry.provider]`. An entry naming a bare foundation
    model (`profile_scope == NO_PROFILE_SCOPE`) is checked against the
    foundation-model patterns instead — a vendor-wide wildcard, or its exact
    `bedrock_model_id` in the vendor's exact-id grant list.

    `grants` and `ecs_stack_path` are exclusive knobs for tests: pass `grants`
    to check against a fabricated policy without touching the filesystem, or
    `ecs_stack_path` to point at a fixture file. Neither is required for the
    ordinary (no-AWS, no-network) run, which reads the real `ecs-stack.ts`.
    """
    entries = tuple(entries) if entries is not None else registry_entries()
    if grants is None:
        grants = load_ecs_stack_grants(ecs_stack_path)

    for entry in entries:
        vendor = entry.provider
        if entry.profile_scope == NO_PROFILE_SCOPE:
            wildcard_ok = vendor in grants.foundation_model_wildcard_vendors
            exact_ok = entry.bedrock_model_id in grants.foundation_model_exact_ids.get(
                vendor, frozenset()
            )
            if not (wildcard_ok or exact_ok):
                raise ValueError(
                    f"{entry.bedrock_model_id!r} (provider={vendor!r}) names a bare "
                    f"foundation model but ecs-stack.ts grants neither a "
                    f"'{vendor}.*' foundation-model wildcard nor this exact id "
                    f"for {vendor!r}"
                )
            continue
        granted_scopes = grants.profile_scopes.get(vendor, frozenset())
        if entry.profile_scope not in granted_scopes:
            raise ValueError(
                f"{entry.bedrock_model_id!r} (provider={vendor!r}) declares "
                f"profile_scope {entry.profile_scope!r}, which ecs-stack.ts does "
                f"not grant an inference-profile ARN for; granted scopes for "
                f"{vendor!r}: {sorted(granted_scopes)}"
            )
