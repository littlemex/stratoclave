<!-- Last updated: 2026-04-27 -->
<!-- Applies to: Stratoclave main @ 48b9533 (or later) -->

# アーキテクチャ

Stratoclave は **Amazon Bedrock の前段に置く薄いプロキシゲートウェイ**で、
マルチテナント性、ロールベースアクセス制御、クレジット割り当て、統一された
ログイン面 (Amazon Cognito または AWS SSO) を、AWS 以外の依存関係を導入すること
なく追加する。デプロイメントは単一の AWS アカウントとリージョン内で完結し、
運用者が本ドキュメントだけでシステム全体を理解できるよう、意図的に少数の
可動部品で構成されている。

本ドキュメントでは、コンポーネント、データモデル、認証・認可のフロー、
Stratoclave が依拠する不変条件を説明する。対象は運用者、コードを初めて読む
コントリビューター、セキュリティレビュアーである。段階的なセットアップガイドが
必要なら [GETTING_STARTED.md](GETTING_STARTED.md) を、デプロイメントと day-2
運用については [DEPLOYMENT.md](DEPLOYMENT.md) と [ADMIN_GUIDE.md](ADMIN_GUIDE.md)
を参照すること。リポジトリは [`https://github.com/littlemex/stratoclave`](https://github.com/littlemex/stratoclave)
にある。

<!-- TODO(docs): Insert architecture diagram (hero image) here -->

## 目次

- [設計原則](#設計原則)
- [システム図](#システム図)
- [コンポーネント](#コンポーネント)
- [認証フロー](#認証フロー)
- [認可 (RBAC)](#認可-rbac)
- [クレジットモデル](#クレジットモデル)
- [監査ログ](#監査ログ)
- [well-known 設定](#well-known-設定)
- [データモデル](#データモデル)
- [スケーリングに関する考慮](#スケーリングに関する考慮)
- [セキュリティに関する考慮](#セキュリティに関する考慮)
- [拡張ポイント](#拡張ポイント)

---

## 設計原則

1. **単一リージョン、単一アカウント、SaaS なし**。Stratoclave は自分の
   アカウント内の単一 AWS リージョンにデプロイする。外部の制御プレーン、
   ホスト型メタデータサービス、サードパーティ依存は一切なく、
   単一障害点やデータリークの原因となり得るものを持たない。
2. **ステートレスなバックエンド**。FastAPI コンテナは短寿命のインメモリ
   キャッシュ以上のユーザー単位状態を持たない。すべての可変状態は
   DynamoDB にあり、条件付き書き込みで更新される。単一の Fargate タスクで
   正しさは十分担保される。複数タスクは調整なしで水平スケールする。
3. **永続化は DynamoDB のみ**。RDS なし、Redis なし、外部キューなし。
   クレジット計算は条件付き `UpdateItem` を使う。シーディングは冪等
   (単発挿入には `attribute_not_exists`、設定ライクなテーブルにはバージョン比較)。
4. **Cognito はトークン発行のためのもの、DynamoDB が真実の源**。
   Stratoclave は `cognito:groups` を*決して*読まないし、認可を Cognito に
   依存しない。`Users` テーブルがユーザーのロールとテナントメンバーシップの
   権威あるレコードである。Cognito は狭いトークンベンダーとして扱われる。
5. **パスワードレスログインに STS による vouch を使う**。SSO フローは
   HashiCorp Vault の AWS auth backend と同じパターンを使う。CLI が
   `sts:GetCallerIdentity` リクエストに署名し、バックエンドがそれを STS に
   転送し、バックエンドは STS の返答だけを信頼する。クレデンシャルは
   Stratoclave に送信されない。
6. **タスクロールには最小権限**。ECS タスクロールは、バックエンドが必要と
   する特定の DynamoDB テーブル、Cognito 管理アクション、Bedrock 推論
   プロファイルだけにスコープされている。`iam:*`, `ec2:*`, S3 アクセスは
   持たない。

---

## システム図

```
   Clients                          CloudFront (TLS termination, WAF-ready)
  ┌───────────┐                     ┌─────────────────────────────┐
  │ Web UI    │───── HTTPS ───────▶ │  /              → S3 (SPA)  │
  │ CLI       │                     │  /config.json   → S3        │
  │ SDKs      │                     │  /api/*         → ALB       │
  │ Cowork    │                     │  /v1/*          → ALB       │
  └───────────┘                     │  /.well-known/* → ALB       │
                                    └──────────┬──────────────────┘
                                               │
                     ┌─────────────────────────┴─────────────────────────┐
                     │                                                   │
                     ▼                                                   ▼
           ┌──────────────────┐                              ┌──────────────────────┐
           │  S3 (static SPA) │                              │  ALB (HTTP)          │
           │  Vite build      │                              └──────────┬───────────┘
           └──────────────────┘                                         │
                                                                        ▼
                                                         ┌──────────────────────────┐
                                                         │  ECS Fargate (public)    │
                                                         │  FastAPI backend         │
                                                         │   /api/mvp/*   RBAC      │
                                                         │   /v1/messages Bedrock   │
                                                         │   /v1/models             │
                                                         │   /.well-known/...       │
                                                         │  Audit logger (JSON)     │
                                                         └─────┬───────────┬────────┘
                                                               │           │
                                               ┌───────────────┘           └──────────────┐
                                               ▼                                          ▼
                                 ┌──────────────────────────┐                ┌───────────────────────┐
                                 │  DynamoDB                │                │  Amazon Bedrock       │
                                 │   Users / Tenants        │                │   converse            │
                                 │   UserTenants (credit)   │                │   converseStream      │
                                 │   Permissions            │                │   inference profiles  │
                                 │   UsageLogs (TTL)        │                └───────────────────────┘
                                 │   ApiKeys                │
                                 │   TrustedAccounts        │
                                 │   SsoPreRegistrations    │
                                 └──────────────────────────┘

                                 ┌──────────────────────────┐
                                 │  Amazon Cognito          │
                                 │   User Pool              │
                                 │   Hosted UI + App Client │
                                 │   access_token issuer    │
                                 └──────────────────────────┘
```

AWS SSO パスは、クラスタ外への 1 ホップを追加する。CLI が公開 STS
エンドポイントへの呼び出しに署名し、バックエンドがその署名済み呼び出しを
STS へ中継して、正準な `<Arn>/<UserId>/<Account>` タプルを受け取る。
クレデンシャルはバックエンドの境界を決して越えない。

```
  CLI     aws-sdk creds chain      Backend                 STS (global)         Cognito
   │                                  │                      │                    │
   │  sigv4-sign GetCallerIdentity    │                      │                    │
   │─────────────────────────────────▶│                      │                    │
   │  POST /api/mvp/auth/sso-exchange │                      │                    │
   │                                  │  forward signed req  │                    │
   │                                  │─────────────────────▶│                    │
   │                                  │  <Arn><UserId><Acc>  │                    │
   │                                  │◀─────────────────────│                    │
   │                                  │  Gate: trusted acct  │                    │
   │                                  │  Gate: role pattern  │                    │
   │                                  │  Gate: identity type │                    │
   │                                  │  Gate: invite policy │                    │
   │                                  │  admin_create_user / │                    │
   │                                  │  set_password /      │                    │
   │                                  │  admin_initiate_auth │                    │
   │                                  │─────────────────────────────────────────▶ │
   │                                  │  access_token                             │
   │                                  │◀───────────────────────────────────────── │
   │  access_token + user profile     │                      │                    │
   │◀─────────────────────────────────│                      │                    │
```

---

## コンポーネント

### バックエンド — FastAPI (Python)

**パス**: [`backend/`](../backend)

バックエンドは単一の FastAPI アプリケーションで、コンテナイメージとして
パッケージされ、内部向け ALB の背後の ECS Fargate 上で動作する。意図的に
小さい。`backend/mvp/` 以下のほとんどのファイルは、`backend/dynamo/`
リポジトリに対する薄い HTTP アダプタである。

```
backend/
├── main.py                    # App factory, router wiring, lifespan seed
├── permissions.json           # Seed source of truth for Permissions table
├── bootstrap/
│   └── seed.py                # Idempotent Permissions + default tenant seed
├── core/
│   └── logging.py             # structlog + python-json-logger
├── middleware/
│   └── correlation.py         # X-Correlation-ID propagation
├── dynamo/                    # Repository layer (one module per table)
│   ├── users.py
│   ├── user_tenants.py        # Credit accounting with optimistic locking
│   ├── usage_logs.py
│   ├── tenants.py
│   ├── permissions.py
│   ├── api_keys.py            # SHA-256 stored, plaintext never persisted
│   ├── trusted_accounts.py
│   └── sso_pre_registrations.py
└── mvp/                       # FastAPI routers + auth/authz helpers
    ├── deps.py                # JWT verification + API key path
    ├── authz.py               # has_permission, require_permission, audit log
    ├── anthropic.py           # POST /v1/messages, GET /v1/models
    ├── me.py                  # /api/mvp/me + usage summaries
    ├── admin_users.py
    ├── admin_tenants.py
    ├── admin_usage.py
    ├── admin_api_keys.py
    ├── admin_trusted_accounts.py
    ├── admin_sso_invites.py
    ├── team_lead.py
    ├── cognito_auth.py        # Email + password login
    ├── sso_sts.py             # Presigned URL validation + STS round-trip
    ├── sso_gate.py            # Identity-type classifier + allowlist gates
    ├── sso_exchange.py        # POST /api/mvp/auth/sso-exchange
    ├── me_api_keys.py         # Self-serve long-lived API key management
    └── well_known.py          # GET /.well-known/stratoclave-config
```

**責務**:

- 入力クレデンシャル (Cognito `access_token`, 長寿命 API キー, または
  署名済み STS リクエスト) を検証する。
- 各認証済みリクエストを `AuthenticatedUser` に解決する。ロールは DynamoDB
  から取り、Cognito グループからは取らない。
- `require_permission(...)` FastAPI 依存関係を通じて RBAC を強制する。
- Anthropic `Messages API` リクエストを Bedrock `converse` /
  `converseStream` 呼び出しに変換し、結果をストリームで返し、トークン
  使用量を計上する。
- クレジットをアトミックに減算し、特権操作の監査ログを送出する。
- 起動時に `Permissions` テーブルとデフォルトテナントをシードする。

**主な依存**: `boto3` (DynamoDB, Bedrock, Cognito Identity Provider),
Cognito JWKS 検証のための `PyJWT` と `PyJWKClient`, STS vouch ラウンド
トリップのための `httpx`, 構造化ログのための `structlog` +
`python-json-logger`。

**なぜステートレスか**: すべてのユーザー単位データは DynamoDB にある。
パーミッションはホットパスを吸収するためにプロセス内で 10 秒間キャッシュ
される。JWKS キーは `PyJWKClient` によってキャッシュされる。ローリング
デプロイは、インフライトの HTTP リクエスト以上をドレインせずにタスクを
置換できる。

### フロントエンド — Vite + React (TypeScript)

**パス**: [`frontend/`](../frontend)

CloudFront を通じて S3 から配信されるシングルページアプリケーションである。
SPA は静的ビルドで、すべての設定はランタイムに `/config.json`
(Cognito Hosted UI URL に現れる同じ 4 つの Cognito 値、加えて CloudFront
ドメイン) から取得される。バンドルにシークレットは焼き込まれない。

```
frontend/src/
├── main.tsx                   # Entry + QueryClientProvider + ErrorProvider
├── App.tsx                    # Routing, AuthProvider gate
├── contexts/AuthContext.tsx   # access_token lifecycle (3 ingress paths)
├── lib/
│   ├── api.ts                 # Typed client + TanStack Query
│   ├── authFetch.ts           # Bearer injection + 401 soft-logout
│   ├── cognito.ts             # PKCE + refresh_token rotation
│   └── runtimeConfig.ts       # Fetches /config.json at startup
├── pages/                     # Login, Callback, Dashboard, Me, Admin, Team Lead
└── components/
    ├── layout/AppShell.tsx
    └── ui/                    # Primitives (shadcn/ui-style)
```

**AuthContext** は `access_token` を 3 つの入口経路で受け入れる:
(1) `stratoclave ui open` 時に CLI が書く `?token=` URL パラメータ、
(2) PKCE を伴う Cognito Hosted UI のコールバック、
(3) `localStorage` に保存済みのトークン。バックエンドは `token_use=access`
を強制するので、フロントエンドは決して `id_token` を渡さない。

Vite の開発サーバーは `/api/*`, `/v1/*`, `/.well-known/*` を本番と同じ
ALB にプロキシするので、ローカルと本番の挙動は CloudFront キャッシュ層
でしか差が出ない。

### CLI — Rust (`stratoclave`)

**パス**: [`cli/`](../cli)

単一の静的バイナリである。`~/.stratoclave/config.toml` (サーバー URL と
デフォルト値) と `~/.stratoclave/mvp_tokens.json` (トークン、モード `0600`)
以外は状態を持たない。

```
cli/src/
├── main.rs                    # clap derive dispatch
├── mvp/
│   ├── auth.rs                # Cognito password login + NEW_PASSWORD_REQUIRED
│   ├── sso.rs                 # aws-sdk-sts presign → backend
│   ├── claude_cmd.rs          # Wraps `claude` with ANTHROPIC_BASE_URL injected
│   ├── api.rs                 # reqwest client, error rendering
│   ├── tokens.rs              # ~/.stratoclave/mvp_tokens.json (0600)
│   ├── config.rs              # STRATOCLAVE_* env + config.toml
│   ├── admin.rs / team_lead.rs / usage.rs
│   └── ...
└── commands/ui.rs             # stratoclave ui open (opens browser)
```

CLI はブラウザでは快適にできない 2 つの仕事を吸収するために存在する:
AWS SDK API (`sts:GetCallerIdentity`) の呼び出しと、`ANTHROPIC_BASE_URL`
を設定した Claude SDK ツールの起動である。それ以外 — RBAC, Bedrock
プロキシ, クレジット計算 — はサーバー側で行われる。

**ブートストラップ**: 新しいマシンでは CLI は単一コマンドで設定される:
`stratoclave setup <server-url>`。このコマンドは
`GET /.well-known/stratoclave-config` を呼び、`config.toml` を具体化する。
以下の [well-known 設定](#well-known-設定) を参照。

### IaC — AWS CDK v2 (TypeScript)

**パス**: [`iac/`](../iac)

8 つのスタック、すべて単一リージョンにデプロイされる。スタック名は
設定可能な `prefix` で名前空間化される。

```
iac/lib/
├── network-stack.ts           # VPC, 2 public subnets
├── dynamodb-stack.ts          # All DynamoDB tables + GSIs (PAY_PER_REQUEST)
├── ecr-stack.ts               # Backend image repository
├── alb-stack.ts               # ALB + target group
├── frontend-stack.ts          # S3 bucket + CloudFront distribution + SPA fallback Function + OAC
├── cognito-stack.ts           # User Pool + domain + App Client (Callback URL from frontend)
├── ecs-stack.ts               # Fargate task, env vars, task role, Secrets Manager wiring
└── config-validator.ts        # Runtime validation of synth-time inputs
```

**依存順** (`addDependency` で強制):
`network → dynamodb → ecr → alb → frontend → cognito → ecs → config`。

Cognito スタックは意図的にフロントエンドスタックに依存する。これにより
CloudFront ドメイン名が通常の CloudFormation `Fn::ImportValue` 機構を
通じて App Client コールバック URL として注入できる —
`crossRegionReferences` も手動のデプロイ後スクリプトも必要ない。

**SPA フォールバック**は、S3 オリジンのデフォルト動作にのみ付与された
CloudFront Function (viewer-request) で実装される。API パス
(`/api/*`, `/v1/*`, `/.well-known/*`) はそのまま通過するので、
バックエンドからの正当な `403` / `404` レスポンスが `index.html` に
書き換えられることはない。

**ECS タスクロール**は次にスコープされる:
- `bedrock:InvokeModel` / `InvokeModelWithResponseStream` (us-east-1 推論プロファイル)。
- Stratoclave のテーブルとその GSI に対する DynamoDB `GetItem`/`PutItem`/`UpdateItem`/`DeleteItem`/`Query`/`Scan`。
- Cognito `AdminCreateUser`, `AdminGetUser`, `AdminInitiateAuth`, `AdminRespondToAuthChallenge`, `AdminDeleteUser`, `AdminSetUserPassword`, `AdminUpdateUserAttributes`, `AdminUserGlobalSignOut`, `ListUsers`。
- タスク自身の Secret ARN のみに対する `secretsmanager:GetSecretValue`。
- `/${prefix}/*` 以下のみに対する `ssm:GetParameter` / `ssm:GetParametersByPath`。

---

## 認証フロー

Stratoclave は 3 つの認証パスをサポートし、いずれも認可で消費される同じ
`AuthenticatedUser` 抽象に収束する。

### 1. Cognito パスワードフロー

ローカル/オフラインの管理と初期管理者ブートストラップに使う。

```
  CLI or Web UI                  Backend                Cognito
    │  POST /api/mvp/auth/login    │                     │
    │  { email, password }         │                     │
    │ ────────────────────────────▶│  admin_initiate_auth│
    │                              │  (ADMIN_USER_       │
    │                              │   PASSWORD_AUTH)    │
    │                              │ ───────────────────▶│
    │                              │◀────────────────────│
    │  { access_token, ... }       │                     │
    │◀─────────────────────────────│                     │
    │                              │                     │
    │  (If NEW_PASSWORD_REQUIRED)  │                     │
    │  POST /api/mvp/auth/respond  │                     │
    │  { new_password, session }   │                     │
    │ ────────────────────────────▶│ admin_respond_      │
    │                              │ to_auth_challenge   │
```

### 2. AWS SSO — STS による vouch

日常的なエンジニアリングアクセスに使う。ユーザーは既に
`aws sso login` を実行済み (またはプロバイダチェーンの他のクレデンシャル
を持つ) で、`stratoclave auth sso` がそのローカル AWS ID を Stratoclave
アクセストークンに変換する。クレデンシャルは送信されない。

```
  CLI                               Backend            STS (global)       Cognito
    │  pick credentials from        │                  │                   │
    │  provider chain               │                  │                   │
    │  sigv4-sign a POST to         │                  │                   │
    │  https://sts.<region>         │                  │                   │
    │  .amazonaws.com/?             │                  │                   │
    │  Action=GetCallerIdentity     │                  │                   │
    │                               │                  │                   │
    │  POST /api/mvp/auth/          │                  │                   │
    │  sso-exchange                 │                  │                   │
    │  { method, url, headers, body}│                  │                   │
    │  ────────────────────────────▶│                  │                   │
    │                               │  forward signed  │                   │
    │                               │  request         │                   │
    │                               │ ────────────────▶│                   │
    │                               │ <Arn><UserId><Account>               │
    │                               │◀─────────────────│                   │
    │                               │                  │                   │
    │                               │  Classify identity_type:             │
    │                               │   sso_user |                         │
    │                               │   federated_role |                   │
    │                               │   iam_user |                         │
    │                               │   instance_profile                   │
    │                               │                  │                   │
    │                               │  Gate 1: account in TrustedAccounts? │
    │                               │  Gate 2: role_name matches allow?    │
    │                               │  Gate 3: identity_type permitted?    │
    │                               │  Gate 4: invite policy (hybrid)      │
    │                               │                  │                   │
    │                               │  Mint random password, set it,       │
    │                               │  run admin_initiate_auth, discard    │
    │                               │  the password                        │
    │                               │ ────────────────────────────────────▶│
    │                               │  { AccessToken, IdToken, … }         │
    │                               │◀──────────────────────────────────── │
    │  { access_token, email,       │                  │                   │
    │    user_id, roles, org_id,    │                  │                   │
    │    identity_type }            │                  │                   │
    │◀──────────────────────────────│                  │                   │
```

4 つのゲートが順序通りに評価され、それぞれがログインを拒否できる。

| ゲート | チェック | 明示的に許可されない場合のデフォルト |
|------|-------|-----------------------------------|
| 1. Trusted account | `account_id` が `TrustedAccounts` に存在するか | **deny** |
| 2. Role pattern | `role_name` が `allowed_role_patterns` の 1 つに一致するか | リストが空なら許可 |
| 3. Identity type | `instance_profile` は `allow_instance_profile=true` を要する。`iam_user` は `allow_iam_user=true` を要する | **deny** (共有 ID のデフォルトは DENY) |
| 4. Provisioning | `invite_only` は `SsoPreRegistrations` を参照。`auto_provision` はセッション名からメールを導出 | 未設定なら `invite_only` |

既存の招待は常にアカウントレベルのポリシーより先に参照される —
これは意図的で、「このロールパターンは自動プロビジョニング、ただしこれらの
指名個人は招待する」を同一アカウントで混在させられるようにするためである。

### 3. 長寿命 API キーフロー

ヘッドレスクライアント (例: Claude Desktop Cowork, CI) が
`access_token` の 1 時間より長い有効期間を持つベアラトークンを必要とする
場合に使う。

```
  Client (Cowork / SDK)          Backend
    │  Authorization:              │
    │  Bearer sk-stratoclave-...   │
    │ ────────────────────────────▶│
    │                              │  Token starts with "sk-stratoclave-"?
    │                              │   → SHA-256 hash
    │                              │   → Lookup ApiKeys by hash
    │                              │   → Check revoked_at, expires_at
    │                              │   → Load owner from Users
    │                              │   → AuthenticatedUser(
    │                              │       auth_kind="api_key",
    │                              │       key_scopes=[...])
    │                              │
    │                              │  On require_permission(X):
    │                              │   allow iff owner.roles.covers(X)
    │                              │   AND key.scopes.covers(X)
```

API キーのプレーンテキストは `POST /api/mvp/me/api-keys` のレスポンスで
**一度だけ**返され、サーバーには保存されない。SHA-256 ハッシュだけが永続化
される。キーの取り消しは条件付き更新で、次のリクエストで反映される
(インプロセスキャッシュなし)。

---

## 認可 (RBAC)

### ロールとパーミッション

デフォルトで 3 つのロールが提供される: `admin`, `team_lead`, `user`。
ロールは `Users.roles: list[str]` に格納される — ユーザーは複数のロールを
持つことができ、パーミッションはそれらの和集合として評価される。

パーミッションは `<resource>:<action>[:<scope>]` 形式の文字列である。
ワイルドカード `resource:*` は*同一リソース*の任意のアクションにマッチする。
ワイルドカードマッチングはリソース名の完全一致を要するので、`users:*` は
`users:create` をカバーするが `users-admin:create` はカバー**しない**。
この保証を安全にするため、リソース名は `-` や `_` を含んではならない
(アクションは含んでもよい)。

出荷時のパーミッションテーブル:

| ロール | パーミッション |
|------|-------------|
| `admin` | `users:*`, `tenants:*`, `usage:*`, `permissions:*`, `accounts:*`, `apikeys:*`, `messages:send` |
| `team_lead` | `tenants:create`, `tenants:read-own`, `usage:read-own-tenant`, `usage:read-self`, `apikeys:read-self`, `apikeys:create-self`, `apikeys:revoke-self`, `messages:send` |
| `user` | `messages:send`, `usage:read-self`, `apikeys:read-self`, `apikeys:create-self`, `apikeys:revoke-self` |

### パーミッションのシード方法

`backend/permissions.json` が人間が編集する真実の源である。
アプリケーション起動時に `backend/bootstrap/seed.py` がファイルの
`version` フィールドと `Permissions` DynamoDB テーブル内の値を比較し、
異なる場合にのみ書き込む。シードは冪等で、再実行 (再デプロイや再起動)
は常に安全である。バージョンが既に最新なら no-op になる。

ランタイムでは `backend/mvp/authz.py` がロール → パーミッションを
プロセス内で 10 秒間キャッシュする。これにより、パーミッションの変更は
TTL 内で調整なしで全 Fargate タスクに有効化される。

### テナント分離

team lead は他のテナントを*構造的に*見ることができてはならず、単に
拒否されるだけでは不十分である。2 つの機構がこれを強制する。

1. `require_tenant_owner(tenant_id)` — FastAPI 依存で、呼び出し元が所有
   しないテナントに対して `404 Not Found` を返す。admin は例外である。
   存在しないテナントも同じ `404` を返す。呼び出し元は「このテナントは
   存在しない」と「このテナントは自分のものではない」を区別できない。
2. `Tenants.team_lead_user_id` は GSI のパーティションキーである。
   team lead の一覧は呼び出し元自身の `user_id` でクエリするので、他の
   テナントは最初からサーバー側で取得されない。

どの team lead にも所有されていない管理者作成のテナントについては、
`team_lead_user_id` 列はセンチネル文字列 `admin-owned` に設定される。
これにより GSI パーティションキーは非 null のまま、実際のユーザーが
誤ってオーナーになることを防ぐ。

### API キーのスコープ絞り込み

長寿命 API キーでリクエストが到着すると、認可は二重チェックを行う。
呼び出し元は、要求するパーミッションがキー所有者のロール**および**
キーのスコープの**両方**で保持されている場合にのみ許可される。

```
allow(permission) ≡
    owner_roles.covers(permission)  AND  key_scopes.covers(permission)
```

ロールはいつでも取り消され得るため (例: admin が user に降格) これは
重要である。admin が所有していた時点で `apikeys:*` で発行されたキーが
あっても、スコープ絞り込みによりキーが所有者の現在の特権を超えることは
ない。降格後の次のリクエストは AND の
`owner_roles.covers(permission)` 側で失敗する。

---

## クレジットモデル

クレジットは Bedrock のトークン (input + output) を単位とする予算で、
`(user_id, tenant_id)` のペアにスコープされる。ユーザーのクレジットは
テナント間で移転しない。ユーザーを新しいテナントに移動させると、新しい
残高が初期化される。

### 計算

`backend/dynamo/user_tenants.py` は、前回の `credit_used` 値を前提条件
として使う条件付き更新で減算を行う。

```
UpdateItem(
    Key = {user_id, tenant_id},
    UpdateExpression    = "SET credit_used = :new_used",
    ConditionExpression = "credit_used = :old_used AND total_credit >= :new_used"
)
```

最後に読んでから別のリクエストが残高を減算していれば、この呼び出しは
`ConditionalCheckFailedException` を発生させ、バックエンドはこれを
リトライヒント付きの `503 Service Unavailable` に変換する。

### 初期残高の優先順位

ユーザーがテナントに紐付けられたとき、開始時の `total_credit` は最初に
一致したルールで選ばれる。

1. ユーザー作成時の明示的な `--total-credit N` → `credit_source = user_override`。
2. さもなくばテナントの `default_credit` → `credit_source = tenant_default`。
3. さもなくばデプロイメント全体のデフォルト (100,000 トークン) → `credit_source = global_default`。

### 呼び出し後の減算 (とクレジット負債ウィンドウ)

`/v1/messages` は呼び出しの*前*には `remaining > 0` だけをチェックし、
その後、レスポンスの*後*に Bedrock が報告した実際の
`input_tokens + output_tokens` で減算する。つまり、単一のリクエストが
残り以上のトークンを消費すると、残高は一時的にわずかに負になり得る。
この動作はエンドポイントの docstring に記載されており、アルファリリース
では許容範囲である。将来の予約ベースモデルがこのウィンドウを閉じる。

### 補充

補充は管理者が `PATCH /api/mvp/admin/users/{user_id}/credit` 経由で行い、
`total_credit` を上書きし、任意で `credit_used` をリセットする。補充
操作は監査される (`event=credit_overwritten`)。

---

## 監査ログ

特権操作は専用のロガー (`stratoclave.audit`) 経由で CloudWatch Logs に
構造化 JSON 行を送出する。ロガーはアプリケーションロガーから分離されて
いるので、下流のログルーティングは通常のリクエストトラフィックを巻き込
まずに監査イベントを購読できる。

| イベント | 発生元 | 主要フィールド |
|-------|------------|------------|
| `admin_created` | `admin_users.py` | `actor_id`, `target_id`, `email` |
| `user_deleted` | `admin_users.py` | `actor_id`, `target_id`, `tenant_id` |
| `user_tenant_switched` | `admin_users.py` | `actor_id`, `target_id`, `before`, `after` |
| `credit_overwritten` | `admin_users.py` | `actor_id`, `target_id`, `before`, `after` |
| `tenant_owner_changed` | `admin_tenants.py` | `actor_id`, `target_id`, `before`, `after` |
| `sso_login_success` | `sso_exchange.py` | `actor_id`, `account_id`, `identity_type`, `arn`, `new_user` |
| `sso_login_denied` | `sso_exchange.py` | `reason`, `account_id`, `identity_type`, `arn` |
| `sso_user_provisioned` | `sso_exchange.py` | `target_id`, `email`, `role`, `tenant_id` |
| `api_key_created` | `me_api_keys.py`, `admin_api_keys.py` | `actor_id`, `target_id`, `scopes`, `expires_at`, `on_behalf_of` |
| `api_key_revoked` | `me_api_keys.py`, `admin_api_keys.py` | `actor_id`, `target_id`, `owner_user_id` |
| `trusted_account_created` / `_updated` / `_deleted` | `admin_trusted_accounts.py` | `actor_id`, `target_id` (=account), `details` |

すべての監査イベントは RFC 3339 UTC の `timestamp` を持つ。将来のリリース
では監査イベントを検索 UI 付きの専用 DynamoDB テーブルに昇格させる。
ワイヤフォーマットは前方互換に設計されている。

---

## well-known 設定

`GET /.well-known/stratoclave-config` は、CLI が自身をブートストラップする
のに必要な情報を返す、認証不要のエンドポイントである: バックエンド URL、
Cognito User Pool ID、App Client ID、Cognito ドメイン、いくつかの CLI
ヒント。レスポンスはキャッシュされ (`Cache-Control: public, max-age=300`)、
バックエンドは `X-Forwarded-Host` + `X-Forwarded-Proto` から `api_endpoint`
を導出するので、CLI は CloudFront URL だけを知っていればよい。

レスポンス形状 (schema_version = `"1"`):

```json
{
  "schema_version": "1",
  "api_endpoint": "https://d8b03j8erit4k.cloudfront.net",
  "cognito": {
    "user_pool_id": "us-east-1_XXXXXXXX",
    "client_id": "1abcd2efgh3ijkl4mnop5qrstu",
    "domain": "https://stratoclave.auth.us-east-1.amazoncognito.com",
    "region": "us-east-1"
  },
  "cli": {
    "default_model": "us.anthropic.claude-opus-4-7",
    "callback_port": 18080
  }
}
```

上記の `api_endpoint` は本ドキュメント全体で使っているサンプルデプロイ
メント URL である。実際の値は `deploy-all.sh` がデプロイの最後に出力する
CloudFront URL である。

### なぜ認証不要で公開できるのか

返されるフィールドはすべて、ユーザーが Cognito Hosted UI をブラウザで
ロードした時点で既に可視である (User Pool ID, App Client ID, Cognito
ドメイン, リージョンはすべて OAuth URL に埋め込まれている)。いずれの
フィールドも単独では何の機能も付与しない — Cognito は依然として有効な
ユーザークレデンシャル、PKCE 検証子、または署名済み STS リクエストを
トークン鋳造に要求する。エンドポイントは `secret`, `password`,
`private_key`, `aws_secret_access_key` のいずれかにマッチする名前の
フィールドを含めることを明示的に拒否する。ランタイムガードが回帰防止
として強制する。

### レスポンスに*含まれない*もの

- 長寿命 API キー (`sk-stratoclave-...`) — これらはユーザー単位の秘密である。
- バックエンドの Bedrock IAM ロール、Secrets Manager ARN、その他の内部
  識別子。
- Cognito クライアントシークレット。Stratoclave が使う App Client は
  *パブリック*クライアント (クライアントシークレットなし) である。
  SPA + PKCE + ネイティブ CLI では標準的。

### CLI の使い方

新規 CLI インストールは次のコマンドで設定される。

```bash
stratoclave setup https://d8b03j8erit4k.cloudfront.net   # your deployment URL
```

これは `GET /.well-known/stratoclave-config` を取得し、
`~/.stratoclave/config.toml` を書き出す。以降のコマンドはそのファイルを
読む。他の帯域外設定は不要である。完全なコマンドリファレンスは
[CLI_GUIDE.md](CLI_GUIDE.md#setup) を参照。

---

## データモデル

すべての永続状態は DynamoDB にある。各テーブルは `PAY_PER_REQUEST`
(オンデマンド) モードでプロビジョニングされる。これは想定されるアクセス
パターン (小規模でバースト性あり) に適しており、キャパシティ調整が不要
になる。

### テーブル

| テーブル | PK | SK | GSIs | 目的 |
|-------|----|----|------|---------|
| `users` | `user_id` (Cognito sub) | `sk="PROFILE"` | `email-index`, `auth-provider-user-id-index` | 権威あるユーザーレコード: email, roles, tenant, SSO メタデータ |
| `user-tenants` | `user_id` | `tenant_id` | `tenant-id-index` | メンバーシップ単位のクレジット残高、ロール、ステータス (active/archived) |
| `tenants` | `tenant_id` | — | `team-lead-index` | テナントメタデータ; GSI は team-lead 一覧を可能にする |
| `permissions` | `role` | — | — | RBAC の真実の源 (`permissions.json` からシード) |
| `usage-logs` | `tenant_id` | `timestamp_log_id` | `user-id-index` | 不変の呼び出しごとレコード; 90 日の TTL |
| `api-keys` | `key_hash` (SHA-256) | — | `user-id-index` | 長寿命 API キー; プレーンテキストは保存されない |
| `trusted-accounts` | `account_id` | — | — | SSO 許可リスト + プロビジョニングポリシー |
| `sso-pre-registrations` | `email` | — | `iam-user-index` | invite-only プロビジョニング用招待 |

### 注目すべき不変条件

- **ユーザーは一度作成され、リネームされない**。PK は Cognito の不変
  `sub` である。メールアドレスの変更は `email-index` を更新するが PK は
  更新しない。
- **使用量ログは不変**。ユーザーが別のテナントに移動されたとき、過去の
  ログは元の `tenant_id` を保持する。team lead のテナントビューは自然に、
  ユーザーがメンバーであった期間のレコードのみを含む。
- **`default-org` は常に存在する**。シードは
  `ConditionExpression = attribute_not_exists(tenant_id)` で挿入する。
  テナント削除では `default-org` は決して削除されない。
- **クレジット残高はアトミックに減算される**。条件付き更新経由であり、
  同時に起こる管理者編集 (例: 補充) とユーザーリクエストは
  `credit_used` の前提条件でシリアライズされる。

---

## スケーリングに関する考慮

- **Fargate**。1 タスクで数百 req/s を快適に処理する (FastAPI + `uvloop`
  のおかげ)。水平スケーリングは CPU と ALB ターゲットレスポンスタイムで
  駆動する。バックエンドはステートレスなので、スケーリングにセッション
  親和性は不要。
- **DynamoDB**。クレジット計算が最もホットなパスである。リクエストごとに
  1 回の条件付き `UpdateItem` である。`usage-logs` の書き込みは
  `tenant_id` でパーティション分散されるので、潜在的にホットなパー
  ティションは `default-org` テナントだけである。`default-org` が大量
  トラフィックを受けるデプロイメントでは、ヘビーユーザーを専用テナント
  へ移動させる。`user-id-index` GSI は依然としてユーザーが自身の履歴を
  グローバルに見られるようにする。
- **CloudFront**。静的アセットは無期限キャッシュ (コンテンツハッシュ付き
  ファイル名)。`/config.json` は毎ページロードで新鮮に配信されるので、
  Cognito 設定の変更は invalidation なしで伝播する。`/api/*` と `/v1/*`
  はキャッシュ無効のパススルー。
- **Cognito**。パスワードパスは `AdminInitiateAuth` を叩く。SSO パスは
  `AdminCreateUser`, `AdminSetUserPassword`, `AdminInitiateAuth` を順に
  叩く。3 つすべて、現実的なワークロードでは Cognito のデフォルト管理
  API レート制限内に収まる (ログインは `/v1/messages` に比べて頻度が
  低い)。
- **Bedrock**。モデルアクセス制限は Bedrock 自身が強制する。Stratoclave
  は 4xx/5xx レスポンスを呼び出し元にそのまま返すが、リトライはしない。

---

## セキュリティに関する考慮

### 認証

- `token_use == "access"` は必須である。`id_token` は `401` で拒否される。
- `client_id` クレームは設定された App Client と一致しなければならない。
  ワイルドカードなし、`aud` なし (アクセストークンは `aud` を持たない)。
- JWKS キーは遅延フェッチされ、`PyJWKClient` でキャッシュされる。
  キーローテーションは透過的。
- 長寿命 API キーは保存時に SHA-256 ハッシュ化される。プレーンテキストは
  ディスクに触れず、ログに現れず、作成レスポンスでのみ返される。

### 認可

- ロールは DynamoDB からのみ来る。Cognito グループ、カスタムクレーム、
  ユーザー属性は無視される。
- パーミッションチェックは*デフォルトで拒否*。すべての保護されたルートは
  `require_permission("...")` を FastAPI `Depends` として使う。
- スコープ絞り込み (所有者ロール ∩ キースコープ) により、侵害されたキー
  が所有者の現在の特権を超えることはない。

### 列挙防御

- team-lead ルートは「自分のものではない」と「存在しない」の両方に対し
  一様に `404` を返す。admin レベルの `GET` だけが、特定の `tenant_id`
  が使用中かを知る唯一の方法である。
- 管理者のユーザー検索も未知の ID に対して同様に `404` で標準化される。

### トランスポートとブラウザ

- HSTS は 1 年の max-age + `includeSubDomains`。
- CSP: `script-src 'none'; default-src 'self'; object-src 'none';
  frame-ancestors 'none'; base-uri 'self'; form-action 'self'`。SPA は
  インライン `<script>` なしでビルドされている。CSP は本番ビルドで
  検証済み。
- すべての入力モデルは `ConfigDict(extra="forbid")` を使うので、予期しない
  リクエストフィールドはエッジで拒否される。

### SSRF

SSO vouch フローは、バックエンドが呼び出し元の代理でアウトバウンド HTTP
呼び出しをする唯一の場所である。`backend/mvp/sso_sts.py` は次を防御する。

- リクエスト URL のホストは STS リージョンエンドポイントのハードコード
  された許可リストに含まれる必要がある。
- URL スキームは `https` でなければならない。
- クエリはちょうど `Action=GetCallerIdentity` を含む必要がある。
- HTTP メソッドは `POST` でなければならない。
- `X-Amz-Date` ヘッダはバックエンドのウォールクロックの ±5 分以内で
  なければならない。
- リクエストは 10 秒のタイムアウトで行われる。

将来のリリースでは、5 分のスキュー内のリプレイウィンドウを閉じるために、
STS 署名-nonce テーブル (TTL 5 分) を追加する。現リリースでは意図的に
省略している。攻撃には署名済みリクエスト**と**バックエンドへのネット
ワークアクセスの同時保有が必要だからである。[拡張ポイント](#拡張ポイント) を
参照。

### シークレット管理

バックエンドが保持する唯一のシークレットは Cognito App Client の
シークレット (client-secret フローを有効にした場合。デフォルト設定は
シークレットレス) である。タスク起動時に ARN で AWS Secrets Manager から
ロードされる。タスクロールの `secretsmanager:GetSecretValue` はその 1 つの
ARN にスコープされている。

---

## 拡張ポイント

Stratoclave は意図的に機能を絞っている。以下は設計済みだがまだ実装されて
いない項目である。

- **STS nonce テーブル**。`X-Amz-Signature` 値をキーとし TTL 5 分の
  DynamoDB テーブル。挿入時の
  `ConditionExpression = attribute_not_exists` で 5 分のリプレイウィンドウ
  を完全に除去できる。
- **監査ログテーブル**。`stratoclave.audit` イベントを CloudWatch から
  検索 UI 付きの専用 DynamoDB テーブルに昇格させ、コンプライアンスクエリ
  に CloudWatch Insights を要求しないようにする。
- **テナント階層**。`tenants` の `parent_tenant_id` 属性により、ネスト
  された組織 (例: 会社内の部門) を可能にする。
- **予約ベースのクレジット**。Bedrock 呼び出し前に見積もりを予約し、
  呼び出し後に差分を精算することで、[クレジットモデル](#クレジットモデル) で
  説明した一時的なクレジット負債ウィンドウを除去する。
- **Verified Permissions**。条件付き認可 (例: 「1 日 $X まで許可」) が
  必要なデプロイメントでは、`require_permission` 境界で Amazon Verified
  Permissions を統合する。

コントリビューションは歓迎である。プロセスは
[CONTRIBUTING.md](../CONTRIBUTING.md) を、脆弱性報告は
[SECURITY.md](../SECURITY.md) を参照。リポジトリ URL は
[`https://github.com/littlemex/stratoclave`](https://github.com/littlemex/stratoclave) である。

---

## 既知の制限

以下は既知の追跡中のギャップである。それぞれ後続リリースで対応予定である。
それまでは以下の通り回避すること。

- **STS リプレイウィンドウ**。署名済み `GetCallerIdentity` リクエストは、
  nonce テーブルなしで ±5 分のスキュー内で受け入れられる。計画された
  修正については [拡張ポイント](#拡張ポイント) を参照。
- **`api-key revoke` は SHA-256 ハッシュを必要とする**。CLI の
  `api-key list` 出力は現在、マスクされた `key_id`
  (`sk-stratoclave-XXXX...YYYY`) を表示するが `key_hash` は表示しないため、
  CLI から取り消すにはハッシュを Admin UI または直接 HTTP 呼び出しから
  取得する必要がある。
  [CLI_GUIDE.md -> 既知の制限](CLI_GUIDE.md#known-limitations) を参照。
- **`admin user create` はデフォルトで一時パスワードを返さない**。
  バックエンドで `EXPOSE_TEMPORARY_PASSWORD=true` が設定されていない限り、
  レスポンスフィールドは `null` である。推奨ワークフローは
  `aws cognito-idp admin-set-user-password --no-permanent` で初回ログイン
  クレデンシャルを発行することである。
  [ADMIN_GUIDE.md -> 新規ユーザーのプロビジョニング](ADMIN_GUIDE.md#provisioning-a-new-user) を参照。
- **`admin trusted-accounts` に CLI サブコマンドがない**。CLI が追いつく
  まで、Web UI または直接の HTTP コールで管理する。
- **単一リージョン**。`us-east-1` のみ。
  [DEPLOYMENT.md -> リージョン制約](DEPLOYMENT.md#regional-constraints) を参照。
