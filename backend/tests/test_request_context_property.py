"""Property-based tests for the P0-12 correlation-context sanitizer.

This is a SECURITY BOUNDARY test suite.  Client-supplied header strings become
(a) DynamoDB key components (delimiter '#') in a later phase and (b) values
echoed into HTTP response headers.  So the sanitizer's guarantees must hold
for ARBITRARY input — the properties below are stated over all strings that
Hypothesis can produce (full unicode, control chars, surrogates excluded by
Hypothesis itself, huge strings), not over hand-picked cases.

Complements the example-based unit tests: those pin the documented contract on
known cases; this suite states the contract as universally quantified
invariants and lets Hypothesis hunt for counterexamples.

Invariants:

    I1. Injection-safety: for ANY input, build_request_context either raises
        InvalidCorrelationHeader OR the resulting group_id / workflow_run_id
        contain no '#', no CR/LF, no control char, no whitespace, and match
        \\A[A-Za-z0-9._:-]{1,64}\\Z.
    I2. tenant_id in the result ALWAYS equals the tenant_id argument — a
        header can never influence the tenant.
    I3. span_id == request_id always; a fresh grammar-valid 'req_...' id is
        minted when request_id is None; server ids are themselves echo-safe.
    I4. Absent OR empty/whitespace-only header => None (group) or a
        server-generated 'wr_...' (run), NEVER an error (empty ≡ absent).
    I5. workflow_run_supplied is True IFF a non-empty valid run header was given.
    I6. Accepted values are canonical fixed points: output == input.strip(),
        and feeding an accepted output back yields the same value.
    I7. response_headers(ctx) values never contain CR or LF.
    Plus: the 64/65 length boundary, and rejection messages are truncated and
    repr-escaped (no raw newline leaks into logs).

No I/O, no DynamoDB, no network — the module under test is pure.
"""

import re

import pytest
from hypothesis import HealthCheck, assume, example, given, settings, strategies as st

from mvp.observability.context import (
    _ID_GRAMMAR,
    HDR_SPAN_ID,
    HDR_WORKFLOW_RUN_ID,
    InvalidCorrelationHeader,
    build_request_context,
    response_headers,
)

# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------

# Fully adversarial header values: default st.text() alphabet includes control
# characters, unicode whitespace, combining chars, etc.  We additionally allow
# very long strings and None (header absent).
ADVERSARIAL = st.one_of(
    st.none(),
    st.text(max_size=300),
    # Oversized strings, to hammer the length bound and any slicing bugs.
    st.text(min_size=65, max_size=5_000),
)

# Values guaranteed to be grammar-valid — forces non-vacuous coverage of the
# "accepted" branch of every conditional property.
VALID_IDS = st.from_regex(_ID_GRAMMAR)

# Whitespace-only values (ASCII and unicode): must be treated as absent.
WHITESPACE_ONLY = st.text(
    alphabet=st.sampled_from(list(" \t\r\n\x0b\x0c\u00a0\u2007\u2028\u2029\u3000")),
    min_size=0,
    max_size=40,
)

TENANTS = st.text(min_size=1, max_size=64)

DEFAULT_SETTINGS = settings(
    max_examples=300,
    suppress_health_check=[HealthCheck.too_slow],
)

_NO_CR_LF = re.compile(r"[\r\n]")


def _build(group, run, tenant="tenant-a", request_id=None):
    return build_request_context(
        tenant_id=tenant,
        group_id_header=group,
        workflow_run_id_header=run,
        request_id=request_id,
    )


# --------------------------------------------------------------------------
# I1 — injection safety over ALL inputs
# --------------------------------------------------------------------------

@DEFAULT_SETTINGS
@given(group=ADVERSARIAL, run=ADVERSARIAL)
@example(group="a#b", run=None)                      # DynamoDB key delimiter
@example(group=None, run="wr\r\nSet-Cookie: x=1")    # CRLF header injection
@example(group="abc\n", run=None)                    # trailing LF: the ^..$ trap
@example(group="abc\r", run=None)                    # trailing CR
@example(group="a b", run=None)                      # inner space
@example(group="\u00a0", run="\u00a0")               # NBSP: strips to empty
@example(group="a\u00a0b", run=None)                 # inner NBSP: must reject
@example(group="", run="")                           # empty ≡ absent
@example(group="   ", run="   ")                     # whitespace ≡ absent
@example(group="a" * 64, run="a" * 64)               # boundary: accept
@example(group="a" * 65, run="a" * 65)               # boundary: reject
@example(group="\x00", run="\x1f")                   # control chars
def test_i1_outputs_are_always_grammar_safe(group, run):
    """For ANY input: either a clean InvalidCorrelationHeader, or every
    client-derived id in the result is grammar-valid (hence '#'-free,
    CR/LF-free, control-free, whitespace-free, <=64 chars)."""
    try:
        ctx = _build(group, run)
    except InvalidCorrelationHeader:
        return  # clean rejection is an allowed outcome

    for value in (ctx.workflow_run_id, ctx.group_id):
        if value is None:
            continue
        assert _ID_GRAMMAR.match(value), f"grammar violation escaped: {value!r}"
        assert "#" not in value
        assert not _NO_CR_LF.search(value)
        assert not any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)
        assert not any(c.isspace() for c in value)
        assert 1 <= len(value) <= 64


