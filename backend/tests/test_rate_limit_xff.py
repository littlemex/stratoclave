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
