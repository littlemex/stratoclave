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

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Optional

from botocore.exceptions import ClientError

from core.logging import get_logger
from dynamo.client import get_dynamodb_resource, promotion_candidates_table_name

from ..authz import user_has_permission
from ..deps import AuthenticatedUser
from ..models import ModelEntry
from .promotion import PromotionCandidate, get_promotion_candidate
from .records import get_discovered_record
from .verdict import ProbeVerdict, get_probe_verdict

logger = get_logger(__name__)

SCHEMA_VERSION = 1

_ACTIVE_PREFIX = "ACTIVE#"
_ACTIVE_SK = "ACTIVE"

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

    Seven reasons, not six: `RECORD_NOT_FOUND` was added alongside the
    `jurisdiction_bounded` fix below, once building a correct entry started
    depending on the discovered record too, not just the candidate and the
    verdict. The other six are unchanged.
    """

    NOT_PERMITTED = "not_permitted"
    CANDIDATE_NOT_FOUND = "candidate_not_found"
    RECORD_NOT_FOUND = "record_not_found"
    VERDICT_NOT_FOUND = "verdict_not_found"
    VERDICT_NOT_VERIFIED = "verdict_not_verified"
    PRICING_KEY_MISMATCH = "pricing_key_mismatch"
    WIRE_PROTOCOL_MISMATCH = "wire_protocol_mismatch"
    REASONS = frozenset({
        NOT_PERMITTED, CANDIDATE_NOT_FOUND, RECORD_NOT_FOUND, VERDICT_NOT_FOUND,
        VERDICT_NOT_VERIFIED, PRICING_KEY_MISMATCH, WIRE_PROTOCOL_MISMATCH,
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
    """
    try:
        _table().put_item(Item=_to_item(activated))
    except ClientError as exc:
        raise ActivationStoreUnavailable(
            f"activated-entry store unreachable writing profile_id={activated.profile_id!r}: {exc}"
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


def activate_candidate(profile_id: str, invocation: str, *, actor: AuthenticatedUser) -> ModelEntry:
    """Activate the promotion candidate named `profile_id`, against the
    probe verdict recorded for `invocation` ("sync" or "stream" — the closed
    set the verdict's own sort key is keyed on; an `invocation` outside that
    set simply finds no verdict and refuses `VERDICT_NOT_FOUND`, since this
    module does not own that vocabulary and re-validating it here would be a
    second, possibly-diverging opinion).

    Gated on `PROMOTE_SCOPE` ("models:promote") checked here, inside the
    domain function, rather than at a FastAPI route dependency: nothing this
    unit is bound to specifies an HTTP surface for activation — no path, no
    method, no request/response shape — and inventing one would be inventing
    an interface the documents are silent on. Any future route that wants to
    expose this over HTTP should call THIS function rather than
    re-implementing the gate, so the check is made exactly once regardless
    of how many callers there end up being.

    Raises `ActivationRefused` (see its reason vocabulary) on any refusal.
    On success, persists the derived `ModelEntry` and returns it; the caller
    does not need to also call `put_activated_entry` — this function is the
    one place that does the whole thing.
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

    entry = _build_entry(candidate, jurisdiction_bounded=record.jurisdiction_bounded)
    put_activated_entry(ActivatedEntry(
        profile_id=profile_id,
        invocation=invocation,
        entry=entry,
        activated_at=_now_iso(),
        activated_by=actor.user_id,
    ))
    # The TTL carries other replicas. It must not carry this one: a caller who
    # just activated a model and immediately asks the registry about it would
    # otherwise be told it does not exist, for up to a full window, by the very
    # process that wrote it.
    from ..models import invalidate_composed_registry

    invalidate_composed_registry()
    return entry
