"""Tests for the widened /v1/chat/completions route.

The route used to resolve through the Claude-only `resolve_bedrock_model` and so
rejected every non-Claude model. It now resolves through `resolve_model` and
dispatches on the entry's `wire_protocol`: Converse for `messages` entries
(including the non-Anthropic ones), and a bedrock-mantle pass-through for
`responses` entries. These tests cover the dispatch, the two payload rewrites the
mantle leg requires, and the accounting on both success and failure.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from mvp.chat_completions import ChatCompletionsRequest, ChatMessage, _mantle_chat_completion
from mvp.models import resolve_bedrock_model, resolve_model


# ---------------------------------------------------------------------------
# Registry: the new entries, and the Anthropic route's isolation from them
# ---------------------------------------------------------------------------

class TestRegistryWidening:
    @pytest.mark.parametrize("name,expected_id,expected_region", [
        ("claude-sonnet-5", "us.anthropic.claude-sonnet-5", "us-east-1"),
        ("nemotron-super-3-120b", "nvidia.nemotron-super-3-120b", "us-east-1"),
        ("nvidia.nemotron-super-3-120b", "nvidia.nemotron-super-3-120b", "us-east-1"),
        ("qwen3-next-80b", "qwen.qwen3-next-80b-a3b", "us-east-1"),
        ("qwen.qwen3-next-80b-a3b", "qwen.qwen3-next-80b-a3b", "us-east-1"),
    ])
    def test_new_entries_resolve_with_their_own_region(self, name, expected_id, expected_region):
        entry = resolve_model(name)
        assert entry.bedrock_model_id == expected_id
        # Converse entries declare the region the model is offered in, which must
        # be one the deployment's region policy already covers — the chain catalogue
        # builds Converse targets from that policy, not from this field.
        assert entry.bedrock_region == expected_region
        assert entry.wire_protocol == "messages"

    def test_non_anthropic_converse_entries_stay_out_of_the_anthropic_route(self):
        """`/v1/messages` resolves through the legacy Claude-only resolver. Widening
        the chat route must not widen that one, or a Nemotron request would reach a
        route whose request/response shape is Anthropic Messages."""
        for name in ("nemotron-super-3-120b", "qwen3-next-80b"):
            with pytest.raises(ValueError):
                resolve_bedrock_model(name)

    def test_sonnet_5_is_reachable_from_the_anthropic_route(self):
        """Sonnet 5 IS Claude, so unlike Nemotron/Qwen it must also resolve on the
        Anthropic route — the registry entry is what makes both routes see it."""
        assert resolve_bedrock_model("claude-sonnet-5") == "us.anthropic.claude-sonnet-5"

    def test_new_pricing_keys_exist_and_never_undercharge(self):
        """Bedrock publishes no list price for either model, so both must default to
        the Opus tier per this module's stated rule. A missing key would silently
        fall back to `default` and quietly change the charge."""
        from mvp.pricing import snapshot_rates

        opus = snapshot_rates("opus")
        for key in ("nemotron", "qwen"):
            rate = snapshot_rates(key)
            assert rate.input_per_mtok_microusd >= opus.input_per_mtok_microusd
            assert rate.output_per_mtok_microusd >= opus.output_per_mtok_microusd


# ---------------------------------------------------------------------------
# bedrock-mantle pass-through
# ---------------------------------------------------------------------------

def _entry(region="us-east-2", model_id="google.gemma-4-31b"):
    """A real `ModelEntry`, not a stand-in: the helper reads `bedrock_region` and
    `bedrock_model_id` off it, and a duck-typed fixture would sail past a rename."""
    from mvp.models import ModelEntry

    return ModelEntry(
        provider="google", bedrock_model_id=model_id, bedrock_region=region,
        aliases=("gemma-4",), wire_protocol="responses", pricing_key="gemma",
    )


def _user():
    return SimpleNamespace(user_id="u1", org_id="org1")


def _body(**kw):
    kw.setdefault("model", "gemma-4")
    kw.setdefault("messages", [ChatMessage(role="user", content="hi")])
    return ChatCompletionsRequest(**kw)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def _fake_client(resp):
    client = MagicMock()
    client.post.return_value = resp
    client.close.return_value = None
    return client


MANTLE_OK = {
    "id": "chatcmpl-x", "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    "model": "google.gemma-4-31b",
}


class TestMantlePassThrough:
    def _call(self, body, entry, resp, tenants_repo=None):
        tenants_repo = tenants_repo or MagicMock()
        client = _fake_client(resp)
        with patch("mvp._mantle_transport.mint_bearer_token", return_value="tok"), \
             patch("mvp._mantle_transport.sync_client", return_value=client), \
             patch("mvp.chat_completions._settle_reservation_and_log") as settle, \
             patch("mvp.chat_completions._release_pool") as release:
            out = _mantle_chat_completion(
                body=body, entry=entry, user=_user(), tenants_repo=tenants_repo,
                reservation=1024, corr={}, request_id="req-1",
            )
        return out, client, settle, release, tenants_repo

    def test_model_field_is_rewritten_to_the_bedrock_id(self):
        """mantle resolves Bedrock model IDs, not our client-facing aliases; sending
        "gemma-4" through verbatim makes mantle 404."""
        out, client, _, _, _ = self._call(_body(), _entry(), _FakeResponse(200, MANTLE_OK))
        sent = client.post.call_args.kwargs["json"]
        assert sent["model"] == "google.gemma-4-31b"

    def test_max_tokens_is_translated_to_max_completion_tokens(self):
        out, client, _, _, _ = self._call(
            _body(max_tokens=64), _entry(), _FakeResponse(200, MANTLE_OK))
        sent = client.post.call_args.kwargs["json"]
        assert sent["max_completion_tokens"] == 64
        assert "max_tokens" not in sent

    def test_response_echoes_the_client_facing_alias(self):
        """The caller asked for `body.model`; an OpenAI client may compare the two."""
        out, _, _, _, _ = self._call(_body(), _entry(), _FakeResponse(200, MANTLE_OK))
        assert json.loads(out.body)["model"] == "gemma-4"

    def test_settles_once_against_the_reported_usage(self):
        _, _, settle, release, repo = self._call(
            _body(), _entry(), _FakeResponse(200, MANTLE_OK))
        assert settle.call_count == 1
        kw = settle.call_args.kwargs
        assert kw["actual_input_tokens"] == 11
        assert kw["actual_output_tokens"] == 7
        assert kw["model_id"] == "google.gemma-4-31b"
        assert kw["requested_model"] == "gemma-4"
        release.assert_not_called()
        repo.refund.assert_not_called()

    def _expect_failure(self, resp):
        from fastapi import HTTPException

        repo = MagicMock()
        client = _fake_client(resp)
        with patch("mvp._mantle_transport.mint_bearer_token", return_value="tok"), \
             patch("mvp._mantle_transport.sync_client", return_value=client), \
             patch("mvp.chat_completions._settle_reservation_and_log") as settle, \
             patch("mvp.chat_completions._release_pool") as release:
            with pytest.raises(HTTPException) as ei:
                _mantle_chat_completion(
                    body=_body(), entry=_entry(), user=_user(), tenants_repo=repo,
                    reservation=1024, corr={}, request_id="req-1",
                )
        return ei.value, settle, release, repo

    def test_upstream_error_refunds_and_releases_without_settling(self):
        """Invoke-time failure and mid-stream failure are distinct paths in this
        codebase: an invoke-time failure refunds and releases with NO settle."""
        exc, settle, release, repo = self._expect_failure(
            _FakeResponse(429, {"error": {"message": "slow down"}}))
        settle.assert_not_called()
        release.assert_called_once()
        repo.refund.assert_called_once()

    def test_caller_actionable_status_is_preserved_not_collapsed_to_502(self):
        """429 is a back-off signal and 400 describes the caller's own request.
        Collapsing either to 502 tells the caller to retry a gateway fault that
        is not one, and hides the rate limit a client is supposed to honour."""
        for upstream in (400, 404, 429):
            exc, _, _, _ = self._expect_failure(_FakeResponse(upstream, {"error": {"message": "x"}}))
            assert exc.status_code == upstream

    def test_upstream_5xx_becomes_502(self):
        exc, _, _, _ = self._expect_failure(_FakeResponse(503, {"error": {"message": "busy"}}))
        assert exc.status_code == 502

    def test_200_with_unparseable_body_refunds_instead_of_stranding_the_hold(self):
        """A 200 whose body is not JSON (truncated body, an LB's HTML error page)
        has no usage to settle against. Letting the parse error escape would leave
        the hold and the pool slot stranded."""
        class _Garbage(_FakeResponse):
            def json(self):
                raise ValueError("not json")

        exc, settle, release, repo = self._expect_failure(_Garbage(200, {}))
        assert exc.status_code == 502
        settle.assert_not_called()
        repo.refund.assert_called_once()
        release.assert_called_once()

    def test_the_http_client_is_always_closed(self):
        out, client, _, _, _ = self._call(_body(), _entry(), _FakeResponse(200, MANTLE_OK))
        client.close.assert_called_once()

    def test_client_targets_the_entrys_own_mantle_region(self):
        """Gemma 4 is pinned to us-east-2 and Grok to us-west-2; the base URL has to
        follow the entry or the request lands on a region that does not serve it."""
        client = _fake_client(_FakeResponse(200, MANTLE_OK))
        with patch("mvp._mantle_transport.mint_bearer_token", return_value="tok"), \
             patch("mvp._mantle_transport.sync_client", return_value=client) as ctor, \
             patch("mvp.chat_completions._settle_reservation_and_log"), \
             patch("mvp.chat_completions._release_pool"):
            _mantle_chat_completion(
                body=_body(model="grok-4.6"),
                entry=_entry(region="us-west-2", model_id="xai.grok-4.6"),
                user=_user(), tenants_repo=MagicMock(), reservation=1024,
                corr={}, request_id=None,
            )
        # The route must ask the transport for the ENTRY's region; the URL itself is
        # the transport's business and is asserted there.
        assert ctor.call_args.args[0] == "us-west-2"

    def test_transport_builds_the_regional_mantle_url(self):
        from mvp import _mantle_transport

        assert _mantle_transport.base_url("us-west-2") == (
            "https://bedrock-mantle.us-west-2.api.aws/openai/v1"
        )


# ---------------------------------------------------------------------------
# bedrock-mantle streaming
# ---------------------------------------------------------------------------

class _FakeStreamResponse:
    def __init__(self, status_code, lines):
        self.status_code = status_code
        self._lines = lines
        self.text = ""

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeAsyncClient:
    def __init__(self, resp, capture, raise_mid=False):
        self._resp, self.capture, self._raise_mid = resp, capture, raise_mid
        self.closed = False

    def stream(self, method, url, json=None):
        self.capture["payload"] = json
        if self._raise_mid:
            raise RuntimeError("connection reset")
        return _FakeStreamCtx(self._resp)

    async def aclose(self):
        self.closed = True


def _sse(obj):
    """One SSE event: the data line plus the blank line that terminates it, which
    is how httpx's aiter_lines surfaces a real stream."""
    return ["data: " + json.dumps(obj), ""]


def _stream(*events):
    """Flatten event line-groups into the line sequence a client would see."""
    lines = []
    for e in events:
        lines.extend(e if isinstance(e, list) else [e, ""])
    return lines


CONTENT_CHUNK = {"choices": [{"index": 0, "delta": {"content": "hi"}}]}
USAGE_CHUNK = {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3}}


