"""E7 (the probe) and the parts of E8 (its metering ledger) observable
through a probe call: a response resolving through the entry's own pricing
key rather than `default`, the wire protocol actually spoken (not merely
declared), the charge landing on the system tenant and nowhere else, and the
system tenant's own submission to the same eligibility predicate every other
caller answers to.

**Ratified while this file was being written; every change below is already
applied, not left as the pre-ratification version.**

  - The verdict's (and the probe's) invocation axis is `invocation` (`"sync"`
    / `"stream"`), never the pricing axis's `mode` -- see
    `test_discovery_verdict.py`'s module docstring for why the two must not
    be confused.
  - `probe` takes `pricing_key` and `wire_protocol` as explicit keyword
    parameters -- a discovered record carries no price at all, and
    assertion 4 has nothing to resolve without one supplied. `record` is
    therefore the real, already-shipped `mvp.discovery.records.
    DiscoveredRecord` (E1), not an invented stand-in: nothing else about a
    candidate is needed to drive a probe call, now that price and protocol
    arrive as arguments.
  - A refusal is `mvp.discovery.probe.ProbeAttemptRefused(ValueError)`,
    **raised, never returned**, with `.reason` closed to `"probe_refused"`
    (a deliberate refusal by rate limit or scope -- this file constructs the
    scope case) and `"probe_unmetered"` (the probe could not obtain a
    metered hold at all -- the system tenant's cap is exhausted, the credit
    store is unavailable, or its identity was never provisioned). The
    cap-exhausted shape of `probe_unmetered` turned out to be cheaply
    constructible with the same pool-limit mechanism `seed_tenant_with_pool`
    already uses elsewhere in this suite, so it is pinned below rather than
    flagged as unconstructed.
  - `ProbeResult`'s fields are frozen: `passed`, `invocation`, `verdict`,
    `blocker`, `charged_microusd`. Every assertion below that used to be a
    tolerant scan for a subtype string is now a hard assertion on
    `result.blocker.subtype`. The four assertion failures that do NOT refuse
    the attempt -- the probe ran, and failed -- are blocker subtypes under
    `protocol_unverified`: `converse_call_failed` (assertion 1),
    `usage_counters_missing` (assertion 2), `counter_dimension_mismatch`
    (assertion 3), `charge_not_attributed` (assertion 4).

**Still committed here, not ratified, and flagged as such -- see
`test_discovery_records.py`'s own precedent for this exact move.** Neither
document names the exact Bedrock-client accessor `probe()` calls.
`_patch_bedrock_clients` below patches all three names
(`bedrock_runtime_client` / `client_for_model` / `deployment_client`) on both
`mvp._bedrock_clients` and (best-effort, non-raising) on
`mvp.discovery.probe`'s own namespace, to cover either import style without
guessing wrong silently. Nor does either document say how the system
tenant's pool and token balance come to exist; `_system_tenant_pool` seeds a
`UserTenants` membership and a `TenantBudgets` pool for `SYSTEM_TENANT_ID`
directly, under an invented user_id (`discovery-probe`) -- if the real
system tenant is provisioned some other way (a bootstrap seed, most
plausibly), that would show up as a setup-time failure, not a wrong verdict.

The fourth assertion is deliberately tested as an A/B pair that changes only
`pricing_key`, so what distinguishes the two outcomes is never in doubt --
this change's own verification plan asks for exactly this discipline
elsewhere (E6: "a floor comparison that only checks existence must fail a
test, since that is the defect it replaces"), and it applies just as much to
a probe that only checks for a 200.
"""
from __future__ import annotations

from typing import Any, Optional

import pytest
from botocore.exceptions import ClientError

from mvp.discovery.records import SYSTEM_TENANT_ID, DiscoveredRecord, ObservationScope

SYNC = "sync"

# A pricing key already present in the bundled floor, distinct from
# "default" -- `tests/test_metering_fault_settle.py` already relies on this
# same key being real and resolvable, so this file inherits that grounding
# rather than asserting it afresh.
REAL_PRICING_KEY = "opus"

# A key that exists nowhere: not in the bundled floor, not in any admin
# override this file installs. `mvp.pricing.rate_for` / `snapshot_rates`
# resolve it to the `default` row (E11, already shipped) -- silently, at the
# pricing layer -- which is exactly the trap assertion 4 exists to catch.
UNRESOLVABLE_PRICING_KEY = "not-a-registered-pricing-key-zzz"


def _scope() -> ObservationScope:
    return ObservationScope(
        account="776010787911", region="us-east-1",
        credentials_fingerprint="test-fingerprint", observed_at="2026-09-12T00:00:00+00:00",
    )


