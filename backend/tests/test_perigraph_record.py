"""The perigraph record `/v1/chat/completions` returns (mvp.perigraph_record).

Three kinds of check. perigraph's own identity fixtures, vendored from perigraph f24674a, pin the digests: a collector
that agrees only with itself would pass every other test here. The record's shape is checked against the rules a
receiver applies (every part accounted for once, model parts present and unable to key a model identity). And both
transports are driven through the route, because a header set on the wrong Response object is lost silently, and
because the record must describe the body as sent rather than as validated.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp import perigraph_record as pg
from mvp.authz import _PERMS_CACHE
from mvp.chat_completions import ChatCompletionsRequest, _build_openai_chat_payload
from mvp.chat_completions import router as chat_router
from mvp.deps import get_current_user

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "perigraph_identity.json").read_text(encoding="utf-8"))
PREFIX = "perigraph."


def _parts_accounted(attrs: dict) -> dict[str, str]:
    """kind -> 'held' or the absence reason, failing on a kind that is both or neither."""
    vocab = json.loads(pg.VOCABULARIES_PATH.read_text(encoding="utf-8"))
    out = {}
    for kind in vocab["parts"]["values"]:
        held = f"{PREFIX}part.{kind}.sourcing" in attrs
        reason = attrs.get(f"{PREFIX}absent.{kind}.reason")
        assert held != (reason is not None), f"{kind} is held={held} and absent={reason}"
        out[kind] = "held" if held else reason
    return out


# ---------------------------------------------------------------------------
# The vendored vocabularies and fixtures
# ---------------------------------------------------------------------------

def test_the_vendored_vocabularies_are_the_ones_the_collector_implements():
    assert hashlib.sha256(pg.VOCABULARIES_PATH.read_bytes()).hexdigest() == pg.VOCABULARIES_SHA256
    assert pg.SPEC_VERSION == 3


def test_a_vocabularies_file_with_another_digest_is_not_read(tmp_path):
    """Imports a copy of the module beside an altered vocabularies file, so the real import path runs."""
    shutil.copy(Path(pg.__file__), tmp_path / "perigraph_record.py")
    (tmp_path / "perigraph_vocabularies.json").write_bytes(pg.VOCABULARIES_PATH.read_bytes() + b"\n")
    spec = importlib.util.spec_from_file_location("perigraph_record_altered", tmp_path / "perigraph_record.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["perigraph_record_altered"] = mod
    try:
        spec.loader.exec_module(mod)
        assert mod._VOCAB is None
        assert mod.record({"messages": [{"role": "system", "content": "x"}]}) is None
        assert mod.response_headers({"messages": []}, model_id="m", want_record=True) == {
            mod.HDR_STATUS: "collector_failed"}
    finally:
        sys.modules.pop("perigraph_record_altered", None)


@pytest.mark.parametrize("case", FIXTURES["cases"], ids=lambda c: c["name"])
def test_every_identity_fixture_gives_the_spec_digests(case):
    attrs = pg.record(case["body"])
    assert attrs[f"{PREFIX}status"] == "complete"
    assert attrs.get(f"{PREFIX}harness_identity") == case["identity"]
    assert attrs.get(f"{PREFIX}part.instruction.digest") == case["instruction_digest"]
    want = case.get("request_template")
    if isinstance(want, dict) and "digest" in want:
        assert attrs[f"{PREFIX}part.request_template.digest"] == want["digest"]
    elif isinstance(want, dict) and "reason" in want:
        assert attrs[f"{PREFIX}absent.request_template.reason"] == want["reason"]
    _parts_accounted(attrs)


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

def test_the_model_parts_name_the_invoked_model_and_key_nothing():
    attrs = pg.record({"messages": [{"role": "system", "content": "s"}]}, model_id="us.anthropic.claude-x")
    assert attrs[f"{PREFIX}model_reaches"] == ""
    assert attrs[f"{PREFIX}model.checkpoint.sourcing"] == "not_observable"
    assert attrs[f"{PREFIX}model.checkpoint.label"] == "us.anthropic.claude-x"
    for kind in ("precision", "runtime", "answer_mode"):
        assert attrs[f"{PREFIX}model_absent.{kind}.reason"] == "not_observable"
    assert f"{PREFIX}model_identity" not in attrs and f"{PREFIX}artifact_identity" not in attrs
    assert not any(k.startswith(f"{PREFIX}model.") and k.endswith(".digest") for k in attrs)


def test_an_arn_model_id_is_not_a_label():
    attrs = pg.record({"messages": []}, model_id="arn:aws:bedrock:us-east-1:111122223333:application-inference-profile/x")
    assert not any("111122223333" in str(v) for v in attrs.values())
    assert attrs[f"{PREFIX}model_absent.checkpoint.reason"] == "not_observable"


def test_without_a_model_id_every_model_part_is_absent():
    attrs = pg.record({"messages": []})
    for kind in ("checkpoint", "precision", "runtime", "answer_mode"):
        assert attrs[f"{PREFIX}model_absent.{kind}.reason"] == "not_observable"


def test_the_model_id_does_not_change_the_harness_identity():
    body = {"messages": [{"role": "system", "content": "s"}]}
    assert pg.record(body, model_id="a")[f"{PREFIX}harness_identity"] == \
        pg.record(body, model_id="b")[f"{PREFIX}harness_identity"]


def test_two_system_prompts_are_two_identities_and_sampler_settings_are_not():
    a = pg.record({"messages": [{"role": "system", "content": "Answer A or B."}], "temperature": 0.0})
    b = pg.record({"messages": [{"role": "system", "content": "Answer A or B. Think first."}], "temperature": 0.0})
    a2 = pg.record({"messages": [{"role": "system", "content": "Answer A or B."}], "temperature": 0.7})
    assert a[f"{PREFIX}harness_identity"] != b[f"{PREFIX}harness_identity"]
    assert a[f"{PREFIX}harness_identity"] == a2[f"{PREFIX}harness_identity"]
    assert a[f"{PREFIX}part.decoding.digest"] != a2[f"{PREFIX}part.decoding.digest"]


def test_response_format_is_readout_and_decoding():
    attrs = pg.record({"messages": [{"role": "system", "content": "s"}],
                       "response_format": {"type": "json_object"}, "seed": 7})
    assert attrs[f"{PREFIX}part.readout.label"] == "json_object"
    assert attrs[f"{PREFIX}part.decoding.label"] == "seed,response_format"
    assert attrs[f"{PREFIX}part.readout.boundary"] == attrs[f"{PREFIX}part.decoding.boundary"] == "parsed"


@pytest.mark.parametrize("kind", [{"k": "SECRET"}, "x" * 65, "\ud800", ""])
def test_a_readout_label_never_carries_request_content(kind):
    attrs = pg.record({"messages": [], "response_format": {"type": kind}})
    assert f"{PREFIX}part.readout.label" not in attrs
    assert f"{PREFIX}part.readout.digest" in attrs


def test_an_unreadable_part_names_the_field_and_type_and_never_the_content():
    attrs = pg.record({"messages": [{"role": "system", "content": [{"type": "text", "text": "SECRET"}]}]})
    detail = attrs[f"{PREFIX}absent.instruction.detail"]
    assert attrs[f"{PREFIX}absent.instruction.reason"] == "extraction_failed"
    assert "messages[0].content" in detail and "array" in detail and "SECRET" not in detail
    assert f"{PREFIX}harness_identity" not in attrs


def test_a_system_text_with_a_lone_surrogate_is_unreadable_not_a_collector_failure():
    attrs = pg.record({"messages": [{"role": "system", "content": "a\ud800"}]})
    assert attrs[f"{PREFIX}status"] == "complete"
    assert attrs[f"{PREFIX}absent.instruction.reason"] == "extraction_failed"


def test_a_null_function_is_named_as_such():
    attrs = pg.record({"messages": [], "tools": [{"function": None}]})
    assert attrs[f"{PREFIX}absent.tool_schemas.detail"] == "tools[0].function is null, not an object"


def test_a_body_that_is_not_an_object_is_a_record_not_an_error():
    attrs = pg.record([1, 2])
    assert attrs[f"{PREFIX}status"] == "complete"
    assert attrs[f"{PREFIX}absent.instruction.detail"] == "the body is an array, not an object"


def test_a_defect_in_the_collector_is_a_failed_record_not_a_failed_request(monkeypatch):
    def boom(_):
        raise KeyError("x")
    monkeypatch.setattr(pg, "_read", boom)
    attrs = pg.record({"messages": []}, model_id="m")
    assert attrs[f"{PREFIX}status"] == "collector_failed"
    assert f"{PREFIX}harness_identity" not in attrs
    assert attrs[f"{PREFIX}model.checkpoint.label"] == "m"
    assert set(_parts_accounted(attrs).values()) == {"extraction_failed"}


def test_a_defect_while_encoding_is_a_failed_status_not_a_failed_request(monkeypatch):
    monkeypatch.setattr(pg, "encode_record", lambda _: (_ for _ in ()).throw(ValueError("x")))
    assert pg.response_headers({"messages": []}, model_id="m", want_record=True) == {pg.HDR_STATUS: "collector_failed"}


def test_a_body_that_was_not_decoded_is_a_failed_record():
    assert pg.record(pg.BODY_NOT_DECODED)[f"{PREFIX}status"] == "collector_failed"


def test_the_record_header_round_trips_non_ascii_text():
    attrs = pg.record({"messages": [{"role": "system", "content": "日本語で答えて"}]}, model_id="モデル")
    value = pg.encode_record(attrs)
    assert value.isascii() and "=" not in value
    assert pg.decode_record(value) == attrs


def test_status_and_identity_are_sent_by_default_and_the_record_only_on_request():
    body = {"messages": [{"role": "system", "content": "s"}]}
    plain = pg.response_headers(body, model_id="m", want_record=False)
    full = pg.response_headers(body, model_id="m", want_record=True)
    assert set(plain) == {pg.HDR_STATUS, pg.HDR_HARNESS_IDENTITY}
    assert pg.decode_record(full[pg.HDR_RECORD])[f"{PREFIX}harness_identity"] == plain[pg.HDR_HARNESS_IDENTITY]


def test_no_identity_header_when_the_record_has_none():
    headers = pg.response_headers({"messages": [{"role": "user", "content": "q"}]}, model_id="m",
                                  want_record=False)
    assert headers == {pg.HDR_STATUS: "complete"}


# ---------------------------------------------------------------------------
# Forwarding
# ---------------------------------------------------------------------------

TEMPLATE = {"messages": [{"role": "system"}, {"role": "user", "fixed": ["Q: ", ""]}]}


def test_the_openai_transport_payload_does_not_carry_the_template():
    from mvp.models import ModelEntry
    entry = ModelEntry(provider="google", bedrock_model_id="google.gemma-4-31b", bedrock_region="us-east-2",
                       aliases=("gemma-4",), wire_protocol="responses", pricing_key="gemma")
    body = ChatCompletionsRequest(model="gemma-4", messages=[{"role": "system", "content": "s"},
                                                             {"role": "user", "content": "Q: 1"}],
                                  perigraph_template=TEMPLATE)
    payload, _ = _build_openai_chat_payload(body, entry, "org1", user_id="u1", asserted_task_tag=None)
    assert pg.TEMPLATE_FIELD not in payload


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

@dataclass
class _FakeUser:
    user_id: str = "user-11111111-1111-1111-1111-111111111111"
    org_id: str = "default-org"
    email: str = "test@example.com"
    roles: Optional[list] = None
    auth_kind: str = "jwt"
    key_scopes: Optional[list] = None

    def __post_init__(self):
        if self.roles is None:
            self.roles = ["user", "admin"]


@pytest.fixture
def api_client(dynamodb_mock, seed_active_tenant, monkeypatch):
    monkeypatch.setitem(_PERMS_CACHE, "user", (["messages:send", "usage:read-self"], time.time() + 3600))
    monkeypatch.setitem(_PERMS_CACHE, "admin", (["messages:send", "usage:read-self"], time.time() + 3600))
    app = FastAPI()
    app.include_router(chat_router)
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()
    with patch("mvp.chat_completions.deployment_client") as mock_bedrock:
        mock_bedrock.return_value.converse.return_value = {
            "output": {"message": {"content": [{"text": "A"}]}}, "stopReason": "end_turn",
            "usage": {"inputTokens": 3, "outputTokens": 1},
        }
        yield TestClient(app), mock_bedrock


CLAUDE = "us.anthropic.claude-opus-4-7"
WANT = {pg.HDR_WANT_RECORD: "record"}


def _expected_identity(body: dict) -> str:
    return pg.record(body)[f"{PREFIX}harness_identity"]


def _templated(model: str, system: str) -> dict:
    return {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": "Q: 1"}],
            "perigraph_template": TEMPLATE}


def test_the_converse_transport_sends_the_identity_over_the_text_it_forwards(api_client):
    client, bedrock = api_client
    body = _templated(CLAUDE, "Answer A.")
    resp = client.post("/v1/chat/completions", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.headers[pg.HDR_STATUS] == "complete"
    assert resp.headers[pg.HDR_HARNESS_IDENTITY] == _expected_identity(body)
    assert pg.HDR_RECORD not in resp.headers
    sent = bedrock.return_value.converse.call_args.kwargs
    assert sent["system"] == [{"text": "Answer A."}]
    assert sent["messages"] == [{"role": "user", "content": [{"text": "Q: 1"}]}]


def test_the_record_describes_the_body_as_sent_not_as_validated(api_client):
    """`temperature: 1` is an integer on the wire and a float after validation; the decoding digest must follow the
    wire value, as perigraph's fixtures do."""
    client, _ = api_client
    body = {"model": CLAUDE, "temperature": 1, "messages": [{"role": "system", "content": "s"},
                                                             {"role": "user", "content": "q"}]}
    resp = client.post("/v1/chat/completions", json=body, headers=WANT)
    attrs = pg.decode_record(resp.headers[pg.HDR_RECORD])
    raw = pg.record(body)
    coerced = pg.record({**body, "temperature": 1.0})
    assert raw[f"{PREFIX}part.decoding.digest"] != coerced[f"{PREFIX}part.decoding.digest"]
    assert attrs[f"{PREFIX}part.decoding.digest"] == raw[f"{PREFIX}part.decoding.digest"]


