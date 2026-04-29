<!-- Last updated: 2026-04-27 -->
<!-- Applies to: Stratoclave main @ 48b9533 (or later) -->

# Stratoclave と Claude Desktop Cowork の連携

Claude Desktop の **Cowork** 機能は、長時間のエージェント的タスクを実行し、Anthropic を直接呼び出す代わりにカスタムゲートウェイ経由で推論リクエストをルーティングするように設定できる。本ガイドは、そのゲートウェイとして Stratoclave デプロイメントを使い、Cowork リクエストがユーザー単位およびテナント単位で認証され、クレジット計上され、監査ログに記録されるようにする方法を示す。モデル呼び出し自体は引き続き Amazon Bedrock 上で実行される。

Cowork はヘッドレスで長時間稼働するクライアントなので、Cognito `access_token` の 1 時間の有効期間は不便である。したがって Stratoclave は、管理者が許可する限り Cowork がベアラトークンとして使える**長寿命 API キー** (`sk-stratoclave-...`) を発行する。

## 目次

- [前提条件](#前提条件)
- [ステップ 1. 長寿命 API キーを発行する](#ステップ-1-長寿命-api-キーを発行する)
- [ステップ 2. Claude Desktop を設定する](#ステップ-2-claude-desktop-を設定する)
- [ステップ 3. 接続を検証する](#ステップ-3-接続を検証する)
- [ステップ 4. Cowork を使い始める](#ステップ-4-cowork-を使い始める)
- [ダウンタイムなしでキーをローテートする](#ダウンタイムなしでキーをローテートする)
- [トラブルシューティング](#トラブルシューティング)
- [セキュリティに関する注記](#セキュリティに関する注記)
- [内部動作 (興味がある人向け)](#内部動作-興味がある人向け)

---

## 前提条件

始める前に、以下を確認すること。

- Stratoclave デプロイメントが `https://<subdomain>.cloudfront.net` の形式 (例: `https://<your-deployment>.cloudfront.net`) の CloudFront URL で HTTPS 越しに到達可能であること。まだない場合は、先に [DEPLOYMENT.md](DEPLOYMENT.md) に従うこと。
- Web UI または CLI (`stratoclave auth login` あるいは `stratoclave auth sso`) のどちらかでデプロイメントにログインできること。任意のロール (`user`, `team_lead`, `admin`) が自身の API キーを発行できる。
- Claude Desktop がワークステーションにインストールされていること。Cowork のゲートウェイ機能には **Developer Mode** が必要 (ステップ 2 参照)。
- Stratoclave ユーザーが `messages:send` パーミッションを持つこと。デフォルトの 3 つのロールはすべてこれを含む。カスタマイズされたパーミッションテーブルを運用している場合は、値が含まれていることを確認する。

デフォルト以外のモデルを呼び出す予定なら、デプロイメントのリージョンで目的の Bedrock 推論プロファイルが有効になっているか管理者に確認すること。

---

## ステップ 1. 長寿命 API キーを発行する

Stratoclave は同一のキーを生成する 2 つの発行経路を公開している。便利な方を選ぶ。

### オプション A -- CLI

```bash
stratoclave api-key create \
  --name "cowork on laptop" \
  --scope messages:send \
  --scope usage:read-self \
  --expires-days 30
```

出力の最終行が、`sk-stratoclave-` で始まるプレーンテキストのキーである。

```text
sk-stratoclave-EXAMPLE0000000000000000000000000000
```

このプレーンテキストは即座にパスワードマネージャに保存すること。サーバーは SHA-256 ハッシュだけを保持し、プレーンテキストを再表示することはできない。

### オプション B -- Web UI

1. Stratoclave Web UI にログインする。
2. ヘッダから **API keys** をクリックするか、直接 `/me/api-keys` へ移動する。
3. **New key** をクリックし、ダイアログを埋める:

   | フィールド       | 推奨値                              | 備考 |
   | ----------- | -------------------------------------------- | ----- |
   | Label       | `cowork on <machine-name>`                   | 最大 64 文字。後で正しいキーを取り消せるようリストに表示される。 |
   | Scopes      | `messages:send`, `usage:read-self` (デフォルト) | スコープは自分自身のロールで絞り込まれる。自分のアカウントより強力なキーは発行できない。 |
   | Expiration  | `30 days` (推奨)                      | 選択肢: 7 / 30 / 90 / 180 / 365 日、または無期限。 |

4. 確定する。キーの**プレーンテキスト**がコピー用コントロール付きのモーダルで**一度だけ**表示される。即座にパスワードマネージャにコピーする。

---

## ステップ 2. Claude Desktop を設定する

### 2.1 Developer Mode を有効にする

1. Claude Desktop を開く。
2. アプリケーションメニューで **Help -> Troubleshooting -> Enable Developer Mode** を選ぶ。
3. メニューバーに **Developer** メニューが表示される。

### 2.2 ゲートウェイ設定を開く

**Developer** メニューから **Configure Third-Party Inference** を選び、モードを **Gateway** に切り替える。

### 2.3 ゲートウェイフィールドを埋める

Stratoclave デプロイメントの CloudFront URL と、ステップ 1 のプレーンテキスト API キーを使う。

| フィールド                     | 値 |
| ------------------------- | ----- |
| Gateway base URL          | `https://<your-deployment>.cloudfront.net` (例: `https://<your-deployment>.cloudfront.net`) |
| Gateway auth scheme       | `Bearer` |
| Gateway API key           | `sk-stratoclave-...` (ステップ 1 のプレーンテキスト) |
| Gateway extra headers     | *(空のまま)* |
| Model list                | `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5` (または自動検出のために空のまま) |
| Organization UUID         | *(任意、空のまま)* |
| Credential helper script  | *(空のまま。長寿命キーでは不要)* |

> **重要**: ゲートウェイのベース URL は `/v1` を**含めてはならない**。Cowork は自動で `/v1/models` と `/v1/messages` をベース URL に追加する。`https://<host>/v1` と入力すると、Cowork は `https://<host>/v1/v1/models` をリクエストしてしまい、バックエンドはこれをルーティングせず `404` を返す。これは最も一般的な設定ミスであり、何かが動かないときは最初にチェックすること。

### 2.4 保存と再起動

**Save locally** をクリックしてから **Claude Desktop を再起動**する。Cowork はゲートウェイ設定を起動時にのみ読むので、フィールド変更を反映するには再起動が必要である。

---

## ステップ 3. 接続を検証する

**Developer** メニューから **Test Third-Party Inference** を選ぶ。成功するとテストは次を表示する。

```text
Gateway API key was accepted.
```

Stratoclave 側では、`GET /v1/models` および/または `POST /v1/messages` の呼び出しが CloudWatch Logs に自分の `user_id` タグ付きで記録される。キーに `usage:read-self` を含めていれば、CLI からも確認できる。

```bash
stratoclave usage show
```

---

## ステップ 4. Cowork を使い始める

テストが緑になったら、Cowork を通常通り使える。各推論リクエストは次を通過する。

1. Cowork が `Authorization: Bearer sk-stratoclave-...` を付けて `POST /v1/messages` を CloudFront URL に送る。
2. CloudFront がリクエストを ALB に転送し、ALB がバックエンドタスクへルーティングする。
3. バックエンドがキーを検証する: ハッシュ検索、取り消しチェック、有効期限チェック。次に所有者の現在のロールを解決する。
4. 要求されたパーミッションが所有者のロール**かつ**キーのスコープの**両方**で保持されている場合にのみ、リクエストは許可される。
5. バックエンドは Bedrock `converse` / `converseStream` を呼び、レスポンスを Cowork へストリームで返し、Bedrock が報告した正確な `input_tokens` と `output_tokens` で使用量ログを書き込む。
6. `(user_id, tenant_id)` のクレジット残高が、条件付き DynamoDB 更新で合計トークン数だけ減算される。

---

## ダウンタイムなしでキーをローテートする

1. `stratoclave api-key create ...` で新しいキーを発行する。
2. Claude Desktop の **Gateway API key** フィールドを新しいプレーンテキストで更新し、Claude Desktop を再起動する。
3. 古いキーを取り消す。

Stratoclave は取り消しをキャッシュしないので、取り消し直後に古いキーは無効になる。

> **取り消しの現状**: `stratoclave api-key revoke` はプレーンテキストの SHA-256 ハッシュを必要とし、`stratoclave api-key list` が表示するマスク済みの `sk-stratoclave-XXXX...YYYY` 識別子ではない。リスト出力が拡充されるまでは、Web UI (**Account -> API keys -> Revoke**) または `DELETE /api/mvp/me/api-keys/{key_hash}` を直接使って取り消すこと。[CLI_GUIDE.md -> 既知の制限](CLI_GUIDE.md#known-limitations) を参照。

---

## トラブルシューティング

### `Gateway returned HTTP 404` で `https://<host>/` や `https://<host>/v1/v1/models` のようなエンドポイント

ゲートウェイのベース URL にほぼ確実に `/v1` が含まれている。URL を裸の `https://<host>` にし、ローカルに保存して Claude Desktop を再起動する。

### `Gateway API key was rejected` / `401 Unauthorized`

可能性の高い順の原因:

- キーが周囲の空白付き、または文字欠落で貼り付けられた。キーを再発行し、注意深く貼り付け直す。
- キーが失効した。Web UI または `stratoclave api-key list` で確認する。
- キーが取り消された。リストビューの `revoked_at` 列が埋まっている。
- 所有者が削除された。API キーパスは所有者の `Users` レコードが引き続き存在することを要求する。

### テストリクエストは成功したのに `POST /v1/messages` で `403 Forbidden`

キーは認証を通過した (テストは認証のみ検査する) が、`messages:send` の認可で失敗した。よくある原因は 2 つ:

- キーが `--scope usage:read-self` で発行されたが `--scope messages:send` は**含まれていない**。両方のスコープで再発行するか、デフォルトで再発行する。
- 所有者の現在のロールに `messages:send` が含まれない。デフォルトの 3 ロールはすべて含むが、カスタマイズされたパーミッションテーブルで変わる可能性がある。管理者に相談する。

### `402 Payment Required` / `credit_exhausted`

所有者のテナントがクレジットを使い切っている。管理者に `stratoclave admin user set-credit <user_id> --total N --reset-used` で増やしてもらう。

### モデルリストが空で Cowork が *No models* を表示する

**Model list** フィールドが空だと、Cowork は `GET /v1/models` を呼びモデル ID のリストを期待する。Stratoclave の `/v1/models` は有効な `Authorization: Bearer` ヘッダを要求する。キーが受け入れられてもそのスコープが `messages:send` をカバーしないと、自動検出プローブは `403` を返し、Cowork は「No models」と描画する。キーに `messages:send` を追加するか、モデル ID を明示的に列挙する (`claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`, ...)。

### ストリーミングがハングしているように見える

Cowork は Server-Sent Events (`Accept: text/event-stream`) を使う。Stratoclave は `Cache-Control: no-cache`, `Connection: keep-alive`, `X-Accel-Buffering: no` を設定しているので CloudFront はストリームをバッファしない。デプロイメントの前に別の CDN を置いている場合、そちらでも SSE パススルーが有効になっていることを確認する。

### `422 max_tokens exceeds 32768`

バックエンドはリクエストごとに `max_tokens` を 32768 に制限する。Cowork はより低いクライアント側制限を設定できる。32768 以下にする。

---

## セキュリティに関する注記

- **プレーンテキストはサーバー側に保持されない**。SHA-256 ハッシュだけが格納される。データベースが漏洩しても API キーは漏洩しない。
- **取り消しは即時**。取り消し後に到着する次のリクエストは `401` で拒否される。
- **スコープ絞り込み**。必要最小限のスコープでキーを発行する (`messages:send` だけで通常は十分。クライアントが自身の使用量をクエリする必要があるときだけ `usage:read-self` を足す)。スコープ絞り込みは、所有者ユーザーが降格された場合、キーも即座に所有者ができないことをできなくなることを意味する。
- **ユーザー単位のアクティブキー制限**。各ユーザーはアクティブ (未取り消しかつ未失効) なキーを 5 個までに制限される。上限に近づいたら再発行前に取り消すこと。
- **影響範囲**。漏洩したキーは所有者ユーザーのクレジットを消費するだけで、テナントのデフォルトクレジットとユーザー単位のオーバーライドで上限が決まる。監査ログ (`api_key_created`, `api_key_revoked`) が完全な発行と取り消しの履歴を保持する。
- **代理発行**。管理者は `POST /api/mvp/admin/users/{user_id}/api-keys` 経由で他ユーザーを代理してキーを発行できる。監査ログはアクターと `on_behalf_of` ユーザーの両方を記録する。[ADMIN_GUIDE.md -> API キー](ADMIN_GUIDE.md#api-keys) を参照。

---

## 内部動作 (興味がある人向け)

### バックエンドが API キーと Cognito トークンを区別する方法

`backend/mvp/deps.py` は入力ベアラトークンを検査し、プレフィックスで振り分ける。

- `sk-stratoclave-...` -> API キーパス: SHA-256 ハッシュ、DynamoDB `ApiKeys` 検索、取り消しと有効期限のチェック、`Users` から所有者のロード。
- それ以外 -> Cognito `access_token` パス: JWKS キー取得、`RS256` 検証、`token_use == "access"` アサーション、`client_id` クレームのチェック、`Users` 検索。

どちらの場合も、ハンドラは同じ `AuthenticatedUser` データクラスを生成する。API キー呼び出し元では `AuthenticatedUser.auth_kind = "api_key"` および `AuthenticatedUser.key_scopes` がキーのスコープから埋められる。認可ヘルパー `user_has_permission(user, permission)` が所有者のロールとキーのスコープの AND を強制する。

### `/v1/models` エンドポイント

Cowork は設定されたモデルリストが空のとき `GET /v1/models` をプローブする。エンドポイントは認証を要する (リストが公に列挙されないように)。Stratoclave が Bedrock 推論プロファイルに翻訳できるモデル ID の集合を、Anthropic Models API 互換の形で返す。

### なぜゲートウェイのベース URL に `/v1` を含めてはならないか

Cowork は最終 URL を構築するときに `/v1` プレフィックスをハードコードする。これは Anthropic の API 面をミラーしている。ベース URL はオリジンで、バージョンはパスにある。ベース URL に `/v1` を含めると、すべてのリクエストが `/v1/v1/...` になり、`/v1/messages`, `/v1/models`, `/api/*`, `/.well-known/*` しかルーティングされないバックエンドが `404` を返す。

### 監査ログに記録されるもの

各発行で `api_key_created` がスコープ、ラベル、ターゲットユーザーとともに送出される。各取り消しで `api_key_revoked` がアクターと所有者とともに送出される。管理者起点の発行では `on_behalf_of` も記録されるので、ログからアクターを復元できる。
