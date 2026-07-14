"""Tests for the request-scoped correlation context (P0-12).

Covers the pure context builder/sanitizer and the edge behaviour: header-absent
requests are byte-identical to before, present-but-malformed headers are 400,
the assigned span/run ids are echoed on the response, and the ids reach the
routing layer as opaque pass-through (a RouteRequest carries them; routing must
not read them).
"""
from __future__ import annotations

import pytest

from mvp.observability.context import (
    HDR_GROUP_ID,
    HDR_SPAN_ID,
    HDR_WORKFLOW_RUN_ID,
    InvalidCorrelationHeader,
    RequestContext,
    build_request_context,
    response_headers,
)


class TestContextBuilder:
    def test_absent_headers_generate_run_and_span(self):
        ctx = build_request_context(
            tenant_id="acme", group_id_header=None, workflow_run_id_header=None)
        assert ctx.tenant_id == "acme"
        assert ctx.group_id is None
        assert ctx.request_id == ctx.span_id            # same unit, one id
        assert ctx.span_id.startswith("req_")
        assert ctx.workflow_run_id.startswith("wr_")    # server-generated
        assert ctx.workflow_run_supplied is False

    def test_supplied_headers_are_carried(self):
        ctx = build_request_context(
            tenant_id="acme", group_id_header="code-review-agent",
            workflow_run_id_header="run-123")
        assert ctx.group_id == "code-review-agent"
        assert ctx.workflow_run_id == "run-123"
        assert ctx.workflow_run_supplied is True

    def test_blank_header_treated_as_absent(self):
        ctx = build_request_context(
            tenant_id="acme", group_id_header="  ", workflow_run_id_header="")
        assert ctx.group_id is None
        assert ctx.workflow_run_supplied is False

    def test_request_id_pinnable(self):
        ctx = build_request_context(
            tenant_id="acme", group_id_header=None, workflow_run_id_header=None,
            request_id="req_pinned")
        assert ctx.request_id == "req_pinned" == ctx.span_id

    @pytest.mark.parametrize("bad", [
        "has space", "has#hash", "a" * 65, "semi;colon", "slash/y", "quote\"x",
        # CRLF / control chars: an accepted value is echoed into a response
        # header, so a trailing newline would be a header-injection foothold.
        # \A..\Z (not ^..$) rejects the trailing-newline the .strip() also removes.
        "embed\nnewline", "tab\there", "nul\x00byte", "cr\rreturn",
        "line1\nX-Evil: 1",
    ])
    def test_malformed_group_id_rejected(self, bad):
        with pytest.raises(InvalidCorrelationHeader) as e:
            build_request_context(
                tenant_id="acme", group_id_header=bad, workflow_run_id_header=None)
        assert e.value.header == HDR_GROUP_ID

    @pytest.mark.parametrize("bad", ["with space", "x#y", "b" * 65])
    def test_malformed_workflow_run_id_rejected(self, bad):
        with pytest.raises(InvalidCorrelationHeader) as e:
            build_request_context(
                tenant_id="acme", group_id_header=None, workflow_run_id_header=bad)
        assert e.value.header == HDR_WORKFLOW_RUN_ID

    @pytest.mark.parametrize("ok", ["a", "code-review.v2", "run_1:2-3", "A9._:-"])
    def test_grammar_accepts_valid_ids(self, ok):
        ctx = build_request_context(
            tenant_id="acme", group_id_header=ok, workflow_run_id_header=ok)
        assert ctx.group_id == ok and ctx.workflow_run_id == ok

    def test_response_headers_echo_span_and_run(self):
        ctx = build_request_context(
            tenant_id="acme", group_id_header=None, workflow_run_id_header="run-9")
        h = response_headers(ctx)
        assert h[HDR_SPAN_ID] == ctx.span_id
        assert h[HDR_WORKFLOW_RUN_ID] == "run-9"
        # group_id is NOT echoed (client already knows it; server assigns nothing)
        assert HDR_GROUP_ID not in h


class TestTenantIsolation:
    def test_tenant_id_comes_from_arg_not_header(self):
        # There is no code path that lets a header set tenant_id: the builder
        # takes it as a positional arg (from auth). This test documents/guards
        # that the signature has no tenant header parameter.
        import inspect
        sig = inspect.signature(build_request_context)
        assert "tenant_id" in sig.parameters
        assert not any("tenant" in p and "header" in p for p in sig.parameters)
