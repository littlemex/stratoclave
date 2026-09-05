"""Contract tests for C12.6 (audit log email scrub) and its two riders,
P-E2 (field rename) and P-E3 (marker correspondence with UsageLogs).

`mvp/authz.py::log_audit_event` must never emit a plaintext address, by
ANY route a caller can get one into the payload. The router team filed
four routes; review found two more that a naive "recursive walk over
dict/list values, before json.dumps" fix passes while still leaking:

  R1  the named ``actor_email`` argument                  (admin_tenants)
  R2  an address as a VALUE inside ``details``             (admin_users)
  R3  an address inside a human-readable ``reason`` sentence (sso_exchange)
  R4  an address AS ``target_id`` itself                   (admin_sso_invites)
  R5  an address as a dict KEY inside ``details``          (review finding)
  R6  an address reachable only through ``default=str``    (review finding)

R5 and R6 are the two that distinguish a complete fix (scrub the
*serialised* string, once, after ``json.dumps(..., default=str)``) from
one that only walks the pre-serialisation dict/list structure: a walk
over values never looks at keys (R5), and a walk over the structure runs
before ``default=str`` has had a chance to turn a non-str/dict/list leaf
into a string (R6).

Each test calls ``log_audit_event`` directly and asserts against the
*emitted line* (via ``caplog`` on the ``stratoclave.audit`` logger,
matching the pattern in test_ceiling_mode_sentence_r21.py) with a regex
for an address-shaped substring, rather than asserting that one
particular key was scrubbed -- the strong form the task calls for, since
a per-key assertion would miss a leak the author didn't think to name a
key for.

The implementation does not exist at this worktree's base (`bb0fb2c`),
so every leak-shaped test below is EXPECTED TO FAIL until it lands.
"""
from __future__ import annotations

import json
import logging
import re

from dynamo import usage_logs

from mvp import authz

# A conservative "looks like an email" match: local-part @ host . tld.
# Intentionally independent of whatever pattern the eventual fix uses --
# this is the auditor's regex, not the implementation's.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _emit(caplog, **kwargs) -> str:
    """Call log_audit_event and return the single emitted audit line."""
    caplog.set_level(logging.INFO, logger="stratoclave.audit")
    authz.log_audit_event(**kwargs)
    lines = [r.getMessage() for r in caplog.records if r.name == "stratoclave.audit"]
    assert len(lines) == 1, f"expected exactly one audit line, got {len(lines)}: {lines}"
    return lines[0]


# ---------------------------------------------------------------------------
# R1 (reported) -- named actor_email argument. Call site: admin_tenants
# (create_tenant / _provision_seat_pool pass actor.email as actor_email=).
# ---------------------------------------------------------------------------

def test_actor_email_argument_is_not_in_audit_line(caplog):
    line = _emit(
        caplog,
        event="tenant_created",
        actor_id="admin-1",
        actor_email="alice@example.com",
        target_id="acme-eng",
        target_type="tenant",
        details={"name": "Acme Eng", "team_lead_user_id": "u-9"},
    )
    assert not _EMAIL_RE.search(line), f"address leaked in audit line: {line!r}"


# ---------------------------------------------------------------------------
# R2 (reported) -- an address as a VALUE inside `details`. Call site:
# admin_users (create_user / delete_user pass details={"email": email, ...}).
# ---------------------------------------------------------------------------

def test_email_value_inside_details_is_not_in_audit_line(caplog):
    # actor_email is deliberately omitted so the ONLY address in the line is
    # the details["email"] value -- an assertion failure here can only be
    # about route 2.
    line = _emit(
        caplog,
        event="user_created",
        actor_id="admin-1",
        target_id="u-42",
        target_type="user",
        tenant_id="acme-eng",
        details={"email": "bob@example.com", "role": "user", "allow_admin_creation": False},
    )
    assert not _EMAIL_RE.search(line), f"address leaked in audit line: {line!r}"


# ---------------------------------------------------------------------------
# R3 (reported) -- an address inside a human-readable `reason` sentence.
# Call site: sso_exchange. No current sso_exchange call site literally
# passes an email-embedded sentence to log_audit_event's `reason` (its
# `sso_login_denied` branches pass either `e.detail` -- which, walking
# sso_gate.py, never itself embeds an address -- or a static reason
# string plus a SEPARATE `email` key). The shape this test pins is the
# one the HTTPException raised in that same branch actually uses
# (`f"{trusted.email} already has a password-based Cognito account. ..."`,
# mvp/sso_exchange.py ~line 280): an address inline in an otherwise
# human-readable sentence. See report for this as a flagged ambiguity.
# ---------------------------------------------------------------------------

