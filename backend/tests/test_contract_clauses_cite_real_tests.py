"""Every P/E clause in the contract cites a test that exists.

The contract's own opening rule is that a clause with no test is a statement about
one commit. The failure mode that rule does not prevent is subtler and much more
likely: the clause keeps its citation while the test it names is renamed, moved or
deleted. Nothing breaks, the row still reads as enforced, and the document degrades
into exactly the decoration it was written against — with the extra harm that it
now looks audited.

So the citation is checked. A clause at **P** or **E** must name at least one
`test_*.py`, because for those levels the citation IS the clause. What any row
names — including a mixed row like "E for the mechanisms; B against an operator
recreating the row" — must then resolve: the file has to exist under
`backend/tests/`, and a `file::node` or backticked `test_...` has to be a function
defined in the suite. Only the requirement to cite is level-gated; a citation that
exists is checked wherever it appears, since a rot in a B row's enforced half reads
just as authoritative.

WHERE THIS ENDS AND ANOTHER CHECK BEGINS

`test_doc_references_resolve.py` already resolves every `test_...` name cited
anywhere in the repository, so the node half of a citation is covered there rather
than duplicated here — and closing this gap surfaced a hole in that check, which
skipped a citation of the form `test_foo.py` instead of resolving it as a module.
It resolves it now. What remains genuinely local to this file is the requirement a
contract has and a document in general does not: a clause claiming P or E must cite
SOMETHING. An enforced-looking row with an empty third column is invisible to a
checker that only validates the citations that are present.

WHAT NEITHER CHECKS

That the cited test actually establishes the clause. Nothing mechanical can.
"""
from __future__ import annotations

import ast
import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[2]
TESTS = pathlib.Path(__file__).resolve().parent
CONTRACTS = ROOT / "docs" / "design" / "CONTRACTS.md"

#: A clause row: | **C1.2** text | LEVEL | enforced-by |
ROW = re.compile(r"^\|\s*\*\*(C\d+(?:\.\d+[a-c]?)?)\*\*(.*?)\|(.*?)\|(.*)\|\s*$")

#: `test_foo.py` or `test_foo.py::test_bar` or a bare `test_bar` in backticks.
FILE_REF = re.compile(r"([A-Za-z0-9_]*test_[A-Za-z0-9_]*\.py)(?:::([A-Za-z0-9_]+))?")
NODE_REF = re.compile(r"`\W*(test_[A-Za-z0-9_]+)`")


def _levels_and_citations():
    for line in CONTRACTS.read_text().split("\n"):
        m = ROW.match(line.strip())
        if not m:
            continue
        clause, _text, level, enforced = m.group(1), m.group(2), m.group(3), m.group(4)
        yield clause, level.strip(), enforced


def _proven_or_enforced(level: str) -> bool:
    """A level cell is prose ("E, with a **P** subset", "**B** — and NOT in the
    default deployment"), so read it for what it asserts rather than matching it
    whole. A cell naming B or N somewhere is making a bounded claim and its third
    column is a configuration; only a cell that is purely P/E promises a test."""
    letters = set(re.findall(r"\b([PEBN])\b", level))
    return bool(letters & {"P", "E"}) and not (letters & {"B", "N"})


def _defined_test_functions() -> set[str]:
    names: set[str] = set()
    for path in TESTS.rglob("test_*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - a broken test file fails elsewhere
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_"):
                    names.add(node.name)
    return names


def test_every_enforced_clause_cites_a_test_file_that_exists():
    problems: list[str] = []
    for clause, level, enforced in _levels_and_citations():
        files = FILE_REF.findall(enforced)
        # Only a purely P/E clause is REQUIRED to cite a test. But a mixed cell
        # ("E for the mechanisms; B against ...") cites tests for its enforced half,
        # and those citations rot the same way — so whatever any row names is
        # checked, whether or not the row had to name it.
        if not files and _proven_or_enforced(level):
            problems.append(
                f"{clause} is at level {level!r} but its third column names no test file"
            )
            continue
        for filename, _node in files:
            if not (TESTS / filename).exists() and not list(TESTS.rglob(filename)):
                problems.append(f"{clause} cites {filename}, which does not exist")
    assert not problems, "\n  ".join([""] + problems)


def test_every_cited_test_node_exists():
    """A `file::node` or a backticked `test_name` has to name a real function.

    This is the half that catches a rename: the file survives, so a file-existence
    check passes, and the specific property the clause pointed at is gone."""
    defined = _defined_test_functions()
    problems: list[str] = []
    for clause, _level, enforced in _levels_and_citations():
        cited = {n for _f, n in FILE_REF.findall(enforced) if n}
        cited |= set(NODE_REF.findall(enforced))
        for node in cited:
            if node not in defined:
                problems.append(
                    f"{clause} cites the test {node!r}, which is not defined anywhere "
                    "under backend/tests"
                )
    assert not problems, "\n  ".join([""] + problems)


def test_the_check_would_notice_a_dangling_citation():
    """The guard on the guard. Both tests above pass trivially if the row parser
    stops matching rows — a markdown reformat would silently disable them — so pin
    that a realistic number of clauses is being read, and that a fabricated
    citation is rejected."""
    rows = list(_levels_and_citations())
    assert len(rows) >= 30, f"only {len(rows)} clause rows parsed; the row regex has drifted"
    enforced = [r for r in rows if _proven_or_enforced(r[1])]
    assert len(enforced) >= 15, f"only {len(enforced)} P/E clauses found among {len(rows)}"
    # Assembled rather than written out: a literal name here would itself be a
    # citation, and `test_doc_references_resolve.py` correctly fails a citation to a
    # test that does not exist. A guard that has to lie to test itself is a guard
    # worth rewriting.
    fabricated = "test_" + "a_name_no_suite_" + "defines"
    assert fabricated not in _defined_test_functions()
