"""Regression test for A-01-app: bedrock-runtime clients must ship with
explicit `connect_timeout` / `read_timeout` so a hung Bedrock TCP session
cannot pin a worker thread for an unbounded duration.

botocore's defaults are 60 s connect and *no* read timeout, which would
let a stuck Converse stream tie up an event-loop thread indefinitely.
The factory under `mvp/_bedrock_clients.py` must override both.
"""
from __future__ import annotations


def test_default_client_has_explicit_timeouts():
    from mvp._bedrock_clients import bedrock_runtime_client

    client = bedrock_runtime_client("us-east-1")
    cfg = client.meta.config
    # Both must be set and finite. Pinning the exact values is OK
    # because changes here are intentional and need a paired commit.
    assert cfg.connect_timeout == 10, (
        "connect_timeout must be explicitly set; default 60 s is too high "
        "for the Bedrock invocation hot path."
    )
    assert cfg.read_timeout == 120, (
        "read_timeout must be explicitly set; without it boto3 will block "
        "indefinitely on a silent Bedrock socket."
    )


def test_default_client_retries_are_capped():
    """Streaming responses must not be silently retried by the SDK; a
    mid-stream retry would double-bill credit. Cap to a small number;
    botocore exposes the cap under either `max_attempts` (legacy) or
    `total_max_attempts` (standard mode) — accept whichever is set.
    """
    from mvp._bedrock_clients import bedrock_runtime_client

    client = bedrock_runtime_client("us-east-1")
    retries = client.meta.config.retries or {}
    cap = retries.get("max_attempts") or retries.get("total_max_attempts")
    assert cap is not None, "retries must be configured"
    assert cap <= 3, (
        "More than 3 attempts risks silent mid-stream double-billing on "
        "streaming responses."
    )


def test_factory_returns_fresh_client_each_call():
    """ECS task-role credentials rotate via IMDS every ~6 hours; caching
    a single client snapshots the credential provider and starts emitting
    `ExpiredTokenException` after rotation. The factory must hand back a
    new instance on every call.
    """
    from mvp._bedrock_clients import bedrock_runtime_client

    a = bedrock_runtime_client("us-east-1")
    b = bedrock_runtime_client("us-east-1")
    assert a is not b
