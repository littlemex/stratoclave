"""Property tests for the routing-config admin API (P0-11 write path).

Seam under test: the PUT validator/serializer in mvp.routing.admin_api must
agree with the READ side in mvp.routing.config.

P1  Tenant round-trip: validator accepts req  =>  tenant_config_to_item(req)
    parses via config._parse_tenant_config into EXACTLY the intended
    RoutingConfig (not merely "parses without error" -- the parser is lenient,
    so equality is the real property).
P2  User round-trip: same for user configs, including the PK format
    CONFIG#ROUTING#USER#<uid> and the chain-subsequence constraint.
P3  Rejection: unknown model ids (error names the offender), duplicate chain
    entries (alias-aware), chain outside a non-empty allowlist, free_tier
    outside allowlist, non-subsequence / empty user chains, negative quota
    limits, unknown fields, bad enum values.
P4  Differential: _is_subsequence vs a reference implementation, and
    mask-derived sublists are always subsequences.
"""
import pytest
from hypothesis import assume, given, settings, strategies as st
from pydantic import ValidationError

from mvp.models import _ALIAS_MAP, resolve_model
from mvp.routing import config as routing_config
from mvp.routing.config import ModelQuotaConfig, RoutingConfig, UserRoutingConfig
from mvp.admin_routing import (
    ModelQuota,
    RoutingValidationError,
    TenantRoutingConfigRequest,
    UserRoutingConfigRequest,
    _is_subsequence,
    tenant_config_to_item,
    user_config_to_item,
    validate_tenant_routing,
    validate_user_routing,
)

KNOWN_MODELS = sorted(_ALIAS_MAP.keys())
BOGUS_MODEL = "model-that-does-not-exist-zz9"


def canon(model_id: str) -> int:
    """Canonical identity of a model: registry entries are shared objects, so
    object identity collapses alias / bedrock-id spellings of the same model."""
    return id(resolve_model(model_id))


# ---- module guards ---------------------------------------------------------
try:
    resolve_model(BOGUS_MODEL)
    raise RuntimeError("BOGUS_MODEL unexpectedly resolves; pick another string")
except ValueError:
    pass

if len({canon(m) for m in KNOWN_MODELS}) < 2:
    pytest.skip("registry has <2 distinct models", allow_module_level=True)


def canon_id(model_id: str) -> str:
    """The canonical stored spelling (entry.aliases[0]). The admin write path
    canonicalizes every model id, so the round-trip property is
    'canonical in -> identical out' (Fable rev1 F1 fix)."""
    entry = resolve_model(model_id)
    return entry.aliases[0] if entry.aliases else model_id


# ---- strategies ------------------------------------------------------------
model_ids = st.sampled_from(KNOWN_MODELS)


def model_lists(**kw):
    return st.lists(model_ids, unique_by=canon, **kw)


quota_st = st.builds(
    ModelQuota,
    unit=st.just("usd_micro"),  # only usd_micro is accepted (P0-11 caps micro-USD)
    limit=st.one_of(st.none(), st.integers(min_value=0, max_value=10**15)),
    period=st.just("monthly"),  # P0-11 enforces monthly only
)


def _merge_unique(first: list[str], second: list[str]) -> list[str]:
    seen, out = set(), []
    for m in first + second:
        k = canon(m)
        if k not in seen:
            seen.add(k)
            out.append(m)
    return out


@st.composite
def tenant_requests(draw) -> TenantRoutingConfigRequest:
    """Generate requests that satisfy the documented coherence rules."""
    chain = draw(model_lists(max_size=5))
    if draw(st.booleans()):
        extra = draw(model_lists(max_size=3))
        allowlist = _merge_unique(chain, extra)  # superset of chain
    else:
        allowlist = []  # empty = unrestricted
    free_pool = allowlist or KNOWN_MODELS
    free_tier = draw(st.one_of(st.none(), st.sampled_from(free_pool)))
    quota_models = draw(model_lists(max_size=4))
    quotas = {m: draw(quota_st) for m in quota_models}
    return TenantRoutingConfigRequest(
        allowlist=allowlist,
        chain=chain,
        quotas=quotas,
        fallback_mode=draw(st.sampled_from(["loud", "silent"])),
        fallback_default=draw(st.sampled_from(["on", "off"])),
        free_tier_model=free_tier,
    )


