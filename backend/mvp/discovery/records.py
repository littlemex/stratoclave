"""E1 — the discovered record: PR2's local model of one Bedrock inference profile.

One record per `(profile_id, observation_scope)`. In practice a single deployment
observes from exactly one account, through exactly one control-plane region, at a
time, so the store below keys on `profile_id` alone and each reconciliation pass
UPSERTS the one record that profile owns — the same "full replace, one row per
identity" shape `mvp.routing.config`'s `CONFIG#ROUTING` item already uses in this
table, and for the same reason: a discovered record is a fresh, complete snapshot
of what the last pass saw, not an append-only log of every pass that ever ran.
`observation_scope` is still carried on every record (not folded into the key)
because it is what makes "how current, and against which account and region, is
this snapshot" a fact on the record instead of an assumption a reader makes.

Storage is a separate item type in the existing `stratoclave-user-tenants` table,
never a new one:

    user_id = "DISCOVERED#{profile_id}", tenant_id = "SYSTEM"

This is sound against how the table is actually keyed and read. The table's
primary key is `(user_id, tenant_id)` with a `tenant-id-index` GSI on
`(tenant_id, user_id)` (`iac/lib/dynamodb-stack.ts`), and it already carries two
other reserved-prefix item types beside the real per-user membership rows this
table was built for: `mvp.routing.config`'s `CONFIG#ROUTING` (keyed by the real
`tenant_id`) and `mvp.admin_entitlements`'s `ENTITLEMENT#{model_family}#
{profile_scope}` (also keyed by the real `tenant_id`, listed via the GSI narrowed
to that prefix). A discovered record is neither tenant-owned nor user-owned — it
is a fact about the ACCOUNT's Bedrock catalogue, observed once per reconciliation
pass — so `tenant_id = "SYSTEM"` is the one choice that does not misattribute it
to a tenant that happens to be first, and it composes with the existing
`tenant-id-index` GSI exactly the way `list_entitlements` already lists a
tenant's grants: query the GSI for `tenant_id = "SYSTEM"`, narrowed to
`DISCOVERED#` the same way entitlement rows are narrowed to `ENTITLEMENT#`. No
new table, no new index, and — because the reserved prefix is disjoint from every
Cognito `sub` and from `CONFIG#`/`ENTITLEMENT#` — no risk of a discovered record
being misread as a membership row, a routing config, or a grant.

The pool money counters (`pool_reserved_microusd`, `pool_settled_microusd`) live
on a DIFFERENT table (`dynamo.tenant_budgets`, resolved by
`tenant_budgets_table_name()`), never on this one, so nothing in this module
touches the write-discipline guard's axioms — there is no counter attribute for
a string literal here to accidentally name.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Optional

from boto3.dynamodb.conditions import Key as boto3_key
from botocore.exceptions import ClientError

from dynamo.client import get_dynamodb_resource, user_tenants_table_name

from ..pricing_feeds.dimensions import (
    _GEO_PROFILE_PREFIXES as _GEO_ID_PREFIXES,
    _GLOBAL_PROFILE_PREFIX as _GLOBAL_ID_PREFIX,
    base_model_id,
)

SCHEMA_VERSION = 1

_DISCOVERED_PREFIX = "DISCOVERED#"
SYSTEM_TENANT_ID = "SYSTEM"

# Same one-off spelling exception as `mvp.models._GOV_ID_PREFIX`: every other
# scope token is the id prefix with its trailing dot removed, but AWS's GovCloud
# prefix is "us-gov." and the vocabulary's own word for it is "gov", not "us-gov".
_GOV_ID_PREFIX = "us-gov."


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Blocker types, exhaustively (see the module docstring in `mvp.discovery.gates`
# for which check mints which type, and under what condition). `protocol_
# unverified` arrives with PR3's probe — invoking the model is the only thing
# that can observe whether its wire protocol works, and nothing in this PR
# invokes a model. There is no `alias_unpublished` because a profile's id IS
# its name, so nothing about an unpublished alias can block it.
#
# `no_model_access` and `no_agreement_offer` are two different facts about the
# SAME call (`bedrock:ListFoundationModelAgreementOffers`), not two names for
# one: `no_model_access` is "the mechanism exists and this account has not
# been granted it" (the fix is a click in the provider console) —
# `ListFoundationModelAgreementOffers` already answers this distinctly, as
# "not authorized to invoke this API operation" — while `no_agreement_offer`
# is "the mechanism does not exist for this model at all" (`Agreement not
# supported for this model`; no click fixes that). Collapsing the two sends an
# operator to click a button that cannot help, or tells them nothing when a
# button would.
BLOCKER_TYPES = frozenset({
    "unsupported_output_modality",
    "no_agreement_offer",
    "no_model_access",
    "no_token_pricing",
    "price_dimensions_unknown",
})


@dataclass(frozen=True)
class Blocker:
    """One reason a discovered profile is not usable yet.

    `type` is one of `BLOCKER_TYPES`; `subtype` narrows the reason within that
    type without growing the enumerated vocabulary every time a gate learns a
    new way to fail — see `mvp.discovery.gates` for the full per-gate mapping.
    `evidence` carries the raw string a live API answered, so auditing a
    blocker never requires replaying the call that produced it.

    `first_seen`/`last_seen` are ISO-8601 instants, and they default rather
    than being required: a blocker's identity is `(type, subtype)` (see
    `merge_blockers` below, which matches on exactly that pair), and the
    timestamps are provenance the STORE stamps, not a fact a caller that only
    means "a blocker of this kind" should have to invent a clock to supply.
    `Blocker(type=..., subtype=..., evidence=...)` is complete on its own — a
    gate has no memory of a previous pass, so an omitted pair of timestamps
    means "observed right now", and both default to the SAME instant (never
    two independent clock reads, which could disagree at sub-second
    resolution and silently break `first_seen == last_seen`, the property
    `mvp.discovery.reconcile` uses to tell a freshly-appeared blocker from one
    this pass merely reconfirmed). There is exactly one way to build a
    `Blocker`: this constructor. A second helper that also stamps would be a
    second answer to "what does a fresh blocker look like", and two answers
    is how they drift.
    """

    type: str
    subtype: str
    evidence: str
    first_seen: str = ""
    last_seen: str = ""

    def __post_init__(self) -> None:
        if self.type not in BLOCKER_TYPES:
            raise ValueError(
                f"unknown blocker type {self.type!r}; must be one of "
                f"{sorted(BLOCKER_TYPES)}"
            )
        if not self.first_seen and not self.last_seen:
            stamp = _now_iso()
            object.__setattr__(self, "first_seen", stamp)
            object.__setattr__(self, "last_seen", stamp)
        elif not self.first_seen:
            object.__setattr__(self, "first_seen", self.last_seen)
        elif not self.last_seen:
            object.__setattr__(self, "last_seen", self.first_seen)


def merge_blockers(previous: tuple[Blocker, ...],
                   fresh: tuple[Blocker, ...]) -> tuple[Blocker, ...]:
    """Carry `first_seen` forward for a blocker this pass saw again; drop one
    this pass did not see at all (the underlying gate stopped tripping, so the
    blocker is resolved, not merely unmentioned).

    Matched on `(type, subtype)`, not on `evidence` — the exact string a live
    API answers can change (a message gets reworded, an error code drifts)
    without the REASON changing, and treating that as a new blocker would
    reset `first_seen` on every such wording change.
    """
    by_key = {(b.type, b.subtype): b for b in previous}
    merged = []
    for blocker in fresh:
        prior = by_key.get((blocker.type, blocker.subtype))
        if prior is not None:
            merged.append(Blocker(type=blocker.type, subtype=blocker.subtype,
                                  evidence=blocker.evidence,
                                  first_seen=prior.first_seen,
                                  last_seen=blocker.last_seen))
        else:
            merged.append(blocker)
    return tuple(merged)


@dataclass(frozen=True)
class ObservationScope:
    """An inventory is account-, permission-, region- and time-specific, not
    the Bedrock universe. Every discovered record carries the scope it was
    observed under so a reader never mistakes "what this account could see,
    just now" for "what Bedrock offers everywhere"."""

    account: str
    region: str
    credentials_fingerprint: str
    observed_at: str


