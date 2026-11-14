"""The classification and the liability policy, one test per branch.

`docs/design/charge-loss.md` acceptance criterion 4 asks for a test per state and,
specifically, for an unrecognised error to land in the expensive state. That last
one is the point of the whole module: the defect being fixed was a generic
`except Exception` that refunded, so a test that only covers the errors somebody
thought of would pass while the real hole stayed open.
"""
from __future__ import annotations

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ParamValidationError,
    ReadTimeoutError,
)

from mvp import provider_outcome as po


def _client_error(code: str, status: int = 400, op: str = "Converse") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "x"},
         "ResponseMetadata": {"HTTPStatusCode": status}},
        op,
    )


# --- the states ------------------------------------------------------------

def test_serialiser_refusal_never_reached_the_wire():
    exc = ParamValidationError(report="maxTokens must be positive")
    assert po.classify_exception(exc) == po.NOT_SUBMITTED
    assert po.refunds_immediately(po.NOT_SUBMITTED)


@pytest.mark.parametrize("exc", [
    ConnectTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"),
    EndpointConnectionError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"),
])
def test_never_connected_is_not_submitted(exc):
    assert po.classify_exception(exc) == po.NOT_SUBMITTED


def test_service_rejection_is_its_own_state():
    assert po.classify_exception(_client_error("ValidationException")) == (
        po.REJECTED_PRE_INFERENCE
    )
    assert po.refunds_immediately(po.REJECTED_PRE_INFERENCE)


def test_throttle_is_a_rejection_so_throttled_traffic_does_not_eat_budget():
    assert po.classify_exception(_client_error("ThrottlingException", 429)) == (
        po.REJECTED_PRE_INFERENCE
    )


def test_read_timeout_holds_the_ceiling():
    """The measured case: abandoned at the client, billed at the provider."""
    exc = ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")
    assert po.classify_exception(exc) == po.SUBMITTED_UNSETTLED
    assert not po.refunds_immediately(po.SUBMITTED_UNSETTLED)
    assert po.liability_for(po.SUBMITTED_UNSETTLED) == po.LIABILITY_FULL_CEILING


def test_server_error_is_unsettled_because_the_model_may_have_run():
    for code, status in (("InternalServerException", 500),
                         ("ServiceUnavailableException", 503),
                         ("ModelErrorException", 424)):
        assert po.classify_exception(_client_error(code, status)) == (
            po.SUBMITTED_UNSETTLED
        ), code


def test_an_unlisted_4xx_does_not_get_the_cheap_row():
    """A rejection by shape is not a rejection by evidence.

    Reading the status code and concluding "4xx, therefore free" is the same kind
    of assumption this module exists to avoid; a code nobody measured stays
    expensive until someone measures it.
    """
    assert po.classify_exception(_client_error("SomeNewBedrockRefusal", 400)) == (
        po.SUBMITTED_UNSETTLED
    )


def test_unrecognised_exception_lands_in_the_expensive_state():
    class Weird(Exception):
        pass

    assert po.classify_exception(Weird("no idea")) == po.SUBMITTED_UNSETTLED
    assert not po.refunds_immediately(po.SUBMITTED_UNSETTLED)


def test_unknown_state_is_expensive_not_free():
    """A programming error on a money path must fail toward holding."""
    assert po.liability_for("state_that_does_not_exist") == po.LIABILITY_FULL_CEILING
    assert not po.refunds_immediately("state_that_does_not_exist")


# --- the policy table ------------------------------------------------------

def test_every_state_has_a_policy_row_with_cited_evidence():
    for state in po.STATES:
        row = po.LIABILITY_POLICY[state]
        assert row["liability"] in (
            po.LIABILITY_NONE, po.LIABILITY_FULL_CEILING, po.LIABILITY_OBSERVED
        )
        assert row["evidence"] and len(row["evidence"]) > 40, (
            f"{state} needs the measurement that justifies its liability, so a "
            f"reader can tell a measured zero from an assumed one"
        )


