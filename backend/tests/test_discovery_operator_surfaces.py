"""The operator's surfaces for model discovery: reading what discovery found,
and the three actions that turn a finding into a servable model.

The specification this file is written against fixes what has to be true of
each surface (which blocker a listing keeps or drops, what an absent verdict
must read as, when a probe may spend money, what a compare-and-set at
activation must refuse) and, for every write, the exact HTTP path and method.
It leaves the JSON shapes open. This file commits to concrete request and
response field names and says so here, rather than refusing to write a test
until someone else decides -- the same move `test_discovery_operator_queue.py`
and `test_promotion_activation.py` already make for an identical gap in this
same suite.

Route paths: every write path is named literally by the specification. The
read paths are named literally too. None of them carries a prefix, so this
file mounts them on the discovery admin router's own existing prefix
(`/api/mvp/admin/discovery`) -- the same module and prefix the already-shipped
queue route uses, and the natural home for a same-domain surface following
this repository's `admin_<domain>.py` convention.

Field names this file commits to, none of them fixed anywhere else:

  - A record listing is `{"records": [...]}` and a candidate listing is
    `{"candidates": [...]}` -- a named key, not a bare list, matching the
    convention every other admin listing in this suite already uses (the
    queue's own `entries`, the entitlement store's own `grants`).
  - The record store carries no notion of a revision today. This file names
    the field `revision`, reusing the specification's own word rather than
    inventing a second one, and treats its value as opaque: read once from a
    single-record response and handed back unchanged on candidate creation.
    Nothing here assumes what the value is made of.
  - A created candidate's response names its identifiers under
    `newly_live_identifiers`, reusing the exact name of the already-shipped
    helper that computes them (`mvp.discovery.promotion.newly_live_identifiers`)
    rather than a fresh name for the same list.
  - A candidate's per-invocation verdict is reported under `verdicts`, keyed
    by invocation name, present for every invocation this build recognises
    whether or not a verdict has ever been recorded -- the presence of the key
    is what this file pins; the exact word chosen for "never probed" is not,
    since the specification never names one.

What this file does not attempt: two items appeared in the specification
after most of this file was drafted, and this file does not pin either,
because neither is safely constructible or safely resolvable from the
specification alone.

  - A claim that two different candidates can each be built for one alias,
    each obtain a valid verdict, and race to activate. Every public
    identifier a candidate could claim is already reserved, transactionally,
    at the moment that candidate is CREATED (`mvp.discovery.promotion.
    put_promotion_candidate`'s own identifier-reservation rows, already
    shipped and already exercised by this suite's own promotion tests) -- a
    second candidate naming an identifier the first already claimed cannot be
    created at all, so it can never reach the point of holding a verdict to
    race with. Building a test for the described race would mean bypassing
    that already-shipped guarantee to manufacture two colliding candidates,
    which would not be testing the real system. This looks like a genuine
    tension between the new item and already-merged, already-tested code
    rather than a gap this file can safely fill by picking a reading -- flagged
    for the integrator rather than guessed at.
  - A rule that a provider timeout returns a distinct "indeterminate" status
    rather than the ordinary completed-probe shape. The probe as merged
    classifies every transport exception, timeout included, into the same
    completed-probe-with-a-failed-assertion shape every other transport
    failure takes (`converse_call_failed`) -- nothing in the merged probe
    distinguishes a timeout from any other exception, so pinning a
    distinct status for it would be pinning a mechanism that does not exist
    anywhere in the code this file is allowed to read, on a distinction
    (ambiguous-vs-definite failure) the specification does not give this file
    a way to construct from outside. Also flagged rather than guessed at.

  What IS pinned instead, because it is the same underlying rule stated a
  different way and is fully constructible: a probe consults the CURRENT
  discovered record at the moment it runs, not a snapshot frozen when the
  candidate was created. A candidate created against a clean record still
  refuses, without spending, once that same record is rediscovered with a
  permanent blocker; a candidate created against a blocked record is probed
  successfully once that record is rediscovered clean. Candidate creation
  itself is blind to blockers -- nothing in `derive_candidate` reads them --
  so both starting points are constructible without any workaround.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.deps import AuthenticatedUser, get_current_user
from mvp.discovery.records import (
    Blocker,
    DiscoveredRecord,
    ObservationScope,
    put_discovered_record,
)
from mvp.discovery.promotion import PromotionCandidate, put_promotion_candidate
from mvp.discovery.verdict import ProbeVerdict, put_probe_verdict

_PERMISSIONS_PATH = Path(__file__).resolve().parent.parent / "permissions.json"

SYNC = "sync"

# A pricing key already present in the bundled floor -- shared with the
# shipped Opus-tier entries and already relied on elsewhere in this suite
# (`test_promotion_activation.py`, `test_discovery_probe.py`), so a candidate
# naming it is never refused for a reason this file is not testing.
_PRICING_KEY = "opus"


# ---------------------------------------------------------------------------
# App/permission scaffolding: the real evaluator, the real seeded
# permissions document, mounted the same way `test_entitlement_store.py`'s
# own `TestPermissionGate` mounts an admin router -- so a test that asks
# "can a discover-only caller activate" is asking the real dependency, not a
# monkeypatched stand-in for it.
# ---------------------------------------------------------------------------
def _seed_real_permissions(dynamodb_mock) -> None:
    dynamodb_mock.create_table(
        TableName="stratoclave-permissions",
        KeySchema=[{"AttributeName": "role", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "role", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    from dynamo.permissions import PermissionsRepository

    PermissionsRepository().seed_from_file(_PERMISSIONS_PATH)
    import mvp.authz as authz

    authz._clear_permissions_cache()


def _actor(roles: list[str], *, user_id: str = "operator-1") -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id, email=f"{user_id}@example.com", roles=roles,
        org_id="default-org", auth_kind="jwt",
    )


def _client_as(dynamodb_mock, roles: list[str]) -> TestClient:
    _seed_real_permissions(dynamodb_mock)
    from mvp.admin_discovery import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: _actor(roles)
    # raise_server_exceptions=False: a refusal that this file expects to
    # surface as a normal 4xx response must not instead blow up the test
    # itself if the real route lets an exception escape unhandled -- the
    # same reasoning `test_entitlement_store.py` gives for the identical
    # setting on its own admin-route client.
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Fixture builders. Every field is required by the frozen shape of the store
# it belongs to, mirroring `test_promotion_activation.py`'s own builders for
# the same dataclasses.
# ---------------------------------------------------------------------------
def _scope(**overrides) -> ObservationScope:
    base = dict(
        account="776010787911", region="us-east-1",
        credentials_fingerprint="abc123", observed_at="2026-09-01T00:00:00+00:00",
    )
    base.update(overrides)
    return ObservationScope(**base)


def _record(profile_id: str, **overrides) -> DiscoveredRecord:
    base = dict(
        profile_id=profile_id, provider="anthropic", profile_scope="us",
        model_family=profile_id.split(".", 2)[-1] if "." in profile_id else profile_id,
        jurisdiction_bounded=True, destination_regions=("us-east-1",),
        invocation_region="us-east-1", raw_id=profile_id,
        raw_payload={"inferenceProfileId": profile_id}, observation_scope=_scope(),
        blockers=(),
    )
    base.update(overrides)
    return DiscoveredRecord(**base)


_PERMANENT_BLOCKER = Blocker(
    type="no_agreement_offer", subtype="not_marketplace_metered",
    evidence="ValidationException: Agreement not supported for this model.",
)


def _candidate(profile_id: str, *, alias: str, **overrides) -> PromotionCandidate:
    base = dict(
        profile_id=profile_id, observation_scope=_scope(), state="candidate",
        aliases=(alias,), pricing_key=_PRICING_KEY, jurisdiction="us",
        provider="anthropic", bedrock_model_id=profile_id, bedrock_region="us-east-1",
        wire_protocol="messages", model_family=profile_id.split(".", 2)[-1],
        profile_scope="us", created_at="2026-09-01T00:00:00+00:00", created_by="admin-1",
    )
    base.update(overrides)
    return PromotionCandidate(**base)


def _verdict(profile_id: str, **overrides) -> ProbeVerdict:
    base = dict(
        profile_id=profile_id, observation_scope=_scope(), invocation=SYNC,
        verified_at="2026-09-01T01:00:00+00:00", verified_by="probe",
        pricing_key_at_verification=_PRICING_KEY, wire_protocol_verified="messages",
        state="verified",
    )
    base.update(overrides)
    return ProbeVerdict(**base)


@pytest.fixture(autouse=True)
def _fresh_composed_registry():
    """Every activation test in this file reads `mvp.models.registry_entries()`
    through the process-wide composed-registry cache, which deliberately
    outlives one request. Without dropping it around each test, one test's
    activation stays visible to the next through the cache after its backing
    rows are already gone -- the exact hazard `test_promotion_activation.py`'s
    own identically-named fixture exists to close."""
    from mvp.models import invalidate_composed_registry

    invalidate_composed_registry()
    yield
    invalidate_composed_registry()


# ---------------------------------------------------------------------------
# Fake Bedrock runtime + system-tenant ledger scaffolding for the probe
# route, reused verbatim from the shape `test_discovery_probe.py` already
# established for the domain-level `probe()` function -- this file drives the
# same call through the HTTP route instead.
# ---------------------------------------------------------------------------
class _FakeBedrockRuntime:
    def __init__(self, *, response: Optional[dict] = None, exc: Optional[BaseException] = None):
        self.response = response
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.response


def _valid_converse_response(*, input_tokens: int = 37, output_tokens: int = 11) -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens},
    }


def _converse_unsupported_error():
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": "ValidationException",
                    "Message": "This model does not support the Converse API operation."},
         "ResponseMetadata": {"HTTPStatusCode": 400}},
        "Converse",
    )


def _patch_bedrock(monkeypatch: pytest.MonkeyPatch, fake: _FakeBedrockRuntime) -> None:
    monkeypatch.setattr("mvp._bedrock_clients.bedrock_runtime_client", lambda *a, **k: fake)


@pytest.fixture
def _system_tenant_pool(dynamodb_mock):
    """Provisions the system tenant's dollar pool -- the probe's own domain
    function only provisions its per-user token ceiling
    (`mvp.discovery.ledger.ensure_system_tenant`), and every probe test in
    `test_discovery_probe.py` needs this SEPARATE pool row for a reservation
    to succeed at all. Reused verbatim rather than re-derived."""
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
    from dynamo.user_tenants import UserTenantsRepository
    from mvp.discovery.records import SYSTEM_TENANT_ID

    period = current_period()
    UserTenantsRepository().ensure(
        user_id="discovery-probe", tenant_id=SYSTEM_TENANT_ID, role="system",
        total_credit=1_000_000_000,
    )
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=SYSTEM_TENANT_ID, period=period, manual_limit_microusd=50_000_000,
    )
    return {"tenant_id": SYSTEM_TENANT_ID, "period": period}


@pytest.fixture(autouse=True)
def _unrestricted_system_scope(monkeypatch: pytest.MonkeyPatch):
    """Every probe test in this file wants the system tenant unrestricted on
    the scope axis -- the one axis it is still subject to
    (`mvp.discovery.ledger.check_probe_scope_eligibility`) -- so a probe
    against a `us`-scoped fixture record never trips on an axis this file is
    not testing."""
    from mvp.routing.model_resolver import RoutingConfig

    monkeypatch.setattr(
        "mvp.routing.config.get_tenant_routing_config", lambda tenant_id: RoutingConfig(),
    )


def _usage_rows_for(tenant_id: str) -> list[dict]:
    from boto3.dynamodb.conditions import Attr
    from dynamo.client import get_dynamodb_resource, usage_logs_table_name

    table = get_dynamodb_resource().Table(usage_logs_table_name())
    return table.scan(FilterExpression=Attr("tenant_id").eq(tenant_id)).get("Items", [])


# ===========================================================================
# The record listing: a permanent blocker is kept here even though the
# already-shipped queue deliberately drops it.
# ===========================================================================
def test_a_permanently_blocked_record_appears_in_the_listing_the_queue_omits_it_from(
    dynamodb_mock,
):
    """An operator asking "why can this model not be promoted" needs an
    answer even for the ordinary, forever-blocked shape every AWS-billed
    family in a real account carries (`not_marketplace_metered`) -- the
    queue is right to hide it as a task nobody can act on, but hiding it
    from every surface would leave that question with no answer anywhere.
    Checked on the SAME record through both surfaces, mounted in the same
    request, so a listing that quietly reused the queue's own filter would
    be caught here rather than passing a queue-shaped test by coincidence."""
    profile_id = "us.acme.forever-blocked-v1"
    put_discovered_record(_record(profile_id, blockers=(_PERMANENT_BLOCKER,)))

    client = _client_as(dynamodb_mock, ["team_lead"])
    records_resp = client.get("/api/mvp/admin/discovery/records")
    assert records_resp.status_code == 200, records_resp.text
    listed_ids = {r["profile_id"] for r in records_resp.json()["records"]}
    assert profile_id in listed_ids, (
        "a permanently blocked record is missing from the record listing -- "
        "an operator has no surface left to ask why this profile cannot be "
        "promoted"
    )

    queue_resp = client.get("/api/mvp/admin/discovery/queue")
    assert queue_resp.status_code == 200, queue_resp.text
    queued_ids = {e["profile_id"] for e in queue_resp.json()["entries"]}
    assert profile_id not in queued_ids, (
        "the permanently blocked profile appeared in the actionable queue -- "
        "that queue exists to be clearable, and this blocker never clears "
        "on its own"
    )


def test_the_single_record_read_404s_for_an_unknown_profile_and_carries_evidence_when_known(
    dynamodb_mock,
):
    client = _client_as(dynamodb_mock, ["team_lead"])

    missing = client.get("/api/mvp/admin/discovery/records/no.such.profile")
    assert missing.status_code == 404, missing.text

    profile_id = "us.acme.evidenced-v1"
    put_discovered_record(_record(profile_id, blockers=(_PERMANENT_BLOCKER,)))
    found = client.get(f"/api/mvp/admin/discovery/records/{profile_id}")
    assert found.status_code == 200, found.text
    body = found.json()
    assert body["profile_id"] == profile_id
    evidence_seen = [b.get("evidence") for b in body.get("blockers", [])]
    assert _PERMANENT_BLOCKER.evidence in evidence_seen, (
        f"the single-record read did not carry the blocker's evidence -- "
        f"got blockers={body.get('blockers')!r}"
    )


def test_records_read_is_gated_on_the_discover_scope_not_open_to_every_caller(dynamodb_mock):
    put_discovered_record(_record("us.acme.gate-check-v1"))
    client = _client_as(dynamodb_mock, ["user"])
    resp = client.get("/api/mvp/admin/discovery/records")
    assert resp.status_code == 403, resp.text


# ===========================================================================
# The candidate listing/read: an unprobed candidate reads as unverified, not
# as an absent verdict.
# ===========================================================================
def test_a_candidate_with_no_verdict_reads_as_unverified_not_as_a_missing_field(dynamodb_mock):
    profile_id = "us.acme.unprobed-v1"
    put_discovered_record(_record(profile_id))
    put_promotion_candidate(_candidate(profile_id, alias="acme-unprobed-v1"))

    client = _client_as(dynamodb_mock, ["team_lead"])
    resp = client.get(f"/api/mvp/admin/discovery/candidates/{profile_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    verdicts = body.get("verdicts")
    assert isinstance(verdicts, dict), (
        f"expected a verdicts mapping keyed by invocation, got {verdicts!r}"
    )
    assert SYNC in verdicts, (
        "the sync invocation is absent from the verdicts mapping for a "
        "candidate that has never been probed -- absence reads as 'no "
        "information', which is a different, weaker fact than 'not yet "
        "verified'"
    )
    assert verdicts[SYNC].get("state") != "verified", (
        f"an unprobed candidate reported a verified state: {verdicts[SYNC]!r}"
    )

    # Non-vacuity: once a verdict really does exist, the SAME key reports it.
    put_probe_verdict(_verdict(profile_id))
    resp2 = client.get(f"/api/mvp/admin/discovery/candidates/{profile_id}")
    assert resp2.json()["verdicts"][SYNC].get("state") == "verified", (
        "a real, stored, verified verdict is not reflected under the same "
        "key an absent one was reported under -- the two cases must use "
        "the same field to be comparable at all"
    )


def test_candidate_listing_includes_a_created_candidate_and_404s_for_an_unknown_one(
    dynamodb_mock,
):
    profile_id = "us.acme.listed-v1"
    put_discovered_record(_record(profile_id))
    put_promotion_candidate(_candidate(profile_id, alias="acme-listed-v1"))

    client = _client_as(dynamodb_mock, ["team_lead"])
    listing = client.get("/api/mvp/admin/discovery/candidates")
    assert listing.status_code == 200, listing.text
    assert profile_id in {c["profile_id"] for c in listing.json()["candidates"]}

    missing = client.get("/api/mvp/admin/discovery/candidates/no.such.candidate")
    assert missing.status_code == 404, missing.text


# ===========================================================================
# Candidate creation: a stale record revision refuses; each missing human
# field refuses on its own reason and names it; the response names both
# newly-live identifiers.
# ===========================================================================
def _create_body(*, revision: str, alias: str = "acme-created-v1",
                  pricing_key: Optional[str] = _PRICING_KEY,
                  jurisdiction: Optional[str] = "us",
                  aliases: Optional[list] = None) -> dict:
    body = {
        "profile_id": "us.acme.created-v1",
        "revision": revision,
        "aliases": [alias] if aliases is None else aliases,
        "wire_protocol": "messages",
    }
    if pricing_key is not None:
        body["pricing_key"] = pricing_key
    if jurisdiction is not None:
        body["jurisdiction"] = jurisdiction
    return body


def test_creating_a_candidate_against_a_stale_revision_refuses(dynamodb_mock):
    profile_id = "us.acme.created-v1"
    put_discovered_record(_record(profile_id))
    client = _client_as(dynamodb_mock, ["admin"])

    stale_revision = client.get(
        f"/api/mvp/admin/discovery/records/{profile_id}"
    ).json()["revision"]

    # A re-discovery pass between the read above and the create below --
    # exactly the sequence the specification says must not be able to
    # silently change what a candidate derives from.
    put_discovered_record(_record(
        profile_id, raw_payload={"inferenceProfileId": profile_id, "status": "REFRESHED"},
        observation_scope=_scope(observed_at="2026-09-05T00:00:00+00:00"),
    ))

    stale_resp = client.post(
        "/api/mvp/admin/discovery/candidates", json=_create_body(revision=stale_revision),
    )
    assert stale_resp.status_code == 409, stale_resp.text

    fresh_revision = client.get(
        f"/api/mvp/admin/discovery/records/{profile_id}"
    ).json()["revision"]
    assert fresh_revision != stale_revision, (
        "the record's own revision did not change across a re-discovery "
        "pass -- this test cannot tell a real staleness refusal from a "
        "refusal that would have fired regardless"
    )
    fresh_resp = client.post(
        "/api/mvp/admin/discovery/candidates", json=_create_body(revision=fresh_revision),
    )
    assert fresh_resp.status_code == 201, (
        f"the SAME creation attempt, against the CURRENT revision, was "
        f"still refused: {fresh_resp.text}"
    )


_OMIT = object()  # a sentinel meaning "leave this key out of the request body"


@pytest.mark.parametrize(
    "missing_field, body_overrides, must_name",
    [
        # An empty value AND an omitted key, for each field. The distinction is
        # not academic: a field that is required at the wire is rejected by the
        # framework when it is OMITTED, in the framework's own error shape,
        # while an empty value still reaches this surface's own vocabulary. A
        # real run refused two of these three in the wrong shape while every
        # empty-value case passed, so only the omission cases catch it.
        ("aliases", {"aliases": []}, "alias"),
        ("aliases", {"aliases": _OMIT}, "alias"),
        ("pricing_key", {"pricing_key": None}, "pricing_key"),
        ("pricing_key", {"pricing_key": _OMIT}, "pricing_key"),
        ("jurisdiction", {"jurisdiction": None}, "jurisdiction"),
        ("jurisdiction", {"jurisdiction": _OMIT}, "jurisdiction"),
    ],
)
def test_a_missing_human_field_refuses_on_its_own_reason_naming_the_field(
    dynamodb_mock, missing_field, body_overrides, must_name,
):
    profile_id = "us.acme.missing-field-v1"
    put_discovered_record(_record(profile_id))
    client = _client_as(dynamodb_mock, ["admin"])
    revision = client.get(f"/api/mvp/admin/discovery/records/{profile_id}").json()["revision"]

    body = _create_body(revision=revision)
    body["profile_id"] = profile_id
    for key, value in body_overrides.items():
        if value is _OMIT:
            body.pop(key, None)
        else:
            body[key] = value
    for key, value in list(body.items()):
        if value is None:
            del body[key]

    resp = client.post("/api/mvp/admin/discovery/candidates", json=body)
    assert resp.status_code == 422, (
        f"a missing {missing_field!r} was not refused as an invalid human "
        f"field: {resp.status_code} {resp.text}"
    )
    # Substring-matching the whole body is not enough, and a real run proved it:
    # the web framework's own validation error also contains the field name, so
    # two of these three passed while refusing in a shape this surface does not
    # promise. Assert the shape -- one refusal vocabulary, with `type` and
    # `field` -- so a required-at-the-wire field is caught here instead of by
    # someone reading a response on a running gateway.
    detail = resp.json()["detail"]
    assert isinstance(detail, dict), (
        f"a missing {missing_field!r} was refused by the framework rather than "
        f"by this surface: an operator gets a different shape for this field "
        f"than for its siblings. {resp.text}"
    )
    assert detail.get("field") == missing_field, (
        f"the refusal does not name {missing_field!r} in its own `field`: {detail}"
    )
    assert must_name in detail.get("type", ""), (
        f"the refusal's type does not identify the missing field: {detail}"
    )


def test_a_complete_candidate_creation_names_both_newly_live_identifiers(dynamodb_mock):
    profile_id = "us.acme.complete-v1"
    put_discovered_record(_record(profile_id))
    client = _client_as(dynamodb_mock, ["admin"])
    revision = client.get(f"/api/mvp/admin/discovery/records/{profile_id}").json()["revision"]

    resp = client.post(
        "/api/mvp/admin/discovery/candidates",
        json={
            "profile_id": profile_id, "revision": revision,
            "aliases": ["acme-complete-v1"], "pricing_key": _PRICING_KEY,
            "jurisdiction": "us", "wire_protocol": "messages",
        },
    )
    assert resp.status_code == 201, resp.text
    identifiers = set(resp.json().get("newly_live_identifiers") or [])
    assert identifiers == {"acme-complete-v1", profile_id}, (
        f"expected both the declared alias and the Bedrock id it would also "
        f"make reachable, got {identifiers!r}"
    )


def test_candidate_creation_is_gated_on_the_promote_scope(dynamodb_mock):
    profile_id = "us.acme.gate-write-v1"
    put_discovered_record(_record(profile_id))
    client = _client_as(dynamodb_mock, ["team_lead"])  # discover only
    revision = client.get(f"/api/mvp/admin/discovery/records/{profile_id}").json()["revision"]

    resp = client.post(
        "/api/mvp/admin/discovery/candidates", json=_create_body(revision=revision),
    )
    assert resp.status_code == 403, (
        f"a discover-only caller was able to create a promotion candidate: "
        f"{resp.status_code} {resp.text}"
    )


# ===========================================================================
# The probe route: a permanent blocker on the CURRENT record refuses before
# any money is spent; a failed assertion is a completed probe, not an error
# status; the current record decides, not the candidate's own snapshot.
# ===========================================================================
def test_probing_a_candidate_whose_current_record_is_permanently_blocked_spends_nothing(
    dynamodb_mock,
):
    profile_id = "us.acme.blocked-probe-v1"
    put_discovered_record(_record(profile_id, blockers=(_PERMANENT_BLOCKER,)))
    put_promotion_candidate(_candidate(profile_id, alias="acme-blocked-probe-v1"))

    fake = _FakeBedrockRuntime(response=_valid_converse_response())

    def _never_built(*a, **k):  # pragma: no cover - defence in depth
        raise AssertionError("a refused probe must never build a Bedrock client at all")

    client = _client_as(dynamodb_mock, ["admin"])
    resp = client.post(
        f"/api/mvp/admin/discovery/candidates/{profile_id}/probe", json={"invocation": SYNC},
    )
    assert resp.status_code == 409, resp.text
    assert fake.calls == [], "the probe reached the provider despite a permanent blocker"
    from mvp.discovery.records import SYSTEM_TENANT_ID

    assert _usage_rows_for(SYSTEM_TENANT_ID) == [], (
        "a refused probe left a usage row on the system tenant's ledger -- "
        "money moved for an attempt that was never supposed to reach the "
        "provider"
    )


def test_a_failed_probe_assertion_is_a_200_with_a_failed_verdict_not_an_error_status(
    dynamodb_mock, _system_tenant_pool, monkeypatch,
):
    profile_id = "us.acme.failed-probe-v1"
    put_discovered_record(_record(profile_id))
    put_promotion_candidate(_candidate(profile_id, alias="acme-failed-probe-v1"))

    _patch_bedrock(monkeypatch, _FakeBedrockRuntime(exc=_converse_unsupported_error()))

    client = _client_as(dynamodb_mock, ["admin"])
    resp = client.post(
        f"/api/mvp/admin/discovery/candidates/{profile_id}/probe", json={"invocation": SYNC},
    )
    assert resp.status_code == 200, (
        "a probe whose own assertion failed must still report a COMPLETED "
        f"operation -- a non-200 here means the tempting-but-wrong reading "
        f"that a failed assertion is an error: {resp.status_code} {resp.text}"
    )
    body = resp.json()
    assert body.get("passed") is False, body
    assert body.get("blocker", {}).get("type") == "protocol_unverified", body


def test_a_passing_probe_returns_200_verified_and_a_non_zero_charge(
    dynamodb_mock, _system_tenant_pool, monkeypatch,
):
    """Non-vacuity for the two probe-status tests above: the SAME route,
    against a clean candidate and a Bedrock double that actually answers,
    must report success rather than refusing or failing everything
    regardless of input."""
    profile_id = "us.acme.passing-probe-v1"
    put_discovered_record(_record(profile_id))
    put_promotion_candidate(_candidate(profile_id, alias="acme-passing-probe-v1"))

    _patch_bedrock(monkeypatch, _FakeBedrockRuntime(response=_valid_converse_response()))

    client = _client_as(dynamodb_mock, ["admin"])
    resp = client.post(
        f"/api/mvp/admin/discovery/candidates/{profile_id}/probe", json={"invocation": SYNC},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("passed") is True, body
    assert body.get("charged_microusd", 0) > 0, (
        "assertion 4 requires a non-zero charge, not merely a successful call"
    )


def test_probe_refuses_once_the_current_record_is_blocked_even_though_the_candidate_predates_it(
    dynamodb_mock,
):
    profile_id = "us.acme.blocked-after-creation-v1"
    put_discovered_record(_record(profile_id))  # clean at candidate-creation time
    put_promotion_candidate(_candidate(profile_id, alias="acme-blocked-after-v1"))

    # A later reconciliation pass finds a permanent blocker this candidate's
    # own creation never saw.
    put_discovered_record(_record(profile_id, blockers=(_PERMANENT_BLOCKER,)))

    fake = _FakeBedrockRuntime(response=_valid_converse_response())
    client = _client_as(dynamodb_mock, ["admin"])
    resp = client.post(
        f"/api/mvp/admin/discovery/candidates/{profile_id}/probe", json={"invocation": SYNC},
    )
    assert resp.status_code == 409, (
        f"a candidate created before its record was blocked was still "
        f"probed against the STALE, clean snapshot: {resp.status_code} {resp.text}"
    )
    assert fake.calls == [], "the probe reached the provider despite the current record's blocker"


def test_probe_succeeds_once_the_current_record_is_cleared_even_though_the_candidate_predates_it(
    dynamodb_mock, _system_tenant_pool, monkeypatch,
):
    profile_id = "us.acme.cleared-after-creation-v1"
    # Blocked at candidate-creation time -- creation itself never reads
    # blockers, so this succeeds regardless.
    put_discovered_record(_record(profile_id, blockers=(_PERMANENT_BLOCKER,)))
    put_promotion_candidate(_candidate(profile_id, alias="acme-cleared-after-v1"))

    # A later pass clears it.
    put_discovered_record(_record(profile_id, blockers=()))

    _patch_bedrock(monkeypatch, _FakeBedrockRuntime(response=_valid_converse_response()))
    client = _client_as(dynamodb_mock, ["admin"])
    resp = client.post(
        f"/api/mvp/admin/discovery/candidates/{profile_id}/probe", json={"invocation": SYNC},
    )
    assert resp.status_code == 200, (
        f"a candidate whose record has SINCE been cleared was still refused "
        f"against its stale, blocked snapshot: {resp.status_code} {resp.text}"
    )
    assert resp.json().get("passed") is True, resp.json()


def test_probe_is_gated_on_the_promote_scope_not_the_discover_scope_alone(dynamodb_mock):
    """The specification's own second reason: a verdict is half of promote's
    authority, so probing must be gated exactly like the other two writes,
    never left reachable by discover alone."""
    profile_id = "us.acme.gate-probe-v1"
    put_discovered_record(_record(profile_id))
    put_promotion_candidate(_candidate(profile_id, alias="acme-gate-probe-v1"))

    client = _client_as(dynamodb_mock, ["team_lead"])  # discover only
    resp = client.post(
        f"/api/mvp/admin/discovery/candidates/{profile_id}/probe", json={"invocation": SYNC},
    )
    assert resp.status_code == 403, (
        f"a discover-only caller was able to probe -- probing manufactures "
        f"the verdict that authorises activation, and must not be reachable "
        f"by a caller who cannot activate: {resp.status_code} {resp.text}"
    )


# ===========================================================================
# Activation: a mismatched verdict identity refuses; re-activating an
# already-active candidate is idempotent, per the merged activation
# function's own stated design.
# ===========================================================================
def _seed_for_activation(profile_id: str, *, alias: str, verdict_overrides: dict):
    put_discovered_record(_record(profile_id))
    put_promotion_candidate(_candidate(profile_id, alias=alias))
    seeded = _verdict(profile_id, **verdict_overrides)
    put_probe_verdict(seeded)
    return seeded


def test_activation_with_a_mismatched_verdict_identity_refuses(dynamodb_mock):
    profile_id = "us.acme.mismatched-verdict-v1"
    # The verdict is current and speaks the declared protocol, but it was
    # earned against a DIFFERENT pricing key than the candidate now names --
    # the compare-and-set this route exists to enforce.
    seeded = _seed_for_activation(
        profile_id, alias="acme-mismatched-v1",
        verdict_overrides={"pricing_key_at_verification": "sonnet"},
    )
    client = _client_as(dynamodb_mock, ["admin"])
    resp = client.post(
        f"/api/mvp/admin/discovery/candidates/{profile_id}/activate", json={"invocation": SYNC, "verified_at": seeded.verified_at},
    )
    assert resp.status_code == 409, (
        f"activation succeeded against a verdict that verified a DIFFERENT "
        f"pricing key than the candidate now names: {resp.status_code} {resp.text}"
    )
    from mvp.models import registry_entries

    assert not any("acme-mismatched-v1" in e.aliases for e in registry_entries()), (
        "the model became reachable despite the pricing-key mismatch"
    )


def test_reactivating_an_already_active_candidate_is_idempotent(dynamodb_mock):
    """Pinned per the merged activation function's own stated design:
    re-running activation for an already-active profile re-derives and
    re-writes the same entry rather than treating 'already active' as a
    distinct, refusable state."""
    profile_id = "us.acme.reactivate-v1"
    seeded = _seed_for_activation(profile_id, alias="acme-reactivate-v1", verdict_overrides={})
    client = _client_as(dynamodb_mock, ["admin"])

    first = client.post(
        f"/api/mvp/admin/discovery/candidates/{profile_id}/activate", json={"invocation": SYNC, "verified_at": seeded.verified_at},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        f"/api/mvp/admin/discovery/candidates/{profile_id}/activate", json={"invocation": SYNC, "verified_at": seeded.verified_at},
    )
    assert second.status_code == 200, (
        f"re-activating an already-active candidate did not behave "
        f"idempotently: {second.status_code} {second.text}"
    )
    from mvp.models import registry_entries

    assert any("acme-reactivate-v1" in e.aliases for e in registry_entries())


def test_activation_is_gated_on_the_promote_scope(dynamodb_mock):
    profile_id = "us.acme.gate-activate-v1"
    seeded = _seed_for_activation(profile_id, alias="acme-gate-activate-v1", verdict_overrides={})

    client = _client_as(dynamodb_mock, ["team_lead"])  # discover only
    resp = client.post(
        f"/api/mvp/admin/discovery/candidates/{profile_id}/activate", json={"invocation": SYNC, "verified_at": seeded.verified_at},
    )
    assert resp.status_code == 403, (
        f"a discover-only caller was able to activate: {resp.status_code} {resp.text}"
    )
    from mvp.models import registry_entries

    assert not any("acme-gate-activate-v1" in e.aliases for e in registry_entries()), (
        "the model became reachable despite the actor lacking the promotion "
        "permission"
    )


# --- a refusal has to name the FACT, not just its reason token ----------------
#
# Measured against a running gateway: seven of the eight promotion refusals came
# back with `message` equal to their own reason string, so a caller was told
# `provider_unsupported` and nothing they could act on. Only `identifier_taken`
# named the conflicting fact. The contract's status clause says every 409 names
# it, and nothing here checked that.
@pytest.mark.parametrize(
    "overrides, expected_type, must_appear",
    [
        # The value the caller supplied, so they can see what was read.
        ({"pricing_key": "default"}, "pricing_key_is_default", "default"),
        # The closed set, so they can pick from it rather than guess.
        ({"wire_protocol": "telepathy"}, "protocol_mismatch", "messages"),
    ],
)
def test_a_refusal_names_the_conflicting_fact_not_only_its_reason(
    dynamodb_mock, overrides, expected_type, must_appear,
):
    profile_id = "us.acme.names-the-fact-v1"
    put_discovered_record(_record(profile_id))
    client = _client_as(dynamodb_mock, ["admin"])
    revision = client.get(f"/api/mvp/admin/discovery/records/{profile_id}").json()["revision"]

    body = _create_body(revision=revision)
    body["profile_id"] = profile_id
    body.update(overrides)

    resp = client.post("/api/mvp/admin/discovery/candidates", json=body)

    assert resp.status_code in (409, 422), resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, dict), "a refusal returned a bare string or a list"
    assert detail["type"] == expected_type, detail
    message = str(detail.get("message", ""))
    assert message != detail["type"], (
        f"the message for {expected_type} is just the reason token again, so it "
        "tells the caller nothing they can act on"
    )
    assert must_appear in message, f"the refusal does not name {must_appear!r}: {message!r}"


@pytest.mark.parametrize(
    "path_suffix, body, missing_field",
    [
        ("/probe", {}, "invocation"),
        ("/activate", {"verified_at": "2026-01-01T00:00:00+00:00"}, "invocation"),
        ("/activate", {"invocation": "sync"}, "verified_at"),
    ],
)
def test_an_omitted_field_on_probe_or_activate_refuses_in_this_surfaces_vocabulary(
    dynamodb_mock, path_suffix, body, missing_field,
):
    """These fields were required at the wire, so an OMITTED one was rejected by
    the framework in its own error shape -- a list of
    `{"type": "missing", "loc": [...]}` -- where every other refusal here returns
    `{"type", "field", "message"}`. Found on a real gateway, because the tests
    only ever sent the fields."""
    profile_id = "us.acme.omitted-on-write-v1"
    put_discovered_record(_record(profile_id))
    client = _client_as(dynamodb_mock, ["admin"])
    revision = client.get(f"/api/mvp/admin/discovery/records/{profile_id}").json()["revision"]
    created = client.post(
        "/api/mvp/admin/discovery/candidates",
        json={**_create_body(revision=revision), "profile_id": profile_id},
    )
    assert created.status_code == 201, created.text

    resp = client.post(
        f"/api/mvp/admin/discovery/candidates/{profile_id}{path_suffix}", json=body,
    )

    assert resp.status_code in (409, 422), resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, dict), (
        f"omitting {missing_field} was refused in the framework's shape, not this "
        f"surface's: {resp.text[:200]}"
    )
    assert detail.get("field") == missing_field, detail


def test_a_row_this_build_cannot_parse_is_named_rather_than_dropped(dynamodb_mock):
    """A row whose schema this build does not know is correctly skipped by every
    reader that reasons about records. But the operator surface owes the
    opposite: a row no screen mentions is durable state nobody is looking for.

    Measured on a real store before this: 76 rows, 75 listed, and the one
    difference was an empty-`profile_id` row written by the malformed-summary
    defect. Nothing anywhere reported that it existed.
    """
    from dynamo.client import user_tenants_table_name

    put_discovered_record(_record("us.acme.readable-v1"))

    # A row under the same reserved prefix with a schema version from a
    # different deploy -- the realistic shape, not a corrupt blob.
    table = dynamodb_mock.Table(user_tenants_table_name())
    table.put_item(Item={
        "user_id": "DISCOVERED#us.acme.from-another-deploy-v1",
        "sk": "PROFILE",
        "tenant_id": "SYSTEM",
        "profile_id": "us.acme.from-another-deploy-v1",
        "schema_version": 999,
    })

    client = _client_as(dynamodb_mock, ["admin"])
    body = client.get("/api/mvp/admin/discovery/records").json()

    listed = {r["profile_id"] for r in body["records"]}
    assert "us.acme.readable-v1" in listed
    assert "us.acme.from-another-deploy-v1" not in listed, (
        "a row this build cannot parse was returned as if it were understood"
    )
    assert any("from-another-deploy" in key for key in body["unreadable_rows"]), (
        "the unparseable row was dropped silently: "
        f"unreadable_rows={body['unreadable_rows']}"
    )


def test_the_unreadable_list_is_empty_when_every_row_parses(dynamodb_mock):
    """The positive half, so an empty list is a statement rather than the
    absence of one -- and so the test above cannot pass by the field simply
    always being populated."""
    put_discovered_record(_record("us.acme.all-readable-v1"))
    client = _client_as(dynamodb_mock, ["admin"])
    body = client.get("/api/mvp/admin/discovery/records").json()
    assert body["unreadable_rows"] == []
    assert len(body["records"]) == 1