def _record(profile_id: str = "us.anthropic.claude-probe-target", **overrides) -> DiscoveredRecord:
    base = dict(
        profile_id=profile_id, provider="anthropic", profile_scope="us",
        model_family="claude-probe-target", jurisdiction_bounded=True,
        destination_regions=("us-east-1",), invocation_region="us-east-1",
        raw_id=profile_id, raw_payload={"inferenceProfileId": profile_id},
        observation_scope=_scope(), blockers=(),
    )
    base.update(overrides)
    return DiscoveredRecord(**base)


class _FakeBedrockRuntime:
    """A `bedrock-runtime` client double whose only method a non-streaming
    probe needs: `converse`. Records every call so a test can confirm the
    probe actually invoked it, not merely that a canned result appeared."""

    def __init__(self, *, response: Optional[dict] = None, exc: Optional[BaseException] = None):
        self.response = response
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.response


def _valid_converse_response(*, input_tokens: int = 37, output_tokens: int = 11) -> dict:
    """A Converse response with a real, present, sane usage block -- the
    shape assertions 2 and 3 need to have something to succeed against, so
    every test in this file that is not specifically about a MISSING or
    malformed usage block uses this one."""
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens},
    }


def _converse_unsupported_error() -> ClientError:
    """A `ValidationException` naming Converse as unsupported for this
    model -- illustrative phrasing, not a message verified against live
    AWS (unlike `mvp.pricing_feeds.agreement`'s own classifier, which the
    contract's amendment log records as checked against a real response).
    This file only needs the shape assertion 1 must refuse on; the exact
    provider wording is not load-bearing here the way it was for A9's
    classifier-ordering hazard."""
    return ClientError(
        {"Error": {"Code": "ValidationException",
                    "Message": "This model does not support the Converse API operation."},
         "ResponseMetadata": {"HTTPStatusCode": 400}},
        "Converse",
    )


def _patch_bedrock_clients(monkeypatch: pytest.MonkeyPatch, fake_client: _FakeBedrockRuntime) -> None:
    factory = lambda *a, **k: fake_client  # noqa: E731 — a throwaway adapter, named for clarity below.

    monkeypatch.setattr("mvp._bedrock_clients.bedrock_runtime_client", factory)
    monkeypatch.setattr("mvp._bedrock_clients.client_for_model", factory)
    monkeypatch.setattr("mvp._bedrock_clients.deployment_client", factory)
    for name in ("bedrock_runtime_client", "client_for_model", "deployment_client"):
        monkeypatch.setattr(f"mvp.discovery.probe.{name}", factory, raising=False)


@pytest.fixture
def _system_tenant_pool(dynamodb_mock):
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
    from dynamo.user_tenants import UserTenantsRepository

    user_id = "discovery-probe"
    period = current_period()
    UserTenantsRepository().ensure(
        user_id=user_id, tenant_id=SYSTEM_TENANT_ID, role="system", total_credit=1_000_000_000
    )
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=SYSTEM_TENANT_ID, period=period, manual_limit_microusd=50_000_000
    )
    return {"user_id": user_id, "tenant_id": SYSTEM_TENANT_ID, "period": period}


@pytest.fixture(autouse=True)
def _unrestricted_system_scope(monkeypatch: pytest.MonkeyPatch):
    """Every test in this file except `TestSystemTenantEligibility` wants the
    system tenant UNRESTRICTED, so a probe against a `us`-scoped record
    never trips on the eligibility axis this file is not testing in that
    test. `TestSystemTenantEligibility` overrides this."""
    from mvp.routing.model_resolver import RoutingConfig

    monkeypatch.setattr(
        "mvp.routing.config.get_tenant_routing_config",
        lambda tenant_id: RoutingConfig(),
    )


def _usage_rows_for(tenant_id: str) -> list[dict]:
    from boto3.dynamodb.conditions import Attr

    from dynamo.client import get_dynamodb_resource, usage_logs_table_name

    table = get_dynamodb_resource().Table(usage_logs_table_name())
    return table.scan(FilterExpression=Attr("tenant_id").eq(tenant_id)).get("Items", [])


def _pool_summary(tenant_id: str, period: str) -> dict:
    from dynamo.tenant_budgets import TenantBudgetsRepository

    return TenantBudgetsRepository().pool_summary(tenant_id, period)