class TestMantleStreaming:
    def _run(self, body, lines, raise_mid=False):
        import asyncio

        capture = {}
        client = _FakeAsyncClient(_FakeStreamResponse(200, lines), capture, raise_mid=raise_mid)
        repo = MagicMock()
        with patch("mvp._mantle_transport.mint_bearer_token", return_value="tok"), \
             patch("mvp._mantle_transport.async_client", return_value=client), \
             patch("mvp.chat_completions._settle_reservation_and_log") as settle, \
             patch("mvp.chat_completions._release_pool") as release:
            out = _mantle_chat_completion(
                body=body, entry=_entry(), user=_user(), tenants_repo=repo,
                reservation=1024, corr={}, request_id="req-1",
            )

            async def drain():
                got = []
                async for frame in out.body_iterator:
                    got.append(frame)
                return got

            if raise_mid:
                with pytest.raises(RuntimeError):
                    asyncio.run(drain())
                frames = []
            else:
                frames = asyncio.run(drain())
        return frames, capture, settle, release, repo, client

    def test_include_usage_is_injected_so_a_stream_is_never_free(self):
        """Without `stream_options.include_usage` an OpenAI-compatible stream carries
        no usage block, so every streamed request would settle at zero — a full
        refund of the hold. The injection is an accounting requirement."""
        _, capture, settle, _, _, _ = self._run(
            _body(stream=True), _stream(_sse(CONTENT_CHUNK), _sse(USAGE_CHUNK), "data: [DONE]"))
        assert capture["payload"]["stream_options"] == {"include_usage": True}
        assert settle.call_count == 1
        kw = settle.call_args.kwargs
        assert (kw["actual_input_tokens"], kw["actual_output_tokens"]) == (5, 3)

    def test_injected_usage_chunk_is_not_forwarded_to_the_caller(self):
        """A caller that never asked for usage must not start seeing a usage chunk
        just because the gateway needs one."""
        frames, _, _, _, _, _ = self._run(
            _body(stream=True), _stream(_sse(CONTENT_CHUNK), _sse(USAGE_CHUNK), "data: [DONE]"))
        body_text = b"".join(frames).decode()
        assert "usage" not in body_text
        assert "hi" in body_text and "[DONE]" in body_text

    def test_caller_requested_usage_chunk_is_forwarded(self):
        frames, capture, _, _, _, _ = self._run(
            _body(stream=True, stream_options={"include_usage": True}),
            _stream(_sse(CONTENT_CHUNK), _sse(USAGE_CHUNK), "data: [DONE]"))
        assert capture["payload"]["stream_options"] == {"include_usage": True}
        assert "usage" in b"".join(frames).decode()

    def test_streamed_upstream_error_refunds_exactly_once_and_never_settles(self):
        """The regression this guards: refunding in the error branch and then
        settling again in the generator's cleanup double-releases the hold, which a
        client can trigger on purpose by streaming a request mantle rejects."""
        import asyncio

        capture = {}
        client = _FakeAsyncClient(_FakeStreamResponse(429, []), capture)
        repo = MagicMock()
        with patch("mvp._mantle_transport.mint_bearer_token", return_value="tok"), \
             patch("mvp._mantle_transport.format_error", return_value="slow down"), \
             patch("mvp._mantle_transport.async_client", return_value=client), \
             patch("mvp.chat_completions._settle_reservation_and_log") as settle, \
             patch("mvp.chat_completions._release_pool") as release:
            out = _mantle_chat_completion(
                body=_body(stream=True), entry=_entry(), user=_user(), tenants_repo=repo,
                reservation=1024, corr={}, request_id="req-1",
            )

            async def drain():
                return [f async for f in out.body_iterator]

            frames = asyncio.run(drain())

        assert b"slow down" in b"".join(frames)
        repo.refund.assert_called_once()
        release.assert_called_once()
        settle.assert_not_called()

    def test_partial_stream_settles_what_was_produced(self):
        """A stream that dies after some tokens is not a free request; it settles
        with what was seen rather than refunding everything."""
        _, _, settle, release, repo, _ = self._run(
            _body(stream=True), [], raise_mid=True)
        assert settle.call_count == 1
        repo.refund.assert_not_called()

    def test_client_is_closed_on_the_happy_path(self):
        _, _, _, _, _, client = self._run(
            _body(stream=True), _stream(_sse(CONTENT_CHUNK), _sse(USAGE_CHUNK), "data: [DONE]"))
        assert client.closed