def test_a_zero_liability_row_names_its_accepted_risk():
    """Except the one row that is a property of our own transport.

    A zero that costs nothing to be wrong about needs no risk statement; a zero
    that asserts something about the provider does.
    """
    for state, row in po.LIABILITY_POLICY.items():
        if row["liability"] != po.LIABILITY_NONE:
            continue
        if state == po.NOT_SUBMITTED:
            continue
        assert row["accepted_risk"], (
            f"{state} assigns zero liability from provider behaviour, so it must "
            f"say what happens when that behaviour changes"
        )


def test_measured_and_by_shape_rejection_codes_are_distinguished():
    """So nobody later reads the union and believes all of it was measured."""
    assert po._REJECTION_CODES_MEASURED
    assert po._REJECTION_CODES_BY_SHAPE
    assert not (po._REJECTION_CODES_MEASURED & po._REJECTION_CODES_BY_SHAPE)
    assert "ValidationException" in po._REJECTION_CODES_MEASURED
    assert "measured" in po.LIABILITY_POLICY[po.REJECTED_PRE_INFERENCE]["accepted_risk"]


def test_policy_version_is_pinned():
    assert po.LIABILITY_POLICY_VERSION


# --- the switch and the correlation handle ---------------------------------

def test_enforcement_is_on_unless_explicitly_turned_off(monkeypatch):
    monkeypatch.delenv(po.UNOBSERVED_HOLD_ENV, raising=False)
    assert po.unobserved_holds_enforced() is True
    monkeypatch.setenv(po.UNOBSERVED_HOLD_ENV, "1")
    assert po.unobserved_holds_enforced() is True
    monkeypatch.setenv(po.UNOBSERVED_HOLD_ENV, "no")
    assert po.unobserved_holds_enforced() is False


def test_request_metadata_carries_ids_and_nothing_else():
    md = po.attempt_request_metadata("hold-abc", "tenant-1")
    assert md == {"sc_attempt_id": "hold-abc", "sc_tenant": "tenant-1"}
    assert po.attempt_request_metadata(None) == {}
    long = po.attempt_request_metadata("x" * 400, "y" * 400)
    assert all(len(v) <= 256 for v in long.values()), (
        "Bedrock bounds requestMetadata values; an over-long id must not turn a "
        "billable call into a validation error"
    )


# ---------------------------------------------------------------------------
# Issue #247 — the OpenAI-compatible routes' correlation marker. These three
# functions decide what `metadata` the gateway actually sends on
# `/v1/chat/completions` and `/openai/v1/responses`: `merge_openai_metadata`
# (collision rule + OpenAI's 16-key ceiling), `stamp_openai_metadata_marker`
# (called from inside the payload builder, pre-reserve) and `stamp_attempt_id`
# (the post-reserve, same-length swap — see the module docstring above them).
# ---------------------------------------------------------------------------


