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


def test_sdk_makes_exactly_one_attempt():
    """The SDK must not re-send a request the ledger has priced once.

    This assertion used to accept any cap up to 3, reasoning about "mid-stream
    double-billing". Both parts were wrong, and the loophole is what let the real
    defect ship. No retry mode retries mid-stream once the event stream is
    returned; what standard mode does do is silently re-send the INITIAL call on
    a connection error, read timeout included. And a read timeout does not mean
    the provider did no work: measured on real Bedrock, a call abandoned at 2 s
    was still billed 1,493 output tokens.

    The cap is also read from `total_max_attempts` only, because
    `Config(retries={"max_attempts": N})` means N RETRIES — botocore rewrites it
    to `total_max_attempts = N + 1`. Accepting either key, as this test did,
    cannot tell two attempts from three.
    """
    from mvp._bedrock_clients import bedrock_runtime_client

    retries = bedrock_runtime_client("us-east-1").meta.config.retries or {}
    assert retries.get("max_attempts") is None, (
        "`max_attempts` is off by one against its name; configure "
        "`total_max_attempts` so the number means attempts."
    )
    assert retries.get("total_max_attempts") == 1, (
        "the SDK must make exactly one attempt: a retry it makes on its own is "
        "an unaccounted provider charge, so retries belong to the router, which "
        "records and can price each attempt."
    )


def test_routing_client_is_the_same_configured_client():
    """The failover path must not get its own unconfigured client.

    `mvp.routing.clients` used to call `boto3.client("bedrock-runtime")` with no
    Config, so the one path that re-sends on purpose ran with no read timeout and
    botocore's default of three attempts — and this file never noticed, because
    it only ever inspected the other factory.
    """
    from mvp._bedrock_clients import bedrock_runtime_client
    from mvp.routing.clients import bedrock_client

    assert bedrock_client("us-east-1") is bedrock_runtime_client("us-east-1")


def test_factory_pools_per_region():
    """The factory hands back one client per region.

    An earlier version built a fresh client per call so that rotating ECS
    task-role credentials could not be snapshotted. That cost a new connection
    pool — and a new TLS handshake — on every request, and it was unnecessary:
    the signer holds the credentials object, which refreshes itself. Pooling and
    its rotation invariant are covered in
    `test_transport_pooling_and_capacity.py`.
    """
    from mvp._bedrock_clients import bedrock_runtime_client, reset_client_cache

    reset_client_cache()
    a = bedrock_runtime_client("us-east-1")
    b = bedrock_runtime_client("us-east-1")
    assert a is b
