"""Backend-agnostic streaming skeleton (layer a).

Owns the ONE canonical reserve → invoke → stream → end shape for a streamed
request, and nothing about money beyond *which observation* each ending reports.
The hold decides what that observation costs (`mvp._money.Hold`), so the endings
here are statements of fact rather than policy:

  - nothing was sent yet: a request abandoned before its provider call cannot have
    been billed.
  - cancelled during the provider call: the bytes may already be on the wire,
    which is the measured expensive case, so it is classified rather than settled
    at zero. Whether the reservation is then held or returned is the gate's
    decision (`STRATOCLAVE_UNOBSERVED_HOLDS`), not this file's.
  - invoke-time failure: the classifier decides from the exception.
  - the stream stopped: what arrived is charged, and `provider_responded` says
    whether anything came back — the usage block is the LAST event on Converse, so
    the token counters cannot answer that question.
  - clean completion / consumer stopped reading: the observation is charged.

The event loop drives normalized StreamEvents (from _converse_core.normalized_events)
through an injected adapter. Both the Anthropic Messages wire and the OpenAI Chat
Completions wire share this single machine.

Two orderings in here are load-bearing:

* **Claim, then yield, then write.** Every ending claims synchronously before the
  frame that announces it is handed to the caller. Yield first and the consumer can
  close on that very frame, which runs the `finally` — a different ending, of a
  different kind, claiming ahead of the one this branch decided on. The write stays
  single either way; what changes is which ending it records.
* **The write is dispatched by the ending, not by the call site.** `Ending.awaited()`
  shields, `Ending.detached()` needs no loop. A bare `await to_thread(write)` drops
  the write on a cancellation while the claim is already taken, and a spent claim
  cannot be replaced: the hold would end with nothing recorded.
"""
from __future__ import annotations

from typing import Any, AsyncGenerator, Callable, Iterable, Protocol

from . import _converse_types as t
from ._money import Hold


class StreamAdapter(Protocol):
    """Protocol for wire-format adapters driven by run_stream."""

    def prologue(self) -> Iterable[bytes]: ...
    def render_event(self, event: t.StreamEvent) -> Iterable[bytes]: ...
    def epilogue(self) -> Iterable[bytes]: ...
    def error_event(self, message: str) -> Iterable[bytes]: ...


async def run_stream(
    *,
    body: Any,
    model_id: str,
    hold: Hold,
    invoke_stream: Callable[..., Any],
    adapter: StreamAdapter,
) -> AsyncGenerator[bytes, None]:
    """Streaming flow. The hold's claim is what makes the ending single."""
    import asyncio

    from core.error_handler import sanitize_exception_message

    from . import _converse_core as core
    from . import provider_outcome as _outcome

    acc = t.UsageAccumulator()
    sent = False                 # the provider call was started
    provider_responded = False   # at least one event came back from it

    try:
        for frame in adapter.prologue():
            yield frame

        try:
            sent = True
            import inspect
            if inspect.iscoroutinefunction(invoke_stream):
                resp = await invoke_stream(body=body, model_id=model_id)
            else:
                resp = await asyncio.to_thread(invoke_stream, body=body, model_id=model_id)
        except asyncio.CancelledError:
            # The client went away while the request was in flight. `except
            # Exception` does not cover this, and letting the `finally` settle a
            # zero would treat a call that may have run to completion as free.
            ending = hold.claim_unobserved(
                state=_outcome.SUBMITTED_UNSETTLED, observation=acc,
                status="invoke_error",
            )
            if ending is not None:
                # Already cancelled: awaiting here would raise again.
                ending.detached()
            raise
        except Exception as e:
            # Nothing was streamed. Claim on THIS thread, before the error frame
            # goes out, so a client closing on that frame cannot end the hold
            # differently.
            ending = hold.claim_unobserved(exc=e, observation=acc, status="invoke_error")
            for frame in adapter.error_event(sanitize_exception_message(str(e))):
                yield frame
            if ending is not None:
                await ending.awaited()
            return

        try:
            async for event in core.normalized_events(resp.get("stream", [])):
                provider_responded = True
                acc.absorb(event)
                for frame in adapter.render_event(event):
                    yield frame
        except Exception as e:
            ending = hold.claim_stream_interrupted(
                acc, provider_responded=provider_responded, sent=sent, exc=e
            )
            for frame in adapter.error_event(sanitize_exception_message(str(e))):
                yield frame
            if ending is not None:
                await ending.awaited()
            return

        ending = hold.claim_settle(acc)
        for frame in adapter.epilogue():
            yield frame
        if ending is not None:
            await ending.awaited()
    finally:
        # Reached when the generator is closed before any ending claimed it: a
        # client disconnect, or a cancellation at an await that `except Exception`
        # does not see. Which ending that is depends on how far the request got,
        # and reading it as a zero settle in every case was the defect here.
        # An ending claimed just above, whose write the close interrupted, is
        # written here rather than lost — see Hold.dispatch_pending.
        # One call: it completes a write this close interrupted, or ends the hold
        # according to how far the request got. See Hold.close.
        hold.close(acc, sent=sent, provider_responded=provider_responded)
