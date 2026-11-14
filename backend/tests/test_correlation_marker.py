"""C13.6: every provider call carries the correlation marker, on every transport.

The marker names the tenant, the attempt's money hold and the gateway's own user
id (`provider_outcome.MARKER_KEYS`). Bedrock echoes it into model invocation log
records, which is what lets an operator attribute a logged call to a tenant and a
user without the gateway holding anything (`docs/design/request-bodies.md`).

How it travels, per transport:

| Transport | Where the marker goes |
|---|---|
| Bedrock Converse, streaming or not | `requestMetadata` |
| The OpenAI-compatible surface on `bedrock-runtime` | the request's `metadata` field |
| A self-hosted endpoint (vLLM) | HTTP headers `provider_outcome.MARKER_HEADERS` |

For the self-hosted row the responsibility splits: the gateway sends the headers
on every inference call; whether they are recorded is decided by whoever runs the endpoint
(a reverse proxy's access log is enough). The gateway stores nothing and reads
nothing back.

The streaming paths used to send no marker at all: they built their own payload
and the marker was attached only on the non-streaming paths. The marker is now
handed to every `invoke_stream` by `_budget_flow.run_stream`, from the hold, so a
streaming route cannot omit it; the AST check below fails if one does not use it.
"""
from __future__ import annotations

import ast
import asyncio
import json
import pathlib

import httpx
import pytest

from mvp import provider_outcome as po

BACKEND = pathlib.Path(__file__).resolve().parents[1]


# --- the marker itself -------------------------------------------------------

def test_the_marker_names_the_attempt_the_tenant_and_the_user():
    assert po.attempt_request_metadata("h-1", "t-1", "u-1") == {
        "sc_attempt_id": "h-1", "sc_tenant": "t-1", "sc_user": "u-1"}
    assert set(po.MARKER_KEYS) == set(po.MARKER_HEADERS)


def test_an_absent_part_is_left_out_rather_than_sent_empty():
    assert po.attempt_request_metadata(None, "t-1", "u-1") == {"sc_tenant": "t-1", "sc_user": "u-1"}
    assert po.attempt_request_metadata(None, None, None) == {}


@pytest.mark.parametrize("bad", ["a|b", "ユーザー", " u", "u ", "a\tb", "a\r\nInjected: 1", "x" * 257, ""])
def test_a_value_bedrock_would_refuse_is_left_out_and_the_rest_still_sent(bad):
    """Bedrock refuses the whole call when one `requestMetadata` value is outside
    its character set (checked on real Bedrock with "a|b" and non-ASCII). A tenant
    id is chosen by an admin, so this happens; the bad part is dropped, never
    altered, and the call goes out with the rest."""
    assert po.attempt_request_metadata("h-1", bad, "u-1") == {"sc_attempt_id": "h-1", "sc_user": "u-1"}


def test_a_dropped_value_is_logged_once_by_key_and_never_by_value(monkeypatch):
    seen = []
    monkeypatch.setattr(po, "_reported_drops", set())
    monkeypatch.setattr(po.logger, "warning", lambda event, **kw: seen.append((event, kw)))
    po.attempt_request_metadata("h-1", "team|secret", "u-1")
    po.attempt_request_metadata("h-2", "team|secret", "u-2")
    assert seen == [("correlation_marker_value_dropped", {"key": "sc_tenant", "reason": "characters"})]


def test_the_ids_the_gateway_mints_always_pass():
    from dynamo.tenants import TenantsRepository
    from mvp._pipeline import _fresh_idempotency_token

    assert po.marker_value_ok(_fresh_idempotency_token())
    from mvp.discovery.ledger import SYSTEM_TENANT_ID

    assert po.marker_value_ok(TenantsRepository.new_tenant_id())
    assert po.marker_value_ok(SYSTEM_TENANT_ID)
    assert po.marker_value_ok(po.ATTEMPT_ID_PLACEHOLDER)