class TestMergeOpenAIMetadata:
    def test_no_caller_metadata_gateway_keys_pass_through(self):
        marker = {"sc_attempt_id": "abc", "sc_tenant": "t1"}
        assert po.merge_openai_metadata(None, marker) == marker

    def test_caller_metadata_is_preserved_alongside_the_marker(self):
        caller = {"customer_ref": "order-42", "region": "jp"}
        marker = {"sc_attempt_id": "abc", "sc_tenant": "t1"}
        merged = po.merge_openai_metadata(caller, marker)
        assert merged == {
            "customer_ref": "order-42",
            "region": "jp",
            "sc_attempt_id": "abc",
            "sc_tenant": "t1",
        }
        # The input dict itself must not be mutated -- a caller-owned object
        # a route handed in must come back unchanged from its own point of view.
        assert caller == {"customer_ref": "order-42", "region": "jp"}

    def test_gateway_keys_win_on_a_name_collision(self):
        caller = {"sc_attempt_id": "whatever-the-caller-sent"}
        marker = {"sc_attempt_id": "real-hold-id"}
        merged = po.merge_openai_metadata(caller, marker)
        assert merged["sc_attempt_id"] == "real-hold-id", (
            "the gateway's own correlation id is the one that must reach the "
            "log record; a caller that happens to reuse the name must not "
            "silently own it"
        )

    def test_empty_marker_is_a_no_op(self):
        caller = {"a": "b"}
        assert po.merge_openai_metadata(caller, {}) == caller

    def test_non_dict_caller_metadata_is_treated_as_absent(self):
        marker = {"sc_attempt_id": "abc"}
        assert po.merge_openai_metadata("not-a-dict", marker) == marker
        assert po.merge_openai_metadata(123, marker) == marker
        assert po.merge_openai_metadata(None, marker) == marker
        assert po.merge_openai_metadata([1, 2, 3], marker) == marker

    def test_logs_when_the_marker_is_dropped_for_the_key_ceiling(self, capsys, caplog):
        import logging

        # `mvp.provider_outcome.logger` is structlog. Whether it prints
        # straight to stdout (capsys) or routes through stdlib logging
        # (caplog) depends on whether some OTHER test in this process has
        # already called `configure_logging()` -- observed to differ between
        # a solo run and a full `-n 4` xdist run, since worker/import order
        # differs. Checking both makes this test depend on the message being
        # emitted, not on which of the two paths structlog happens to be
        # wired through at the moment it runs. `caplog.set_level(INFO)` is
        # required on the stdlib-logging path: this event is logged at
        # `info` (an ordinary, caller-triggerable condition -- see
        # `merge_openai_metadata`'s comment), and caplog's own capturing
        # handler defaults to WARNING, which would silently drop it.
        caplog.set_level(logging.INFO)
        caller = {f"k{i}": "v" for i in range(po.OPENAI_METADATA_MAX_KEYS)}
        marker = {"sc_attempt_id": "abc", "sc_tenant": "t1"}
        po.merge_openai_metadata(caller, marker)
        out = capsys.readouterr().out
        seen = "openai_metadata_marker_dropped_key_ceiling" in out or any(
            "openai_metadata_marker_dropped_key_ceiling" in str(r.msg)
            for r in caplog.records
        )
        assert seen, "dropping the marker for the key ceiling must be observable, not silent"

    def test_caller_at_the_key_ceiling_drops_the_whole_marker_not_the_caller(self):
        # 16 keys is OpenAI's own documented ceiling (acceptance criterion:
        # "the gateway's marker fits with headroom for caller keys" / "never
        # break a valid caller request").
        caller = {f"k{i}": "v" for i in range(po.OPENAI_METADATA_MAX_KEYS)}
        marker = {"sc_attempt_id": "abc", "sc_tenant": "t1"}
        merged = po.merge_openai_metadata(caller, marker)
        assert merged == caller, (
            "a caller already at the 16-key ceiling must get its own request "
            "back unmodified -- refusing it, or pushing it over the ceiling, "
            "would turn a correlation nicety into an outage"
        )

    def test_marker_is_all_or_nothing_not_partial(self):
        # 15 keys: there is room for exactly ONE more, not both of ours. A
        # marker with only `sc_tenant` (or only `sc_attempt_id`) is a
        # misleading half-handle, not a smaller one, so neither is added.
        caller = {f"k{i}": "v" for i in range(po.OPENAI_METADATA_MAX_KEYS - 1)}
        marker = {"sc_attempt_id": "abc", "sc_tenant": "t1"}
        merged = po.merge_openai_metadata(caller, marker)
        assert "sc_attempt_id" not in merged
        assert "sc_tenant" not in merged
        assert merged == caller

    def test_collision_keys_are_stripped_not_counted_as_already_occupying_a_slot(self):
        # 15 keys, one of which IS one of ours ("sc_attempt_id"): it is
        # stripped as a reserved name BEFORE counting (not kept and
        # overwritten in place), leaving 14 of the caller's own keys plus
        # the 2-key marker = 16, which fits exactly.
        caller = {f"k{i}": "v" for i in range(po.OPENAI_METADATA_MAX_KEYS - 2)}
        caller["sc_attempt_id"] = "stale"
        marker = {"sc_attempt_id": "fresh", "sc_tenant": "t1"}
        merged = po.merge_openai_metadata(caller, marker)
        assert merged["sc_attempt_id"] == "fresh"
        assert merged["sc_tenant"] == "t1"
        assert len(merged) == po.OPENAI_METADATA_MAX_KEYS

    def test_a_caller_cannot_forge_the_reserved_keys_by_padding_metadata_to_the_ceiling(self):
        # Found in review (issue #247): the ORIGINAL version of this function
        # counted a caller's own `sc_tenant`/`sc_attempt_id` as "already
        # occupying a slot" and left it untouched whenever the marker had to
        # be dropped for the key-count ceiling -- so a caller who filled the
        # other 15 slots with filler and set `sc_tenant` to a victim tenant id
        # got that forged value sent to Bedrock's invocation log looking
        # exactly like the gateway's own correlation marker. The fix: the
        # reserved names are stripped unconditionally, on every path,
        # BEFORE anything is counted.
        caller = {f"k{i}": "v" for i in range(po.OPENAI_METADATA_MAX_KEYS - 1)}
        caller["sc_tenant"] = "victim-tenant"
        marker = {"sc_attempt_id": "real-hold-id", "sc_tenant": "real-tenant"}
        merged = po.merge_openai_metadata(caller, marker)
        assert merged.get("sc_tenant") != "victim-tenant", (
            "a caller-supplied value under a reserved key name must never "
            "reach the wire, whether or not the gateway's own marker fits"
        )
        assert "sc_attempt_id" not in merged
        assert "sc_tenant" not in merged
        # The caller's own 15 non-reserved keys are untouched.
        assert all(merged[f"k{i}"] == "v" for i in range(po.OPENAI_METADATA_MAX_KEYS - 1))

    def test_a_caller_cannot_forge_sc_attempt_id_the_same_way(self):
        caller = {f"k{i}": "v" for i in range(po.OPENAI_METADATA_MAX_KEYS - 1)}
        caller["sc_attempt_id"] = "another-tenants-hold-id"
        marker = {"sc_attempt_id": po.ATTEMPT_ID_PLACEHOLDER, "sc_tenant": "t1"}
        merged = po.merge_openai_metadata(caller, marker)
        assert merged.get("sc_attempt_id") != "another-tenants-hold-id"
        assert "sc_attempt_id" not in merged
        assert "sc_tenant" not in merged

    def test_reserved_key_stripping_is_case_and_whitespace_insensitive(self):
        # Round-3 review finding: a caller sending `SC_TENANT` or `sc_tenant `
        # does not literally collide with the lowercase, untrimmed key the
        # marker itself uses, so it would otherwise survive as a decoy a
        # case-insensitive log query could mistake for the gateway's field.
        caller = {"SC_TENANT": "victim", " sc_attempt_id ": "another-hold", "k": "v"}
        marker = {"sc_attempt_id": "real", "sc_tenant": "real-tenant"}
        merged = po.merge_openai_metadata(caller, marker)
        assert merged == {"k": "v", "sc_attempt_id": "real", "sc_tenant": "real-tenant"}

    def test_14_caller_keys_plus_both_reserved_names_still_fits_at_16(self):
        # The exact boundary round-3 asked to pin: 14 generic keys + the
        # caller's OWN (forged) copies of both reserved names = 16 keys
        # before stripping. After stripping, 14 generic keys remain, and the
        # gateway's 2 keys fit exactly (14 + 2 = 16) -- the marker is
        # stamped, not dropped, and none of the caller's forged values leak
        # through.
        caller = {f"k{i}": "v" for i in range(14)}
        caller["sc_attempt_id"] = "forged-attempt"
        caller["sc_tenant"] = "forged-tenant"
        assert len(caller) == 16
        marker = {"sc_attempt_id": "real-attempt", "sc_tenant": "real-tenant"}
        merged = po.merge_openai_metadata(caller, marker)
        assert len(merged) == 16
        assert merged["sc_attempt_id"] == "real-attempt"
        assert merged["sc_tenant"] == "real-tenant"
        assert all(merged[f"k{i}"] == "v" for i in range(14))

    def test_reserved_keys_constant_matches_attempt_request_metadata(self):
        # Round-3 nit: the reserved-name set must be the SAME set
        # `attempt_request_metadata` (Converse's own marker) actually
        # produces, by construction -- not two places maintaining the same
        # two string literals independently.
        assert po.RESERVED_METADATA_KEYS == frozenset(
            po.attempt_request_metadata("x", "x", "x").keys()
        ) == frozenset(po.MARKER_KEYS) == frozenset(po.MARKER_HEADERS)


