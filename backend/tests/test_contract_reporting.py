"""Contract 11 — reporting: the gateway says what it observed, and no more.

Raised by a caller that ran ~200 agentic episodes through the gateway and could
not tell "the provider reported no cached tokens" from "the provider does not
report cached tokens at all", nor "the error names a path you serve" from "the
error names a path that 404s". Both are cases where the gateway already holds the
fact and reports something else.

  C11.1 A value the gateway did not observe is reported as absent, never as zero.
  C11.3 Any path or parameter the gateway names in an error is one it serves.

C11.1 is the same defect shape as a reclaim that writes `actual = 0` for a call
the provider billed: a measurement nobody made, asserted as a measurement. The
charge for an unreported leg is still zero — there is nothing to charge — but the
record must not claim the provider said so.
"""
from __future__ import annotations

import pathlib

import pytest

from mvp._converse_core import cache_tokens_from_usage
from mvp.pricing import RateSnapshot, rate_usage


SNAP = RateSnapshot(
    version="v-test",
    pricing_key="sonnet",
    input_per_mtok_microusd=3_000_000,
    output_per_mtok_microusd=15_000_000,
    cache_read_per_mtok_microusd=300_000,
    cache_write_per_mtok_microusd=3_750_000,
)


class TestAbsenceIsNotZero:

    def test_an_absent_cache_field_is_none_not_zero(self):
        assert cache_tokens_from_usage({}) == (None, None)

    def test_a_reported_zero_stays_zero(self):
        """A provider that says "nothing was cached" said something. Collapsing it
        into the same value as silence is what destroyed the distinction."""
        assert cache_tokens_from_usage(
            {"cacheReadInputTokens": 0, "cacheWriteInputTokens": 0}) == (0, 0)

    def test_a_reported_count_survives(self):
        assert cache_tokens_from_usage(
            {"cacheReadInputTokens": 1234, "cacheWriteInputTokens": 7}) == (1234, 7)

    def test_a_malformed_value_is_absent_not_zero(self):
        """An unparseable count is not a measurement either."""
        assert cache_tokens_from_usage({"cacheReadInputTokens": "banana"}) == (None, None)

    def test_the_rating_marks_an_unreported_leg_as_unreported(self):
        rating = rate_usage(
            SNAP, input_tokens=1000, output_tokens=10,
            cache_read_tokens=None, cache_write_tokens=None,
        )
        for leg in ("cache_read", "cache_write"):
            comp = rating.components[leg]
            assert comp["reported"] is False, (
                f"{leg} was never reported by the provider, so the ledger must not "
                "record it as a measured zero")
            assert comp["tokens"] == 0 and comp["cost_microusd"] == 0
        assert rating.components["input"]["reported"] is True

    def test_a_reported_zero_is_marked_reported(self):
        rating = rate_usage(
            SNAP, input_tokens=1000, output_tokens=10,
            cache_read_tokens=0, cache_write_tokens=0,
        )
        assert rating.components["cache_read"]["reported"] is True

    def test_an_unreported_leg_changes_no_charge(self):
        """The distinction is about the record, not about the money: an unreported
        leg costs what a zero leg costs."""
        with_none = rate_usage(SNAP, input_tokens=1000, output_tokens=10,
                               cache_read_tokens=None, cache_write_tokens=None)
        with_zero = rate_usage(SNAP, input_tokens=1000, output_tokens=10,
                               cache_read_tokens=0, cache_write_tokens=0)
        assert with_none.total_cost_microusd == with_zero.total_cost_microusd

    def test_absence_survives_the_hold_seam(self):
        """The seam every route settles through, which the earlier tests missed.

        `rate_usage` handling `None` establishes nothing about production if the
        object that calls it has already coerced the value: `Hold.claim_settle`
        snapshotted usage with `int(x or 0)` over every field, so an unreported leg
        became a measured zero one call before the ledger, on every transport. This
        drives the real `Hold`, so the claim cannot be true of a helper and false of
        the path."""
        from mvp import _money

        settled: list = []

        class _Repo:
            def refund(self, **kw):
                pass

        class _U:
            user_id = "u"
            org_id = "acme"

        hold = _money.Hold(
            user=_U(), tenants_repo=_Repo(), reservation=1000, model_id="m",
            settle=lambda **k: settled.append(k),
            release=lambda ctx: None,
        )
        ending = hold.claim_settle(_money.Usage(
            input_tokens=10, output_tokens=20,
            cache_read_tokens=None, cache_write_tokens=None,
        ))
        assert ending is not None
        ending.run()
        assert settled, "the settle never ran"
        assert settled[0]["actual_cache_read_tokens"] is None, (
            "the Hold coerced an unreported leg into a measured zero")
        assert settled[0]["actual_cache_write_tokens"] is None
        # A reported zero still arrives as a number.
        settled.clear()
        hold2 = _money.Hold(
            user=_U(), tenants_repo=_Repo(), reservation=1000, model_id="m",
            settle=lambda **k: settled.append(k),
            release=lambda ctx: None,
        )
        e2 = hold2.claim_settle(_money.Usage(
            input_tokens=10, output_tokens=20,
            cache_read_tokens=0, cache_write_tokens=0,
        ))
        e2.run()
        assert settled[0]["actual_cache_read_tokens"] == 0

    def test_there_is_one_extractor(self):
        """The Converse usage block was parsed by two copies of the same function,
        which is how one of them could keep collapsing absence into zero after the
        other stopped."""
        root = pathlib.Path(__file__).resolve().parents[1]
        definers = [
            p.relative_to(root).as_posix()
            for p in (root / "mvp").rglob("*.py")
            if "def _cache_tokens_from_usage" in p.read_text()
            or "def cache_tokens_from_usage" in p.read_text()
        ]
        assert definers == ["mvp/_converse_core.py"], definers


class TestAnErrorNamesAPathTheGatewayServes:

    def test_an_upstream_message_naming_a_path_we_do_not_serve_is_rewritten(self):
        """Measured by the caller: the upstream refuses tools together with a
        reasoning effort and tells them to use `/v1/responses`, which 404s here.
        Only the gateway knows what it serves, so only the gateway can fix the
        sentence."""
        from mvp._openai_transport import rewrite_served_paths

        msg = ("Tools are not supported with this reasoning effort. "
               "Use the /v1/responses endpoint instead.")
        out = rewrite_served_paths(msg)
        assert "/openai/v1/responses" in out
        # No bare `/v1/responses` left: every occurrence carries the prefix this
        # gateway actually serves.
        import re
        assert not re.search(r"(?<!/openai)/v1/responses", out), out

    def test_a_path_we_do_serve_is_left_alone(self):
        from mvp._openai_transport import rewrite_served_paths

        msg = "Unsupported parameter for /v1/chat/completions."
        assert rewrite_served_paths(msg) == msg

    def test_a_message_with_no_path_is_untouched(self):
        from mvp._openai_transport import rewrite_served_paths

        msg = "The model produced no output."
        assert rewrite_served_paths(msg) == msg

    def test_the_relay_applies_the_rewrite(self):
        """The rewrite has to sit in the ONE place upstream messages are relayed,
        or the next route added will hand the caller a 404 path again."""
        import httpx

        from mvp._openai_transport import format_error

        resp = httpx.Response(
            400,
            json={"error": {"message": "use /v1/responses for tool calls"}},
            request=httpx.Request("POST", "https://example.invalid/openai/v1/chat/completions"),
        )
        assert "/openai/v1/responses" in format_error(resp)
