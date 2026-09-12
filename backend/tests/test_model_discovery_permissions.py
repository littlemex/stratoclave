"""The two new scopes an operator surface for discovered models needs:
`models:discover` (see a discovered record, its blockers and its evidence)
and `models:promote` (create a promotion candidate, and activate one).

Written from the frozen cross-unit decision alone, split-impl style: the
code author is blind to this file, and this file is blind to their code.
Neither scope exists in `mvp.authz.ALL_SCOPES`, `backend/permissions.json`
or the frontend copy on the commit this file was written against, so every
test below is expected to be RED until that lands -- that is the point of
writing them first, not a sign this file is wrong.

WHY TWO SCOPES, NOT ONE: seeing what discovery has found and acting on it
(minting a promotion candidate, activating it into the servable catalogue)
are different authorities -- the same split the entitlement scopes already
made (`entitlements:read` vs `entitlements:grant`) and the money-ceiling
scopes made a third time (`limits:raise-self` vs `limits:approve*`). Role
assignment is decided upstream, not by this file: `models:discover` goes to
both `admin` and `team_lead`; `models:promote` goes to `admin` alone,
because minting a candidate widens the public API surface and that is not a
team-lead act. The negative pin below -- team_lead does NOT hold
`models:promote` -- matters as much as the positive ones: a version that
handed every scope to every role would pass every "X holds Y" assertion and
still be wrong.

WHAT THIS FILE DOES NOT DO, ON PURPOSE:

- It does not re-assert that the frontend copy equals `permissions.json`
  per role. `frontend/src/lib/permissions.test.ts` already does that,
  generically, for every role -- once the two new scopes land in both
  files with the right role grants, that existing test starts covering
  them for free. A second copy of that comparison here would be the exact
  drift-by-duplication this whole permission-mirror design exists to avoid.
- It does not hand-maintain a second permission universe.
  `tests/test_authz_lattice.py::CONCRETE` is the universe
  `test_universe_is_complete` greps every `require_permission(...)` call
  against; the two new scopes are added there (not here) so that guard
  keeps working once a route gates on either of them.

THE VERSION BUMP: `permissions.json`'s seeder is a no-op when the stored
version already matches the file's `version` field
(`bootstrap/seed.py::seed_permissions` -> `PermissionsRepository.
seed_from_file`) -- this is the one failure mode every other test in this
file cannot catch, because every one of them reads the file directly rather
than through the idempotent seed path. A change that adds both scopes to
the roles but forgets to bump `version` ships a permissions.json that is
correct on disk and never reaches a deployed table with an existing row.
The test below reads the version at the change's own base commit -- rather
than a literal copied into this file -- so it cannot be satisfied by
copy-pasting the old string back in.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mvp.authz import _grants, ALL_SCOPES

NEW_SCOPES = ("models:discover", "models:promote")

# The commit this whole change is built on top of. Named once, here, as a
# plain git identifier -- not a document title or an item label -- because
# the version-bump test needs to read what shipped THERE, not guess it.
_BASE_COMMIT = "207c0b0"

_PERMISSIONS_PATH = Path(__file__).resolve().parent.parent / "permissions.json"


def _current_permissions() -> dict:
    return json.loads(_PERMISSIONS_PATH.read_text(encoding="utf-8"))


def _base_commit_version() -> str:
    """The `version` field `permissions.json` carried at the commit this
    change starts from, read from git history rather than hard-coded --
    the whole point being that copying the old literal into this file
    would make the bump test satisfiable by never bumping anything."""
    repo = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "show", f"{_BASE_COMMIT}:backend/permissions.json"],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip("git unavailable — cannot read the base commit's permissions.json")
    if result.returncode != 0:
        pytest.skip(
            f"base commit {_BASE_COMMIT} not reachable from this checkout "
            f"(shallow clone?) — cannot read its permissions.json"
        )
    return json.loads(result.stdout)["version"]


def _seed_permissions(dynamodb_mock):
    """Seed the real Permissions table from the repo's own permissions.json
    -- the same helper `test_entitlements_permission.py` and
    `test_capability_role_and_permissions.py` already use, not a private
    copy of it."""
    dynamodb_mock.create_table(
        TableName="stratoclave-permissions",
        KeySchema=[{"AttributeName": "role", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "role", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    from dynamo.permissions import PermissionsRepository

    repo = PermissionsRepository()
    repo.seed_from_file(_PERMISSIONS_PATH)
    return repo


def _clear_perm_cache():
    import mvp.authz as authz

    authz._clear_permissions_cache()


def _user(roles):
    from mvp.deps import AuthenticatedUser

    return AuthenticatedUser(
        user_id="u1", email="u1@example.com", roles=roles,
        org_id="default-org", auth_kind="jwt",
    )


# ---------------------------------------------------------------------------
# The scopes exist in the canonical backend universe.
# ---------------------------------------------------------------------------
def test_both_scopes_are_in_all_scopes():
    """Non-vacuous: on the base commit neither string is in ALL_SCOPES, so
    this fails until both land, and a future rename/typo of either literal
    fails it again."""
    for scope in NEW_SCOPES:
        assert scope in ALL_SCOPES, f"{scope!r} missing from mvp.authz.ALL_SCOPES"


# ---------------------------------------------------------------------------
# Role grants, exercised through the real evaluator -- not a re-derivation
# of it, so this can never drift from what a request path actually enforces.
# ---------------------------------------------------------------------------
def test_admin_holds_both_scopes(dynamodb_mock):
    _seed_permissions(dynamodb_mock)
    _clear_perm_cache()
    from mvp.authz import user_has_permission

    admin = _user(["admin"])
    for scope in NEW_SCOPES:
        assert user_has_permission(admin, scope) is True, (
            f"admin does not hold {scope!r} — every operator surface this "
            f"change adds is reachable by the root role"
        )


def test_team_lead_holds_discover_only(dynamodb_mock):
    """The positive half of team_lead's grant."""
    _seed_permissions(dynamodb_mock)
    _clear_perm_cache()
    from mvp.authz import user_has_permission

    team_lead = _user(["team_lead"])
    assert user_has_permission(team_lead, "models:discover") is True, (
        "team_lead must be able to see discovered records, their blockers "
        "and their evidence"
    )