@st.composite
def user_cases(draw):
    """A tenant RoutingConfig (built through the real serialize->parse path)
    plus a coherent user request whose chain is a mask-derived subsequence."""
    treq = draw(tenant_requests())
    tenant_cfg = routing_config._parse_tenant_config(tenant_config_to_item("t", treq))
    chain = None
    if treq.chain and draw(st.booleans()):
        mask = draw(st.lists(st.booleans(), min_size=len(treq.chain), max_size=len(treq.chain)))
        sub = [m for m, keep in zip(treq.chain, mask) if keep]
        chain = sub or None  # empty list is rejected by design; use None
    pref_pool = list(treq.allowlist) or KNOWN_MODELS
    preferred = draw(st.one_of(st.none(), st.sampled_from(pref_pool)))
    fallback = draw(st.sampled_from([None, "on", "off"]))
    return tenant_cfg, UserRoutingConfigRequest(
        preferred_model=preferred, chain=chain, fallback=fallback
    )


# ---- P1: tenant round-trip -------------------------------------------------
@settings(max_examples=200, deadline=None)
@given(tenant_requests())
def test_tenant_roundtrip_exact(req):
    validate_tenant_routing(req)  # must accept: generated to satisfy the rules
    item = tenant_config_to_item("tenant-1", req, updated_by="test-admin")

    # Exact key shape config.py reads:
    assert item["user_id"] == "CONFIG#ROUTING"
    assert item["tenant_id"] == "tenant-1"

    parsed = routing_config._parse_tenant_config(item)
    expected = RoutingConfig(
        allowlist=tuple(canon_id(m) for m in req.allowlist),
        chain=tuple(canon_id(m) for m in req.chain),
        quotas={
            canon_id(m): ModelQuotaConfig(
                model=canon_id(m), unit=q.unit, limit=q.limit, period=q.period
            )
            for m, q in req.quotas.items()
        },
        fallback_mode=req.fallback_mode,
        fallback_default=req.fallback_default,
        free_tier_model=canon_id(req.free_tier_model) if req.free_tier_model else None,
    )
    assert parsed == expected


def test_alias_and_bedrock_id_canonicalize_to_same_stored_key():
    """Fable rev1 F1: a model written as an alias in the chain and as its
    bedrock id in the quotas dict must land under the SAME stored key, so the
    enforcement layer's quotas.get(chain_entry) can never miss."""
    entry = resolve_model(KNOWN_MODELS[0])
    alias = entry.aliases[0]
    bedrock = entry.bedrock_model_id
    if alias == bedrock:
        pytest.skip("model has no distinct bedrock-id spelling")
    req = TenantRoutingConfigRequest(
        chain=[alias],
        quotas={bedrock: ModelQuota(limit=0)},  # different spelling, same model
    )
    validate_tenant_routing(req)
    item = tenant_config_to_item("t", req)
    # both normalized to the canonical id -> the quota keys the chain entry
    assert item["chain"] == [alias]
    assert list(item["quotas"].keys()) == [alias]


def test_empty_request_roundtrips_to_defaults():
    """Backward compat: writing an all-defaults config parses to RoutingConfig()."""
    req = TenantRoutingConfigRequest()
    validate_tenant_routing(req)
    parsed = routing_config._parse_tenant_config(tenant_config_to_item("t", req))
    assert parsed == RoutingConfig()


# ---- P2: user round-trip ----------------------------------------------------
@settings(max_examples=200, deadline=None)
@given(user_cases())
def test_user_roundtrip_exact(case):
    tenant_cfg, req = case
    validate_user_routing(req, tenant_cfg)
    item = user_config_to_item("tenant-1", "user-42", req, updated_by="test-admin")

    assert item["user_id"] == "CONFIG#ROUTING#USER#user-42"
    assert item["tenant_id"] == "tenant-1"

    parsed = routing_config._parse_user_config(item)
    assert parsed == UserRoutingConfig(
        preferred_model=canon_id(req.preferred_model) if req.preferred_model else None,
        chain=tuple(canon_id(m) for m in req.chain) if req.chain else None,
        fallback=req.fallback,
    )


