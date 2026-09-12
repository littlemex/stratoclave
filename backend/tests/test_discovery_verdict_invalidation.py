"""E9 — verdict invalidation by signal, never by clock. "The verdict does not
expire" half already lives in `test_discovery_verdict.py::TestNoClock`
(it needs nothing but the store); this file is the "invalidated by signal, or
it stands" half.

**Ratified while this file was being written.** The axis is `invocation`
(`"sync"` / `"stream"`), never `mode` -- every call below uses it. And the
classifier this file exercises is explicitly biased: "the classifier that
decides whether an exception is a typed protocol failure has no verified AWS
message string behind it... the deliberate bias is that an exception it
cannot classify does not invalidate." Three signal shapes follow from that
directly and are all pinned below: a recognised typed protocol failure
(invalidates), a recognised transient failure -- a 5xx, a throttle -- (does
not), and an UNRECOGNISED exception the classifier has never seen (also does
not, by the same default-deny bias, and this is the case production will
actually meet most often).

**Reconciled to the ratified names, not this file's own first guess.** The
coordinator names two functions in `mvp.discovery.verdict`, already ratified
against a counterpart implementation that is live against them:

  - `is_typed_protocol_failure(exc) -> bool` — the pure classifier, over
    EXCEPTIONS only. A fake `ValidationException` naming Converse or the
    model as unsupported answers `True`; a throttle, a 5xx, and an exception
    it has never seen all answer `False` -- explicitly biased toward the
    last of those three, since an unrecognised failure clearing a verified
    verdict on every provider hiccup nobody anticipated is the expensive
    direction to be wrong in.
  - `on_served_traffic_outcome(profile_id, invocation, *, exc=None,
    metering_fault=False)` — the single seam the money path calls, from
    BOTH the clean-completion metering-fault site (E10's `claim_settle`,
    called with `metering_fault=True` — "a 200 with absent usage counters"
    is not an exception at all, it is exactly the condition
    `mvp._money.METERING_FAULT_NO_FINAL_USAGE` already names on that path)
    and the unobserved-outcome site (`claim_unobserved`, called with `exc=`
    the exception that was seen). This is also what this file drives
    directly: it needs no DynamoDB or `Hold` machinery for the
    classification half, only the verdict store for the observable effect.

**What this file does not attempt, and why.** "A refused grant does not
invalidate" is pinned below using the REAL, already-shipped grant path
(`mvp.admin_entitlements.grant_entitlement` / `GrantFloorRefusal`, E6) rather
than a second invented call, because that flow already exists and already
raises on refusal — nothing about it needs a naming commitment. It is,
however, a test that would pass even with the invalidation feature entirely
unimplemented (nothing currently wires a grant attempt into the verdict
store at all), so it is a regression guard against a future wrong wiring,
not standalone proof the right wiring exists.
"""
from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from mvp.discovery.records import ObservationScope

SYNC = "sync"


def _client_error(code: str, status: int, message: str = "x") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message},
         "ResponseMetadata": {"HTTPStatusCode": status}},
        "Converse",
    )


def _protocol_unsupported_error() -> ClientError:
    return _client_error(
        "ValidationException", 400,
        "This model does not support the Converse API operation.",
    )


def _server_error() -> ClientError:
    return _client_error("InternalServerException", 500, "internal error")


def _throttle_error() -> ClientError:
    return _client_error("ThrottlingException", 429, "rate exceeded")


class _NeverSeenBeforeProviderCondition(Exception):
    """An exception shape the classifier was never written against --
    the case the coordinator's own bias statement calls out as the one
    production will actually meet, and the one a wrong answer is expensive
    for: treating an unrecognised failure as a typed protocol failure would
    clear a verified binding on every novel error shape a provider ever
    introduces."""


def _scope() -> ObservationScope:
    return ObservationScope(
        account="776010787911", region="us-east-1",
        credentials_fingerprint="test-fingerprint", observed_at="2026-09-10T00:00:00+00:00",
    )


def _verified_verdict(profile_id: str, *, invocation: str = SYNC, pricing_key: str = "opus"):
    from mvp.discovery.verdict import ProbeVerdict

    return ProbeVerdict(
        profile_id=profile_id, observation_scope=_scope(), invocation=invocation,
        verified_at="2026-09-10T00:00:00+00:00", verified_by="probe",
        pricing_key_at_verification=pricing_key, wire_protocol_verified="messages",
        state="verified",
    )


def _seed_verified(profile_id: str, *, invocation: str = SYNC, pricing_key: str = "opus"):
    from mvp.discovery.verdict import put_probe_verdict

    v = _verified_verdict(profile_id, invocation=invocation, pricing_key=pricing_key)
    put_probe_verdict(v)
    return v


