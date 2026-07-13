"""Regression test for A-01-app: streaming `_stream_messages` must NOT
call boto3's synchronous `converse_stream` from the event loop directly,
nor iterate the resulting EventStream synchronously.

A synchronous boto3 call inside an `async def` generator pins the
uvicorn event loop for the entire Bedrock TCP handshake; a synchronous
`for event in resp["stream"]` then pins it again for every chunk
boundary. Multi-tenant traffic (incl. /healthz) blocks until the call
returns. The fix offloads both via `asyncio.to_thread`.

The verifications below pin the contract through the helper
`_aiter_blocking_stream` plus a smoke test that the upstream
`StopIteration` is converted to a clean async generator return without
leaking `RuntimeError: generator raised StopIteration`.
"""
from __future__ import annotations

import asyncio


def test_aiter_blocking_stream_yields_in_order():
    from mvp.anthropic import _aiter_blocking_stream

    events = [
        {"contentBlockDelta": {"delta": {"text": "a"}}},
        {"contentBlockDelta": {"delta": {"text": "b"}}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]

    async def collect():
        out = []
        async for ev in _aiter_blocking_stream(iter(events)):
            out.append(ev)
        return out

    got = asyncio.run(collect())
    assert got == events


def test_aiter_blocking_stream_terminates_on_stopiteration():
    """If the upstream raises StopIteration, the helper must NOT propagate
    it as RuntimeError. Async generators that raise StopIteration get
    converted to RuntimeError by Python (PEP 479); the helper guards by
    swallowing it via a sentinel.
    """
    from mvp.anthropic import _aiter_blocking_stream

    async def collect():
        out = []
        async for ev in _aiter_blocking_stream(iter([])):
            out.append(ev)
        return out

    assert asyncio.run(collect()) == []


def test_aiter_blocking_stream_offloads_to_thread():
    """`next(...)` must be dispatched to the default thread executor so
    the event loop is free between events. We assert by checking the
    thread the iterator's `__next__` runs in differs from the main one.
    """
    import threading

    from mvp.anthropic import _aiter_blocking_stream

    main_thread_id = threading.get_ident()
    threads_seen: list[int] = []

    class TracingIter:
        def __init__(self):
            self._n = 0

        def __iter__(self):
            return self

        def __next__(self):
            threads_seen.append(threading.get_ident())
            self._n += 1
            if self._n > 2:
                raise StopIteration
            return {"contentBlockDelta": {"delta": {"text": "x"}}}

    async def collect():
        out = []
        async for ev in _aiter_blocking_stream(TracingIter()):
            out.append(ev)
        return out

    asyncio.run(collect())
    assert threads_seen, "the iterator must have been advanced"
    assert all(tid != main_thread_id for tid in threads_seen), (
        "every next() call must run in a worker thread, not on the event loop"
    )


def test_stream_messages_uses_to_thread_for_converse_stream(monkeypatch):
    """End-to-end: when `_stream_messages` invokes Bedrock, the actual
    `converse_stream` call must NOT happen synchronously on the event
    loop. We patch the bedrock client and verify the call is awaited
    via `asyncio.to_thread` (i.e. ran in a worker thread).
    """
    import threading

    from mvp import anthropic as anth

    main_thread_id = threading.get_ident()
    converse_thread_holder: dict[str, int] = {}

    class FakeStream:
        def __init__(self):
            self._events = iter(
                [
                    {"contentBlockDelta": {"delta": {"text": "hi"}}},
                    {"messageStop": {"stopReason": "end_turn"}},
                    {
                        "metadata": {
                            "usage": {"inputTokens": 5, "outputTokens": 1}
                        }
                    },
                ]
            )

        def __iter__(self):
            return self._events

    class FakeBedrock:
        def converse_stream(self, **kwargs):
            converse_thread_holder["tid"] = threading.get_ident()
            return {"stream": FakeStream()}

    from mvp.routing import infrarouter
    monkeypatch.setattr(infrarouter, "bedrock_client", lambda region: FakeBedrock())

    # Simulate the minimum tenants_repo / user surface the generator needs.
    class FakeRepo:
        def refund(self, **kwargs):
            pass

    class FakeUser:
        user_id = "u-1"
        org_id = "default-org"

    body = anth.AnthropicMessagesRequest.model_validate(
        {
            "model": "us.anthropic.claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16,
            "stream": True,
        }
    )

    # Make `_settle_reservation_and_log` a no-op so we do not need a
    # full DynamoDB / UsageLogs stack just to exercise the threading.
    monkeypatch.setattr(anth, "_settle_reservation_and_log", lambda **kw: None)

    async def drive():
        async for _chunk in anth._stream_messages(
            body=body,
            model_id="us.anthropic.claude-opus-4-7",
            user=FakeUser(),
            tenants_repo=FakeRepo(),
            reservation=100,
        ):
            pass

    asyncio.run(drive())
    assert "tid" in converse_thread_holder, "converse_stream must have been called"
    assert converse_thread_holder["tid"] != main_thread_id, (
        "converse_stream must run in a worker thread, not on the event loop"
    )
