"""A money decision may not branch on an attribute nothing writes.

This exists because of a specific failure, and the failure is worth stating exactly,
because the shape of it is what the check is for.

`STRATOCLAVE_UNOBSERVED_HOLDS` turns on retaining a reservation whose provider call
had departed. The reaper's branch read `hold.get("provider_invoked_at")` to decide
whether a call had departed. Nothing in production wrote that attribute. So the
branch could never be taken, the feature could never fire, and the contract claimed
it at a guarantee level — while the test that "proved" it wrote the attribute
directly, from the test, and passed.

Every existing guard missed it. The suite passed, because the test manufactured the
state. Mutation checking passed, because reverting the branch did change the test's
outcome. The write-discipline guard passed, because no money counter was touched. The
claim lint passed, because the sentence pointed at a clause and the clause pointed at
a test that existed. A feature that cannot fire is worse than one that is off,
because the operator believes it is on — and nothing in a suite that only ever drives
production forward can see it.

WHAT THIS CHECKS

In the modules where money is decided, every attribute name read out of a row inside
a CONDITION must be written somewhere in production code. Reads outside conditions are
not checked: carrying an absent value through to a log line or a response is exactly
what the absence rule asks for. It is the branch that is the problem — a branch on an
unwritten fact is dead code wearing a feature's name.

WHAT IT DOES NOT CHECK

That the writer runs on a path that reaches the reader. A writer behind its own dead
flag would satisfy this and still leave the branch unreachable. Closing that needs an
integration test per branch, which is a judgement about which branches matter; this
is the mechanical floor underneath it.
"""
from __future__ import annotations

import ast
import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Where money is decided. Deliberately not "everywhere": a config reader branching
#: on an absent key is choosing a default, which is its job, and sweeping those in
#: would fill this with exemptions until an exemption is where the next one hides.
MONEY_MODULES = (
    "mvp/_pipeline.py",
    "mvp/_money.py",
    "mvp/provider_outcome.py",
    "mvp/billing_authorize.py",
    "dynamo/tenant_budgets.py",
    "dynamo/credit_ledger.py",
)

#: Keys of a DynamoDB *response*, not attributes of an item. Nothing writes them
#: because the service does.
RESPONSE_KEYS = frozenset({
    "Item", "Items", "Attributes", "LastEvaluatedKey", "Count", "ScannedCount",
    "Error", "Code", "Message", "CancellationReasons", "ResponseMetadata",
})


def _keys_read_in_conditions(path: pathlib.Path) -> dict[str, int]:
    """{attribute name: first line}, for `.get("name")` inside a branch condition."""
    found: dict[str, int] = {}

    def keys_of(node: ast.AST):
        for n in ast.walk(node):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get"
                and n.args
                and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[0].value, str)
            ):
                yield n.args[0].value, n.lineno

    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        test = getattr(node, "test", None)
        if test is None:
            continue
        for name, line in keys_of(test):
            found.setdefault(name, line)
    return found


def _production_source() -> str:
    parts = []
    for pkg in ("mvp", "dynamo", "core", "migrations"):
        d = ROOT / pkg
        if not d.exists():
            continue
        parts += [p.read_text(errors="replace") for p in d.rglob("*.py")]
    return "\n".join(parts)


def _is_written(name: str, source: str) -> bool:
    """Whether production code anywhere writes this attribute ONTO A ROW.

    Three shapes, because the codebase legitimately uses all three: a literal key in
    an item dict (`"name": value`), a subscript assignment onto an item
    (`item["name"] = value`), and an UpdateExpression fragment (`SET name = :v`).

    A keyword argument (`name=...`) is deliberately NOT one of them, and that
    exclusion is the whole check. It was included at first, and it made the check
    vacuous for the very attribute that motivated it: `provider_invoked_at` was
    passed as a keyword to a ledger builder and to a log line in the same module that
    branched on it, so "somebody mentions this name in an assignment" was satisfied
    while nothing put it on a row. Passing a value to a function is not persisting a
    fact, and a detector that cannot tell those apart answers yes to everything.
    """
    n = re.escape(name)
    return bool(
        re.search(r'["\']%s["\']\s*:' % n, source)
        or re.search(r'\[\s*["\']%s["\']\s*\]\s*=' % n, source)
        or re.search(r'\b%s\s*=\s*:' % n, source)
    )


def test_no_money_branch_reads_an_attribute_nothing_writes():
    source = _production_source()
    problems: list[str] = []
    for rel in MONEY_MODULES:
        path = ROOT / rel
        for name, line in sorted(_keys_read_in_conditions(path).items()):
            if name in RESPONSE_KEYS:
                continue
            if not _is_written(name, source):
                problems.append(
                    f"{rel}:{line} branches on {name!r}, and no production code "
                    f"writes it — so the branch cannot be taken by a real request, "
                    f"whatever a test that sets the attribute itself shows"
                )
    assert not problems, "\n  ".join([""] + problems)


def test_the_check_can_actually_fail():
    """The guard on the guard.

    Both halves are pinned, because either one silently breaking leaves a test that
    passes by reading nothing: the condition walker must find real keys, and the
    writer detector must be able to answer no.
    """
    found = _keys_read_in_conditions(ROOT / "mvp" / "_pipeline.py")
    assert len(found) >= 5, f"only {len(found)} branch-condition keys parsed"
    source = _production_source()
    assert not _is_written("an_attribute_no_module_ever_writes", source)
    # And a name that IS written is recognised, so the detector is not answering no
    # to everything.
    assert _is_written("provider_invoked_at", source)


#: Every module that opens a reservation-owning `Hold`. A fourth route added here
#: without the marker would leave retention unreachable on that route only, which is
#: the quiet half of the same defect.
ROUTE_MODULES = ("mvp/anthropic.py", "mvp/chat_completions.py", "mvp/openai_responses.py")


def test_every_route_wires_the_departure_marker():
    """The writer has to be connected on every route, not on the one with a test.

    The behavioural test for retention builds its own `Hold` and passes the marker in,
    which proves the ending records the departure — and would go on passing if a route
    stopped handing the marker over. So the wiring is checked where it lives: every
    construction of a reservation-owning `Hold` names `mark_departed`.
    """
    problems: list[str] = []
    for rel in ROUTE_MODULES:
        tree = ast.parse((ROOT / rel).read_text())
        sites = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "Hold"
        ]
        if not sites:
            problems.append(f"{rel}: no Hold construction found; this list has drifted")
            continue
        for site in sites:
            names = {kw.arg for kw in site.keywords if kw.arg}
            if "mark_departed" not in names:
                problems.append(
                    f"{rel}:{site.lineno} opens a Hold without `mark_departed`, so an "
                    f"unobserved ending on this route keeps the reservation and records "
                    f"nothing — the reaper then hands the budget back"
                )
    assert not problems, "\n  ".join([""] + problems)