# ---------------------------------------------------------------------------
# The pure classification: which EXCEPTIONS are a typed protocol failure,
# in isolation. `metering_fault` (the OTHER positive signal) is not an
# exception and is not this classifier's concern -- see the class below.
# ---------------------------------------------------------------------------
class TestIsTypedProtocolFailure:
    def test_a_converse_unsupported_validation_error_is_a_typed_protocol_failure(self):
        from mvp.discovery.verdict import is_typed_protocol_failure

        assert is_typed_protocol_failure(_protocol_unsupported_error()) is True

    @pytest.mark.parametrize("exc", [
        pytest.param(_server_error(), id="server_error_5xx"),
        pytest.param(_throttle_error(), id="throttle"),
        pytest.param(_NeverSeenBeforeProviderCondition("boom"), id="unrecognised_exception"),
    ])
    def test_transient_and_unrecognised_exceptions_are_not(self, exc):
        """Three negatives, not one, per the classifier's own stated bias: a
        5xx and a throttle are RECOGNISED as transient and must answer
        `False`, and an exception the classifier has never seen must ALSO
        answer `False` -- default-deny, because a classifier that answered
        `True` for anything it could not place would clear a verified
        verdict every time a provider introduced a failure shape nobody had
        written a branch for yet, which is the expensive direction to be
        wrong in."""
        from mvp.discovery.verdict import is_typed_protocol_failure

        assert is_typed_protocol_failure(exc) is False, (
            f"{exc!r} must NOT classify as a typed protocol failure -- either "
            "it is transient, or it is unrecognised, and the bias is to leave "
            "a verdict standing on either"
        )


# ---------------------------------------------------------------------------
# on_served_traffic_outcome: the single seam the money path calls, from
# both the clean-completion metering-fault site (metering_fault=True) and
# the unobserved-outcome site (exc=...).
# ---------------------------------------------------------------------------
class TestOnServedTrafficOutcome:
    def test_a_typed_protocol_failure_exception_flips_a_verified_verdict(self, dynamodb_mock):
        from mvp.discovery.verdict import get_probe_verdict, on_served_traffic_outcome

        pid = "us.anthropic.claude-signal-protocol-failure"
        _seed_verified(pid)

        on_served_traffic_outcome(pid, SYNC, exc=_protocol_unsupported_error())

        got = get_probe_verdict(pid, SYNC)
        assert got is not None
        assert got.state == "invalidated"

    def test_a_metering_fault_flips_a_verified_verdict(self, dynamodb_mock):
        """"A 200 with absent usage counters" -- called exactly the way
        E10's clean-completion site would call it: no exception at all,
        `metering_fault=True`."""
        from mvp.discovery.verdict import get_probe_verdict, on_served_traffic_outcome

        pid = "us.anthropic.claude-signal-metering-fault"
        _seed_verified(pid)

        on_served_traffic_outcome(pid, SYNC, metering_fault=True)

        got = get_probe_verdict(pid, SYNC)
        assert got is not None
        assert got.state == "invalidated"

    @pytest.mark.parametrize("exc", [
        pytest.param(_server_error(), id="server_error_5xx"),
        pytest.param(_throttle_error(), id="throttle"),
        pytest.param(_NeverSeenBeforeProviderCondition("boom"), id="unrecognised_exception"),
    ])
    def test_a_transient_or_unrecognised_exception_leaves_a_verified_verdict_verified(
        self, exc, dynamodb_mock
    ):
        """Non-vacuous companion to the pair above, at the store rather than
        the pure predicate: this is the assertion that actually matters
        operationally -- a stored, verified verdict surviving a routine
        provider hiccup, or a failure shape nobody anticipated, untouched --
        not merely a predicate function returning the right boolean in
        isolation."""
        from mvp.discovery.verdict import get_probe_verdict, on_served_traffic_outcome

        pid = "us.anthropic.claude-signal-negative"
        _seed_verified(pid)

        on_served_traffic_outcome(pid, SYNC, exc=exc)

        got = get_probe_verdict(pid, SYNC)
        assert got is not None
        assert got.state == "verified", (
            f"{exc!r} must not have touched a verdict that was verified "
            "before it fired"
        )

    def test_an_outcome_for_a_profile_with_no_verdict_does_nothing_observable(self, dynamodb_mock):
        """There is nothing to invalidate when nothing was ever verified --
        pinned so an implementation cannot satisfy the pair above by writing
        a fresh invalidated row for any outcome it sees, profile or not."""
        from mvp.discovery.verdict import get_probe_verdict, on_served_traffic_outcome

        pid = "us.anthropic.claude-never-probed-at-all"
        on_served_traffic_outcome(pid, SYNC, exc=_protocol_unsupported_error())
        assert get_probe_verdict(pid, SYNC) is None


