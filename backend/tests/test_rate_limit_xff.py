"""Regression tests for the rate-limit client-key extractor.

History
-------
* **Sweep-1 (C-A)** replaced the original leftmost-trust extractor
  (which was trivially bypassable) with a right-side peel using
  ``parts[:-hops]`` + fallback to ``parts[-1]`` when the slice went
  empty. Shipped with default ``TRUSTED_HOPS=1``.
* **Sweep-2 (Y-1)** bumped the default to ``2`` on the incorrect
  assumption that the ECS-side XFF carried ``<viewer>, <cf-edge>,
  <alb>``. AWS ALB does not append its own IP to XFF — it appends
  the immediate upstream's IP. So in the CloudFront → ALB topology
  the ECS-side XFF has 2 entries, not 3. With ``hops=2`` the slice
  emptied and the fallback returned the CF edge IP as the bucket
  key, silently coalescing every viewer behind one edge into a
  single rate-limit bucket.
* **Sweep-3 (Z-2)** — the fix tested here. The extractor is now
  right-indexed (``parts[-hops-1]``), default hops is back to ``1``,
  and the fallback is ``parts[-1]`` only when the list is shorter
  than ``hops + 1``.

Production topology we test
---------------------------
``viewer → CloudFront → ALB → ECS``:

* **Clean request** — ECS sees ``X-Forwarded-For: <viewer>, <cf-edge>``
  (two entries). The correct bucket key is ``<viewer>``.
* **Forged request** — the viewer prepends ``<attacker>`` to XFF.
  ECS sees ``<attacker>, <viewer>, <cf-edge>`` (three entries).
  The bucket key must still be ``<viewer>`` — the attacker must not
  be able to choose their own bucket by rotating the forged entry.
"""
from __future__ import annotations

import pytest
from fastapi import Request

from types import SimpleNamespace
from typing import Mapping


def _request(headers: Mapping[str, str], client_host: str = "10.0.0.1"):
    return SimpleNamespace(
        headers={k.lower(): v for k, v in headers.items()},
        client=SimpleNamespace(host=client_host),
    )


def _reload_with(env_hops: str | None, monkeypatch):
    """Reload core.rate_limit with a specific ``RATE_LIMIT_TRUSTED_HOPS``
    value (or with the env var unset → default)."""
    if env_hops is None:
        monkeypatch.delenv("RATE_LIMIT_TRUSTED_HOPS", raising=False)
    else:
        monkeypatch.setenv("RATE_LIMIT_TRUSTED_HOPS", env_hops)
    from importlib import reload
    import core.rate_limit as rl
    reload(rl)
    return rl


class TestDefaultCloudFrontPlusAlbTopology:
    """``hops=1`` default. ECS sees <viewer>, <cf-edge>."""

    def test_no_xff_falls_back_to_peer(self, monkeypatch):
        rl = _reload_with(None, monkeypatch)
        assert rl._client_key(_request({}, client_host="10.0.0.42")) == "10.0.0.42"

    def test_clean_request_bucket_is_viewer(self, monkeypatch):
        rl = _reload_with(None, monkeypatch)
        key = rl._client_key(
            _request({"x-forwarded-for": "203.0.113.7, 130.176.12.45"})
        )
        assert key == "203.0.113.7"

    def test_forged_leftmost_does_not_leak_into_bucket(self, monkeypatch):
        """Viewer forges XFF. ECS sees <forged>, <viewer>, <cf-edge>.
        Bucket must still be <viewer>."""
        rl = _reload_with(None, monkeypatch)
        key = rl._client_key(
            _request({
                "x-forwarded-for": "8.8.8.8, 198.51.100.5, 130.176.12.45"
            })
        )
        assert key == "198.51.100.5"

    def test_50_forged_rotations_collapse_to_one_bucket(self, monkeypatch):
        rl = _reload_with(None, monkeypatch)
        seen: set[str] = set()
        for i in range(50):
            forged = f"8.8.8.{i}"
            seen.add(
                rl._client_key(
                    _request(
                        {"x-forwarded-for": f"{forged}, 198.51.100.5, 130.176.12.45"}
                    )
                )
            )
        assert seen == {"198.51.100.5"}


class TestShorterChainFallback:
    """If the list is shorter than ``hops + 1`` we fall back to the
    rightmost entry. That's not attacker-controlled (a real proxy
    wrote it); worst case a bucket gets coarser, never attacker-pickable.
    """

    def test_single_entry_xff_uses_that_entry(self, monkeypatch):
        rl = _reload_with(None, monkeypatch)
        # hops=1, list=[1.2.3.4]. Fallback → parts[-1] = 1.2.3.4.
        assert rl._client_key(_request({"x-forwarded-for": "1.2.3.4"})) == "1.2.3.4"

    def test_hops_larger_than_list_uses_rightmost(self, monkeypatch):
        rl = _reload_with("3", monkeypatch)
        # hops=3, list=[1.2.3.4, 5.6.7.8]. Fallback → parts[-1] = 5.6.7.8.
        assert (
            rl._client_key(_request({"x-forwarded-for": "1.2.3.4, 5.6.7.8"}))
            == "5.6.7.8"
        )


