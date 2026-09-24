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

**Wire protocol scope.** Both members of `mvp.models._WIRE_PROTOCOLS` are
implemented: `"messages"` over Bedrock Converse, and `"responses"` over the
Bedrock OpenAI-compatible endpoint. A record's own wire protocol is not
derivable pre-activation (that is unit 2's G3, and it is verified against THIS
module's own output, not the other way around) — `wire_protocol` here is
supplied by whoever calls `probe()` (unit 1's promotion flow, or an operator
surface), and one outside that set is refused before any ledger call.

`"responses"` was previously refused rather than implemented, on the grounds
that its request/response shape could not be built blind. It is built here from
measurements against the live endpoint rather than from a specification, and it
drives the SAME helpers the serving route drives — one SSE framer
(`mvp.openai_responses._drain_events`), one terminal-event detector
(`sse_event_type`) and one usage parser (`mvp._converse_core.
usage_from_responses`). That sharing is the point: a probe that reimplemented any
of them would certify a sibling of the code that bills instead of the code
itself, and the two would drift apart silently. The `Usage` that parser returns
is mapped to the money type in `probe` itself, once, for both transports, so
there is one mapping there too.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol

from core.logging import get_logger

from .. import _converse_types as t
from .. import _responses_wire as _wire
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


logger = get_logger(__name__)

#: The blocker subtype for assertion 1 failing on a call whose OUTCOME is
#: unknown rather than definitely negative -- a read timeout or a closed
#: connection after bytes were already sent (`mvp.provider_outcome.
#: classify_exception` returning `SUBMITTED_UNSETTLED`; see that module's own
#: measured example: a call abandoned on a 2s client read timeout was still
#: executed and billed). Named separately from `converse_call_failed`
#: (assertion 1 failing on a call the provider is known to have REJECTED, or
#: one that never left this process) because an operator surface reading this
#: subtype has to answer a different question than every other failure here:
#: not "did the model reject this", but "did this even run", and the honest
#: answer is that nobody watching from here can say. Exported (not
#: underscore-prefixed) so a caller does not have to duplicate the literal.
INDETERMINATE_SUBTYPE = "converse_call_indeterminate"

#: The probe's own prompt. Minimal on purpose — assertion 4's whole point is
#: that the charge is non-zero and attributable, not that it is large; a
#: single short user turn with `maxTokens=1` is the cheapest input that still
#: forces a real input-token count and a real (if tiny) output-token count.
#: `max_tokens=1` bounds OUTPUT only (D5's own caution) — the input side is
#: priced at whatever this exact prompt tokenises to, which is why assertion 4
#: computes the charge from the OBSERVED usage rather than from an estimate.
_PROBE_MESSAGE = {"role": "user", "content": [{"text": "ping"}]}
_PROBE_MAX_OUTPUT_TOKENS = 1
#: The `/responses` cap. NOT 1: the Bedrock OpenAI-compatible endpoint refuses it
#: outright -- "Invalid 'max_output_tokens': integer below minimum value. Expected a
#: value >= 16, but got 1 instead." (measured 2026-09-24). 16 is that minimum, and a
#: reasoning model can spend all sixteen on reasoning and end `status: "incomplete"`
#: with `incomplete_details.reason == "max_output_tokens"` -- also measured, which is
#: why the terminal-event set this probe reads includes `response.incomplete`.
_RESPONSES_MAX_OUTPUT_TOKENS = 16
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


@dataclass(frozen=True)
class _Attempt:
    """What one provider call produced, when it returned at all.

    Exceptions are NOT caught by a transport; they propagate to `probe`'s single
    handler so the exception-to-ledger-state mapping lives in one place. The three
    states a returning call can be in:

    * `usage` set        -- a trusted measurement, so `claim_settle`.
    * `status_code` set  -- the provider answered non-2xx, so
                            `claim_unobserved(status_code=...)`, which routes the
                            status through the SAME table serving uses.
    * neither            -- a 2xx whose usage could not be trusted, so
                            `claim_unobserved(state=SUBMITTED_UNSETTLED)`: the model
                            ran and what it did is unreadable, which is the one case
                            that must never settle at zero.
    """

    #: A `mvp._converse_types.Usage` -- the SAME shape `usage_from_bedrock` returns,
    #: so `probe`'s assertions read one set of field names regardless of which
    #: transport ran. The money type is built from it at the settle, once.
    usage: Optional[t.Usage] = None
    status_code: Optional[int] = None
    evidence: str = ""


class _TransportCall(Protocol):
    """The call contract every transport satisfies.

    Written out rather than left as `Any` because the keywords and the return type are
    the entire agreement between `probe` and a transport: one that returned a bare
    `Usage`, or forgot `on_wire`, would otherwise type-check and then break the ledger
    at runtime.
    """

    def __call__(
        self, *, record: DiscoveredRecord, region: str, invocation: str,
        max_output_tokens: int, on_wire: Callable[[], None], client: Optional[Any],
    ) -> "_Attempt":
        ...


#: Which injection seam a transport's client comes from. Named so `probe` can REFUSE a
#: client belonging to the other transport instead of silently dropping it -- a test
#: that passed `bedrock=stub, wire_protocol="responses"` used to have its stub ignored,
#: build a real pooled client, mint a real bearer, and bill a real call.
_SEAM_BEDROCK = "bedrock"
_SEAM_HTTP = "http"


@dataclass(frozen=True)
class _Transport:
    """One wire protocol's half of a probe: how to call, what the call costs, and which
    client seam it reads.

    `max_output_tokens` is per protocol because the caps are not negotiable in the same
    way: Converse accepts 1, and the Bedrock OpenAI-compatible endpoint refuses anything
    below 16 ("Invalid 'max_output_tokens': integer below minimum value. Expected a
    value >= 16, but got 1 instead.", measured 2026-09-24). The hold is opened against
    this number, so it has to be known before any money moves -- and `probe` hands the
    same attribute to the call, so the reservation and the wire cannot disagree.
    """

    wire_protocol: str
    seam: str
    max_output_tokens: int
    input_tokens_est: int
    call: _TransportCall


def _call_messages(
    *, record: DiscoveredRecord, region: str, invocation: str, max_output_tokens: int,
    on_wire: Any, client: Optional[Any],
) -> _Attempt:
    """Bedrock Converse. The original probe transport, unchanged in behaviour."""
    from .._converse_core import usage_from_bedrock

    bedrock = client or _default_bedrock_client(region)
    kwargs = {
        "modelId": record.raw_id,
        "messages": [_PROBE_MESSAGE],
        "inferenceConfig": {"maxTokens": max_output_tokens},
    }
    on_wire()
    if invocation == INVOCATION_STREAM:
        usage_block = _drain_converse_stream(bedrock.converse_stream(**kwargs))
    else:
        usage_block = bedrock.converse(**kwargs).get("usage")
    usage = usage_from_bedrock(usage_block)
    if usage is None:
        return _Attempt(evidence="Converse response carried no readable usage block")
    return _Attempt(usage=usage)


#: The probe's `/responses` body. `input` carries the same single short user turn the
#: Converse probe sends. Three fields are deliberately absent: `temperature`, which
#: this family rejects outright ("This model doesn't support the temperature field",
#: measured); `reasoning`, whose accepted effort values differ across the GPT tiers so
#: an unsupported one would fail the probe for a reason that says nothing about the
#: binding; and `stream_options`, which is a Chat Completions concept -- this endpoint
#: puts usage on the terminal event without being asked.
def _responses_payload(model_id: str, *, max_output_tokens: int, stream: bool) -> dict:
    return {
        "model": model_id,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "ping"}]}],
        "max_output_tokens": max_output_tokens,
        "stream": stream,
    }