# ---------------------------------------------------------------------------
# Assertion 4: a 200 is not a pass. Pricing must resolve through the
# entry's OWN key, not `default`, to a non-zero amount.
# ---------------------------------------------------------------------------
class TestPricingMustResolveThroughTheEntrysOwnKey:
    def test_a_resolvable_own_key_verifies_and_charges_non_zero(
        self, _system_tenant_pool, monkeypatch
    ):
        fake = _FakeBedrockRuntime(response=_valid_converse_response())
        _patch_bedrock_clients(monkeypatch, fake)

        from mvp.discovery.probe import probe
        from mvp.discovery.verdict import get_probe_verdict

        record = _record()
        result = probe(
            record, invocation=SYNC, pricing_key=REAL_PRICING_KEY, wire_protocol="messages"
        )

        assert result.passed is True
        assert result.invocation == SYNC
        assert result.verdict is not None
        assert result.blocker is None
        assert result.charged_microusd > 0, (
            "assertion 4 requires a NON-ZERO charge, not merely a successful call"
        )

        verdict = get_probe_verdict(record.profile_id, SYNC)
        assert verdict is not None
        assert verdict.state == "verified", (
            "a real Converse response, priced through a real, resolvable, "
            "non-default key, is exactly the case this probe exists to verify"
        )
        assert verdict.pricing_key_at_verification == REAL_PRICING_KEY

        rows = _usage_rows_for(SYSTEM_TENANT_ID)
        assert len(rows) >= 1, "the probe's own charge must be a real UsageLogs row"
        summary = _pool_summary(SYSTEM_TENANT_ID, _system_tenant_pool["period"])
        assert summary["pool_settled_microusd"] > 0

    def test_pricing_through_default_does_not_yield_a_verified_verdict(
        self, _system_tenant_pool, monkeypatch
    ):
        """The exact scenario the module docstring calls out: the Converse
        call succeeds (a real 200, real sane usage counters -- everything
        assertions 1 through 3 ask for), and ONLY `pricing_key` differs from
        the test above. If a probe verified on a 200 alone, this test and
        the one above would both pass; they are written as a pair so an
        implementation that dropped assertion 4 shows up here, not just as
        a missing feature nobody happened to test."""
        fake = _FakeBedrockRuntime(response=_valid_converse_response())
        _patch_bedrock_clients(monkeypatch, fake)

        from mvp.discovery.probe import probe
        from mvp.discovery.verdict import get_probe_verdict

        record = _record()
        result = probe(
            record, invocation=SYNC, pricing_key=UNRESOLVABLE_PRICING_KEY,
            wire_protocol="messages",
        )

        assert result.passed is False
        assert result.verdict is None
        assert result.blocker is not None
        assert result.blocker.type == "protocol_unverified"
        assert result.blocker.subtype == "charge_not_attributed", (
            "a call that succeeded but priced through the `default` fallback "
            "must fail specifically on assertion 4, not on some other subtype "
            "-- assertion 4 is the whole reason this probe pays for itself "
            "rather than trusting a 200"
        )

        verdict = get_probe_verdict(record.profile_id, SYNC)
        assert verdict is None or verdict.state != "verified"


# ---------------------------------------------------------------------------
# Assertion 1, briefly: a Converse call that fails outright must not verify.
# ---------------------------------------------------------------------------
class TestConverseCallFailure:
    def test_a_converse_unsupported_error_does_not_verify(
        self, _system_tenant_pool, monkeypatch
    ):
        fake = _FakeBedrockRuntime(exc=_converse_unsupported_error())
        _patch_bedrock_clients(monkeypatch, fake)

        from mvp.discovery.probe import probe
        from mvp.discovery.verdict import get_probe_verdict

        record = _record()
        result = probe(
            record, invocation=SYNC, pricing_key=REAL_PRICING_KEY, wire_protocol="messages"
        )

        assert result.passed is False
        assert result.verdict is None
        assert result.blocker is not None
        assert result.blocker.type == "protocol_unverified"
        assert result.blocker.subtype == "converse_call_failed"

        verdict = get_probe_verdict(record.profile_id, SYNC)
        assert verdict is None or verdict.state != "verified"


