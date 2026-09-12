"""The operator's work queue: discovered profiles that need a person.

Separate from the route that serves it, deliberately. The property worth
guarding is that this queue and the reconciliation command's own exit code
never disagree about which blockers matter -- and a test can only compare the
two if the queue is available as data rather than only as an HTTP response.
Filtering inside a route handler would make that comparison impossible to
write, which is the same reason the actionability rule itself lives in one
importable place rather than being restated per surface.

A permanent blocker never appears here. `not_marketplace_metered` is the
normal, forever shape for every AWS-billed family the account can see, so a
queue that included it would be permanently full and therefore unread.
"""
from __future__ import annotations

from typing import Iterable

from .records import Blocker, DiscoveredRecord, list_discovered_records
from .reconcile import is_actionable_blocker


def actionable_blockers_of(record: DiscoveredRecord) -> list[Blocker]:
    """This record's blockers that a person can do something about."""
    return [b for b in record.blockers if is_actionable_blocker(b)]


def list_actionable_blockers(
    records: Iterable[DiscoveredRecord] | None = None,
) -> list[tuple[str, Blocker]]:
    """Every `(profile_id, blocker)` pair an operator should act on.

    Reads the store when `records` is not supplied. A profile with only
    permanent blockers contributes nothing; a profile with both contributes
    only its actionable ones, because the permanent ones are context rather
    than work.
    """
    source = list_discovered_records() if records is None else records
    return [
        (record.profile_id, blocker)
        for record in source
        for blocker in actionable_blockers_of(record)
    ]
