<!-- Last updated: 2026-04-27 -->
<!-- Applies to: Stratoclave main @ 48b9533 (or later) -->

# 管理者ガイド

本ガイドは Stratoclave デプロイメントで `admin` ロールを持つ運用者向けである。ユーザーとテナントの管理、API キーの発行、SSO 用 AWS アカウントの許可リスト化、使用量の確認、ブートストラップ後の制御プレーンのロックダウンといった、管理者が行うべき日常タスクを扱う。

単に Claude と会話したり CLI を実行したいだけのユーザーは、まず [GETTING_STARTED.md](GETTING_STARTED.md) を参照。自分の AWS アカウントに Stratoclave を**デプロイ**する場合は、先に [DEPLOYMENT.md](DEPLOYMENT.md) を読み、ブートストラップ管理者が存在するようになってから本ガイドに戻ること。CLI の権威あるリファレンスは [CLI_GUIDE.md](CLI_GUIDE.md) を参照。

---

## 目次

1. [RBAC モデル](#rbac-モデル)
2. [管理者としてのログイン](#管理者としてのログイン)
3. [ユーザー管理](#ユーザー管理)
4. [テナント管理](#テナント管理)
5. [SSO: 信頼された AWS アカウント](#sso-信頼された-aws-アカウント)
6. [API キー](#api-キー)
7. [使用量の確認](#使用量の確認)
8. [Web URL を CLI ユーザーへ渡す](#web-url-を-cli-ユーザーへ渡す)
9. [ブートストラップ後のロックダウン](#ブートストラップ後のロックダウン)
10. [監査ログのリファレンス](#監査ログのリファレンス)
11. [パスワードリセット (管理者支援)](#パスワードリセット-管理者支援)
12. [トラブルシューティング](#トラブルシューティング)

---

## RBAC モデル

Stratoclave は 3 つのロールを提供する。ロールは `stratoclave-users` DynamoDB テーブルに格納され、リクエストごとにバックエンドで評価される。

| ロール      | スコープ |
| ----------- | ----- |
| `admin`     | すべて。ユーザー、テナント、信頼アカウント、SSO 招待、API キーの管理、グローバル使用量の閲覧、クレジット設定。 |
| `team_lead` | 自分が所有するテナントのみを管理: メンバーの招待と削除、クレジットの調整、テナント別使用量の閲覧。他のテナントは見えない。 |
| `user`      | メッセージ送信、自分の使用量の閲覧、自分の API キーの更新。他のユーザーは見えない。 |

**テナント分離**: 各ユーザーは常にちょうど 1 つのアクティブテナントに所属する。`team_lead` は自分が所有するテナントしか見られない。`admin` は全テナントを見られる。`default-org` テナントはバックエンドの初回起動時に自動的にシードされ、削除できない。明示的なテナント割り当てがないユーザーのフォールバック先である。

**パーミッションのシード**: DynamoDB (`stratoclave-permissions`) の `admin`, `team_lead`, `user` のパーミッション行は、バックエンド起動時に `bootstrap/seed.py` によって冪等にシードされる。管理者が手動で DynamoDB スクリプトを実行する必要は**ない**。

---

## 管理者としてのログイン

管理者は Web UI からログインする。他のユーザーと同じ手順である。ブートストラップ管理者は [`scripts/bootstrap-admin.sh`](../scripts/bootstrap-admin.sh) によって作成される。作成方法は [DEPLOYMENT.md](DEPLOYMENT.md#post-deploy-first-admin) を参照。

1. ブラウザでデプロイメント URL (例: `https://<your-deployment>.cloudfront.net`) を開く。
2. 管理者のメールアドレスと `bootstrap-admin.sh` が出力したパスワードを入力する。
3. ヘッダに管理者専用のナビゲーション項目 (**Users**, **Tenants**, **Trusted Accounts**, **Usage**) が表示される。

`STRATOCLAVE_API_ENDPOINT` を設定しておけば、すべての管理操作を CLI から実行することもできる。

```bash
export STRATOCLAVE_API_ENDPOINT="https://<your-deployment>.cloudfront.net"   # your deployment URL
stratoclave setup https://<your-deployment>.cloudfront.net
stratoclave auth login --email admin@example.com
stratoclave admin user list
```

> **CLI の名詞は単数形**: CLI サブコマンドは `stratoclave admin user ...` と `stratoclave admin tenant ...` である。複数形の `admin users` / `admin tenants` は**存在しない**。

---

## ユーザー管理

### 新規ユーザーのプロビジョニング

Stratoclave の `user create` フローは、意図的に初回ログイン用パスワードを**発行しない**。ユーザーレコードを作成してから、Cognito 側で一時パスワードを設定し、安全なチャネルでユーザーに渡す。

#### CLI から (推奨)

```bash
# 1. Stratoclave のユーザーレコードを作成する。
stratoclave admin user create \
  --email newuser@example.com \
  --role user \
  --tenant default-org

# 2. Cognito で一時パスワードを設定する。`--no-permanent` でユーザーを
#    FORCE_CHANGE_PASSWORD 状態にするので、次回ログイン時に新しいパスワードが必要になる。
aws cognito-idp admin-set-user-password \
  --user-pool-id us-east-1_EXAMPLE \
  --username newuser@example.com \
  --password 'TempPassword!23' \
  --no-permanent \
  --region us-east-1
```

一時パスワードはパスワードマネージャの共有リンク、1Password リンク、または他の安全なチャネル経由でユーザーに渡すこと。初回ログイン時、ユーザーは恒久パスワードの設定を求められる。

> **なぜ 2 ステップなのか**: デフォルトでは `admin user create` のレスポンスには `temporary_password` フィールドが**含まれない**。バックエンドはこれを `null` に設定するので、作成者のログ、スクリーンショット、シェル履歴に再利用可能なクレデンシャルが残らない。レガシー動作 (プレーンテキストをレスポンスに含める) が必要なら、デプロイ前にバックエンド ECS タスク定義に `EXPOSE_TEMPORARY_PASSWORD=true` を設定する。本番環境では非推奨である。

#### Web UI から

**Users -> New user** に移動する。次を埋める。

| フィールド            | 備考 |
| ---------------- | ----- |
| Email            | Cognito のユーザー名として使われる。デプロイメント内でユニークである必要がある。 |
| Role             | `user` または `team_lead`。`admin` オプションはバックエンドで `ALLOW_ADMIN_CREATION=true` が設定されていないと無効である。 |
| Tenant           | 空欄にすると `default-org`。 |
| Credit override  | 空欄にするとテナントの `default_credit` を継承する。 |

UI でも一時パスワードは表示されない。フォーム送信後、上述の `aws cognito-idp admin-set-user-password --no-permanent` を同様に行う。

### ユーザーの一覧表示

```bash
stratoclave admin user list [--role R] [--tenant T] [--limit N]
```

`--limit` のデフォルトは `50` である。コマンドは email, user_id, tenant, roles, total credit, remaining credit の固定幅テーブルを表示する。

### 単一ユーザーの検査

```bash
stratoclave admin user show <user_id>
```

### ユーザーを別テナントへ移動する

```bash
stratoclave admin user assign-tenant <user_id> \
  --tenant <new_tenant_id> \
  [--new-role user|team_lead] \
  [--total-credit N]
```

バックエンドは次を行う。

1. 現在の `user_tenants` 行をアーカイブする。
2. `status=active` の新しい `user_tenants` 行を作成し、`--total-credit` か新しいテナントの `default_credit` を適用する。
3. Cognito 属性 `custom:org_id` を更新する。
4. `AdminUserGlobalSignOut` を呼んでユーザーの全セッションを無効化し、再認証を強制する。

### ユーザーのクレジット調整

```bash
stratoclave admin user set-credit <user_id> --total N [--reset-used]
```

`--reset-used` は `credit_used` をゼロにする。新しい請求期間の開始時に便利。変更は即時に反映され、ユーザーの次回リクエストは新しい値で評価される。

### ユーザーの削除

```bash
stratoclave admin user delete <user_id>
```

起こること:

1. ユーザーが Cognito から削除される (ログインできなくなる)。
2. `stratoclave-users` の行が削除される。
3. `stratoclave-user-tenants` の行はアーカイブ (削除されない) され、過去のメンバーシップが保持される。
4. `stratoclave-usage-logs` のエントリは保持されるので、監査履歴の帰属は維持される。

ガードレール:

- 自分自身の削除は HTTP `409` で拒否される。
- 最後の `admin` の削除は HTTP `409` で拒否される。

---

## テナント管理

**テナント**はクレジットプールを所有する組織単位である。通常はチーム、部門、または顧客アカウントに対応する。

### テナントの作成

```bash
stratoclave admin tenant create --name "Team A" \
  [--team-lead <user_id> | --team-lead-email lead@example.com] \
  [--default-credit N]
```

`--team-lead` と `--team-lead-email` は最大 1 つだけ指定できる。両方省略すると所有権はセンチネル文字列 `admin-owned` になる。これは「共有、管理者にのみ可視」を意味する。`--team-lead-email` 形式はクライアント側で `admin user list` を通じて Cognito sub に解決される。

### テナントの一覧表示と検査

```bash
stratoclave admin tenant list [--limit N]
stratoclave admin tenant show <tenant_id>
stratoclave admin tenant members <tenant_id>
stratoclave admin tenant usage <tenant_id> [--since-days N]
```

`team-lead` 版と比較して、`admin tenant members` は出力に `user_id` を含める。

### 所有権の再割り当て

```bash
stratoclave admin tenant set-owner <tenant_id> \
  [--team-lead <user_id> | --team-lead-email lead@example.com]
```

重大な操作である。監査ログはアクターと前オーナーの両方を記録する。前オーナーはテナントへの可視性を即座に失う。

### テナントのアーカイブ

```bash
stratoclave admin tenant delete <tenant_id>
```

アーカイブはソフトである。行は `status=archived` でフラグされるが、使用量ログとユーザー-テナント履歴は保持される。`default-org` テナントはアーカイブできない。テナントをアクティブテナントとして使っていたユーザーは自動的には再割り当てされない。アクティブメンバーがゼロになっているテナントのみをアーカイブすること。

---

## SSO: 信頼された AWS アカウント

Stratoclave は AWS ネイティブな ID からのフェデレーションログインを受け入れる。IAM Identity Center ユーザー、SAML フェデレーションロール、IAM ユーザー、EC2 インスタンスプロファイルである。フェデレーションを許可する AWS アカウントを許可リストに登録し、オプションでアカウントごとのプロビジョニングルールを指定できる。

### サポートされる ID タイプ

| ID タイプ        | `identity_type`    | 典型的なソース |
| -------------------- | ------------------ | -------------- |
| SSO user             | `sso_user`         | IAM Identity Center (`session_name == email`)。 |
| Federated role       | `federated_role`   | SAML またはエンタープライズ IdP 経由の `AssumeRoleWithSAML`。 |
| IAM user             | `iam_user`         | アクセスキーを持つ長寿命 IAM ユーザー。 |
| EC2 instance profile | `instance_profile` | EC2 インスタンスメタデータから引き受けたロール。 |

デフォルトでは `sso_user` と `federated_role` のみが受け入れられる。`iam_user` と `instance_profile` は複数の人間が共有できるため、信頼アカウントごとに明示的にオプトインする必要がある。

### 信頼アカウントの追加

Web UI の **Trusted Accounts -> Add account**、または `POST /api/mvp/admin/trusted-accounts` を直接呼び出して管理する。`stratoclave admin trusted-account ...` 系の CLI サブコマンドはまだ利用できない ([既知の制限](CLI_GUIDE.md#known-limitations) 参照)。

| フィールド                   | 備考 |
| ----------------------- | ----- |
| AWS Account ID          | 12 桁のアカウント番号。 |
| Provisioning policy     | `invite_only` (デフォルト、最も安全) または `auto_provision`。 |
| Allowed role patterns   | 引き受けたロール ARN にマッチする glob パターン。空リストは「任意のロール」を意味する。 |
| Allow IAM user          | デフォルトでオフ。ブレイクグラスまたは自動化アカウントのみでオプトインする。 |
| Allow instance profile  | デフォルトでオフ。対話的用途では強く非推奨。 |
| Default tenant / credit | 招待が上書きしない限り、自動プロビジョニングされたユーザーに適用される。 |

### Invite-only と auto-provisioning

- `invite_only` (推奨): 管理者がメールアドレスを事前登録しない限り、誰もログインできない。本番環境で最も安全。
- `auto_provision`: セッション名が有効なメールアドレスにマッチする限り、アカウントからの任意の呼び出し元が `user` ロールで要求時に作成される。すべての SSO ユーザーを信頼できる内部の IdP バックアカウントに適している。

**招待は常に勝つ**: `auto_provision` であっても、着信メールアドレスに招待が存在すれば、そのロール、テナント、クレジットはアカウントレベルのデフォルトを上書きする。

### `session_name != email` のエンタープライズ SAML

一部のエンタープライズ IdP は、セッション名をユーザーのメールアドレスではなく不透明な識別子に設定する。これをマッピングするには次を行う。

1. セッション名を収集する (ログイン失敗後に CloudTrail で確認できる)。
2. `Email = user@example.com` **かつ** `IAM user name = <session-name>` の招待を作成する。
3. 次回そのセッションからログインすると、メールアドレスをキーとした Stratoclave ユーザーがプロビジョニングされる。

### 信頼アカウントの無効化

信頼アカウントを削除すると、そのアカウントの保留中の招待も削除される。そのアカウントからプロビジョニングされた既存の Stratoclave ユーザーは、`stratoclave admin user delete <user_id>` で明示的に削除するまで動作し続ける。

---

## API キー

Stratoclave は、マシン間アクセス、CI ジョブ、同梱 CLI や Claude Desktop Cowork を含む統合向けに、`sk-stratoclave-...` 形式の長寿命 API キーを発行する。

キーが保持するスコープ:

- `messages:send` -- `POST /v1/messages` を呼ぶ。
- `usage:read-self` -- 所有者自身の使用量を読む。

### 自分自身のキーを発行する

Web UI: **Account -> API keys -> New key**。または CLI で:

```bash
stratoclave api-key create \
  --name "my-ci-key" \
  --scope messages:send \
  --scope usage:read-self \
  --expires-days 30
```

フルシークレットは**ちょうど一度**表示される。即座に保存すること。バックエンドは SHA-256 ハッシュだけを保持する。

### 他のユーザーを代理してキーを発行する

バックエンドは代理発行用に `POST /api/mvp/admin/users/{user_id}/api-keys` を公開している。対話的にはログインしないヘッドレスなサービスアカウントのオンボーディングに便利。監査ログはアクターと `on_behalf_of` ユーザーの両方を記録する。

専用 CLI サブコマンドはまだ利用できない。それまでは HTTP API を直接呼び出すこと。

```bash
curl -X POST "https://<your-deployment>.cloudfront.net/api/mvp/admin/users/$USER_ID/api-keys" \
  -H "Authorization: Bearer $(jq -r .access_token ~/.stratoclave/mvp_tokens.json)" \
  -H 'Content-Type: application/json' \
  -d '{"name": "pipeline-prod", "scopes": ["messages:send"], "expires_in_days": 90}'
```

返されたプレーンテキストを安全なチャネルでユーザーに渡す。

### キーの取り消し

取り消しは即時に有効化される。キーを使った次のリクエストは `401 Unauthorized` を返す。

- セルフサービス: **Account -> API keys -> Revoke**、または `stratoclave api-key revoke <key_hash>`。
- 管理者オーバーライド: **Admin -> API keys -> Revoke any key**、または `stratoclave api-key admin-revoke <key_hash>`。

> **既知の制限**: `stratoclave api-key list` は現在、出力に `key_hash` を含めないため、CLI から取り消すには SHA-256 ハッシュを他の場所 (管理者 Web UI、または `GET /api/mvp/admin/api-keys` への直接 HTTP コール) から取得する必要がある。回避策は [CLI_GUIDE.md](CLI_GUIDE.md#known-limitations) を参照。

### すべての API キーの閲覧 (管理者のみ)

```bash
stratoclave api-key admin-list [--include-revoked]
```

各行にはマスク済みの `key_id`, `owner=<user_id>`, キーの名前が含まれる。

---

## 使用量の確認

### グローバル使用量

`stratoclave admin usage show` (または Web UI の Admin Usage ページ) は `stratoclave-usage-logs` をクエリする。

```bash
stratoclave admin usage show \
  [--tenant T] \
  [--user U] \
  [--since 2026-04-01T00:00:00Z] \
  [--until 2026-04-30T23:59:59Z] \
  [--limit N]
```

バックエンドはインデックスを `tenant_id > user_id > フルスキャン` の順で選ぶ。テーブルスキャンを避けるため、可能な限り `--tenant` または `--user` を渡すこと。

### テナント別サマリ

```bash
stratoclave admin tenant usage <tenant_id> [--since-days N]
```

Web UI のテナント詳細ページは同じデータを 2 本の棒グラフ (モデル別、ユーザー別) と CSV エクスポートで描画する。

### ユーザー別サマリ

Web UI の各ユーザー詳細ページは `credit_remaining`, `credit_used`, および過去 30 日のアクティビティのスパークラインを表示する。ユーザー一覧は同じ列を一目で把握できる形で提示する。

---

## Web URL を CLI ユーザーへ渡す

デプロイして管理者をブートストラップしたら、デプロイメント URL をユーザーに共有する。

```bash
stratoclave setup https://<your-deployment>.cloudfront.net    # your deployment URL
export STRATOCLAVE_API_ENDPOINT="https://<your-deployment>.cloudfront.net"
stratoclave auth login --email user@example.com
```

`stratoclave setup` はバックエンドから `/.well-known/stratoclave-config` を取得し、`~/.stratoclave/config.toml` を書き出す。ユーザーは Cognito プール ID やクライアント ID を知る必要は**ない**。`setup` がそれらを自動で検出する。

---

## ブートストラップ後のロックダウン

`scripts/bootstrap-admin.sh` はバックエンドが `ALLOW_ADMIN_CREATION=true` で動作している必要がある。これは `POST /api/mvp/admin/users` に `roles=['admin']` で任意の呼び出し元に公開することを意味し、ゼロステートのケースに意図されているが、**本番環境で有効のままにしてはならない**。

最初の管理者がログインできるようになったら、フラグを無効化する。

1. CDK 実行環境で `ALLOW_ADMIN_CREATION=false` を設定する (または unset する)。
2. ECS スタックを再デプロイする:

   ```bash
   cd iac && npx cdk deploy <Prefix>EcsStack
   ```

3. (任意) 環境変数を即座に反映するために新規タスクの起動を強制する:

   ```bash
   aws ecs update-service \
     --cluster <PREFIX>-cluster \
     --service <PREFIX>-backend \
     --force-new-deployment
   ```

これ以降、新規管理者は既存の管理者によって Web UI 経由でのみ昇格できる。

---

## 監査ログのリファレンス

バックエンドはすべての特権操作について、CloudWatch Logs グループ `/ecs/<prefix>-backend` に構造化 JSON ログを送出する。便利な CloudWatch Logs Insights クエリ:

```
fields @timestamp, event, actor_email, target_email, tenant_id
| filter event like /^admin_|^user_|^tenant_|^sso_|^trusted_account_|^api_key_|^credit_/
| sort @timestamp desc
```

| イベント                                                                           | 発生元 |
| ------------------------------------------------------------------------------- | ---------- |
| `admin_created`, `user_created`, `user_deleted`                                 | `POST /DELETE /api/mvp/admin/users` |
| `tenant_created`, `tenant_updated`, `tenant_archived`, `tenant_owner_changed`   | `/api/mvp/admin/tenants[*]` |
| `user_tenant_switched`, `credit_overwritten`                                    | ユーザー変更エンドポイント |
| `sso_login_success`, `sso_login_denied`, `sso_user_provisioned`                 | `POST /api/mvp/auth/sso-exchange` |
| `sso_invite_created`, `sso_invite_deleted`                                      | `/api/mvp/admin/sso-invites[*]` |
| `trusted_account_created`, `trusted_account_updated`, `trusted_account_deleted` | `/api/mvp/admin/trusted-accounts[*]` |
| `api_key_created`, `api_key_revoked`                                            | `/api/mvp/{me,admin}/api-keys[*]` |

すべてのイベントには `request_id` (ALB ヘッダから伝播) が含まれるので、UI または CLI のアクションと正確なバックエンドログ行を相関させられる。

---

## パスワードリセット (管理者支援)

ユーザーがパスワードを忘れた場合、AWS CLI でリセットを強制する。

```bash
aws cognito-idp admin-set-user-password \
  --user-pool-id <YOUR_USER_POOL_ID> \
  --username 'user@example.com' \
  --password 'TempPassword!23' \
  --no-permanent \
  --region us-east-1
```

`--no-permanent` はユーザーを `FORCE_CHANGE_PASSWORD` 状態に戻す。次回の `stratoclave auth login` で新しい恒久パスワードの入力が求められる。

---

## トラブルシューティング

| 症状 | 対処 |
|---------|-----|
| `stratoclave admin user create` が `temporary_password` が `null` のオブジェクトを返す | 意図的なデフォルト。[新規ユーザーのプロビジョニング](#新規ユーザーのプロビジョニング) を参照し、`aws cognito-idp admin-set-user-password --no-permanent` を追いかけること。 |
| `stratoclave admin user create ... --role admin` が `403` を返す | 実行中のバックエンドタスクで `ALLOW_ADMIN_CREATION` が `true` になっていない。ブートストラップ期間中だけ再有効化し、その後無効に戻すこと。[ブートストラップ後のロックダウン](#ブートストラップ後のロックダウン) を参照。 |
| `stratoclave admin users list` が `clap` 解析エラーを返す | 名詞は単数形。`stratoclave admin user list` を実行する。`admin tenant list` も同様。 |
| `stratoclave api-key revoke` が "key not found" で失敗する | コマンドは SHA-256 ハッシュを期待しており、`api-key list` が出力するマスク済み `sk-stratoclave-XXXX...YYYY` 識別子ではない。Admin UI を使うか、`GET /api/mvp/admin/api-keys` でハッシュを取得して `DELETE /api/mvp/admin/api-keys/{key_hash}` を呼ぶこと。 |
| 初回デプロイ後に Admin UI のタイルがおかしい | `Cmd+Shift+R` / `Ctrl+Shift+R` でハードリロードし、CloudFront がキャッシュしている SPA バンドルを破棄する。 |
| 他のリクエストは成功するのに管理者アクションが `403` を返す | ユーザーレコードが `roles=["user"]` のまま。Web UI を使うか `PATCH /api/mvp/admin/users/{user_id}/roles` を呼んで `admin` を追加する。 |
| SSO ログインが `sso_login_denied` で拒否される | 対応する `sso_login_denied` 監査イベントの `reason` フィールドを確認する。よくある原因: 信頼アカウントエントリが無い、ロールパターンが一致しない、ID タイプがオプトインされていない。 |

それでも解決しない場合は、[`littlemex/stratoclave`](https://github.com/littlemex/stratoclave/issues) に関連する監査イベントの `request_id` を添えて Issue を立てること。

---

## 関連ドキュメント

- [GETTING_STARTED.md](GETTING_STARTED.md) -- エンドユーザー向け。
- [CLI_GUIDE.md](CLI_GUIDE.md) -- すべての `stratoclave` サブコマンドのリファレンス。
- [DEPLOYMENT.md](DEPLOYMENT.md) -- 新規 Stratoclave デプロイメントの立ち上げ方。
- [ARCHITECTURE.md](ARCHITECTURE.md) -- 各要素がどう組み合わさっているか。
- [SECURITY.md](../SECURITY.md) -- 脆弱性の報告と脅威モデル。
