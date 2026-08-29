"""OpenAI-compatible transport: endpoint, auth, clients, error and usage parsing.

Two routes speak this surface — the OpenAI Responses route and the widened OpenAI
Chat Completions route — and they differ only in which path they POST to and what
they do with the body. Everything below that is transport: the endpoint template,
the short-lived bearer, client construction with the right timeouts, and reading an
error message or a usage block out of a response.

The endpoint is `bedrock-runtime`, the one AWS recommends; see `_ENDPOINT_TEMPLATE`
for what moving off `the OpenAI-compatible endpoint` bought and what it did not.

Keeping those here means the endpoint template exists once. A second copy is the
kind of duplication that goes unnoticed until one of them is pointed at the wrong
region or path and only one route breaks.

The bearer TTL is deliberately short: it lives in the ECS task heap as a plain
string, so a smaller window bounds the blast radius after a task compromise. A
SigV4-from-task-role migration is tracked as a follow-up.

Clients are pooled per region and the bearer is cached under its own TTL, and the
two are kept SEPARATE on purpose. An earlier version baked the Authorization
header into the client at construction, which tied the connection pool's lifetime
to the token's: refreshing the token meant discarding the pool, and reusing the
pool meant holding a token past its TTL. Pooling the connection and passing the
auth header per request lets each rotate on its own schedule, and it takes the
TLS handshake and the token mint off every request — both of which the
2026-08-24 concurrency measurement showed dominating this route's overhead.
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, Optional

import httpx
from fastapi import HTTPException

from core.error_handler import sanitize_exception_message
from core.logging import get_logger

logger = get_logger(__name__)

# 15 min, against the library default of 1 h (12 h max).
DEFAULT_TOKEN_TTL = timedelta(seconds=900)

# The OpenAI-compatible surface on `bedrock-runtime`, which is the endpoint AWS
# recommends: the `the OpenAI-compatible endpoint` documentation opens with "For new applications,
# we recommend the `bedrock-runtime` endpoint" and marks it as such in its own
# endpoint table.
#
# This used to point at `https://bedrock-runtime.{region}.amazonaws.com/openai/v1`, and
# moving it is not cosmetic. Two capabilities this gateway needs are documented as
# absent on the OpenAI-compatible endpoint and present here:
#
#   * Model invocation logging is "only supported for calls made through the
#     `bedrock-runtime` endpoint. This includes the OpenAI-compatible Responses and
#     Chat Completions APIs on that endpoint. Calls made through other endpoints,
#     such as the same APIs on `the OpenAI-compatible endpoint`, are not currently captured."
#     Verified: a call here produces a log record with `operation:
#     "ChatCompletions"` and its token counts. On the OpenAI-compatible endpoint there is no record at all,
#     so an abandoned call left nothing to reconcile against.
#   * The usage block carries `input_tokens_details.cached_tokens`,
#     `cache_write_tokens` and `output_tokens_details.reasoning_tokens`, so a
#     cached or reasoning-heavy call can be priced on what it actually was.
#
# What did NOT change is the auth: `provide_token` mints a Bedrock API key, and
# this endpoint accepts it as a bearer. Verified against a real call rather than
# assumed, which is why the token machinery below is untouched by the move.
#
# What this endpoint does NOT give us: `requestMetadata` is accepted and then
# recorded as `null` on the OpenAI-compatible APIs (the per-request metadata
# documentation lists only InvokeModel/Converse and their streaming forms). So
# these routes have aggregate evidence that an attempt occurred and what it cost,
# not per-hold attribution. `/v1/messages` on Converse has both.
#
# The `/openai/v1` prefix is part of the contract and distinct from the bare `/v1`
# the OpenAI-compatible endpoint served these models under.
_ENDPOINT_TEMPLATE = "https://bedrock-runtime.{region}.amazonaws.com/openai/v1"

# The non-streaming read window is bounded BELOW the CDN's origin timeout (60 s)
# on purpose. A request that outlives the CDN's patience reaches the caller as a
# CloudFront 504 with an HTML body: an unparseable failure for a problem that is
# neither the caller's nor the gateway's. Failing first, ourselves, means the caller
# gets the JSON 502 the rest of this surface returns. Measured on 2026-08-25, 21
# such 504s appeared in one open-loop run whose slowest upstream calls took 15-28 s.
#
# Streaming keeps the long window: bytes flow, so the CDN's timeout applies to each
# read rather than to the whole stream, and a reasoning model may legitimately stay
# quiet for a while before its first token.
NONSTREAM_READ_TIMEOUT_ENV = "OPENAI_TRANSPORT_NONSTREAM_READ_TIMEOUT_SECONDS"
DEFAULT_NONSTREAM_READ_TIMEOUT = 50.0


def nonstream_timeout() -> httpx.Timeout:
    """Per-request timeout for a non-streaming call.

    Passed per request rather than baked into the client, so the pooled connection
    is shared while the deadline stays specific to the call.
    """
    from ._concurrency import capacity_env_int

    seconds = float(
        capacity_env_int(
            NONSTREAM_READ_TIMEOUT_ENV, int(DEFAULT_NONSTREAM_READ_TIMEOUT)
        )
    )
    return httpx.Timeout(seconds, connect=10.0, pool=10.0)


# Long read window: a reasoning model can stay silent for a while before its first
# token. Connect stays tight because a slow TLS handshake is never the model.
#
# `pool` is set explicitly and SHORT. Waiting for a free connection happens after
# the budget reservation is taken, so a long pool wait holds a customer's balance
# hostage on a queue that is our own saturation rather than the model's work. Ten
# seconds is long enough to ride out a burst and short enough that the caller gets
# a 502 it can retry instead of a minutes-long hang.
_DEFAULT_TIMEOUT = httpx.Timeout(600.0, connect=10.0, pool=10.0)

# Named so `mvp._pipeline`'s reap-timeout derivation can read the SAME configured
# deadline this transport actually uses (docs/design/hard-ceiling.md section 5). httpx performs NO automatic retry by default and none is
# configured on this client (unlike the Bedrock botocore client's
# `RETRY_MAX_ATTEMPTS` in `_bedrock_clients.py`), so this transport's own
# retry budget is exactly 1 attempt.
STREAM_READ_TIMEOUT_SECONDS = 600.0
RETRY_MAX_ATTEMPTS = 1


def base_url(region: str) -> str:
    """The OpenAI-compatible base URL on `bedrock-runtime` for `region`."""
    return _ENDPOINT_TEMPLATE.format(region=region)


def mint_bearer_token(region: str) -> str:
    """Mint a short-lived bearer token (a Bedrock API key) for `region`."""
    # Imported lazily so the module loads even where the dependency is absent
    # (e.g. a dev environment running only the Converse tests); the route-time
    # failure is then a 503 rather than a worker that will not start.
    try:
        from aws_bedrock_token_generator import provide_token  # type: ignore
    except ImportError as exc:  # pragma: no cover — covered at deploy time
        raise HTTPException(
            status_code=503,
            detail=(
                "An OpenAI-compatible route is enabled but "
                "aws-bedrock-token-generator is not installed. "
                "Add it to backend/requirements.txt."
            ),
        ) from exc
    try:
        return provide_token(region=region, expiry=DEFAULT_TOKEN_TTL)
    except TypeError:
        # Older versions lack the `expiry` kwarg. Staying functional beats failing
        # closed here, but the token then defaults to 1 h.
        logger.warning(
            "bedrock_token_generator_no_expiry",
            region=region,
            note="library version does not support expiry kwarg; using default TTL",
        )
        return provide_token(region=region)


# How early a cached bearer is refreshed. Inside this window the cached token is
# still served — the OpenAI-compatible endpoint validates the credential when it admits the request, so a
# token that expires mid-stream does not interrupt one already accepted — while a
# single background refresh replaces it. That is what the margin is for: without
# it, every in-flight request in a region misses the cache at the same instant and
# they all queue behind one mint, which puts a periodic latency spike on the money
# path (settle and refund share the same offload threads).
#
# It is NOT a guarantee that a token outlives the request it is given to: the read
# window is 600 s and this is 120 s. Admission-time validation is the assumption
# that makes that acceptable; if the OpenAI-compatible endpoint ever revalidates mid-stream, this has to
# grow past the longest request instead.
_TOKEN_REFRESH_MARGIN = timedelta(seconds=120)

# Connection ceiling per pooled client. This is the per-task in-flight limit for
# the the OpenAI-compatible endpoint surface, so it has to be raised alongside the task count when the
# concurrency target moves; httpx would otherwise queue at its default of 100
# while the task still had CPU and threads to spare.
_MAX_CONNECTIONS_ENV = "OPENAI_TRANSPORT_MAX_CONNECTIONS"
_DEFAULT_MAX_CONNECTIONS = 256

def _now() -> float:
    """Monotonic seconds. Indirected so a test can control the clock without
    patching the stdlib module for everything else running in the process."""
    return time.monotonic()


_tokens: dict[str, tuple[str, float]] = {}
# Regions with a refresh already scheduled, so a burst of callers inside the
# window queues one mint rather than one per caller.
_refreshing: set[str] = set()
_refresh_guard = threading.Lock()
_refresher: Optional["ThreadPoolExecutor"] = None
# Per region. A single lock would make one region's mint block every other
# region's requests — including async streams, whose event loop waits on it.
_tokens_locks: dict[str, threading.Lock] = {}
_tokens_locks_guard = threading.Lock()
_sync_clients: dict[str, httpx.Client] = {}
_async_clients: dict[str, httpx.AsyncClient] = {}
_clients_lock = threading.Lock()


def openai_transport_connection_ceiling() -> int:
    """Connections this task may hold to the OpenAI-compatible endpoint. Validated at startup."""
    from ._concurrency import capacity_env_int

    return capacity_env_int(_MAX_CONNECTIONS_ENV, _DEFAULT_MAX_CONNECTIONS)


# How long an idle pooled connection is kept. httpx defaults to 5 s, which throws
# the pool away between bursts: measured on 2026-08-25, 400 requests sent in four
# bursts a few seconds apart produced 131 TLS handshakes, because every connection
# had expired in the gaps. Five minutes keeps a bursty client's pool warm while
# staying well inside the idle timeouts of the load balancers in front of the
# upstream, so we do not hand out a connection the peer has already dropped.
_KEEPALIVE_EXPIRY_ENV = "OPENAI_TRANSPORT_KEEPALIVE_EXPIRY_SECONDS"
_DEFAULT_KEEPALIVE_EXPIRY = 300.0


def _keepalive_expiry() -> float:
    from ._concurrency import capacity_env_int

    return float(
        capacity_env_int(_KEEPALIVE_EXPIRY_ENV, int(_DEFAULT_KEEPALIVE_EXPIRY))
    )


def _limits() -> httpx.Limits:
    # Keepalive matches the connection ceiling: a connection closed between
    # requests puts the TLS handshake back on the hot path, which is the cost this
    # pool exists to remove.
    ceiling = openai_transport_connection_ceiling()
    return httpx.Limits(
        max_connections=ceiling,
        max_keepalive_connections=ceiling,
        keepalive_expiry=_keepalive_expiry(),
    )


def auth_headers(region: str) -> dict[str, str]:
    """The Authorization header for `region`, minting only when the cache is cold.

    Passed per request rather than pinned to a client, so the token's TTL and the
    connection pool's lifetime stay independent.
    """
    hit = _cached_headers(region)
    if hit is not None:
        headers, due_for_refresh = hit
        if due_for_refresh:
            _schedule_refresh(region)
        return headers
    with _token_lock(region):
        # The clock is read again HERE, not before the lock: waiting for another
        # thread's mint can outlast the remaining life of the token this thread
        # arrived with, and a stale reading would hand back an expired bearer.
        cached = _tokens.get(region)
        if cached is None or cached[1] <= _now():
            cached = _mint_and_store(region)
    return {"Authorization": f"Bearer {cached[0]}"}


def _cached_headers(region: str) -> Optional[tuple[dict[str, str], bool]]:
    """`(headers, due_for_refresh)` for a usable cached token, else None.

    One decision for both the sync and the async caller. Duplicating it let the
    async path skip the refresh window entirely, which is exactly the case the
    window exists for — streams are the traffic that arrives in bulk.
    """
    cached = _tokens.get(region)
    if cached is None:
        return None
    remaining = cached[1] - _now()
    if remaining <= 0:
        return None
    return (
        {"Authorization": f"Bearer {cached[0]}"},
        remaining <= _TOKEN_REFRESH_MARGIN.total_seconds(),
    )


def _mint_and_store(region: str) -> tuple[str, float]:
    """Mint, store and return the entry. Caller holds the region's lock."""
    token = mint_bearer_token(region)
    entry = (token, _now() + DEFAULT_TOKEN_TTL.total_seconds())
    _tokens[region] = entry
    return entry