# ---------------------------------------------------------------------------
# The protocol the probe spoke is RECORDED -- the value actually confirmed,
# not merely echoed from what was asked for.
# ---------------------------------------------------------------------------
class TestWireProtocolIsRecordedNotMerelyPresent:
    def test_a_successful_probe_records_the_protocol_it_actually_confirmed(
        self, _system_tenant_pool, monkeypatch
    ):
        fake = _FakeBedrockRuntime(response=_valid_converse_response())
        _patch_bedrock_clients(monkeypatch, fake)

        from mvp.discovery.probe import probe
        from mvp.discovery.verdict import get_probe_verdict

        record = _record()
        result = probe(
            record, invocation=SYNC, pricing_key=REAL_PRICING_KEY, wire_protocol="messages"
        )
        assert result.passed is True
        assert result.verdict.wire_protocol_verified == "messages"

        verdict = get_probe_verdict(record.profile_id, SYNC)
        assert verdict is not None
        assert verdict.wire_protocol_verified == "messages"

    def test_a_claimed_protocol_that_was_never_confirmed_does_not_verify(
        self, _system_tenant_pool, monkeypatch
    ):
        """The non-vacuous companion, built the only way this file can
        without inventing a second transport: the caller claims
        `wire_protocol="messages"`, but the Converse call itself fails in a
        way that specifically names Converse as unsupported. A verdict that
        blindly copied the CLAIMED protocol forward regardless of outcome
        would still pass the test above; this is the one that catches it,
        because a verdict must never assert a protocol it did not actually
        confirm working."""
        fake = _FakeBedrockRuntime(exc=_converse_unsupported_error())
        _patch_bedrock_clients(monkeypatch, fake)

        from mvp.discovery.probe import probe
        from mvp.discovery.verdict import get_probe_verdict

        record = _record()
        result = probe(
            record, invocation=SYNC, pricing_key=REAL_PRICING_KEY, wire_protocol="messages"
        )
        assert result.passed is False
        assert result.verdict is None

        verdict = get_probe_verdict(record.profile_id, SYNC)
        assert verdict is None or verdict.state != "verified", (
            "the claimed protocol was never actually confirmed to work, so "
            "no verdict recording it as verified may exist"
        )


# ---------------------------------------------------------------------------
# The charge lands on the system tenant, through production metering,
# visible like any other charge -- never the granting tenant, never nobody.
# ---------------------------------------------------------------------------
class TestChargeLandsOnTheSystemTenantOnly:
    def test_charge_is_on_system_tenant_id_the_named_constant(
        self, _system_tenant_pool, monkeypatch
    ):
        fake = _FakeBedrockRuntime(response=_valid_converse_response())
        _patch_bedrock_clients(monkeypatch, fake)

        from mvp.discovery.probe import probe

        record = _record()
        result = probe(
            record, invocation=SYNC, pricing_key=REAL_PRICING_KEY, wire_protocol="messages"
        )

        rows = _usage_rows_for(SYSTEM_TENANT_ID)
        assert len(rows) >= 1
        assert all(row.get("tenant_id") == SYSTEM_TENANT_ID for row in rows)
        assert result.charged_microusd == _pool_summary(
            SYSTEM_TENANT_ID, _system_tenant_pool["period"]
        )["pool_settled_microusd"]

    def test_an_unrelated_tenants_pool_is_untouched_by_the_probe(
        self, _system_tenant_pool, seed_tenant_with_pool, monkeypatch
    ):
        """The negative half: an ordinary tenant that happens to exist in
        the same table (`seed_tenant_with_pool`'s `acme-eng`) must see NO
        movement at all from a probe's charge -- pinning "never the
        granting tenant" in the strongest available form, since a probe
        call carries no granting tenant of its own to begin with."""
        fake = _FakeBedrockRuntime(response=_valid_converse_response())
        _patch_bedrock_clients(monkeypatch, fake)

        from mvp.discovery.probe import probe

        record = _record()
        probe(record, invocation=SYNC, pricing_key=REAL_PRICING_KEY, wire_protocol="messages")

        other_rows = _usage_rows_for(seed_tenant_with_pool["tenant_id"])
        assert other_rows == []
        other_summary = _pool_summary(
            seed_tenant_with_pool["tenant_id"], seed_tenant_with_pool["period"]
        )
        assert other_summary["pool_settled_microusd"] == 0
        assert other_summary["pool_reserved_microusd"] == 0