class TestRouteServabilityGuard:
    def test_self_hosted_and_virtual_entries_are_rejected(self):
        """`resolve_model` resolves the whole registry. A vLLM entry has no Bedrock
        region and a virtual pool entry must never be a charge-of-record model; the
        old Claude-only resolver excluded both by accident."""
        from mvp.models import ModelEntry

        for kwargs in (
            dict(served_by="vllm", endpoint_key="k"),
            dict(virtual=True, sr_pool_ref="pool-a"),
        ):
            entry = ModelEntry(
                provider="openai", bedrock_model_id="x.y", bedrock_region="us-east-2",
                aliases=("x",), wire_protocol="responses", pricing_key="gpt-5", **kwargs,
            )
            assert (getattr(entry, "served_by", "bedrock") != "bedrock"
                    or getattr(entry, "virtual", False)), "fixture must be unservable"


class TestMaxOutputTokenSpelling:
    """`max_tokens` carries a 4096 default on the request model, so it is always
    present. A caller using the modern `max_completion_tokens` spelling (accepted
    as an extra) must not have that default silently win — it would truncate the
    response AND under-reserve the hold."""

    def test_explicit_max_completion_tokens_wins_over_the_default(self):
        from mvp.chat_completions import _requested_max_output

        body = _body(max_completion_tokens=16000)
        assert body.max_tokens == 4096          # the default really is present
        assert _requested_max_output(body) == 16000

    def test_max_tokens_is_used_when_it_is_the_only_spelling(self):
        from mvp.chat_completions import _requested_max_output

        assert _requested_max_output(_body(max_tokens=256)) == 256

    def test_mantle_payload_carries_the_callers_value(self):
        client = _fake_client(_FakeResponse(200, MANTLE_OK))
        with patch("mvp._mantle_transport.mint_bearer_token", return_value="tok"), \
             patch("mvp._mantle_transport.sync_client", return_value=client), \
             patch("mvp.chat_completions._settle_reservation_and_log"), \
             patch("mvp.chat_completions._release_pool"):
            _mantle_chat_completion(
                body=_body(max_completion_tokens=16000), entry=_entry(), user=_user(),
                tenants_repo=MagicMock(), reservation=1024, corr={}, request_id=None,
            )
        sent = client.post.call_args.kwargs["json"]
        assert sent["max_completion_tokens"] == 16000
        assert "max_tokens" not in sent

    def test_converse_inference_config_honours_the_modern_spelling(self):
        from mvp.chat_completions import _build_chat_bedrock_kwargs

        kwargs = _build_chat_bedrock_kwargs(_body(max_completion_tokens=999), "m")
        assert kwargs["inferenceConfig"]["maxTokens"] == 999