class TestOperatorOverrides:
    def test_zero_hops_trusts_no_xff(self, monkeypatch):
        rl = _reload_with("0", monkeypatch)
        key = rl._client_key(
            _request({"x-forwarded-for": "1.2.3.4"}, client_host="10.0.0.9")
        )
        assert key == "10.0.0.9"

    def test_two_hops_custom_waf_topology(self, monkeypatch):
        """custom WAF → CloudFront → ALB → ECS. ALB + WAF both write
        entries, so hops=2. ECS sees <forged>, <viewer>, <cf-edge>, <waf>."""
        rl = _reload_with("2", monkeypatch)
        key = rl._client_key(
            _request({
                "x-forwarded-for": "8.8.8.8, 198.51.100.5, 130.176.12.45, 203.0.113.99"
            })
        )
        assert key == "198.51.100.5"

    def test_malformed_hops_falls_back_to_one(self, monkeypatch):
        rl = _reload_with("not-a-number", monkeypatch)
        key = rl._client_key(
            _request({"x-forwarded-for": "203.0.113.7, 130.176.12.45"})
        )
        # Default=1 on malformed env.
        assert key == "203.0.113.7"

    def test_negative_hops_falls_back_to_one(self, monkeypatch):
        rl = _reload_with("-5", monkeypatch)
        assert (
            rl._client_key(
                _request({"x-forwarded-for": "203.0.113.7, 130.176.12.45"})
            )
            == "203.0.113.7"
        )


class TestWhitespaceAndEmptyEntries:
    def test_empty_and_whitespace_entries_are_dropped(self, monkeypatch):
        rl = _reload_with(None, monkeypatch)
        key = rl._client_key(
            _request({"x-forwarded-for": " , 198.51.100.5 , 130.176.12.45 , "})
        )
        # After stripping, parts = [198.51.100.5, 130.176.12.45];
        # hops=1 → parts[-2] = 198.51.100.5.
        assert key == "198.51.100.5"


class TestRequestKey:
    """`_request_key`: an authenticated request is bucketed by its verified user, so
    callers sharing an address (a corporate NAT, or the private entry where every
    request arrives from the load balancer) do not share one bucket; a header the
    caller presents never chooses the bucket."""

    @staticmethod
    def _req(headers, host="10.0.0.1", principal=None):
        r = _request(headers, client_host=host)
        r.state = SimpleNamespace(**({"rate_principal": principal} if principal else {}))
        return r

    def test_a_made_up_credential_on_an_unauthenticated_route_keys_on_the_address(self, monkeypatch):
        rl = _reload_with(None, monkeypatch)
        a = rl._request_key(self._req({"x-api-key": "sk-stratoclave-aaa"}))
        b = rl._request_key(self._req({"authorization": "Bearer zzz"}))
        assert a == b == "10.0.0.1"

    def test_a_verified_user_gets_one_bucket_across_addresses_and_credentials(self, monkeypatch):
        rl = _reload_with(None, monkeypatch)
        a = rl._request_key(self._req({"x-api-key": "k1"}, host="10.0.0.1", principal="u-1"))
        b = rl._request_key(self._req({"authorization": "Bearer t"}, host="10.0.0.2", principal="u-1"))
        assert a == b == "user:u-1"

    def test_on_the_private_entry_no_other_client_ip_header_is_trusted(self, monkeypatch):
        rl = _reload_with(None, monkeypatch)
        key = rl._request_key(self._req({"x-real-ip": "1.2.3.4", "forwarded": "for=1.2.3.4",
                                         "cloudfront-viewer-address": "1.2.3.4:443"}, host="10.0.0.9"))
        assert key == "10.0.0.9"


@pytest.mark.parametrize("is_async", [False, True])
def test_a_limited_route_is_bucketed_by_the_user_the_auth_dependency_verified(monkeypatch, is_async):
    """Through FastAPI, the real limiter decorator and the real get_current_user
    (with only the token check faked): the bucket is the verified user, for sync
    and async routes alike, and a made-up key on an unauthenticated route still
    lands on the address."""
    import asyncio

    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    import core.rate_limit as rl_mod
    import core.rate_limit_ddb as ddb
    from mvp import deps

    seen = []
    monkeypatch.setattr(ddb, "_check", lambda scope, key, limit, window: seen.append(key))
    monkeypatch.setattr(deps, "_resolve_current_user",
                        lambda authorization, x_api_key: SimpleNamespace(user_id="u-42"))
    lim = ddb.DynamoRateLimiter(client_key_func=rl_mod._request_key)
    app = FastAPI()

    if is_async:
        @app.post("/limited")
        @lim.limit("5/minute")
        async def limited(request: Request, user=Depends(deps.get_current_user)):
            return {"ok": True}
    else:
        @app.post("/limited")
        @lim.limit("5/minute")
        def limited(request: Request, user=Depends(deps.get_current_user)):
            return {"ok": True}

    @app.post("/open")
    @lim.limit("5/minute")
    def open_route(request: Request):
        return {"ok": True}

    client = TestClient(app)
    assert client.post("/limited", headers={"x-api-key": "anything"}).status_code == 200
    assert client.post("/open", headers={"x-api-key": "made-up"}).status_code == 200
    assert seen[0] == "user:u-42"
    assert not seen[1].startswith(("user:", "cred:"))
