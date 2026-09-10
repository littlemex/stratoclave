"""PR1: the registry grows four new per-entry fields -- `profile_scope`,
`model_family`, `access`, and `jurisdiction_bounded` (plus `jurisdiction` when
bounded) -- plus two offline checks over them (C10, C13) and one new pricing
row (C16). PR1 changes NO runtime behaviour: nothing here consults a tenant,
refuses a request, or filters a listing.

C10 and C13 are named, importable checks (`mvp.registry_checks`), not logic
this file reimplements. Every C10/C13 assertion below calls
`check_family_scope_pricing_keys_distinct` / `check_profile_scopes_granted_by_iam`
directly, passing either the real registry (the default) or entries built by
hand for a case `load_registry()` itself would refuse (same-family/same-scope
duplication, which is its own load-time rejection now -- see
`test_same_family_same_scope_may_share_a_pricing_key`). Constructing those
entries needs no knowledge of the two functions' insides, only the field names
the contract already names.

Interface gaps resolved without reading the implementation:
  - `profile_scope`'s seventh value (the "no inference profile" sentinel) is
    never given a literal. It is discovered from the two shipped bare entries
    (nvidia, qwen) at runtime rather than guessed, so a naming mismatch with
    the implementation cannot produce a false pass.
  - The pricing key C16 adds is not spelled out either; `FABLE_GLOBAL_KEY`
    below is inferred from the one concrete string the contract's "Measured
    facts" gives ("There is no `fable-global` key") and from this document's
    own <family>-<qualifier> naming convention.
"""
from __future__ import annotations

import json

import pytest

from mvp import price_sources
from mvp.models import ModelEntry, load_registry, registry_entries
from mvp.registry_checks import (
    check_family_scope_pricing_keys_distinct,
    check_profile_scopes_granted_by_iam,
)

# ---------------------------------------------------------------------------
# Shared JSON-entry builder, extending tests/test_registry_and_price_sources.py's
# `_entry()` with PR1's four new required fields (kept local rather than
# imported: that file's helper is a fixture other tests already depend on, and
# this module's defaults differ in one deliberate way -- see
# `jurisdiction_bounded` below).
# ---------------------------------------------------------------------------

KNOWN_GEO_SCOPES = ("jp", "us", "global", "eu", "apac", "gov")


def _entry(**over):
    base = {
        "provider": "anthropic",
        "bedrock_model_id": "us.anthropic.claude-opus-5",
        "bedrock_region": "us-east-1",
        "aliases": ["claude-opus-5"],
        "wire_protocol": "messages",
        "pricing_key": "opus",
        "profile_scope": "us",
        "model_family": "opus",
        "access": "general",
        # False, not True: True would also need a `jurisdiction` value.
        "jurisdiction_bounded": False,
    }
    base.update(over)
    return base


def _geo_id(scope: str, vendor: str, name: str) -> str:
    """A `bedrock_model_id` whose first dot-segment agrees with `scope`. The
    registry now cross-checks the two when the segment is one of the six
    geography tokens, so any test that declares a geography `profile_scope`
    has to spell its id this way or it fails on the cross-check instead of
    whatever it actually means to exercise."""
    return f"{scope}.{vendor}.{name}"


def _doc(*entries):
    return {"schema_version": 1, "models": list(entries)}