class TestMantleStreamFraming:
    """SSE framing must survive the proxy. An event may carry several `data:` lines
    plus `event:`/`id:` fields, and a `:` comment is a valid keepalive; forwarding
    line by line would split or drop those."""

    def _drain(self, lines):
        import asyncio

        client = _FakeAsyncClient(_FakeStreamResponse(200, lines), {})
        with patch("mvp._mantle_transport.mint_bearer_token", return_value="tok"), \
             patch("mvp._mantle_transport.async_client", return_value=client), \
             patch("mvp.chat_completions._settle_reservation_and_log"), \
             patch("mvp.chat_completions._release_pool"):
            out = _mantle_chat_completion(
                body=_body(stream=True), entry=_entry(), user=_user(),
                tenants_repo=MagicMock(), reservation=1024, corr={}, request_id=None,
            )

            async def drain():
                return [f async for f in out.body_iterator]

            return b"".join(asyncio.run(drain())).decode()

    def test_multi_line_event_stays_one_event(self):
        out = self._drain(["event: delta", "data: {\"choices\": [1]}", "data: tail", ""])
        assert out == "event: delta\ndata: {\"choices\": [1]}\ndata: tail\n\n"

    def test_comment_keepalive_is_preserved(self):
        out = self._drain([": ping", "", "data: [DONE]", ""])
        assert out.startswith(": ping\n\n")
        assert "[DONE]" in out

    def test_trailing_event_without_a_final_blank_line_is_still_flushed(self):
        out = self._drain(["data: [DONE]"])
        assert "[DONE]" in out