class TestStampOpenAIMetadataMarker:
    def test_adds_placeholder_attempt_id_and_tenant(self):
        payload = {"input": "hi"}
        added = po.stamp_openai_metadata_marker(payload, "tenant-1")
        assert added is True
        assert payload["metadata"] == {
            "sc_attempt_id": po.ATTEMPT_ID_PLACEHOLDER,
            "sc_tenant": "tenant-1",
        }

    def test_placeholder_is_the_same_length_as_a_real_hold_id(self):
        import uuid

        assert len(po.ATTEMPT_ID_PLACEHOLDER) == len(str(uuid.uuid4()))

    def test_placeholder_matches_the_actual_hold_id_minting_function(self):
        # The length match above is against `uuid.uuid4()` directly; this
        # pins it against the function `mvp._pipeline` actually calls, so a
        # future change to how hold ids are minted (not just to uuid's own
        # string format) is caught here rather than silently disabling the
        # swap in `stamp_attempt_id` everywhere.
        from mvp._pipeline import _fresh_idempotency_token

        assert len(_fresh_idempotency_token()) == len(po.ATTEMPT_ID_PLACEHOLDER)

    def test_preserves_callers_own_metadata(self):
        payload = {"input": "hi", "metadata": {"order_id": "o-1"}}
        po.stamp_openai_metadata_marker(payload, "tenant-1")
        assert payload["metadata"]["order_id"] == "o-1"
        assert payload["metadata"]["sc_tenant"] == "tenant-1"

    def test_no_tenant_still_stamps_the_attempt_placeholder(self):
        payload = {"input": "hi"}
        po.stamp_openai_metadata_marker(payload, None)
        assert payload["metadata"] == {"sc_attempt_id": po.ATTEMPT_ID_PLACEHOLDER}

    def test_returns_false_and_leaves_caller_metadata_untouched_at_the_ceiling(self):
        caller_md = {f"k{i}": "v" for i in range(po.OPENAI_METADATA_MAX_KEYS)}
        payload = {"input": "hi", "metadata": dict(caller_md)}
        added = po.stamp_openai_metadata_marker(payload, "tenant-1")
        assert added is False
        assert payload["metadata"] == caller_md