@DEFAULT_SETTINGS
@given(group=ADVERSARIAL, run=ADVERSARIAL)
def test_i1_only_the_documented_exception_escapes(group, run):
    """The sanitizer is total: no exception other than
    InvalidCorrelationHeader for any string input."""
    try:
        _build(group, run)
    except InvalidCorrelationHeader:
        pass  # the one documented failure mode


# --------------------------------------------------------------------------
# I2 — tenant integrity: headers can never influence the tenant
# --------------------------------------------------------------------------

@DEFAULT_SETTINGS
@given(tenant=TENANTS, group=ADVERSARIAL, run=ADVERSARIAL)
@example(tenant="victim#tenant", group="attacker", run="attacker")
@example(tenant="  spaced  ", group=None, run=None)  # tenant is NOT stripped
def test_i2_tenant_id_is_verbatim_and_header_independent(tenant, group, run):
    try:
        ctx = _build(group, run, tenant=tenant)
    except InvalidCorrelationHeader:
        return
    # Strict identity of value: no strip, no normalisation, no header leakage.
    assert ctx.tenant_id == tenant


# --------------------------------------------------------------------------
# I3 — span == request, server-minted ids are themselves echo-safe
# --------------------------------------------------------------------------

@DEFAULT_SETTINGS
@given(group=ADVERSARIAL, run=ADVERSARIAL, rid=st.one_of(st.none(), VALID_IDS))
def test_i3_span_equals_request_and_minted_ids_are_valid(group, run, rid):
    try:
        ctx = _build(group, run, request_id=rid)
    except InvalidCorrelationHeader:
        return

    assert ctx.span_id == ctx.request_id  # same unit, one id

    if rid is None:
        assert ctx.request_id.startswith("req_")
        assert _ID_GRAMMAR.match(ctx.request_id), (
            "server-minted request id must itself be echo-safe"
        )
    else:
        assert ctx.request_id == rid

    # The run id (client OR server) must always be grammar-valid: it is echoed.
    assert _ID_GRAMMAR.match(ctx.workflow_run_id)


def test_i3_two_mints_differ():
    """Freshness sanity: two mints without pinned ids yield distinct
    request and run ids (uuid4-backed; collision would break correlation)."""
    a = _build(None, None)
    b = _build(None, None)
    assert a.request_id != b.request_id
    assert a.workflow_run_id != b.workflow_run_id


# --------------------------------------------------------------------------
# I4 — absent / empty / whitespace-only  =>  absent semantics, never an error
# --------------------------------------------------------------------------

@DEFAULT_SETTINGS
@given(group=st.one_of(st.none(), WHITESPACE_ONLY),
       run=st.one_of(st.none(), WHITESPACE_ONLY))
@example(group="", run="")
@example(group="   ", run="\t\r\n")
@example(group="\u00a0\u3000", run="\u2028")  # unicode spaces: .strip() eats them
def test_i4_empty_is_absent_never_an_error(group, run):
    ctx = _build(group, run)  # must NOT raise
    assert ctx.group_id is None
    assert ctx.workflow_run_id.startswith("wr_")
    assert _ID_GRAMMAR.match(ctx.workflow_run_id)
    assert ctx.workflow_run_supplied is False


# --------------------------------------------------------------------------
# I5 — workflow_run_supplied is a truthful flag
# --------------------------------------------------------------------------

@DEFAULT_SETTINGS
@given(run=ADVERSARIAL)
@example(run="wr_client_supplied")
@example(run="  padded-but-valid  ")
def test_i5_run_supplied_iff_nonempty_valid_header(run):
    try:
        ctx = _build(None, run)
    except InvalidCorrelationHeader:
        return  # rejected: no context, nothing to assert

    header_meaningful = run is not None and run.strip() != ""
    assert ctx.workflow_run_supplied is header_meaningful
    if header_meaningful:
        assert ctx.workflow_run_id == run.strip()
    else:
        assert ctx.workflow_run_id.startswith("wr_")


# --------------------------------------------------------------------------
# I6 — accepted values are canonical fixed points (idempotence)
# --------------------------------------------------------------------------

