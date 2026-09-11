"""E5 — the pricing key: a content hash of the selector's output.

Discovery does not compute its own projection: `dimensions.select()` already
prices each token class at the maximum over a set of candidate regions and a
scope, and `key_for_selection` below only ever hashes what THAT function
returned. There is no second selector here.

**A hash proves the selection is stable, never that it is right.** A freshly
minted, internally consistent *cheap* key passes content-addressing perfectly —
nothing here checks the number against a floor, because there is no floor to
check it against yet. That check is PR3's, at grant time; this module's only
claim is "the same inputs mint the same key", which is what lets two
reconciliation passes agree a price has not moved without either of them
re-deriving what it should be.

Superset outcomes, in the direction a rate card actually grows over time:

- A dimension that resolves OUTSIDE `selection.rates` — `dimensions.select()`
  already reports these as `absent` or `widened`, never as a member of
  `rates` — leaves the key unchanged, because the hash below is only ever
  taken over `selection.rates`. A long-context row, a non-`standard`-mode row,
  or a class this provider does not price at all cannot move a key that never
  looked at it.
- A dimension that resolves INSIDE `selection.rates` for the first time (a
  provider starts publishing a cache leg it did not before) mints a NEW key —
  the hash changes because its input changed — and never widens the OLD key
  in place. A pricing key names a specific, closed set of legs; growing that
  set is a different key, not a mutation of the old one's meaning, because
  anything already priced under the old key was priced under ITS set of legs,
  and rewriting that meaning after the fact would be re-describing a price
  that already happened.
- A dimension that CANNOT BE CLASSIFIED — a rate-card row
  `pricing_feeds.dimensions.parse_agreement_dimension` answers `None` for,
  rather than `EXCLUDED` or a resolved slot — is not something `Selection`
  carries any memory of: `dimensions.select()` only ever sees the rows that
  DID parse, so a `Selection` built from a card with an unparseable row looks
  identical to one built from a fully-understood card missing that row
  entirely. **This module therefore never sees that case; the caller does.**
  `mvp.discovery.reconcile` parses every row of a model's rate card before it
  ever builds a `Selection`, and when any row comes back unparseable it
  records a `price_dimensions_unknown` blocker and never calls
  `key_for_selection` for that pass — a key minted from an incompletely-read
  card could be missing a leg that turns out to be cheaper OR dearer, and
  "maybe" is not a fact this module is willing to hash.

`enabled_modes` is folded into the hash alongside the version prefix, not read
by the hashing logic to filter anything: `select()` already hardcodes "only
`standard`" today, so two calls under today's policy always agree on
`enabled_modes`. The parameter exists so that WHEN that policy changes — batch
or flex becomes billable — a `Selection` computed under the new policy cannot
collide with a key minted under the old one even if, by coincidence, the two
happen to select the same rates dict; the version prefix guards against the
hashING rule changing, `enabled_modes` guards against the SELECTION policy
changing underneath an unchanged hash rule.
"""
from __future__ import annotations

import hashlib
from typing import Iterable

from ..pricing_feeds.dimensions import Selection

#: Bumped whenever the hashing RULE below changes (which fields are hashed, in
#: what order, with what separator) — never when the priced numbers change.
#: Exists so two keys minted under different rules cannot collide silently;
#: see the module docstring.
KEY_VERSION = "v1"


def key_for_selection(
    selection: Selection, *, enabled_modes: Iterable[str] = ("standard",),
) -> str:
    """A stable, content-addressed key for `selection` — `dimensions.select()`'s
    own output for one discovered model against its candidate regions and scope.

    Hashed over the sorted `(token_class, price)` pairs of `selection.rates`
    ONLY: `selection.absent` and `selection.widened` name classes the selector
    could not resolve exactly or at all, and neither changes what this model is
    actually priced at, so neither belongs in a key that is supposed to answer
    "did the price change". `enabled_modes` is sorted and deduplicated before
    hashing so that `{"standard", "batch"}` and `{"batch", "standard"}` are
    always the same key.
    """
    modes = ",".join(sorted(set(enabled_modes)))
    parts = [f"modes:{modes}"]
    for token_class in sorted(selection.rates):
        parts.append(f"{token_class}:{selection.rates[token_class]}")
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{KEY_VERSION}:{digest}"
