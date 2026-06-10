"""Bedrock client factory shared between the Anthropic Messages and OpenAI
Responses routes.

The factory deliberately constructs a fresh `boto3.client("bedrock-runtime",
...)` per call. ECS task-role credentials rotate every ~6 hours via IMDS;
a long-lived cached client snapshots the credential provider chain at
construction time and starts emitting `ExpiredTokenException` after rotation
unless the cache is invalidated. boto3 client construction is cheap
relative to a Bedrock invocation, so caching here would optimise a non-hot
path while introducing a real production footgun.

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
from typing import Optional

import boto3
from botocore.config import Config


# Defaults tuned for Bedrock invocations:
#   - connect_timeout: 10 s is generous for AWS-internal TLS handshake.
#   - read_timeout: 120 s caps the longest plausible "silent" stretch
#     between Bedrock SSE chunks. A model that genuinely needs >120 s of
#     thinking before its first token is misconfigured for our path.
#   - retries.mode "standard" keeps boto3 quiet retries off for streaming
#     responses (retrying mid-stream silently double-bills).
_DEFAULT_BOTO_CONFIG = Config(
    connect_timeout=10,
    read_timeout=120,
    retries={"max_attempts": 2, "mode": "standard"},
)


def bedrock_runtime_client(region: str, *, config: Optional[Config] = None):
    """Return a fresh boto3 `bedrock-runtime` client bound to `region`.

    Not memoized: see module docstring for the IMDS-rotation rationale.
    Construction overhead is dominated by HTTPS connection setup on the
    first call; subsequent calls reuse the underlying urllib3 pool.

    `config` defaults to `_DEFAULT_BOTO_CONFIG` so `connect_timeout` and
    `read_timeout` are always set. Tests that need to override (e.g.
    inject a mock with no timeout) can pass an explicit Config.
    """
    return boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=config or _DEFAULT_BOTO_CONFIG,
    )


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