@pytest.mark.parametrize("org, starts", [("default-org", True), ("team|a", False), (" x", False)])
def test_a_default_tenant_id_the_marker_cannot_carry_stops_the_gateway_starting(
        monkeypatch, org, starts):
    """Through `main.py`'s real lifespan, the one boot path."""
    import main
    import mvp.deps
    import mvp.price_sources as price_sources

    monkeypatch.setattr(mvp.deps, "DEFAULT_ORG_ID", org)
    monkeypatch.setattr(price_sources, "validate_configuration", lambda: "bundled")
    reached = []
    monkeypatch.setattr("bootstrap.seed_all", lambda: reached.append(True))

    async def once():
        async with main.lifespan(main.app):
            pass

    if starts:
        asyncio.run(once())
        assert reached, "startup did not reach the seed"
    else:
        with pytest.raises(RuntimeError, match="DEFAULT_ORG_ID"):
            asyncio.run(once())
        assert not reached, "the check must run before the seed"


def test_no_tenant_row_is_written_with_an_id_the_marker_cannot_carry(dynamodb_mock):
    from dynamo.tenants import ADMIN_OWNED, TenantsRepository

    repo = TenantsRepository()
    with pytest.raises(ValueError, match="correlation marker"):
        repo.create(name="a", team_lead_user_id=ADMIN_OWNED, created_by="t", tenant_id="team|a")
    with pytest.raises(ValueError, match="correlation marker"):
        repo.seed_default(tenant_id="ユーザー")
    assert repo.create(name="a", team_lead_user_id=ADMIN_OWNED, created_by="t")["tenant_id"].startswith("tenant-")


def test_no_tenant_creation_route_takes_an_id_from_the_caller():
    from pydantic import ValidationError

    from mvp.admin_tenants import CreateTenantRequest
    from mvp.team_lead import CreateTenantTeamLeadRequest

    admin = {"name": "a", "team_lead_user_id": "admin-owned"}
    lead = {"name": "a"}
    CreateTenantRequest.model_validate(admin)
    CreateTenantTeamLeadRequest.model_validate(lead)
    with pytest.raises(ValidationError):
        CreateTenantRequest.model_validate({**admin, "tenant_id": "team|a"})
    with pytest.raises(ValidationError):
        CreateTenantTeamLeadRequest.model_validate({**lead, "tenant_id": "team|a"})


@pytest.mark.parametrize("good", ["default-org", "u@x.com/1+2=3,#$:_-.", "team a", "x" * 256,
                                  "3f2b8c1e-0d4a-4e7b-9c1f-2a6b5d8e9f01"])
def test_ids_inside_the_bedrock_set_are_sent_unchanged(good):
    assert po.attempt_request_metadata(None, good, None) == {"sc_tenant": good}


def test_the_headers_carry_only_the_gateways_keys_and_only_header_safe_values():
    md = {"sc_attempt_id": "h-1", "sc_tenant": "t-1", "sc_user": "u-1", "other": "x"}
    assert po.marker_headers(md) == {
        "X-Stratoclave-Attempt-Id": "h-1", "X-Stratoclave-Tenant": "t-1", "X-Stratoclave-User": "u-1"}
    assert po.marker_headers(None) == {}


@pytest.mark.parametrize("value", ["a|b", "ユーザー", " u ", "a\r\nInjected: 1", "team a", "u@x.com"])
def test_the_headers_and_the_bedrock_marker_follow_one_value_rule(value):
    """A self-hosted endpoint sees exactly the values a Bedrock log record would."""
    sent_to_bedrock = "sc_user" in po.attempt_request_metadata(None, None, value)
    sent_as_header = "X-Stratoclave-User" in po.marker_headers({"sc_user": value})
    assert sent_to_bedrock == sent_as_header


# --- the OpenAI-compatible surface -------------------------------------------

def test_the_openai_metadata_carries_the_user_too():
    payload: dict = {}
    assert po.stamp_openai_metadata_marker(payload, "t-1", user_id="u-1", asserted_task_tag=None) is True
    assert payload["metadata"]["sc_user"] == "u-1"
    assert payload["metadata"]["sc_tenant"] == "t-1"


@pytest.mark.parametrize("caller_keys, sent", [
    (13, {"sc_attempt_id", "sc_tenant", "sc_user"}),
    (14, {"sc_attempt_id", "sc_tenant"}),
    (15, set()),
])
def test_under_the_openai_key_ceiling_the_user_key_gives_way_first(caller_keys, sent):
    """Bedrock accepts 16 `metadata` keys. When the full marker does not fit,
    `sc_user` is dropped first, so the attempt and tenant pair that fitted before
    `sc_user` existed still fits; the pair itself is all or nothing, and the
    request is sent rather than refused."""
    payload: dict = {"metadata": {f"k{i}": "v" for i in range(caller_keys)}}
    added = po.stamp_openai_metadata_marker(payload, "t-1", user_id="u-1", asserted_task_tag=None)
    assert set(payload["metadata"]) & set(po.MARKER_KEYS) == sent
    assert added is ("sc_attempt_id" in sent)
    assert len(payload["metadata"]) <= po.OPENAI_METADATA_MAX_KEYS