def credentials_fingerprint(arn: str) -> str:
    """A stable, non-reversible identifier for the credentials that made an
    observation, so two reconciliation passes can tell "same identity, still
    observing" from "the role that scanned this changed" without this record
    ever holding a raw ARN's full account/role detail as the ONLY source of
    that fact. Same construction as `dynamo.sso_nonces.fingerprint`: SHA-256
    hex, deterministic, and of a value that is already not a secret (an STS
    caller identity ARN), fingerprinted for uniform handling rather than for
    concealment.
    """
    return hashlib.sha256(arn.strip().encode("utf-8")).hexdigest()


def profile_scope_from_id(raw_id: str) -> tuple[str, bool]:
    """Parse the geography token off an inference-profile id's own leading
    prefix. Returns `(profile_scope, jurisdiction_bounded)`.

    A cross-check, never a permanent contract: this build recognises the
    prefixes `pricing_feeds.dimensions` already recognises (the same set
    `mvp.models` derives `PROFILE_SCOPES` from), plus GovCloud's one-off
    spelling. A prefix outside that set is not guessed at or dropped — the
    literal leading segment becomes the scope token, and the profile is still
    treated as bounded to SOMETHING, because a provider adding a new geography
    tomorrow must not either crash this pass or read as if it always meant
    unbounded. `global.` is the one prefix that is genuinely unbounded, so it
    alone answers `jurisdiction_bounded=False`.
    """
    if raw_id.startswith(_GLOBAL_ID_PREFIX):
        return "global", False
    for prefix in _GEO_ID_PREFIXES:
        if raw_id.startswith(prefix):
            token = "gov" if prefix == _GOV_ID_PREFIX else prefix[:-1]
            return token, True
    head, sep, _ = raw_id.partition(".")
    return (head if sep else raw_id), True


