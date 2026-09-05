"""The audit query in ADMIN_GUIDE.md may only name fields the audit writer emits.

The guide shipped a CloudWatch Logs Insights query selecting `target_email`, a field
`log_audit_event` has never written. An operator running it got a column of blanks,
and a blank column in an audit query is the failure mode you least want: it looks
exactly like a quiet system.

The check is not "the query is correct", which needs a human. It is mechanical: the
field list in the documented query is compared against the keys the writer actually
produces when every argument is supplied. A field renamed in the writer, or invented
in the guide, fails here rather than in an incident.

`@timestamp` is excluded from the comparison because CloudWatch supplies it rather
than the writer.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from mvp.authz import log_audit_event

_GUIDE = Path(__file__).resolve().parents[2] / "docs" / "ADMIN_GUIDE.md"

#: Fields CloudWatch itself provides, so the writer is not expected to emit them.
_CLOUDWATCH_PROVIDED = frozenset({"@timestamp", "@message", "@logStream"})


def _documented_fields() -> list[str]:
    """The field list from the guide's Logs Insights query."""
    text = _GUIDE.read_text(encoding="utf-8")
    matches = re.findall(r"^fields (.+)$", text, flags=re.MULTILINE)
    assert matches, "ADMIN_GUIDE.md no longer contains a Logs Insights `fields` line"
    assert len(matches) == 1, (
        f"ADMIN_GUIDE.md has {len(matches)} `fields` lines; this check assumes the one "
        "audit query, so add the new one to this test deliberately rather than letting "
        "it go unchecked"
    )
    return [f.strip() for f in matches[0].split(",") if f.strip()]


def _emitted_keys(caplog: pytest.LogCaptureFixture) -> set[str]:
    """The keys `log_audit_event` writes when every optional argument is supplied.

    Every argument is passed, because a field the guide names is only missing from the
    writer's output if the writer never emits it at all -- not merely if this call
    happened to omit it.
    """
    with caplog.at_level(logging.INFO, logger="stratoclave.audit"):
        log_audit_event(
            event="admin_created",
            actor_id="user-1",
            actor_email="actor@example.com",
            target_id="target-1",
            target_type="user",
            tenant_id="acme",
            before={"role": "user"},
            after={"role": "admin"},
            details={"reason": "promotion"},
        )
    lines = [r.getMessage() for r in caplog.records if r.name == "stratoclave.audit"]
    assert len(lines) == 1, f"expected one audit line, got {len(lines)}"
    return set(json.loads(lines[0]).keys())


def test_documented_query_names_only_fields_the_writer_emits(caplog):
    """No field in the guide's query is absent from the writer's payload.

    This is the assertion that `target_email` failed: it was in the query and had
    never been in the payload.
    """
    documented = [f for f in _documented_fields() if f not in _CLOUDWATCH_PROVIDED]
    emitted = _emitted_keys(caplog)
    missing = [f for f in documented if f not in emitted]
    assert not missing, (
        f"ADMIN_GUIDE.md's audit query selects {missing}, which log_audit_event does "
        f"not emit. It emits {sorted(emitted)}. An operator running that query gets a "
        f"blank column, which is indistinguishable from a quiet system."
    )


def test_documented_query_carries_an_actor_and_a_subject(caplog):
    """The query must still let an operator answer who did what to whom.

    Checking only that every named field exists would be satisfied by a query naming
    nothing, so the useful minimum is pinned too: the actor's identity, the actor's
    marker, and the subject of the action.
    """
    documented = set(_documented_fields())
    emitted = _emitted_keys(caplog)
    for required in ("event", "actor_id", "target_id", "tenant_id"):
        assert required in documented, (
            f"the audit query no longer selects {required!r}, so an operator cannot "
            f"answer who did what to whom from its output"
        )
        assert required in emitted


def test_the_guide_does_not_promise_a_plaintext_address(caplog):
    """The guide must not tell an operator to select an address field.

    An address is deliberately not in an audit line. A guide that selects one is
    either naming a field that does not exist or describing a state this project's
    own clause C12.4 forbids -- and it was the first of those for long enough that
    nobody noticed the column was empty.
    """
    documented = set(_documented_fields())
    for forbidden in ("actor_email", "target_email", "email", "user_email"):
        assert forbidden not in documented, (
            f"ADMIN_GUIDE.md's audit query selects {forbidden!r}. An audit line carries "
            f"a marker, not an address; select actor_email_hash and compute the marker "
            f"from a known address to search by it."
        )