def _schedule_refresh(region: str) -> None:
    """Replace the region's token on a background thread, at most once at a time.

    The caller returns immediately with the token it already has. Doing the mint
    inline would mean the first caller inside the refresh window pays for it while
    holding a budget reservation, and doing it per caller would put every in-flight
    request in the region behind one mint at the moment of expiry — a periodic
    latency spike that lands on the money path, since settle and refund share the
    offload threads.

    Single-flight is by the `_refreshing` set rather than by the executor's queue:
    a one-worker executor would still accumulate one queued job per caller.
    """
    with _refresh_guard:
        if region in _refreshing:
            return
        _refreshing.add(region)
    try:
        _refresh_executor().submit(_refresh_now, region)
    except Exception:  # noqa: BLE001 — refusal to schedule must not fail a request
        with _refresh_guard:
            _refreshing.discard(region)
        raise


def _refresh_now(region: str) -> None:
    """Body of the background refresh. Runs on the refresher thread."""
    try:
        with _token_lock(region):
            cached = _tokens.get(region)
            if cached is not None and (
                cached[1] - _now() > _TOKEN_REFRESH_MARGIN.total_seconds()
            ):
                return  # somebody already replaced it
            _mint_and_store(region)
    except Exception:  # noqa: BLE001 — the cached token is still usable
        logger.warning(
            "openai_bearer_background_refresh_failed",
            region=region,
            note="serving the cached token for the rest of its life",
        )
    finally:
        with _refresh_guard:
            _refreshing.discard(region)


