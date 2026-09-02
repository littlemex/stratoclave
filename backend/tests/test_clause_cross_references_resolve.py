"""A clause that cites another clause has to cite the RIGHT one, by number and by
what it says.

WHAT DEFECT THIS CLOSES

C14.27's own row read: "a pool-wall refusal is never caught and advanced past
(C14.25's own \"leaves on a pool refusal\")". When the numbering collision in the
surfaces block (F3) was resolved and those clauses were shifted to C14.26-C14.31,
the table row and the anchor pins that rest on it were re-pinned correctly, but the
internal prose reference inside C14.27's OWN row was not: it kept citing C14.25 —
"headroom is one fungible counter", a different clause entirely that carries no
"leaves on a pool refusal" text — instead of C14.26, the clause that actually says
that. Nothing caught it, because `test_claims_are_anchored.py`'s pin mechanism only
re-checks a clause's wording against ANCHORS RESTING ON IT from outside; it has no
opinion about a stale cross-reference sitting inside the clause's own prose.

WHAT THIS FILE CHECKS

Two things, over every clause row in `docs/design/CONTRACTS.md`:

1. Every `Cxx.yy`-shaped token appearing anywhere in a clause row names a clause
   that actually exists in this document. A row citing a retired or renumbered
   clause id fails here rather than silently reading as a real citation.
2. Where a clause reference in a row is immediately followed by a quoted phrase —
   the `Cxx.yy's own "..."` idiom this document uses to attribute specific words to
   another clause — that quoted phrase must actually appear in the CITED clause's
   own row. A citation whose number resolves but whose quoted words do not is the
   exact defect this file exists to catch: the anchor-pin mechanism cannot see it,
   because pinning only watches the wording of the row an anchor points AT, never
   the wording INSIDE a row that itself points elsewhere.

WHAT THIS DOES NOT DO

It cannot tell whether a *paraphrased* (unquoted) cross-reference is still
accurate — only a quoted one, because only a quote makes a checkable claim about
another row's exact words. A rewrite that drops the quotation marks removes the
claim this file can verify, not the risk of being wrong; a human reading both rows
is still required for prose that names another clause without quoting it.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs" / "design" / "CONTRACTS.md"

#: A clause row: `| **C1.2** rest of the row... |`. Kept close to the pattern the
#: other clause-scanning tests use so all three read the same rows the same way.
ROW_START = re.compile(r"^\|\s*\*\*(C\d+(?:\.\d+[a-c]?)?)\*\*")

#: Any Cxx.yy-shaped token, anywhere in a row -- including the row's own
#: self-declaration, which trivially resolves against itself and costs nothing to
#: leave in.
CLAUSE_TOKEN = re.compile(r"\bC\d+(?:\.\d+[a-c]?)?\b")

#: A clause token followed, within a few words, by a quoted phrase -- the
#: "Cxx.yy's own '...'" idiom (and near variants: "Cxx.yy's rule", "per Cxx.yy",
#: with the quote landing shortly after). Deliberately loose on what sits BETWEEN
#: the token and the quote, and deliberately tight on requiring the quote to follow
#: closely -- a quote many words later in the same long row is not a claim about
#: what THAT clause says.
QUOTED_REFERENCE = re.compile(
    r"\b(C\d+(?:\.\d+[a-c]?)?)\b(?:'s)?(?:\s+[A-Za-z][\w-]*){0,4}\s*\"([^\"]{3,200})\""
)


def _clause_rows() -> dict[str, str]:
    """{clause id: the whole row it lives on}, normalised for whitespace only --
    deliberately not for punctuation, since the quoted phrase this file checks is
    itself punctuation-sensitive (it is a literal substring match against the
    referenced row)."""
    rows: dict[str, str] = {}
    for line in CONTRACTS.read_text().split("\n"):
        m = ROW_START.match(line.strip())
        if m:
            rows[m.group(1)] = re.sub(r"\s+", " ", line).strip()
    return rows


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def test_every_clause_cross_reference_names_a_clause_that_exists():
    """A `Cxx.yy` token inside a clause row that names no real clause is a stale
    or typo'd reference -- exactly the shape of the C14.27-cited-C14.25 defect,
    caught at the cheaper "does the number even exist" level before the quoted-
    phrase check below asks the harder question."""
    rows = _clause_rows()
    ids = set(rows)
    problems: list[str] = []
    for cid, row in rows.items():
        for token in CLAUSE_TOKEN.findall(row):
            if token not in ids:
                problems.append(f"{cid} references {token}, which is not a clause "
                                 f"in this document")
    assert not problems, (
        "these clause rows reference a Cxx.yy id that does not exist -- a stale "
        "renumbering or a typo:\n  " + "\n  ".join(problems)
    )


def test_every_quoted_clause_reference_quotes_that_clause_own_words():
    """The C14.27-cited-C14.25 defect, at the level that actually caught it: the
    number can resolve to a real clause while the quoted phrase attributed to it
    belongs to a DIFFERENT clause entirely. This walks every row, finds every
    `Cxx.yy ... "quoted phrase"` idiom, and requires the phrase to be a literal
    substring of the clause it names -- not of the document as a whole, which
    would pass for a quote that is simply true of some OTHER row too."""
    rows = _clause_rows()
    ids = set(rows)
    problems: list[str] = []
    for cid, row in rows.items():
        for ref_id, phrase in QUOTED_REFERENCE.findall(row):
            if ref_id not in ids:
                continue  # already reported by the existence check above
            target_row = _normalize(rows[ref_id])
            if _normalize(phrase) not in target_row:
                problems.append(
                    f'{cid} quotes {ref_id} as saying "{phrase}", but that phrase '
                    f"does not appear in {ref_id}'s own row"
                )
    assert not problems, (
        "these clauses attribute a quoted phrase to another clause that does not "
        "actually contain it -- the exact shape of the C14.27/C14.25 defect this "
        "file exists to catch:\n  " + "\n  ".join(problems)
    )
