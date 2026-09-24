"""The probe over the `responses` wire protocol.

This path was previously refused rather than implemented, so a model served through
the Bedrock OpenAI-compatible endpoint could be discovered and promoted and then
never activated. Every assertion below is about the money, because that is the only
thing a probe is for: a 200 proves transport and nothing else.

The transport is driven through an injected `httpx.MockTransport`, so nothing here
touches the network or spends anything, and the fixtures are verbatim captures from
the live endpoint (2026-09-24) rather than shapes read off a specification.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx
import pytest

from mvp.deps import AuthenticatedUser  # noqa: F401 — imported for parity with the sibling file.
from mvp.discovery.records import DiscoveredRecord, ObservationScope

SYNC = "sync"
STREAM = "stream"
#: A key with a reviewed floor row, so assertion 4 has a non-zero charge to find.
REAL_PRICING_KEY = "gpt-5.6-sol"

MEASURED_USAGE = {
    "input_tokens": 7,
    "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
    "output_tokens": 5,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 12,
}


def _scope() -> ObservationScope:
    return ObservationScope(
        account="776010787911", region="us-east-1",
        credentials_fingerprint="test-fingerprint", observed_at="2026-09-24T00:00:00+00:00",
    )


def _record(profile_id: str = "us.openai.gpt-probe-target", **overrides) -> DiscoveredRecord:
    base = dict(
        profile_id=profile_id, provider="openai", profile_scope="us",
        model_family="gpt-probe-target", jurisdiction_bounded=True,
        destination_regions=("us-east-1",), invocation_region="us-east-1",
        raw_id=profile_id, raw_payload={"inferenceProfileId": profile_id},
        observation_scope=_scope(), blockers=(),
    )
    base.update(overrides)
    return DiscoveredRecord(**base)


def _sse(*frames: dict) -> bytes:
    """An SSE body in the shape the endpoint actually sends: `data:` only, no
    `event:` lines, the event name carried inside the JSON as `"type"`."""
    return b"".join(f"data: {json.dumps(f)}\n\n".encode("utf-8") for f in frames)


def _client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1",
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture(autouse=True)
def _no_real_bearer(monkeypatch):
    """`auth_headers` would mint a Bedrock API key against real credentials. The
    probe calls it BEFORE the wire moment on purpose, so it is stubbed rather than
    bypassed: a test that skipped it would not exercise the ordering."""
    from mvp import _openai_transport

    monkeypatch.setattr(
        _openai_transport, "auth_headers", lambda region: {"Authorization": "Bearer test"})


@pytest.fixture(autouse=True)
def _unrestricted_system_scope(monkeypatch):
    from mvp.discovery import ledger

    monkeypatch.setattr(ledger, "check_probe_scope_eligibility", lambda record: None)


@pytest.fixture
def _system_tenant_pool(dynamodb_mock):
    from mvp.discovery import ledger

    ledger.ensure_system_tenant()
    return ledger


class TestAVerifiedResponsesProbe:
    def test_a_measured_usage_block_verifies_and_charges_non_zero(
        self, _system_tenant_pool, monkeypatch
    ):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"status": "completed", "usage": MEASURED_USAGE})

        from mvp.discovery.probe import probe

        result = probe(_record(), invocation=SYNC, pricing_key=REAL_PRICING_KEY,
                       wire_protocol="responses", http=_client(handler))

        assert result.passed is True, result.blocker
        assert result.verdict is not None
        assert result.verdict.wire_protocol_verified == "responses", (
            "the verdict must record the protocol actually spoken; activation "
            "refuses when it disagrees with the candidate"
        )
        assert result.charged_microusd and result.charged_microusd > 0

        assert len(seen) == 1
        body = json.loads(seen[0].content)
        assert body["model"] == "us.openai.gpt-probe-target", (
            "the probe must send the same id serving sends, or it verifies a "
            "different invocation than the one that will be served"
        )
        assert body["max_output_tokens"] == 16, (
            "1 is refused by this endpoint: 'integer below minimum value. Expected "
            "a value >= 16'"
        )
        assert "temperature" not in body, (
            "this family rejects the field outright, so sending it would fail the "
            "probe for a reason that says nothing about the binding"
        )

    def test_a_truncated_stream_verifies(self, _system_tenant_pool):
        """A reasoning model can spend the whole 16-token cap on reasoning and end
        `response.incomplete`. Measured, so the probe has to accept it or no
        reasoning model can ever be activated."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_sse(
                {"type": "response.created", "response": {"id": "r"}},
                {"type": "response.incomplete",
                 "response": {"id": "r", "status": "incomplete",
                              "incomplete_details": {"reason": "max_output_tokens"},
                              "usage": MEASURED_USAGE}},
            ))

        from mvp.discovery.probe import probe

        result = probe(_record(), invocation=STREAM, pricing_key=REAL_PRICING_KEY,
                       wire_protocol="responses", http=_client(handler))
        assert result.passed is True, result.blocker


