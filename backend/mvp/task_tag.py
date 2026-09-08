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

#: The recorded value when no tag was asserted. Reserved -- see module docstring.
SENTINEL: str = "unlabelled"

#: Longest canonical tag `resolve` will assert. Named separately from GRAMMAR
#: so a reader of `resolve` sees the bound without reading the pattern.
MAX_LEN: int = 64


class Source(str, Enum):
    """How the `task_tag` on a record came to have its value."""

    ABSENT = "absent"                    # header absent, or present and empty
    ASSERTED = "asserted"                 # grammar-valid, canonicalised
    DROPPED_GRAMMAR = "dropped_grammar"   # present, non-empty, and not usable


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
    that canonicalises to blank
    is ``(SENTINEL, ABSENT)`` -- empty is absent, the same reading
    ``observability.context`` gives the correlation headers. A canonical form
    that is over ``MAX_LEN``, IS the reserved sentinel, or falls outside
    ``GRAMMAR`` is ``(SENTINEL, DROPPED_GRAMMAR)`` -- present but unusable,
    never a refusal. Anything else is ``(canonical(raw), ASSERTED)``.
    """
    if raw is None:
        return SENTINEL, Source.ABSENT
    c = canonical(raw)
    if c == "":
        return SENTINEL, Source.ABSENT
    if len(c) > MAX_LEN or c == SENTINEL or not GRAMMAR.match(c):
        return SENTINEL, Source.DROPPED_GRAMMAR
    return c, Source.ASSERTED