def test_team_lead_does_not_hold_promote(dynamodb_mock):
    """The negative half, pinned on its own: a version that granted
    `models:promote` to every role would still pass every positive
    assertion above (admin holds it; team_lead holding it too would not
    fail `test_admin_holds_both_scopes`, which never checks team_lead at
    all). Promotion widens the public API surface, which is deliberately
    not a team-lead act -- this is the one assertion that would catch an
    over-grant."""
    _seed_permissions(dynamodb_mock)
    _clear_perm_cache()
    from mvp.authz import user_has_permission

    team_lead = _user(["team_lead"])
    assert user_has_permission(team_lead, "models:promote") is False, (
        "team_lead must not be able to create or activate a promotion "
        "candidate — only admin holds this authority"
    )


def test_end_user_holds_neither_scope(dynamodb_mock):
    """Least-privilege default for a new, catalogue-widening authority.
    True even before either scope lands (permissions.json has no `models:*`
    entries yet), so this guards against over-grant rather than proving the
    change landed -- the positive proof is the two tests above."""
    _seed_permissions(dynamodb_mock)
    _clear_perm_cache()
    from mvp.authz import user_has_permission

    end_user = _user(["user"])
    for scope in NEW_SCOPES:
        assert user_has_permission(end_user, scope) is False


def test_effective_permissions_admin_projects_both_scopes(dynamodb_mock):
    """The whoami projection must show the same answer `user_has_permission`
    enforces -- `test_effective_permissions_equals_enforcement`'s own
    invariant, specifically at the two new scopes."""
    _seed_permissions(dynamodb_mock)
    _clear_perm_cache()
    from mvp.authz import effective_permissions

    admin_perms = set(effective_permissions(_user(["admin"])))
    assert set(NEW_SCOPES) <= admin_perms