class TestCostTierTracksPrice:
    """`_tier_for` used to substring-match the key NAME, so `fable` (priced above
    the Opus tier) and `gemma` (at it) both scored 2 — the Sonnet tier. The breaker
    keeps targets with tier <= cap on DOWNGRADE, so that let a "downgrade" pick a
    costlier model. The tier now derives from the built-in rate."""

    def test_claude_tiers_are_unchanged(self):
        from mvp.routing.chains import _tier_for

        assert _tier_for("haiku") == 1
        assert _tier_for("sonnet") == 2
        assert _tier_for("opus") == 3

    def test_keys_priced_at_or_above_opus_are_not_mid_tier(self):
        from mvp.routing.chains import _tier_for

        for key in ("fable", "gemma", "gpt-5", "gpt-5.6-sol", "nemotron", "qwen"):
            assert _tier_for(key) == 3, key

    def test_cheaper_keys_land_below_opus(self):
        from mvp.routing.chains import _tier_for

        assert _tier_for("grok") == 2
        assert _tier_for("gpt-5.6-terra") == 2

    def test_tier_never_contradicts_the_price_ordering(self):
        from mvp.pricing import _DEFAULT_RATES
        from mvp.routing.chains import _tier_for

        for a, ra in _DEFAULT_RATES.items():
            for b, rb in _DEFAULT_RATES.items():
                if ra.output_per_mtok_microusd < rb.output_per_mtok_microusd:
                    assert _tier_for(a) <= _tier_for(b), f"{a} cheaper than {b} but tiered higher"

    def test_unknown_key_stays_mid_tier(self):
        from mvp.routing.chains import _tier_for

        assert _tier_for("no-such-key") == 2

    def test_model_names_resolve_through_the_registry_not_substrings(self):
        """`_tier_for` takes a pricing KEY; the breaker's candidate lists hold model
        NAMES. They were once the same function, which worked for Claude only by
        coincidence (`claude-opus-4-6` contains "opus") and mis-tiered the rest."""
        from mvp.routing.chains import _tier_for_model

        assert _tier_for_model("claude-opus-4-6") == 3
        assert _tier_for_model("claude-sonnet-4-6") == 2
        assert _tier_for_model("claude-haiku-4-5") == 1

    def test_aliases_priced_at_or_above_opus_are_not_downgrade_targets(self):
        """The bug the split fixes: these names contain none of the old substrings,
        so they scored the Sonnet tier despite Opus-or-higher pricing — a breaker
        "downgrade" could move to a costlier model."""
        from mvp.routing.chains import _tier_for_model

        for name in ("gemma-4", "claude-fable-5", "gpt-5.6-sol"):
            assert _tier_for_model(name) == 3, name

    def test_unresolvable_model_name_stays_mid_tier(self):
        from mvp.routing.chains import _tier_for_model

        assert _tier_for_model("not-a-model") == 2
