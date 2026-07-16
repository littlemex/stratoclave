"""Drift guard for the OpenAI/codex region residency contract.

The IaC residency analysis (`iac/bin/iac.ts::OPENAI_REGISTRY_REGIONS`) hardcodes
the set of regions the codex path calls Bedrock in, because it cannot import this
Python registry at CDK synth time. That IaC constant is what decides whether
`STRATOCLAVE_RESIDENCY=strict` passes or fails and what remediation the residency
warning prints. If someone adds an OpenAI model in a new region here WITHOUT
updating the IaC constant, strict mode would silently certify a residency posture
that no longer holds (the exact NEW-1 failure class).

This test pins the source of truth. When it fails, update BOTH:
  * this expected set, and
  * `OPENAI_REGISTRY_REGIONS` in iac/bin/iac.ts
so the residency analysis keeps telling the truth.

It also asserts the invariant the residency analysis relies on: the OpenAI region
comes from the registry entry (`entry.bedrock_region`), NOT from the
`OPENAI_BEDROCK_REGIONS` env var (which is a display-only hint).
"""
from __future__ import annotations

import os

from mvp.models import _REGISTRY

# Must equal OPENAI_REGISTRY_REGIONS in iac/bin/iac.ts (order-independent).
EXPECTED_OPENAI_REGIONS = {"us-east-2", "us-west-2"}


def test_openai_registry_regions_match_iac_constant():
    actual = {e.bedrock_region for e in _REGISTRY if e.provider == "openai"}
    assert actual == EXPECTED_OPENAI_REGIONS, (
        f"OpenAI registry regions changed to {sorted(actual)}. Update "
        f"OPENAI_REGISTRY_REGIONS in iac/bin/iac.ts AND EXPECTED_OPENAI_REGIONS "
        f"here, or STRATOCLAVE_RESIDENCY=strict will certify a stale posture."
    )


def test_default_failover_regions_match_iac_constant():
    # _DEFAULT_FAILOVER_REGIONS is duplicated in the IaC residency analysis
    # (iac/lib/region-config.ts::DEFAULT_FAILOVER_REGIONS). Drift would make the
    # CDK residency warnings/strict-mode reason about a different default set than
    # the backend actually uses. Keep them in sync (Fable final review Q1).
    from mvp.routing.chains import _DEFAULT_FAILOVER_REGIONS

    assert set(_DEFAULT_FAILOVER_REGIONS) == {"us-west-2", "eu-west-1"}, (
        f"Backend _DEFAULT_FAILOVER_REGIONS changed to {_DEFAULT_FAILOVER_REGIONS}. "
        f"Update DEFAULT_FAILOVER_REGIONS in iac/lib/region-config.ts to match."
    )


def test_openai_region_is_not_driven_by_env_hint(monkeypatch):
    # OPENAI_BEDROCK_REGIONS must NOT change any registry entry's region — it is
    # a display-only hint. A same-process re-read of the already-imported module
    # would be a tautology (the frozen tuple can't change), so we RELOAD the
    # module WITH the env var set and assert the regions are still the literals.
    # This actually exercises module-load-time behavior: if mvp/models.py ever
    # started reading OPENAI_BEDROCK_REGIONS at import, this would catch it and
    # the IaC residency analysis (which ignores the var) would need updating.
    import importlib
    import sys

    monkeypatch.setenv("OPENAI_BEDROCK_REGIONS", "eu-west-1")
    # Drop cached modules so the reload re-executes top-level registry code.
    for name in list(sys.modules):
        if name == "mvp.models" or name.startswith("mvp.models."):
            del sys.modules[name]
    reloaded = importlib.import_module("mvp.models")
    try:
        regions = {
            e.bedrock_region for e in reloaded._REGISTRY if e.provider == "openai"
        }
        assert regions == EXPECTED_OPENAI_REGIONS, (
            "OPENAI_BEDROCK_REGIONS moved the codex registry regions on reload — "
            "the IaC residency analysis ignores that var and would now be wrong."
        )
    finally:
        # Restore a clean import for the rest of the suite.
        importlib.reload(reloaded)


def test_every_openai_region_is_a_valid_region_id():
    # Matches the assertRegion regex in iac/bin/iac.ts so a malformed registry
    # region would be caught here too.
    import re

    pat = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
    for e in _REGISTRY:
        if e.provider == "openai":
            assert pat.match(e.bedrock_region), e.bedrock_region