def test_the_record_carries_the_invoked_model_id(api_client):
    client, bedrock = api_client
    body = {"model": CLAUDE, "messages": [{"role": "system", "content": "Answer A."},
                                           {"role": "user", "content": "q"}]}
    resp = client.post("/v1/chat/completions", json=body, headers=WANT)
    attrs = pg.decode_record(resp.headers[pg.HDR_RECORD])
    assert attrs[f"{PREFIX}model.checkpoint.label"] == bedrock.return_value.converse.call_args.kwargs["modelId"]
    assert attrs[f"{PREFIX}harness_identity"] == resp.headers[pg.HDR_HARNESS_IDENTITY]


def test_the_label_follows_a_cascade_selection(api_client):
    client, bedrock = api_client
    with patch("mvp.chat_completions._selected_bedrock_model", return_value="us.anthropic.claude-haiku-4-5-20251001-v1:0"):
        resp = client.post("/v1/chat/completions", json={"model": CLAUDE, "messages": [{"role": "user", "content": "q"}]},
                           headers=WANT)
    assert resp.status_code == 200, resp.text
    sent = bedrock.return_value.converse.call_args.kwargs["modelId"]
    assert sent == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert pg.decode_record(resp.headers[pg.HDR_RECORD])[f"{PREFIX}model.checkpoint.label"] == sent