# ---- P3: rejection ----------------------------------------------------------
@settings(deadline=None)
@given(tenant_requests(), st.data())
def test_unknown_model_in_chain_rejected_naming_offender(req, data):
    pos = data.draw(st.integers(min_value=0, max_value=len(req.chain)))
    chain = list(req.chain)
    chain.insert(pos, BOGUS_MODEL)
    bad = req.model_copy(update={"chain": chain})
    with pytest.raises(RoutingValidationError) as exc:
        validate_tenant_routing(bad)
    assert BOGUS_MODEL in str(exc.value)


@given(model_ids)
def test_duplicate_chain_model_rejected(m):
    with pytest.raises(RoutingValidationError, match="duplicate"):
        validate_tenant_routing(TenantRoutingConfigRequest(chain=[m, m]))


@given(model_ids, model_ids)
def test_chain_model_outside_nonempty_allowlist_rejected(a, b):
    assume(canon(a) != canon(b))
    with pytest.raises(RoutingValidationError, match="allowlist"):
        validate_tenant_routing(TenantRoutingConfigRequest(allowlist=[a], chain=[b]))


@given(model_ids, model_ids)
def test_free_tier_outside_nonempty_allowlist_rejected(a, ft):
    assume(canon(a) != canon(ft))
    with pytest.raises(RoutingValidationError, match="free_tier"):
        validate_tenant_routing(
            TenantRoutingConfigRequest(allowlist=[a], chain=[a], free_tier_model=ft)
        )


@settings(deadline=None)
@given(st.data())
def test_non_subsequence_user_chain_rejected(data):
    tchain = data.draw(model_lists(min_size=2, max_size=5))
    perm = list(data.draw(st.permutations(tchain)))
    assume(perm != tchain)  # equal-length non-identical => not a subsequence
    tenant_cfg = RoutingConfig(chain=tuple(tchain))
    with pytest.raises(RoutingValidationError, match="subsequence"):
        validate_user_routing(UserRoutingConfigRequest(chain=perm), tenant_cfg)


@settings(deadline=None)
@given(st.data())
def test_user_chain_model_not_in_tenant_chain_rejected(data):
    tchain = data.draw(model_lists(min_size=1, max_size=4))
    outsider = data.draw(model_ids)
    assume(canon(outsider) not in {canon(m) for m in tchain})
    tenant_cfg = RoutingConfig(chain=tuple(tchain))
    with pytest.raises(RoutingValidationError, match="subsequence"):
        validate_user_routing(
            UserRoutingConfigRequest(chain=tchain + [outsider]), tenant_cfg
        )


def test_empty_user_chain_list_rejected():
    tenant_cfg = RoutingConfig(chain=(KNOWN_MODELS[0],))
    with pytest.raises(RoutingValidationError, match="empty"):
        validate_user_routing(UserRoutingConfigRequest(chain=[]), tenant_cfg)


def test_negative_quota_limit_rejected():
    with pytest.raises(ValidationError):
        ModelQuota(limit=-1)


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        TenantRoutingConfigRequest(surprise_field=1)
    with pytest.raises(ValidationError):
        UserRoutingConfigRequest(chian=["typo"])


def test_token_unit_quota_rejected():
    # Fable rev2 F5: unit="tokens" would be silently reinterpreted as micro-USD
    # (a x10^6 misconfig), so the schema rejects it until tokens are enforced.
    with pytest.raises(ValidationError):
        ModelQuota(unit="tokens", limit=1_000_000)


def test_bad_enums_rejected():
    with pytest.raises(ValidationError):
        TenantRoutingConfigRequest(fallback_default="maybe")
    with pytest.raises(ValidationError):
        TenantRoutingConfigRequest(fallback_mode="quietish")
    with pytest.raises(ValidationError):
        UserRoutingConfigRequest(fallback="sometimes")


# ---- P4: subsequence differential -------------------------------------------
@given(st.lists(st.integers(0, 5), max_size=8), st.data())
def test_mask_derived_sublists_are_subsequences(full, data):
    mask = data.draw(st.lists(st.booleans(), min_size=len(full), max_size=len(full)))
    sub = [x for x, keep in zip(full, mask) if keep]
    assert _is_subsequence(sub, full)


@given(st.lists(st.integers(0, 3), max_size=6), st.lists(st.integers(0, 3), max_size=6))
def test_is_subsequence_matches_reference(sub, full):
    def ref(s, f):
        i = 0
        for x in f:
            if i < len(s) and s[i] == x:
                i += 1
        return i == len(s)

    assert _is_subsequence(sub, full) == ref(sub, full)