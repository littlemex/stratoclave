"""Activation: a verified promotion candidate becomes a routable registry
entry.

Every other step in this change decides whether a model *may* be served.
This module is the one that makes it *served*: it reads a promotion
candidate (the discovery-and-human-review store owned elsewhere in this
package) and a probe verdict (the store owned by the probe), checks that the
verdict is actually a statement about the candidate being activated, and — on
success — persists a `ModelEntry` that `mvp.models`' own composed-registry
cache folds in on its next refresh.

`get_probe_verdict(profile_id, invocation)` returns `None` when no probe has
ever recorded a verdict for that exact `(profile_id, invocation)` pair —
mirroring `get_promotion_candidate`'s and `get_discovered_record`'s own
"`None` means absent, an exception means unreadable" convention. This module
treats that `None` as `ActivationRefused.VERDICT_NOT_FOUND`, exactly the same
disposition a `state="invalidated"` verdict gets a different reason for
(`VERDICT_NOT_VERIFIED`) — "never probed" and "probed and since invalidated"
are different facts and get different reasons, but both refuse activation.

What this module deliberately does NOT do, because retirement and lifecycle
are out of scope for every unit in this change: there is no deactivation, no
tombstone, no expiry, and re-running activation for a profile that is already
active simply re-derives and re-writes the same entry rather than branching
on "already active" as a distinct state. Nothing here can un-serve a model
that a later probe invalidation would want retired — that mechanism is
explicitly future work (see the frozen interfaces document).

Storage: the SAME table as the promotion-candidate store
(`dynamo.client.promotion_candidates_table_name()`), as its own row kind —
`pk = f"ACTIVE#{profile_id}"`, `sk = "ACTIVE"` — rather than a new table.
This mirrors the probe verdict's own choice to live in that table, keyed on
its own `(profile_id, invocation)` pair, as a third row kind rather than a
fourth table for what is, at bottom, more provenance about the same
candidate identity. No `iac/` change is needed as a result.

Three names are intentionally NOT defined here even though they would look
at home in this module: `PromotionCandidate` and its store accessors,
`ProbeVerdict` and its store accessors, and `DiscoveredRecord`'s own
`get_discovered_record` (this last one IS already implemented, in
`mvp.discovery.records`, and is imported rather than re-derived — its
`jurisdiction_bounded` is the authoritative fact about whether a profile is
geography-bound at all, which the candidate does not itself carry). The
first two are owned by the units writing `mvp/discovery/promotion.py` and
`mvp/discovery/verdict.py` concurrently with this one; this module imports
them by the names the frozen interfaces document fixes, and does not define
a parallel copy.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Optional

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from core.aws_pool import boto_config
from core.logging import get_logger
from dynamo.client import (
    DYNAMODB_POOL_ENV,
    get_dynamodb_resource,
    promotion_candidates_table_name,
)

from ..authz import user_has_permission
from ..deps import AuthenticatedUser
from ..models import ModelEntry
from .promotion import PromotionCandidate, get_promotion_candidate
from .records import get_discovered_record
from .verdict import STATE_VERIFIED, ProbeVerdict, get_probe_verdict

logger = get_logger(__name__)

SCHEMA_VERSION = 1

_ACTIVE_PREFIX = "ACTIVE#"
_ACTIVE_SK = "ACTIVE"

# One claim row per public identifier a LIVE activation holds -- disjoint from
# both `_ACTIVE_PREFIX` above (a different string: `list_activated_entries`'s
# own `pk.startswith("ACTIVE#")` filter does not match this prefix) and from
# `mvp.discovery.promotion`'s own `IDENTIFIER#`/`RESERVATION` row (a
# candidate's pre-activation reservation, checked at candidate-creation time
# against every OTHER candidate; this is the activation-time analogue,
# checked at activation time against every OTHER activation — see
# `_commit_activation`'s own docstring for why candidate-time reservation
# alone is not enough).
_ACTIVE_IDENTIFIER_PREFIX = "ACTIVE_IDENTIFIER#"
_ACTIVE_IDENTIFIER_SK = "CLAIM"

# The probe verdict's own key scheme, mirrored here rather than imported: it
# is documented, stable, cross-unit surface (`mvp.discovery.verdict`'s own
# module docstring states it plainly: "VERDICT#{profile_id}" /
# "INVOCATION#{invocation}"), not a private implementation detail of that
# module, and every store in this package already keeps its OWN copy of the
# key-building helpers it needs rather than importing another module's
# private `_pk`/`_sk` (see `_json_safe`'s own docstring, above, for the same
# convention applied to a different helper). Needed here, independent of
# `verdict.get_probe_verdict`, because the compare-and-set below has to name
# this exact item as a `ConditionCheck` INSIDE the same transaction as the
# activation write — a read followed by a write is exactly the race this
# guards against (see `_commit_activation`).
_VERDICT_PK_PREFIX = "VERDICT#"
_VERDICT_SK_PREFIX = "INVOCATION#"

_serializer = TypeSerializer()

# The permission that gates activation. Declared and seeded elsewhere
# (permissions.json, mvp.authz.ALL_SCOPES, the frontend mirror) by the unit
# that owns the two new scopes; this module only ever spells the string and
# checks it, never adds it to a scope universe.
PROMOTE_SCOPE = "models:promote"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ActivationRefused(ValueError):
    """A refused activation attempt. `reason` is validated against a closed
    set on construction, mirroring `mvp.discovery.promotion.PromotionRefused`
    and `mvp.admin_entitlements.EntitlementError`.

    This is a NEW, separate closed vocabulary from the discovery/promotion
    blocker-and-refusal strings the wider change's design already closes for
    the discovered-record and probe producers. That vocabulary answers "why
    is this record blocked" on an operator surface; activation's own refusal
    reasons answer a different question ("why did THIS activation attempt
    fail"), and nothing decided so far says the two should share one
    enumeration. Kept apart here rather than folding activation's reasons
    into that vocabulary uninvited.

    Seven reasons became nine once the operator surface's compare-and-set
    (an activation names the verdict identity it saw, and the write must
    honour exactly that identity or refuse) and the transactional identifier
    claim (below) both needed their own vocabulary rather than borrowing an
    existing reason for a different fact:

    - `VERDICT_IDENTITY_MISMATCH` -- the verdict for `(profile_id,
      invocation)` no longer carries the `verified_at` the caller named,
      whether because a fresher probe re-verified it or an invalidation
      raced the activation. Distinct from `VERDICT_NOT_VERIFIED`: that
      reason means "not currently verified at all"; this one means "verified,
      but not verified AS THE THING THE OPERATOR REVIEWED".
    - `IDENTIFIER_TAKEN` -- an alias or Bedrock id this activation would make
      live is already claimed, live, by a DIFFERENT profile's activation.
      Mirrors `mvp.discovery.promotion.PromotionRefused.IDENTIFIER_TAKEN`'s
      spelling deliberately (same fact, one layer later: candidate creation
      already reserved these names against every OTHER candidate, and this
      is the analogous guard against two candidates that each cleared that
      check separately both going live for a name they never actually
      shared until now — see `_commit_activation`'s own docstring).
    """

    NOT_PERMITTED = "not_permitted"
    CANDIDATE_NOT_FOUND = "candidate_not_found"
    RECORD_NOT_FOUND = "record_not_found"
    VERDICT_NOT_FOUND = "verdict_not_found"
    VERDICT_NOT_VERIFIED = "verdict_not_verified"
    VERDICT_IDENTITY_MISMATCH = "verdict_identity_mismatch"
    PRICING_KEY_MISMATCH = "pricing_key_mismatch"
    WIRE_PROTOCOL_MISMATCH = "wire_protocol_mismatch"
    IDENTIFIER_TAKEN = "identifier_taken"
    REASONS = frozenset({
        NOT_PERMITTED, CANDIDATE_NOT_FOUND, RECORD_NOT_FOUND, VERDICT_NOT_FOUND,
        VERDICT_NOT_VERIFIED, VERDICT_IDENTITY_MISMATCH, PRICING_KEY_MISMATCH,
        WIRE_PROTOCOL_MISMATCH, IDENTIFIER_TAKEN,
    })

    def __init__(self, reason: str, message: str) -> None:
        if reason not in self.REASONS:
            raise ValueError(
                f"unknown ActivationRefused reason {reason!r}; must be one of "
                f"{sorted(self.REASONS)}"
            )
        super().__init__(message)
        self.reason = reason


class ActivationStoreUnavailable(Exception):
    """A read or write of the activated-entry store failed and nothing was
    read or written. Mirrors `PromotionStoreUnavailable` /
    `DiscoveredRecordStoreUnavailable`: an unreadable store is not evidence
    that nothing is activated, so a caller must not treat this as "empty".

    `mvp.models`' composed-registry cache is the one caller that DOES choose
    to treat a refresh failure as "keep what was last known good" — never as
    "empty" past the very first refresh in a process. See that cache's own
    docstring for the reasoning; this module has no opinion on it beyond
    raising accurately.
    """


@dataclass(frozen=True)
class ActivatedEntry:
    """One activation record: the `ModelEntry` it produced, plus the
    provenance of when and by whom and against which probe invocation."""

    profile_id: str
    invocation: str
    entry: ModelEntry
    activated_at: str
    activated_by: str


def _table():
    return get_dynamodb_resource().Table(promotion_candidates_table_name())


def _pk(profile_id: str) -> str:
    return f"{_ACTIVE_PREFIX}{profile_id}"


def _json_safe(value: Any) -> Any:
    """One conversion for the whole item on the way out — see
    `mvp.discovery.records._json_safe`, which this mirrors exactly (same
    reasoning, same construction, kept as its own copy per this package's
    existing convention of a private per-module helper rather than a shared
    one). `float` becomes `Decimal(str(...))` because botocore refuses a
    `float` and the type only becomes illegal at serialisation; `Mapping`/
    `list`/`tuple` recurse so calling this once on a whole item is enough.
    """
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _entry_fields(entry: ModelEntry) -> dict[str, Any]:
    """The subset of `ModelEntry` activation actually produces, flattened for
    storage. Only the fields the candidate-to-registry-entry mapping
    populates are written — every other `ModelEntry` field (hybrid-serving,
    hosting the same model at a different scope's price tier via
    `sr_pool_ref`, and so on) is out of scope for a promoted-candidate entry
    and is left at the dataclass default on read, not persisted."""
    return {
        "provider": entry.provider,
        "bedrock_model_id": entry.bedrock_model_id,
        "bedrock_region": entry.bedrock_region,
        "aliases": list(entry.aliases),
        "wire_protocol": entry.wire_protocol,
        "pricing_key": entry.pricing_key,
        "profile_scope": entry.profile_scope,
        "model_family": entry.model_family,
        "access": entry.access,
        "jurisdiction_bounded": entry.jurisdiction_bounded,
        "jurisdiction": entry.jurisdiction,
    }


def _entry_from_fields(fields: Mapping[str, Any]) -> ModelEntry:
    return ModelEntry(
        provider=fields["provider"],
        bedrock_model_id=fields["bedrock_model_id"],
        bedrock_region=fields["bedrock_region"],
        aliases=tuple(fields["aliases"]),
        wire_protocol=fields["wire_protocol"],
        pricing_key=fields["pricing_key"],
        profile_scope=fields["profile_scope"],
        model_family=fields["model_family"],
        access=fields["access"],
        jurisdiction_bounded=bool(fields["jurisdiction_bounded"]),
        jurisdiction=fields.get("jurisdiction"),
    )


def _to_item(activated: ActivatedEntry) -> dict[str, Any]:
    item = {
        "pk": _pk(activated.profile_id),
        "sk": _ACTIVE_SK,
        "schema_version": SCHEMA_VERSION,
        "profile_id": activated.profile_id,
        "invocation": activated.invocation,
        "activated_at": activated.activated_at,
        "activated_by": activated.activated_by,
        "entry": _entry_fields(activated.entry),
    }
    return _json_safe(item)


def _from_item(item: Mapping[str, Any]) -> Optional[ActivatedEntry]:
    schema = item.get("schema_version")
    try:
        schema = int(schema)
    except (TypeError, ValueError):
        schema = None
    if schema != SCHEMA_VERSION:
        # An unrecognised schema could mean the field layout changed meaning;
        # skip the row rather than guess, same posture as every other store
        # in this package.
        return None
    entry_fields = item.get("entry")
    if not isinstance(entry_fields, Mapping):
        return None
    try:
        return ActivatedEntry(
            profile_id=str(item.get("profile_id") or ""),
            invocation=str(item.get("invocation") or ""),
            entry=_entry_from_fields(entry_fields),
            activated_at=str(item.get("activated_at") or ""),
            activated_by=str(item.get("activated_by") or ""),
        )
    except (KeyError, TypeError, ValueError):
        # A malformed row is quarantined, not fatal — the same convention
        # `records._from_item` and `promotion`'s own store use.
        return None


def get_activated_entry(profile_id: str) -> Optional[ActivatedEntry]:
    """Read the one activation record `profile_id` owns, consistently."""
    try:
        resp = _table().get_item(
            Key={"pk": _pk(profile_id), "sk": _ACTIVE_SK},
            ConsistentRead=True,
        )
    except ClientError as exc:
        raise ActivationStoreUnavailable(
            f"activated-entry store unreachable reading profile_id={profile_id!r}: {exc}"
        ) from exc
    item = resp.get("Item")
    return _from_item(item) if item else None


def list_activated_entries() -> list[ActivatedEntry]:
    """Every activation record, via a `Scan` narrowed to `ACTIVE#` rows.

    A `Scan`, not a GSI query: the promotion-candidate table's own index
    layout is unit 1's to design, and this row kind is a late addition to
    that table rather than a first-class citizen of whatever GSI unit 1
    built for its own listing.

    This is the function `mvp.models`' composed-registry cache calls on
    every TTL refresh — not a hot path (it runs once per refresh window
    across the whole process, not once per request), but not a one-shot
    boot-time read either any more. Raises `ActivationStoreUnavailable` on
    a failed read; that cache decides what "unavailable" means for a
    refresh, this function's only job is to report it accurately.
    """
    try:
        entries: list[ActivatedEntry] = []
        kwargs: dict[str, Any] = {}
        while True:
            resp = _table().scan(**kwargs)
            for item in resp.get("Items", []):
                if not str(item.get("pk") or "").startswith(_ACTIVE_PREFIX):
                    continue
                parsed = _from_item(item)
                if parsed is not None:
                    entries.append(parsed)
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
    except ClientError as exc:
        raise ActivationStoreUnavailable(
            f"activated-entry store unreachable listing entries: {exc}"
        ) from exc
    return entries


def put_activated_entry(activated: ActivatedEntry) -> None:
    """Full-replace write of the one activation record `activated.profile_id`
    owns. Unconditional, like `records.put_discovered_record`: re-activating
    a profile (the only way this ever runs twice for one profile) is meant
    to replace the previous snapshot with a fresh one derived from the
    candidate, the record and the verdict as they stand right now, not to be
    blocked by the fact that a snapshot already exists.

    This single-item write is NOT what `activate_candidate` calls to commit a
    real activation — see `_commit_activation`, below, for why a lone `put_
    item` on this row is not enough by itself (it claims no identifier and
    checks no verdict identity). Kept as its own function, and still exported,
    because it is the direct write a fixture wants when seeding an existing
    activation without going through the full gate — the same role `records.
    put_discovered_record` plays for that store's own tests.
    """
    try:
        _table().put_item(Item=_to_item(activated))
    except ClientError as exc:
        raise ActivationStoreUnavailable(
            f"activated-entry store unreachable writing profile_id={activated.profile_id!r}: {exc}"
        ) from exc


def _low_level_client():
    """A low-level DynamoDB client, built fresh rather than taken from the
    shared resource's `.meta.client` — the SAME fix, for the SAME measured
    reason, as `mvp.discovery.promotion._low_level_client` (see that
    function's own docstring): the resource's client still carries a
    `before-parameter-build.dynamodb` handler that double-serialises an item
    this module has already run through `TypeSerializer` itself, and that
    raises deep inside botocore only once a real `transact_write_items` call
    is made — invisible to any in-memory check of the serialised item alone.
    """
    region = os.getenv("AWS_REGION", "us-east-1")
    return boto3.client("dynamodb", region_name=region, config=boto_config(DYNAMODB_POOL_ENV))


def _verdict_key(profile_id: str, invocation: str) -> dict[str, Any]:
    return {
        "pk": f"{_VERDICT_PK_PREFIX}{profile_id}",
        "sk": f"{_VERDICT_SK_PREFIX}{invocation}",
    }


def _active_identifier_pk(identifier: str) -> str:
    return f"{_ACTIVE_IDENTIFIER_PREFIX}{identifier}"


def _active_identifier_item(identifier: str, *, profile_id: str, activated_at: str) -> dict[str, Any]:
    """One claim row: `identifier` is live, and `profile_id` is who claims
    it. Carries `activated_at` for the same attributability reason `mvp.
    discovery.promotion._reservation_item` gives for its own extra fields —
    an orphaned claim (the debt a candidate REWRITE leaves behind: the old
    aliases' claim rows are not released when a candidate is re-promoted
    with new ones, exactly mirroring that module's own named, not-built-here
    reservation debt) is at least attributable when someone eventually looks.
    """
    return _json_safe({
        "pk": _active_identifier_pk(identifier),
        "sk": _ACTIVE_IDENTIFIER_SK,
        "schema_version": SCHEMA_VERSION,
        "identifier": identifier,
        "profile_id": profile_id,
        "activated_at": activated_at,
    })


def _public_identifiers(entry: ModelEntry) -> tuple[str, ...]:
    """Every public identifier `entry` would make reachable: its aliases,
    plus its Bedrock model id — the SAME two-kind identifier space
    `mvp.discovery.promotion.PromotionCandidate.identifiers()` computes for
    the candidate this entry was built from, recomputed here rather than
    imported because `entry` (a `ModelEntry`) has no `identifiers()` method
    of its own and this module must not reach back into the candidate for a
    fact the ACTIVATED entry itself already carries. Order-preserving,
    deduplicated, for the same reason that method gives: a Bedrock id that is
    also listed as one of its own aliases must not be claimed twice in one
    transaction, which DynamoDB rejects outright as a duplicate key within a
    single `TransactWriteItems`.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for identifier in (*entry.aliases, entry.bedrock_model_id):
        if identifier not in seen:
            seen.add(identifier)
            ordered.append(identifier)
    return tuple(ordered)


def _commit_activation(
    activated: ActivatedEntry, *, expected_verified_at: str,
) -> None:
    """Commit one activation atomically: the verdict identity the caller
    named is still the current one, the `ACTIVE#{profile_id}` row is written,
    and every public identifier `activated.entry` makes reachable is claimed
    — all in ONE `TransactWriteItems`, or none of it.

    **Why a lone `put_item` (what this replaced) is not enough.** Two records
    can each produce a candidate, each obtain its own valid verdict, and each
    call `activate_candidate` — every check `_verify` runs passes for BOTH,
    independently, because each is checking its OWN candidate against its OWN
    verdict; neither observes the other's claim before committing. If the two
    candidates' derived `ModelEntry`s ever name an identifier or a
    `(model_family, profile_scope)` pair in common — a fact `mvp.discovery.
    promotion.put_promotion_candidate`'s own reservation transaction cannot
    see, because it only ever compares a NEW candidate against OTHER
    candidates and the code-resident registry, never against what has
    already gone LIVE — a plain `put_item` here would let both activations
    succeed, and only the NEXT process restart's `check_registry_at_start`
    would ever notice, by refusing to boot. This function moves that
    detection from boot time, where it is a deploy-wide outage, to activation
    time, where it is one refused write.

    **The verdict identity check is IN this transaction, not before it.** A
    read of the verdict followed by this write would leave exactly the gap
    the compare-and-set exists to close: between the read and the write, a
    concurrent probe could re-verify (a fresh `verified_at`, still `state=
    "verified"`) or an invalidation could land. A `ConditionCheck` item names
    the exact row and the exact fields — `verified_at` AND `state`, both;
    `invalidate_verdict` preserves `verified_at` across an invalidation on
    purpose (see that function's own docstring: "a reader asking what this
    verdict's evidence was before it stopped being trusted needs the
    original values still there"), so `verified_at` equality ALONE would not
    catch a verdict that was invalidated without ever being re-verified. Both
    conditions, in the SAME transaction as the write they gate, is what makes
    this a true compare-and-set rather than a check with a gap after it.

    **The identifier claim is idempotent for the SAME profile, exclusive
    against every other one.** `attribute_not_exists(pk) OR profile_id = :pid`
    admits a fresh claim and a re-claim by the profile that already holds it
    (re-activation, explicitly required to be idempotent) while refusing a
    claim already held by a DIFFERENT profile — the identifier-collision half
    of the race described above.

    Raises `ActivationRefused(VERDICT_IDENTITY_MISMATCH, ...)` when the
    verdict `ConditionCheck` item is the one that failed, `ActivationRefused
    (IDENTIFIER_TAKEN, ...)` naming every colliding identifier when one or
    more claim items failed instead, and `ActivationStoreUnavailable` for any
    other transaction failure (the store could not even attempt the write).
    """
    table_name = promotion_candidates_table_name()
    identifiers = _public_identifiers(activated.entry)
    verdict_key = _verdict_key(activated.profile_id, activated.invocation)

    transact_items: list[dict[str, Any]] = [
        {
            "ConditionCheck": {
                "TableName": table_name,
                "Key": {k: _serializer.serialize(v) for k, v in verdict_key.items()},
                "ConditionExpression": "verified_at = :vat AND #st = :verified",
                "ExpressionAttributeNames": {"#st": "state"},
                "ExpressionAttributeValues": {
                    ":vat": _serializer.serialize(expected_verified_at),
                    ":verified": _serializer.serialize(STATE_VERIFIED),
                },
            }
        },
        {
            "Put": {
                "TableName": table_name,
                "Item": {k: _serializer.serialize(v) for k, v in _to_item(activated).items()},
            }
        },
    ]
    for identifier in identifiers:
        transact_items.append({
            "Put": {
                "TableName": table_name,
                "Item": {
                    k: _serializer.serialize(v)
                    for k, v in _active_identifier_item(
                        identifier, profile_id=activated.profile_id,
                        activated_at=activated.activated_at,
                    ).items()
                },
                "ConditionExpression": "attribute_not_exists(pk) OR profile_id = :pid",
                "ExpressionAttributeValues": {":pid": _serializer.serialize(activated.profile_id)},
            }
        })

    client = _low_level_client()
    try:
        client.transact_write_items(TransactItems=transact_items)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "TransactionCanceledException":
            reasons = exc.response.get("CancellationReasons") or []
            # Index 0 is the verdict `ConditionCheck`; index 1 is the
            # unconditioned `ACTIVE#` put (never itself the failing item);
            # index i+2 of `identifiers` is `transact_items[i + 2]`, in the
            # same order — mirrors `mvp.discovery.promotion.
            # put_promotion_candidate`'s own reason-to-identifier mapping,
            # offset by one extra leading item.
            if reasons and (reasons[0] or {}).get("Code") == "ConditionalCheckFailed":
                raise ActivationRefused(
                    ActivationRefused.VERDICT_IDENTITY_MISMATCH,
                    f"verdict for profile_id={activated.profile_id!r} "
                    f"invocation={activated.invocation!r} no longer matches "
                    f"verified_at={expected_verified_at!r} in state={STATE_VERIFIED!r} "
                    f"— it was re-verified or invalidated since this activation was read",
                ) from exc
            collided = [
                identifiers[i]
                for i, reason in enumerate(reasons[2:])
                if (reason or {}).get("Code") == "ConditionalCheckFailed"
            ]
            if collided:
                raise ActivationRefused(
                    ActivationRefused.IDENTIFIER_TAKEN,
                    f"identifier(s) {sorted(collided)} are already live under a "
                    f"different profile_id; profile_id={activated.profile_id!r} "
                    f"cannot claim them",
                ) from exc
        raise ActivationStoreUnavailable(
            f"activated-entry store unreachable committing profile_id="
            f"{activated.profile_id!r}: {exc}"
        ) from exc


def _build_entry(candidate: PromotionCandidate, *, jurisdiction_bounded: bool) -> ModelEntry:
    """The candidate-to-registry-entry mapping, applied.

    `provider`, `model_family`, `profile_scope` and the three human inputs
    (`aliases`, `pricing_key`, `jurisdiction`) all come straight off the
    candidate — unit 1/2 already required and validated them before a
    candidate could exist at all, and re-validating them here would be a
    second, possibly-diverging opinion about checks that already ran.
    `access` is always `"entitlement_required"`, spelled out explicitly
    rather than left at the dataclass default (which is the permissive
    `"general"` — the one trap this whole item exists to avoid).

    `jurisdiction_bounded` is NOT derived from whether the candidate happens
    to carry a `jurisdiction` value — it is the discovered record's own
    authoritative boolean, passed in by the caller (`activate_candidate`,
    which reads it via `get_discovered_record`). A `global` profile's record
    reports `jurisdiction_bounded=False`; guessing `True` because a human
    filled in `jurisdiction` anyway would produce exactly the contradiction
    `mvp.models._parse_entry` refuses (`jurisdiction_bounded=False` with a
    non-`None` `jurisdiction`) — and would be silently wrong metadata for
    every entry it is wrong about, which is the case that matters most,
    since a `global` entry is the UNBOUNDED one. `jurisdiction` is therefore
    only ever set to the candidate's value when the record says the entry
    really is bounded; otherwise it is `None`, matching the boolean rather
    than contradicting it.
    """
    return ModelEntry(
        provider=candidate.provider,
        bedrock_model_id=candidate.bedrock_model_id,
        bedrock_region=candidate.bedrock_region,
        aliases=candidate.aliases,
        wire_protocol=candidate.wire_protocol,
        pricing_key=candidate.pricing_key,
        profile_scope=candidate.profile_scope,
        model_family=candidate.model_family,
        access="entitlement_required",
        jurisdiction_bounded=jurisdiction_bounded,
        jurisdiction=candidate.jurisdiction if jurisdiction_bounded else None,
    )


def _verify(candidate: PromotionCandidate, verdict: Optional[ProbeVerdict], invocation: str) -> None:
    """The three checks activation requires, and only those three: the
    verdict is verified, and its recorded pricing key and wire protocol both
    equal the candidate's. Any mismatch refuses, because it means the thing
    the verdict verified is not the thing being activated."""
    if verdict is None:
        raise ActivationRefused(
            ActivationRefused.VERDICT_NOT_FOUND,
            f"no probe verdict for profile_id={candidate.profile_id!r} "
            f"invocation={invocation!r}",
        )
    if verdict.state != "verified":
        raise ActivationRefused(
            ActivationRefused.VERDICT_NOT_VERIFIED,
            f"probe verdict for profile_id={candidate.profile_id!r} "
            f"invocation={invocation!r} has state={verdict.state!r}, not 'verified'",
        )
    if verdict.pricing_key_at_verification != candidate.pricing_key:
        raise ActivationRefused(
            ActivationRefused.PRICING_KEY_MISMATCH,
            f"verdict for profile_id={candidate.profile_id!r} invocation={invocation!r} "
            f"verified pricing_key={verdict.pricing_key_at_verification!r}, but the "
            f"candidate now names pricing_key={candidate.pricing_key!r}",
        )
    if verdict.wire_protocol_verified != candidate.wire_protocol:
        raise ActivationRefused(
            ActivationRefused.WIRE_PROTOCOL_MISMATCH,
            f"verdict for profile_id={candidate.profile_id!r} invocation={invocation!r} "
            f"verified wire_protocol={verdict.wire_protocol_verified!r}, but the "
            f"candidate now names wire_protocol={candidate.wire_protocol!r}",
        )


def _unobservable_reason(entry: ModelEntry) -> Optional[str]:
    """`None` when the composed registry can see `entry`, else why it cannot.

    Called AFTER the commit and after the local invalidation, so a `None` here
    means the registry this process serves from really does hold what was just
    written. The check exists because every other signal available to a caller
    is indistinguishable between "activated" and "never activated": the commit
    returns nothing, the entry object is what the caller passed in, and the
    registry's own refresh is fail-static by design (it keeps its last good
    activated set and logs, rather than emptying, on a failed read). That
    posture is right and it is also exactly what made a missing
    `dynamodb:Scan` grant produce a 200 nobody could tell from a no-op.

    Matched on `bedrock_model_id` rather than on an alias: the alias map is one
    of several derived indexes, and the identity a caller is owed an answer
    about is the Bedrock model this activation bound, not the name it happened
    to be given.
    """
    from ..models import registry_entries

    try:
        entries = registry_entries()
    except Exception as exc:  # noqa: BLE001 — a readback fault is reported, never raised.
        return f"the composed registry could not be read back: {exc}"
    if any(e.bedrock_model_id == entry.bedrock_model_id for e in entries):
        return None
    return (
        f"the commit succeeded but the composed registry does not list "
        f"bedrock_model_id={entry.bedrock_model_id!r}; this deployment is serving "
        f"from a registry that cannot see its own activation"
    )


def activate_candidate(
    profile_id: str, invocation: str, *, actor: AuthenticatedUser, expected_verified_at: str,
) -> tuple[ModelEntry, Optional[str]]:
    """Activate the promotion candidate named `profile_id`, against the
    probe verdict recorded for `invocation` ("sync" or "stream" — the closed
    set the verdict's own sort key is keyed on; an `invocation` outside that
    set simply finds no verdict and refuses `VERDICT_NOT_FOUND`, since this
    module does not own that vocabulary and re-validating it here would be a
    second, possibly-diverging opinion).

    `expected_verified_at` is the verdict identity the CALLER saw — the
    `verified_at` an operator surface read off the same verdict before
    presenting it for activation. Required, no default: activation is a
    compare-and-set against a SPECIFIC verified moment, not against
    "whatever is currently verified", so there is no reading of "the caller
    didn't say" that is safe to guess at. `_verify` below still checks that a
    verdict exists and is `state="verified"` at all (a cheap, early rejection
    for the common case); the identity match against `expected_verified_at`
    is re-checked, authoritatively, INSIDE the same transaction that commits
    the activation (`_commit_activation`) — a read-then-compare here alone
    would leave exactly the gap between the read and the write that a
    concurrent re-probe or invalidation could land in.

    Gated on `PROMOTE_SCOPE` ("models:promote") checked here, inside the
    domain function, rather than at a FastAPI route dependency: nothing this
    unit is bound to specifies an HTTP surface for activation — no path, no
    method, no request/response shape — and inventing one would be inventing
    an interface the documents are silent on. Any future route that wants to
    expose this over HTTP should call THIS function rather than
    re-implementing the gate, so the check is made exactly once regardless
    of how many callers there end up being.

    Raises `ActivationRefused` (see its reason vocabulary) on any refusal, and
    `ActivationStoreUnavailable` when this deployment cannot READ the activated-
    entry store — checked before anything is written, because an activation this
    deployment could never observe must refuse rather than report success. That
    is not a hypothetical: with the store readable but the `dynamodb:Scan` grant
    for it absent, every activation committed, answered 200 with the entry it had
    just written, and stayed absent from the registry, from routing and from the
    entitlement surface, with the only evidence in a log line.

    On success, persists the derived `ModelEntry` and returns it together with an
    `unobservable_reason`: `None` in the normal case, and otherwise prose saying
    the commit landed but the registry cannot see it. A tuple rather than a
    refusal because the write DID commit — the same reading
    `mvp.admin_entitlements.grant_entitlement` applies to its own
    `(grant, audit_dropped_reason)`, and for the same reason: reporting failure
    over a committed write is the same lie in the other direction. The caller
    decides how to surface it; what it must not do is stay silent. The commit
    (`_commit_activation`) also claims every public identifier this entry
    makes reachable, in the SAME transaction, so two candidates that each
    independently pass every check above cannot both go live for a name they
    only turn out to share once activated — see that function's own
    docstring for the race this closes.
    """
    if not user_has_permission(actor, PROMOTE_SCOPE):
        raise ActivationRefused(
            ActivationRefused.NOT_PERMITTED,
            f"actor {actor.user_id!r} lacks {PROMOTE_SCOPE!r}",
        )
    candidate = get_promotion_candidate(profile_id)
    if candidate is None:
        raise ActivationRefused(
            ActivationRefused.CANDIDATE_NOT_FOUND,
            f"no promotion candidate for profile_id={profile_id!r}",
        )
    verdict = get_probe_verdict(profile_id, invocation)
    _verify(candidate, verdict, invocation)
    if verdict.verified_at != expected_verified_at:
        raise ActivationRefused(
            ActivationRefused.VERDICT_IDENTITY_MISMATCH,
            f"verdict for profile_id={profile_id!r} invocation={invocation!r} "
            f"was verified at {verdict.verified_at!r}, not the "
            f"expected_verified_at={expected_verified_at!r} the caller named",
        )

    record = get_discovered_record(profile_id)
    if record is None:
        # A later discovery pass no longer seeing this profile is an
        # already-known gap this unit must not deepen, not one it closes —
        # but it must not silently guess `jurisdiction_bounded` either, so
        # it refuses by name instead.
        raise ActivationRefused(
            ActivationRefused.RECORD_NOT_FOUND,
            f"no discovered record for profile_id={profile_id!r}",
        )

    # Read the store before writing to it. `list_activated_entries` is the exact
    # read the composed registry performs on every refresh, so a deployment that
    # cannot serve this activation fails here, by name, with nothing committed —
    # instead of committing, answering 200, and leaving the model unreachable.
    # Raises `ActivationStoreUnavailable`, which every caller already handles.
    list_activated_entries()

    entry = _build_entry(candidate, jurisdiction_bounded=record.jurisdiction_bounded)
    activated = ActivatedEntry(
        profile_id=profile_id,
        invocation=invocation,
        entry=entry,
        activated_at=_now_iso(),
        activated_by=actor.user_id,
    )
    _commit_activation(activated, expected_verified_at=expected_verified_at)
    # The TTL carries other replicas. It must not carry this one: a caller who
    # just activated a model and immediately asks the registry about it would
    # otherwise be told it does not exist, for up to a full window, by the very
    # process that wrote it.
    from ..models import invalidate_composed_registry

    invalidate_composed_registry()
    return entry, _unobservable_reason(entry)
