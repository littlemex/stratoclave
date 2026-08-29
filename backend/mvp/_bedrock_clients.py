"""Bedrock client factory shared between the Anthropic Messages and OpenAI
Responses routes.

Clients are memoized per region for the life of the process.

An earlier version built a fresh client per call, to avoid snapshotting rotating
ECS task-role credentials. That reasoning does not hold: `botocore` gives the
request signer the session's credentials OBJECT, and for a container or IMDS role
that object is `RefreshableCredentials`, which re-fetches whenever a signature is
taken inside its refresh window. Rotation is therefore handled by the credentials,
not by rebuilding the client, and `test_cached_client_signs_with_rotated_credentials`
pins that wiring.

What a fresh client per call did cost is a new connection pool per call: two
clients from one session hold separate `http_session` objects, so every request
paid a fresh TLS handshake to Bedrock. Measured against this gateway on
2026-08-24, a request through it took p50 547 ms where the same model called
directly took 231 ms, and the gap did not close as concurrency rose — it is
per-request setup, not queueing. Memoizing removes that setup from every request
after the first.

Per-region selection is driven by `ModelEntry.bedrock_region` from the
model registry. The legacy `BEDROCK_REGION` env var is preserved as a
fallback for the Anthropic route only — OpenAI models ship with explicit
regions in their registry entry and never read this env.

Timeouts are explicit. botocore's defaults are 60 s (connect) and no
read timeout, which is wrong for our blast radius: a hung Bedrock TCP
session in `converse_stream` can pin a worker thread for an unbounded
duration. We pin both via `Config(connect_timeout=10, read_timeout=120)`;
streaming work continues to flow during the read window because the SDK
emits events as bytes arrive — `read_timeout` only fires when the upstream
goes silent for >120 s.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Optional

import boto3
from botocore.config import Config


# Defaults tuned for Bedrock invocations:
#   - connect_timeout: 10 s is generous for AWS-internal TLS handshake.
#   - read_timeout: 120 s caps the longest plausible "silent" stretch
#     between Bedrock SSE chunks. A model that genuinely needs >120 s of
#     thinking before its first token is misconfigured for our path.
#   - retries: the SDK makes exactly ONE attempt. An earlier version set
#     `max_attempts: 2` in "standard" mode believing that meant two attempts and
#     that the mode kept "quiet retries off for streaming responses". Both halves
#     were wrong. botocore's `Config(retries={"max_attempts": N})` means N
#     RETRIES — `ClientArgsCreator._compute_retry_max_attempts` rewrites it to
#     `total_max_attempts = N + 1` — so that config made up to THREE provider
#     invocations against a reservation priced for one, which is a ceiling breach
#     the ledger cannot see. And no retry mode retries mid-stream once the event
#     stream is returned, while "standard" mode DOES silently retry the initial
#     call on a connection error, read timeout included. Measured on real
#     Bedrock: `max_attempts=1` produced two counted invocations,
#     `total_max_attempts=1` produced exactly one; and an attempt abandoned on
#     read timeout is billed in full (1,493 output tokens for a call whose caller
#     received nothing). A retry the gateway cannot see is an unaccounted charge,
#     so retries belong to `mvp.routing.infrarouter`, which records an
#     `AttemptRecord` per attempt and can price them.
#   - max_pool_connections: botocore defaults to 10. Pooling the client is
#     pointless if its connection pool is smaller than the number of requests the
#     task admits: urllib3 opens the extra connections and then discards them
#     after use, which puts the TLS handshake back on every request past the
#     tenth. It therefore tracks the same per-task concurrency ceiling as the
#     thread limits in `_concurrency`.
MAX_POOL_CONNECTIONS_ENV = "BEDROCK_MAX_POOL_CONNECTIONS"

# Named (not inline) so `mvp._pipeline`'s reap-timeout derivation
# (CONTRACT-hard-ceiling.md section 5: "derive it from those values in code
# rather than choosing a constant") can import the SAME numbers this client
# is actually configured with, instead of a second, independently-maintained
# copy that could drift from the real timeout the moment either changes.
CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 120
# TOTAL attempts this client may make, not retries on top of one. The name says
# attempts and the wire key below says the same thing, so the reap-timeout
# derivation that multiplies by this number is multiplying by what actually
# happens. `total_max_attempts` is the unambiguous botocore key; `max_attempts`
# is off by one and was the defect.
RETRY_MAX_ATTEMPTS = 1


def bedrock_pool_size() -> int:
    """Connections this task may hold to Bedrock.

    Falls back to the process's request ceiling rather than to a literal of its
    own, so the two cannot drift apart. Validated at startup.
    """
    from core.aws_pool import max_pool_connections

    from ._concurrency import capacity_env_int

    # Read through the capacity validator first so an unusable value fails
    # startup like every other ceiling, then resolve the default.
    capacity_env_int(MAX_POOL_CONNECTIONS_ENV, max_pool_connections())
    return max_pool_connections(MAX_POOL_CONNECTIONS_ENV)


def _default_config() -> Config:
    return Config(
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=READ_TIMEOUT_SECONDS,
        retries={"total_max_attempts": RETRY_MAX_ATTEMPTS, "mode": "standard"},
        max_pool_connections=bedrock_pool_size(),
    )


# Memoized per region so the connection pool — and its TLS session — survives
# across requests. Guarded because a burst of concurrent requests would otherwise
# each build a client for the same region and race to install it, which is how a
# pool ends up per-request again under exactly the load it exists for.
_CLIENTS: dict[str, Any] = {}
_CLIENTS_LOCK = threading.Lock()

# A session of our own rather than boto3's module-level default. Concurrent client
# construction on the shared default session races on its event hooks and loaders,
# and construction happens on the request path the first time a region is used.
_SESSION = boto3.session.Session()


def bedrock_runtime_client(region: str, *, config: Optional[Config] = None):
    """Return the process-wide boto3 `bedrock-runtime` client for `region`.

    Memoized: see the module docstring for why that is safe under credential
    rotation and what the per-call version cost.

    `config` defaults to `_default_config()` so the timeouts and the connection
    pool size are always set. An explicit `config` is NOT memoized: a caller
    that overrides the defaults (a test injecting a mock, a route that needs a
    different read timeout) wants its own client, and caching it under the region
    key would hand those settings to everyone else.
    """
    if config is not None:
        # Still built under the lock: two threads constructing clients from one
        # session at the same time is the race the dedicated session avoids only
        # for the pooled path.
        with _CLIENTS_LOCK:
            return _build_client(region, config)
    client = _CLIENTS.get(region)
    if client is not None:
        return client
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(region)
        if client is None:
            client = _build_client(region, _default_config())
            _CLIENTS[region] = client
    return client


def _build_client(region: str, config: Config):
    return _SESSION.client(
        "bedrock-runtime",
        region_name=region,
        config=config,
    )


def reset_client_cache() -> None:
    """Close and drop the memoized clients. TESTS ONLY.

    Closing before dropping matters: the client owns a urllib3 pool, and letting
    it fall out of the dict unclosed leaks the sockets it holds for as long as the
    process lives. Not offered as an operational tool for the same reason as the
    OpenAI-transport equivalent — closing a client while requests are in flight severs them.
    """
    with _CLIENTS_LOCK:
        for client in _CLIENTS.values():
            close = getattr(client, "close", None)
            if callable(close):
                close()
        _CLIENTS.clear()


def deployment_client():
    """A `bedrock-runtime` client for the deployment's Converse region.

    This is the PRIMARY of the region policy the Converse chain is built from
    (`routing.chains` puts it first), so a route that invokes Converse without a
    chosen failover target must use it — otherwise the request is charged against a
    target in one region and invoked in another. It does NOT follow a failover
    selection: a caller that has picked an alternate target must build a client for
    that target's region. `ModelEntry.bedrock_region` is authoritative only for the
    OpenAI-compatible surface on bedrock-runtime.
    """
    region = os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION") or "us-east-1"
    return bedrock_runtime_client(region)


def client_for_model(entry):
    """Return a `bedrock-runtime` client for the region bound to `entry`.

    `entry.bedrock_region` is authoritative. The fallback chain
    (`BEDROCK_REGION` env → `us-east-1`) only fires when an entry is
    missing the field, which the registry today never does — kept as a
    safety net for future entries.

    The return type is intentionally unannotated: boto3 does not export a
    public type for its client factories, and threading `Any` here adds
    noise without buying anything `bedrock_runtime_client` does not.
    """
    region: Optional[str] = entry.bedrock_region
    if not region:
        region = os.getenv("BEDROCK_REGION") or "us-east-1"
    return bedrock_runtime_client(region)