def test_a_provider_error_response_carries_no_perigraph_headers(api_client):
    client, bedrock = api_client
    bedrock.return_value.converse.side_effect = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "bad"}}, "Converse")
    resp = client.post("/v1/chat/completions", json=_templated(CLAUDE, "s"), headers=WANT)
    assert resp.status_code >= 400
    assert not any(h.startswith("x-sc-perigraph") for h in resp.headers)


def test_the_streamed_converse_transport_sends_the_identity(api_client):
    client, bedrock = api_client
    bedrock.return_value.converse_stream.return_value = {"stream": iter([
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": "A"}, "contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 3, "outputTokens": 1}}},
    ])}
    body = {"model": CLAUDE, "stream": True,
            "messages": [{"role": "system", "content": "Answer B."}, {"role": "user", "content": "q"}]}
    resp = client.post("/v1/chat/completions", json=body, headers=WANT)
    assert resp.status_code == 200
    assert resp.headers[pg.HDR_HARNESS_IDENTITY] == _expected_identity(body)
    assert pg.decode_record(resp.headers[pg.HDR_RECORD])[f"{PREFIX}status"] == "complete"


def test_the_openai_transport_sends_the_identity_and_drops_the_template(api_client):
    client, _ = api_client
    upstream = MagicMock()
    upstream.post.return_value = MagicMock(status_code=200, json=lambda: {
        "id": "x", "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "A"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
    body = _templated("gpt-5.6-sol", "Answer A.")
    with patch("mvp._openai_transport.mint_bearer_token", return_value="tok"), \
         patch("mvp._openai_transport.sync_client", return_value=upstream):
        resp = client.post("/v1/chat/completions", json=body, headers=WANT)
    assert resp.status_code == 200, resp.text
    assert resp.headers[pg.HDR_HARNESS_IDENTITY] == _expected_identity(body)
    attrs = pg.decode_record(resp.headers[pg.HDR_RECORD])
    assert attrs[f"{PREFIX}part.request_template.sourcing"] == "in_the_request"
    sent = upstream.post.call_args.kwargs["json"]
    assert pg.TEMPLATE_FIELD not in sent
    assert sent["messages"] == body["messages"]
    assert attrs[f"{PREFIX}model.checkpoint.label"] == sent["model"]


def test_a_direct_call_without_a_decoded_body_is_a_failed_record():
    from fastapi import Request, Response, params
    from mvp.chat_completions import _add_perigraph_headers
    corr: dict[str, str] = {}
    resp = Response()
    _add_perigraph_headers(corr, resp, Request({"type": "http", "headers": []}), params.Depends(None), "m")
    assert corr == {pg.HDR_STATUS: "collector_failed"} and resp.headers[pg.HDR_STATUS] == "collector_failed"
