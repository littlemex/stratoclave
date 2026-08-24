"""Pooling, token reuse and the per-task concurrency ceilings.

These pin the invariants behind the 2026-08-24 concurrency work. The measurement
that motivated it: a request through the gateway took p50 547 ms where the same
model called directly took 231 ms, and the gap stayed flat as concurrency rose, so
it was per-request setup rather than queueing. The setup was a fresh connection
pool and a freshly minted bearer on every request, and a task could hold only 40
chat requests at once regardless of CPU.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest


def _iso_in(*, seconds: int) -> str:
    """An ISO timestamp `seconds` from now, as botocore's metadata expects."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


# --------------------------------------------------------------------------- #
# Bedrock clients
# --------------------------------------------------------------------------- #


class TestBedrockClientPooling:
    def test_same_region_is_one_client(self):
        """One client per region means one connection pool, so the TLS handshake
        happens once rather than on every request."""
        from mvp._bedrock_clients import bedrock_runtime_client, reset_client_cache

        reset_client_cache()
        first = bedrock_runtime_client("us-east-1")
        assert bedrock_runtime_client("us-east-1") is first

    def test_regions_do_not_share_a_client(self):
        """A cache keyed too loosely would invoke a us-west-2 model against a
        us-east-1 endpoint — the exact confusion the registry's per-entry region
        exists to prevent."""
        from mvp._bedrock_clients import bedrock_runtime_client, reset_client_cache

        reset_client_cache()
        assert bedrock_runtime_client("us-east-1") is not bedrock_runtime_client("us-west-2")

    def test_explicit_config_is_not_pooled(self):
        """A caller overriding the timeouts wants its own client; caching it under
        the region key would hand those timeouts to every other caller."""
        from botocore.config import Config

        from mvp._bedrock_clients import bedrock_runtime_client, reset_client_cache

        reset_client_cache()
        pooled = bedrock_runtime_client("us-east-1")
        custom = bedrock_runtime_client("us-east-1", config=Config(read_timeout=5))
        assert custom is not pooled
        assert custom.meta.config.read_timeout == 5
        # ...and it must not have displaced the pooled one.
        assert bedrock_runtime_client("us-east-1") is pooled

    def test_cached_client_signs_with_rotated_credentials(self, monkeypatch):
        """Why pooling is safe under ECS credential rotation.

        This is the load-bearing assumption of the whole change, so it is tested
        against a real `RefreshableCredentials` rather than by inspecting the type:
        a pooled client is handed credentials that have already expired, and the
        values it would sign with must be the ones the refresh produced. If a
        future change froze credentials at construction instead, this fails here
        rather than in production six hours after a deploy.
        """
        import boto3
        from botocore.credentials import RefreshableCredentials

        from mvp import _bedrock_clients

        rotations = []

        def refresh():
            rotations.append(len(rotations))
            generation = len(rotations)
            return {
                "access_key": f"AK{generation}",
                "secret_key": f"SK{generation}",
                "token": f"TOK{generation}",
                # Already inside the refresh window, so the next read renews.
                "expiry_time": _iso_in(seconds=1),
            }

        credentials = RefreshableCredentials.create_from_metadata(
            metadata=refresh(), refresh_using=refresh, method="test-container-role"
        )
        session = boto3.Session(region_name="us-east-1")
        monkeypatch.setattr(session._session, "get_credentials", lambda: credentials)
        # The module builds clients from its own session, so that is what a test
        # has to stand in for.
        monkeypatch.setattr(_bedrock_clients, "_SESSION", session)

        _bedrock_clients.reset_client_cache()
        client = _bedrock_clients.bedrock_runtime_client("us-east-1")
        # The signer holds the credentials OBJECT, so the refresh reaches it.
        assert client._request_signer._credentials is credentials

        first = client._request_signer._credentials.get_frozen_credentials()
        second = client._request_signer._credentials.get_frozen_credentials()
        assert len(rotations) > 1, "expired credentials must be refreshed on read"
        assert second.access_key != first.access_key, (
            "a pooled client must sign with the refreshed key, not the one it was "
            "built with"
        )
        assert first.access_key != "AK1", "the first read should already have renewed"
        _bedrock_clients.reset_client_cache()

    def test_connection_pool_is_sized_for_the_task(self, monkeypatch):
        """botocore's default of 10 would put the handshake back on every request
        past the tenth, which defeats pooling under exactly the concurrency it
        exists for."""
        from mvp import _bedrock_clients

        from core.aws_pool import DEFAULT_POOL_CONNECTIONS

        _bedrock_clients.reset_client_cache()
        default = _bedrock_clients.bedrock_runtime_client("us-east-1")
        assert default.meta.config.max_pool_connections == DEFAULT_POOL_CONNECTIONS
        assert DEFAULT_POOL_CONNECTIONS > 10

        monkeypatch.setenv(_bedrock_clients.MAX_POOL_CONNECTIONS_ENV, "300")
        _bedrock_clients.reset_client_cache()
        assert _bedrock_clients.bedrock_runtime_client("us-east-1").meta.config.max_pool_connections == 300
        _bedrock_clients.reset_client_cache()

    @pytest.mark.parametrize("bad", ["0", "-3", "plenty"])
    def test_an_unusable_pool_size_is_rejected(self, monkeypatch, bad):
        from mvp._concurrency import CapacityConfigError
        from mvp import _bedrock_clients

        monkeypatch.setenv(_bedrock_clients.MAX_POOL_CONNECTIONS_ENV, bad)
        with pytest.raises(CapacityConfigError):
            _bedrock_clients.bedrock_pool_size()

    def test_pooled_timeouts_still_explicit(self):
        """Pooling must not have quietly dropped the timeout policy: a silent
        Bedrock socket would otherwise pin a worker thread indefinitely."""
        from mvp._bedrock_clients import bedrock_runtime_client, reset_client_cache

        reset_client_cache()
        config = bedrock_runtime_client("us-east-1").meta.config
        assert config.connect_timeout == 10
        assert config.read_timeout == 120


