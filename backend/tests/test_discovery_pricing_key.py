"""The discovery pricing key: version-prefixed, and the three superset outcomes.

Every assertion calls the production `mvp.discovery.pricing_key.key_for_selection`
directly — never a local reimplementation of the hashing rule — and every input
is a `mvp.pricing_feeds.dimensions.Selection`, the real, already-tested return
value of `dimensions.select()` (see `test_pricing_feeds_dimensions.py`), not a
hand-rolled stand-in for it. This module is explicitly told not to compute its
own projection, so these tests never call `select()` themselves either; they
construct the `Selection` values `select()` would have produced and hand them
straight to the key function, which is the whole of what this module owns.

**What a passing test here does NOT prove.** The handoff's own words: "a hash
proves the selection is stable, never that it is right". A `Selection` built
with a cheap, wrong rate hashes into a perfectly stable, internally consistent
key — nothing below catches that, and nothing below claims to; the independent
check is the floor comparison a later PR adds at grant time. What IS pinned
here is narrower and mechanical: same rates in, same key out; different rates
in, a different key out.

One place where the interface admits more than one reading, resolved by
picking a concrete, falsifiable contract rather than a permissive one — see
the docstring on `test_a_dimension_outside_the_selection_leaves_the_key_unchanged`
for the reasoning about what "outside the selection" can mean once the input
is already `select()`'s reduced output rather than a raw rate card.

**A test that was here and is not any more.** An earlier version of this file
also asserted `key_for_selection(None, ...)` returns a `price_dimensions_unknown`
blocker rather than a key, reasoning that `select()`'s own refusal (`None`) is
the only input this function could ever see that is "unclassifiable" — a real
`Selection` is classified by construction. That reasoning about the TYPE is
still correct, but it described a call that does not happen: the settled
design mints `price_dimensions_unknown` in the reconciliation itself, when a
rate-card row fails to parse, because a `Selection` carries no memory of which
raw rows fed it and so cannot be the place that notices one was unreadable.
`key_for_selection` is never called with `None` on the real path. Pinning that
call would have pinned a contract the function does not have, so it is
removed rather than kept as a defensive extra; the "cannot be classified"
outcome is exercised in `test_discovery_reconcile.py` instead, against the
component that actually produces it.
"""
from __future__ import annotations

from decimal import Decimal

from mvp.discovery.pricing_key import key_for_selection
from mvp.pricing_feeds.dimensions import Selection


def _selection(rates: dict, *, absent: frozenset = frozenset(),
              widened: frozenset = frozenset()) -> Selection:
    return Selection(rates={k: Decimal(v) for k, v in rates.items()},
                     absent=absent, widened=widened)


# --- the version prefix -------------------------------------------------------
def test_key_carries_a_version_marker_not_a_bare_digest():
    """"a content hash... with a version prefix, so the hashing rule can change
    without silently colliding with keys minted under the old one." A bare
    digest cannot do that: nothing distinguishes a key minted under rule 1 from
    one minted under rule 2 if the bytes hashed happen to coincide, so a future
    change to the hashing rule would silently collide with history — the exact
    failure the version prefix exists to prevent.

    Checked structurally rather than against one literal spelling, since the
    handoff does not fix the separator or the version's format: any bare hex
    digest (sha256's 64 lowercase hex characters, sha1's 40, md5's 32, or any
    length in between) would fail this — the failing case this test exists to
    catch — while a key carrying any recognisable prefix segment passes it
    regardless of which prefix scheme the implementer chose.
    """
    key = key_for_selection(
        _selection({"input": "1", "output": "2"}), enabled_modes=("standard",))
    assert isinstance(key, str) and key
    assert any(c not in "0123456789abcdefABCDEF" for c in key), (
        f"key {key!r} contains nothing but hex characters — it looks like a bare "
        f"digest with no version prefix, which is exactly what would let a future "
        f"hashing-rule change collide silently with keys minted under this one"
    )


def test_key_is_deterministic_for_the_same_selection():
    """The baseline a content hash has to meet before "version-prefixed" or
    "superset" mean anything: calling it twice on equal input must not itself
    be a source of a new key."""
    sel = _selection({"input": "5.5", "output": "27.5"})
    assert (key_for_selection(sel, enabled_modes=("standard",))
            == key_for_selection(sel, enabled_modes=("standard",)))