@pytest.mark.parametrize("caller_keys, sent", [
    (12, {"sc_attempt_id", "sc_tenant", "sc_user", "sc_task_tag"}),
    (13, {"sc_attempt_id", "sc_tenant", "sc_user"}),
    (14, {"sc_attempt_id", "sc_tenant"}),
    (15, set()),
])
def test_the_task_tag_gives_way_before_the_user(caller_keys, sent):
    payload: dict = {"metadata": {f"k{i}": "v" for i in range(caller_keys)}}
    added = po.stamp_openai_metadata_marker(payload, "t-1", user_id="u-1", asserted_task_tag="nightly-eval")
    assert set(payload["metadata"]) & set(po.MARKER_KEYS) == sent
    assert added is ("sc_attempt_id" in sent)
    assert len(payload["metadata"]) <= po.OPENAI_METADATA_MAX_KEYS


def test_the_marker_carries_the_asserted_task_tag_and_only_that():
    from types import SimpleNamespace

    from mvp import task_tag
    assert po.attempt_request_metadata("h-1", "t-1", "u-1", "nightly-eval")["sc_task_tag"] == "nightly-eval"
    asserted = SimpleNamespace(task_tag="nightly-eval", task_tag_source="asserted")
    absent = SimpleNamespace(task_tag=task_tag.SENTINEL, task_tag_source="absent")
    dropped = SimpleNamespace(task_tag=task_tag.SENTINEL, task_tag_source="dropped_grammar")
    assert task_tag.asserted(asserted) == "nightly-eval"
    assert task_tag.asserted(absent) is None
    assert task_tag.asserted(dropped) is None
    assert task_tag.asserted(None) is None


def test_every_tag_the_grammar_accepts_passes_the_marker_rule():
    """Every character the task tag grammar accepts, alone, between two plain
    letters, and repeated to the grammar's longest length, gives a value the
    marker can carry; so no asserted tag is ever left out of the marker."""
    from mvp import task_tag

    assert task_tag.MAX_LEN <= po._MARKER_VALUE_MAX
    checked = 0
    for c in map(chr, range(0x180)):
        for candidate in (c, f"a{c}a", "a" + c * (task_tag.MAX_LEN - 2) + "a"):
            tag, source = task_tag.resolve(candidate)
            if source is task_tag.Source.ASSERTED:
                checked += 1
                assert po.marker_value_ok(tag), repr(candidate)
    assert checked > 3 * 60, checked


# The calls that carry the tag towards the marker. The usage record and the
# decision log take `task_tag` too, with the sentinel for "none"; not these.
_MARKER_PATHS = {"_open_hold", "_stream_chat", "_stream_response", "_openai_chat_completion",
                 "_build_openai_chat_payload", "_build_openai_responses_payload",
                 "stamp_openai_metadata_marker"}


def test_every_marker_path_is_handed_the_asserted_tag():
    """Every call under mvp/ that carries the tag towards the marker (the hold,
    the payload builders and the helpers between) names `task_tag`, and what it
    passes is the asserted tag from the request context or a `task_tag`
    parameter threaded from one. `None` is allowed only where the caller is not
    a tenant's request."""
    allowed_none = {"mvp/discovery/ledger.py"}
    seen = 0
    for path in sorted((BACKEND / "mvp").rglob("*.py")):
        rel = path.relative_to(BACKEND).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and _callee_name(call) in _MARKER_PATHS | {"Hold"}):
                continue
            if rel == "mvp/discovery/ledger.py" and _callee_name(call) == "_open_hold":
                continue  # the probe's own factory sets asserted_task_tag=None
            kws = {k.arg: k.value for k in call.keywords}
            if None in kws and "asserted_task_tag" not in kws:
                continue  # a factory forwarding **kwargs; its own callers are checked
            assert "asserted_task_tag" in kws, (
                f"{rel}:{call.lineno} {_callee_name(call)} without asserted_task_tag")
            seen += 1
            v = kws["asserted_task_tag"]
            ok = (isinstance(v, ast.Name) and v.id == "asserted_task_tag") or (
                isinstance(v, ast.Call) and _callee_name(v) == "_asserted_task_tag") or (
                rel in allowed_none and isinstance(v, ast.Constant) and v.value is None)
            assert ok, f"{rel}:{call.lineno} passes asserted_task_tag={ast.unparse(v)}"
    assert seen, "no marker path found"