class TestStampAttemptId:
    def test_swaps_placeholder_for_the_real_hold_id(self):
        payload = {"metadata": {"sc_attempt_id": po.ATTEMPT_ID_PLACEHOLDER, "sc_tenant": "t1"}}
        real_id = "11111111-2222-3333-4444-555555555555"
        po.stamp_attempt_id(payload, real_id)
        assert payload["metadata"]["sc_attempt_id"] == real_id
        assert payload["metadata"]["sc_tenant"] == "t1"

    def test_swap_does_not_change_the_serialised_byte_length(self):
        import json

        payload = {"metadata": {"sc_attempt_id": po.ATTEMPT_ID_PLACEHOLDER}}
        before = len(json.dumps(payload).encode("utf-8"))
        po.stamp_attempt_id(payload, "11111111-2222-3333-4444-555555555555")
        after = len(json.dumps(payload).encode("utf-8"))
        assert before == after, (
            "the hard-ceiling bound (docs/design/hard-ceiling.md section 3a) "
            "is computed over the payload WITH the placeholder in it; the "
            "post-reserve swap must not move the payload's serialised byte "
            "count out from under that bound"
        )

    def test_no_op_when_the_marker_was_never_stamped(self):
        payload = {"metadata": {"other": "value"}}
        po.stamp_attempt_id(payload, "some-hold-id")
        assert payload["metadata"] == {"other": "value"}

    def test_no_op_without_metadata_at_all(self):
        payload = {"input": "hi"}
        po.stamp_attempt_id(payload, "some-hold-id")
        assert payload == {"input": "hi"}

    def test_no_usable_id_deletes_only_the_attempt_id_not_the_whole_marker(self):
        # Round-2 review finding: leaving the PLACEHOLDER on the wire when
        # there is no real id to swap in would put the SAME 36 characters on
        # every such request -- a marker that looks real, joins to nothing,
        # and is indistinguishable from every other request this ever
        # happens to. Deleting it only ever shrinks the payload, which cannot
        # move the serialised byte count above what the bound was computed
        # over, so it stays sound.
        #
        # `sc_tenant`, unlike `sc_attempt_id`, is NOT a placeholder here --
        # `stamp_openai_metadata_marker` resolves it to its real value at
        # build time with no placeholder stage, so by the time this runs it
        # is already correct, true information. Discarding it along with the
        # attempt id would throw away a working correlation handle for no
        # reason, and would make this route STRICTER than Converse, which
        # sends `attempt_request_metadata`'s `sc_tenant` alone in exactly
        # this situation (an unpooled tenant with no hold id at all --
        # hard-ceiling.md section 7a).
        payload = {
            "metadata": {
                "sc_attempt_id": po.ATTEMPT_ID_PLACEHOLDER,
                "sc_tenant": "t1",
                "caller_key": "x",
            }
        }
        po.stamp_attempt_id(payload, None)
        assert "sc_attempt_id" not in payload["metadata"]
        assert payload["metadata"]["sc_tenant"] == "t1"
        assert payload["metadata"]["caller_key"] == "x"

    def test_no_usable_id_drops_metadata_entirely_when_nothing_else_is_in_it(self):
        payload = {"metadata": {"sc_attempt_id": po.ATTEMPT_ID_PLACEHOLDER}}
        po.stamp_attempt_id(payload, None)
        assert "metadata" not in payload

    def test_mutation_guard_a_length_changing_id_is_refused_not_swapped_in(self):
        # If a future change to hold-id minting ever produced a differently
        # sized id, swapping it in would silently move the bound out from
        # under the payload actually sent. This pins the fail-safe: such an
        # id is refused (and the attempt-id key deleted, same as the falsy
        # case), never substituted.
        payload = {"metadata": {"sc_attempt_id": po.ATTEMPT_ID_PLACEHOLDER}}
        po.stamp_attempt_id(payload, "too-short")
        assert "metadata" not in payload