def provider_from_id(raw_id: str) -> str:
    """The provider token: the id's own leading dotted segment once any
    recognised geography prefix is stripped — `us.anthropic.claude-opus-5` ->
    `anthropic`, matching the lowercase, id-derived spelling the discovery
    facts were measured in (`stability`, `twelvelabs`, `xai`, ...), not a
    display name a modality lookup might answer instead."""
    stripped = base_model_id(raw_id)
    provider, _, _ = stripped.partition(".")
    return provider or stripped


def model_family_from_id(raw_id: str) -> str:
    """The model identity a `profile_scope` varies without varying: the id
    with both its geography prefix AND its provider segment removed."""
    stripped = base_model_id(raw_id)
    _, _, rest = stripped.partition(".")
    return rest or stripped


def destination_regions_from_models(models: Any) -> tuple[str, ...]:
    """Every region this profile's traffic could land in, from the profile's
    own `models[]` — plural, one ARN per destination.

    An ARN's region segment (`arn:aws:bedrock:<region>::foundation-model/...`)
    is read positionally rather than by name because that is the shape the API
    returns, and a `global.` profile carries a REGION-LESS arn (an empty
    segment) alongside the region-bound ones — kept as the empty string rather
    than dropped, because dropping it is exactly how "this profile is
    unbounded" would stop being visible in the data. Order is preserved and
    duplicates are folded (a profile does not usually repeat a destination,
    but nothing here assumes that of the API).
    """
    regions: list[str] = []
    seen: set[str] = set()
    for model in models or ():
        arn = model.get("modelArn") if isinstance(model, Mapping) else None
        if not isinstance(arn, str):
            continue
        parts = arn.split(":", 5)
        region = parts[3] if len(parts) > 3 else ""
        if region not in seen:
            seen.add(region)
            regions.append(region)
    return tuple(regions)


