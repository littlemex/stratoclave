"""The durable rung of the ladder: a version on change, and nothing on a no-op.

`mvp.pricing` already keeps the last table a source returned, so a blip does not drop a
task to the bundled floor. That memory is process-local, which means a deploy, a
scale-out or an OOM restart lands a fresh task with no record that better numbers were
ever known — and the floor is a document from whenever the release was cut. This module
is the missing rung:

    admin override  >  a fresh fetch  >  the current stored version  >  the bundled floor

**A version is cut only when a price actually moves.** An hourly refresh that reads the
same numbers writes no version: polling must not turn into a pile of daily rows, and
"how many versions exist" should answer "how many times prices changed" rather than
"how many days has this been running". Mechanically the version id IS the digest of the
table, each version row is written once under `attribute_not_exists` — so an unchanged
table is a no-op even when several tasks fetch in the same second — and one small
pointer row says which version is current and when it was last confirmed:

    pk = CONFIG#pricefeed, sk = CURRENT              -> {active_version, first_seen_at,
                                                         last_seen_at, previous_version}
    pk = CONFIG#pricefeed, sk = __ratefeed__<digest>  -> {rates, provenance, live_classes,
                                                         created_at}

That is the same shape as the admin override rows beside it (immutable versions plus a
`CURRENT` pointer), for the same reason: an immutable version is what a dispute can be
answered against, and a pointer is what a reader needs.

The history is not decoration. Token counts are the record of origin, so what a period
would have cost under any version this store holds is a recomputation rather than a
guess — see `mvp.reprice`.

Failure is always downward, never sideways: an unreadable, unwritable or unrecognised
version means the rung is skipped, not that charging stops.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Mapping, Optional

from core.logging import get_logger

from ..rates import RATE_FIELDS, Rate

logger = get_logger(__name__)

_PK = "CONFIG#pricefeed"
_POINTER_SK = "CURRENT"
_VERSION_SK_PREFIX = "__ratefeed__"
SCHEMA_VERSION = 1

# How stale the current version may be before it is REPORTED as stale. Not an expiry:
# expiring a version would change the amount charged with nobody deciding to, which is
# the one thing this subsystem must not do quietly. It also bounds how often the pointer
# is touched when nothing changes — `last_seen_at` is refreshed at most once per half
# window, so a fleet of tasks confirming the same prices writes almost nothing.
STALE_AFTER_SECONDS_ENV = "STRATOCLAVE_PRICE_FEED_STALE_AFTER_SECONDS"
DEFAULT_STALE_AFTER_SECONDS = 24 * 3600


def _version_sk(version: str) -> str:
    return f"{_VERSION_SK_PREFIX}{version}"


@dataclass(frozen=True)
class Snapshot:
    """One stored version, with the facts needed to judge it."""

    rates: dict[str, Rate]
    provenance: dict[str, str] = field(default_factory=dict)
    # When these numbers were FIRST seen, not when they were last confirmed. A price
    # that has not moved for a month is a month old and still current; conflating the
    # two would make a stable table look stale.
    fetched_at: float = 0.0
    digest: str = ""
    # pricing key -> the token classes a feed actually published for it. Stored because a
    # leg can stop being readable while the key stays present, and then the stored value
    # is silently re-published forever: a promotional cache-write rate that was live in
    # August keeps being charged in November with no event to see. Comparing this set
    # across fetches is what makes that visible.
    live_classes: dict[str, frozenset[str]] = field(default_factory=dict)
    # When this version was last confirmed to still be what the provider publishes.
    last_seen_at: float = 0.0
    # Whether `digest_of(rates)` matched the version id this row was read under. True for
    # every snapshot this module builds itself (the digest and the rates come from the same
    # write), so the only place this is ever `False` is a version read back with a corrupt
    # row dropped — the table can then differ from the name it is stored under, and this is
    # what lets a dispute tool refuse it while the charging path keeps serving the rest.
    digest_verified: bool = True

    @property
    def age_seconds(self) -> float:
        confirmed = self.last_seen_at or self.fetched_at
        return max(0.0, time.time() - confirmed) if confirmed else float("inf")

    def is_stale(self, *, now: Optional[float] = None) -> bool:
        confirmed = self.last_seen_at or self.fetched_at
        limit = stale_after_seconds()
        age = max(0.0, (now or time.time()) - confirmed) if confirmed else float("inf")
        return age > limit


def stale_after_seconds() -> float:
    raw = os.getenv(STALE_AFTER_SECONDS_ENV)
    if not raw:
        return float(DEFAULT_STALE_AFTER_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_STALE_AFTER_SECONDS)
    return value if value > 0 else float(DEFAULT_STALE_AFTER_SECONDS)


def digest_of(rates: Mapping[str, Rate]) -> str:
    """A stable digest of a rate table, used as its version id.

    Content-addressed on purpose: it makes "cut a version only when something changed" a
    property of the write rather than a comparison someone has to remember, and it makes
    a version id mean the same thing in every deployment. It is also the only change
    signal available — AWS publishes no end date for a promotional price and every
    Bedrock `effectiveDate` reads as the first of the current month, so a difference
    between two fetches is what a price change looks like from outside.
    """
    import hashlib

    parts = []
    for key in sorted(rates):
        rate = rates[key]
        parts.append(key + ":" + ",".join(str(getattr(rate, f)) for f in RATE_FIELDS))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


class SnapshotStore:
    """DynamoDB-backed store. Constructed lazily so import never needs credentials."""

    def __init__(self, table_name: Optional[str] = None) -> None:
        self._table_name = table_name
        self._table = None

    def _get_table(self):
        if self._table is None:
            from dynamo.client import get_dynamodb_resource, pricing_config_table_name

            self._table = get_dynamodb_resource().Table(
                self._table_name or pricing_config_table_name()
            )
        return self._table

    # ----- read ---------------------------------------------------------------
    def load(self) -> Optional[Snapshot]:
        """The current version, or `None` when there is not one to read."""
        try:
            # Strong reads on BOTH steps. The pointer is written after the version row, so
            # an eventually-consistent pair can show a reader the new pointer and not yet
            # the version it names — and this module's answer to a missing version row is
            # "no stored version", which drops the whole table to the floor. A read that
            # can invent that state is not a read worth having.
            pointer = (self._get_table()
                       .get_item(Key={"pk": _PK, "sk": _POINTER_SK}, ConsistentRead=True)
                       .get("Item"))
        except Exception as exc:  # noqa: BLE001 — no stored version is a valid state.
            logger.warning("price_feed_snapshot_read_failed", error=str(exc))
            return None
        if not pointer:
            return None
        version = pointer.get("active_version")
        if not isinstance(version, str) or not version:
            logger.warning("price_feed_pointer_malformed")
            return None
        return self.load_version(version, pointer=pointer)

    def load_version(self, version: str,
                     pointer: Optional[Mapping] = None) -> Optional[Snapshot]:
        """A specific version, for a recompute against a table that is no longer current."""
        try:
            item = (self._get_table()
                    .get_item(Key={"pk": _PK, "sk": _version_sk(version)},
                              ConsistentRead=True)
                    .get("Item"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("price_feed_snapshot_read_failed", error=str(exc),
                           version=version)
            return None
        if not item:
            logger.warning("price_feed_version_missing", version=version)
            return None
        return _parse_version(item, version=version, pointer=pointer)

    def versions(self, limit: int = 50) -> list[dict]:
        """Version ids with their timestamps, newest first. For the ops CLI.

        Short by construction: a version exists per price CHANGE, not per fetch, so this
        is the history of what moved rather than a log of every poll.

        `Limit` is a DynamoDB page size, not a result count, and DynamoDB applies it in
        sort-key order — the digest, which carries no relationship to time — before this
        method ever sees the items. Passing `limit` straight through as `Limit` can hand
        back an arbitrary old page and drop the version a dispute actually wants, so the
        partition is paged in full first and `limit` is applied only after sorting by
        `created_at`.
        """
        from boto3.dynamodb.conditions import Key

        items: list[dict] = []
        key_condition = Key("pk").eq(_PK) & Key("sk").begins_with(_VERSION_SK_PREFIX)
        exclusive_start_key = None
        try:
            while True:
                kwargs: dict = {"KeyConditionExpression": key_condition}
                if exclusive_start_key is not None:
                    kwargs["ExclusiveStartKey"] = exclusive_start_key
                response = self._get_table().query(**kwargs)
                items.extend(response.get("Items", ()))
                exclusive_start_key = response.get("LastEvaluatedKey")
                if not exclusive_start_key:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.warning("price_feed_versions_read_failed", error=str(exc))
            return []
        out = []
        for item in items:
            sk = str(item.get("sk") or "")
            out.append({
                "version": sk[len(_VERSION_SK_PREFIX):],
                "created_at": int(item.get("created_at") or 0),
                "keys": len(item.get("rates") or {}),
            })
        out.sort(key=lambda row: row["created_at"], reverse=True)
        return out[:max(1, limit)]

    # ----- write --------------------------------------------------------------
    def save(self, rates: Mapping[str, Rate], provenance: Mapping[str, str],
             live_classes: Optional[Mapping[str, frozenset[str]]] = None,
             *, now: Optional[float] = None,
             fenced_on: Optional[str]) -> Optional[Snapshot]:
        """Store `rates`, cutting a version only if these numbers are new.

        `fenced_on` is required and keyword-only: every caller states, at the call site,
        what it read at the pointer before fetching. A digest is the active version this
        pass STARTED from — its fence — and the pointer moves only if that is still the
        active version, which makes ordering a fact about the data rather than about
        clocks: a pass that began before another finished finds the pointer moved and
        steps aside. `None` means the caller read the pointer and found no active version
        yet — the fleet's first pass — and fences on exactly that: the write only lands if
        the pointer still has none, so two tasks cold-starting together cannot both win the
        first version. Making the parameter required rather than defaulted is the fix: a
        default is how the fence went unused everywhere in production while every test of
        it passed.

        Returns the stored snapshot — newly cut or already there — or `None` on failure or
        on a lost fence, and never raises: a missing table name, a missing region and a
        `ClientError` all return `None`. Usually zero or one write, never more than three:

        1. the version row, `attribute_not_exists`-guarded, so an unchanged table costs
           nothing and two tasks racing in the same second cannot both create it;
        2. the pointer, only when the active version actually moves;
        3. a `last_seen_at` touch on the pointer, at most once per half staleness window,
           so "still what the provider publishes" is recorded without a row per poll.

        Refuses an empty table: "the feed returned nothing" is precisely the state this
        store exists to survive, so writing it would erase what it is meant to preserve.
        """
        if not rates:
            logger.warning("price_feed_snapshot_write_skipped", reason="empty table")
            return None
        moment = now if now is not None else time.time()
        version = digest_of(rates)
        snapshot = Snapshot(
            rates=dict(rates),
            provenance=dict(provenance),
            fetched_at=moment,
            digest=version,
            live_classes={k: frozenset(v) for k, v in (live_classes or {}).items()},
            last_seen_at=moment,
        )
        try:
            table = self._get_table()
        except Exception as exc:  # noqa: BLE001 — a store failure skips the rung, it does
            # not raise through `load()` on the next read: `_get_table()` used to sit
            # outside this try, so a mis-deployed task (no table name, no region) raised
            # out of a successful fetch instead of just losing the write.
            logger.warning("price_feed_snapshot_table_unavailable", error=str(exc))
            return None
        created = self._put_version_if_new(table, snapshot, moment)
        if created is None:
            return None
        # Exactly what the pointer says, never the local table. Returning the caller's own
        # numbers after a refused pointer move would tell it that what it fetched is
        # current when something newer is — and the caller adopts this as its stored rung.
        return self._advance_pointer(table, snapshot, moment, version_is_new=created,
                                     fenced_on=fenced_on)

    def _put_version_if_new(self, table, snapshot: Snapshot,
                            moment: float) -> Optional[bool]:
        """Write the version row unless it exists. `None` means the write failed."""
        item = {
            "pk": _PK,
            "sk": _version_sk(snapshot.digest),
            "schema_version": SCHEMA_VERSION,
            "created_at": int(moment),
            "rates": {
                key: {f: int(getattr(rate, f)) for f in RATE_FIELDS}
                for key, rate in snapshot.rates.items()
            },
            "provenance": dict(snapshot.provenance),
            "live_classes": {k: sorted(v) for k, v in snapshot.live_classes.items()},
        }
        try:
            table.put_item(Item=item, ConditionExpression="attribute_not_exists(sk)")
        except Exception as exc:  # noqa: BLE001
            if _is_conditional_failure(exc):
                # These exact numbers are already stored: the expected outcome of every
                # refresh that finds prices unchanged, and the reason polling does not
                # accumulate rows.
                return False
            logger.warning("price_feed_version_write_failed", error=str(exc),
                           version=snapshot.digest)
            return None
        logger.info("price_feed_version_cut", version=snapshot.digest,
                    keys=len(snapshot.rates))
        return True

    def _advance_pointer(self, table, snapshot: Snapshot, moment: float,
                         *, version_is_new: bool,
                         fenced_on: Optional[str]) -> Optional[Snapshot]:
        current = None
        try:
            current = table.get_item(Key={"pk": _PK, "sk": _POINTER_SK},
                                     ConsistentRead=True).get("Item")
        except Exception as exc:  # noqa: BLE001 — treated as "no pointer yet".
            logger.warning("price_feed_pointer_read_failed", error=str(exc))
        active = (current or {}).get("active_version")
        if active == snapshot.digest:
            first_seen = _as_float((current or {}).get("first_seen_at"),
                                   snapshot.fetched_at)
            last_seen = _as_float((current or {}).get("last_seen_at"), 0.0)
            if moment - last_seen > stale_after_seconds() / 2:
                self._touch(table, moment)
                last_seen = moment
            return Snapshot(rates=snapshot.rates, provenance=snapshot.provenance,
                            fetched_at=first_seen, digest=snapshot.digest,
                            live_classes=snapshot.live_classes, last_seen_at=last_seen)
        item = {
            "pk": _PK,
            "sk": _POINTER_SK,
            "schema_version": SCHEMA_VERSION,
            "active_version": snapshot.digest,
            "first_seen_at": int(moment),
            "last_seen_at": int(moment),
        }
        if isinstance(active, str) and active:
            item["previous_version"] = active
        # Compare-and-set on the version this pass STARTED from, not on the one read a
        # microsecond ago. An unconditional put lets two tasks race and the last writer win
        # regardless of which fetch was newer, which is how a stale pass re-points CURRENT at
        # yesterday's prices; a CAS against a just-read value has the same hole, because the
        # stale writer reads the winner's version and then satisfies its own condition.
        # Fencing on the pass's starting point closes it without asking a client clock to be
        # right — a future-dated write would otherwise lock the pointer until the world
        # caught up. `fenced_on is None` is still a fence, not an escape from one: it is the
        # caller that read the pointer and found no active version, so the write must land
        # only if that is still true — an unconditional put here would let two tasks
        # cold-starting together both win the very first version, reopening this same hole
        # at the one moment there is no prior version to fence on.
        try:
            if fenced_on is None:
                table.put_item(Item=item,
                               ConditionExpression="attribute_not_exists(active_version)")
            else:
                table.put_item(Item=item, ConditionExpression="active_version = :expected",
                               ExpressionAttributeValues={":expected": fenced_on})
        except Exception as exc:  # noqa: BLE001
            if _is_conditional_failure(exc):
                # Another pass moved the pointer since this one started. Its version row is
                # stored either way, so nothing is lost; the loser steps aside rather than
                # overwrite a decision that may rest on a newer fetch than its own.
                logger.info("price_feed_pointer_race_lost", version=snapshot.digest,
                            fenced_on=fenced_on, active_now=active)
                return None
            # The version row is stored, so a task that restarts reads the OLD pointer: a
            # real table, not a wrong one.
            logger.warning("price_feed_pointer_write_failed", error=str(exc))
            return None
        logger.info("price_feed_version_activated", version=snapshot.digest,
                    previous=active, was_new=version_is_new)
        return snapshot

    def _touch(self, table, moment: float) -> None:
        try:
            table.update_item(
                Key={"pk": _PK, "sk": _POINTER_SK},
                UpdateExpression="SET last_seen_at = :now",
                ExpressionAttributeValues={":now": int(moment)},
            )
        except Exception as exc:  # noqa: BLE001 — a missed touch only mislabels staleness.
            logger.warning("price_feed_pointer_touch_failed", error=str(exc))


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if code == "ConditionalCheckFailedException":
            return True
    return "ConditionalCheckFailed" in str(exc)


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_version(item: Mapping, *, version: str,
                   pointer: Optional[Mapping] = None) -> Optional[Snapshot]:
    schema = item.get("schema_version")
    try:
        schema = int(schema)
    except (TypeError, ValueError):
        schema = None
    if schema != SCHEMA_VERSION:
        # A schema this build does not know could mean anything, including that a field
        # changed meaning. Skipping the rung charges the floor, which is wrong-but-known;
        # guessing charges a number nobody can explain.
        logger.warning("price_feed_snapshot_schema_unknown",
                       schema_version=item.get("schema_version"), version=version)
        return None
    raw_rates = item.get("rates")
    if not isinstance(raw_rates, Mapping) or not raw_rates:
        logger.warning("price_feed_snapshot_malformed", reason="rates missing or empty",
                       version=version)
        return None
    rates: dict[str, Rate] = {}
    for key, row in raw_rates.items():
        if not isinstance(key, str) or not key or not isinstance(row, Mapping):
            logger.warning("price_feed_snapshot_row_skipped", key=str(key)[:60])
            continue
        values: dict[str, int] = {}
        ok = True
        for field_name in RATE_FIELDS:
            value = row.get(field_name)
            if isinstance(value, bool) or value is None:
                ok = False
                break
            try:
                ivalue = int(value)
            except (TypeError, ValueError):
                ok = False
                break
            if ivalue < 0:
                ok = False
                break
            values[field_name] = ivalue
        if not ok:
            # One bad row is dropped rather than failing the whole version: the other
            # keys are still better than the floor, and the dropped key falls back to it.
            logger.warning("price_feed_snapshot_row_skipped", key=key[:60])
            continue
        rates[key] = Rate(**values)
    if not rates:
        return None
    provenance = {
        str(k): str(v) for k, v in (item.get("provenance") or {}).items()
        if isinstance(k, str)
    }
    live_classes: dict[str, frozenset[str]] = {}
    for key, classes in (item.get("live_classes") or {}).items():
        if isinstance(key, str) and isinstance(classes, (list, tuple, set)):
            live_classes[key] = frozenset(str(c) for c in classes)
    created_at = _as_float(item.get("created_at"), 0.0)
    first_seen = _as_float((pointer or {}).get("first_seen_at"), created_at)
    last_seen = _as_float((pointer or {}).get("last_seen_at"), created_at)
    # The version id IS the digest of the table it names (see `digest_of`), so recomputing
    # it here is the check that the row read back is the row that was written — a dropped
    # or altered field above already changed `rates` without anyone deciding to. A mismatch
    # does not fail the read: charging may still serve the surviving keys, but a dispute
    # tool answering from this table needs to be able to refuse it, which is what the flag
    # is for.
    verified = digest_of(rates) == version
    if not verified:
        logger.warning("price_feed_snapshot_digest_mismatched", version=version)
    return Snapshot(rates=rates, provenance=provenance, fetched_at=first_seen,
                    digest=version, live_classes=live_classes, last_seen_at=last_seen,
                    digest_verified=verified)