# --------------------------------------------------------------------------- #
# mantle transport
# --------------------------------------------------------------------------- #


class TestMantleTokenReuse:
    def test_token_is_minted_once_per_ttl(self, monkeypatch):
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        calls = []
        monkeypatch.setattr(
            _mantle_transport, "mint_bearer_token", lambda region: calls.append(region) or "tok"
        )

        first = _mantle_transport.auth_headers("us-east-2")
        second = _mantle_transport.auth_headers("us-east-2")
        assert first == second == {"Authorization": "Bearer tok"}
        assert calls == ["us-east-2"], "a cached bearer must not be re-minted per request"

    def test_expired_token_is_re_minted(self, monkeypatch):
        """The cache must not outlive the token: a bearer handed out past its TTL
        would fail upstream with a 401 the caller cannot act on."""
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        minted = iter(["first", "second"])
        monkeypatch.setattr(_mantle_transport, "mint_bearer_token", lambda region: next(minted))

        assert _mantle_transport.auth_headers("us-east-2")["Authorization"] == "Bearer first"
        # Expire it by making the clock jump past the TTL.
        base = _mantle_transport._now()
        monkeypatch.setattr(
            _mantle_transport,
            "_now",
            lambda: base + _mantle_transport.DEFAULT_TOKEN_TTL.total_seconds() + 1,
        )
        assert _mantle_transport.auth_headers("us-east-2")["Authorization"] == "Bearer second"

    def test_refresh_happens_before_expiry(self):
        """The margin exists so a request cannot be given a token that dies while
        it is still upstream."""
        from mvp import _mantle_transport

        assert _mantle_transport._TOKEN_REFRESH_MARGIN > timedelta(0)
        assert _mantle_transport._TOKEN_REFRESH_MARGIN < _mantle_transport.DEFAULT_TOKEN_TTL

    def test_expiry_is_re_checked_after_waiting_for_the_lock(self, monkeypatch):
        """A thread that waited behind another thread's mint must judge freshness
        from the clock NOW, not from the reading it took before waiting.

        The window is narrow but the failure is silent: the waiter hands back a
        token that has already expired and the request 401s upstream, which the
        caller can do nothing about. Reproduced deterministically by replacing the
        cache entry while a real thread is blocked on the region's lock.
        """
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        clock = {"now": 1600.0}
        monkeypatch.setattr(_mantle_transport, "_now", lambda: clock["now"])
        monkeypatch.setattr(_mantle_transport, "mint_bearer_token", lambda region: "fresh")

        # An entry that is already expired at 1600, so the waiter takes the lock.
        _mantle_transport._tokens["us-east-2"] = ("old", 1500.0)
        lock = _mantle_transport._token_lock("us-east-2")
        lock.acquire()

        result = {}
        waiter = threading.Thread(
            target=lambda: result.update(_mantle_transport.auth_headers("us-east-2"))
        )
        waiter.start()

        # While it waits: another thread mints (expiry 1900) and enough time passes
        # that the new entry is expired too by the time the waiter is let through.
        _mantle_transport._tokens["us-east-2"] = ("minted-while-waiting", 1900.0)
        clock["now"] = 2000.0
        lock.release()
        waiter.join(timeout=5)

        assert not waiter.is_alive()
        assert result == {"Authorization": "Bearer fresh"}, (
            "the waiter served a token that expired while it was blocked"
        )

    def test_regions_do_not_serialise_on_one_lock(self):
        """One global lock would make a slow mint in one region stall requests for
        every other region — including async streams, whose event loop waits on
        it."""
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        a = _mantle_transport._token_lock("us-east-2")
        b = _mantle_transport._token_lock("us-west-2")
        assert a is not b
        assert _mantle_transport._token_lock("us-east-2") is a
        assert isinstance(a, type(threading.Lock()))

    def test_tokens_are_per_region(self, monkeypatch):
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        monkeypatch.setattr(_mantle_transport, "mint_bearer_token", lambda region: f"tok-{region}")
        assert _mantle_transport.auth_headers("us-east-2")["Authorization"] == "Bearer tok-us-east-2"
        assert _mantle_transport.auth_headers("us-west-2")["Authorization"] == "Bearer tok-us-west-2"


