"""
DynamoDB repositories for MVP (Phase 2 / Phase S).

全テーブル名は環境変数から取得する (CDK 側で SSM Parameter Store にも書かれる)。
Phase 2 で使うテーブル:
- Users: ユーザー基本情報 (Cognito sub と 1:1)
- UserTenants: ユーザー x テナントの紐付け + クレジット残高 (status: active/archived)
- UsageLogs: トークン消費履歴
- Tenants: テナントメタデータ (name, owner, default_credit)
- Permissions: role -> permissions (RBAC 真実源、permissions.json から seed)

Phase S (AWS SSO / STS login) で使うテーブル:
- TrustedAccounts: 信頼する AWS Account ID の allowlist + provisioning policy
- SsoPreRegistrations: invite_only 用の email 事前登録
"""
from .client import get_dynamodb_resource
from .users import UsersRepository
from .user_tenants import UserTenantsRepository, CreditExhaustedError
from .usage_logs import UsageLogsRepository
from .tenants import (
    TenantsRepository,
    TenantNotFoundError,
    TenantLimitExceededError,
    ADMIN_OWNED,
)
from .permissions import PermissionsRepository
from .trusted_accounts import (
    TrustedAccountsRepository,
    TrustedAccountNotFoundError,
)
from .sso_pre_registrations import (
    SsoPreRegistrationsRepository,
    SsoInviteNotFoundError,
    build_iam_user_lookup_key,
)
from .api_keys import (
    ApiKeysRepository,
    ApiKeyLimitExceededError,
    ApiKeyNotFoundError,
    MAX_ACTIVE_KEYS_PER_USER,
    KEY_PREFIX as API_KEY_PREFIX,
    is_api_key,
    hash_key as hash_api_key,
    to_public_dict as api_key_to_public_dict,
)

__all__ = [
    "get_dynamodb_resource",
    "UsersRepository",
    "UserTenantsRepository",
    "CreditExhaustedError",
    "UsageLogsRepository",
    "TenantsRepository",
    "TenantNotFoundError",
    "TenantLimitExceededError",
    "ADMIN_OWNED",
    "PermissionsRepository",
    "TrustedAccountsRepository",
    "TrustedAccountNotFoundError",
    "SsoPreRegistrationsRepository",
    "SsoInviteNotFoundError",
    "build_iam_user_lookup_key",
    "ApiKeysRepository",
    "ApiKeyLimitExceededError",
    "ApiKeyNotFoundError",
    "MAX_ACTIVE_KEYS_PER_USER",
    "API_KEY_PREFIX",
    "is_api_key",
    "hash_api_key",
    "api_key_to_public_dict",
]
