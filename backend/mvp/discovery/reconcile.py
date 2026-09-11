"""E2 — the reconciliation: walk a live account's inference profiles, run the
E3 gates, and write E1 records. Follows `mvp.pricing_feeds.fetch`'s own
conventions exactly, for the same reason that module states them: dry run by
default and safe against production credentials, `--apply` writes, `--strict`
exits non-zero on a finding a person should see and is the unattended deploy
gate.

    python -m mvp.discovery.reconcile                 # dry run: discover and print
    python -m mvp.discovery.reconcile --apply         # discover and store
    python -m mvp.discovery.reconcile --strict        # exit 2 on a finding
    python -m mvp.discovery.reconcile --json          # machine-readable report

A dry run touches `ListInferenceProfiles`, `GetFoundationModel`,
`ListFoundationModelAgreementOffers`, `GetCallerIdentity`, and the discovered-
record store's READ — never its write — so it is safe to run against
production credentials, exactly like `fetch.py --apply`-less runs.

This module discovers and records. It grants nothing, probes nothing (no
Converse/InvokeModel call is ever made — see `mvp.discovery.gates` for the
control-plane-only calls this pass makes), and enforces nothing: nothing it
writes is read by the reserve path, a route, or `permissions.json` today.

Findings extend `fetch.py`'s own model of what `--strict` is for — a change or
an operational fault a person should see before an unattended deploy proceeds
— rather than reusing its six tokens verbatim, because discovery's findings
are facts about a different thing (a profile's Bedrock catalogue entry, not a
priced key) and forcing them into `fetch.py`'s literal vocabulary would either
misname them or leave real findings unnamed. The four tokens below are this
module's own reviewed set, held to the same standard: named, documented, and
checked in the order this docstring lists them.

  - `actionable_blocker`— some profile in THIS pass carries a blocker of an
                          actionable kind (see below) — every pass it is
                          still there, not only the first, because the
                          problem has not gone away just because it was
                          already reported once. Also fires when the pass
                          itself never got that far: if this pass's Bedrock
                          client could not be built, NO profile was ever
                          listed, so the finding is not any profile's — it
                          is the pass's own `no_agreement_offer`/
                          `client_unavailable` (the same token
                          `gates.fetch_rate_card` already mints for one
                          unreadable rate card, reused rather than renamed —
                          see `PassResult.observation_blocker`), and it is
                          checked exactly as unconditionally as every
                          profile's blockers below.
  - `profiles_truncated`— `ListInferenceProfiles` did not finish (a page
                          request failed); the discovered set is a subset of
                          the account's actual catalogue this pass.
  - `store_unavailable` — a read or write of the discovered-record store
                          failed for at least one profile.
  - `apply_incomplete`  — `--apply` was requested and at least one record that
                          this pass successfully built was not, in the end,
                          written.

Not every blocker earns `actionable_blocker`, and this is a judgement about
what a deploy gate is FOR, stated once here rather than left to be
reverse-engineered from the code: a gate a person cannot act on, that will
never clear, trains everyone to ignore it — which is a worse failure mode
than not gating on it at all, because a permanently-red gate hides the NEXT
real finding beside it. So the split is by whether the underlying fact can
change through action available to this account, not by how alarming the
name sounds:

  - ACTIONABLE (fails `--strict` every pass it is present):
    `price_dimensions_unknown` — a rate-card row this build cannot parse.
    Directly gates the pricing key's correctness, so quarantining the row and
    exiting 0 would tell an unattended deploy everything is fine while a
    model's price is unknown; `no_model_access` — an account permission this
    account does not hold, fixable by a console click; `no_agreement_offer`
    EXCEPT its `not_marketplace_metered` subtype (below) — a call that should
    have answered something and did not (`client_unavailable`, `call_failed`,
    `empty_rate_card`) is an anomaly worth a person's attention, not a fact
    about the model.
  - PERMANENT (never fails `--strict`, on any pass): `unsupported_output_
    modality` — a model whose output is not `TEXT` will never become one;
    `no_token_pricing` — a rate card that prices nothing per token is a fact
    about that SKU, not a transient gap; `no_agreement_offer`'s
    `not_marketplace_metered` subtype specifically — this is the normal,
    permanent shape for every AWS-billed family this account can see (Nova,
    Titan, Llama, Mistral, ...), which is most of a full-account scan, and it
    is NOT evidence the model has no price anywhere (the handoff's own
    caution: Price List may still have it). Gating `--strict` on the TYPE
    `no_agreement_offer` as a whole would make an ordinary account-wide
    discovery pass permanently red for a reason nobody can fix through this
    API, which is exactly the failure mode above — so this one subtype is
    carved out of an otherwise-actionable type rather than the type being
    graded as a whole.

`--strict` here and `mvp.pricing_feeds.fetch --strict` are two commands, not
one — this module answers "does this account's Bedrock catalogue have a new
problem", `fetch.py` answers "did a price move in a way nobody decided" — and
NEITHER covers the other's findings. Running one is not a substitute for the
other, and an unattended deploy that only gates on one of them is only half
gated.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Optional

from core.logging import get_logger

from .gates import fetch_rate_card, gate_agreement_exists, gate_card_prices_tokens, gate_output_is_text
from .pricing_key import key_for_selection
from .records import (
    Blocker,
    DiscoveredRecord,
    DiscoveredRecordStoreUnavailable,
    ObservationScope,
    credentials_fingerprint,
    destination_regions_from_models,
    get_discovered_record,
    merge_blockers,
    model_family_from_id,
    profile_scope_from_id,
    provider_from_id,
    put_discovered_record,
)
from ..pricing_feeds.base import STRATOCLAVE_REGION_ENV
from ..pricing_feeds.dimensions import (
    EXCLUDED,
    base_model_id,
    parse_agreement_dimension,
    per_mtok,
    scope_for_model_id,
    select,
)

logger = get_logger(__name__)

_DEFAULT_REGION = "us-east-1"

# `dimensions.select()` hardcodes "only `standard` is ever charged" today (see
# its own module docstring). Named here, once, rather than re-derived from
# `dimensions.MODES` (which also lists `batch`/`flex`/`priority` — modes that
# exist to be RECOGNISED and excluded, not modes this pass may treat as
# enabled) so the one place this policy is stated for discovery's own pricing
# key matches the one place it is enforced for the charging selector.
ENABLED_MODES: frozenset[str] = frozenset({"standard"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client(service: str, *, region: str, injected: Optional[Any] = None):
    if injected is not None:
        return injected
    import boto3

    return boto3.client(service, region_name=region)


@dataclass
class PassResult:
    """Everything one reconciliation pass produced, before any write.

    `observation_blocker` is set instead of every other field when this pass
    could not even build the Bedrock client `run_pass` needs to look at
    anything — a bad region, missing or broken credentials, a service name
    the installed botocore does not know. It is the SAME `no_agreement_offer`/
    `client_unavailable` blocker `gates.fetch_rate_card` already mints when
    ITS client cannot be built (see that function's own docstring) — reused
    rather than reinvented, because the fact is identical: a client failed to
    construct, so there is no card, no gate, nothing to report. The
    difference is only where it attaches: `fetch_rate_card`'s copy names one
    profile's rate card as unreadable; this one names the whole pass as
    unable to observe anything, before a single profile was ever listed, so
    it cannot be pinned to any `DiscoveredRecord.blockers` — there are none
    yet. `_actionable_blocker_findings` below reads this field unconditionally,
    the same way it reads every record's blockers, so `--strict` fails on it
    every pass it recurs, exactly like any other actionable blocker."""

    records: list[DiscoveredRecord] = field(default_factory=list)
    pricing_keys: dict[str, Optional[str]] = field(default_factory=dict)
    profiles_truncated: bool = False
    discovery_errors: list[str] = field(default_factory=list)
    observation_blocker: Optional[Blocker] = None


def _model_details(
    bedrock, base_id: str, cache: dict[str, tuple[Optional[Mapping[str, Any]], Optional[str]]],
) -> tuple[Optional[Mapping[str, Any]], Optional[str]]:
    """`GetFoundationModel`'s `modelDetails`, cached per base model id — many
    profiles (every geography variant of one model) share one underlying id,
    and this pass must not ask the same question once per profile."""
    if base_id in cache:
        return cache[base_id]
    try:
        response = bedrock.get_foundation_model(modelIdentifier=base_id)
        details = response.get("modelDetails") or {}
        result: tuple[Optional[Mapping[str, Any]], Optional[str]] = (details, None)
    except Exception as exc:  # noqa: BLE001 — recorded as a blocker, not fatal.
        result = (None, str(exc))
    cache[base_id] = result
    return result


def _list_inference_profiles(bedrock) -> tuple[list[Mapping[str, Any]], bool]:
    """Every inference profile this account's credentials can see from this
    endpoint, paginated in full. `(summaries, truncated)` — `truncated` is set
    when a page request itself failed, so the caller can tell "the account has
    exactly these profiles" from "this pass could not finish looking"."""
    summaries: list[Mapping[str, Any]] = []
    token: Optional[str] = None
    while True:
        kwargs: dict[str, Any] = {}
        if token:
            kwargs["nextToken"] = token
        try:
            response = bedrock.list_inference_profiles(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("discovery_list_inference_profiles_failed", error=str(exc))
            return summaries, True
        summaries.extend(response.get("inferenceProfileSummaries") or ())
        token = response.get("nextToken")
        if not token:
            break
    return summaries, False


def _build_card(
    rate_card: list[Mapping[str, Any]],
) -> tuple[dict[tuple[Optional[str], Any], Decimal], list[str]]:
    """Parse every row of a rate card into `dimensions.select()`'s own `Card`
    shape, and separately name every row that did not parse at all.

    A row `parse_agreement_dimension` answers `EXCLUDED` for is dropped
    silently (recognised, and not a token price — reserved throughput, a
    non-default cache TTL, ...). A row it answers `None` for, or whose price/
    unit `per_mtok` refuses, is named in the returned list rather than
    dropped: `select()` would simply never see it, and a card missing a row
    it could not read must not be mistaken for a card that was fully
    understood — that is exactly the distinction `price_dimensions_unknown`
    exists to preserve (see `mvp.discovery.pricing_key`'s module docstring).
    """
    card: dict[tuple[Optional[str], Any], Decimal] = {}
    unknown: list[str] = []
    for row in rate_card:
        dimension = row.get("dimension") if isinstance(row, Mapping) else None
        if not dimension:
            continue
        parsed = parse_agreement_dimension(dimension)
        if parsed is EXCLUDED:
            continue
        if parsed is None:
            unknown.append(str(dimension))
            continue
        region, slot = parsed
        unit = row.get("unit")
        value = per_mtok(row.get("price"), unit) if unit else None
        if value is None:
            unknown.append(str(dimension))
            continue
        key = (region, slot)
        previous = card.get(key)
        if previous is None or value > previous:
            card[key] = value
    return card, unknown


def build_record(
    summary: Mapping[str, Any], *, bedrock, invocation_region: str,
    observation_scope: ObservationScope,
    model_cache: dict[str, tuple[Optional[Mapping[str, Any]], Optional[str]]],
) -> tuple[DiscoveredRecord, Optional[str], Optional[str]]:
    """One profile summary -> `(record, pricing_key_or_None, note_or_None)`.

    The pricing key is returned alongside the record rather than stored on
    it: PR2 records and gates, and a persisted key is one step closer to
    "loadable" than this PR is scoped to go (see the package docstring). It is
    still computed here — never a second time downstream — because the
    `price_dimensions_unknown` blocker it can produce belongs on the record.

    `note` carries an operational fault this pass hit while building the
    record — today, only a failed `GetFoundationModel` call — for the
    caller's `discovery_errors`, NOT for `blockers`. `GetFoundationModel`
    failing is not one of the five modelled facts about a profile's
    usability (`mvp.discovery.gates`'s settled mapping has no "the describe
    call itself failed" case, and guessing which of the five it resembles is
    exactly the mistake this taxonomy exists to rule out): it says something
    went wrong with THIS pass's ability to look, not something wrong with the
    model. When it happens, `gate_output_is_text` is simply not run for this
    profile — no modality blocker, because there is no modality fact to
    report — rather than minting a guess.
    """
    raw_id = str(summary.get("inferenceProfileId") or "")
    profile_scope, jurisdiction_bounded = profile_scope_from_id(raw_id)
    provider = provider_from_id(raw_id)
    model_family = model_family_from_id(raw_id)
    destination_regions = destination_regions_from_models(summary.get("models"))
    base_id = base_model_id(raw_id)

    blockers: list[Blocker] = []
    note: Optional[str] = None

    details, details_error = _model_details(bedrock, base_id, model_cache)
    if details is None:
        note = f"GetFoundationModel({base_id}) failed: " + (
            details_error or "returned nothing"
        )
    else:
        modality_blocker = gate_output_is_text(details)
        if modality_blocker is not None:
            blockers.append(modality_blocker)

    rate_card, agreement_blocker = fetch_rate_card(base_id, client=bedrock)
    pricing_key: Optional[str] = None
    if agreement_blocker is not None:
        blockers.append(agreement_blocker)
    else:
        tokens_blocker = gate_card_prices_tokens(rate_card or [])
        if tokens_blocker is not None:
            blockers.append(tokens_blocker)
        else:
            card, unknown_dimensions = _build_card(rate_card or [])
            if unknown_dimensions:
                blockers.append(Blocker(
                    type="price_dimensions_unknown", subtype="unparseable_rate_card_row",
                    evidence=f"{len(unknown_dimensions)} row(s) did not parse: "
                    f"{sorted(unknown_dimensions)[:5]!r}",
                ))
            else:
                scope = scope_for_model_id(raw_id)
                candidate_regions = destination_regions or (invocation_region,)
                selection = select(card, regions=candidate_regions, scope=scope)
                if selection is None:
                    blockers.append(Blocker(
                        type="no_token_pricing", subtype="selector_refused",
                        evidence="dimensions.select() found no usable input/output "
                        "price among this card's resolved rows",
                    ))
                else:
                    pricing_key = key_for_selection(selection, enabled_modes=ENABLED_MODES)

    record = DiscoveredRecord(
        profile_id=raw_id,
        provider=provider,
        profile_scope=profile_scope,
        model_family=model_family,
        jurisdiction_bounded=jurisdiction_bounded,
        destination_regions=destination_regions,
        invocation_region=invocation_region,
        raw_id=raw_id,
        raw_payload=summary,
        observation_scope=observation_scope,
        blockers=tuple(blockers),
    )
    return record, pricing_key, note


def run_pass(*, bedrock=None, sts=None, region: Optional[str] = None) -> PassResult:
    """Discover every visible profile and build its record, merging each
    against whatever this profile's store already holds so `first_seen`
    survives across passes. Never writes — that is `main`'s job, gated on
    `--apply`.

    The Bedrock client is guarded, the STS client is not, and that split is
    deliberate rather than an oversight of one of them. Bedrock IS the pass:
    every gate below reads it, so a Bedrock client that cannot be built means
    this pass cannot look at anything, which is exactly the fact
    `PassResult.observation_blocker` exists to carry (see its docstring) —
    caught here, at the one place it happens, and returned as a result
    instead of left to blow `main()` up. STS is not the pass, it is metadata
    ABOUT the pass — `observation_scope`'s account/arn, attached to whatever
    records this pass does build so a reader can tell which credentials
    produced them. A pass that cannot resolve its own identity can still
    correctly discover every profile and run every gate, so STS failing
    (construction or the call itself, the same try below covers both) must
    not stop it: only the account/arn fields degrade to `""`, exactly as they
    already did before this fix, for exactly the reason the existing comment
    below already gave."""
    endpoint_region = region or os.getenv(STRATOCLAVE_REGION_ENV) or _DEFAULT_REGION
    try:
        bedrock = _client("bedrock", region=endpoint_region, injected=bedrock)
    except Exception as exc:  # noqa: BLE001 — no client, no pass; recorded, not fatal.
        logger.warning("discovery_bedrock_client_unavailable", error=str(exc))
        return PassResult(observation_blocker=Blocker(
            type="no_agreement_offer", subtype="client_unavailable", evidence=str(exc),
        ))

    try:
        sts = _client("sts", region=endpoint_region, injected=sts)
        identity = sts.get_caller_identity()
        account = str(identity.get("Account") or "")
        arn = str(identity.get("Arn") or "")
    except Exception as exc:  # noqa: BLE001 — observation scope degrades, pass continues.
        logger.warning("discovery_get_caller_identity_failed", error=str(exc))
        account = ""
        arn = ""

    observation_scope = ObservationScope(
        account=account,
        region=endpoint_region,
        credentials_fingerprint=credentials_fingerprint(arn) if arn else "",
        observed_at=_now_iso(),
    )

    summaries, truncated = _list_inference_profiles(bedrock)
    result = PassResult(profiles_truncated=truncated)
    model_cache: dict[str, tuple[Optional[Mapping[str, Any]], Optional[str]]] = {}

    for summary in summaries:
        try:
            record, pricing_key, note = build_record(
                summary, bedrock=bedrock, invocation_region=endpoint_region,
                observation_scope=observation_scope, model_cache=model_cache,
            )
        except Exception as exc:  # noqa: BLE001 — one bad profile must not fail the pass.
            profile_id = str(summary.get("inferenceProfileId") or "<unknown>")
            result.discovery_errors.append(f"{profile_id}: {exc}")
            logger.warning("discovery_build_record_failed", profile_id=profile_id, error=str(exc))
            continue
        if note is not None:
            result.discovery_errors.append(f"{record.profile_id}: {note}")
        try:
            previous = get_discovered_record(record.profile_id)
        except DiscoveredRecordStoreUnavailable as exc:
            result.discovery_errors.append(f"{record.profile_id}: store read failed: {exc}")
            previous = None
        if previous is not None:
            record = DiscoveredRecord(
                profile_id=record.profile_id, provider=record.provider,
                profile_scope=record.profile_scope, model_family=record.model_family,
                jurisdiction_bounded=record.jurisdiction_bounded,
                destination_regions=record.destination_regions,
                invocation_region=record.invocation_region, raw_id=record.raw_id,
                raw_payload=record.raw_payload, observation_scope=record.observation_scope,
                blockers=merge_blockers(previous.blockers, record.blockers),
            )
        result.records.append(record)
        result.pricing_keys[record.profile_id] = pricing_key
    return result


# `no_agreement_offer`'s one PERMANENT subtype (see the module docstring's
# actionable/permanent split for why this is a subtype-level carve-out, not a
# type-level one).
_PERMANENT_NO_AGREEMENT_OFFER_SUBTYPES = frozenset({"not_marketplace_metered"})

# Blocker TYPES that never make --strict fail, on any pass, because the
# underlying fact cannot change through anything this account can do (see the
# module docstring). `no_agreement_offer` is deliberately absent from this
# set: it is actionable except for the one subtype above.
_PERMANENT_BLOCKER_TYPES = frozenset({"unsupported_output_modality", "no_token_pricing"})


def _is_actionable(blocker: Blocker) -> bool:
    if blocker.type in _PERMANENT_BLOCKER_TYPES:
        return False
    if blocker.type == "no_agreement_offer":
        return blocker.subtype not in _PERMANENT_NO_AGREEMENT_OFFER_SUBTYPES
    return True


def _actionable_blocker_findings(result: PassResult) -> list[str]:
    """Every actionable blocker THIS pass produced, regardless of whether it
    is new or merely reconfirmed: an unresolved, actionable problem is not
    less true on its second pass, so `--strict` must keep failing on it until
    it actually clears (a gate that only nags once and then goes quiet about
    an unfixed, fixable problem is worse than one that never nagged).

    Checked unconditionally, same as every record's blockers below:
    `result.observation_blocker` is set exactly when this pass could not
    build its Bedrock client (see `PassResult`'s docstring), which is
    `no_agreement_offer`/`client_unavailable` — already ACTIONABLE under
    `_is_actionable` — so it is read here rather than given its own strict
    reason. A total failure to observe is not a lesser fact than one
    profile's blocker; it does not get a quieter check."""
    findings = []
    if result.observation_blocker is not None and _is_actionable(result.observation_blocker):
        b = result.observation_blocker
        findings.append(f"<pass>: {b.type}/{b.subtype}")
    for record in result.records:
        for blocker in record.blockers:
            if _is_actionable(blocker):
                findings.append(f"{record.profile_id}: {blocker.type}/{blocker.subtype}")
    return findings


def _strict_reasons(result: PassResult, *, apply_errors: list[str]) -> list[str]:
    reasons = []
    if _actionable_blocker_findings(result):
        reasons.append("actionable_blocker")
    if result.profiles_truncated:
        reasons.append("profiles_truncated")
    if result.discovery_errors:
        reasons.append("store_unavailable")
    if apply_errors:
        reasons.append("apply_incomplete")
    return reasons


def _apply(result: PassResult) -> list[str]:
    """Write every record this pass built. Returns the profile ids that did
    not, in the end, get written."""
    errors = []
    for record in result.records:
        try:
            put_discovered_record(record)
        except DiscoveredRecordStoreUnavailable as exc:
            errors.append(f"{record.profile_id}: {exc}")
            logger.warning("discovery_apply_failed", profile_id=record.profile_id, error=str(exc))
    return errors


def main(argv: Optional[list[str]] = None, *,
        bedrock: Optional[Any] = None, sts: Optional[Any] = None) -> int:
    """The one entry point for the CLI. `argv` is parsed exactly as before;
    `bedrock`/`sts` are the same injection seam `run_pass` already takes,
    threaded through here rather than a second, class-shaped entry point.

    This is the only way to reach `--apply` — the only path that changes
    stored state — with an injected client instead of a real one: `main([],
    bedrock=fake, sts=fake)` runs a dry pass against a fake and writes
    nothing, `main(["--apply"], bedrock=fake, sts=fake)` runs the same pass
    and writes. Without this seam the write path is unexercisable before
    real-machine verification, since moto implements none of the Bedrock
    discovery APIs `run_pass` calls.
    """
    parser = argparse.ArgumentParser(prog="mvp.discovery.reconcile")
    parser.add_argument("--apply", action="store_true",
                        help="store every discovered record (dry run by default)")
    parser.add_argument("--strict", action="store_true",
                        help="exit 2 when the pass raised a finding a person should "
                             "see, named by exactly these tokens: actionable_blocker "
                             "(a profile carries an actionable blocker — "
                             "price_dimensions_unknown, no_model_access, or "
                             "no_agreement_offer other than its "
                             "not_marketplace_metered subtype — or the pass itself "
                             "could not build its Bedrock client, reported as the "
                             "same no_agreement_offer/client_unavailable at the pass "
                             "level; fires every pass it is present, not only the "
                             "first), profiles_truncated "
                             "(ListInferenceProfiles did not finish), "
                             "store_unavailable (a record read or write failed), "
                             "apply_incomplete (--apply was given and a built record "
                             "was not written)")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args(argv)

    result = run_pass(bedrock=bedrock, sts=sts)
    apply_errors = _apply(result) if args.apply else []
    reasons = _strict_reasons(result, apply_errors=apply_errors)
    exit_code = 2 if (args.strict and reasons) else (1 if not result.records else 0)

    if args.json:
        payload = {
            "profiles": len(result.records),
            "records": [
                {
                    "profile_id": r.profile_id,
                    "provider": r.provider,
                    "profile_scope": r.profile_scope,
                    "model_family": r.model_family,
                    "jurisdiction_bounded": r.jurisdiction_bounded,
                    "destination_regions": list(r.destination_regions),
                    "invocation_region": r.invocation_region,
                    "pricing_key": result.pricing_keys.get(r.profile_id),
                    "blockers": [
                        {"type": b.type, "subtype": b.subtype, "evidence": b.evidence,
                         "first_seen": b.first_seen, "last_seen": b.last_seen}
                        for b in r.blockers
                    ],
                }
                for r in result.records
            ],
            "profiles_truncated": result.profiles_truncated,
            "discovery_errors": result.discovery_errors,
            "observation_blocker": (
                {"type": result.observation_blocker.type,
                 "subtype": result.observation_blocker.subtype,
                 "evidence": result.observation_blocker.evidence}
                if result.observation_blocker is not None else None
            ),
            "applied": bool(args.apply and not apply_errors),
            "apply_errors": apply_errors,
            "strict_reasons": reasons,
        }
        print(json.dumps(payload, indent=1, sort_keys=True))
        return exit_code

    print(f"discovered {len(result.records)} profile(s)"
         f"{' (truncated)' if result.profiles_truncated else ''}")
    if result.observation_blocker is not None:
        b = result.observation_blocker
        print(f"\n[BLOCKED] this pass could not observe the account at all: "
             f"{b.type}/{b.subtype} — {b.evidence}")
    for record in result.records:
        key = result.pricing_keys.get(record.profile_id)
        print(f"  {record.profile_id:<48} scope={record.profile_scope:<8} "
             f"key={key or '-'}")
        for blocker in record.blockers:
            fresh = " [new]" if blocker.first_seen == blocker.last_seen else ""
            print(f"    blocked: {blocker.type}/{blocker.subtype}{fresh} — {blocker.evidence}")
    if result.discovery_errors:
        print("\nerrors:")
        for error in result.discovery_errors:
            print(f"  {error}")
    if args.apply:
        if apply_errors:
            print(f"\n[ERROR] {len(apply_errors)} record(s) did not apply:")
            for error in apply_errors:
                print(f"  {error}")
        else:
            print(f"\napplied — stored {len(result.records)} record(s)")
    else:
        print("\n(dry run — nothing stored; re-run with --apply)")
    if args.strict and reasons:
        print("\n[STRICT] exit 2 for: " + ", ".join(reasons))
    return exit_code


if __name__ == "__main__":  # pragma: no cover — ops entry point.
    raise SystemExit(main())