async def auth_headers_async(region: str) -> dict[str, str]:
    """`auth_headers` for an async caller.

    A cache hit answers inline. A miss mints, and minting takes a lock and does
    signing work, so it goes to a thread: on the event loop it would stall every
    other in-flight request — including the streams this route exists for — for the
    duration of one mint, and the loop would block on `threading.Lock.acquire`
    behind whichever worker thread happens to be minting.
    """
    hit = _cached_headers(region)
    if hit is not None:
        headers, due_for_refresh = hit
        if due_for_refresh:
            _schedule_refresh(region)
        return headers
    import asyncio

    return await asyncio.to_thread(auth_headers, region)


def invalidate_token(region: str, used: Optional[dict[str, str]] = None) -> None:
    """Drop the cached bearer for `region` when the upstream rejected it.

    Under per-request minting a dead token cost one request, because the next one
    minted again. A cached token would instead be handed to every request in the
    region until its TTL ran out, and independently per task, so a credential that
    stops working takes the whole the OpenAI-compatible endpoint surface down for a refresh interval rather
    than for one request. The money path is unaffected either way — those responses
    refund — but the outage is real.

    Two deliberate details:

    * No lock. `dict.pop` is atomic, and this is called from the event loop on
      three of its four paths. The region's lock is held for the whole of a mint,
      and a credential service that is returning 401 is exactly the one whose mint
      is slow, so taking that lock here would block the loop — every stream, and
      `/health` with them — during the failure this function exists to handle.
    * `used` is the header the caller actually sent. Without comparing it, a burst
      of 401s carrying the OLD token would each discard whatever is in the cache,
      including a good token a refresh had just installed, and every discard costs
      another mint. Passing it makes this a compare-and-pop.
    """
    cached = _tokens.get(region)
    if cached is None:
        return
    if used is not None and used.get("Authorization") != f"Bearer {cached[0]}":
        return  # already replaced; the rejected token is gone anyway
    _tokens.pop(region, None)