def test_email_inside_reason_sentence_is_scrubbed_in_place(caplog):
    line = _emit(
        caplog,
        event="sso_login_denied",
        actor_id="sso:123456789012",
        target_id="us-east-1:AROAFAKE:carol",
        details={
            "reason": (
                "carol@example.com already has a password-based Cognito "
                "account. Ask an administrator to delete and re-invite."
            ),
        },
    )
    assert not _EMAIL_RE.search(line), f"address leaked in audit line: {line!r}"
    # The fix must rewrite the address IN PLACE, not drop the sentence: the
    # rest of the sentence must still be legible around the redaction.
    assert "already has a password-based Cognito account." in line
    assert "Ask an administrator to delete and re-invite." in line


# ---------------------------------------------------------------------------
# R4 (reported) -- an address as `target_id` itself. Call site:
# admin_sso_invites (an invite is keyed by the address it invites).
# ---------------------------------------------------------------------------

def test_email_as_target_id_is_not_in_audit_line(caplog):
    # actor_email is deliberately omitted so the ONLY address in the line is
    # target_id -- an assertion failure here can only be about route 4.
    line = _emit(
        caplog,
        event="sso_invite_created",
        actor_id="admin-1",
        target_id="dave@example.com",
        target_type="sso_invite",
        details={"account_id": "123456789012", "invited_role": "user", "iam_user_name": None},
    )
    assert not _EMAIL_RE.search(line), f"address leaked in audit line: {line!r}"


# ---------------------------------------------------------------------------
# R5 (review finding, NOT filed by the requester) -- an address as a dict
# KEY inside `details`. A recursive walk over dict VALUES -- the shape of
# the originally filed fix -- never inspects dict KEYS, so a per-invitee
# status map keyed by address sails through untouched. Designed to fail a
# naive values-only walk while R1-R4 above pass it.
# ---------------------------------------------------------------------------

def test_email_as_a_dict_key_is_not_in_audit_line(caplog):
    # actor_email is deliberately omitted so the ONLY addresses in the line
    # are the dict keys -- an assertion failure here can only be about route 5.
    line = _emit(
        caplog,
        event="sso_invites_bulk_status",
        actor_id="admin-1",
        target_id="acme-eng",
        target_type="sso_invite_batch",
        details={"per_invite_status": {"erin@example.com": "sent", "frank@example.com": "pending"}},
    )
    assert not _EMAIL_RE.search(line), f"address leaked in audit line: {line!r}"


# ---------------------------------------------------------------------------
# R6 (review finding, NOT filed by the requester) -- an address reachable
# only through `default=str`. A scrub that walks the pre-serialisation
# dict/list structure runs BEFORE json.dumps(..., default=str) has had a
# chance to coerce a non-str/dict/list leaf into a string, so it never
# sees a leaf like an exception whose __str__ happens to embed an
# address. Designed to fail a walk-then-serialise fix while R1-R4 pass it.
# ---------------------------------------------------------------------------

class _FakeProviderError(Exception):
    """Stand-in for an upstream error whose str() happens to carry an
    address -- e.g. Cognito or STS echoing back the identity it rejected."""


def test_email_reachable_only_through_default_str_is_not_in_audit_line(caplog):
    line = _emit(
        caplog,
        event="sso_user_provisioned",
        actor_id="sso:123456789012",
        target_id="u-77",
        target_type="user",
        details={"upstream_error": _FakeProviderError("frank@example.com not found")},
    )
    assert not _EMAIL_RE.search(line), f"address leaked in audit line: {line!r}"


# ---------------------------------------------------------------------------
# P-E2 -- the emitted field is `actor_email_hash`; the plaintext-named key
# `"actor_email":` must not appear (the exact-colon substring so this does
# NOT false-positive on the correct `"actor_email_hash":` key it is
# replaced by).
# ---------------------------------------------------------------------------

def test_actor_email_field_is_renamed_to_actor_email_hash(caplog):
    line = _emit(
        caplog,
        event="tenant_created",
        actor_id="admin-1",
        actor_email="alice@example.com",
        target_id="acme-eng",
        target_type="tenant",
    )
    assert '"actor_email":' not in line
    assert '"actor_email_hash":' in line


