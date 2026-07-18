"""Request-scoped correlation context (P0-12).

Carries the three observability identifiers through a request:

  * ``span_id``          — one gateway request = one LLM call = one money-path
                           reserve/settle lifecycle. **Server-minted, always**;
                           equal to ``request_id`` (they denote the same unit,
                           so we mint one id and expose it under both names).
                           Never trusted from the client.
  * ``workflow_run_id``  — one execution of a workflow/agent run (a DAG of LLM
                           calls that belong together). Client-supplied via
                           ``x-sc-workflow-run-id``; **server-generated** (a run
                           of one span) when the header is absent.
  * ``group_id``         — a stable *logical policy group* (e.g. a named agent),
                           the unit the future offline evaluator learns routing
                           policy for. Client-supplied via ``x-sc-group-id``;
                           ``None`` when absent.

Client contract is HTTP headers, NOT body fields: the request body is forwarded
to Bedrock, so stuffing correlation ids into it either fails upstream validation
or forces fragile field-stripping. Headers never reach the upstream payload.

Security: ``tenant_id`` always comes from the authenticated principal, never
from a header — so a client can at worst pollute its OWN tenant's id namespace
with garbage labels; it can never address another tenant's records. Client
values become DynamoDB key components later, so they are grammar-validated here
(and in particular may not contain ``#``, our key delimiter).

This module has NO I/O and NO DynamoDB dependency — it is pure request plumbing.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from typing import Optional

# Grammar for client-supplied correlation ids. Forbids '#' (our DynamoDB key
# delimiter) so a client can never inject into another item collection, bounds
# the length so a header can't bloat a key, and allows the usual id characters.
# Uses \A..\Z (not ^..$): Python's `$` also matches just before a TRAILING
# newline, so `^...$` would accept "abc\n" — a CRLF-injection foothold if the
# value is ever echoed into a response header. \Z anchors the true end of
# string, rejecting any trailing newline outright (defence in depth alongside
# the .strip() below).
_ID_GRAMMAR = re.compile(r"\A[A-Za-z0-9._:-]{1,64}\Z")

# Header names, following the existing ``x-sc-fault`` convention.
HDR_GROUP_ID = "x-sc-group-id"
HDR_WORKFLOW_RUN_ID = "x-sc-workflow-run-id"
HDR_SPAN_ID = "x-sc-span-id"  # response only (server echoes the assigned span)
# SAAR session id: the routing session an agentic client wants continuity for.
# Same grammar as the correlation ids (so it is a safe DynamoDB sk component and
# response-header value), and — like them — tenant_id is NEVER taken from it; the
# SAAR memory partition is keyed by the authenticated tenant, so a session id can
# only ever address the caller's OWN tenant's routing state. Absent ⇒ SAAR falls
# back to workflow_run_id, then group_id (see ``session_key``).
HDR_SESSION_ID = "x-sc-session-id"


class InvalidCorrelationHeader(ValueError):
    """A present correlation header violated the id grammar (maps to HTTP 400)."""

    def __init__(self, header: str, value: str):
        self.header = header
        super().__init__(
            f"invalid {header!r}: must match {_ID_GRAMMAR.pattern} "
            f"(got {value[:80]!r})"
        )


@dataclass(frozen=True)
class RequestContext:
    """Correlation ids for one gateway request. Built at the edge, threaded to
    the routing layer as opaque pass-through (routing must not read these)."""

    tenant_id: str            # from auth, never from a header
    request_id: str           # server-minted at the edge
    span_id: str              # == request_id (same unit, one id)
    workflow_run_id: str      # client header, else server-generated "wr_..."
    group_id: Optional[str]   # client header, else None
    workflow_run_supplied: bool  # True iff the client sent the run id
    received_at_ms: int
    # SAAR: the client-supplied routing session id (``x-sc-session-id``), or None
    # when absent. Opaque, grammar-validated. NOT a tenant selector — see the
    # module docstring and ``session_key``.
    session_id_supplied: Optional[str] = None

    def session_key(self) -> str:
        """The SAAR session key for this request: the explicit ``x-sc-session-id``
        if the client sent one, else the workflow_run_id (always present — server
        -generated when the client omitted it), else the group_id. A run without
        any of these still gets a stable key (the server-minted workflow_run_id),
        so every request maps to exactly one session — a fresh one per request in
        the degenerate no-header case, which correctly means "no continuity to
        preserve". The tenant partition is supplied separately from the auth
        principal, never from this value."""
        return self.session_id_supplied or self.workflow_run_id or (self.group_id or "")


def _validate(header: str, value: Optional[str]) -> Optional[str]:
    """Return the trimmed value if present and valid; None if absent.

    Raises InvalidCorrelationHeader when the header IS present with a non-empty
    but malformed value. Two cases are treated as absent (return None, no error):
      * the header is not sent at all;
      * the header is sent empty / whitespace-only.
    DECISION (not an accident): empty ≡ absent. A client that sends
    ``x-sc-group-id:`` with no value plainly means "no group", so erroring would
    be hostile — and an empty value can never become a DynamoDB key component or
    a response-header value, so there is no safety reason to reject it. Only a
    present, non-empty, out-of-grammar value (e.g. contains ``#`` or a space) is
    a client mistake worth a 400. Note ``.strip()`` also normalises surrounding
    whitespace on an otherwise-valid value.
    """
    if value is None:
        return None
    v = value.strip()
    if v == "":
        return None
    if not _ID_GRAMMAR.match(v):
        raise InvalidCorrelationHeader(header, v)
    return v


def build_request_context(
    *,
    tenant_id: str,
    group_id_header: Optional[str],
    workflow_run_id_header: Optional[str],
    session_id_header: Optional[str] = None,
    request_id: Optional[str] = None,
) -> RequestContext:
    """Assemble a RequestContext from the authenticated tenant + raw headers.

    ``request_id`` is minted here when not supplied by the caller (tests may
    pin it). ``span_id`` is always ``request_id``. A missing workflow-run header
    yields a fresh server-generated run id (a run of one span). The optional
    ``x-sc-session-id`` is validated by the same grammar (empty ≡ absent) and is
    the preferred SAAR session key; absence is the compatible default.
    """
    group_id = _validate(HDR_GROUP_ID, group_id_header)
    supplied_run = _validate(HDR_WORKFLOW_RUN_ID, workflow_run_id_header)
    session_id = _validate(HDR_SESSION_ID, session_id_header)

    rid = request_id or f"req_{uuid.uuid4().hex[:16]}"
    workflow_run_id = supplied_run or f"wr_{uuid.uuid4().hex[:16]}"

    return RequestContext(
        tenant_id=tenant_id,
        request_id=rid,
        span_id=rid,
        workflow_run_id=workflow_run_id,
        group_id=group_id,
        workflow_run_supplied=supplied_run is not None,
        received_at_ms=int(time.time() * 1000),
        session_id_supplied=session_id,
    )


def response_headers(ctx: RequestContext) -> dict[str, str]:
    """Correlation headers to echo on the response: the assigned span id and
    the (possibly server-generated) workflow-run id, so a client that did not
    pre-generate a run id can reuse it for later calls in the same run."""
    return {
        HDR_SPAN_ID: ctx.span_id,
        HDR_WORKFLOW_RUN_ID: ctx.workflow_run_id,
    }