# ---------------------------------------------------------------------------
# The pricing key changing invalidates -- the SHORT REGISTRY KEY, never the
# content-addressed projection fingerprint.
# ---------------------------------------------------------------------------
class TestPricingKeyDrift:
    def test_a_changed_registry_pricing_key_invalidates(self, dynamodb_mock):
        from mvp.discovery.verdict import get_probe_verdict, invalidate_for_pricing_key_change

        pid = "us.anthropic.claude-repriced"
        _seed_verified(pid, pricing_key="opus")

        invalidate_for_pricing_key_change(pid, SYNC, "sonnet")

        got = get_probe_verdict(pid, SYNC)
        assert got is not None
        assert got.state == "invalidated", (
            "the entry now being priced under a DIFFERENT registry key than "
            "the one this verdict verified means the binding it certified no "
            "longer describes what will actually be billed"
        )

    def test_the_same_registry_pricing_key_does_not_invalidate(self, dynamodb_mock):
        """Non-vacuous companion. Also the one place this file can pin
        "the short registry key, never the fingerprint" at all: `ProbeVerdict`
        (frozen) carries only `pricing_key_at_verification`, no second
        fingerprint field, so there is nothing else on the verdict a
        comparison COULD read instead -- this test only defends the
        registry-key comparison being an actual equality check, not a
        trap that always fires."""
        from mvp.discovery.verdict import get_probe_verdict, invalidate_for_pricing_key_change

        pid = "us.anthropic.claude-stable-pricing"
        _seed_verified(pid, pricing_key="opus")

        invalidate_for_pricing_key_change(pid, SYNC, "opus")

        got = get_probe_verdict(pid, SYNC)
        assert got is not None
        assert got.state == "verified"


# ---------------------------------------------------------------------------
# A refused grant does not invalidate. The channel reads only served
# traffic.
# ---------------------------------------------------------------------------
class TestRefusedGrantDoesNotInvalidate:
    @pytest.fixture(autouse=True)
    def _reset_pricing_cache(self):
        from mvp import pricing

        pricing.reset_cache()
        yield
        pricing.reset_cache()

    @pytest.fixture
    def _registry(self, monkeypatch):
        from mvp.models import ModelEntry

        entry = ModelEntry(
            provider="anthropic", bedrock_model_id="us.anthropic.claude-grant-refusal-target",
            bedrock_region="us-east-1", aliases=("grant-refusal-target",),
            wire_protocol="messages", pricing_key="opus",
            model_family="claude-grant-refusal-target", profile_scope="us",
            access="entitlement_required", jurisdiction_bounded=True, jurisdiction="us",
        )
        monkeypatch.setattr("mvp.models._REGISTRY", (entry,))
        return entry

    def test_a_grant_floor_refusal_leaves_an_existing_verdict_untouched(
        self, dynamodb_mock, _registry
    ):
        """Sets up a real `GrantFloorRefusal` (E6, already shipped) against
        the SAME `(model_family, profile_scope)` a verified verdict already
        exists for, via a discovered record whose live rate disagrees with
        the bundled floor -- the exact refusing shape `test_grant_floor_e6.py`
        already exercises. Without this, `refused grant does not invalidate`
        would only ever be true because nothing calls into the verdict store
        from the grant path at all; asserting it explicitly is what makes a
        future, wrong wiring (someone routing a grant refusal's reason
        through to `apply_signal`) show up here instead of shipping quietly."""
        from dataclasses import dataclass, field as dc_field
        from typing import Optional as _Optional

        from mvp import pricing
        from mvp.admin_entitlements import GrantFloorRefusal, grant_entitlement
        from mvp.discovery.records import DiscoveredRecord, put_discovered_record
        from mvp.discovery.verdict import get_probe_verdict
        from mvp.rates import Rate

        @dataclass
        class _Actor:
            user_id: str = "admin-1"
            email: str = "admin@example.com"
            org_id: str = "ops"
            roles: list = dc_field(default_factory=lambda: ["admin"])
            auth_kind: str = "jwt"
            key_scopes: _Optional[list] = None

        family, scope = "claude-grant-refusal-target", "us"
        pid = "us.anthropic.claude-grant-refusal-target"
        _seed_verified(pid, pricing_key="opus")

        put_discovered_record(DiscoveredRecord(
            profile_id=pid, provider="anthropic", profile_scope=scope, model_family=family,
            jurisdiction_bounded=True, destination_regions=("us-east-1",),
            invocation_region="us-east-1", raw_id=pid, raw_payload={"inferenceProfileId": pid},
            observation_scope=_scope(),
        ))

        floor = pricing.baseline_rates()["opus"]
        below = Rate(
            floor.input_per_mtok_microusd, floor.output_per_mtok_microusd,
            floor.cache_read_per_mtok_microusd, floor.cache_write_per_mtok_microusd - 1,
        )
        from dynamo.pricing_config import PricingConfigRepository

        PricingConfigRepository().set_rates(version="test-grant-refusal-1", rates={"opus": below})
        pricing.reset_cache()

        with pytest.raises(GrantFloorRefusal):
            grant_entitlement(
                tenant_id="acme-grant-refusal-tenant", model_family=family, profile_scope=scope,
                actor=_Actor(),
            )

        got = get_probe_verdict(pid, SYNC)
        assert got is not None
        assert got.state == "verified", (
            "a refused grant request must never invalidate a verdict -- the "
            "channel this contract wires reads only SERVED traffic, and a "
            "grant request that never runs anything against the provider is "
            "not that"
        )