# ---------------------------------------------------------------------------
# P-E3 -- the same actor yields the SAME marker in an audit line and in a
# usage row: the marker equals dynamo.usage_logs.hash_user_email(address),
# the digest a usage row carries. Derived, not hard-coded, so this pins
# the CORRESPONDENCE between the two writers rather than one writer's
# literal digest.
# ---------------------------------------------------------------------------

def test_actor_email_hash_matches_usage_log_hash_for_the_same_address(caplog):
    address = "grace@example.com"
    line = _emit(
        caplog,
        event="tenant_created",
        actor_id="admin-1",
        actor_email=address,
        target_id="acme-eng",
        target_type="tenant",
    )
    payload = json.loads(line)
    assert payload["actor_email_hash"] == usage_logs.hash_user_email(address)


def test_actor_email_hash_correspondence_is_case_insensitive(caplog):
    """usage_logs.hash_user_email lower-cases before hashing (Cognito
    normalises case but external IdPs may not); the audit writer's marker
    must agree with a usage row's marker for the SAME actor regardless of
    which casing either writer happened to see.
    """
    line = _emit(
        caplog,
        event="tenant_created",
        actor_id="admin-1",
        actor_email="Grace@Example.com",
        target_id="acme-eng",
        target_type="tenant",
    )
    payload = json.loads(line)
    assert payload["actor_email_hash"] == usage_logs.hash_user_email("grace@example.com")


# ---------------------------------------------------------------------------
# actor_id survives, unchanged, alongside the actor_email -> actor_email_hash
# rename.
# ---------------------------------------------------------------------------

def test_actor_id_is_present_and_unchanged(caplog):
    line = _emit(
        caplog,
        event="tenant_created",
        actor_id="admin-1",
        actor_email="alice@example.com",
        target_id="acme-eng",
        target_type="tenant",
    )
    payload = json.loads(line)
    assert payload["actor_id"] == "admin-1"


# ---------------------------------------------------------------------------
# The scrub must not corrupt ordinary content: a payload whose only address
# is the actor's must come out identical apart from the field rename, and a
# payload with no address anywhere (including a near-miss "@" that is not
# part of an address) must be untouched.
# ---------------------------------------------------------------------------

def test_ordinary_payload_survives_scrub_unchanged_apart_from_the_rename(caplog):
    line = _emit(
        caplog,
        event="tenant_pool_mode_changed",
        actor_id="admin-1",
        actor_email="operator@example.com",
        target_id="acme-eng",
        target_type="tenant",
        before={"seat_tracked": True, "manual_limit_microusd": None, "seats": 3},
        after={"seat_tracked": False, "manual_limit_microusd": 50_000, "seats": 3},
        details={"note": "switched via /pool-budget PUT", "count": 3, "tags": ["billing", "manual"]},
    )
    payload = json.loads(line)
    assert payload["event"] == "tenant_pool_mode_changed"
    assert payload["actor_id"] == "admin-1"
    assert payload["target_id"] == "acme-eng"
    assert payload["target_type"] == "tenant"
    assert payload["before"] == {"seat_tracked": True, "manual_limit_microusd": None, "seats": 3}
    assert payload["after"] == {"seat_tracked": False, "manual_limit_microusd": 50_000, "seats": 3}
    assert payload["details"] == {
        "note": "switched via /pool-budget PUT", "count": 3, "tags": ["billing", "manual"],
    }
    assert "actor_email" not in payload
    assert payload["actor_email_hash"] == usage_logs.hash_user_email("operator@example.com")


def test_payload_with_at_sign_but_no_address_is_unaffected(caplog):
    """Precision guard, the flip side of the leak tests above: an `@` that
    is not part of an address (a time-of-day mention, a handle with no
    domain) must survive untouched. A scrub that over-matches on any bare
    '@' would corrupt ordinary content, which this contract forbids just
    as much as a leak.
    """
    line = _emit(
        caplog,
        event="tenant_created",
        actor_id="admin-1",
        target_id="acme-eng",
        target_type="tenant",
        details={"name": "Acme Eng", "note": "reached the seat cap @ 3pm, no domain here"},
    )
    payload = json.loads(line)
    assert payload["details"]["note"] == "reached the seat cap @ 3pm, no domain here"
    assert "actor_email" not in payload
    assert "actor_email_hash" not in payload