# ---------------------------------------------------------------------------
# The other transport. Every route that reaches the Bedrock OpenAI-compatible
# endpoint raises `httpx` exceptions, and none of them was recognised here: they
# all fell through to the catch-all and held a full ceiling. That is the right
# default for something unrecognised and the wrong answer for a TLS handshake that
# failed -- a reservation retained for a request that provably never left.
# ---------------------------------------------------------------------------
class TestHttpxExceptionsAreClassified:
    def test_a_connection_that_was_never_established_retains_nothing(self):
        import httpx

        from mvp.provider_outcome import (
            NOT_SUBMITTED, classify_exception, refunds_immediately,
        )

        for exc in (httpx.ConnectError("no route"), httpx.ConnectTimeout("slow"),
                    httpx.PoolTimeout("pool full"), httpx.InvalidURL("bad")):
            assert classify_exception(exc) == NOT_SUBMITTED, type(exc).__name__
        # The reading only matters because of what it implies for the money: this is
        # the row that returns the reservation immediately.
        assert refunds_immediately(NOT_SUBMITTED) is True

    def test_a_request_that_left_keeps_the_ceiling(self):
        import httpx

        from mvp.provider_outcome import SUBMITTED_UNSETTLED, classify_exception

        for exc in (httpx.ReadTimeout("silent"), httpx.ReadError("cut"),
                    httpx.RemoteProtocolError("bad frame")):
            assert classify_exception(exc) == SUBMITTED_UNSETTLED, type(exc).__name__

    def test_a_partial_send_stays_expensive(self):
        """`WriteError`/`WriteTimeout` are deliberately NOT named as never-submitted:
        a partial send may have delivered a complete request."""
        import httpx

        from mvp.provider_outcome import SUBMITTED_UNSETTLED, classify_exception

        assert classify_exception(httpx.WriteTimeout("half")) == SUBMITTED_UNSETTLED
        assert classify_exception(httpx.WriteError("half")) == SUBMITTED_UNSETTLED

    def test_the_unknown_is_still_expensive(self):
        """The non-vacuous companion: naming some classes must not turn the
        catch-all into a cheap answer."""
        from mvp.provider_outcome import SUBMITTED_UNSETTLED, classify_exception

        assert classify_exception(RuntimeError("who knows")) == SUBMITTED_UNSETTLED
