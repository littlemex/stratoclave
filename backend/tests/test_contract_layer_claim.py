"""C13.3 — "ships all five layers" is checked against the table that says so.

The README says, twice, that this gateway now ships all five layers of the canonical
billing-gateway model. It is a completeness claim about the project, so `descriptive:`
would be a fig leaf, and there was no clause to anchor it to. Two options existed: soften
the sentence, or make the claim checkable. This is the second.

The claim's own evidence is the table directly above it, whose third column gives each
layer's status. So the test reads the table: five rows, each marked shipped. A layer
demoted to a roadmap item makes the surrounding prose false, and this is what notices.

What it does NOT establish is that a row marked shipped is shipped. That is the same
residue every clause in this contract carries — a document saying so is not the system
doing so — and it is why the row's status is written where a reader can see it rather than
asserted only in prose.
"""
from __future__ import annotations

import pathlib
import re


README = pathlib.Path(__file__).resolve().parents[2] / "README.md"


def _layer_rows() -> list[tuple[str, str]]:
    """`(layer name, status cell)` for the numbered rows of the five-layer table."""
    rows = []
    for line in README.read_text().split("\n"):
        m = re.match(r"^\|\s*(\d)\.\s*([^|]+?)\s*\|[^|]*\|\s*(.+?)\s*\|\s*$", line)
        if m:
            rows.append((f"{m.group(1)}. {m.group(2)}", m.group(3)))
    return rows


def test_the_five_layer_table_has_five_layers():
    rows = _layer_rows()
    assert len(rows) == 5, (
        f"the README claims all five layers ship, and the table has {len(rows)} rows: "
        f"{[r[0] for r in rows]}"
    )
    assert [r[0][0] for r in rows] == list("12345"), [r[0] for r in rows]


def test_every_layer_row_says_shipped():
    """The sentence "now ships all five layers" is true iff every row says so."""
    not_shipped = [
        name for name, status in _layer_rows()
        if "shipped" not in status.lower()
    ]
    assert not not_shipped, (
        "the README says all five layers ship, and these rows do not: "
        + ", ".join(not_shipped)
    )


def test_a_demoted_layer_would_be_caught():
    """The guard on the guard: the status column is really being read, so a row that
    stopped saying shipped would fail rather than pass unnoticed."""
    statuses = [s for _n, s in _layer_rows()]
    assert statuses, "no status cells parsed; the row regex has drifted"
    assert all(s.strip() for s in statuses)
    # A roadmap marker in this column is what a demotion looks like in this table.
    assert not any(re.search(r"\bP[12]\b|roadmap|not built", s, re.I) for s in statuses)