def _call_responses(
    *, record: DiscoveredRecord, region: str, invocation: str, max_output_tokens: int,
    on_wire: Any, client: Optional[Any],
) -> _Attempt:
    """The Bedrock OpenAI-compatible `/responses` endpoint.

    Drives the SAME helpers the serving route drives -- `_openai_transport.
    sync_client`, `auth_headers`, `format_error`, and (through
    `mvp.openai_responses`) the one terminal-event detector and the one usage
    decomposition. A probe that reimplemented any of them would certify a sibling of
    the code that bills rather than the code itself.

    `auth_headers` is called BEFORE `on_wire()`: it can mint a token, and a mint that
    fails is an attempt that never reached the provider, which must not retain a
    reservation.
    """
    import httpx

    from .. import _openai_transport

    http = client or _openai_transport.sync_client(region)
    auth = _openai_transport.auth_headers(region)
    payload = _responses_payload(
        record.raw_id, max_output_tokens=max_output_tokens,
        stream=invocation == INVOCATION_STREAM,
    )

    if invocation == INVOCATION_STREAM:
        on_wire()
        with http.stream(
            "POST", "/responses", json=payload, headers=auth,
            timeout=httpx.Timeout(
                _openai_transport.STREAM_READ_TIMEOUT_SECONDS, connect=10.0, pool=10.0),
        ) as resp:
            if not 200 <= resp.status_code < 300:
                # Drop the bearer and keep the status BEFORE reading the body. Reading a
                # streamed error body can itself fail, and letting that escape would
                # turn a definite rejection into an indeterminate transport failure --
                # holding a ceiling, and leaving a credential the provider has already
                # rejected in the process-wide cache.
                status_code = resp.status_code
                if status_code in (401, 403):
                    _openai_transport.invalidate_token(region, auth)
                try:
                    resp.read()
                    evidence = _openai_transport.format_error(resp)
                except Exception as read_error:  # noqa: BLE001 — evidence is best-effort.
                    evidence = f"error body unreadable: {read_error!r}"
                return _Attempt(status_code=status_code, evidence=evidence)
            usage = None
            # Serving's own framer, not a second one. It is the part of the stream
            # path that could differ silently: two framers reading the same bytes
            # into different frames make the verdict certify a cut this route does
            # not make. `_drain_events` also already handles `\r\n\r\n`, which a
            # split on a literal `"\n\n"` never finds -- such a stream would
            # accumulate whole and then parse as one frame with every `data:` line
            # joined, so no usage would be read from a perfectly good response.
            buffer = bytearray()
            try:
                for chunk in resp.iter_bytes():
                    if not chunk:
                        continue
                    buffer.extend(chunk)
                    for frame in _wire.drain_events(buffer):
                        usage = _wire.terminal_usage_from_frame(frame).usage or usage
            except Exception:  # noqa: BLE001 — see below.
                if usage is None:
                    # Nothing was observed, so this is a transport failure and the
                    # caller's classifier must see it.
                    raise
                # A terminal already arrived and validated. The stream breaking
                # afterwards does not unmake that measurement, and reporting it as
                # indeterminate would hold a ceiling instead of charging an amount we
                # know exactly.
                logger.warning(
                    "responses_probe_stream_faulted_after_terminal",
                    extra={"profile_id": record.profile_id, "invocation": invocation},
                )
            # The trailing unterminated frame is parsed for the same reason serving
            # parses it: this upstream is measured to close the body before the final
            # blank line, and discarding it per the SSE spec would drop the usage
            # block on exactly the streams that report one.
            if buffer:
                usage = (
                    _wire.terminal_usage_from_frame(bytes(buffer)).usage or usage)
        if usage is not None:
            # Returned here, inside the `with`, so a failure while the context manager
            # closes the connection cannot discard a terminal usage block that already
            # arrived and was validated. A trailing transport fault says nothing about
            # a measurement the provider already delivered, and letting it escape would
            # hold a ceiling in place of a charge we can compute exactly.
            return _Attempt(usage=usage)
        if usage is None:
            return _Attempt(
                evidence="the stream ended with no terminal event carrying a readable "
                         f"usage block (terminals read: "
                         f"{sorted(_wire.METERED_TERMINAL_TYPES)})",
            )
        return _Attempt(usage=usage)

    on_wire()
    resp = http.post(
        "/responses", json=payload, headers=auth,
        timeout=_openai_transport.nonstream_timeout(),
    )
    if not 200 <= resp.status_code < 300:
        # Not `>= 400`: httpx does not follow redirects by default, so a 3xx would
        # otherwise be read as a success carrying no usage and reported as an
        # unreadable body. It is an answer with a status, and the status table is
        # what should decide what it means.
        if resp.status_code in (401, 403):
            _openai_transport.invalidate_token(region, auth)
        return _Attempt(
            status_code=resp.status_code, evidence=_openai_transport.format_error(resp))
    try:
        body = resp.json()
        if not isinstance(body, dict):
            raise ValueError(f"expected a JSON object, got {type(body).__name__}")
        usage = _wire.usage_from_responses(body.get("usage"))
    except ValueError as exc:
        # `ResponsesUsageShapeError` IS a `ValueError`, and so is a JSON decode
        # failure, and so is a body that is not an object. All three are one answer
        # -- the model ran and we cannot read what it did -- and catching only the
        # narrow type let a list body escape as an `AttributeError` that this
        # module's caller would have classified through its catch-all instead.
        return _Attempt(evidence=f"the 200 carried no readable usage block: {exc}")
    return _Attempt(usage=usage)


