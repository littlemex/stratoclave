"""MVP 用の FastAPI 依存関係 (Phase 2 v2.1 + Phase C).

Bearer トークンを受け取り、以下 2 種類を判別する:
  - 値が `sk-stratoclave-*` で始まる → 長期 API Key (Phase C)
  - それ以外 → Cognito access_token (Phase 2)

変更点 (v1 → v2.1):
- `cognito:groups` / `roles` claim を **完全に無視**
- `token_use == "access"` を必須検証 (id_token は 401)
- roles / email / org_id は DynamoDB Users テーブルから取得 (RBAC 真実源)
- email が空 (access_token に email claim なし) の場合は Users テーブルから補填、
  それでも無ければ `cognito-idp:AdminGetUser` で取得して Users に put (冪等)

変更点 (v2.1 → Phase C):
- `AuthenticatedUser` に `auth_kind` と `key_scopes` を追加
- `sk-stratoclave-*` を prefix に持つトークンは SHA-256 でハッシュ化し
  ApiKeys テーブルで lookup. expires_at / revoked_at を検証、有効なら
  key owner の roles と scopes を組み合わせて AuthenticatedUser を構築
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Literal, Optional

import boto3
import jwt as pyjwt
from botocore.exceptions import ClientError
from fastapi import Depends, Header, HTTPException, status
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError

from dynamo import (
    ApiKeysRepository,
    UsersRepository,
    hash_api_key,
    is_api_key,
)


DEFAULT_ORG_ID = os.getenv("DEFAULT_ORG_ID", "default-org")

_log = logging.getLogger(__name__)


AuthKind = Literal["jwt", "api_key"]


@dataclass(frozen=True)
class AuthenticatedUser:
    """認証済みユーザー情報 (DynamoDB Users 由来)."""

    user_id: str          # Cognito sub
    email: str
    org_id: str
    roles: list[str]      # DynamoDB Users.roles、Cognito Group は参照しない
    raw_claims: dict[str, Any] = field(default_factory=dict)
    # Phase C 追加: 認証種別と API Key scopes
    auth_kind: AuthKind = "jwt"
    key_scopes: Optional[list[str]] = None
    api_key_hash: Optional[str] = None

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    @property
    def is_team_lead(self) -> bool:
        return "team_lead" in self.roles


def is_user_deleted(user_record: Optional[dict[str, Any]]) -> bool:
    """Predicate for X-1: has this user been soft-deleted?

    Returning ``True`` means the auth layer must reject any token
    for this user with 401 *and must not* attempt to backfill a row
    — otherwise the physically-gone user is resurrected as a
    fresh ``user`` under ``default-org``. Biased towards "not
    deleted" on any unexpected shape so a corrupt row cannot lock
    out every user.
    """
    if user_record is None:
        return False
    status = user_record.get("status")
    return isinstance(status, str) and status == "deleted"


def is_token_revoked(
    *,
    user_record: Optional[dict[str, Any]],
    token_iat: Optional[int],
) -> bool:
    """Predicate for C-C: is this access_token older than the last
    forced-logout watermark written by
    :py:meth:`UsersRepository.revoke_all_sessions`?

    The function is intentionally simple and biases towards "not
    revoked" on any unexpected shape (missing record, missing / malformed
    watermark, missing iat). The strict path lives in the route layer
    via ``get_current_user``: we want auth to keep working for users
    with no Users row yet (backfill flow), and a corrupt watermark
    must not turn every request into a 401.
    """
    if user_record is None:
        return False
    raw = user_record.get("token_revoked_after")
    if raw is None:
        return False
    try:
        watermark = int(raw)
    except (TypeError, ValueError):
        # Structural inconsistency: log and fall open.
        _log.warning("token_revoked_after_malformed", extra={"value": repr(raw)})
        return False
    if token_iat is None:
        # Defensive: Cognito always sets iat, but we do not want to
        # invent 0 and wipe every session on a spec change.
        return False
    return int(token_iat) < watermark


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    issuer = os.getenv("OIDC_ISSUER_URL")
    if not issuer:
        raise HTTPException(
            status_code=500,
            detail="OIDC_ISSUER_URL is not configured on the server",
        )
    return PyJWKClient(issuer.rstrip("/") + "/.well-known/jwks.json", cache_keys=True)


def _decode_cognito_access_token(token: str) -> dict[str, Any]:
    """Cognito access_token のみを受け入れる (id_token は拒否).

    v2.1 §3.8, Security H1 対応:
    - token_use="access" を必須化
    - access_token は aud claim を持たないため、client_id claim で検証
    """
    issuer = os.getenv("OIDC_ISSUER_URL")
    client_id = os.getenv("OIDC_AUDIENCE") or os.getenv("COGNITO_CLIENT_ID")
    if not issuer or not client_id:
        raise HTTPException(
            status_code=500,
            detail="OIDC_ISSUER_URL / OIDC_AUDIENCE must be configured",
        )

    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token).key
    except PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid JWT: {e}")

    try:
        claims = pyjwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=issuer,
            options={"verify_aud": False},  # access_token は aud を持たないため手動検証
        )
    except PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"JWT verification failed: {e}")

    # token_use claim は access / id / refresh のいずれか (Cognito 仕様)
    token_use = claims.get("token_use")
    if token_use != "access":
        raise HTTPException(
            status_code=401,
            detail=f"Only access_token is accepted (got token_use={token_use!r})",
        )

    token_client_id = claims.get("client_id")
    if token_client_id != client_id:
        raise HTTPException(
            status_code=401,
            detail="JWT client_id does not match expected audience",
        )

    return claims


_cognito_idp_client: Optional[Any] = None


def _get_cognito_idp():
    global _cognito_idp_client
    if _cognito_idp_client is None:
        region = os.getenv("COGNITO_REGION") or os.getenv("AWS_REGION", "us-east-1")
        _cognito_idp_client = boto3.client("cognito-idp", region_name=region)
    return _cognito_idp_client


def _fetch_email_from_cognito(sub: str) -> Optional[str]:
    """access_token 由来の sub から email を引く (Users テーブルに無い場合の最終手段)."""
    pool_id = os.getenv("COGNITO_USER_POOL_ID")
    if not pool_id:
        return None
    try:
        resp = _get_cognito_idp().admin_get_user(UserPoolId=pool_id, Username=sub)
    except ClientError as e:
        _log.warning(
            "cognito_admin_get_user_failed",
            extra={"sub": sub, "error": e.response.get("Error", {}).get("Code")},
        )
        return None
    for attr in resp.get("UserAttributes", []):
        if attr.get("Name") == "email":
            return attr.get("Value")
    return None


# ---------------------------------------------------------------
# API Key path (Phase C)
# ---------------------------------------------------------------
def _authenticate_api_key(plain_key: str) -> AuthenticatedUser:
    """`sk-stratoclave-*` プレーンキーを検証し、対応する owner の AuthenticatedUser を返す.

    検証項目:
      - DB に key_hash が存在
      - revoked_at が None
      - expires_at が未設定、または未来
      - owner の Users が存在
    成功時は last_used_at を best-effort 更新.
    """
    repo = ApiKeysRepository()
    key_hash = hash_api_key(plain_key)
    item = repo.get_by_hash(key_hash)
    if not item:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if item.get("revoked_at"):
        raise HTTPException(status_code=401, detail="API key has been revoked")
    expires_at = item.get("expires_at")
    if expires_at:
        now_iso = datetime.now(timezone.utc).isoformat()
        if expires_at <= now_iso:
            raise HTTPException(status_code=401, detail="API key has expired")

    owner_id = str(item.get("user_id") or "")
    scopes_raw = item.get("scopes") or []
    scopes = [str(s) for s in scopes_raw] if isinstance(scopes_raw, list) else []
    if not owner_id:
        raise HTTPException(status_code=401, detail="API key missing owner")

    users_repo = UsersRepository()
    user_rec = users_repo.get_by_user_id(owner_id)
    if not user_rec:
        raise HTTPException(status_code=401, detail="API key owner no longer exists")

    # Z-1 (2026-04 third blind review): the tombstone + watermark
    # checks used to live only on the Cognito JWT path, so a
    # long-lived sk-stratoclave-* key outlived a ``mark_deleted`` on
    # its owner. A deleted admin's API key kept calling /v1/messages
    # for up to the key's own expires_at (often no expiry at all).
    # We now apply the same two checks here before trusting the key:
    #
    #   (a) is_user_deleted     → owner has a tombstone row → 401
    #   (b) key created before   → owner was force-signed-out after
    #       token_revoked_after    the key was minted → 401. This
    #                              catches tenant switch / demote
    #                              resets that should have invalidated
    #                              all credentials belonging to the
    #                              user, not just Cognito bearers.
    if is_user_deleted(user_rec):
        raise HTTPException(
            status_code=401, detail="API key owner has been deleted"
        )
    wm_raw = user_rec.get("token_revoked_after")
    created_raw = item.get("created_at")
    if wm_raw is not None and created_raw:
        try:
            watermark = int(wm_raw)
        except (TypeError, ValueError):
            watermark = None
        if watermark is not None:
            try:
                created_epoch = int(
                    datetime.fromisoformat(str(created_raw).replace("Z", "+00:00")).timestamp()
                )
            except (TypeError, ValueError):
                created_epoch = None
            if created_epoch is not None and created_epoch < watermark:
                raise HTTPException(
                    status_code=401,
                    detail=(
                        "API key predates a forced session revocation on its"
                        " owner — mint a new key after re-authenticating."
                    ),
                )

    email = str(user_rec.get("email") or "")
    org_id = str(user_rec.get("org_id") or DEFAULT_ORG_ID)
    roles_raw = user_rec.get("roles") or []
    roles: list[str] = (
        [roles_raw] if isinstance(roles_raw, str) else [str(r) for r in roles_raw]
    )

    # last_used_at を更新 (失敗は黙殺)
    repo.touch_last_used(key_hash)

    return AuthenticatedUser(
        user_id=owner_id,
        email=email,
        org_id=org_id,
        roles=roles,
        raw_claims={},
        auth_kind="api_key",
        key_scopes=scopes,
        api_key_hash=key_hash,
    )


# ---------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------
def get_current_user(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
) -> AuthenticatedUser:
    """`Authorization: Bearer <token>` または `x-api-key: <token>` を受け付ける.

    prefix が `sk-stratoclave-` ならば長期 API Key、そうでなければ Cognito access_token.
    """
    token: Optional[str] = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(None, 1)[1].strip()
    elif x_api_key:
        token = x_api_key.strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Phase C: API Key path
    if is_api_key(token):
        return _authenticate_api_key(token)

    # Phase 2: Cognito access_token path
    claims = _decode_cognito_access_token(token)
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="JWT has no 'sub' claim")

    # ---- DynamoDB Users を真実源として roles / email / org_id を解決 ----
    users_repo = UsersRepository()
    user_record = users_repo.get_by_user_id(sub)

    email: str = ""
    roles: list[str] = []
    org_id: str = DEFAULT_ORG_ID
    needs_backfill = False

    # X-1 (2026-04 critical-sweep follow-up): soft-delete tombstone.
    # If the row is marked deleted we refuse the token immediately.
    # Doing this BEFORE the backfill path is essential — otherwise
    # deps.py would happily rebuild the row as a fresh ``user`` and
    # resurrect the victim for up to one access_token lifetime.
    if is_user_deleted(user_record):
        raise HTTPException(
            status_code=401,
            detail="User has been deleted — authentication refused",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user_record:
        email = str(user_record.get("email") or "")
        roles_raw = user_record.get("roles") or []
        if isinstance(roles_raw, str):
            roles = [roles_raw]
        else:
            roles = [str(r) for r in roles_raw]
        org_id = str(user_record.get("org_id") or DEFAULT_ORG_ID)
    else:
        needs_backfill = True

    # C-C (2026-04 critical sweep): DB-owned session revocation.
    # Cognito's AdminUserGlobalSignOut only kills refresh tokens; the
    # already-issued access_token is live until exp. Whenever an admin
    # reassigns tenants / demotes / deletes a user, we stamp
    # ``token_revoked_after = now()`` on the Users row. Here we refuse
    # any JWT whose ``iat`` is earlier than that watermark so the
    # stale tab cannot keep acting with the new org_id / roles.
    try:
        token_iat_claim = claims.get("iat")
        token_iat = int(token_iat_claim) if token_iat_claim is not None else None
    except (TypeError, ValueError):
        token_iat = None
    if is_token_revoked(user_record=user_record, token_iat=token_iat):
        raise HTTPException(
            status_code=401,
            detail="Session has been revoked — please sign in again",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # email 空 (access_token に email claim が無いケースの #1 fix):
    # 1. Users テーブルにあれば使う (上で済み)
    # 2. Cognito AdminGetUser で email を引いて Users に backfill (冪等)
    if not email:
        fetched = _fetch_email_from_cognito(sub)
        if fetched:
            email = fetched
            needs_backfill = True

    # roles が空 = 未登録。`user` ロールで backfill する (admin への昇格は API 経由のみ)
    if not roles:
        roles = ["user"]
        needs_backfill = True

    if needs_backfill:
        try:
            users_repo.put_user(
                user_id=sub,
                email=email,
                auth_provider="cognito",
                auth_provider_user_id=sub,
                org_id=org_id,
                roles=roles,
            )
        except Exception as e:
            # 書き込み失敗しても認証結果は返す (次回 me.py で再度補填される)
            _log.warning("users_backfill_failed", extra={"sub": sub, "error": str(e)})

    return AuthenticatedUser(
        user_id=sub,
        email=email,
        org_id=org_id,
        roles=roles,
        raw_claims=claims,
        auth_kind="jwt",
        key_scopes=None,
        api_key_hash=None,
    )
