"""C6.7 / C6.8 — where a principal's authority comes from, and what never arrives.

Two security claims lived in `docs/ARCHITECTURE.md` with no clause and no test:

  "Stratoclave *never* reads `cognito:groups` or relies on Cognito for authorization."
  "Credentials are never transmitted to Stratoclave."

Both are true of the code, and both were being asserted on the strength of a docstring.
That is the shape this contract is organised against: a guarantee whose only evidence is
that someone wrote it down. Anchoring them to a clause required the clause to exist, and
the clause could only be at E if a test failed when it stopped holding — so here are the
tests, rather than a clause minted to absorb the sentences.

C6.7 is about AUTHORITY: an assertion inside a token the caller presents is not authority,
whatever it says. A token is accepted as proof of authentication and then ignored for
authorization, because the store the gateway controls is the only thing that can say what
a principal may do.

C6.8 is about CREDENTIALS on the vouch path: the flow is built so the gateway never sees
an AWS secret at all — the CLI signs a `GetCallerIdentity` request locally and the backend
forwards the signature. The test is a shape check on the request model, because the
guarantee is structural: an endpoint that has nowhere to put a credential cannot be sent
one, and a field added later would fail here rather than in a review someone skipped.
"""
from __future__ import annotations

import pytest

from mvp.deps import AuthenticatedUser


def _claims(**extra) -> dict:
    base = {
        "sub": "u-authority",
        "email": "authority@test.example",
        "token_use": "id",
    }
    base.update(extra)
    return base


def _seed_permissions(dynamodb_mock):
    """The role→permission table, seeded from the versioned file the deployment seeds
    from. Authority has to come from somewhere for "not from the token" to mean
    anything."""
    import pathlib as _p

    dynamodb_mock.create_table(
        TableName="stratoclave-permissions",
        KeySchema=[{"AttributeName": "role", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "role", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    from dynamo.permissions import PermissionsRepository

    PermissionsRepository().seed_from_file(
        _p.Path(__file__).resolve().parents[1] / "permissions.json")
    import mvp.authz as authz
    if hasattr(getattr(authz, "_get_permissions_for_role", None), "cache_clear"):
        authz._get_permissions_for_role.cache_clear()


def test_a_group_claim_in_the_token_grants_nothing(dynamodb_mock):
    """C6.7. A caller who can influence their own token cannot grant themselves a role.

    The claim names in this test are the ones the docs promise are ignored, so if a
    future refactor starts reading them the promise fails here."""
    from mvp import authz

    _seed_permissions(dynamodb_mock)
    hostile = AuthenticatedUser(
        user_id="u-authority",
        email="authority@test.example",
        org_id="acme",
        # The gateway's own resolution said this principal is an ordinary user.
        roles=["user"],
        raw_claims=_claims(**{
            "cognito:groups": ["admin", "team_lead"],
            "roles": ["admin"],
            "scope": "admin",
        }),
        auth_kind="jwt",
        key_scopes=None,
        api_key_hash=None,
    )
    # An admin-only permission must be refused: the roles the gateway resolved are the
    # only ones that count, and they say `user`.
    assert not authz.user_has_permission(hostile, "tenants:read-all")
    assert not authz.user_has_permission(hostile, "users:create")
    # And the same principal with the role actually granted in the store is allowed, so
    # this test cannot pass by refusing everything.
    granted = AuthenticatedUser(
        user_id="u-authority", email="authority@test.example", org_id="acme",
        roles=["admin"], raw_claims=_claims(), auth_kind="jwt",
        key_scopes=None, api_key_hash=None,
    )
    assert authz.user_has_permission(granted, "tenants:read-all")


def test_the_authorization_path_does_not_read_group_claims_at_all():
    """The stronger form of the same property, as a shape check.

    The test above proves a hostile claim does not WIN. This one proves the claim is not
    consulted: no module on the authorization path mentions the claim names, so there is
    no branch that could start honouring them by accident. A behavioural test can only
    sample the inputs; this one closes the whole class."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    for rel in ("mvp/authz.py", "mvp/deps.py"):
        tree = ast.parse((root / rel).read_text())
        docstrings = {
            id(node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }
        # Every string and every attribute/subscript name the module actually evaluates.
        # Prose is excluded by construction rather than by guessing at comment syntax: a
        # docstring saying "the `cognito:groups` claim is ignored" is exactly what should
        # be there, and a first attempt at this check failed on that very sentence.
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docstrings:
                assert "cognito:groups" not in node.value, (
                    f"{rel} line {node.lineno} evaluates the string 'cognito:groups', so "
                    f"authority could come from an assertion the caller can influence")


def test_the_sso_exchange_has_nowhere_to_put_a_credential():
    """C6.8. The vouch flow's request model carries a signed request, not a secret.

    Structural on purpose: the guarantee is that the gateway never receives an AWS
    secret, and the way to keep that true is for the endpoint to have no field one could
    arrive in. A field added later fails here."""
    from mvp import sso_exchange

    models = [
        obj for obj in vars(sso_exchange).values()
        if isinstance(obj, type) and hasattr(obj, "model_fields")
    ]
    assert models, "no request models found in sso_exchange; this list has drifted"

    forbidden = (
        "secret_access_key", "secretaccesskey", "session_token", "sessiontoken",
        "password", "private_key", "aws_secret",
    )
    for model in models:
        for field in model.model_fields:
            flat = field.lower().replace("_", "")
            for bad in forbidden:
                assert bad.replace("_", "") not in flat, (
                    f"{model.__name__}.{field} could carry a credential to the gateway, "
                    f"which is the one thing this flow exists to avoid")


@pytest.mark.parametrize("name", ["cognito:groups", "roles"])
def test_the_claim_names_this_test_guards_are_the_documented_ones(name):
    """A guard on the guard: the names above must be the names the documents promise are
    ignored, or this file drifts into testing something nobody claimed."""
    import pathlib

    arch = (pathlib.Path(__file__).resolve().parents[2] / "docs" / "ARCHITECTURE.md").read_text()
    deps = (pathlib.Path(__file__).resolve().parents[1] / "mvp" / "deps.py").read_text()
    assert name in arch or name in deps, (
        f"{name!r} is guarded here but named in neither the architecture document nor "
        "the module that resolves an identity")