def test_a_caller_cannot_forge_the_user_key():
    payload = {"metadata": {"sc_user": "someone-else", " SC_USER ": "x"}}
    po.stamp_openai_metadata_marker(payload, "t-1", user_id="u-1", asserted_task_tag=None)
    assert payload["metadata"]["sc_user"] == "u-1"
    assert " SC_USER " not in payload["metadata"]


# --- streaming: the hand-off through run_stream ------------------------------

def test_run_stream_hands_the_holds_marker_to_invoke_stream():
    from mvp import _budget_flow

    seen = {}

    class _Hold:
        def provider_call_starting(self):
            pass

        def request_metadata(self):
            return {"sc_tenant": "t-1", "sc_user": "u-1"}

        def claim_unobserved(self, **kw):
            return None

        def claim_stream_interrupted(self, **kw):
            return None

        def claim_settle(self, acc):
            return None

    def invoke(*, body, model_id, request_metadata):
        seen["md"] = request_metadata
        raise RuntimeError("stop here")

    class _Adapter:
        def prologue(self):
            return []

        def error_event(self, message):
            return []

    async def drive():
        try:
            async for _ in _budget_flow.run_stream(body=None, model_id="m", hold=_Hold(),
                                                   invoke_stream=invoke, adapter=_Adapter()):
                pass
        except Exception:
            pass

    asyncio.run(drive())
    assert seen["md"] == {"sc_tenant": "t-1", "sc_user": "u-1"}


STREAMING_MODULES = {"mvp/anthropic.py", "mvp/chat_completions.py"}


def _callee_name(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")


def _functions(tree):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _innermost_function(tree, node):
    """The innermost function whose body contains `node`."""
    best = None
    for fn in _functions(tree):
        if any(n is node for n in ast.walk(fn)):
            if best is None or any(n is fn for n in ast.walk(best)):
                best = fn
    return best


def _invoke_stream_functions():
    """Every function passed as `invoke_stream=` to `run_stream` under mvp/,
    resolved inside the function that makes the call (where `_invoke` is
    defined), so a same-named helper elsewhere in the module cannot stand in."""
    for path in sorted((BACKEND / "mvp").rglob("*.py")):
        rel = path.relative_to(BACKEND).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and _callee_name(call) == "run_stream"):
                continue
            for kw in call.keywords:
                if kw.arg != "invoke_stream":
                    continue
                assert isinstance(kw.value, ast.Name), (
                    f"{rel}: invoke_stream= must be a named function this check can read")
                scope = _innermost_function(tree, call) or tree
                local = [n for n in _functions(scope) if n.name == kw.value.id and n is not scope]
                assert len(local) == 1, f"{rel}: cannot resolve invoke_stream={kw.value.id}"
                yield rel, local[0]


def _passes_on(fn, name):
    """Whether `name` is passed on: an argument of a call, a dict value, or the
    value of an assignment, not merely tested."""
    for n in ast.walk(fn):
        values = []
        if isinstance(n, ast.Call):
            values = n.args + [k.value for k in n.keywords]
        elif isinstance(n, ast.Dict):
            values = n.values
        elif isinstance(n, ast.Assign):
            values = [n.value]
        for v in values:
            if any(isinstance(x, ast.Name) and x.id == name for x in ast.walk(v)):
                return True
    return False


def test_every_streaming_route_attaches_the_marker_it_is_handed():
    found = list(_invoke_stream_functions())
    assert {rel for rel, _ in found} == STREAMING_MODULES, found
    for rel, fn in found:
        params = [a.arg for a in fn.args.kwonlyargs + fn.args.args]
        assert "request_metadata" in params, f"{rel}:{fn.name} does not take request_metadata"
        assert _passes_on(fn, "request_metadata"), f"{rel}:{fn.name} takes request_metadata and drops it"