class TestMantleTokenInvalidation:
    def test_an_upstream_rejection_drops_the_cached_bearer(self, monkeypatch):
        """Per-request minting self-healed after a rejection because the next
        request minted again. A cached token would be served to every request in
        the region until its TTL expired — and independently per task, so a dead
        credential takes the whole surface down rather than one request."""
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        minted = iter(["dead", "live"])
        monkeypatch.setattr(_mantle_transport, "mint_bearer_token", lambda region: next(minted))

        assert _mantle_transport.auth_headers("us-east-2")["Authorization"] == "Bearer dead"
        _mantle_transport.invalidate_token("us-east-2")
        assert _mantle_transport.auth_headers("us-east-2")["Authorization"] == "Bearer live"

    def test_a_stale_rejection_does_not_discard_a_fresh_token(self, monkeypatch):
        """Compare-and-pop. A burst of 401s carrying the OLD token would otherwise
        each discard whatever is cached — including a good token a refresh had just
        installed — and every discard costs another mint."""
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        monkeypatch.setattr(_mantle_transport, "mint_bearer_token", lambda region: "fresh")
        _mantle_transport.auth_headers("us-east-2")

        stale = {"Authorization": "Bearer dead"}
        _mantle_transport.invalidate_token("us-east-2", stale)
        assert _mantle_transport._tokens.get("us-east-2", ("",))[0] == "fresh"

        # The token that WAS rejected still goes.
        _mantle_transport.invalidate_token("us-east-2", {"Authorization": "Bearer fresh"})
        assert "us-east-2" not in _mantle_transport._tokens

    def test_invalidation_does_not_wait_on_the_region_lock(self, monkeypatch):
        """Called from the event loop on three of its four paths. The region lock
        is held for the whole of a mint, and a credential service returning 401 is
        exactly the one whose mint is slow, so blocking here would freeze the loop
        during the failure this exists to handle."""
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        monkeypatch.setattr(_mantle_transport, "mint_bearer_token", lambda region: "tok")
        _mantle_transport.auth_headers("us-east-2")

        lock = _mantle_transport._token_lock("us-east-2")
        lock.acquire()  # stand in for a mint in progress
        try:
            _mantle_transport.invalidate_token("us-east-2")
        finally:
            lock.release()
        assert "us-east-2" not in _mantle_transport._tokens

    def test_invalidating_one_region_leaves_the_others(self, monkeypatch):
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        counts = {"us-east-2": 0, "us-west-2": 0}

        def mint(region):
            counts[region] += 1
            return f"{region}-{counts[region]}"

        monkeypatch.setattr(_mantle_transport, "mint_bearer_token", mint)
        _mantle_transport.auth_headers("us-east-2")
        _mantle_transport.auth_headers("us-west-2")
        _mantle_transport.invalidate_token("us-east-2")
        _mantle_transport.auth_headers("us-east-2")
        _mantle_transport.auth_headers("us-west-2")
        assert counts == {"us-east-2": 2, "us-west-2": 1}


