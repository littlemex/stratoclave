"""Self-hosted vLLM serving branch (hybrid serving, P0).

This module is the ENTIRE vLLM transport. It is reachable ONLY from
``infrarouter._attempt_invoke`` and ONLY when both are true:

  * ``HYBRID_SERVING_ENABLED=true`` on the ECS task, AND
  * the committed ``Target.served_by == "vllm"``.

With the flag off (the shipped default) and every registry entry defaulting
``served_by="bedrock"``, nothing here is imported on the hot path and no HTTP
client is ever constructed — the Bedrock path is byte-behaviour-identical.

WHY A SYNCHRONOUS, BEDROCK-SHAPED GENERATOR
-------------------------------------------
``_attempt_invoke`` is called via ``asyncio.to_thread`` and returns a
``converse_stream``-shaped dict whose ``["stream"]`` is a BLOCKING iterable of
Bedrock-shaped event dicts. ``_peek_first_event`` (a daemon thread), the
first-event-commit, the fault-injection hook and ``_converse_core`` all consume
exactly that shape. So the vLLM branch:

  1. runs fully synchronously inside the worker thread (no coroutine — you
     cannot ``to_thread`` a coroutine),
  2. translates the OpenAI-compatible SSE stream into the SAME Bedrock event
     dicts BEFORE they reach ``_peek_first_event``/``normalized_events`` (so
     those run unchanged), and
  3. returns ``{"stream": <blocking generator>, "ResponseMetadata": {...}}``.

FIRST-EVENT COMMIT
------------------
The first SSE chunk (usually the role-only delta) is translated to a
``messageStart`` event — deliberately symmetric with the Bedrock stream's
commit point. A 200 that never sends a chunk yields nothing: the existing
``_peek_first_event`` first-event timeout fires and the attempt fails
pre-commit, so no money is committed.

EXCEPTION TAXONOMY (so ``classify()`` is untouched)
---------------------------------------------------
``classify()`` keys FAILOVER off botocore/stdlib timeout + ``OSError``. httpx
raises its OWN exception hierarchy (not ``OSError`` subclasses), so a raw httpx
error would fall through to FATAL (a 500 to the client) — wrong for a dead
self-hosted endpoint. We therefore translate at THIS boundary:

  * connect failure / timeout / read timeout / protocol error / mid-stream
    disconnect / HTTP 5xx / 429  -> ``ConnectionError`` or ``TimeoutError``
    (both route to FAILOVER; a single-target vLLM chain then exhausts and the
    request fails cleanly with that error — never a silent success),
  * a clean 200 that closes with zero chunks -> ``RuntimeError("empty stream:
    vllm")`` (the existing empty-stream FAILOVER branch),
  * HTTP 4xx (a malformed request, incl. an old build rejecting
    ``stream_options``) -> ``VllmClientError`` -> FATAL (retrying/failing over
    a bad request is pointless).

MONEY
-----
The synthesized ``metadata`` event ALWAYS carries the full four usage keys with
the cache fields hard-zero (vLLM has no Bedrock cache-token split), so
``normalized_events`` populates ``Usage`` with cache_read=cache_write=0. If the
stream ends cleanly with NO usage chunk we emit NO metadata event; the
``_budget_flow`` finalizer (the sole money authority) then settles at the
reserved amount (over-charge-safe) — this module never fabricates usage.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Iterator, Optional

import httpx

from core.logging import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------
# Flag + endpoint allowlist (SSRF guard: the URL set is closed at process
# start from an operator-set env/param; never from a request or the registry).
# --------------------------------------------------------------------------

def hybrid_serving_enabled() -> bool:
    """Master switch, checked at request time (not import time) so an operator
    can flip it via env without a code change — same pattern as SAAR_ENABLED."""
    return os.getenv("HYBRID_SERVING_ENABLED", "false").lower() == "true"


class VllmClientError(Exception):
    """A 4xx from the vLLM endpoint: a malformed/unservable REQUEST (bad params,
    an old build rejecting stream_options, ...). Routes to FATAL in classify()
    because retrying or failing over an invalid request cannot help."""


# Connect fast, read-timeout UNDER the router's 10s first-event backstop
# (infrarouter._FIRST_EVENT_TIMEOUT_S) so a 200-that-never-chunks endpoint is
# reaped by httpx's own read timeout shortly after the router has already failed
# the attempt over — the reader thread is blocked in the socket read and only
# checks its stop signal between frames, so the socket read timeout (not the
# stop flag) is the real leak bound. Keeping it < 10s means the thread + pooled
# connection are released promptly rather than pinned for tens of seconds.
_CONNECT_TIMEOUT_S = 2.0
_READ_TIMEOUT_S = 8.0

_ENDPOINTS: Optional[dict[str, str]] = None
_endpoints_lock = threading.Lock()
_clients: dict[str, httpx.Client] = {}
_clients_lock = threading.Lock()


def _load_endpoints() -> dict[str, str]:
    """Parse the operator endpoint allowlist from VLLM_ENDPOINTS (a JSON object
    ``{"<endpoint_key>": "<internal-url>"}``). Values are internal VPC URLs set
    by IaC; keys are the opaque tokens the registry references. Malformed or
    absent config yields an empty map (every vLLM entry becomes unservable —
    fail-closed to Bedrock-only)."""
    raw = os.getenv("VLLM_ENDPOINTS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception as e:  # noqa: BLE001 — malformed config must not crash boot.
        logger.warning("vllm_endpoints_parse_failed", error=str(e))
        return {}
    if not isinstance(parsed, dict):
        logger.warning("vllm_endpoints_not_object")
        return {}
    out: dict[str, str] = {}
    for k, v in parsed.items():
        if isinstance(k, str) and isinstance(v, str) and v.startswith(("http://", "https://")):
            out[k] = v.rstrip("/")
    return out


def endpoints() -> dict[str, str]:
    global _ENDPOINTS
    if _ENDPOINTS is None:
        with _endpoints_lock:
            if _ENDPOINTS is None:
                _ENDPOINTS = _load_endpoints()
    return _ENDPOINTS


def endpoint_is_servable(endpoint_key: Optional[str]) -> bool:
    """True iff hybrid serving is on AND the key resolves to an allowlisted URL.
    Used by the registry/chain layer to mark a vLLM entry servable or not — the
    SAME servability filtering an unavailable Bedrock region gets."""
    return bool(endpoint_key) and hybrid_serving_enabled() and endpoint_key in endpoints()


def reset_for_test() -> None:
    """Drop cached endpoint map + clients so a test can vary VLLM_ENDPOINTS."""
    global _ENDPOINTS
    with _endpoints_lock:
        _ENDPOINTS = None
    with _clients_lock:
        for c in _clients.values():
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        _clients.clear()


def _client_for(endpoint_key: str) -> tuple[httpx.Client, str]:
    """Lazily build (and memoize) a pooled sync httpx client for an endpoint
    key. Lazy so the flag-off path constructs nothing. Returns (client, base_url).

    ``retries`` are left at httpx's default of 0 (no transport retries): the
    router's own attempt/failover machinery is the ONLY retry mechanism, and a
    hidden client retry would both mask the exception taxonomy and multiply load
    on a browning endpoint."""
    base = endpoints().get(endpoint_key)
    if not base:
        # SSRF guard: an unknown key never reaches here (servability filter),
        # but fail-closed defensively rather than inventing a URL.
        raise VllmClientError(f"unknown vllm endpoint_key: {endpoint_key!r}")
    with _clients_lock:
        client = _clients.get(endpoint_key)
        if client is None:
            timeout = httpx.Timeout(
                connect=_CONNECT_TIMEOUT_S, read=_READ_TIMEOUT_S,
                write=_CONNECT_TIMEOUT_S, pool=_CONNECT_TIMEOUT_S,
            )
            client = httpx.Client(base_url=base, timeout=timeout)
            _clients[endpoint_key] = client
    return client, base


# --------------------------------------------------------------------------
# Converse payload -> OpenAI chat.completions (text + system + inferenceConfig).
# P0 does NOT map tools/multimodal: a request carrying those makes a vLLM entry
# unservable at resolve time (pre-reserve), so this branch only ever sees plain
# text turns. We assert that here as a defensive backstop, not a feature.
# --------------------------------------------------------------------------

def _converse_to_openai(model_id: str, payload: dict) -> dict:
    messages: list[dict] = []
    system = payload.get("system")
    if system:
        # Bedrock system is a list of {"text": ...} blocks (or a bare string).
        if isinstance(system, list):
            sys_text = "".join(b.get("text", "") for b in system if isinstance(b, dict))
        else:
            sys_text = str(system)
        if sys_text:
            messages.append({"role": "system", "content": sys_text})

    for m in payload.get("messages", []):
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            # P0: text-only. Non-text blocks should have been filtered pre-reserve.
            text = "".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and "text" in b
            )
        else:
            text = str(content)
        messages.append({"role": role, "content": text})

    body: dict = {
        "model": model_id,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    inf = payload.get("inferenceConfig") or {}
    if "maxTokens" in inf:
        body["max_tokens"] = int(inf["maxTokens"])
    if "temperature" in inf:
        body["temperature"] = float(inf["temperature"])
    if "topP" in inf:
        body["top_p"] = float(inf["topP"])
    if inf.get("stopSequences"):
        body["stop"] = list(inf["stopSequences"])
    return body


_FINISH_TO_BEDROCK = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",   # unreachable in P0 (tools filtered pre-reserve)
    "content_filter": "content_filtered",
}


def _translate_sse(resp: httpx.Response) -> Iterator[dict]:
    """Translate an OpenAI-compatible chat.completions SSE stream into the
    Bedrock event-dict sequence ``normalized_events`` consumes. Yields nothing
    until the first real chunk (first-event commit symmetry). Closes the HTTP
    response in ``finally`` on ANY exit (normal, error, or generator .close()
    on abandonment) so an abandoned/hung stream cannot leak the connection."""
    started = False
    block_open = False
    saw_any = False
    finish_reason: Optional[str] = None
    usage_event: Optional[dict] = None
    try:
        for line in resp.iter_lines():
            if not line:
                continue
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:  # noqa: BLE001 — a malformed frame is a broken stream.
                raise ConnectionError("vllm malformed SSE frame")
            saw_any = True

            # Usage chunk (choices == [] when stream_options.include_usage).
            usage = obj.get("usage")
            if usage and not obj.get("choices"):
                # Coerce defensively: a broken/hostile endpoint sending a
                # non-numeric prompt_tokens (or null) would make a bare int()
                # raise ValueError/TypeError, which is NOT in this generator's
                # translated-exception set and would leak out as a raw error ->
                # classify() has no branch for it -> FATAL (500 to the client) —
                # exactly the "a dead self-hosted endpoint must FAILOVER, not
                # FATAL" property this module exists to preserve. Treat a
                # malformed usage chunk as a broken stream (ConnectionError ->
                # FAILOVER), symmetric with the malformed-SSE-frame branch above.
                try:
                    in_tok = int(usage.get("prompt_tokens", 0))
                    out_tok = int(usage.get("completion_tokens", 0))
                except (ValueError, TypeError) as e:
                    raise ConnectionError(f"vllm malformed usage chunk: {e}") from e
                usage_event = {
                    "metadata": {
                        "usage": {
                            "inputTokens": in_tok,
                            "outputTokens": out_tok,
                            "cacheReadInputTokens": 0,
                            "cacheWriteInputTokens": 0,
                        }
                    }
                }
                continue

            choices = obj.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}

            if not started:
                started = True
                yield {"messageStart": {"role": delta.get("role", "assistant")}}

            text = delta.get("content")
            if text:
                if not block_open:
                    block_open = True
                    yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
                yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}}

            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

        if not saw_any:
            # A 200 that closed with zero data frames: existing empty-stream
            # FAILOVER branch. (A truly zero-chunk connection never reaches
            # here — _peek_first_event's timeout fires first — but a stream that
            # opens and closes without a frame lands here.)
            raise RuntimeError("empty stream: vllm")

        if block_open:
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {
            "messageStop": {
                "stopReason": _FINISH_TO_BEDROCK.get(finish_reason or "stop", "end_turn")
            }
        }
        # Emit usage LAST (Bedrock orders metadata after messageStop). If it is
        # missing we deliberately emit nothing — the finalizer settles at reserve.
        if usage_event is not None:
            yield usage_event
    except (RuntimeError, ConnectionError, TimeoutError, VllmClientError):
        raise
    except httpx.TimeoutException as e:
        raise TimeoutError(f"vllm read timeout: {e}") from e
    except httpx.HTTPError as e:
        # Mid-stream disconnect / protocol error.
        raise ConnectionError(f"vllm stream error: {e}") from e
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def vllm_invoke(target, payload: dict) -> dict:
    """Synchronous vLLM invoke. Runs inside ``asyncio.to_thread`` from
    ``_attempt_invoke``. Returns a ``converse_stream``-shaped dict; raises the
    Bedrock/stdlib exception taxonomy so ``classify()`` is untouched.

    All connect-phase failures raise BEFORE returning any stream, so the router
    sees zero events and fails over / exhausts the chain pre-commit."""
    endpoint_key = getattr(target, "endpoint_key", None)
    client, _base = _client_for(endpoint_key)
    body = _converse_to_openai(target.model_id, payload)

    # Open the stream synchronously. httpx.Client.stream is a context manager;
    # we must keep it open across the generator's lifetime, so drive it manually.
    try:
        cm = client.stream(
            "POST", "/v1/chat/completions", json=body,
            headers={"Accept": "text/event-stream", "Accept-Encoding": "identity"},
        )
        resp = cm.__enter__()
    except httpx.ConnectError as e:
        raise ConnectionError(f"vllm connect failed: {e}") from e
    except httpx.TimeoutException as e:
        raise TimeoutError(f"vllm connect timeout: {e}") from e
    except httpx.HTTPError as e:
        raise ConnectionError(f"vllm transport error: {e}") from e

    status = resp.status_code
    if status >= 500 or status == 429:
        try:
            resp.close()
        finally:
            cm.__exit__(None, None, None)
        raise ConnectionError(f"vllm {status}")
    if status >= 400:
        try:
            resp.close()
        finally:
            cm.__exit__(None, None, None)
        raise VllmClientError(f"vllm {status}")

    def _stream() -> Iterator[dict]:
        try:
            yield from _translate_sse(resp)
        finally:
            try:
                cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    return {"stream": _stream(), "ResponseMetadata": {"HTTPStatusCode": status}}