def test_the_streaming_messages_route_sends_the_marker_to_converse_stream(monkeypatch, dynamodb_mock):
    """Through the real `/v1/messages` streaming code and the real router, against
    a fake Bedrock client."""
    from mvp import anthropic as anth
    from mvp.routing import infrarouter

    seen = {}

    class _Stream:
        def __iter__(self):
            return iter([{"messageStart": {"role": "assistant"}},
                         {"messageStop": {"stopReason": "end_turn"}},
                         {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1}}}])

    class _Bedrock:
        def converse_stream(self, **kwargs):
            seen["kwargs"] = kwargs
            return {"stream": _Stream()}

    monkeypatch.setattr(infrarouter, "bedrock_client", lambda region: _Bedrock())
    monkeypatch.setattr(anth, "_settle_reservation_and_log", lambda **kw: None)

    class _Repo:
        hold_id = "hold-MARK"

        def refund(self, **kwargs):
            pass

    class _User:
        user_id = "u-1"
        org_id = "default-org"

    body = anth.AnthropicMessagesRequest.model_validate({
        "model": "us.anthropic.claude-opus-4-7", "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16, "stream": True})

    async def drive():
        from mvp.observability.context import build_request_context

        ctx = build_request_context(tenant_id="default-org", group_id_header=None,
                                    workflow_run_id_header=None, task_tag_header="nightly-eval")
        async for _ in anth._stream_messages(body=body, model_id="us.anthropic.claude-opus-4-7",
                                             user=_User(), tenants_repo=_Repo(), reservation=100,
                                             ctx=ctx):
            pass

    asyncio.run(drive())
    md = seen["kwargs"].get("requestMetadata")
    assert md == {"sc_tenant": "default-org", "sc_user": "u-1", "sc_attempt_id": "hold-MARK",
                  "sc_task_tag": "nightly-eval"}, seen["kwargs"]


def test_the_streaming_chat_route_sends_the_marker_to_converse_stream(monkeypatch):
    """The twin for streaming `/v1/chat/completions`, which calls Converse
    directly rather than through the router."""
    from mvp import chat_completions as cc

    seen = {}

    class _Bedrock:
        def converse_stream(self, **kwargs):
            seen["kwargs"] = kwargs
            return {"stream": iter([{"messageStart": {"role": "assistant"}},
                                    {"messageStop": {"stopReason": "end_turn"}},
                                    {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1}}}])}

    monkeypatch.setattr(cc, "deployment_client", lambda *a, **k: _Bedrock())
    monkeypatch.setattr(cc, "_settle_reservation_and_log", lambda **kw: None)
    monkeypatch.setattr(cc, "_release_pool", lambda *a, **k: None, raising=False)

    class _Repo:
        hold_id = "hold-MARK"

        def refund(self, **kwargs):
            pass

    class _User:
        user_id = "u-1"
        org_id = "default-org"

    body = cc.ChatCompletionsRequest.model_validate({
        "model": "us.anthropic.claude-opus-4-7", "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16, "stream": True})
    kwargs = {"messages": [{"role": "user", "content": [{"text": "hi"}]}],
              "inferenceConfig": {"maxTokens": 16}}

    async def drive():
        async for _ in cc._stream_chat(body, "us.anthropic.claude-opus-4-7", _User(), _Repo(), 100,
                                       kwargs, entry=None, asserted_task_tag="nightly-eval"):
            pass

    asyncio.run(drive())
    md = seen["kwargs"].get("requestMetadata")
    assert md == {"sc_tenant": "default-org", "sc_user": "u-1", "sc_attempt_id": "hold-MARK",
                  "sc_task_tag": "nightly-eval"}, seen["kwargs"]


# --- every Bedrock call site under mvp/ -------------------------------------

# Call sites that do not attach the marker themselves, and why.
_PROVIDER_CALLS_EXEMPT = {
    # The router sends the payload the route built; `_invoke` in mvp/anthropic.py
    # put the marker in it (checked through the route above).
    ("mvp/routing/infrarouter.py", "_attempt_invoke"),
    # An admin's probe of a model's availability: not a tenant's call, no hold.
    ("mvp/discovery/probe.py", "_call_messages"),
}


def test_every_bedrock_call_site_attaches_the_marker_or_is_named_here():
    found = set()
    for path in sorted((BACKEND / "mvp").rglob("*.py")):
        rel = path.relative_to(BACKEND).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and node.attr in {"converse", "converse_stream"}
                    and isinstance(node.ctx, ast.Load)):
                continue
            fn = _innermost_function(tree, node)
            name = fn.name if fn else "<module>"
            found.add((rel, name))
            if (rel, name) in _PROVIDER_CALLS_EXEMPT:
                continue
            mentions = {getattr(n, "id", None) for n in ast.walk(fn)} | {
                getattr(n, "value", None) for n in ast.walk(fn) if isinstance(n, ast.Constant)}
            assert mentions & {"request_metadata", "requestMetadata", "_attempt_md"}, (
                f"{rel}:{name} calls Bedrock without the correlation marker")
    assert _PROVIDER_CALLS_EXEMPT <= found, _PROVIDER_CALLS_EXEMPT - found


# --- self-hosted: the headers -------------------------------------------------

def test_a_self_hosted_call_carries_the_marker_as_headers(monkeypatch):
    from mvp.serving import vllm

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = request.content.decode()
        sse = (b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}],'
               b'"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    client = httpx.Client(base_url="http://selfhosted.test", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(vllm, "_client_for", lambda key: (client, "http://selfhosted.test"))

    class _Target:
        model_id = "local-model"
        endpoint_key = "local"
        served_by = "vllm"
        region = "self-hosted"

    monkeypatch.setenv("HYBRID_SERVING_ENABLED", "true")
    marker = {"sc_tenant": "tenant-MARK", "sc_attempt_id": "hold-MARK", "sc_user": "user-MARK",
              "sc_task_tag": "tag-MARK"}
    payload = {"messages": [{"role": "user", "content": [{"text": "hi"}]}],
               "inferenceConfig": {"maxTokens": 8}, "requestMetadata": marker}

    # Through the router's dispatch, the one way a self-hosted target is reached.
    from mvp.routing import infrarouter

    async def drive():
        out = await infrarouter._attempt_invoke(_Target(), payload)
        for _ in out["stream"]:
            pass

    asyncio.run(drive())
    headers = {k.lower(): v for k, v in seen["headers"].items()}
    assert headers["x-stratoclave-tenant"] == "tenant-MARK"
    assert headers["x-stratoclave-attempt-id"] == "hold-MARK"
    assert headers["x-stratoclave-user"] == "user-MARK"
    assert headers["x-stratoclave-task-tag"] == "tag-MARK"
    body = json.loads(seen["body"])
    assert not set(po.MARKER_KEYS) & set(body) and "requestMetadata" not in body
    assert not any(v in seen["body"] for v in marker.values()), (
        "the marker must not reach the self-hosted model's input")