def _json_safe(value: Any) -> Any:
    """Recursively normalise a value into the vocabulary DynamoDB's `Table`
    resource actually accepts. Two Python types a real API response (or any
    other source feeding `_to_item`) can carry are ones it refuses outright.
    `datetime` (boto3 parses a timestamp field into one) is converted to an
    ISO-8601 string, the same shape every other instant in this record is
    already in. `float` — a score, a ratio, any provider number that is not
    an integer — raises `TypeError: Float types are not supported. Use
    Decimal types instead.` from botocore's own serialiser the moment this
    reaches a real `put_item`, which is invisible to an in-memory `_to_item`/
    `_from_item` round trip and only surfaces once boto3 actually serialises
    the call: converted via `str` first, the same construction `per_mtok`
    already uses elsewhere in this package, so a float is preserved as the
    decimal digits the source actually published rather than its binary
    approximation. `Mapping` and `list`/`tuple` recurse into every value they
    hold, so calling this once on a whole item reaches every value botocore
    will also reach — there is no need to call it again on any one field.
    Every other type (`str`, `int`, `bool`, `None`, `Decimal`) is returned
    unchanged; `bool` in particular is never rewritten, because the only
    `isinstance` check above it is `float`, and a `bool` is never a `float`
    even though `int` is one of its ancestors.
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


@dataclass(frozen=True)
class DiscoveredRecord:
    """PR2's local model of one Bedrock inference profile, as observed by one
    reconciliation pass. Grants nothing, loads nothing — a fact, recorded."""

    profile_id: str
    provider: str
    profile_scope: str
    model_family: str
    jurisdiction_bounded: bool
    destination_regions: tuple[str, ...]
    invocation_region: str
    raw_id: str
    raw_payload: Mapping[str, Any]
    observation_scope: ObservationScope
    blockers: tuple[Blocker, ...] = field(default_factory=tuple)


class DiscoveredRecordStoreUnavailable(Exception):
    """A read or write of the discovered-record store failed and nothing was
    read or written. Raised rather than answered as "not found" or silently
    dropped — an unreadable store is not evidence that a profile does not
    exist, and `reconcile.py --strict` needs to be able to tell the two apart.
    """


def _table():
    return get_dynamodb_resource().Table(user_tenants_table_name())


def _pk(profile_id: str) -> str:
    return f"{_DISCOVERED_PREFIX}{profile_id}"


def _to_item(record: DiscoveredRecord) -> dict[str, Any]:
    scope = record.observation_scope
    item = {
        "user_id": _pk(record.profile_id),
        "tenant_id": SYSTEM_TENANT_ID,
        "schema_version": SCHEMA_VERSION,
        "profile_id": record.profile_id,
        "provider": record.provider,
        "profile_scope": record.profile_scope,
        "model_family": record.model_family,
        "jurisdiction_bounded": bool(record.jurisdiction_bounded),
        "destination_regions": list(record.destination_regions),
        "invocation_region": record.invocation_region,
        "raw_id": record.raw_id,
        "raw_payload": record.raw_payload,
        "observation_scope": {
            "account": scope.account,
            "region": scope.region,
            "credentials_fingerprint": scope.credentials_fingerprint,
            "observed_at": scope.observed_at,
        },
        "blockers": [
            {
                "type": b.type,
                "subtype": b.subtype,
                "evidence": b.evidence,
                "first_seen": b.first_seen,
                "last_seen": b.last_seen,
            }
            for b in record.blockers
        ],
    }
    return _json_safe(item)


def _from_item(item: Mapping[str, Any]) -> Optional[DiscoveredRecord]:
    schema = item.get("schema_version")
    try:
        schema = int(schema)
    except (TypeError, ValueError):
        schema = None
    if schema != SCHEMA_VERSION:
        # A schema this build does not know could mean anything, including a
        # field changing meaning. Same posture as `pricing_feeds.snapshot`:
        # skip the row rather than guess at it.
        return None
    scope_raw = item.get("observation_scope") or {}
    if not isinstance(scope_raw, Mapping):
        return None
    blockers = []
    for raw in item.get("blockers") or ():
        if not isinstance(raw, Mapping):
            continue
        try:
            blockers.append(Blocker(
                type=str(raw.get("type")),
                subtype=str(raw.get("subtype")),
                evidence=str(raw.get("evidence") or ""),
                first_seen=str(raw.get("first_seen") or ""),
                last_seen=str(raw.get("last_seen") or ""),
            ))
        except ValueError:
            # An unrecognised blocker type is dropped rather than failing the
            # whole record: the record is still a real observation, and a
            # future BLOCKER_TYPES member should not make an old record
            # unreadable.
            continue
    try:
        return DiscoveredRecord(
            profile_id=str(item.get("profile_id") or ""),
            provider=str(item.get("provider") or ""),
            profile_scope=str(item.get("profile_scope") or ""),
            model_family=str(item.get("model_family") or ""),
            jurisdiction_bounded=bool(item.get("jurisdiction_bounded")),
            destination_regions=tuple(str(r) for r in item.get("destination_regions") or ()),
            invocation_region=str(item.get("invocation_region") or ""),
            raw_id=str(item.get("raw_id") or ""),
            raw_payload=dict(item.get("raw_payload") or {}),
            observation_scope=ObservationScope(
                account=str(scope_raw.get("account") or ""),
                region=str(scope_raw.get("region") or ""),
                credentials_fingerprint=str(scope_raw.get("credentials_fingerprint") or ""),
                observed_at=str(scope_raw.get("observed_at") or ""),
            ),
            blockers=tuple(blockers),
        )
    except Exception:  # noqa: BLE001 — a malformed row is skipped, not fatal.
        return None


def get_discovered_record(profile_id: str) -> Optional[DiscoveredRecord]:
    """Read the one record `profile_id` owns, consistently."""
    try:
        resp = _table().get_item(
            Key={"user_id": _pk(profile_id), "tenant_id": SYSTEM_TENANT_ID},
            ConsistentRead=True,
        )
    except ClientError as exc:
        raise DiscoveredRecordStoreUnavailable(
            f"discovered-record store unreachable reading profile_id={profile_id!r}: {exc}"
        ) from exc
    item = resp.get("Item")
    return _from_item(item) if item else None


def list_discovered_records() -> list[DiscoveredRecord]:
    """Every discovered record, via the table's existing `tenant-id-index`
    GSI (no new table, no new index) — the same pattern
    `admin_entitlements.list_entitlements` already uses for its own reserved
    prefix on this table."""
    try:
        records: list[DiscoveredRecord] = []
        kwargs: dict[str, Any] = {
            "IndexName": "tenant-id-index",
            "KeyConditionExpression": (
                boto3_key("tenant_id").eq(SYSTEM_TENANT_ID)
                & boto3_key("user_id").begins_with(_DISCOVERED_PREFIX)
            ),
        }
        while True:
            resp = _table().query(**kwargs)
            for item in resp.get("Items", []):
                parsed = _from_item(item)
                if parsed is not None:
                    records.append(parsed)
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
    except ClientError as exc:
        raise DiscoveredRecordStoreUnavailable(
            f"discovered-record store unreachable listing records: {exc}"
        ) from exc
    return records


def put_discovered_record(record: DiscoveredRecord) -> None:
    """Full-replace write of the one record `record.profile_id` owns.

    Unconditional, unlike `admin_entitlements`'s create-once grant row: a
    discovered record is a fresh, complete snapshot of what THIS pass saw, so
    the whole point of writing it again is to replace what the previous pass
    saw — `merge_blockers` (above) is what carries `first_seen` forward across
    that replace, not a conditional write.
    """
    try:
        _table().put_item(Item=_to_item(record))
    except ClientError as exc:
        raise DiscoveredRecordStoreUnavailable(
            f"discovered-record store unreachable writing profile_id={record.profile_id!r}: {exc}"
        ) from exc