def _drain_refresher() -> None:
    """Wait for any scheduled refresh, then discard the refresher.

    Tests assert on what the refresh produced, so they need a point where it has
    certainly finished; leaving a live refresher between tests also lets one test's
    mint land in another's cache.
    """
    global _refresher
    with _refresh_guard:
        refresher, _refresher = _refresher, None
    if refresher is not None:
        refresher.shutdown(wait=True)
    with _refresh_guard:
        _refreshing.clear()


def _refresh_executor() -> "ThreadPoolExecutor":
    """The one thread that renews bearers.

    Its own thread on purpose: the offload executor carries settle and refund, and
    a token renewal must not queue behind — or ahead of — accounting work.
    """
    global _refresher
    if _refresher is not None:
        return _refresher
    with _refresh_guard:
        if _refresher is None:
            from concurrent.futures import ThreadPoolExecutor

            _refresher = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="the OpenAI-compatible endpoint-token-refresh"
            )
    return _refresher


def _token_lock(region: str) -> threading.Lock:
    lock = _tokens_locks.get(region)
    if lock is not None:
        return lock
    with _tokens_locks_guard:
        return _tokens_locks.setdefault(region, threading.Lock())


def async_client(region: str) -> httpx.AsyncClient:
    """The pooled async OpenAI-transport client for `region`.

    Use for streaming: a sync client would hold a threadpool worker for the life
    of the stream. Process-wide and NOT to be closed by the caller — closing it
    would drop connections that other in-flight requests are using. Callers pass
    `auth_headers(region)` per request.

    There is deliberately no per-call override: a factory that returns a pooled
    client for one caller and a caller-owned client for another inverts the
    ownership rule behind one name, which is how a pooled connection ends up
    closed by whoever thought they owned it.
    """
    client = _async_clients.get(region)
    if client is not None and not client.is_closed:
        return client
    with _clients_lock:
        client = _async_clients.get(region)
        if client is None or client.is_closed:
            # Rebuilt rather than trusted. Under the old per-request construction
            # any mistake cost one request; with a pooled client, one stray close
            # would otherwise fail every request for this region until the task is
            # replaced. The comment asking callers not to close it is not a
            # guarantee, so this does not depend on one.
            client = httpx.AsyncClient(
                base_url=base_url(region), timeout=_DEFAULT_TIMEOUT, limits=_limits()
            )
            _async_clients[region] = client
    return client