class TestWhatMustNotBeSettled:
    """Three answers that must never become a charge, and must never become a
    verdict. The first is the one that produces a FALSE PASS rather than a false
    fail: a block with only `input_tokens` becomes `(5, 0)` under the transport's
    shared `extract_usage`, settles as fully observed, and rates above zero on the
    input leg alone -- a verdict for a model whose output counter was never read."""

    @pytest.mark.parametrize(
        "usage",
        [None, {}, {"input_tokens": 5}, {"input_tokens": 5, "output_tokens": True},
         {"input_tokens": 5, "output_tokens": 3, "total_tokens": 20}],
        ids=["absent", "empty", "input-only", "bool-output", "total-disagrees"],
    )
    def test_an_unreadable_usage_block_does_not_verify(
        self, _system_tenant_pool, usage
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "completed", "usage": usage})

        from mvp.discovery.probe import probe
        from mvp.discovery.verdict import get_probe_verdict

        record = _record()
        result = probe(record, invocation=SYNC, pricing_key=REAL_PRICING_KEY,
                       wire_protocol="responses", http=_client(handler))

        assert result.passed is False
        assert result.blocker is not None
        assert result.blocker.subtype == "usage_counters_missing", result.blocker
        assert result.charged_microusd is None
        assert get_probe_verdict(record.profile_id, SYNC) is None

    def test_an_upstream_refusal_is_reported_as_the_status_it_was(
        self, _system_tenant_pool
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"message": "slow down"})

        from mvp.discovery.probe import probe

        result = probe(_record(), invocation=SYNC, pricing_key=REAL_PRICING_KEY,
                       wire_protocol="responses", http=_client(handler))
        assert result.passed is False
        assert "429" in (result.blocker.evidence if result.blocker else "")

    def test_a_rejected_bearer_is_dropped_so_it_is_not_served_to_every_request(
        self, _system_tenant_pool, monkeypatch
    ):
        """A 401 or 403 means OUR credential was rejected. Leaving it in the
        process-wide cache serves a dead bearer to every request in the region for
        the rest of its life."""
        from mvp import _openai_transport

        dropped: list[str] = []
        monkeypatch.setattr(
            _openai_transport, "invalidate_token",
            lambda region, used=None: dropped.append(region))

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "nope"})

        from mvp.discovery.probe import probe

        result = probe(_record(), invocation=SYNC, pricing_key=REAL_PRICING_KEY,
                       wire_protocol="responses", http=_client(handler))
        assert result.passed is False
        assert dropped == ["us-east-1"], dropped

    def test_a_stream_that_ends_without_a_terminal_event_does_not_verify(
        self, _system_tenant_pool
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_sse(
                {"type": "response.created", "response": {"id": "r"}},
                {"type": "response.output_text.delta", "delta": "o"},
            ))

        from mvp.discovery.probe import probe

        result = probe(_record(), invocation=STREAM, pricing_key=REAL_PRICING_KEY,
                       wire_protocol="responses", http=_client(handler))
        assert result.passed is False
        assert result.blocker is not None
        assert result.blocker.subtype == "usage_counters_missing", result.blocker


