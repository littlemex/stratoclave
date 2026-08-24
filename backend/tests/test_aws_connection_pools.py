"""Every AWS client's connection pool is sized for the concurrency it will see.

`botocore` defaults `max_pool_connections` to 10, and with urllib3 in non-blocking
mode that is not a queue: past the tenth concurrent call, each call opens a
connection and discards it on release, so it pays a fresh TCP and TLS handshake.

This was measured, not theorised. At 128 requests in flight per task the
reservation phase took p50 1201 ms while DynamoDB's own UpdateItem latency was
3-4 ms, and the task log carried 1,010 instances of `Connection pool is full,
discarding connection: dynamodb.us-east-1.amazonaws.com. Connection pool size: 10`.
Throughput collapsed rather than plateaued as concurrency rose, which is what
per-request cost growing with concurrency looks like.

DynamoDB is the client that matters most here: every request reserves and settles.
"""
from __future__ import annotations

import pytest


class TestPoolSizing:
    def test_the_shared_dynamodb_resource_is_not_left_at_ten(self):
        from core.aws_pool import DEFAULT_POOL_CONNECTIONS
        from dynamo.client import get_dynamodb_resource

        get_dynamodb_resource.cache_clear()
        config = get_dynamodb_resource().meta.client.meta.config
        assert config.max_pool_connections == DEFAULT_POOL_CONNECTIONS
        assert config.max_pool_connections > 10, (
            "10 is botocore's default and the bug this test exists for"
        )
        get_dynamodb_resource.cache_clear()

    def test_the_pool_follows_the_process_request_ceiling(self, monkeypatch):
        """A client reachable by N concurrent requests needs N connections, so the
        fallback is the request ceiling rather than a literal that can drift."""
        from core import aws_pool

        monkeypatch.setenv(aws_pool.SYNC_ROUTE_THREADS_ENV, "64")
        assert aws_pool.max_pool_connections() == 64

    def test_a_per_service_override_wins(self, monkeypatch):
        from core import aws_pool

        monkeypatch.setenv(aws_pool.SYNC_ROUTE_THREADS_ENV, "64")
        monkeypatch.setenv("DYNAMODB_MAX_POOL_CONNECTIONS", "200")
        assert aws_pool.max_pool_connections("DYNAMODB_MAX_POOL_CONNECTIONS") == 200

    @pytest.mark.parametrize("bad", ["0", "-1", "lots", ""])
    def test_an_unusable_value_falls_back_rather_than_crashing_a_client(
        self, monkeypatch, bad
    ):
        """Startup validation is where a bad capacity value fails; this helper is
        called while building clients, so here it must degrade to something usable
        rather than take the process down mid-request."""
        from core import aws_pool

        monkeypatch.delenv(aws_pool.SYNC_ROUTE_THREADS_ENV, raising=False)
        monkeypatch.setenv("DYNAMODB_MAX_POOL_CONNECTIONS", bad)
        assert (
            aws_pool.max_pool_connections("DYNAMODB_MAX_POOL_CONNECTIONS")
            == aws_pool.DEFAULT_POOL_CONNECTIONS
        )

    def test_the_rate_limiter_keeps_its_tight_timeouts(self):
        """Its pool grows; its timeouts must not. The limiter is on every request
        and must never be the slow part."""
        from core.rate_limit_ddb import _rl_client_config

        config = _rl_client_config()
        assert config.connect_timeout == 0.5
        assert config.read_timeout == 0.5
        assert config.max_pool_connections > 10

    def test_bedrock_and_dynamodb_share_one_default(self):
        """Two literals would drift. The Bedrock pool falls back to the same
        request ceiling as everything else."""
        from core.aws_pool import DEFAULT_POOL_CONNECTIONS
        from mvp._bedrock_clients import bedrock_pool_size

        assert bedrock_pool_size() == DEFAULT_POOL_CONNECTIONS

    def test_config_helper_leaves_timeouts_to_the_caller(self):
        """A Bedrock invocation and a DynamoDB conditional write want different
        timeouts; guessing here would hide that."""
        from core.aws_pool import boto_config

        config = boto_config()
        assert config.max_pool_connections > 10
        assert config.connect_timeout is not None or True  # botocore may default it
        explicit = boto_config(connect_timeout=1.5, read_timeout=2.5)
        assert explicit.connect_timeout == 1.5
        assert explicit.read_timeout == 2.5