def sync_client(region: str) -> httpx.Client:
    """The pooled blocking the OpenAI-compatible endpoint client for `region`.

    Same ownership rule as `async_client`: process-wide, not closed by the caller,
    auth passed per request, and no per-call override.
    """
    client = _sync_clients.get(region)
    if client is not None and not client.is_closed:
        return client
    with _clients_lock:
        client = _sync_clients.get(region)
        if client is None or client.is_closed:
            # See `async_client`: a closed pooled client must heal, not poison the
            # region for the life of the process.
            client = httpx.Client(
                base_url=base_url(region), timeout=_DEFAULT_TIMEOUT, limits=_limits()
            )
            _sync_clients[region] = client
    return client


def reset_transport_cache_for_tests() -> None:
    """Drop the pooled clients and cached bearers. TESTS ONLY.

    Deliberately not offered as an operational tool: closing a pooled client while
    requests are in flight severs their connections, so "force reconstruction" is
    not a safe thing to expose on a running service. Use `aclose_all()` from the
    application lifespan for an orderly shutdown instead.
    """
    with _clients_lock:
        sync_clients = list(_sync_clients.values())
        async_clients = list(_async_clients.values())
        _sync_clients.clear()
        _async_clients.clear()
    _drain_refresher()
    for sync in sync_clients:
        sync.close()
    # The async clients need a loop to close on. `aclose_all` is the orderly path;
    # this helper exists for sync tests, so it borrows a loop rather than dropping
    # the clients unclosed — the very leak `aclose_all` was added to avoid.
    if async_clients:
        import asyncio

        async def _close_them() -> None:
            for client in async_clients:
                await client.aclose()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_close_them())
        else:
            loop.create_task(_close_them())
    with _tokens_locks_guard:
        for region in list(_tokens_locks):
            with _tokens_locks[region]:
                _tokens.pop(region, None)