# Self-hosted transports with no request site of their own yet, and why.
_SERVING_WITHOUT_REQUESTS = {
    # Not wired to a real endpoint: the transport is a hook a test installs.
    # When it gets a request site, this check covers it (C13.6).
    "mvp/serving/semantic_router.py",
}
_SERVING_MODULES = sorted(
    p.relative_to(BACKEND).as_posix() for p in (BACKEND / "mvp" / "serving").glob("*.py")
    if p.name != "__init__.py")


@pytest.mark.parametrize("module", _SERVING_MODULES)
def test_every_self_hosted_request_carries_the_marker_headers(module):
    """Every request site under mvp/serving/ passes `marker_headers`, and the
    header names are spelled once, in `provider_outcome.MARKER_HEADERS`."""
    text = (BACKEND / module).read_text(encoding="utf-8")
    assert "X-Stratoclave-" not in text
    tree = ast.parse(text)
    requests = [c for c in ast.walk(tree) if isinstance(c, ast.Call)
                and getattr(c.func, "attr", "") in {"stream", "post", "request", "send"}]
    if module in _SERVING_WITHOUT_REQUESTS:
        assert not requests, f"{module} now sends requests; remove it from the exemption"
        return
    assert requests, "no request site found"
    for call in requests:
        headers = next((kw.value for kw in call.keywords if kw.arg == "headers"), None)
        carries = headers is not None and any(
            isinstance(n, ast.Call) and _callee_name(n) == "marker_headers" for n in ast.walk(headers))
        assert carries, f"{module}:{call.lineno} sends a request without the marker headers"
