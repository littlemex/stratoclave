"""The promotion candidate store: where a discovered record becomes a
*candidate*.

A discovered record is a fact about an account's Bedrock catalogue; a
promoted candidate is a decision that it should be servable. Between the two
sits a verdict a probe has to recompute on every reconciliation pass, and
that verdict needs somewhere to live that is not `defaults/models.json` --
the file a human reviews and a deploy ships, never a place a background
process writes. This module is that somewhere. A candidate is not servable
yet (see the "explicitly not here" list below): it is a row that exists so
the decision to serve it can be made later, by a verdict this module does
not compute.

What lives here today:

- **The store itself** -- `PromotionCandidate`, its own table, and the three
  verbs (`get_promotion_candidate`, `list_promotion_candidates`,
  `put_promotion_candidate`) that read and write it. Uniqueness of every
  public identifier (an alias, or the Bedrock model id) is a database
  constraint, not a check-then-write race: DynamoDB has no unique constraint
  on a non-key attribute, so the candidate row and one reservation row per
  identifier are written together in a single `TransactWriteItems`, each
  reservation guarded by `ConditionExpression="attribute_not_exists(pk)"` —
  the same pattern `mvp.pricing_feeds.snapshot._put_version_if_new` and
  `mvp.admin_routing.provision_shadow_default_config` already use for a
  single conditional write, composed here into several conditional writes
  that commit or fail together. A check against `list_promotion_candidates()`
  followed by a separate write would be exactly the race this shape exists
  to close.
- **The access constraint on the schema** -- `access` is written as
  `"entitlement_required"` in the stored item (never merely assumed by the
  writer), and the read path (`_from_item`, under both read verbs above)
  rejects any stored row whose `access` is missing or says `"general"`
  rather than defaulting it. The dataclass this whole registry is built on
  defaults an entry's `access` to `"general"` -- reachable by every tenant --
  so every gap between what the store holds and what the dataclass assumes
  (a dropped column, an older restored row, a serialisation that omits the
  field) would otherwise fail open onto a world-reachable entry nobody
  chose. See `_from_item` for where this lives and why.
- **The write-time half of the collision check** -- `put_promotion_candidate`
  refuses a candidate whose alias or Bedrock id collides with an identifier
  already public, checked against BOTH the code-resident registry
  (`mvp.models.registry_entries()`) and every existing reservation row in
  this table. The *other* half -- re-validating the composed registry at
  every process start, so a later code deploy that introduces a collision is
  caught too -- is deliberately not here; it has nothing to compose until
  candidates can exist, and it lands with a separate change.

Explicitly NOT here, because it depends on a probe verdict this module does
not have, or belongs to a different author's slice of the same store:
deriving `bedrock_model_id`/`bedrock_region`/`wire_protocol` from a
discovered record and verifying `wire_protocol` against the probe; the three
required human inputs and their refusals; the demotion identity fields and
their completeness check; which identifiers a promotion reports live,
including a warning for the case where the promoted id equals the
configured default model; and activation of any kind -- a candidate is
inert. `PromotionCandidate.state` has exactly two values, `"candidate"` and
`"suspended"`, because nothing built so far can set a third, and a state
nothing can set is a promise the code cannot keep.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING
from typing import (
    Any, Iterable, Mapping, Optional, Sequence, get_args, get_type_hints,
)

import boto3
from boto3.dynamodb.conditions import Attr
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from core.aws_pool import boto_config
from core.logging import get_logger
from dynamo.client import (
    DYNAMODB_POOL_ENV,
    get_dynamodb_resource,
    promotion_candidates_table_name,
)

from .records import DiscoveredRecord, ObservationScope, _json_safe

# `registry_entries` is deliberately NOT imported at module level. This
# module is one hop from `mvp.models` through the activation path a later
# unit adds: that unit makes `models.py` load activated entries during its
# own initialisation, which means `models.py` importing (indirectly) back
# into this module while it is still defining `registry_entries` for the
# first time. Two modules that import each other at the top only work by
# luck of import order; the same shape `mvp.routing.chains` used to break a
# genuine cycle applies here, so the import stays inside the one function
# that needs it (`_registry_collisions`, below) rather than at the top of
# the file. Do not hoist this back up without first checking whether the
# cycle it exists to avoid is still there.

logger = get_logger(__name__)

SCHEMA_VERSION = 1

_CANDIDATE_PREFIX = "CANDIDATE#"
_IDENTIFIER_PREFIX = "IDENTIFIER#"
_CANDIDATE_SK = "CANDIDATE"
_RESERVATION_SK = "RESERVATION"

# The two, and only two, values `PromotionCandidate.state` may hold. There is
# no third: activation (which would need one) is not in this contract — see
# the module docstring — and a state nothing can set is a promise the code
# cannot keep.
STATE_CANDIDATE = "candidate"
STATE_SUSPENDED = "suspended"
_STATES = frozenset({STATE_CANDIDATE, STATE_SUSPENDED})

_serializer = TypeSerializer()


class PromotionStoreUnavailable(Exception):
    """A read or write of the promotion candidate store failed and nothing
    was read or written. Raised rather than answered as "not found" or
    silently dropped, mirroring `mvp.discovery.records.
    DiscoveredRecordStoreUnavailable` exactly — an unreadable store here is
    not evidence that a candidate does not exist, and it is a WORSE fact to
    get wrong: this table's rows say which models are servable at all.
    """


class PromotionRefused(ValueError):
    """A promotion input, or a promotion write, that this store refuses.

    `reason` is validated against a closed set at construction — the same
    gate `mvp.admin_entitlements.GrantFloorRefusal` puts on its own `reason`
    — so both this module and any caller that inspects a refusal always see
    one of exactly these strings, never a typo of one.

    The closed set below is every reason promotion as a whole can refuse
    for, including five (`provider_unsupported`, `alias_required`,
    `pricing_key_required`, `pricing_key_is_default`, `jurisdiction_required`)
    and one (`protocol_mismatch`) and one more (`record_not_found`) that
    belong to work this module does not do -- deriving the mechanical
    fields, validating the required human inputs, and the demotion-identity
    check all raise this same exception, so its vocabulary is fixed here in
    full rather than grown piecemeal by whoever reaches for the next reason.
    Only `IDENTIFIER_TAKEN` is raised by this module today.

    `reason` plus an optional free-text `detail` is the whole shape, not
    `GrantFloorRefusal`'s richer field set — that class carries
    `leg`/`floor_micro`/`live_micro` because it reports two numbers and a leg
    for ONE reason pair; a promotion refusal reports a category and, for
    `identifier_taken`, which identifier, so seven domain-specific fields
    that are meaningless for the other seven reasons would be the wrong
    shape to freeze here.
    """

    PROVIDER_UNSUPPORTED = "provider_unsupported"
    ALIAS_REQUIRED = "alias_required"
    PRICING_KEY_REQUIRED = "pricing_key_required"
    PRICING_KEY_IS_DEFAULT = "pricing_key_is_default"
    JURISDICTION_REQUIRED = "jurisdiction_required"
    IDENTIFIER_TAKEN = "identifier_taken"
    PROTOCOL_MISMATCH = "protocol_mismatch"
    RECORD_NOT_FOUND = "record_not_found"
    REASONS = frozenset({
        PROVIDER_UNSUPPORTED, ALIAS_REQUIRED, PRICING_KEY_REQUIRED,
        PRICING_KEY_IS_DEFAULT, JURISDICTION_REQUIRED, IDENTIFIER_TAKEN,
        PROTOCOL_MISMATCH, RECORD_NOT_FOUND,
    })

    def __init__(self, reason: str, *, detail: Optional[str] = None) -> None:
        if reason not in self.REASONS:
            raise ValueError(
                f"unknown PromotionRefused reason {reason!r}; must be one of "
                f"{sorted(self.REASONS)}"
            )
        message = reason if detail is None else f"{reason}: {detail}"
        super().__init__(message)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class PromotionCandidate:
    """A candidate row, in full — every field this store's design settles on,
    and no others. No field defaults: this dataclass is always built from
    either a fresh derivation (the mechanical fields, the required human
    inputs, and the demotion-identity fields other work in this package
    supplies) or a stored row re-parsed by `_from_item`, never by a caller
    that "forgot" a field and got something plausible back —
    `ModelEntry.access`'s permissive default is the exact failure shape the
    access constraint above exists to close, generalised here to the whole
    record rather than reopened field by field.

    `state` is one of `_STATES` only (`__post_init__` enforces it) --
    activation is not built yet, so nothing may ever set a third value.
    `aliases` is a tuple, immutable like the rest of this frozen dataclass.
    `observation_scope` reuses `mvp.discovery.records.ObservationScope`
    rather than a second definition of the same four fields -- a candidate
    and the discovered record it came from describe the SAME observation,
    and a later demotion's identity check reads it as such.
    """

    profile_id: str
    observation_scope: ObservationScope
    state: str
    aliases: tuple[str, ...]
    pricing_key: str
    jurisdiction: Optional[str]
    provider: str
    bedrock_model_id: str
    bedrock_region: str
    wire_protocol: str
    model_family: str
    profile_scope: str
    created_at: str
    created_by: str

    def __post_init__(self) -> None:
        if self.state not in _STATES:
            raise ValueError(
                f"unknown PromotionCandidate state {self.state!r}; must be "
                f"one of {sorted(_STATES)}"
            )

    def identifiers(self) -> tuple[str, ...]:
        """Every public identifier this candidate would make reachable: its
        aliases, plus its Bedrock model id (`mvp.models._BEDROCK_ID_MAP`
        exists precisely because a Bedrock id is ALSO a valid client-facing
        identifier for any registry entry). Order-preserving, deduplicated
        — a candidate whose Bedrock id also happens to be listed as one of
        its own aliases must not reserve the same identifier twice in one
        transaction, which DynamoDB would reject outright as a duplicate
        key within a single `TransactWriteItems`.
        """
        seen: set[str] = set()
        ordered: list[str] = []
        for identifier in (*self.aliases, self.bedrock_model_id):
            if identifier not in seen:
                seen.add(identifier)
                ordered.append(identifier)
        return tuple(ordered)


def _table():
    return get_dynamodb_resource().Table(promotion_candidates_table_name())


def _low_level_client():
    """A low-level DynamoDB client, built fresh rather than taken from the
    shared resource's `.meta.client`.

    Measured, not assumed: `boto3.resource("dynamodb").meta.client` still
    carries the resource's own `before-parameter-build.dynamodb` event
    handler, which serialises `TransactItems[*].Put.Item` a SECOND time on
    every call, low-level or not -- harmless for a plain-Python item (the
    handler's serialisation is idempotent-looking on those), but this
    module's items are pre-serialised at the call site (`_serializer.
    serialize`, above) precisely so `_to_item`/`_reservation_item`'s
    `_json_safe` float discipline is visible to the actual wire call. Passing
    an already-`{"S": ...}`-shaped item back through that handler raises
    `TypeError` deep in botocore's own serialiser, and it surfaces only as
    every item in the transaction cancelling -- never in an in-memory test of
    `_serializer.serialize` alone, which is exactly the kind of gap the
    contract's own float-discipline note warns about, one layer up.
    `dynamo.user_tenants.switch_tenant` hit the same thing first and takes
    the same fix: a client built directly, never through a resource.
    """
    region = os.getenv("AWS_REGION", "us-east-1")
    return boto3.client("dynamodb", region_name=region, config=boto_config(DYNAMODB_POOL_ENV))


def _candidate_pk(profile_id: str) -> str:
    return f"{_CANDIDATE_PREFIX}{profile_id}"


def _identifier_pk(identifier: str) -> str:
    return f"{_IDENTIFIER_PREFIX}{identifier}"


def _to_item(candidate: PromotionCandidate) -> dict[str, Any]:
    """The stored shape of a candidate row.

    `access` is written here as the literal `"entitlement_required"` — see
    the module docstring's access-constraint summary and `_from_item` below
    for the read side of the same constraint. Passed through `_json_safe` (imported from
    `records.py`, not reimplemented) for the same reason that module needs
    it: botocore refuses a raw `float`, and the type only becomes illegal at
    serialisation, so an in-memory round trip of this function alone would
    never see the bug that a live `TransactWriteItems` call would raise.
    """
    scope = candidate.observation_scope
    item = {
        "pk": _candidate_pk(candidate.profile_id),
        "sk": _CANDIDATE_SK,
        "schema_version": SCHEMA_VERSION,
        "access": "entitlement_required",
        "profile_id": candidate.profile_id,
        "observation_scope": {
            "account": scope.account,
            "region": scope.region,
            "credentials_fingerprint": scope.credentials_fingerprint,
            "observed_at": scope.observed_at,
        },
        "state": candidate.state,
        "aliases": list(candidate.aliases),
        "pricing_key": candidate.pricing_key,
        "jurisdiction": candidate.jurisdiction,
        "provider": candidate.provider,
        "bedrock_model_id": candidate.bedrock_model_id,
        "bedrock_region": candidate.bedrock_region,
        "wire_protocol": candidate.wire_protocol,
        "model_family": candidate.model_family,
        "profile_scope": candidate.profile_scope,
        "created_at": candidate.created_at,
        "created_by": candidate.created_by,
    }
    return _json_safe(item)


def _reservation_item(identifier: str, candidate: PromotionCandidate) -> dict[str, Any]:
    """One identifier-reservation row. Carries a little more than the bare
    key (`identifier`, `profile_id`, `created_at`) so an orphaned reservation
    — the named, not-built-here debt from a superseding promotion or a
    demotion that has nothing yet to garbage-collect a stale reservation row
    — is at least attributable when someone eventually looks. `created_at` is the
    candidate's own timestamp, not a fresh clock read: one moment, one
    stamp, reused everywhere it applies, the same discipline
    `records.Blocker` documents for its own two timestamps.
    """
    return _json_safe({
        "pk": _identifier_pk(identifier),
        "sk": _RESERVATION_SK,
        "schema_version": SCHEMA_VERSION,
        "identifier": identifier,
        "profile_id": candidate.profile_id,
        "created_at": candidate.created_at,
    })


def _from_item(item: Mapping[str, Any]) -> Optional[PromotionCandidate]:
    """Parse a stored candidate row, or reject it.

    The access constraint lives HERE, on the read side, for the same reason
    `mvp.discovery.records._from_item` is where an unrecognised
    `schema_version` gets caught: this is the one place every stored
    candidate passes through on its way to becoming a `PromotionCandidate`,
    for both store verbs below (`get_promotion_candidate` and
    `list_promotion_candidates`), so a rejection written once here cannot be
    bypassed by calling the other verb. `PromotionCandidate` itself carries
    no `access` field — access is a schema-level constant this module
    writes and checks, not a per-candidate decision — so there is nowhere
    else for the check to live that both verbs would still go through.

    A row failing this check is NOT re-raised as `PromotionRefused` (that
    vocabulary is for a refused WRITE, and nothing was written here) and
    does NOT fail the process (stored data must not be able to block every
    deploy). It skips: excluded from the result, and surfaced with a logged
    warning naming the `profile_id` and what was wrong. Skipping is the
    fail-closed direction — the consequence is that the model stays unserved
    — while raising through this shared parse path would let one tampered
    row take down the listing for every other candidate; the warning is
    what keeps fail-closed from being fail-silent, so the gap is visible to
    an operator instead of being indistinguishable from "no candidate here
    at all".

    The same disposition, and the same log event, covers a `schema_version`
    this build does not recognise and a `state` outside `_STATES`: both are
    "this build cannot trust this row", not "this row does not exist".
    """
    profile_id = str(item.get("profile_id") or item.get("pk") or "")
    schema = item.get("schema_version")
    try:
        schema = int(schema)
    except (TypeError, ValueError):
        schema = None
    if schema != SCHEMA_VERSION:
        logger.warning(
            "promotion_candidate_quarantined", profile_id=profile_id,
            reason="unrecognised_schema_version", schema_version=item.get("schema_version"),
        )
        return None
    access = item.get("access")
    if access != "entitlement_required":
        # A row missing `access`, or restored from before this constraint
        # existed, or carrying the dataclass default's permissive value, is
        # refused here rather than treated as `"general"` -- the one
        # reading that would make it reachable by every tenant with no one
        # having chosen that.
        logger.warning(
            "promotion_candidate_quarantined", profile_id=profile_id,
            reason="access_not_entitlement_required", access=access,
        )
        return None
    scope_raw = item.get("observation_scope") or {}
    if not isinstance(scope_raw, Mapping):
        logger.warning(
            "promotion_candidate_quarantined", profile_id=profile_id,
            reason="observation_scope_unreadable",
        )
        return None
    try:
        return PromotionCandidate(
            profile_id=profile_id,
            observation_scope=ObservationScope(
                account=str(scope_raw.get("account") or ""),
                region=str(scope_raw.get("region") or ""),
                credentials_fingerprint=str(scope_raw.get("credentials_fingerprint") or ""),
                observed_at=str(scope_raw.get("observed_at") or ""),
            ),
            state=str(item.get("state") or ""),
            aliases=tuple(str(a) for a in item.get("aliases") or ()),
            pricing_key=str(item.get("pricing_key") or ""),
            jurisdiction=(
                str(item["jurisdiction"]) if item.get("jurisdiction") is not None else None
            ),
            provider=str(item.get("provider") or ""),
            bedrock_model_id=str(item.get("bedrock_model_id") or ""),
            bedrock_region=str(item.get("bedrock_region") or ""),
            wire_protocol=str(item.get("wire_protocol") or ""),
            model_family=str(item.get("model_family") or ""),
            profile_scope=str(item.get("profile_scope") or ""),
            created_at=str(item.get("created_at") or ""),
            created_by=str(item.get("created_by") or ""),
        )
    except ValueError as exc:
        # `PromotionCandidate.__post_init__`'s own state check, most likely
        # -- a stored `state` outside `_STATES`. Same quarantine, same
        # surface, not a different disposition just because the invariant
        # lives in the dataclass instead of in this function.
        logger.warning(
            "promotion_candidate_quarantined", profile_id=profile_id,
            reason="invalid_field", detail=str(exc),
        )
        return None


def get_promotion_candidate(profile_id: str) -> Optional[PromotionCandidate]:
    """Read the one candidate `profile_id` owns, consistently."""
    try:
        resp = _table().get_item(
            Key={"pk": _candidate_pk(profile_id), "sk": _CANDIDATE_SK},
            ConsistentRead=True,
        )
    except ClientError as exc:
        raise PromotionStoreUnavailable(
            f"promotion candidate store unreachable reading profile_id={profile_id!r}: {exc}"
        ) from exc
    item = resp.get("Item")
    return _from_item(item) if item else None


def list_promotion_candidates() -> list[PromotionCandidate]:
    """Every candidate row in the table (never a reservation row), via a
    full Scan.

    No GSI exists on this table for the purpose (its key schema is only
    `pk`/`sk`), so unlike `records.list_discovered_records`'s GSI `Query`,
    this is a Scan filtered to `sk == "CANDIDATE"` -- acceptable because
    candidates are an admin-driven, low-volume item type (one row per
    onboarded model, not per request), the same volume assumption
    `mvp.admin_entitlements` and `mvp.discovery.records` already make about
    their own tables.
    """
    return _scan_candidates()[0]


def _scan_candidates() -> tuple[list[PromotionCandidate], list[tuple[str, str]]]:
    """Every candidate row, split into the ones this build can parse and the
    ones it cannot.

    Two callers want different halves of one scan. An ordinary reader wants
    only rows it can trust, so `list_promotion_candidates` drops the rest --
    a malformed row must not reach a caller that would act on it. But the
    start-of-process check owes an operator the opposite: a row this build
    cannot re-parse has to be NAMED, because a row that is only logged and
    dropped is durable state nobody is looking for. Returning both halves is
    what lets one rule not silently defeat the other.
    """
    try:
        candidates: list[PromotionCandidate] = []
        unparseable: list[tuple[str, str]] = []
        kwargs: dict[str, Any] = {"FilterExpression": Attr("sk").eq(_CANDIDATE_SK)}
        while True:
            resp = _table().scan(**kwargs)
            for item in resp.get("Items", []):
                parsed = _from_item(item)
                if parsed is not None:
                    candidates.append(parsed)
                else:
                    unparseable.append((str(item.get("profile_id") or item.get("pk") or ""),
                                        "unreadable_stored_row"))
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
    except ClientError as exc:
        raise PromotionStoreUnavailable(
            f"promotion candidate store unreachable listing candidates: {exc}"
        ) from exc
    return candidates, unparseable


def _registry_collisions(identifiers: Iterable[str]) -> list[str]:
    """Every identifier in `identifiers` that the code-resident registry
    already serves -- the write-time check must consider the identifiers
    already in the code-resident registry, not only other candidates, or a
    promotion could reserve a name a request already resolves to a
    different model through. Reads `registry_entries()`, the one sanctioned
    accessor, never the private `_ALIAS_MAP`/`_BEDROCK_ID_MAP` this whole
    change exists to stop a second module from importing.
    """
    # Function-local: see the module-level comment above this file's import
    # block for why `registry_entries` cannot be imported at the top here.
    from ..models import registry_entries

    taken: set[str] = set()
    for entry in registry_entries():
        taken.update(entry.aliases)
        taken.add(entry.bedrock_model_id)
    return [identifier for identifier in identifiers if identifier in taken]


def put_promotion_candidate(candidate: PromotionCandidate) -> None:
    """Write `candidate` and reserve every identifier it makes public, all
    in one `TransactWriteItems`.

    The write-time collision check in full: every identifier in `candidate.
    identifiers()` (aliases plus the Bedrock id -- a discovered record has
    two kinds of public identifier, not one, since a Bedrock id is itself a
    valid client-facing name for any registry entry) is checked against the
    code-resident registry BEFORE any write is attempted (a pure in-memory
    check, cheapest first), then reserved in the SAME transaction as the
    candidate row, each reservation guarded by
    `ConditionExpression="attribute_not_exists(pk)"` -- because DynamoDB has
    no unique constraint on a non-key attribute, this reservation row is
    what makes uniqueness a database constraint instead of a check against
    `list_promotion_candidates()` followed by a separate write, which is
    exactly the race this shape exists to close. Two concurrent promotions
    naming the same identifier race on that reservation row; DynamoDB admits
    exactly one `TransactWriteItems`, so exactly one candidate is written
    and the other raises `PromotionRefused` -- never both, and never a
    check-then-write gap between them.

    The candidate row itself carries no condition: a second promotion of the
    SAME `profile_id` (not concurrent with itself -- a race between two
    such calls is still caught by the reservation rows above, since both
    attempts want the SAME identifiers) overwrites the candidate row.
    Re-promotion semantics are not settled anywhere yet; nothing here
    invents a policy for it beyond what the reservation guard enforces
    anyway.

    Raises `PromotionRefused(PromotionRefused.IDENTIFIER_TAKEN, ...)` when
    any identifier collides, either with the registry or with the store, and
    `PromotionStoreUnavailable` when the transaction could not be attempted
    or failed for a reason other than a losing reservation.
    """
    identifiers = candidate.identifiers()

    registry_taken = _registry_collisions(identifiers)
    if registry_taken:
        raise PromotionRefused(
            PromotionRefused.IDENTIFIER_TAKEN,
            detail=(
                f"identifier(s) {sorted(registry_taken)} already served by the "
                f"code-resident registry; profile_id={candidate.profile_id!r} cannot "
                f"reserve them"
            ),
        )

    table_name = promotion_candidates_table_name()
    transact_items: list[dict[str, Any]] = [
        {
            "Put": {
                "TableName": table_name,
                "Item": {k: _serializer.serialize(v) for k, v in _to_item(candidate).items()},
            }
        }
    ]
    for identifier in identifiers:
        transact_items.append({
            "Put": {
                "TableName": table_name,
                "Item": {
                    k: _serializer.serialize(v)
                    for k, v in _reservation_item(identifier, candidate).items()
                },
                "ConditionExpression": "attribute_not_exists(pk)",
            }
        })

    client = _low_level_client()
    try:
        client.transact_write_items(TransactItems=transact_items)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "TransactionCanceledException":
            reasons = exc.response.get("CancellationReasons") or []
            # `transact_items[0]` is the candidate row (no condition, so it
            # never itself carries "ConditionalCheckFailed"); index i+1 of
            # `identifiers` is `transact_items[i + 1]`, in the same order --
            # a losing reservation at position i names identifier[i].
            collided = [
                identifiers[i]
                for i, reason in enumerate(reasons[1:])
                if (reason or {}).get("Code") == "ConditionalCheckFailed"
            ]
            if collided:
                raise PromotionRefused(
                    PromotionRefused.IDENTIFIER_TAKEN,
                    detail=(
                        f"identifier(s) {sorted(collided)} already reserved by another "
                        f"candidate; profile_id={candidate.profile_id!r} cannot reserve "
                        f"them"
                    ),
                ) from exc
        raise PromotionStoreUnavailable(
            f"promotion candidate store unreachable writing profile_id="
            f"{candidate.profile_id!r}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Everything above turns an already-decided `PromotionCandidate` into a
# stored row. Everything below decides what that candidate's fields ARE:
# the two the record and a probe already answer (`derive_candidate`), the
# three nothing but a human can answer (`validate_human_inputs`), the
# identity a later demotion signal will key on (the same function refuses
# rather than write one that could not be found again), and which
# identifiers a promotion is about to make reachable (`newly_live_
# identifiers`, `default_model_collision_warning`).
#
# `check_registry_at_start` is the other half of collision detection: the
# write-time reservation above only ever looks one direction -- it stops a
# NEW candidate from colliding with what already exists at that moment. It
# cannot see a later code deploy that adds a name a stored candidate
# already claims, because loading the code-resident registry never reads
# this table. This is the half that closes that gap, by composing both
# sources and re-checking every uniqueness property the bundled loader
# enforces only within its own document.
# ---------------------------------------------------------------------------

# The closed six-provider and two-protocol sets, read off `ModelEntry`'s own
# field annotations rather than a second, hand-written tuple -- the same
# "derive, don't duplicate" reasoning `mvp.models` already applies to its
# own scope-prefix table. If the annotation ever changes, this follows it
# with no edit.
# Read lazily, not at import: `mvp.models` imports back into this module through
# a later unit's activation path, so touching it at module level deadlocks process
# start. See the note beside the other deferred import above.
@lru_cache(maxsize=1)
def _entry_field_choices() -> tuple[frozenset[str], frozenset[str]]:
    from ..models import ModelEntry

    hints = get_type_hints(ModelEntry)
    return (frozenset(get_args(hints["provider"])),
            frozenset(get_args(hints["wire_protocol"])))


def _supported_providers() -> frozenset[str]:
    return _entry_field_choices()[0]


def _supported_wire_protocols() -> frozenset[str]:
    return _entry_field_choices()[1]

# `ModelEntry.pricing_key`'s own default. Accepting it silently would charge
# whatever this candidate becomes at that tier's rate, and that tier prices
# above several providers' real legs -- see the registry field's own
# docstring. Refused explicitly rather than merely "not the choice we
# expected".
_DEFAULT_PRICING_KEY = "default"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_human_inputs(
    *,
    aliases: Optional[Sequence[str]],
    pricing_key: Optional[str],
    jurisdiction: Optional[str],
) -> None:
    """The three decisions nothing about a discovered record can answer:
    the public name, the price tier, and the residency posture. Each is
    required with no default, and each refuses on its own -- an absent
    `jurisdiction` is refused rather than read as "unrestricted", because
    unrestricted is a real, nameable posture and a promotion that means it
    must say so rather than leave the field empty.
    """
    cleaned_aliases = tuple(a for a in (aliases or ()) if a and a.strip())
    if not cleaned_aliases:
        raise PromotionRefused(PromotionRefused.ALIAS_REQUIRED)
    if not pricing_key or not pricing_key.strip():
        raise PromotionRefused(PromotionRefused.PRICING_KEY_REQUIRED)
    if pricing_key == _DEFAULT_PRICING_KEY:
        raise PromotionRefused(PromotionRefused.PRICING_KEY_IS_DEFAULT)
    if not jurisdiction or not jurisdiction.strip():
        raise PromotionRefused(PromotionRefused.JURISDICTION_REQUIRED)


def derive_candidate(
    record: DiscoveredRecord,
    *,
    aliases: Optional[Sequence[str]],
    pricing_key: Optional[str],
    jurisdiction: Optional[str],
    probe_wire_protocol: str,
    created_by: str,
) -> PromotionCandidate:
    """Build a candidate from one discovered record and the three decisions
    nothing about that record can answer. The layer below the store verbs
    above: this returns a `PromotionCandidate`, it does not write one --
    `put_promotion_candidate` is a separate, explicit call, so a caller that
    only wants to preview or report on a promotion (see `newly_live_
    identifiers`/`default_model_collision_warning` below) never has to
    reserve an identifier to do it.

    `probe_wire_protocol` is supplied by the caller rather than read from a
    verdict store here: the fact this derivation needs is "the protocol a
    probe already spoke and succeeded with", not "how to find that probe's
    own record", and a caller that already has the answer should not have
    to stand up a second store for this function to ask it again. The one
    thing still checked about it is that it names a protocol this registry
    recognises at all -- a value outside that set cannot become a valid
    entry no matter how confidently a caller supplies it, so it is refused
    the same way an unverified one would be.

    `profile_id` and `observation_scope` are copied verbatim from `record`:
    they are the identity a later demotion signal keys on. An absent
    `profile_id` means that identity is incomplete before anything else is
    even considered, so it is refused first, with the closed reason closest
    to what is actually wrong -- there is no reason string that names "the
    identity is incomplete" any more precisely than "this promotion names no
    record".
    """
    if not record.profile_id:
        raise PromotionRefused(PromotionRefused.RECORD_NOT_FOUND)
    if record.provider not in _supported_providers():
        raise PromotionRefused(PromotionRefused.PROVIDER_UNSUPPORTED)
    if probe_wire_protocol not in _supported_wire_protocols():
        raise PromotionRefused(PromotionRefused.PROTOCOL_MISMATCH)

    validate_human_inputs(
        aliases=aliases, pricing_key=pricing_key, jurisdiction=jurisdiction
    )
    cleaned_aliases = tuple(a for a in (aliases or ()) if a and a.strip())

    return PromotionCandidate(
        profile_id=record.profile_id,
        observation_scope=record.observation_scope,
        state=STATE_CANDIDATE,
        aliases=cleaned_aliases,
        pricing_key=pricing_key,
        jurisdiction=jurisdiction,
        provider=record.provider,
        # The account's own inference-profile id is the id the gateway
        # invokes -- `mvp.discovery.reconcile.build_record` sets both
        # `profile_id` and `raw_id` from the same `inferenceProfileId` the
        # account returned, so there is no separate, more-authoritative
        # spelling to derive.
        bedrock_model_id=record.raw_id,
        # The region this pass actually called Bedrock through is the
        # region a deployment invokes this profile from -- the same fact
        # `bedrock_region` already names on every entry the registry ships
        # today (see `ModelEntry`'s own note on the field's asymmetric
        # meaning between the two wire protocols).
        bedrock_region=record.invocation_region,
        wire_protocol=probe_wire_protocol,
        model_family=record.model_family,
        profile_scope=record.profile_scope,
        created_at=_now_iso(),
        created_by=created_by,
    )


def newly_live_identifiers(candidate: PromotionCandidate) -> tuple[str, ...]:
    """Every identifier this promotion makes reachable once activated --
    every alias, plus the Bedrock model id, which is a valid client-facing
    identifier for any registry entry whether or not an alias was ever
    chosen. A pure read: it reports on a candidate, it never builds or
    writes one, so a caller can preview a promotion's effect on the
    namespace before -- or without ever -- committing it.
    """
    return candidate.identifiers()


def default_model_collision_warning(candidate: PromotionCandidate) -> Optional[str]:
    """The one consequence that is easy to miss because nothing about a
    single promotion mentions it on its own: if the id being promoted, OR
    one of the aliases being promoted, is the exact string `resolve_model`
    falls back to for a request naming no model at all, promoting this
    candidate changes what a model-less request resolves to. `None` when
    neither matches. A pure read, like `newly_live_identifiers` above.
    """
    # Deferred like the other `mvp.models` reads here: that module imports back
    # into this one during its own initialisation once activation exists.
    from ..models import DEFAULT_MODEL

    if DEFAULT_MODEL in candidate.aliases or candidate.bedrock_model_id == DEFAULT_MODEL:
        return (
            f"{DEFAULT_MODEL!r} is the configured default model: requests "
            "that name no model at all will resolve to this candidate once "
            "it is activated."
        )
    return None


@dataclass(frozen=True)
class QuarantinedCandidate:
    """A stored candidate that cannot stand in the composed namespace on
    its own any more. Excluded from that namespace and reported here,
    never silently dropped and never allowed to fail the whole process on
    its own -- see `check_registry_at_start`."""

    profile_id: str
    reason: str


def _quarantine_reason(candidate: PromotionCandidate) -> Optional[str]:
    """Why a stored candidate cannot stand in the composed namespace on its
    own, or `None` when it can. Never about collision with another entry --
    two candidates (or a candidate and a code entry) that each stand fine
    alone but claim the same name, or the same `(model_family,
    profile_scope)` pair, are the OTHER failure `check_registry_at_start`
    catches, by raising instead of quarantining, because that half is the
    one meant to stop boot.
    """
    if candidate.provider not in _supported_providers():
        return PromotionRefused.PROVIDER_UNSUPPORTED
    if candidate.wire_protocol not in _supported_wire_protocols():
        return PromotionRefused.PROTOCOL_MISMATCH
    if candidate.state not in _STATES:
        return "unrecognised_state"
    if not candidate.profile_id or not candidate.bedrock_model_id:
        return "incomplete_identity"
    return None


if TYPE_CHECKING:  # `mvp.models` cannot be imported at runtime here; see the notes above.
    from ..models import ModelEntry


def check_registry_at_start(
    *, registry: Optional[Iterable["ModelEntry"]] = None,
) -> tuple[QuarantinedCandidate, ...]:
    """Re-validate the whole namespace a client can address -- the
    code-resident registry plus every stored candidate, reserved or
    suspended -- at every process start, not only at the moment one
    candidate was written.

    `registry` defaults to the live code-resident registry
    (`registry_entries()`); a caller may pass a different iterable of
    entries to check a candidate document that has not been loaded as the
    process registry, the same reason `mvp.models.load_registry` takes a
    `path` rather than only ever reading the default file.

    Two uniqueness properties the bundled loader enforces, but only within
    its own document, are re-checked here over the UNION of that document
    and every standing candidate:

    - No name -- an alias or a Bedrock model id -- may be claimed twice.
      `mvp.models._ALIAS_MAP`/`_BEDROCK_ID_MAP` are flat dict
      comprehensions; a duplicate does not raise there, the later entry
      simply wins, and client traffic reaches whichever one that happened
      to be.
    - No `(model_family, profile_scope)` pair may name two entries.
      `mvp.admin_entitlements._find_entry` depends on this holding across
      the SAME union this function composes -- its own docstring says "at
      most one can ever match" on the strength of the bundled loader's
      check alone, which knows nothing about a promoted candidate. Once a
      promoted entry can share a pair with a code entry, that guarantee is
      false, and the grant, its floor comparison, and the eligibility
      predicate that hang off `_find_entry` would silently apply to
      whichever entry the union happened to return first.

    Either collision raises `ValueError` naming both claimants, so a bad
    code deploy or a bad promotion fails the process instead of shadowing
    a name or a grant target silently. A suspended candidate still claims
    its identifiers and its pair here even though it is not servable:
    releasing either back into the pool is what would let a new promotion
    take it while old client traffic, or an old grant, still expects the
    entry it used to name.

    A stored row that cannot stand on its own any more -- its provider no
    longer supported, its protocol outside the recognised set, its state
    unrecognised, or an identity field empty -- is excluded from the
    composed set and returned as quarantined, rather than raising for the
    whole process (stored data must never be able to hold every deploy
    hostage) or being dropped without a trace (an operator needs to see it
    to act on it).
    """
    # Deferred for the same reason as the other `mvp.models` imports here: a
    # later unit makes that module import back into this one during its own
    # initialisation, so touching it at module level deadlocks process start.
    from ..models import registry_entries

    entries = tuple(registry_entries() if registry is None else registry)

    readable, unreadable = _scan_candidates()
    quarantined: list[QuarantinedCandidate] = [
        QuarantinedCandidate(profile_id=pid, reason=reason) for pid, reason in unreadable
    ]
    standing: list[PromotionCandidate] = []
    for candidate in readable:
        reason = _quarantine_reason(candidate)
        if reason is not None:
            quarantined.append(
                QuarantinedCandidate(profile_id=candidate.profile_id, reason=reason)
            )
            continue
        standing.append(candidate)

    claimed_names: dict[str, str] = {}
    for entry in entries:
        owner = f"the code-resident registry ({entry.bedrock_model_id!r})"
        for name in (*entry.aliases, entry.bedrock_model_id):
            claimed_names.setdefault(name, owner)
    for candidate in standing:
        owner = f"promoted candidate {candidate.profile_id!r}"
        for name in candidate.identifiers():
            existing = claimed_names.get(name)
            if existing is not None:
                raise ValueError(
                    f"composed registry collision on {name!r}: already claimed "
                    f"by {existing}, also claimed by {owner}"
                )
            claimed_names[name] = owner

    claimed_pairs: dict[tuple[str, str], str] = {}
    for entry in entries:
        pair = (entry.model_family, entry.profile_scope)
        claimed_pairs.setdefault(
            pair, f"the code-resident registry ({entry.bedrock_model_id!r})"
        )
    for candidate in standing:
        pair = (candidate.model_family, candidate.profile_scope)
        owner = f"promoted candidate {candidate.profile_id!r}"
        existing = claimed_pairs.get(pair)
        if existing is not None:
            raise ValueError(
                f"composed registry collision on model_family={pair[0]!r} "
                f"profile_scope={pair[1]!r}: already claimed by {existing}, "
                f"also claimed by {owner}"
            )
        claimed_pairs[pair] = owner

    return tuple(quarantined)