class TestAnUnknownProtocolStillRefusesBeforeSpendingAnything:
    def test_an_unimplemented_protocol_is_refused_before_the_ledger(self, monkeypatch):
        """Ordering, not just outcome: the refusal must happen before
        `ensure_system_tenant` and before any hold, so an unknown protocol cannot
        open a reservation it will never use."""
        from mvp.discovery import ledger
        from mvp.discovery.probe import probe

        def _must_not_run():
            raise AssertionError("the ledger was touched for an unimplemented protocol")

        monkeypatch.setattr(ledger, "ensure_system_tenant", _must_not_run)

        result = probe(_record(), invocation=SYNC, pricing_key=REAL_PRICING_KEY,
                       wire_protocol="telepathy")
        assert result.passed is False
        assert result.blocker is not None
        assert result.blocker.subtype == "wire_protocol_unsupported"


class TestTheFramerIsSharedWithServing:
    def test_a_crlf_stream_is_framed(self, _system_tenant_pool):
        """SSE allows `\\r\\n\\r\\n` as a boundary. A split on a literal `"\\n\\n"`
        never finds one, so the whole body would accumulate and then parse as a
        single frame with every `data:` line joined -- and no usage would be read
        from a response that reported it perfectly well."""
        def handler(request: httpx.Request) -> httpx.Response:
            frames = [
                {"type": "response.created", "response": {"id": "r"}},
                {"type": "response.completed",
                 "response": {"id": "r", "status": "completed", "usage": MEASURED_USAGE}},
            ]
            body = b"".join(
                f"data: {json.dumps(f)}\r\n\r\n".encode("utf-8") for f in frames)
            return httpx.Response(200, content=body)

        from mvp.discovery.probe import probe

        result = probe(_record(), invocation=STREAM, pricing_key=REAL_PRICING_KEY,
                       wire_protocol="responses", http=_client(handler))
        assert result.passed is True, result.blocker

    def test_a_terminal_frame_split_across_chunks_is_framed(self, _system_tenant_pool):
        """The frame boundary can land anywhere, so the body is delivered as a byte
        ITERATOR that cuts the terminal frame mid-JSON and puts the blank-line
        terminator in a chunk of its own. A single `content=` fixture does not test
        this: the whole body arrives in one piece and a framer that dropped every
        frame spanning a chunk boundary would still pass.
        """
        terminal = (
            f"data: {json.dumps({'type': 'response.completed', 'response': {'id': 'r', 'usage': MEASURED_USAGE}})}"
        ).encode("utf-8")
        # The terminal is cut inside its JSON, its blank line arrives in a chunk of its
        # own, and ANOTHER frame follows it. The trailing frame is what makes this a real
        # discriminator: without it the leftover-buffer parse at the end of the loop
        # recovers the terminal whatever the framer did, so a framer that dropped every
        # frame spanning a chunk boundary would still pass.
        cut = len(terminal) // 2
        chunks = [
            terminal[:cut], terminal[cut:], b"\n", b"\n",
            b'data: {"type": "response.output_text.done"}\n\n',
        ]

        class _InPieces(httpx.SyncByteStream):
            """Yields the pieces SEPARATELY. `httpx.ByteStream(b"".join(chunks))`
            does not test this: it hands the body over in one piece, so a framer that
            dropped every frame spanning a chunk boundary would still pass."""

            def __iter__(self):
                yield from chunks

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=_InPieces(),
                                  headers={"content-type": "text/event-stream"})

        from mvp.discovery.probe import probe

        result = probe(_record(), invocation=STREAM, pricing_key=REAL_PRICING_KEY,
                       wire_protocol="responses", http=_client(handler))
        assert result.passed is True, result.blocker

    def test_a_terminal_that_arrives_before_a_broken_connection_is_still_charged(
        self, _system_tenant_pool
    ):
        """A transport fault AFTER a validated terminal does not unmake the
        measurement. Reporting it as indeterminate would hold a ceiling in place of an
        amount the provider already told us exactly."""
        frame = (
            f"data: {json.dumps({'type': 'response.completed', 'response': {'id': 'r', 'usage': MEASURED_USAGE}})}\n\n"
        ).encode("utf-8")

        class _BreaksAfterTheTerminal(httpx.SyncByteStream):
            def __iter__(self):
                yield frame
                raise httpx.ReadError("connection reset after the terminal event")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=_BreaksAfterTheTerminal(),
                                  headers={"content-type": "text/event-stream"})

        from mvp.discovery.probe import probe

        result = probe(_record(), invocation=STREAM, pricing_key=REAL_PRICING_KEY,
                       wire_protocol="responses", http=_client(handler))
        assert result.passed is True, result.blocker
        assert result.charged_microusd and result.charged_microusd > 0

    def test_a_break_before_any_terminal_is_still_a_transport_failure(
        self, _system_tenant_pool
    ):
        """The non-vacuous companion: tolerating a fault after a terminal must not
        tolerate one instead of a terminal."""
        class _BreaksImmediately(httpx.SyncByteStream):
            def __iter__(self):
                yield b'data: {"type": "response.created"}\n\n'
                raise httpx.ReadError("connection reset before any terminal")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=_BreaksImmediately(),
                                  headers={"content-type": "text/event-stream"})

        from mvp.discovery.probe import probe

        result = probe(_record(), invocation=STREAM, pricing_key=REAL_PRICING_KEY,
                       wire_protocol="responses", http=_client(handler))
        assert result.passed is False
        assert result.blocker is not None
        assert result.blocker.subtype != "usage_counters_missing", (
            "a broken connection is a transport outcome, not a readable 200 with no "
            "counters; conflating them loses which question an operator must answer"
        )


