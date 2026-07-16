"""Property-based invariants for the failover catalog (Fable review, formal-lite).

Fable's guidance: the failover state space is small enough that a full Z3 model
is low-ROI, but the catalog-construction invariants are exactly the kind of thing
a residency reviewer must trust. These properties pin them across arbitrary
region configurations:

  P1  The primary is always `BEDROCK_REGION` (the MODEL region) — NEVER the
      task's `AWS_REGION` (the deploy region). This is the region-decoupling
      contract: a mismatch would call Bedrock in the wrong region.
  P2  Every catalog target list starts with the primary (target 0).
  P3  No region appears twice in a target list (dedupe).
  P4  Residency: when STRATOCLAVE_FAILOVER_REGIONS is UNSET, no catalog region
      is in a different jurisdiction than the primary (the back-door-leak fix).
  P5  A disable sentinel / empty value yields a single-region catalog.
"""
from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mvp.routing.chains import (
    _jurisdiction,
    failover_regions,
    get_catalog,
    reset_catalog,
)

# Each example explicitly sets every region env var it depends on (via
# monkeypatch) and calls reset_catalog(), so the function-scoped fixture not
# re-running per example is intentional and safe.
_SETTINGS = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

# A small pool of real-shaped region ids spanning several jurisdictions.
REGIONS = [
    "us-east-1", "us-west-2", "us-east-2",
    "eu-west-1", "eu-central-1", "eu-west-2",
    "ap-northeast-1", "ap-southeast-2",
    "ca-central-1", "sa-east-1",
]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("STRATOCLAVE_FAILOVER_REGIONS", raising=False)
    monkeypatch.delenv("BEDROCK_REGION", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    reset_catalog()
    yield
    reset_catalog()


def _all_catalog_regions() -> set[str]:
    return {t.region for ts in get_catalog().values() for t in ts}


@_SETTINGS
@given(primary=st.sampled_from(REGIONS), deploy=st.sampled_from(REGIONS))
def test_primary_is_model_region_not_deploy_region(monkeypatch, primary, deploy):
    # P1 + P4 (unset failover). BEDROCK_REGION is the model region; AWS_REGION is
    # the deploy region. The catalog primary must follow BEDROCK_REGION.
    monkeypatch.setenv("BEDROCK_REGION", primary)
    monkeypatch.setenv("AWS_REGION", deploy)  # deliberately (maybe) different
    monkeypatch.delenv("STRATOCLAVE_FAILOVER_REGIONS", raising=False)
    reset_catalog()

    catalog = get_catalog()
    assert catalog, "catalog empty — invariant would be vacuous"
    for targets in catalog.values():
        # P2: primary first.
        assert targets[0].region == primary
        # P3: no dupes.
        regions = [t.region for t in targets]
        assert len(regions) == len(set(regions))
    # P4: with failover unset, no region leaves the primary's jurisdiction.
    juris = _jurisdiction(primary)
    for r in _all_catalog_regions():
        assert _jurisdiction(r) == juris, (
            f"catalog region {r} leaves primary jurisdiction {juris} "
            f"(primary={primary}, deploy={deploy})"
        )


@_SETTINGS
@given(
    primary=st.sampled_from(REGIONS),
    alts=st.lists(st.sampled_from(REGIONS), min_size=0, max_size=4),
)
def test_explicit_list_dedup_and_primary_stripped(monkeypatch, primary, alts):
    # An explicit list: primary always stripped, deduped, order preserved, and
    # the primary is still target 0 in the catalog.
    monkeypatch.setenv("BEDROCK_REGION", primary)
    monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", ",".join(alts))
    reset_catalog()

    fo = failover_regions()
    assert primary not in fo
    assert len(fo) == len(set(fo))  # deduped
    # Order preserved relative to the (deduped, primary-stripped) input.
    expected = []
    seen = {primary}
    for a in alts:
        if a not in seen:
            seen.add(a)
            expected.append(a)
    assert fo == expected

    for targets in get_catalog().values():
        assert targets[0].region == primary
        regions = [t.region for t in targets]
        assert len(regions) == len(set(regions))


@_SETTINGS
@given(
    primary=st.sampled_from(REGIONS),
    sentinel=st.sampled_from(["", "none", "disabled", "off", "  OFF  ", ",", " , "]),
)
def test_sentinel_yields_single_region_catalog(monkeypatch, primary, sentinel):
    # P5: any disable sentinel / empty / comma-only value → single-region.
    monkeypatch.setenv("BEDROCK_REGION", primary)
    monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", sentinel)
    reset_catalog()

    assert failover_regions() == []
    assert _all_catalog_regions() == {primary}