class TestMantleTokenRefreshWindow:
    def test_inside_the_margin_the_refresh_leaves_the_calling_thread(self, monkeypatch):
        """The caller serves the token it already has; the replacement is minted
        elsewhere.

        This is the assertion the previous version of this test only claimed: a
        refresh that ran inline would satisfy "the old token was returned" while
        still making the first caller inside the window pay for a mint — after its
        reservation was taken. So the mint's thread is what is pinned here.
        """
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        clock = {"now": 1000.0}
        monkeypatch.setattr(_mantle_transport, "_now", lambda: clock["now"])
        minted: list[str] = []
        threads: list[str] = []
        released = threading.Event()

        def mint(region):
            threads.append(threading.current_thread().name)
            minted.append(region)
            if len(minted) > 1:
                # Hold the refresh open so the caller demonstrably does not wait.
                released.wait(timeout=5)
            return f"tok{len(minted)}"

        monkeypatch.setattr(_mantle_transport, "mint_bearer_token", mint)

        caller = threading.current_thread().name
        assert _mantle_transport.auth_headers("us-east-2")["Authorization"] == "Bearer tok1"
        assert threads == [caller], "a cold mint is the caller's own work"

        ttl = _mantle_transport.DEFAULT_TOKEN_TTL.total_seconds()
        margin = _mantle_transport._TOKEN_REFRESH_MARGIN.total_seconds()
        clock["now"] += ttl - margin + 1

        served = _mantle_transport.auth_headers("us-east-2")["Authorization"]
        assert served == "Bearer tok1", "the caller must be served the cached token"
        released.set()
        _mantle_transport._drain_refresher()
        assert len(minted) == 2, "exactly one refresh should have replaced it"
        assert threads[1] != caller, "the refresh must not run on the calling thread"
        assert _mantle_transport._tokens["us-east-2"][0] == "tok2"

    def test_a_burst_inside_the_margin_schedules_one_refresh(self, monkeypatch):
        """Single-flight. One mint per window, not one per caller — the whole point
        of the window."""
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        clock = {"now": 1000.0}
        monkeypatch.setattr(_mantle_transport, "_now", lambda: clock["now"])
        minted: list[str] = []
        start = threading.Event()

        def mint(region):
            minted.append(region)
            if len(minted) > 1:
                start.wait(timeout=5)
            return f"tok{len(minted)}"

        monkeypatch.setattr(_mantle_transport, "mint_bearer_token", mint)
        _mantle_transport.auth_headers("us-east-2")

        ttl = _mantle_transport.DEFAULT_TOKEN_TTL.total_seconds()
        margin = _mantle_transport._TOKEN_REFRESH_MARGIN.total_seconds()
        clock["now"] += ttl - margin + 1
        for _ in range(20):
            assert _mantle_transport.auth_headers("us-east-2")["Authorization"] == "Bearer tok1"
        start.set()
        _mantle_transport._drain_refresher()
        assert len(minted) == 2, f"expected one refresh, got {len(minted) - 1}"

    def test_the_async_path_refreshes_inside_the_margin_too(self, monkeypatch):
        """Streams are the traffic that arrives in bulk, so the async caller is the
        one that most needs the window. An earlier version checked only that the
        token was still valid and skipped the refresh entirely, which left every
        in-flight stream to pile up at the hard expiry."""
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        clock = {"now": 1000.0}
        monkeypatch.setattr(_mantle_transport, "_now", lambda: clock["now"])
        minted: list[str] = []
        monkeypatch.setattr(
            _mantle_transport,
            "mint_bearer_token",
            lambda region: minted.append(region) or f"tok{len(minted)}",
        )
        _mantle_transport.auth_headers("us-east-2")

        ttl = _mantle_transport.DEFAULT_TOKEN_TTL.total_seconds()
        margin = _mantle_transport._TOKEN_REFRESH_MARGIN.total_seconds()
        clock["now"] += ttl - margin + 1

        served = asyncio.run(_mantle_transport.auth_headers_async("us-east-2"))
        assert served == {"Authorization": "Bearer tok1"}
        _mantle_transport._drain_refresher()
        assert len(minted) == 2, "the async path must schedule the refresh as well"

    def test_a_refresh_failure_keeps_serving_the_valid_token(self, monkeypatch):
        """The token is still good for the rest of the margin, so a failed mint
        must not turn into a failed request."""
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        clock = {"now": 1000.0}
        monkeypatch.setattr(_mantle_transport, "_now", lambda: clock["now"])
        monkeypatch.setattr(_mantle_transport, "mint_bearer_token", lambda region: "tok1")
        _mantle_transport.auth_headers("us-east-2")

        def boom(region):
            raise RuntimeError("mint is down")

        monkeypatch.setattr(_mantle_transport, "mint_bearer_token", boom)
        ttl = _mantle_transport.DEFAULT_TOKEN_TTL.total_seconds()
        margin = _mantle_transport._TOKEN_REFRESH_MARGIN.total_seconds()
        clock["now"] += ttl - margin + 1
        assert _mantle_transport.auth_headers("us-east-2")["Authorization"] == "Bearer tok1"

    def test_past_expiry_the_caller_does_wait(self, monkeypatch):
        """Serving an expired token would 401 upstream, so beyond the hard expiry
        the mint is no longer optional."""
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        clock = {"now": 1000.0}
        monkeypatch.setattr(_mantle_transport, "_now", lambda: clock["now"])
        minted = []
        monkeypatch.setattr(
            _mantle_transport,
            "mint_bearer_token",
            lambda region: minted.append(region) or f"tok{len(minted)}",
        )

        assert _mantle_transport.auth_headers("us-east-2")["Authorization"] == "Bearer tok1"
        clock["now"] += _mantle_transport.DEFAULT_TOKEN_TTL.total_seconds() + 1
        assert _mantle_transport.auth_headers("us-east-2")["Authorization"] == "Bearer tok2"


