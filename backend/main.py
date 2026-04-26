"""
Stratoclave Backend (MVP)

MVP スコープ: Bedrock プロキシゲートウェイ (Anthropic Messages API 互換)
              + Cognito 認証 + DynamoDB クレジット管理

既存の複雑な ACP / Session / STS 認証系は MVP では無効化し、
backend/mvp/ 配下の最小構成のみで起動する。
Phase 2 以降で必要に応じて既存ルーターを段階的に復活させる。
"""
import os

# .env 読込 (ローカル開発向け、本番は環境変数経由)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ロギングを最初にセットアップ
from core.logging import setup_logging

environment = os.getenv("ENVIRONMENT", "production")
setup_logging(environment=environment)

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.logging import get_logger
from middleware.correlation import CorrelationIDMiddleware

# MVP / Phase 2 ルーター
from mvp.anthropic import router as mvp_anthropic_router
from mvp.me import router as mvp_me_router
from mvp.admin_users import router as mvp_admin_users_router
from mvp.admin_tenants import router as mvp_admin_tenants_router
from mvp.admin_usage import router as mvp_admin_usage_router
from mvp.team_lead import router as mvp_team_lead_router
from mvp.cognito_auth import router as mvp_cognito_auth_router
# Phase S: AWS SSO / STS ログイン
from mvp.sso_exchange import router as mvp_sso_exchange_router
from mvp.admin_trusted_accounts import router as mvp_admin_trusted_accounts_router
from mvp.admin_sso_invites import router as mvp_admin_sso_invites_router
# Phase C: 長期 API Key (cowork 等の gateway クライアント用)
from mvp.me_api_keys import router as mvp_me_api_keys_router
from mvp.admin_api_keys import router as mvp_admin_api_keys_router


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Startup 環境変数チェック
# ---------------------------------------------------------------------------

_REQUIRED_IN_PRODUCTION = [
    "COGNITO_USER_POOL_ID",
    "COGNITO_CLIENT_ID",
    "OIDC_ISSUER_URL",
    "OIDC_AUDIENCE",
    "DYNAMODB_USERS_TABLE",
    "DYNAMODB_USER_TENANTS_TABLE",
    "DYNAMODB_USAGE_LOGS_TABLE",
]

if environment == "production":
    missing = [v for v in _REQUIRED_IN_PRODUCTION if not os.getenv(v)]
    if missing:
        raise EnvironmentError(
            f"Missing required environment variables in production: {', '.join(missing)}"
        )

    cors_origins = os.getenv("CORS_ORIGINS", "")
    if not cors_origins or "localhost" in cors_origins:
        raise EnvironmentError(
            "CORS_ORIGINS must be explicitly set (must not contain localhost) in production"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("application_starting", environment=environment)

    # Phase 2 (v2.1) 運用ガード: production で ALLOW_ADMIN_CREATION=true は警告
    allow_admin_creation = os.getenv("ALLOW_ADMIN_CREATION", "false").lower() == "true"
    if environment == "production" and allow_admin_creation:
        logger.warning(
            "allow_admin_creation_enabled_in_production",
            event="allow_admin_creation_warning",
            environment=environment,
        )

    yield
    logger.info("application_shutdown")


app = FastAPI(
    title="Stratoclave",
    description="Bedrock proxy gateway with tenant-level RBAC (Phase 2)",
    version="2.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# セキュリティヘッダー
# ---------------------------------------------------------------------------

from starlette.middleware.base import BaseHTTPMiddleware


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'none'; "
            "style-src 'self'; "
            "connect-src 'self'; "
            "img-src 'self' data:; "
            "font-src 'self' data:; "
            "object-src 'none'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CorrelationIDMiddleware)

# CORS (Starlette は LIFO なので CORS を最後に add = 最初に実行)
from core.constants import DEFAULT_CORS_ORIGINS

cors_origins = os.getenv("CORS_ORIGINS", DEFAULT_CORS_ORIGINS).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in cors_origins if o.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Correlation-ID",
        # X-Tenant-ID は Phase 2 (v2.1) で撤去: Backend が JWT + Users.org_id から
        # tenant_id を解決するため、Frontend / CLI からのヘッダ指定経路は閉じる
        "anthropic-version",
        "anthropic-beta",
        "x-api-key",
    ],
)