def test_effective_permissions_team_lead_excludes_promote(dynamodb_mock):
    """The projection's own negative pin, for the same reason
    `test_team_lead_does_not_hold_promote` is pinned separately from the
    admin-side positive test: a projection that leaked `models:promote`
    into team_lead's whoami result would tell the frontend to show a
    control the backend would then 403 on."""
    _seed_permissions(dynamodb_mock)
    _clear_perm_cache()
    from mvp.authz import effective_permissions

    team_lead_perms = set(effective_permissions(_user(["team_lead"])))
    assert "models:discover" in team_lead_perms
    assert "models:promote" not in team_lead_perms


# ---------------------------------------------------------------------------
# The version bump: the one failure mode no role-grant assertion above can
# see, because every one of them reads permissions.json directly rather
# than through the idempotent seed path.
# ---------------------------------------------------------------------------
def test_permissions_version_has_moved_since_the_base_commit():
    """Non-vacuous by construction: reads the OLD value from git history
    rather than a literal in this file, so the assertion cannot be
    satisfied by leaving the version untouched and this test unmodified.
    A version left exactly as it was on the base commit means
    `PermissionsRepository.seed_from_file`'s no-op-on-match path swallows
    the change -- both new roles' grants are then correct on disk and
    absent from a table that already has a row for `version`."""
    old_version = _base_commit_version()
    new_version = _current_permissions()["version"]
    assert new_version != old_version, (
        f"permissions.json's version is still {old_version!r} — the seeder "
        f"no-ops when the stored version matches, so the two new scopes "
        f"would never reach a table that already ran a seed at this version"
    )


def test_seeding_the_bumped_file_after_the_old_version_actually_changes_the_role(
    dynamodb_mock,
):
    """The concrete consequence of the bump, exercised through the real
    idempotent seed path rather than asserted only as a string compare.
    Seeds the table once at the OLD version/role shape (as if a prior
    deploy had already run), then seeds again from the real, current
    `permissions.json` -- the second seed must actually change the
    `admin` role's stored permissions to include both new scopes. If the
    version were left unbumped, `seed_from_file`'s own no-op-on-match
    behaviour would leave the table exactly as the first seed left it, and
    this assertion is what would catch that silently-skipped write."""
    dynamodb_mock.create_table(
        TableName="stratoclave-permissions",
        KeySchema=[{"AttributeName": "role", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "role", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    from dynamo.permissions import PermissionsRepository

    repo = PermissionsRepository()
    old_version = _base_commit_version()
    old_admin_permissions = json.loads(
        subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parents[2]),
             "show", f"{_BASE_COMMIT}:backend/permissions.json"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    )["roles"]["admin"]["permissions"]

    # Simulate a deploy that already ran the OLD seed.
    table = dynamodb_mock.Table("stratoclave-permissions")
    table.put_item(Item={
        "role": "admin", "version": old_version, "permissions": old_admin_permissions,
    })
    for scope in NEW_SCOPES:
        assert scope not in old_admin_permissions, (
            f"{scope!r} was already present on the base commit's admin role "
            f"— this fixture no longer isolates the version-bump effect"
        )

    repo.seed_from_file(_PERMISSIONS_PATH)
    _clear_perm_cache()
    stored_admin = repo.get("admin") or []
    # `_grants`, not `in`: the admin role holds whole domains as wildcards
    # (`models:*`), which is how every other domain reaches it too, so a literal
    # membership test would fail for a correctly-seeded role.
    for scope in NEW_SCOPES:
        assert any(_grants(held, scope) for held in stored_admin), (
            f"re-seeding from the current permissions.json did not add "
            f"{scope!r} to the stored admin role — either the version was "
            f"not bumped (the no-op path swallowed the write) or the scope "
            f"was never added to the file's admin role"
        )