@DEFAULT_SETTINGS
@given(vid=VALID_IDS)
def test_i6_grammar_valid_inputs_pass_through_verbatim(vid):
    """Non-vacuous acceptance coverage: every grammar-valid string is accepted
    unchanged, in both header positions."""
    ctx = _build(vid, vid)
    assert ctx.group_id == vid
    assert ctx.workflow_run_id == vid
    assert ctx.workflow_run_supplied is True


@DEFAULT_SETTINGS
@given(raw=st.text(max_size=300))
def test_i6_accepted_outputs_are_fixed_points(raw):
    """If ANY raw string is accepted, its output equals raw.strip(), and
    re-submitting that output yields the identical value (stability: a client
    reusing the echoed run id gets the same run)."""
    try:
        ctx1 = _build(raw, raw)
    except InvalidCorrelationHeader:
        return
    assume(ctx1.group_id is not None)  # skip the empty≡absent branch

    assert ctx1.group_id == raw.strip()
    assert ctx1.workflow_run_id == raw.strip()

    # Round-trip: the accepted output is already canonical.
    ctx2 = _build(ctx1.group_id, ctx1.workflow_run_id)
    assert ctx2.group_id == ctx1.group_id
    assert ctx2.workflow_run_id == ctx1.workflow_run_id


# --------------------------------------------------------------------------
# I7 — response headers are CRLF-injection-proof, independently of I1
# --------------------------------------------------------------------------

@DEFAULT_SETTINGS
@given(group=ADVERSARIAL, run=ADVERSARIAL,
       rid=st.one_of(st.none(), VALID_IDS))
@example(group=None, run="evil\r\nX-Injected: 1", rid=None)
def test_i7_response_headers_never_contain_cr_or_lf(group, run, rid):
    """Checked on response_headers() itself, NOT via I1: if a future edit
    echoes a new field without routing it through _validate, this test still
    catches it even though I1 would keep passing."""
    try:
        ctx = _build(group, run, request_id=rid)
    except InvalidCorrelationHeader:
        return
    hdrs = response_headers(ctx)
    assert set(hdrs) == {HDR_SPAN_ID, HDR_WORKFLOW_RUN_ID}
    for name, value in hdrs.items():
        assert isinstance(value, str)
        assert not _NO_CR_LF.search(value), f"CR/LF in response header {name!r}"
        assert not _NO_CR_LF.search(name)


# --------------------------------------------------------------------------
# Length boundary + error-message hygiene
# --------------------------------------------------------------------------

def test_length_boundary_64_accepted_65_rejected():
    ok = "a" * 64
    ctx = _build(ok, ok)
    assert ctx.group_id == ok and ctx.workflow_run_id == ok

    too_long = "a" * 65
    with pytest.raises(InvalidCorrelationHeader):
        _build(too_long, None)
    with pytest.raises(InvalidCorrelationHeader):
        _build(None, too_long)


@DEFAULT_SETTINGS
@given(bad=st.text(min_size=1, max_size=5_000))
@example(bad="evil\r\ninjected: yes")
@example(bad="#" * 200)
@example(bad="x" * 5_000)
def test_rejection_message_is_truncated_and_escaped(bad):
    """A rejected value's exception message must be log-safe: bounded length
    (value truncated to 80 chars, repr-quoted) and no raw CR/LF — an attacker
    must not be able to inject log lines via a malformed header."""
    try:
        _build(bad, None)
    except InvalidCorrelationHeader as exc:
        msg = str(exc)
        assert "\n" not in msg and "\r" not in msg, "raw newline leaked into message"
        # repr of an 80-char slice is at most ~4*80 + quoting overhead;
        # bound the whole message generously but firmly.
        assert len(msg) < 600, f"unbounded error message ({len(msg)} chars)"
        assert exc.header  # header name carried for the 400 handler
    else:
        # Not every string is invalid (e.g. it may strip to empty or be valid);
        # only assert on the exception path.  Non-vacuity: the @example cases
        # above are guaranteed rejections.
        v = bad.strip()
        assert v == "" or _ID_GRAMMAR.match(v), (
            "input was neither rejected, empty, nor grammar-valid"
        )


# --------------------------------------------------------------------------
# Anti-vacuity sentinel: the accept path is really exercised
# --------------------------------------------------------------------------

def test_sentinel_known_valid_header_is_accepted():
    """Guards every 'if accepted then ...' property above against silent
    vacuity: at least this canonical valid id must take the accept path."""
    ctx = _build("agent.checkout:v2", "wr-0123456789abcdef")
    assert ctx.group_id == "agent.checkout:v2"
    assert ctx.workflow_run_id == "wr-0123456789abcdef"
    assert ctx.workflow_run_supplied is True