class TestWhatTheProbeDoesWithAnswersThatAreNotJsonObjects:
    @pytest.mark.parametrize(
        "content", [b"[1, 2, 3]", b'"a string"', b"not json at all"],
        ids=["list", "scalar", "garbage"],
    )
    def test_a_body_that_is_not_an_object_is_reported_as_unreadable_not_as_a_crash(
        self, _system_tenant_pool, content
    ):
        """Catching only the narrow shape error let a list body escape as an
        `AttributeError`, which the caller then classified through its catch-all --
        the same money outcome, but a verdict subtype that named the wrong thing."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=content,
                                  headers={"content-type": "application/json"})

        from mvp.discovery.probe import probe

        result = probe(_record(), invocation=SYNC, pricing_key=REAL_PRICING_KEY,
                       wire_protocol="responses", http=_client(handler))
        assert result.passed is False
        assert result.blocker is not None
        assert result.blocker.subtype == "usage_counters_missing", result.blocker


class TestAFailureBeforeTheWireRetainsNothing:
    def test_a_mint_failure_does_not_keep_the_reservation(self, _system_tenant_pool, monkeypatch):
        """`auth_headers` can mint, and a mint that fails is an attempt that never
        reached the provider. The hold derives "never left" from whether
        `provider_call_starting()` ran, and the probe calls `auth_headers` BEFORE it
        -- so the ordering is what makes this releasable, not the exception's class
        (a custom mint error classifies through the expensive catch-all)."""
        from mvp import _openai_transport
        from mvp.discovery.probe import probe

        def _boom(region):
            raise RuntimeError("token endpoint unreachable")

        monkeypatch.setattr(_openai_transport, "auth_headers", _boom)

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("the wire must not be reached when the mint failed")

        result = probe(_record(), invocation=SYNC, pricing_key=REAL_PRICING_KEY,
                       wire_protocol="responses", http=_client(handler))
        assert result.passed is False
        assert result.verdict is None


class TestTheClientSeamsCannotBeCrossed:
    """The realistic failure a silent seam produces is a test suite minting a real
    bearer and billing a real call, so crossing them raises rather than being
    ignored."""

    def test_a_converse_stub_supplied_for_the_responses_protocol_is_refused(self):
        from mvp.discovery.probe import probe

        with pytest.raises(TypeError) as caught:
            probe(_record(), invocation=SYNC, pricing_key=REAL_PRICING_KEY,
                  wire_protocol="responses", bedrock=object())
        assert "bedrock" in str(caught.value)

    def test_an_http_client_supplied_for_the_messages_protocol_is_refused(self):
        from mvp.discovery.probe import probe

        with pytest.raises(TypeError) as caught:
            probe(_record(), invocation=SYNC, pricing_key=REAL_PRICING_KEY,
                  wire_protocol="messages", http=object())
        assert "http" in str(caught.value)

    def test_the_right_seam_is_accepted(self, _system_tenant_pool):
        """The non-vacuous companion: the guard must not reject the correct seam."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "completed", "usage": MEASURED_USAGE})

        from mvp.discovery.probe import probe

        result = probe(_record(), invocation=SYNC, pricing_key=REAL_PRICING_KEY,
                       wire_protocol="responses", http=_client(handler))
        assert result.passed is True, result.blocker


