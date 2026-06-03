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
"""
from __future__ import annotations

import os
from typing import Optional

import boto3


def bedrock_runtime_client(region: str):
    """Return a fresh boto3 `bedrock-runtime` client bound to `region`.

    Not memoized: see module docstring for the IMDS-rotation rationale.
    Construction overhead is dominated by HTTPS connection setup on the
    first call; subsequent calls reuse the underlying urllib3 pool.
    """
    return boto3.client("bedrock-runtime", region_name=region)


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
