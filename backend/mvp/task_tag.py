"""Task tag: a caller-asserted label carried on a request so usage can be
grouped by the work it was for.

The ONE declaration: nothing outside this module canonicalises or validates a
tag (a second canonicaliser is exactly the drift a later grouping-by-tag PR
would inherit). `resolve` is deliberately not built on top of
`observability.context._validate` -- that function RAISES on a malformed
value (it is what turns a bad correlation header into a 400) -- because a tag
must never be able to refuse a request: there is no correct place to sequence
a refusable label against money (before the reserve it would mask the 402
that names the grantable wall; after it, a refusal would strand debited
counters). `resolve` is a total function with no raising path. `GRAMMAR` is
reused from that module (the same compiled pattern object, not a recompiled
copy) because a pattern is inert data, not something that can refuse.

`SENTINEL` ("unlabelled") is what an untagged request records, and it is
reserved: an asserted tag that canonicalises onto it (any casing of
"unlabelled") is grammar-dropped rather than accepted, so genuinely untagged
spend can never be confused with a caller who typed the sentinel's spelling.
"""
from __future__ import annotations

import unicodedata
from enum import Enum
from typing import Optional

# Reused, not copied: the same correlation-id grammar `observability.context`
# validates client headers with (bounded length, DynamoDB-key-safe characters).
# A tag never uses `_validate` itself -- see the module docstring.
from .observability.context import _ID_GRAMMAR as GRAMMAR

#: The header a client asserts a task tag under.
HDR_TASK_TAG = "x-sc-task-tag"

#: Response header: present only when the request's tag was dropped, naming
#: why (``dropped_reason``'s return value). A tag still cannot refuse a
#: request -- this is informational, never a status -- but the drop is no
#: longer silent to a caller that reads it.
HDR_TASK_TAG_DROPPED = "x-sc-task-tag-dropped"

#: The recorded value when no tag was asserted. Reserved -- see module docstring.
SENTINEL: str = "unlabelled"

#: Longest canonical tag `resolve` will assert. Named separately from GRAMMAR
#: so a reader of `resolve` sees the bound without reading the pattern.
MAX_LEN: int = 64


class Source(str, Enum):
    """How the `task_tag` on a record came to have its value.

    Two of these four values mean "dropped", for two different reasons a
    caller cannot otherwise distinguish: `DROPPED_RESERVED` (the caller's
    tag canonicalised onto the reserved sentinel itself -- a project
    genuinely named "unlabelled") and `DROPPED_GRAMMAR` (anything else
    unusable -- too long, or outside the grammar). `dropped_reason` maps
    each to the caller-facing word (`"reserved"` / `"grammar"`) carried on
    `HDR_TASK_TAG_DROPPED`. `dynamo.usage_logs.aggregate_by_tag` folds BOTH
    into its single `dropped_grammar_count`: that count answers "did a real
    assertion get thrown away", and the two reasons are the same answer to
    that question, differing only in the response header a caller sees.
    """

    ABSENT = "absent"                    # header absent, or present and empty
    ASSERTED = "asserted"                 # grammar-valid, canonicalised
    DROPPED_RESERVED = "dropped_reserved"  # canonical form IS the sentinel
    DROPPED_GRAMMAR = "dropped_grammar"   # present, non-empty, and otherwise not usable


def dropped_reason(source: Source) -> Optional[str]:
    """The caller-facing reason for ``HDR_TASK_TAG_DROPPED``, or `None` when
    the tag was not dropped (``ABSENT`` and ``ASSERTED`` both resolve to `None`
    -- absence is not a drop, and a client never needs telling its own tag
    stuck)."""
    if source is Source.DROPPED_RESERVED:
        return "reserved"
    if source is Source.DROPPED_GRAMMAR:
        return "grammar"
    return None


def canonical(raw: str) -> str:
    """Canonical form of a tag: NFKC, then casefold, then strip.

    Idempotent -- ``canonical(canonical(x)) == canonical(x)`` -- so a value
    that has already been canonicalised (e.g. read back off a stored record)
    normalises to itself.
    """
    return unicodedata.normalize("NFKC", raw).casefold().strip()


def resolve(raw: Optional[str]) -> tuple[str, Source]:
    """Resolve a raw ``x-sc-task-tag`` header value to ``(task_tag, source)``.

    Total: never raises, for any ``str | None`` input, including control
    characters and arbitrarily long strings (``canonical`` hands the value to
    ``unicodedata.normalize``, which requires a ``str``). ``None`` or a value
    that canonicalises to blank is ``(SENTINEL, ABSENT)`` -- empty is absent,
    the same reading ``observability.context`` gives the correlation headers.
    A canonical form that IS the reserved sentinel is
    ``(SENTINEL, DROPPED_RESERVED)``; one that is over ``MAX_LEN`` or falls
    outside ``GRAMMAR`` is ``(SENTINEL, DROPPED_GRAMMAR)`` -- either way,
    present but unusable, never a refusal. Anything else is
    ``(canonical(raw), ASSERTED)``.
    """
    if raw is None:
        return SENTINEL, Source.ABSENT
    c = canonical(raw)
    if c == "":
        return SENTINEL, Source.ABSENT
    if c == SENTINEL:
        return SENTINEL, Source.DROPPED_RESERVED
    if len(c) > MAX_LEN or not GRAMMAR.match(c):
        return SENTINEL, Source.DROPPED_GRAMMAR
    return c, Source.ASSERTED