# ---------------------------------------------------------------------------
# ルーター登録
# ---------------------------------------------------------------------------

# MVP / Phase 2 ルーター
app.include_router(mvp_anthropic_router)         # POST /v1/messages
app.include_router(mvp_me_router)                # GET  /api/mvp/me + usage-summary / usage-history
app.include_router(mvp_admin_users_router)       # /api/mvp/admin/users[*]
app.include_router(mvp_admin_tenants_router)     # /api/mvp/admin/tenants[*]
app.include_router(mvp_admin_usage_router)       # /api/mvp/admin/usage-logs
app.include_router(mvp_team_lead_router)         # /api/mvp/team-lead/tenants[*]
app.include_router(mvp_cognito_auth_router)      # /api/mvp/auth/login, /respond
# Phase S
app.include_router(mvp_sso_exchange_router)              # POST /api/mvp/auth/sso-exchange
app.include_router(mvp_admin_trusted_accounts_router)    # /api/mvp/admin/trusted-accounts[*]
app.include_router(mvp_admin_sso_invites_router)         # /api/mvp/admin/sso-invites[*]
# Phase C
app.include_router(mvp_me_api_keys_router)               # /api/mvp/me/api-keys[*]
app.include_router(mvp_admin_api_keys_router)            # /api/mvp/admin/api-keys[*] + /api/mvp/admin/users/{id}/api-keys

# Frontend 旧実装との互換用: /api/users/me/credit を /api/mvp/me にエイリアス
# (Frontend が /api/users/me/credit を叩いている)
from fastapi import Depends
from mvp.deps import AuthenticatedUser, get_current_user
from dynamo import UserTenantsRepository


_DEPRECATION_HEADERS = {
    "Deprecation": "true",
    "Sunset": "Frontend migrates to /api/mvp/me in Phase 2",
    "Link": '</api/mvp/me>; rel="successor-version"',
}


@app.get("/api/users/me/credit", tags=["compat"], deprecated=True)
def legacy_credit(user: AuthenticatedUser = Depends(get_current_user)):
    """Phase 2 完了時に削除予定。Frontend は /api/mvp/me に移行すること."""
    from fastapi.responses import JSONResponse
    repo = UserTenantsRepository()
    repo.ensure(user_id=user.user_id, tenant_id=user.org_id)
    summary = repo.credit_summary(user.user_id, user.org_id)
    body = {**summary, "currency": "tokens"}
    return JSONResponse(content=body, headers=_DEPRECATION_HEADERS)


@app.get("/api/users/me", tags=["compat"], deprecated=True)
def legacy_me(user: AuthenticatedUser = Depends(get_current_user)):
    """Phase 2 完了時に削除予定。Frontend は /api/mvp/me に移行すること."""
    from fastapi.responses import JSONResponse
    body = {
        "email": user.email,
        "sub": user.user_id,
        "org_id": user.org_id,
        "roles": user.roles,
    }
    return JSONResponse(content=body, headers=_DEPRECATION_HEADERS)


@app.get("/health", tags=["system"])
async def health_check():
    return {"status": "healthy"}


@app.get("/", tags=["system"])
async def root():
    return {
        "name": "Stratoclave",
        "version": app.version,
        "description": "Bedrock proxy gateway with tenant-level RBAC",
        "docs": "/docs",
        "endpoints": {
            "anthropic_messages": "/v1/messages",
            "me": "/api/mvp/me",
            "admin_user_create": "/api/mvp/admin/users",
            "cli_login": "/api/mvp/auth/login",
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
