"""SSO identity の Gate 判定 (Phase S、hybrid 対応版).

フロー (design doc §4.4、改訂版):
  Gate 0: identity_type (iam_user / instance_profile) の許可チェック
  Gate 1: role_pattern の allowlist チェック
  Gate 2: 招待 lookup (email or session_name→iam_user_name 形式)
  Gate 3: 招待がなければ provisioning_policy にフォールバック
     - invite_only (default): 拒否
     - auto_provision: session_name から email を抽出して自動 provision

Hybrid 方針:
  - 招待が存在すれば常に招待を優先 (どのポリシーでも)
  - 招待がなければ trusted account の provisioning_policy に従う
  - 同じ account に対して「社員は自動 provision、特定の外部ユーザは email 明示招待」を併用可能

Out: TrustedSsoIdentity (Cognito provisioning に渡す)
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Literal, Optional

from fastapi import HTTPException

from dynamo import (
    SsoPreRegistrationsRepository,
    TrustedAccountsRepository,
)

from .sso_sts import StsIdentity


Role = Literal["admin", "team_lead", "user"]


@dataclass(frozen=True)
class TrustedSsoIdentity:
    """Cognito / Users テーブルへの provisioning に渡すための解決結果."""

    email: str
    account_id: str
    identity_type: str
    target_role: Role
    target_tenant_id: Optional[str]
    target_credit: Optional[int]
    source_arn: str


def validate_sso_identity(sts: StsIdentity) -> TrustedSsoIdentity:
    """Gate 判定を通し、TrustedSsoIdentity を返す. 拒否なら HTTPException."""

    trusted = TrustedAccountsRepository().get(sts.account_id)
    if not trusted:
        raise HTTPException(
            status_code=403,
            detail=(
                f"AWS account {sts.account_id} is not a trusted account. "
                "Ask an administrator to add this account to Trusted Accounts."
            ),
        )

    # Gate 0: identity_type 許可
    if sts.identity_type == "instance_profile":
        if not bool(trusted.get("allow_instance_profile")):
            raise HTTPException(
                status_code=403,
                detail=(
                    "EC2 Instance Profile login is not allowed for this account. "
                    "Use individual IAM Identity Center / SSO credentials."
                ),
            )
    if sts.identity_type == "iam_user":
        if not bool(trusted.get("allow_iam_user")):
            raise HTTPException(
                status_code=403,
                detail="IAM user login is not allowed for this account.",
            )

    # Gate 1: role_pattern 許可 (iam_user には role がないため対象外)
    role_patterns: list[str] = list(trusted.get("allowed_role_patterns") or [])
    if sts.role_name and role_patterns:
        if not any(fnmatch.fnmatch(sts.role_name, p) for p in role_patterns):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Role {sts.role_name} does not match the allowed patterns "
                    f"for account {sts.account_id}."
                ),
            )

    invites_repo = SsoPreRegistrationsRepository()
    policy = str(trusted.get("provisioning_policy") or "invite_only")

    # Gate 2: まず招待 lookup を試す (identity_type 別に)
    invite = _lookup_invite(sts, invites_repo)

    if invite:
        # 招待あり: account 整合性チェック後、招待値で解決
        invite_account = str(invite.get("account_id") or "")
        if invite_account and invite_account != sts.account_id:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Invite was registered under account {invite_account}, "
                    f"but login came from {sts.account_id}."
                ),
            )
        email = str(invite.get("email") or "")
        target_role: Role = _clamp_role(str(invite.get("invited_role") or "user"))
        target_tenant = invite.get("tenant_id") or trusted.get("default_tenant_id")
        target_credit = invite.get("total_credit") or trusted.get("default_credit")
        return TrustedSsoIdentity(
            email=email,
            account_id=sts.account_id,
            identity_type=sts.identity_type,
            target_role=target_role,
            target_tenant_id=str(target_tenant) if target_tenant is not None else None,
            target_credit=int(target_credit) if target_credit is not None else None,
            source_arn=sts.arn,
        )

    # Gate 3: 招待なし → policy で分岐
    # iam_user は常に招待必須 (auto_provision では救済しない)
    if sts.identity_type == "iam_user":
        raise HTTPException(
            status_code=403,
            detail=(
                f"IAM user {sts.iam_user_name} (account {sts.account_id}) is not invited. "
                "IAM user login always requires an explicit Admin invite."
            ),
        )

    if policy == "invite_only":
        session = (sts.session_name or "").strip()
        if "@" in session:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"{session.lower()} is not pre-registered. "
                    "Ask an administrator to invite. (provisioning_policy=invite_only)"
                ),
            )
        # session_name が email でない場合のヒント
        raise HTTPException(
            status_code=403,
            detail=(
                f"AWS session '{session or '(empty)'}' (role={sts.role_name}) is not pre-registered. "
                f"Configure IdP to use email as session_name, "
                f"or add an invite with iam_user_name='{session}' to map this session to an email. "
                "(provisioning_policy=invite_only)"
            ),
        )

    if policy == "auto_provision":
        email = _derive_email_from_session(sts)
        target_tenant = trusted.get("default_tenant_id")
        target_credit = trusted.get("default_credit")
        return TrustedSsoIdentity(
            email=email,
            account_id=sts.account_id,
            identity_type=sts.identity_type,
            target_role="user",  # auto_provision は常に user、昇格は Admin API
            target_tenant_id=str(target_tenant) if target_tenant is not None else None,
            target_credit=int(target_credit) if target_credit is not None else None,
            source_arn=sts.arn,
        )

    raise HTTPException(
        status_code=500,
        detail=f"Unknown provisioning_policy on trusted account: {policy}",
    )


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
def _lookup_invite(
    sts: StsIdentity,
    invites_repo: SsoPreRegistrationsRepository,
) -> Optional[dict]:
    """identity_type 別に招待を探す. 見つからなければ None."""
    if sts.identity_type == "iam_user":
        return invites_repo.find_by_iam_user(sts.account_id, sts.iam_user_name or "")

    session = (sts.session_name or "").strip()
    if "@" in session:
        # 通常ルート: session_name が email の場合
        return invites_repo.get(session.lower())

    if session:
        # フォールバック: Isengard / 社内 SAML 等で session が email でない場合
        return invites_repo.find_by_iam_user(sts.account_id, session)

    return None


def _derive_email_from_session(sts: StsIdentity) -> str:
    """assumed-role の session_name から email を決定. '@' を含まなければ拒否.

    auto_provision で session_name が email でない場合のエラーメッセージ:
    Admin が個別招待で email を map できる hybrid モードの誘導を含める.
    """
    session = (sts.session_name or "").strip()
    if "@" in session:
        return session.lower()
    raise HTTPException(
        status_code=403,
        detail=(
            f"AWS session '{session or '(empty)'}' (role={sts.role_name}) has no email. "
            f"Ask an administrator to add an invite with iam_user_name='{session}' "
            "to map this session to an email."
        ),
    )


def _clamp_role(role: str) -> Role:
    if role in {"admin", "team_lead", "user"}:
        return role  # type: ignore[return-value]
    return "user"