class TestTheLedgerEffect:
    """Assertions on the LEDGER, not on the result object. A probe that reported a
    charge without persisting one, or a rejection that settled a zero, satisfies every
    other test in this file.
    """

    @staticmethod
    def _usage_rows() -> list:
        from boto3.dynamodb.conditions import Attr

        from dynamo.client import get_dynamodb_resource, usage_logs_table_name
        from mvp.discovery.records import SYSTEM_TENANT_ID

        table = get_dynamodb_resource().Table(usage_logs_table_name())
        return table.scan(
            FilterExpression=Attr("tenant_id").eq(SYSTEM_TENANT_ID)).get("Items", [])

    def test_a_passing_probe_writes_the_charge_it_reports(self, _system_tenant_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "completed", "usage": MEASURED_USAGE})

        from mvp.discovery.probe import probe

        before = len(self._usage_rows())
        result = probe(_record(), invocation=SYNC, pricing_key=REAL_PRICING_KEY,
                       wire_protocol="responses", http=_client(handler))
        assert result.passed is True, result.blocker

        rows = self._usage_rows()
        assert len(rows) == before + 1, (
            f"the probe reported {result.charged_microusd} micro-USD and wrote "
            f"{len(rows) - before} ledger rows; a reported charge that is not persisted "
            "is not a charge"
        )
        row = rows[-1]
        assert int(row.get("input_tokens", 0)) == MEASURED_USAGE["input_tokens"], row
        assert int(row.get("output_tokens", 0)) == MEASURED_USAGE["output_tokens"], row

    def test_an_upstream_rejection_writes_no_charge(self, _system_tenant_pool):
        """A 429 that settled a zero would record the probe as having been served for
        nothing, which is the "free tokens" shape in refusal clothing."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"message": "slow down"})

        from mvp.discovery.probe import probe

        before = len(self._usage_rows())
        result = probe(_record(), invocation=SYNC, pricing_key=REAL_PRICING_KEY,
                       wire_protocol="responses", http=_client(handler))
        assert result.passed is False
        assert len(self._usage_rows()) == before, "a rejected attempt wrote a usage row"


class TestAFaultWhileClosingTheStream:
    """The half a mock raising from `__iter__` cannot reproduce, and the one five
    independent reviewers named: the protective return used to sit AFTER the `with`, so
    an exception raised while the context manager CLOSED a broken connection skipped it
    and the validated measurement was discarded."""

    def test_a_close_that_raises_after_the_terminal_keeps_the_charge(
        self, _system_tenant_pool
    ):
        frame = (
            f"data: {json.dumps({'type': 'response.completed', 'response': {'id': 'r', 'usage': MEASURED_USAGE}})}\n\n"
        ).encode("utf-8")

        class _RaisesOnClose(httpx.SyncByteStream):
            def __iter__(self):
                yield frame

            def close(self) -> None:
                raise httpx.ReadError("connection reset while closing")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=_RaisesOnClose(),
                                  headers={"content-type": "text/event-stream"})

        from mvp.discovery.probe import probe

        result = probe(_record(), invocation=STREAM, pricing_key=REAL_PRICING_KEY,
                       wire_protocol="responses", http=_client(handler))
        assert result.passed is True, result.blocker
        assert result.charged_microusd and result.charged_microusd > 0

    def test_a_close_that_raises_without_a_terminal_is_still_a_failure(
        self, _system_tenant_pool
    ):
        """The negative control: tolerating a close fault after a terminal must not
        tolerate one instead of a terminal."""
        class _RaisesOnClose(httpx.SyncByteStream):
            def __iter__(self):
                yield b'data: {"type": "response.created"}\n\n'

            def close(self) -> None:
                raise httpx.ReadError("connection reset while closing")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=_RaisesOnClose(),
                                  headers={"content-type": "text/event-stream"})

        from mvp.discovery.probe import probe

        result = probe(_record(), invocation=STREAM, pricing_key=REAL_PRICING_KEY,
                       wire_protocol="responses", http=_client(handler))
        assert result.passed is False