def _write(tmp_path, doc, name="models.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


def _no_profile_scope() -> str:
    """The seventh `profile_scope` value: "one distinct value meaning this entry
    names a bare foundation model and has no inference profile." The handoff
    names the other six (`jp`, `us`, `global`, `eu`, `apac`, `gov`) but not this
    one, so it is read off the two shipped bare entries (nvidia, qwen) rather
    than guessed -- whatever literal the implementation picked, this finds it.
    """
    bare_vendors = ("nvidia", "qwen")
    values = {e.profile_scope for e in registry_entries() if e.provider in bare_vendors}
    assert values, (
        "expected the shipped nvidia and qwen entries to be present so the "
        "no-profile profile_scope sentinel can be read off them"
    )
    assert len(values) == 1, (
        f"the two bare (no inference profile) shipped entries must share ONE "
        f"sentinel profile_scope value; got {sorted(values)}"
    )
    (value,) = values
    assert value not in KNOWN_GEO_SCOPES, (
        f"the no-profile sentinel {value!r} collides with one of the six "
        f"declared geography values {KNOWN_GEO_SCOPES}"
    )
    return value


# ---------------------------------------------------------------------------
# C1: profile_scope
# ---------------------------------------------------------------------------

class TestProfileScope:
    def test_missing_profile_scope_aborts_load_naming_the_entry_and_field(self, tmp_path):
        raw = _entry()
        raw.pop("profile_scope")
        path = _write(tmp_path, _doc(raw))
        with pytest.raises(ValueError, match="missing required field") as ei:
            load_registry(path)
        assert "profile_scope" in str(ei.value) and "models[0]" in str(ei.value), (
            "the handoff requires the error to NAME the entry and the field, not "
            "just say something is missing -- a message that does not mention "
            "profile_scope or the entry index leaves an operator guessing which "
            "of 21 entries has the typo"
        )

    def test_unknown_profile_scope_value_aborts_load(self, tmp_path):
        path = _write(tmp_path, _doc(_entry(profile_scope="antarctica")))
        with pytest.raises(ValueError):
            load_registry(path)

    def test_unknown_profile_scope_is_not_silently_skipped(self, tmp_path):
        """The handoff is explicit that this must abort the WHOLE load, not drop
        the one bad entry: skipping would silently remove a model on a typo. Two
        entries, one bad, and the good one must not survive either."""
        path = _write(tmp_path, _doc(
            _entry(),
            _entry(bedrock_model_id="us.anthropic.claude-sonnet-5",
                   aliases=["claude-sonnet-5"], pricing_key="sonnet",
                   model_family="sonnet", profile_scope="not-a-real-scope"),
        ))
        with pytest.raises(ValueError):
            load_registry(path)

    @pytest.mark.parametrize("scope", KNOWN_GEO_SCOPES)
    def test_each_declared_geography_value_is_accepted(self, tmp_path, scope):
        """The id's first segment must AGREE with the declared scope (a
        separate cross-check from declaring the field at all), so each scope
        here gets an id prefixed to match it. `price_model_id` is supplied
        unconditionally: an id whose first segment is a scope token this
        registry recognises can still be a prefix the UNRELATED billed-id
        guesser (`pricing_feeds.dimensions.unknown_profile_prefix`) does not
        recognise (observed for `gov.`), and this test is about the scope
        cross-check, not about that guesser."""
        path = _write(tmp_path, _doc(_entry(
            profile_scope=scope,
            bedrock_model_id=_geo_id(scope, "anthropic", "claude-opus-5-test"),
            aliases=[f"opus-5-{scope}-test"],
            price_model_id="anthropic.claude-opus-5",
        )))
        (entry,) = load_registry(path)
        assert entry.profile_scope == scope

    def test_a_geography_prefixed_id_disagreeing_with_the_declared_scope_is_refused(self, tmp_path):
        """The cross-check itself, not just that a matching pair is accepted:
        an id whose first segment IS one of the six geography tokens must
        agree with the declared `profile_scope`, or load is refused. `eu.` on
        the id against `profile_scope="us"` is exactly the shape a
        copy-pasted entry produces when only one of the two was updated."""
        path = _write(tmp_path, _doc(_entry(
            profile_scope="us",
            bedrock_model_id=_geo_id("eu", "anthropic", "claude-opus-5-mismatch-test"),
            aliases=["opus-5-mismatch-test"],
        )))
        with pytest.raises(ValueError):
            load_registry(path)

    def test_the_no_profile_sentinel_is_accepted_for_a_bare_model_entry(self, tmp_path):
        no_profile = _no_profile_scope()
        path = _write(tmp_path, _doc(_entry(
            provider="nvidia", bedrock_model_id="nvidia.some-test-model",
            aliases=["some-test-model"], profile_scope=no_profile,
            model_family="some-test-family", pricing_key="nemotron",
        )))
        (entry,) = load_registry(path)
        assert entry.profile_scope == no_profile

    def test_us_is_never_read_as_a_single_region(self, tmp_path):
        """"`us` is a geography of THREE regions, never a single region" -- an
        entry using `us` as its profile_scope must not be treated as if it
        pinned exactly `bedrock_region`'s one region. This is a data-only claim
        in PR1 (routing on it is out of scope), so what is checkable here is
        that declaring `us` does not change or constrain `bedrock_region` at
        load -- a region outside the three-region US set must still be
        accepted, because PR1 adds no policy that reads profile_scope to
        validate bedrock_region."""
        path = _write(tmp_path, _doc(_entry(
            profile_scope="us", bedrock_region="ap-northeast-1",
        )))
        (entry,) = load_registry(path)
        assert entry.bedrock_region == "ap-northeast-1"


# ---------------------------------------------------------------------------
# C2: model_family
# ---------------------------------------------------------------------------

class TestModelFamily:
    def test_missing_model_family_aborts_load_naming_the_entry_and_field(self, tmp_path):
        raw = _entry()
        raw.pop("model_family")
        path = _write(tmp_path, _doc(raw))
        with pytest.raises(ValueError, match="missing required field") as ei:
            load_registry(path)
        assert "model_family" in str(ei.value)

    def test_empty_model_family_aborts_load(self, tmp_path):
        """Matches the existing `_REQUIRED_FIELDS` convention (`raw[field] in
        (None, "", [], {})` is treated as missing, not merely absent-key) that
        every other required field already follows in this document."""
        path = _write(tmp_path, _doc(_entry(model_family="")))
        with pytest.raises(ValueError, match="empty required field"):
            load_registry(path)

    def test_two_unrelated_entries_may_declare_different_families_sharing_nothing(self, tmp_path):
        path = _write(tmp_path, _doc(
            _entry(model_family="opus"),
            _entry(bedrock_model_id="us.anthropic.claude-sonnet-5",
                   aliases=["claude-sonnet-5"], pricing_key="sonnet",
                   model_family="sonnet"),
        ))
        entries = load_registry(path)
        assert {e.model_family for e in entries} == {"opus", "sonnet"}

    def test_two_entries_sharing_family_and_scope_are_refused_naming_both(self, tmp_path):
        """The uniqueness rule itself, asserted directly through
        `load_registry()` -- not only reached as a side effect of a C10
        fixture that happens to also violate it (which is how this rule lost
        its own coverage the first time: C10's fixture was rebuilt to bypass
        `load_registry()` entirely once this rule made it unconstructable
        there, so nothing calling `load_registry()` with two same-family-
        same-scope rows was left in the suite). Two rows claiming the same
        `(model_family, profile_scope)` are two rows for one product, and the
        refusal must name BOTH offending entries -- either could be "the"
        duplicate from an operator's point of view."""
        path = _write(tmp_path, _doc(
            _entry(model_family="opus-uniqueness-probe", profile_scope="us",
                   bedrock_model_id="us.anthropic.claude-opus-uniqueness-a",
                   aliases=["opus-uniqueness-a"]),
            _entry(model_family="opus-uniqueness-probe", profile_scope="us",
                   bedrock_model_id="us.anthropic.claude-opus-uniqueness-b",
                   aliases=["opus-uniqueness-b"]),
        ))
        with pytest.raises(ValueError) as ei:
            load_registry(path)
        message = str(ei.value)
        assert "models[0]" in message and "models[1]" in message, (
            f"the refusal must name BOTH offending entries by index (matching "
            f"this document's own convention for the alias/id uniqueness "
            f"checks: 'models[{{previous}}] ... and models[{{index}}]'), not "
            f"just one -- got {message!r}"
        )

    def test_two_entries_sharing_family_with_different_scopes_load(self, tmp_path):
        """The positive counterpart, and precisely PR3's shape: a second-scope
        entry for the SAME product must still load. A uniqueness rule
        implemented too broadly (e.g. keyed on `model_family` alone, ignoring
        `profile_scope`) would reject this pair."""
        path = _write(tmp_path, _doc(
            _entry(model_family="fable-uniqueness-probe", profile_scope="us",
                   bedrock_model_id=_geo_id("us", "anthropic", "claude-fable-uniq-test"),
                   aliases=["fable-uniq-us-test"]),
            _entry(model_family="fable-uniqueness-probe", profile_scope="global",
                   bedrock_model_id=_geo_id("global", "anthropic", "claude-fable-uniq-test"),
                   aliases=["fable-uniq-global-test"]),
        ))
        entries = load_registry(path)
        assert {e.profile_scope for e in entries} == {"us", "global"}


# ---------------------------------------------------------------------------
# C3: access
# ---------------------------------------------------------------------------

class TestAccess:
    def test_missing_access_aborts_load_naming_the_entry_and_field(self, tmp_path):
        raw = _entry()
        raw.pop("access")
        path = _write(tmp_path, _doc(raw))
        with pytest.raises(ValueError, match="missing required field") as ei:
            load_registry(path)
        assert "access" in str(ei.value)

    def test_unknown_access_value_aborts_load(self, tmp_path):
        path = _write(tmp_path, _doc(_entry(access="premium")))
        with pytest.raises(ValueError):
            load_registry(path)

    def test_general_is_accepted(self, tmp_path):
        (entry,) = load_registry(_write(tmp_path, _doc(_entry(access="general"))))
        assert entry.access == "general"

    def test_entitlement_required_is_accepted_even_though_unused_by_any_shipped_entry(self, tmp_path):
        """Forward-compatibility, not vacuous: every SHIPPED entry is `general`
        (the handoff's own "Measured facts"), but the enum has two values and PR1
        must accept the second one at load -- PR2/PR3 will file entries under it.
        A loader that only recognises `general` (e.g. because it was written by
        looking at today's data rather than the stated enum) would reject this
        and silently block the next PR."""
        (entry,) = load_registry(_write(tmp_path, _doc(
            _entry(access="entitlement_required"))))
        assert entry.access == "entitlement_required"

    def test_every_shipped_entry_is_general(self):
        non_general = [
            e.bedrock_model_id for e in registry_entries() if e.access != "general"
        ]
        assert not non_general, (
            f"the handoff's Measured facts say every shipped entry is `general`; "
            f"these are not: {non_general}"
        )


# ---------------------------------------------------------------------------
# C14a: jurisdiction_bounded, and jurisdiction when bounded (data only -- no
# contradictory-scope refusal policy in PR1; that is PR2).
# ---------------------------------------------------------------------------

class TestJurisdictionBounded:
    def test_missing_jurisdiction_bounded_aborts_load_naming_the_entry_and_field(self, tmp_path):
        raw = _entry()
        raw.pop("jurisdiction_bounded")
        path = _write(tmp_path, _doc(raw))
        with pytest.raises(ValueError, match="missing required field") as ei:
            load_registry(path)
        assert "jurisdiction_bounded" in str(ei.value)

    @pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
    def test_jurisdiction_bounded_must_be_a_json_boolean(self, tmp_path, value):
        """Mirrors the existing `virtual` field's strict-boolean check
        (`bool("false") is True` in Python, so a naive cast would silently
        accept the string "false" as a bounded entry)."""
        path = _write(tmp_path, _doc(_entry(jurisdiction_bounded=value)))
        with pytest.raises(ValueError, match="jurisdiction_bounded"):
            load_registry(path)

    def test_true_and_false_are_both_accepted(self, tmp_path):
        (bounded,) = load_registry(_write(tmp_path, _doc(_entry(
            jurisdiction_bounded=True, jurisdiction="us")), "bounded.json"))
        assert bounded.jurisdiction_bounded is True

        (unbounded,) = load_registry(_write(tmp_path, _doc(_entry(
            jurisdiction_bounded=False)), "unbounded.json"))
        assert unbounded.jurisdiction_bounded is False

    def test_jurisdiction_is_required_exactly_when_bounded_is_true(self, tmp_path):
        """`jurisdiction_bounded=True` without a `jurisdiction` must be
        refused (there is nothing to name it), and `jurisdiction_bounded=False`
        WITH one must also be refused (a contradiction: an unbounded entry
        naming a jurisdiction claims two things at once)."""
        missing_jurisdiction = _write(tmp_path, _doc(_entry(
            jurisdiction_bounded=True)), "missing.json")
        with pytest.raises(ValueError, match="jurisdiction_bounded"):
            load_registry(missing_jurisdiction)

        contradictory = _write(tmp_path, _doc(_entry(
            jurisdiction_bounded=False, jurisdiction="us")), "contradiction.json")
        with pytest.raises(ValueError):
            load_registry(contradictory)

    def test_global_scope_declared_bounded_still_loads_in_pr1(self, tmp_path):
        """"the policy that refuses a contradictory scope set is PR2 and must
        not appear here." `profile_scope=global` ("unbounded: it routes to all
        supported regions") together with `jurisdiction_bounded=True` and a
        named jurisdiction is exactly the kind of self-contradictory
        declaration that policy will refuse -- PR1 must still load it, because
        PR1 is data only. If this starts raising, PR2's check landed early."""
        path = _write(tmp_path, _doc(_entry(
            profile_scope="global",
            bedrock_model_id=_geo_id("global", "anthropic", "claude-opus-5-bounded-test"),
            aliases=["opus-5-global-bounded-test"],
            jurisdiction_bounded=True, jurisdiction="us")))
        (entry,) = load_registry(path)
        assert entry.profile_scope == "global"
        assert entry.jurisdiction_bounded is True

    def test_a_geography_scope_declared_unbounded_still_loads_in_pr1(self, tmp_path):
        """The mirror image of the case above -- `us` (a real geography) marked
        `jurisdiction_bounded=False` (so no `jurisdiction` is named at all) is
        just as self-contradictory, and just as much a PR2 concern."""
        path = _write(tmp_path, _doc(_entry(
            profile_scope="us", jurisdiction_bounded=False)))
        (entry,) = load_registry(path)
        assert entry.jurisdiction_bounded is False

    def test_jurisdiction_bounded_is_not_inferred_from_profile_scope(self, tmp_path):
        """"No value is ever inferred or defaulted." Two entries that are
        otherwise IDENTICAL (same profile_scope, same family collapsed to a
        deliberately distinct value below only because `(model_family,
        profile_scope)` is now unique, not because this test is about family)
        except for `jurisdiction_bounded` must load to DIFFERENT values --
        proving the loader reads the declared value rather than deriving it
        (e.g. always computing `profile_scope != "global"`), which would make
        this test's two entries indistinguishable."""
        path = _write(tmp_path, _doc(
            _entry(bedrock_model_id="us.anthropic.claude-opus-5",
                   aliases=["a-bounded"], model_family="opus-bounded-probe",
                   profile_scope="us", jurisdiction_bounded=True,
                   jurisdiction="us"),
            _entry(bedrock_model_id="us.anthropic.claude-opus-4-7",
                   aliases=["a-unbounded"], model_family="opus-unbounded-probe",
                   profile_scope="us", jurisdiction_bounded=False),
        ))
        bounded, unbounded = load_registry(path)
        assert bounded.jurisdiction_bounded is True
        assert unbounded.jurisdiction_bounded is False


# ---------------------------------------------------------------------------
# C10: two entries of one family, different profile_scope, must not share a
# pricing_key. Checked by calling the named production function directly
# (`mvp.registry_checks.check_family_scope_pricing_keys_distinct`), not by
# reimplementing the rule here.
# ---------------------------------------------------------------------------

def _model_entry(**over) -> ModelEntry:
    """A `ModelEntry` built directly, bypassing `load_registry()` entirely.
    Needed for a case the loader itself now refuses at load time (identical
    `(model_family, profile_scope)` on two rows) but that
    `check_family_scope_pricing_keys_distinct` still needs to see fed in
    explicitly to prove it does not ALSO over-reject that legitimate case."""
    base = dict(
        provider="anthropic", bedrock_model_id="us.anthropic.claude-opus-5",
        bedrock_region="us-east-1", aliases=("claude-opus-5",),
        wire_protocol="messages", pricing_key="opus",
        profile_scope="us", model_family="opus", access="general",
        jurisdiction_bounded=False,
    )
    base.update(over)
    return ModelEntry(**base)


class TestC10FamilyScopeSharesNoPricingKey:
    def test_the_shipped_registry_has_no_violation(self):
        """Vacuous at today's data (no shipped family yet spans more than one
        scope), but exercises the real function against the real registry
        rather than a copy of its logic."""
        check_family_scope_pricing_keys_distinct()

    def test_same_family_same_scope_may_share_a_pricing_key(self):
        """The ordinary, common case (e.g. Opus 4.5/4.6/4.7 today): sharing a
        key is fine as long as the scope agrees. If this raised, C10 would be
        over-broad -- forbidding ANY sharing rather than only a
        scope-crossing one. `(model_family, profile_scope)` is now unique at
        load, so this pair is built directly rather than through
        `load_registry()`, which would refuse it for a different, unrelated
        reason before the check under test ever ran."""
        entries = (
            _model_entry(model_family="opus", profile_scope="us", pricing_key="opus",
                         bedrock_model_id="us.anthropic.claude-opus-5-a",
                         aliases=("opus-a-test",)),
            _model_entry(model_family="opus", profile_scope="us", pricing_key="opus",
                         bedrock_model_id="us.anthropic.claude-opus-5-b",
                         aliases=("opus-b-test",)),
        )
        check_family_scope_pricing_keys_distinct(entries=entries)

    def test_same_family_different_scope_same_pricing_key_is_a_c10_violation(self, tmp_path):
        """The exact shape PR3 must avoid for Claude Fable 5: a `global`-scope
        variant sharing "fable" with the `us`-scope one would charge the
        cheaper global request at the dearer in-region rate (or vice versa) --
        scope is a price point. `(model_family, profile_scope)` differs
        between the two rows (fable/us vs. fable/global), so this pair is
        legal at load and is built through `load_registry()`."""
        entries = load_registry(_write(tmp_path, _doc(
            _entry(provider="anthropic",
                   bedrock_model_id=_geo_id("us", "anthropic", "claude-fable-5-test"),
                   aliases=["fable-5-us-test"], model_family="fable",
                   profile_scope="us", pricing_key="fable"),
            _entry(provider="anthropic",
                   bedrock_model_id=_geo_id("global", "anthropic", "claude-fable-5-test"),
                   aliases=["fable-5-global-test"], model_family="fable",
                   profile_scope="global", pricing_key="fable"),
        )))
        with pytest.raises(ValueError):
            check_family_scope_pricing_keys_distinct(entries=entries)

    def test_same_family_different_scope_different_pricing_key_is_fine(self, tmp_path):
        """The corrected version of the case above -- once the global variant
        carries its OWN key (`fable-global`, see C16), C10 must have nothing to
        say. This is the end state PR3 is expected to reach."""
        entries = load_registry(_write(tmp_path, _doc(
            _entry(provider="anthropic",
                   bedrock_model_id=_geo_id("us", "anthropic", "claude-fable-5-test-2"),
                   aliases=["fable-5-us-test-2"], model_family="fable",
                   profile_scope="us", pricing_key="fable"),
            _entry(provider="anthropic",
                   bedrock_model_id=_geo_id("global", "anthropic", "claude-fable-5-test-2"),
                   aliases=["fable-5-global-test-2"], model_family="fable",
                   profile_scope="global", pricing_key="fable-global"),
        )))
        check_family_scope_pricing_keys_distinct(entries=entries)

    def test_different_family_may_share_a_pricing_key_across_scopes(self, tmp_path):
        """Guards against an over-broad reading of C10 that forbids ANY
        pricing_key reuse across differing scopes, regardless of family. C10 is
        scoped to entries that share a `model_family`; these two do not, so a
        shared key ("opus", a real bundled row -- an unpriced test key would
        be refused at load for an unrelated reason) across different scopes
        must NOT be flagged."""
        entries = load_registry(_write(tmp_path, _doc(
            _entry(model_family="opus", profile_scope="us", pricing_key="opus"),
            _entry(provider="openai",
                   bedrock_model_id=_geo_id("global", "openai", "gpt-5.6-sol-test"),
                   aliases=["gpt-5.6-sol-cross-family-test"], wire_protocol="responses",
                   bedrock_region="eu-central-1", model_family="gpt-5.6",
                   profile_scope="global", pricing_key="opus"),
        )))
        check_family_scope_pricing_keys_distinct(entries=entries)


# ---------------------------------------------------------------------------
# C13: a declared profile_scope must be IAM-grantable for that entry's vendor.
# Checked by calling `mvp.registry_checks.check_profile_scopes_granted_by_iam`
# directly against real `iac/lib/ecs-stack.ts` (no override args passed),
# so these tests exercise the actual file, not a copy of its patterns.
# ---------------------------------------------------------------------------

class TestC13ProfileScopeIsGrantable:
    def test_the_shipped_registry_has_no_violation(self):
        """Vacuous at today's data (every shipped entry's scope is already
        granted), but exercises the real function against the real registry
        and the real IaC file rather than a copy of either."""
        check_profile_scopes_granted_by_iam()

    def test_eu_anthropic_is_granted_but_eu_openai_is_not(self, tmp_path):
        """The mandated pair: a fabricated eu.openai entry must FAIL and a
        fabricated eu.anthropic entry must PASS. `eu-central-1` is used for
        the openai entry's `bedrock_region` specifically so the region check
        (`_OPENAI_ENDPOINT_REGIONS`) does not reject it for an unrelated
        reason and mask what this test is actually checking."""
        (anthropic_entry,) = load_registry(_write(tmp_path, _doc(_entry(
            provider="anthropic", model_family="opus", pricing_key="opus",
            profile_scope="eu",
            bedrock_model_id=_geo_id("eu", "anthropic", "claude-opus-5-test"),
            aliases=["opus-5-eu-test"],
        )), "anthropic.json"))
        check_profile_scopes_granted_by_iam(entries=(anthropic_entry,))

        (openai_entry,) = load_registry(_write(tmp_path, _doc(_entry(
            provider="openai", model_family="gpt-5.6", pricing_key="gpt-5.6-sol",
            profile_scope="eu",
            bedrock_model_id=_geo_id("eu", "openai", "gpt-5.6-sol-test"),
            aliases=["gpt-5.6-sol-eu-test"], wire_protocol="responses",
            bedrock_region="eu-central-1",
        )), "openai.json"))
        with pytest.raises(ValueError):
            check_profile_scopes_granted_by_iam(entries=(openai_entry,))

    @pytest.mark.parametrize("vendor", ["anthropic", "openai", "xai"])
    @pytest.mark.parametrize("scope", ["jp", "gov"])
    def test_jp_and_gov_are_not_granted_for_any_vendor(self, tmp_path, vendor, scope):
        """No vendor's IAM statement mentions a `jp.` or `gov.`-style
        inference-profile prefix at all -- these two of the six declared
        geography values are not grantable for anyone yet. `price_model_id`
        sidesteps the UNRELATED billed-id guesser, which does not recognise a
        bare `gov.` prefix (see test_each_declared_geography_value_is_accepted)."""
        kwargs = dict(provider=vendor, profile_scope=scope,
                      model_family="grant-probe", pricing_key="opus",
                      bedrock_model_id=_geo_id(scope, vendor, "probe-test"),
                      aliases=[f"probe-{vendor}-{scope}"],
                      price_model_id=f"{vendor}.probe-test")
        if vendor != "anthropic":
            kwargs.update(wire_protocol="responses", bedrock_region="us-east-2",
                          pricing_key="gpt-5.6-sol" if vendor == "openai" else "grok")
        (entry,) = load_registry(_write(tmp_path, _doc(_entry(**kwargs))))
        with pytest.raises(ValueError):
            check_profile_scopes_granted_by_iam(entries=(entry,))

    def test_apac_is_granted_only_for_anthropic(self, tmp_path):
        (anthropic_entry,) = load_registry(_write(tmp_path, _doc(_entry(
            provider="anthropic", profile_scope="apac", model_family="opus",
            pricing_key="opus",
            bedrock_model_id=_geo_id("apac", "anthropic", "probe-test"),
            aliases=["probe-anthropic-apac"],
        )), "a.json"))
        check_profile_scopes_granted_by_iam(entries=(anthropic_entry,))

        for vendor, key in (("openai", "gpt-5.6-sol"), ("xai", "grok")):
            (entry,) = load_registry(_write(tmp_path, _doc(_entry(
                provider=vendor, profile_scope="apac", model_family="grant-probe",
                pricing_key=key,
                bedrock_model_id=_geo_id("apac", vendor, "probe-test"),
                aliases=[f"probe-{vendor}-apac"], wire_protocol="responses",
                bedrock_region="us-east-2",
            )), f"{vendor}.json"))
            with pytest.raises(ValueError):
                check_profile_scopes_granted_by_iam(entries=(entry,))

    def test_bare_model_grant_is_by_exact_id_for_nvidia_qwen_but_by_vendor_for_others(self, tmp_path):
        """The asymmetry ecs-stack.ts states explicitly in its own comment:
        nvidia/qwen are listed by EXACT foundation-model id, while
        anthropic/openai/xai bare ids ride a vendor-wide
        `foundation-model/<vendor>.*` grant. A check that treats every bare
        entry the same way (either "all pass" or "all fail") gets three of
        these four cases wrong. `nvidia`/`openai` bare ids have no geography
        segment at all, so the id/scope cross-check does not apply to them."""
        no_profile = _no_profile_scope()

        (shipped_nvidia,) = load_registry(_write(tmp_path, _doc(_entry(
            provider="nvidia", profile_scope=no_profile, model_family="nemotron",
            pricing_key="nemotron", bedrock_model_id="nvidia.nemotron-super-3-120b",
            aliases=["nemotron-bare-test"],
        )), "shipped-id.json"))
        check_profile_scopes_granted_by_iam(entries=(shipped_nvidia,))

        (other_nvidia,) = load_registry(_write(tmp_path, _doc(_entry(
            provider="nvidia", profile_scope=no_profile, model_family="some-other-nvidia",
            pricing_key="nemotron", bedrock_model_id="nvidia.some-other-model-test",
            aliases=["other-nvidia-bare-test"],
        )), "other-id.json"))
        with pytest.raises(ValueError):
            check_profile_scopes_granted_by_iam(entries=(other_nvidia,))

        (openai_bare,) = load_registry(_write(tmp_path, _doc(_entry(
            provider="openai", profile_scope=no_profile, model_family="some-openai-bare",
            pricing_key="gpt-5.6-sol", bedrock_model_id="openai.some-bare-model-test",
            aliases=["openai-bare-test"], wire_protocol="responses",
            bedrock_region="us-east-2",
        )), "openai-bare.json"))
        check_profile_scopes_granted_by_iam(entries=(openai_bare,))


# ---------------------------------------------------------------------------
# C16: a real 10/50 rate row for the pricing key PR3's global-scope Claude
# Fable 5 entry will carry.
# ---------------------------------------------------------------------------

# Not spelled out verbatim by the handoff -- inferred from the one concrete
# string its "Measured facts" section gives ("There is no `fable-global` key")
# and from defaults/pricing.json's own <family>-<qualifier> naming convention
# (opus-legacy, sonnet-5, sonnet-3, haiku-3-5, haiku-3, gpt-5.6-sol,
# gpt-5.6-terra all follow this shape). See module docstring: if the
# implementation picked a different literal, every test in this class fails on
# a KeyError/assertion naming this constant, which is the right failure mode
# to surface the mismatch rather than mask it.
FABLE_GLOBAL_KEY = "fable-global"


class _NoOverrideRepo:
    """A PricingConfigRepository stand-in with no admin overrides, matching the
    one already used in tests/test_registry_and_price_sources.py."""

    def current_version(self):
        return None

    def load_rates(self, version):  # pragma: no cover - not reached without a version
        return {}


class TestC16FableGlobalPricingRow:
    def test_the_row_exists_at_the_measured_global_rate(self):
        rates = price_sources.load_rate_document()
        assert FABLE_GLOBAL_KEY in rates, (
            f"{FABLE_GLOBAL_KEY!r} is missing from defaults/pricing.json -- "
            f"PR3's global-scope Claude Fable 5 entry would resolve this key to "
            f"`default` ($15/$75) via pricing.py:529's "
            f"`rates.get(key) or rates.get('default')` fallback, over-charging "
            f"relative to the measured $10/$50 global rate"
        )
        rate = rates[FABLE_GLOBAL_KEY]
        assert rate.input_per_mtok_microusd == 10_000_000
        assert rate.output_per_mtok_microusd == 50_000_000
        # "cache read is one tenth of input and cache write is 1.25x input in
        # every existing row" -- Measured facts.
        assert rate.cache_read_per_mtok_microusd == 1_000_000
        assert rate.cache_write_per_mtok_microusd == 12_500_000

    def test_the_row_resolves_to_itself_not_to_the_default_fallback(self):
        """Non-vacuous by construction: `default` is $15/$75 and
        `fable-global` is $10/$50, so if the key were absent, misspelled, or
        the resolution path fell through to `default` for any other reason,
        EVERY field below would read as the wrong (higher) number instead of
        silently matching by coincidence."""
        from mvp.pricing import _RateCache

        rate = _RateCache().get(FABLE_GLOBAL_KEY, _NoOverrideRepo())
        default = price_sources.load_rate_document()["default"]
        assert (rate.input_per_mtok_microusd, rate.output_per_mtok_microusd) == (
            10_000_000, 50_000_000,
        )
        assert (rate.input_per_mtok_microusd, rate.output_per_mtok_microusd) != (
            default.input_per_mtok_microusd, default.output_per_mtok_microusd,
        )

    def test_default_still_dominates_the_new_row(self):
        """Guards the invariant `test_pricing_floor.py`'s
        `test_default_dominates_every_bundled_provider_rate_leg` already
        enforces generically (it iterates every key in the bundled document),
        pinned again here specifically by name so a regression in the new row
        is legible from this file alone without cross-referencing that one."""
        from mvp.rates import RATE_FIELDS

        rates = price_sources.load_rate_document()
        row, default = rates[FABLE_GLOBAL_KEY], rates["default"]
        for leg in RATE_FIELDS:
            assert getattr(row, leg) <= getattr(default, leg), (
                f"fable-global.{leg} out-prices default -- an unpriced model "
                f"would now under-charge"
            )

    def test_the_row_is_tracked_in_the_bundled_document_not_only_a_test_constant(self):
        """A key that only this test's Python dict knows about is not the same
        as a key the bundled floor actually loads at process start -- read the
        row straight from defaults/pricing.json's own JSON, independent of
        `price_sources.load_rate_document()`'s parsing, so a bug in the parser
        cannot make this pass by both sides sharing one bug."""
        from pathlib import Path

        raw = json.loads(Path(price_sources.pricing_path()).read_text(encoding="utf-8"))
        assert FABLE_GLOBAL_KEY in raw.get("rates", {}), (
            f"{FABLE_GLOBAL_KEY!r} is not a key in defaults/pricing.json's own "
            f"\"rates\" object"
        )