_TRANSPORTS: dict[str, _Transport] = {
    "messages": _Transport(
        wire_protocol="messages", seam=_SEAM_BEDROCK,
        max_output_tokens=_PROBE_MAX_OUTPUT_TOKENS,
        input_tokens_est=_PROBE_INPUT_TOKENS_EST, call=_call_messages,
    ),
    "responses": _Transport(
        wire_protocol="responses", seam=_SEAM_HTTP,
        max_output_tokens=_RESPONSES_MAX_OUTPUT_TOKENS,
        input_tokens_est=_PROBE_INPUT_TOKENS_EST, call=_call_responses,
    ),
}


def probe(
    record: DiscoveredRecord, *, invocation: str, pricing_key: str, wire_protocol: str,
    bedrock: Optional[Any] = None, sts: Optional[Any] = None,
    http: Optional[Any] = None,
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

    `bedrock`/`sts`/`http` are injection seams for tests, exactly like `mvp.
    discovery.reconcile.run_pass`'s own `bedrock`/`sts` parameters — built
    lazily from `boto3`/`httpx` only when not supplied, never at import time.
    `http` is the `"responses"` transport's seam (an `httpx.Client`, which a test builds
    over `httpx.MockTransport`); `bedrock` is the `"messages"` one. Supplying the seam
    that belongs to the OTHER transport raises `TypeError` rather than being ignored --
    dropping it silently is how a test suite ends up minting a real bearer and billing a
    real call.
    """
    if invocation not in INVOCATION_VALUES:
        raise ValueError(f"unknown invocation {invocation!r}; must be one of {sorted(INVOCATION_VALUES)}")

    transport = _TRANSPORTS.get(wire_protocol)
    if transport is None:
        return _failure(
            invocation, "wire_protocol_unsupported",
            f"probe does not implement wire_protocol={wire_protocol!r}; implemented: "
            f"{sorted(_TRANSPORTS)}",
        )

    # A client for the OTHER transport is a programming error, and the silent version
    # of it is the expensive one: a test that passed `bedrock=` while asking for
    # `"responses"` had its stub dropped, built a real pooled client, minted a real
    # bearer and billed a real call. Raised rather than reported as a probe failure,
    # because it is this process's mistake and not the provider's.
    seams: dict[str, Optional[Any]] = {_SEAM_BEDROCK: bedrock, _SEAM_HTTP: http}
    supplied_for_others = sorted(
        name for name, value in seams.items()
        if value is not None and name != transport.seam)
    if supplied_for_others:
        raise TypeError(
            f"wire_protocol={wire_protocol!r} reads the {transport.seam!r} client seam, "
            f"but {supplied_for_others} was supplied; a client for another transport "
            f"would be dropped and a real endpoint called instead"
        )

    region = record.invocation_region or "us-east-1"

    ledger.ensure_system_tenant()
    ledger.check_probe_rate_limit()
    ledger.check_probe_scope_eligibility(record)

    from .. import _money
    from ..pricing import effective_rates, rate_usage, snapshot_rates

    # Everything that can raise and does not need the hold runs BEFORE it. A rate-table
    # read or an STS call failing after the reservation is open, but before the `try`
    # that ends it, leaves the hold with no terminal at all -- the reservation is held
    # until a reaper notices, for an attempt that was never made.
    _, _merged_rates, _ = effective_rates()
    observation_scope = _build_observation_scope(region=region, sts=sts)

    # Sized from the transport, not from a module constant: the two protocols have
    # different output caps, and a reservation that does not cover the cap the call
    # actually sends would refuse the probe as unmetered.
    hold = ledger.open_probe_hold(
        pricing_key=pricing_key, model_id=record.raw_id, invocation=invocation,
        input_tokens_est=transport.input_tokens_est,
        max_output_tokens=transport.max_output_tokens,
    )

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
    _pricing_key_resolves = pricing_key in _merged_rates

    try:
        attempt = transport.call(
            record=record, region=region, invocation=invocation,
            max_output_tokens=transport.max_output_tokens,
            on_wire=hold.provider_call_starting,
            client=seams[transport.seam],
        )
    except Exception as exc:  # noqa: BLE001 — assertion 1 failed; reported, not raised.
        _money.run_ending(hold.claim_unobserved(exc=exc))
        # Classify with the SAME, already-measured classifier the money path
        # itself uses (`mvp.provider_outcome.classify_exception`) rather than
        # a second, probe-local guess at which exceptions are ambiguous — a
        # `SUBMITTED_UNSETTLED` reading here is exactly "the request left,
        # the model may have run to completion, and the client simply
        # stopped waiting", which is a different claim than "the model
        # rejected this" or "this never left the process". A caller that
        # would otherwise report this probe as a definite failure needs to
        # tell the two apart.
        from ..provider_outcome import SUBMITTED_UNSETTLED as _SUBMITTED_UNSETTLED
        from ..provider_outcome import classify_exception

        if classify_exception(exc) == _SUBMITTED_UNSETTLED:
            return _failure(invocation, INDETERMINATE_SUBTYPE, str(exc))
        return _failure(invocation, "converse_call_failed", str(exc))

    if attempt.status_code is not None:
        # The provider answered, and said no. The status goes through the SAME table
        # serving resolves a status against (`provider_outcome.classify_http_status`,
        # reached via `claim_unobserved(status_code=)`) rather than a probe-local
        # reading of which codes are rejections.
        _money.run_ending(hold.claim_unobserved(status_code=attempt.status_code))
        return _failure(
            invocation, "converse_call_failed",
            f"provider answered HTTP {attempt.status_code}: {attempt.evidence}",
        )

    usage_event = attempt.usage
    if usage_event is None:
        # Assertion 2. Mirrors `mvp.anthropic`'s own non-streaming handling of
        # this exact case: a 200 with no readable usage is SUBMITTED_UNSETTLED
        # (the ceiling stays held), never a synthesised zero.
        from ..provider_outcome import SUBMITTED_UNSETTLED

        _money.run_ending(hold.claim_unobserved(state=SUBMITTED_UNSETTLED))
        return _failure(
            invocation, "usage_counters_missing",
            attempt.evidence or "the response carried no readable usage block",
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