class TestMantleAsyncAuth:
    def test_a_cache_hit_answers_without_a_thread(self, monkeypatch):
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        monkeypatch.setattr(_mantle_transport, "mint_bearer_token", lambda region: "tok")
        _mantle_transport.auth_headers("us-east-2")  # warm

        def fail(*args, **kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("a cache hit must not pay for a thread hop")

        monkeypatch.setattr(asyncio, "to_thread", fail)
        got = asyncio.run(_mantle_transport.auth_headers_async("us-east-2"))
        assert got == {"Authorization": "Bearer tok"}

    def test_a_cold_mint_leaves_the_event_loop(self, monkeypatch):
        """Minting takes a lock and does signing work. On the loop it would stall
        every other in-flight request for the duration, and the loop would block on
        a `threading.Lock` held by whichever worker thread is minting."""
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        minted_on = {}

        def mint(region):
            minted_on["thread"] = threading.current_thread().name
            return "tok"

        monkeypatch.setattr(_mantle_transport, "mint_bearer_token", mint)

        async def run():
            main_thread = threading.current_thread().name
            headers = await _mantle_transport.auth_headers_async("us-east-2")
            return main_thread, headers

        main_thread, headers = asyncio.run(run())
        assert headers == {"Authorization": "Bearer tok"}
        assert minted_on["thread"] != main_thread


class TestMantleClientPooling:
    def test_sync_client_is_pooled_per_region(self):
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        first = _mantle_transport.sync_client("us-east-2")
        assert _mantle_transport.sync_client("us-east-2") is first
        assert _mantle_transport.sync_client("us-west-2") is not first

    def test_async_client_is_pooled_per_region(self):
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        first = _mantle_transport.async_client("us-east-2")
        assert _mantle_transport.async_client("us-east-2") is first
        assert _mantle_transport.async_client("us-west-2") is not first

    def test_pooled_client_carries_no_authorization(self):
        """Auth must travel with the request, not the client. Pinning it to the
        client would tie the pool's lifetime to the token's, which is what forced
        a new pool — and a new handshake — on every refresh."""
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        client = _mantle_transport.sync_client("us-east-2")
        assert "authorization" not in {k.lower() for k in client.headers}

    def test_there_is_no_per_call_override(self):
        """One name, one ownership rule. A factory that hands a pooled client to
        one caller and a caller-owned client to another is how a shared pool ends
        up closed by whoever thought they owned it."""
        import inspect

        from mvp import _mantle_transport

        for factory in (_mantle_transport.sync_client, _mantle_transport.async_client):
            params = list(inspect.signature(factory).parameters)
            assert params == ["region"], f"{factory.__name__} takes {params}"

    def test_connection_ceiling_is_configurable(self, monkeypatch):
        """The per-task in-flight limit for this surface. Left at httpx's default
        of 100 it would queue while the task still had CPU and threads free."""
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        monkeypatch.setenv(_mantle_transport._MAX_CONNECTIONS_ENV, "512")
        assert _mantle_transport._limits().max_connections == 512
        # Keepalive must match, or a connection closed between requests puts the
        # handshake back on the hot path.
        assert _mantle_transport._limits().max_keepalive_connections == 512

    @pytest.mark.parametrize("bad", ["0", "-1", "many"])
    def test_an_unusable_ceiling_is_rejected(self, monkeypatch, bad):
        """Same philosophy as the IaC side: a typo must not quietly shrink
        capacity. Startup reads every ceiling, so this surfaces as a deployment
        that does not come up rather than as a fleet that is silently throttled."""
        from mvp._concurrency import CapacityConfigError
        from mvp import _mantle_transport

        monkeypatch.setenv(_mantle_transport._MAX_CONNECTIONS_ENV, bad)
        with pytest.raises(CapacityConfigError):
            _mantle_transport.mantle_connection_ceiling()

    def test_an_unset_ceiling_uses_the_default(self, monkeypatch):
        from mvp import _mantle_transport

        monkeypatch.delenv(_mantle_transport._MAX_CONNECTIONS_ENV, raising=False)
        assert (
            _mantle_transport.mantle_connection_ceiling()
            == _mantle_transport._DEFAULT_MAX_CONNECTIONS
        )

    def test_idle_connections_outlive_a_gap_between_bursts(self):
        """httpx expires an idle pooled connection after 5 s by default, which
        throws the pool away between bursts. Measured: 400 requests in four bursts
        a few seconds apart produced 131 TLS handshakes — the cost pooling exists
        to remove, reappearing whenever traffic is not continuous."""
        from mvp import _mantle_transport

        limits = _mantle_transport._limits()
        assert limits.keepalive_expiry is not None
        assert limits.keepalive_expiry >= 60, (
            "a few seconds of idle must not discard the pool"
        )

    def test_pool_wait_is_bounded(self):
        """A pooled connection is acquired AFTER the budget reservation is taken,
        so an unbounded pool wait would hold a customer's balance on a queue that
        is our own saturation rather than the model's work."""
        from mvp import _mantle_transport

        pool_timeout = _mantle_transport._DEFAULT_TIMEOUT.pool
        assert pool_timeout is not None, "an unbounded pool wait holds a reservation"
        assert pool_timeout <= 30

    def test_the_nonstreaming_deadline_sits_below_the_cdn_timeout(self):
        """A request that outlives the CDN's patience reaches the caller as a
        CloudFront 504 with an HTML body — an unparseable failure for a problem that
        is neither the caller's nor the gateway's. Failing first means the caller
        gets the JSON 502 this surface returns everywhere else."""
        from mvp import _mantle_transport

        CDN_ORIGIN_TIMEOUT_SECONDS = 60
        timeout = _mantle_transport.nonstream_timeout()
        assert timeout.read < CDN_ORIGIN_TIMEOUT_SECONDS
        assert timeout.read > 30, "must still allow a slow-but-real completion"
        assert timeout.pool is not None and timeout.pool <= 30
        # The streaming window stays long: bytes flow, so the CDN times each read
        # rather than the whole stream.
        assert _mantle_transport._DEFAULT_TIMEOUT.read > CDN_ORIGIN_TIMEOUT_SECONDS

    def test_a_closed_client_is_rebuilt(self):
        """One stray `close()` must not poison the region for the life of the
        process. The old per-request construction made any such mistake cost a
        single request; pooling would make it cost every request until the task is
        replaced."""
        from mvp import _mantle_transport

        _mantle_transport.reset_transport_cache_for_tests()
        first = _mantle_transport.sync_client("us-east-2")
        first.close()
        second = _mantle_transport.sync_client("us-east-2")
        assert second is not first
        assert not second.is_closed


# --------------------------------------------------------------------------- #
# per-task concurrency ceilings
# --------------------------------------------------------------------------- #


class TestCapacityConfiguration:
    def test_defaults_are_set_deliberately_in_both_places(self):
        """The sync-route ceiling is set BELOW anyio's default of 40 on purpose.

        128 was tried and measured: p50 went from 390 ms at 4 requests per process
        to 7706 ms at 128, and throughput collapsed rather than plateaued. A process
        that admits more than it can turn around is queueing inside itself where no
        metric can see it, so the ceiling sits near the knee and the fleet grows by
        processes instead.
        """
        from anyio import to_thread

        from mvp._concurrency import (
            DEFAULT_OFFLOAD_THREADS,
            DEFAULT_SYNC_ROUTE_THREADS,
            configure_capacity,
        )

        async def run():
            applied = configure_capacity()
            limiter_total = to_thread.current_default_thread_limiter().total_tokens
            return applied, limiter_total

        applied, limiter_total = asyncio.run(run())
        assert applied.offload_threads == DEFAULT_OFFLOAD_THREADS
        assert applied.sync_route_threads == DEFAULT_SYNC_ROUTE_THREADS
        assert limiter_total == DEFAULT_SYNC_ROUTE_THREADS
        assert DEFAULT_SYNC_ROUTE_THREADS < 40, (
            "the measured knee is below anyio's default; admitting more per process "
            "traded latency for nothing"
        )

    def test_env_overrides_are_applied(self, monkeypatch):
        from anyio import to_thread

        from mvp._concurrency import (
            OFFLOAD_THREADS_ENV,
            SYNC_ROUTE_THREADS_ENV,
            configure_capacity,
        )

        monkeypatch.setenv(OFFLOAD_THREADS_ENV, "17")
        monkeypatch.setenv(SYNC_ROUTE_THREADS_ENV, "23")

        async def run():
            applied = configure_capacity()
            return applied, to_thread.current_default_thread_limiter().total_tokens

        applied, limiter_total = asyncio.run(run())
        assert applied.offload_threads == 17
        assert applied.sync_route_threads == 23
        assert limiter_total == 23

    @pytest.mark.parametrize("bad", ["0", "-4", "lots"])
    def test_an_unusable_value_fails_startup(self, monkeypatch, bad):
        """A typo in a deploy variable must not silently shrink capacity.

        Startup is where it is caught, matching the price source's precedent: a
        deployment that refuses to come up is easier to notice than one quietly
        serving a fraction of its intended load.
        """
        from mvp._concurrency import (
            CapacityConfigError,
            OFFLOAD_THREADS_ENV,
            configure_capacity,
        )

        monkeypatch.setenv(OFFLOAD_THREADS_ENV, bad)
        with pytest.raises(CapacityConfigError):
            asyncio.run(_apply(configure_capacity))

    def test_an_empty_value_is_treated_as_unset(self, monkeypatch):
        """An empty string is how a template renders an omitted variable, so it
        means "not configured" rather than "misconfigured"."""
        from mvp._concurrency import (
            DEFAULT_OFFLOAD_THREADS,
            OFFLOAD_THREADS_ENV,
            configure_capacity,
        )

        monkeypatch.setenv(OFFLOAD_THREADS_ENV, "")
        applied = asyncio.run(_apply(configure_capacity))
        assert applied.offload_threads == DEFAULT_OFFLOAD_THREADS


    def test_offload_executor_is_installed_at_the_configured_width(self, monkeypatch):
        """Not just "one task runs": the executor's width IS the ceiling on
        concurrent offloaded calls, so a regression to the old
        `min(32, cpu_count + 4)` has to fail here.

        `_default_executor` is private, and pinning it is the point: this is the
        object `asyncio.to_thread` dispatches to, and nothing public reports its
        size.
        """
        from concurrent.futures import ThreadPoolExecutor

        from mvp._concurrency import OFFLOAD_THREADS_ENV, configure_capacity

        monkeypatch.setenv(OFFLOAD_THREADS_ENV, "97")

        async def run():
            configure_capacity()
            loop = asyncio.get_running_loop()
            executor = loop._default_executor
            ran = await asyncio.to_thread(lambda: "ran")
            return executor, ran

        executor, ran = asyncio.run(run())
        assert ran == "ran"
        assert isinstance(executor, ThreadPoolExecutor)
        assert executor._max_workers == 97


async def _apply(fn):
    return fn()