async def aclose_all() -> None:
    """Close every pooled client and stop the token refresher. Application shutdown.

    The async clients need `await`, which is why this exists separately from the
    test helper: dropping them from the dict without closing them leaks their
    connections for as long as the process lives.
    """
    with _clients_lock:
        sync_clients = list(_sync_clients.values())
        async_clients = list(_async_clients.values())
        _sync_clients.clear()
        _async_clients.clear()
    global _refresher
    with _refresh_guard:
        refresher, _refresher = _refresher, None
        _refreshing.clear()
    if refresher is not None:
        refresher.shutdown(wait=False)
    for sync in sync_clients:
        sync.close()
    for client in async_clients:
        await client.aclose()


# Paths an upstream may tell a caller to use, mapped to the path THIS gateway
# serves the same API on. Only the gateway knows its own routes, so a relayed
# message naming a path it does not serve sends the caller to a 404 — reported by
# a caller who followed the instruction and had to read the router table to
# recover. Keys are matched longest-first so a prefix never shadows a full path.
_PATH_REWRITES = {
    "/v1/responses": "/openai/v1/responses",
    "/responses": "/openai/v1/responses",
}


def rewrite_served_paths(message: str) -> str:
    """Replace endpoint paths the gateway does not serve with the ones it does.

    Applied to every relayed upstream message. A path the gateway DOES serve
    (`/v1/chat/completions`, `/v1/messages`, `/openai/v1/responses`) is left
    exactly as it is, and a message naming no path is returned unchanged.
    """
    if not message:
        return message
    out = message
    for wrong, right in sorted(_PATH_REWRITES.items(), key=lambda kv: -len(kv[0])):
        if wrong in out and right not in out:
            out = out.replace(wrong, right)
    return out


def format_error(resp: httpx.Response) -> str:
    """A sanitized error message from a non-2xx the OpenAI-compatible endpoint response.

    Relayed messages go through `rewrite_served_paths`: the upstream composes its
    advice for its own routes, and this gateway exposes the Responses API on a
    different path, so passing the sentence through verbatim tells the caller to
    call something that does not exist here.
    """
    try:
        body = resp.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                msg = err.get("message")
                if isinstance(msg, str) and msg:
                    return rewrite_served_paths(sanitize_exception_message(msg))
    except Exception:  # noqa: BLE001 — a non-JSON error body is still an error
        pass
    return rewrite_served_paths(sanitize_exception_message(resp.text[:500]))


def extract_usage(usage: Any) -> tuple[int, int]:
    """Return `(input_tokens, output_tokens)` from a the OpenAI-compatible endpoint usage block.

    Accepts both the Responses spelling (`input_tokens`/`output_tokens`) and the
    Chat Completions one (`prompt_tokens`/`completion_tokens`), because the two
    routes share this parser and intermediate proxies normalise inconsistently.
    `output_tokens_details.reasoning_tokens` is a SUBSET of `output_tokens` in the
    Responses contract; it must not be added separately.
    """
    if not isinstance(usage, dict):
        return 0, 0
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens", 0) or 0)
    return input_tokens, output_tokens