# ---------------------------------------------------------------------------
# The system tenant is not exempt from the eligibility predicate.
# ---------------------------------------------------------------------------
class TestSystemTenantEligibility:
    def test_a_probe_outside_the_system_tenants_scope_is_refused_not_verified(
        self, _system_tenant_pool, monkeypatch
    ):
        """The system tenant's own `profile_scopes` is restricted to `("us",)`
        here, and the record under probe is scoped `"global"` -- outside it.
        This relies on the production reserve/settle path reading tenant
        scope by tenant_id the same way every ordinary reservation does
        (`mvp.routing.config.get_tenant_routing_config`), per this design's
        own claim that the probe's charge "goes through the production
        reserve and settle path... subject to... the eligibility predicate
        like any caller" -- a load-bearing fact of the design, not invented
        here. Refusal is `ProbeAttemptRefused`, raised, with `.reason ==
        "probe_refused"`; a probe that silently returned instead, or that
        verified anyway, would be exactly the unbounded, unmetered caller
        this rule exists to close off."""
        from mvp.routing.model_resolver import RoutingConfig

        def _restricted(tenant_id):
            assert tenant_id == SYSTEM_TENANT_ID, (
                f"eligibility must be read for the system tenant itself, not {tenant_id!r}"
            )
            return RoutingConfig(profile_scopes=("us",))

        monkeypatch.setattr("mvp.routing.config.get_tenant_routing_config", _restricted)

        fake = _FakeBedrockRuntime(response=_valid_converse_response())
        _patch_bedrock_clients(monkeypatch, fake)

        from mvp.discovery.probe import ProbeAttemptRefused, probe
        from mvp.discovery.verdict import get_probe_verdict

        record = _record(profile_scope="global")

        with pytest.raises(ProbeAttemptRefused) as exc_info:
            probe(record, invocation=SYNC, pricing_key=REAL_PRICING_KEY, wire_protocol="messages")
        assert exc_info.value.reason == "probe_refused"

        verdict = get_probe_verdict(record.profile_id, SYNC)
        assert verdict is None or verdict.state != "verified"
        assert not fake.calls, (
            "a refused probe must not have reached the provider at all -- "
            "reaching Bedrock and then discarding the result would still have "
            "spent money on a call this tenant was never eligible to make"
        )

    def test_a_probe_inside_the_system_tenants_scope_still_verifies(
        self, _system_tenant_pool, monkeypatch
    ):
        """Non-vacuous companion: the SAME restricted scope, with the record
        INSIDE it, must still verify -- proving the refusal above is the
        scope boundary doing its job, not a probe that never verifies once
        any restriction is configured at all."""
        from mvp.routing.model_resolver import RoutingConfig

        monkeypatch.setattr(
            "mvp.routing.config.get_tenant_routing_config",
            lambda tenant_id: RoutingConfig(profile_scopes=("us",)),
        )

        fake = _FakeBedrockRuntime(response=_valid_converse_response())
        _patch_bedrock_clients(monkeypatch, fake)

        from mvp.discovery.probe import probe
        from mvp.discovery.verdict import get_probe_verdict

        record = _record(profile_scope="us")
        result = probe(
            record, invocation=SYNC, pricing_key=REAL_PRICING_KEY, wire_protocol="messages"
        )
        assert result.passed is True

        verdict = get_probe_verdict(record.profile_id, SYNC)
        assert verdict is not None
        assert verdict.state == "verified"


# ---------------------------------------------------------------------------
# probe_unmetered: the probe could not obtain a metered hold at all. Pinned
# via the cheapest constructible shape -- the system tenant's own pool cap
# already exhausted -- using the same pool-limit mechanism
# `seed_tenant_with_pool` uses elsewhere in this suite.
# ---------------------------------------------------------------------------
class TestProbeUnmetered:
    def test_a_pool_with_no_headroom_at_all_refuses_as_unmetered_not_refused(
        self, dynamodb_mock, monkeypatch
    ):
        """The system tenant's own pool is provisioned with a cap of 1
        micro-USD -- below what any real reservation could cost -- so the
        production reserve path's own "does not fit the pool limit at all"
        refusal fires. This must surface as `probe_unmetered`, distinct from
        `probe_refused` (a deliberate scope/rate-limit refusal): the probe
        was never able to get a metered hold in the first place, which is a
        different fact than a hold that was available and deliberately
        withheld."""
        from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
        from dynamo.user_tenants import UserTenantsRepository

        period = current_period()
        UserTenantsRepository().ensure(
            user_id="discovery-probe", tenant_id=SYSTEM_TENANT_ID, role="system",
            total_credit=1_000_000_000,
        )
        TenantBudgetsRepository().set_manual_limit(
            tenant_id=SYSTEM_TENANT_ID, period=period, manual_limit_microusd=1
        )

        from mvp.routing.model_resolver import RoutingConfig

        monkeypatch.setattr(
            "mvp.routing.config.get_tenant_routing_config",
            lambda tenant_id: RoutingConfig(),
        )

        fake = _FakeBedrockRuntime(response=_valid_converse_response())
        _patch_bedrock_clients(monkeypatch, fake)

        from mvp.discovery.probe import ProbeAttemptRefused, probe

        record = _record()

        with pytest.raises(ProbeAttemptRefused) as exc_info:
            probe(record, invocation=SYNC, pricing_key=REAL_PRICING_KEY, wire_protocol="messages")
        assert exc_info.value.reason == "probe_unmetered", (
            "a pool with no headroom at all is a failure to obtain a metered "
            "hold, not a deliberate scope/rate-limit refusal -- the two "
            "reasons must not collapse into one"
        )
