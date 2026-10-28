"""Bootstrap seed contract — the fork-safe admin provisioning path.

ARCHITECTURE.md and handover notes describe the lifespan seeding:

  - Permissions are loaded idempotently from permissions.json.
  - The default tenant is created once and left alone afterward.
  - When `STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL` is set and no admin user
    exists yet, the seed provisions a Cognito + DynamoDB admin.
  - The seed is a no-op when an admin already exists (idempotent).
"""
from __future__ import annotations

import boto3
import pytest


@pytest.fixture
def seed_env(dynamodb_mock, monkeypatch):
    """Create the extra tables that bootstrap seed needs (permissions and
    users). Tenants / UserTenants are already created by the shared
    conftest fixture.
    """
    # Permissions table
    dynamodb_mock.create_table(
        TableName="stratoclave-permissions",
        KeySchema=[{"AttributeName": "role", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "role", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    # Users table
    dynamodb_mock.create_table(
        TableName="stratoclave-users",
        KeySchema=[
            {"AttributeName": "user_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "email", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "email-index",
                "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    # Cognito user pool for bootstrap_admin to create into.
    cognito = boto3.client("cognito-idp", region_name="us-east-1")
    pool = cognito.create_user_pool(PoolName="stratoclave-test-pool")
    pool_id = pool["UserPool"]["Id"]
    monkeypatch.setenv("COGNITO_USER_POOL_ID", pool_id)
    monkeypatch.setenv("COGNITO_REGION", "us-east-1")
    # Turn on seeding for these tests only.
    monkeypatch.setenv("STRATOCLAVE_DISABLE_SEED", "false")

    yield {"pool_id": pool_id, "cognito": cognito}


def test_seed_permissions_loads_from_json(seed_env):
    from bootstrap.seed import seed_permissions
    from dynamo import PermissionsRepository

    result = seed_permissions()
    # There is at least one role in the committed permissions.json.
    assert result["total"] >= 3
    # And repository reads the roles back.
    assert PermissionsRepository().get("admin") is not None


def test_seed_default_tenant_is_idempotent(seed_env):
    from bootstrap.seed import seed_default_tenant
    from dynamo import TenantsRepository

    first = seed_default_tenant()
    second = seed_default_tenant()
    assert first["tenant_id"] == "default-org"
    assert second["created"] is False
    assert TenantsRepository().get("default-org") is not None


def test_bootstrap_admin_noop_when_env_is_unset(seed_env, monkeypatch):
    monkeypatch.delenv("STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL", raising=False)
    from bootstrap.seed import seed_bootstrap_admin
    from dynamo import UsersRepository

    result = seed_bootstrap_admin()
    assert result["skipped"] is True
    assert UsersRepository().scan_admins() == []


def test_scan_admins_uses_reserved_keyword_alias(seed_env):
    """Regression guard: `roles` is a DynamoDB reserved word and must be
    referenced through an ExpressionAttributeNames alias, not raw.
    """
    from dynamo import UsersRepository

    # The mere call must not raise ValidationException. On an empty table
    # it returns [], which is fine.
    assert UsersRepository().scan_admins() == []


def test_bootstrap_admin_creates_cognito_and_users_row(seed_env, monkeypatch):
    """When enabled, bootstrap must create both the Cognito user and
    the DynamoDB Users row with the admin role.
    """
    monkeypatch.setenv("STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL", "first-admin@example.com")
    from bootstrap.seed import seed_bootstrap_admin, seed_default_tenant
    from dynamo import UsersRepository

    seed_default_tenant()

    result = seed_bootstrap_admin()
    assert result.get("created") is True
    assert result["email"] == "first-admin@example.com"

    # Cognito side.
    cognito = seed_env["cognito"]
    users = cognito.list_users(UserPoolId=seed_env["pool_id"])["Users"]
    emails = {
        attr["Value"]
        for u in users
        for attr in u.get("Attributes", [])
        if attr["Name"] == "email"
    }
    assert "first-admin@example.com" in emails

    # DynamoDB Users side.
    admins = UsersRepository().scan_admins()
    assert any(a.get("email") == "first-admin@example.com" for a in admins)


def test_bootstrap_admin_is_idempotent_when_admin_exists(seed_env, monkeypatch):
    """A second invocation detects the existing admin and skips."""
    monkeypatch.setenv("STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL", "first-admin@example.com")
    from bootstrap.seed import seed_bootstrap_admin, seed_default_tenant

    seed_default_tenant()
    first = seed_bootstrap_admin()
    assert first.get("created") is True

    second = seed_bootstrap_admin()
    assert second.get("skipped") is True


def _put_users(n: int, *, roles: list[str]) -> None:
    from dynamo import UsersRepository

    repo = UsersRepository()
    for i in range(n):
        repo.put_user(
            user_id=f"user-{roles[0]}-{i:03d}",
            email=f"user-{roles[0]}-{i:03d}@example.com",
            auth_provider="cognito",
            auth_provider_user_id=f"user-{roles[0]}-{i:03d}",
            org_id="default-org",
            roles=roles,
        )


class _ScanLimitedTable:
    """A Users table that answers `scan` the way DynamoDB does: `Limit` bounds the
    items READ, and the filter runs afterwards. moto applies the filter first, so it
    cannot show the difference. The pages are what the live table returned for a
    `Limit=1` admin scan: one item read, none matching, and more to come.
    """

    def __init__(self, pages: list[list[dict]]):
        self._pages = pages
        self.calls: list[dict] = []

    def scan(self, **kwargs):
        self.calls.append(kwargs)
        index = int(kwargs.get("ExclusiveStartKey", {}).get("page", 0))
        resp = {"Items": self._pages[index], "Count": len(self._pages[index])}
        if index + 1 < len(self._pages):
            resp["LastEvaluatedKey"] = {"page": index + 1}
        return resp


def test_scan_admins_reads_past_a_page_with_no_admin(seed_env):
    """A `Limit=1` scan reads one row; unless that row is an admin it answered "no
    admin exists" on a table that has several, and every boot re-ran the seed."""
    from dynamo import UsersRepository

    admin = {"user_id": "admin-1", "email": "a@example.com", "roles": ["admin"]}
    repo = UsersRepository()
    repo._table = _ScanLimitedTable([[], [], [admin], [admin]])

    assert repo.scan_admins(limit=1) == [admin]
    assert len(repo._table.calls) == 3


def test_scan_admins_returns_empty_only_after_reading_the_whole_table(seed_env):
    from dynamo import UsersRepository

    repo = UsersRepository()
    repo._table = _ScanLimitedTable([[], [], []])

    assert repo.scan_admins(limit=1) == []
    assert len(repo._table.calls) == 3


def _pool_client(seed_env) -> str:
    return seed_env["cognito"].create_user_pool_client(
        UserPoolId=seed_env["pool_id"],
        ClientName="seed-test",
        ExplicitAuthFlows=["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
    )["UserPoolClient"]["ClientId"]


def _can_log_in(seed_env, client_id: str, email: str, password: str) -> bool:
    from botocore.exceptions import ClientError

    try:
        seed_env["cognito"].admin_initiate_auth(
            UserPoolId=seed_env["pool_id"],
            ClientId=client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )
        return True
    except ClientError:
        return False


def test_bootstrap_admin_does_not_rotate_a_password_on_a_table_with_other_users(
    seed_env, monkeypatch,
):
    """Every boot of a deployment with more than one user re-ran the seed and replaced
    the admin's password, so the password the operator set stopped working."""
    monkeypatch.setenv("STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL", "first-admin@example.com")
    from bootstrap.seed import seed_bootstrap_admin, seed_default_tenant

    seed_default_tenant()
    assert seed_bootstrap_admin().get("created") is True
    _put_users(40, roles=["user"])

    client_id = _pool_client(seed_env)
    seed_env["cognito"].admin_set_user_password(
        UserPoolId=seed_env["pool_id"], Username="first-admin@example.com",
        Password="Operator-Chosen-1!", Permanent=True,
    )

    assert seed_bootstrap_admin().get("skipped") is True
    assert _can_log_in(seed_env, client_id, "first-admin@example.com", "Operator-Chosen-1!")


def test_bootstrap_admin_adopting_an_existing_cognito_user_keeps_its_password(
    seed_env, monkeypatch,
):
    """The seed adopts a Cognito user it did not create (UsernameExistsException) and
    reconciles only the Users side. Several workers boot at once: one creates the user
    and stashes its password, and the others adopt it. An adopter that also set a
    password would leave the stashed one wrong, and would overwrite whatever the
    operator had set.
    """
    monkeypatch.setenv("STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL", "first-admin@example.com")
    from bootstrap.seed import seed_bootstrap_admin, seed_default_tenant
    from dynamo import UsersRepository

    seed_default_tenant()
    cognito = seed_env["cognito"]
    cognito.admin_create_user(
        UserPoolId=seed_env["pool_id"], Username="first-admin@example.com",
        UserAttributes=[{"Name": "email", "Value": "first-admin@example.com"}],
        MessageAction="SUPPRESS",
    )
    cognito.admin_set_user_password(
        UserPoolId=seed_env["pool_id"], Username="first-admin@example.com",
        Password="Existing-Password-1!", Permanent=True,
    )
    client_id = _pool_client(seed_env)

    result = seed_bootstrap_admin()
    assert result.get("created") is True
    assert result["created_new"] is False
    assert any(a.get("email") == "first-admin@example.com"
               for a in UsersRepository().scan_admins())
    assert _can_log_in(seed_env, client_id, "first-admin@example.com", "Existing-Password-1!")
