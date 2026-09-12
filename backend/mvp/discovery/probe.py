"""E7 — the probe: the publishability boundary, per exposed invocation, under
production identity.

A 200 proves transport and nothing else. The four assertions below are what
make the probe pay for itself (D5): a call that only checked "did Bedrock
answer" would validate reachability and nothing about whether this account's
credentials, this model's protocol support, and this entry's pricing binding
actually agree with each other.

    1. Transport  — Converse (or ConverseStream) succeeds with a valid response.
    2. Usage      — the response carries input and output counters, present
                     and sane.
    3. Mapping    — every counter the response reports maps to a dimension
                     this pricing key actually prices, and no dimension the
                     pricing key prices is left with no counter feeding it.
    4. Ledger     — the probe's own charge, computed from the SAME pricing
                     key and the observed usage, resolves to a non-zero
                     amount and was not silently priced at `default`.

Anything else is `protocol_unverified`, with the subtype naming which
assertion failed (see `mvp.discovery.records.BLOCKER_TYPES`).

**Signature note, reported rather than invented.** The master contract's
Interface section writes this as `probe(record, *, mode) -> ProbeResult`. A
`DiscoveredRecord` carries no price at all — no pricing key, no rate card, no
`Selection` (confirmed by the master contract's own amendment A16, written for
E6's identical problem) — so assertion 4 has nothing to check without a
pricing key from outside the record, and assertion 1 has no wire protocol to
speak without one of those too. Both values are exactly what activation later
compares against the verdict it produces (cross-unit shape 4), so they belong
entering `probe()` from outside, not derived from the record. This module
therefore takes `pricing_key` and `wire_protocol` as required keyword
arguments; `mode` is `invocation` per the ratified correction (see
`mvp.discovery.verdict`'s module docstring for why "mode" was already taken).

**Wire protocol scope.** Only `wire_protocol == "messages"` (Bedrock Converse)
is implemented. A record's own wire protocol is not derivable pre-activation
(that is unit 2's G3, and it is verified against THIS module's own output, not
the other way around) — `wire_protocol` here is supplied by whoever calls
`probe()` (unit 1's promotion flow, or a future operator surface), and
`"responses"` (the bedrock-mantle OpenAI-compatible surface) is refused with
its own `protocol_unverified` subtype rather than implemented, because that
transport has its own request/response shape this unit did not have grounds
to build blind. Reported as a boundary, not silently narrowed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from . import ledger
from .ledger import ProbeAttemptRefused  # re-exported: a caller of `probe` should not
# have to know which layer refused.
from .records import (
    Blocker,
    DiscoveredRecord,
    ObservationScope,
    credentials_fingerprint,
)
from .verdict import (
    INVOCATION_STREAM,
    INVOCATION_VALUES,
    STATE_VERIFIED,
    VERIFIED_BY_PROBE,
    ProbeVerdict,
    put_probe_verdict,
)

#: The one wire protocol this probe speaks. `mvp.models._WIRE_PROTOCOLS`'s
#: other member, `"responses"`, is refused (see the module docstring) rather
#: than silently accepted.
_SUPPORTED_WIRE_PROTOCOL = "messages"

#: The probe's own prompt. Minimal on purpose — assertion 4's whole point is
#: that the charge is non-zero and attributable, not that it is large; a
#: single short user turn with `maxTokens=1` is the cheapest input that still
#: forces a real input-token count and a real (if tiny) output-token count.
#: `max_tokens=1` bounds OUTPUT only (D5's own caution) — the input side is
#: priced at whatever this exact prompt tokenises to, which is why assertion 4
#: computes the charge from the OBSERVED usage rather than from an estimate.
_PROBE_MESSAGE = {"role": "user", "content": [{"text": "ping"}]}
_PROBE_MAX_OUTPUT_TOKENS = 1
#: A conservative reservation estimate for the tiny prompt above — sized to
#: never under-reserve it (a probe reservation failing to cover its own
#: request would itself be a `probe_unmetered` refusal), not a measurement of
#: what it actually costs; the actual cost is read back from the observed
#: usage after the call, per assertion 4.
_PROBE_INPUT_TOKENS_EST = 32


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ProbeResult:
    """What one `probe()` call found. `passed` is the single answer; `verdict`
    is populated iff `passed`, `blocker` iff not — mirroring the discovered
    record's own `Blocker` shape so a caller that wants to record the failure
    can do so with the SAME type `mvp.discovery.records` already defines,
    rather than a second failure shape this unit would have had to invent.

    Deliberately does not itself write `verdict` or `blocker` anywhere except
    the verdict store (`probe()` calls `put_probe_verdict` on a pass, so E7's
    own verified-by-payment fact is never lost between the call returning and
    a caller remembering to persist it — see `probe()`'s docstring for why
    that half is NOT optional the way recording a blocker on the discovered
    record is). Recording a `blocker` onto the `DiscoveredRecord` itself is
    left to the caller: that store's `merge_blockers`/`first_seen` tracking
    is reconciliation's concern (E1/E2), and a probe failure reaching it
    through a second, blind write path here would be a second answer to
    "what does this record's blocker list mean" — the exact defect the
    contract's own scope guards against for E6's refusal reasons.
    """

    passed: bool
    invocation: str
    verdict: Optional[ProbeVerdict]
    blocker: Optional[Blocker]
    charged_microusd: Optional[int]


def _failure(invocation: str, subtype: str, evidence: str) -> ProbeResult:
    return ProbeResult(
        passed=False, invocation=invocation, verdict=None,
        blocker=Blocker(type="protocol_unverified", subtype=subtype, evidence=evidence),
        charged_microusd=None,
    )


def _build_observation_scope(*, region: str, sts: Optional[Any]) -> ObservationScope:
    """The probe's OWN observation scope — fresh at probe time, never borrowed
    from the record's discovery-pass scope, because D5's identity clause ("the
    same account, credentials, invocation region, endpoint and request mode as
    production") is a claim about THIS call, not about whatever last
    reconciled the catalogue. Mirrors `mvp.discovery.reconcile.run_pass`'s own
    STS-degrades-to-empty pattern exactly (same reasoning: STS is metadata
    ABOUT the pass, not the pass itself, so a failed identity read must not
    fail the probe)."""
    import boto3

    try:
        sts_client = sts or boto3.client("sts", region_name=region)
        identity = sts_client.get_caller_identity()
        account = str(identity.get("Account") or "")
        arn = str(identity.get("Arn") or "")
    except Exception:  # noqa: BLE001 — observation scope degrades, probe continues.
        account, arn = "", ""
    return ObservationScope(
        account=account, region=region,
        credentials_fingerprint=credentials_fingerprint(arn) if arn else "",
        observed_at=_now_iso(),
    )


def _default_bedrock_client(region: str):
    """The process-wide, memoized `bedrock-runtime` client for `region` —
    `mvp._bedrock_clients.bedrock_runtime_client`, the SAME accessor every
    route already uses (via `deployment_client`/`client_for_model`, its two
    narrower callers), rather than a raw `boto3.client(...)` built fresh
    here. Two reasons this is not cosmetic: a fresh client per probe call
    skips the module's connection-pool sizing and its per-region memoization
    (see that module's own docstring), and — the one that would have made
    every probe attempt against a real account either hang or fail for an
    unrelated reason — it is the one place a test can inject a fake client
    without this module inventing a second seam for the same thing `mvp.
    anthropic` and friends already have one for."""
    from .. import _bedrock_clients

    return _bedrock_clients.bedrock_runtime_client(region)


def _drain_converse_stream(resp: dict) -> Optional[dict]:
    """The final `usage` block off a `converse_stream` response, or `None`.

    Deliberately minimal: this reads only the trailing `metadata` event for
    its `usage` block, because that is all assertion 2 needs. It is NOT the
    incremental-delta accumulator (`_converse_types.UsageAccumulator`) the
    real streaming ROUTES build to relay content to a caller as it arrives —
    the probe discards the content and only cares whether the metadata event
    ever showed up, which is D5's own framing of the streaming failure mode
    ("a stream terminating before its metadata event").
    """
    usage = None
    for event in resp.get("stream", ()):
        if not isinstance(event, dict):
            continue
        metadata = event.get("metadata")
        if isinstance(metadata, dict) and "usage" in metadata:
            usage = metadata.get("usage")
    return usage


def probe(
    record: DiscoveredRecord, *, invocation: str, pricing_key: str, wire_protocol: str,
    bedrock: Optional[Any] = None, sts: Optional[Any] = None,
) -> ProbeResult:
    """Perform the four assertions against `record`'s underlying Bedrock
    identity, under `invocation`, priced at `pricing_key`. Raises `ledger.
    ProbeAttemptRefused` when the attempt cannot even be made (see that
    exception's docstring for the closed reasons); returns a `ProbeResult`
    for every attempt that WAS made, pass or fail.

    On a pass, writes the verdict (`put_probe_verdict`) before returning —
    unlike a failed assertion's `Blocker`, this is not left to a caller,
    because unit 4 and unit 5 read the verdict store blind to this module and
    a verdict that only exists in a `ProbeResult` a caller forgot to persist
    is indistinguishable, to them, from a probe that never ran.

    `bedrock`/`sts` are injection seams for tests, exactly like `mvp.
    discovery.reconcile.run_pass`'s own `bedrock`/`sts` parameters — built
    lazily from `boto3` only when not supplied, never at import time.
    """
    if invocation not in INVOCATION_VALUES:
        raise ValueError(f"unknown invocation {invocation!r}; must be one of {sorted(INVOCATION_VALUES)}")

    if wire_protocol != _SUPPORTED_WIRE_PROTOCOL:
        return _failure(
            invocation, "wire_protocol_unsupported",
            f"probe does not implement wire_protocol={wire_protocol!r}; only "
            f"{_SUPPORTED_WIRE_PROTOCOL!r} is implemented",
        )

    region = record.invocation_region or "us-east-1"

    ledger.ensure_system_tenant()
    ledger.check_probe_rate_limit()
    ledger.check_probe_scope_eligibility(record)

    hold = ledger.open_probe_hold(
        pricing_key=pricing_key, model_id=record.raw_id, invocation=invocation,
        input_tokens_est=_PROBE_INPUT_TOKENS_EST, max_output_tokens=_PROBE_MAX_OUTPUT_TOKENS,
    )

    from .. import _money
    from ..pricing import effective_rates, rate_usage, snapshot_rates
    from .._converse_core import usage_from_bedrock

    # Assertion 4's resolvability half, checked against the SAME merged map
    # `mvp.pricing.rate_for`/`snapshot_rates` themselves resolve `pricing_key`
    # against (`_RateCache.effective_rates()`'s own docstring: "Rides the SAME
    # refresh path as get() so the read-only view can never diverge from what
    # pricing actually charges"). NOT `rating.pricing_key == "default"`: a
    # `RateSnapshot`/`RatingRecord` built from an unresolved key still carries
    # THAT key as `pricing_key` — only its `version` is tagged to say the
    # RATES came from the bundled floor — so an unresolvable key never
    # produces the literal string `"default"` anywhere on the settled record.
    # Checked here, before the call, rather than only after: whether the key
    # resolves does not depend on what the model answers, and computing it
    # once keeps the post-call check (below) simple.
    _, _merged_rates, _ = effective_rates()
    _pricing_key_resolves = pricing_key in _merged_rates

    client = bedrock or _default_bedrock_client(region)
    observation_scope = _build_observation_scope(region=region, sts=sts)

    kwargs = {
        "modelId": record.raw_id,
        "messages": [_PROBE_MESSAGE],
        "inferenceConfig": {"maxTokens": _PROBE_MAX_OUTPUT_TOKENS},
    }

    try:
        hold.provider_call_starting()
        if invocation == INVOCATION_STREAM:
            resp = client.converse_stream(**kwargs)
            usage_block = _drain_converse_stream(resp)
        else:
            resp = client.converse(**kwargs)
            usage_block = resp.get("usage")
    except Exception as exc:  # noqa: BLE001 — assertion 1 failed; reported, not raised.
        _money.run_ending(hold.claim_unobserved(exc=exc))
        return _failure(invocation, "converse_call_failed", str(exc))

    usage_event = usage_from_bedrock(usage_block)
    if usage_event is None:
        # Assertion 2. Mirrors `mvp.anthropic`'s own non-streaming handling of
        # this exact case: a 200 with no readable usage is SUBMITTED_UNSETTLED
        # (the ceiling stays held), never a synthesised zero.
        from ..provider_outcome import SUBMITTED_UNSETTLED

        _money.run_ending(hold.claim_unobserved(state=SUBMITTED_UNSETTLED))
        return _failure(
            invocation, "usage_counters_missing",
            "Converse response carried no readable usage block",
        )

    rate_snapshot = snapshot_rates(pricing_key)

    # Assertion 3: mapping. A leg the snapshot prices at a rate of exactly
    # zero is not projected — the same "zero legs are real" reading E6's own
    # amendment log (A11) established for `vllm`'s cache legs — so a counter
    # this response never reports for that leg is not a mismatch. A leg the
    # snapshot DOES price (rate > 0) with no counter fed for it is: the
    # binding claims a price for something this call never measured.
    _projected = {
        "input": rate_snapshot.input_per_mtok_microusd > 0,
        "output": rate_snapshot.output_per_mtok_microusd > 0,
        "cache_read": rate_snapshot.cache_read_per_mtok_microusd > 0,
        "cache_write": rate_snapshot.cache_write_per_mtok_microusd > 0,
    }
    _observed = {
        "input": usage_event.input,
        "output": usage_event.output,
        "cache_read": usage_event.cache_read,
        "cache_write": usage_event.cache_write,
    }
    # The implication runs from what the response REPORTED to what the key
    # prices, not the other way round. A request that used no prompt cache
    # legitimately reports no cache counters, and `opus` -- like most keys --
    # prices both cache legs, so requiring a counter for every priced leg would
    # fail nearly every probe on a correct model. The unsound direction is a leg
    # the provider counted and the key does not price: that is a dimension we
    # would be billed for and would charge nothing against.
    mismatches = [
        leg for leg, projected in _projected.items()
        if _observed[leg] and not projected
    ]

    money_usage = _money.Usage(
        input_tokens=usage_event.input, output_tokens=usage_event.output,
        cache_read_tokens=usage_event.cache_read, cache_write_tokens=usage_event.cache_write,
    )
    # The call happened and the model answered either way — settle for real,
    # win or lose the assertions below. A failed assertion is a fact about
    # the binding, not a reason to leave a real charge unsettled.
    _money.run_ending(hold.claim_settle(money_usage))

    if mismatches:
        return _failure(
            invocation, "counter_dimension_mismatch",
            f"the response counted {sorted(mismatches)} but pricing_key="
            f"{pricing_key!r} prices no rate for them, so those tokens would be "
            f"billed by the provider and charged at nothing",
        )

    rating = rate_usage(
        rate_snapshot, input_tokens=usage_event.input, output_tokens=usage_event.output,
        cache_read_tokens=usage_event.cache_read, cache_write_tokens=usage_event.cache_write,
    )
    # Assertion 4. `_pricing_key_resolves` is the "not through default" half
    # (see where it is computed, above, for why `rating.pricing_key` itself
    # cannot answer this — it always echoes back the ASKED-for key, resolved
    # or not). `> 0` is the non-zero-charge half.
    if not _pricing_key_resolves or rating.total_cost_microusd <= 0:
        return _failure(
            invocation, "charge_not_attributed",
            f"pricing_key={pricing_key!r} resolves={_pricing_key_resolves} and "
            f"charged {rating.total_cost_microusd} micro-USD; expected it to "
            f"resolve to a non-zero charge, not fall back to 'default'",
        )

    verdict = ProbeVerdict(
        profile_id=record.profile_id,
        observation_scope=observation_scope,
        invocation=invocation,
        verified_at=_now_iso(),
        verified_by=VERIFIED_BY_PROBE,
        pricing_key_at_verification=pricing_key,
        wire_protocol_verified=wire_protocol,
        state=STATE_VERIFIED,
    )
    put_probe_verdict(verdict)
    return ProbeResult(
        passed=True, invocation=invocation, verdict=verdict, blocker=None,
        charged_microusd=rating.total_cost_microusd,
    )