def test_enabled_modes_is_not_a_decoration():
    """`enabled_modes` is a real keyword argument on the production function,
    not a name the handoff mentions without using — CONTRACT-level material
    elsewhere in this change treats `batch` as "a live price difference" on the
    same underlying card, so a function that accepted the parameter and then
    ignored it would silently collapse a standard-tier and a batch-tier key
    together the day batch pricing is wired up. Caught now, at the one point
    where it is cheap to catch, by requiring the parameter to actually
    participate in the hash."""
    sel = _selection({"input": "1", "output": "2"})
    assert (key_for_selection(sel, enabled_modes=("standard",))
            != key_for_selection(sel, enabled_modes=("standard", "batch")))


# --- outcome 1: outside the selection -----------------------------------------
def test_a_dimension_outside_the_selection_leaves_the_key_unchanged():
    """"a dimension appearing outside the selection leaves the key unchanged."
    By the time a value reaches this function it is already `select()`'s
    reduced output — `rates`, `absent`, `widened` — so "outside the selection"
    can only mean a change that does not move `.rates`: `select()` itself
    already decides, for every class, one committed number or none, and
    anything else about WHY that number was chosen (in-scope exactly, or
    widened from a dearer region because the in-scope leg was unavailable) is
    metadata the resolved rate does not depend on.

    `widened` is exactly that metadata. A leg that resolves to the identical
    price whether it was priced in-scope or had to be widened from elsewhere
    is, from this function's point of view, the same fact reappearing under a
    different `widened` flag — the underlying card changed shape (a region
    came or went) while the number this gateway will actually charge did not
    move, which is precisely "outside the selection". Two `Selection`s that
    agree on every `(token_class, price)` pair and differ only in `widened`
    must therefore mint the same key.
    """
    same_rates = {"input": "5", "output": "25", "cache_read": "0.55"}
    in_scope = _selection(same_rates, widened=frozenset())
    widened_but_same_price = _selection(same_rates, widened=frozenset({"cache_read"}))
    assert (key_for_selection(in_scope, enabled_modes=("standard",))
            == key_for_selection(widened_but_same_price, enabled_modes=("standard",)))


def test_an_absent_class_that_stays_absent_leaves_the_key_unchanged():
    """The same outcome from the other metadata field: two selections that
    agree on every priced class and differ only in which classes are recorded
    `absent` (still nothing) must mint the same key. `absent` names classes
    with NO number at all, so it cannot be part of the `(token_class, price)`
    pairs being hashed by construction — this pins that fact rather than
    assuming it."""
    rates = {"input": "1", "output": "2"}
    a = _selection(rates, absent=frozenset({"cache_read", "cache_write"}))
    b = _selection(rates, absent=frozenset())
    assert (key_for_selection(a, enabled_modes=("standard",))
            == key_for_selection(b, enabled_modes=("standard",)))


# --- outcome 2: inside the selection -------------------------------------------
def test_a_dimension_entering_the_selection_mints_a_new_key():
    """"one appearing inside mints a different key, and never widens an
    existing key's card in place." A class moving from `absent` (no number) to
    priced is the clearest case of "inside": the provider started publishing a
    cache rate that did not exist before, and the set of `(token_class, price)`
    pairs this hashes over is now larger by one pair, so the digest must move."""
    before = _selection({"input": "1", "output": "2"},
                       absent=frozenset({"cache_read"}))
    after = _selection({"input": "1", "output": "2", "cache_read": "0.5"})
    key_before = key_for_selection(before, enabled_modes=("standard",))
    key_after = key_for_selection(after, enabled_modes=("standard",))
    assert key_before != key_after, (
        "a newly-priced dimension did not change the key — this is the failure "
        "'never widens an existing key in place' exists to prevent: an entry "
        "already pointing at the old key would start being billed the new rate "
        "under a name that promised it never would"
    )


def test_a_changed_rate_for_an_already_priced_class_mints_a_new_key():
    """The narrower case of "inside": no class enters or leaves, one value
    moves. Written separately from the entering-class case above because a key
    function that hashed only the SET of priced classes (not their values)
    would pass that test for the wrong reason and would only be caught here."""
    a = _selection({"input": "5.5", "output": "27.5"})
    b = _selection({"input": "6.0", "output": "27.5"})
    assert (key_for_selection(a, enabled_modes=("standard",))
            != key_for_selection(b, enabled_modes=("standard",)))

# outcome 3 ("cannot be classified" -> a `price_dimensions_unknown` blocker
# rather than a key) is settled to be produced by the reconciliation, not by
# this function — see `test_discovery_reconcile.py` and this file's module
# docstring for why a test of `key_for_selection(None, ...)` was removed from
# here rather than kept as a defensive extra.
