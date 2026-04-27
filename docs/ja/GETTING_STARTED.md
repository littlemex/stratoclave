<!-- Last updated: 2026-04-27 -->
<!-- Applies to: Stratoclave main @ 48b9533 (or later) -->

# はじめに

Stratoclave は、Amazon Bedrock の前段に、テナント単位のクレジット予算とロールベースのアクセス制御を配置するセルフホスト型のゲートウェイである。本ガイドは、まっさらな手元マシンを持つ初めてのユーザーが、サインインし、最初の Claude 呼び出しを実行し、Web コンソールを開くまでを案内する。

自分の AWS アカウントに Stratoclave をデプロイするのであれば、まず [DEPLOYMENT.md](DEPLOYMENT.md) から始めること。以下の説明は、運用者が Stratoclave のデプロイメント URL を既に渡している前提で書かれている。

## 目次

- [前提条件](#前提条件)
- [1. CLI のインストール](#1-cli-のインストール)
- [2. CLI 設定のブートストラップ](#2-cli-設定のブートストラップ)
- [3. サインイン](#3-サインイン)
- [4. 最初の呼び出し](#4-最初の呼び出し)
- [5. Web コンソールを開く](#5-web-コンソールを開く)
- [6. リクエストごとに実際に起きていること](#6-リクエストごとに実際に起きていること)
- [7. 次に読むべき場所](#7-次に読むべき場所)
- [8. トラブルシューティング](#8-トラブルシューティング)

---

## 前提条件

- macOS, Linux, または WSL2
- CLI をソースからビルドする場合は Rust 1.75 以降。[`littlemex/stratoclave`](https://github.com/littlemex/stratoclave) の GitHub Releases ページにはまだビルド済みバイナリが公開されていない。公開されるまでは `cargo build --release` が公式のパスである。
- 管理者から渡された Stratoclave のデプロイメント URL。たとえば `https://<your-deployment>.cloudfront.net` (本ガイドでは具体例として用いるが、実際には自分のデプロイメント URL に置き換えること)。
- 以下のいずれかのサインイン方式:
  - 管理者がプロビジョニングしたメールアドレスと、`aws cognito-idp admin-set-user-password` で設定された一時パスワード、**または**
  - 自分の AWS アカウントが信頼された ID ソースとして登録されているデプロイメントの場合、`aws sso login` 済みの AWS プロファイル
- 任意: `stratoclave claude` を使う場合、`claude` バイナリ ([Claude Code](https://docs.claude.com/en/docs/claude-code/overview) から入手) を `PATH` に通しておく。

---

## 1. CLI のインストール

リポジトリをクローンし、`stratoclave` バイナリをビルドする。

```bash
git clone https://github.com/littlemex/stratoclave.git
cd stratoclave/cli
cargo build --release
```

バイナリは `target/release/stratoclave` に生成される。これを `PATH` に配置する。

```bash
# オプション A: 既に PATH に含まれるディレクトリにシンボリックリンクを張る
sudo ln -sf "$PWD/target/release/stratoclave" /usr/local/bin/stratoclave

# オプション B: シェル rc ファイルに alias を追加
echo "alias stratoclave='$PWD/target/release/stratoclave'" >> ~/.zshrc   # または ~/.bashrc
source ~/.zshrc

stratoclave --help
```

`auth`, `claude`, `usage`, `admin`, `team-lead`, `ui`, `api-key`, `setup` の各サブコマンドが列挙されたヘルプが表示されるはずである。

---

## 2. CLI 設定のブートストラップ

Stratoclave は単一コマンドのブートストラップを提供する。管理者から渡されたデプロイメント URL を `stratoclave setup` に与える。

```bash
stratoclave setup https://<your-deployment>.cloudfront.net   # あなたのデプロイメント URL
```

このコマンドは以下を行う。

1. デプロイメントから `/.well-known/stratoclave-config` (認証不要のディスカバリドキュメント) を取得する。
2. レスポンスのスキーマを検証する (`schema_version == "1"`)。
3. 正しい Cognito と API のフィールドを持つ `~/.stratoclave/config.toml` を書き出す。
4. `~/.stratoclave/` をモード `0700` で作成し、ファイルをモード `0600` で書き込む。

期待される出力:

```
[INFO] Fetching config from https://<your-deployment>.cloudfront.net/.well-known/stratoclave-config ...

Saved to /home/you/.stratoclave/config.toml
  api_endpoint      = https://<your-deployment>.cloudfront.net
  cognito.domain    = https://stratoclave.auth.us-east-1.amazoncognito.com
  cognito.region    = us-east-1
  cli.default_model = us.anthropic.claude-opus-4-7

Next steps:
  stratoclave auth login --email you@example.com
  # or
  stratoclave auth sso --profile your-sso-profile
```

> **注記**: サマリの行が `cli.default_model` となっているのは歴史的経緯による。実際の TOML ファイルは同じ値を `[defaults] model` 以下に格納している。値は同一で、表示ラベルだけが異なる。

便利なフラグ:

| フラグ | 用途 |
|------|---------|
| `--dry-run` | 生成された `config.toml` を書き込まずに標準出力へ出力する。レビュー用に便利。 |
| `--force`, `-f` | 既存の `config.toml` を非対話的に上書きする。元のファイルは `config.toml.bak.<epoch>` にリネームされる。 |

### `STRATOCLAVE_API_ENDPOINT` のエクスポート

いくつかのサブコマンド (`auth login`, `admin ...`, `team-lead ...`, `api-key ...`, `usage show`) は、API エンドポイントを `config.toml` の `[api]` セクションではなく、環境変数 `STRATOCLAVE_API_ENDPOINT` から読み込む。これが統一されるまでは、**シェル rc ファイルで `STRATOCLAVE_API_ENDPOINT` をエクスポート**して、すべてのサブコマンドが一貫して動作するようにすること。

```bash
export STRATOCLAVE_API_ENDPOINT="https://<your-deployment>.cloudfront.net"
echo 'export STRATOCLAVE_API_ENDPOINT="https://<your-deployment>.cloudfront.net"' >> ~/.zshrc
```

優先順位の完全なルールは [CLI_GUIDE.md](CLI_GUIDE.md#configuration-file) を参照。

---

## 3. サインイン

Stratoclave は 2 つのサインイン方式をサポートする。ほとんどのチームはどちらか一方を選び、それに統一している。

### オプション A: Cognito メールアドレスとパスワード

管理者に自分のメールアドレスと一時パスワードを依頼し、次を実行する。

```bash
stratoclave auth login --email you@example.com
# Password:  <一時パスワードを貼り付け>
# [INFO] Temporary password detected. Please set a new password.
# New password:  <新しいパスワードを入力>
# Confirm new password:  <もう一度同じパスワード>
# [OK] Logged in as you@example.com. Token saved to ~/.stratoclave/mvp_tokens.json
```

パスワードは端末内に入力するだけで、ブラウザは開かれない。初回ログイン時の Cognito `NEW_PASSWORD_REQUIRED` チャレンジは同一セッション内で処理される。生成されたトークンは `~/.stratoclave/mvp_tokens.json` にモード `0600` で保存される。

### オプション B: AWS SSO (パスワードレス)

AWS アカウントが信頼された ID ソースとして登録されていれば、AWS SSO セッションを直接 Stratoclave トークンに交換できる。Cognito パスワードは不要である。

```bash
aws sso login --profile your-sso-profile
aws sts get-caller-identity --profile your-sso-profile   # 動作確認

stratoclave auth sso --profile your-sso-profile
# [INFO] Loading AWS credentials (profile=your-sso-profile, region=us-east-1)...
# [INFO] Presenting identity to Stratoclave backend...
# [OK] Signed in via sso_user as you@example.com
```

内部的には、CLI が `sts:GetCallerIdentity` 呼び出しに署名し、署名済みヘッダをバックエンドへ転送し、見返りに Cognito アクセストークンを受け取る。長期 AWS クレデンシャルは手元マシンから外へ出ない。

よくある SSO 拒否パターン:

| エラーメッセージ | 原因と対処 |
|---------------|---------------|
| `AWS account ... is not a trusted account` | 管理者に自分の AWS アカウント ID を信頼された ID ソースに追加してもらう。 |
| `Role ... does not match the allowed patterns` | 管理者のロール許可リストが狭すぎる。`allowed_role_patterns` を広げてもらう。 |
| `EC2 Instance Profile login is not allowed` | インスタンスプロファイルはワークロード間で共有されるためデフォルトで拒否される。AWS SSO に切り替えるか、管理者にオプトインしてもらう。 |
| `is not pre-registered` | デプロイメントが invite-only モードで動いている。管理者に SSO 招待を依頼する。 |

### セッションの確認

```bash
stratoclave auth whoami
# email: you@example.com
# user_id: a4f824f8-b041-703d-3ec8-f15588b9c969
# org_id: default-org
# roles: user
# total_credit: 1000000
# credit_used: 42318
# remaining_credit: 957682
```

`roles` はカンマ区切りのリストである。複数のロールを持つユーザーは `roles: admin,team_lead` のように表示される。

---

## 4. 最初の呼び出し

有効なセッションがあれば、Claude に何でも質問できる。

```bash
stratoclave claude -- "Hello, who are you?"
```

舞台裏では、CLI が `claude` をサブプロセスとして起動し、次を注入する。

| 環境変数 | 値 |
|----------------------|-------|
| `ANTHROPIC_BASE_URL` | あなたの Stratoclave エンドポイント。 |
| `ANTHROPIC_API_KEY` | `~/.stratoclave/mvp_tokens.json` に現在入っている Cognito アクセストークン。 |
| `ANTHROPIC_MODEL` | デフォルトでは `us.anthropic.claude-opus-4-7`、`--model` が指定されていればその値。 |

呼び出しごとにモデルを上書きできる。

```bash
stratoclave claude --model claude-haiku-4-5 -- "Summarise the README in three bullets"
```

`--` セパレータの後ろに書いたフラグは `claude` にそのまま転送される。

```bash
stratoclave claude -- --print "List files"
```

### Anthropic SDK を直接使う

`base_url` をデプロイメントに向け、API key に Stratoclave が発行したトークンまたは長寿命 API キーを渡せば、Anthropic 互換の SDK はそのまま動く。Python の例:

```python
import json, os, pathlib
from anthropic import Anthropic

tokens = json.loads(pathlib.Path("~/.stratoclave/mvp_tokens.json").expanduser().read_text())

client = Anthropic(
    base_url=os.environ["STRATOCLAVE_API_ENDPOINT"] + "/v1",
    api_key=tokens["access_token"],
)

msg = client.messages.create(
    model="claude-opus-4-7",
    max_tokens=200,
    messages=[{"role": "user", "content": "Hello"}],
)
print(msg.content)
```

スクリプト、デーモン、Cowork 連携などの長寿命な用途では、Cognito トークンを使い回すのではなく API キーを発行すること。詳細は [`api-key create`](CLI_GUIDE.md#api-key) を参照。

---

## 5. Web コンソールを開く

Web コンソールでは、残りクレジット、テナント、ロール、使用履歴ビューが確認できる。次で開く。

```bash
stratoclave ui open
```

CLI が URL に `?token=<access_token>` を付与するため、ブラウザは既に認証済みの状態で表示される。

ダッシュボードからは次が行える。

- テナントの残りクレジットの確認
- **My Usage** にジャンプして、過去 7, 30, または 90 日のモデル別トークン消費を確認
- ロールが許せば Admin または Team Lead パネルへアクセス

ブラウザを開かずに URL を取得したい場合 (SSH 経由などで便利) は `stratoclave ui url` を使う。出力はシークレットとして扱うこと。

---

## 6. リクエストごとに実際に起きていること

1. 自分の CLI または SDK が `POST /v1/messages` を Stratoclave エンドポイントへ送る。
2. バックエンドが `Authorization: Bearer <token>` を Cognito JWKS で検証する、もしくは長寿命 API キーの SHA-256 ハッシュを検索する。
3. ロールベースのアクセス制御が、呼び出し元が `messages:send` を持つかをチェックする。
4. クレジットチェックが `credit_used + estimated_cost` を呼び出し元テナントの `total_credit` と比較する。予算を超えたリクエストは HTTP `402 credit_exhausted` で失敗する。
5. リクエストは Bedrock の推論プロファイル (例: `us.anthropic.claude-opus-4-7`) に変換され、Bedrock Converse へ転送される。
6. レスポンスは Anthropic 形式の Server-Sent Events としてストリームで返される。
7. トークン数が使用量ログに書き込まれ、テナントの `credit_used` が DynamoDB の条件付き更新でインクリメントされる。

すべてダッシュボードと `stratoclave usage show` から数秒後に確認できる。

---

## 7. 次に読むべき場所

- [CLI_GUIDE.md](CLI_GUIDE.md) -- すべての `stratoclave` サブコマンドの完全リファレンス
- [ADMIN_GUIDE.md](ADMIN_GUIDE.md) -- テナントの作成、ユーザーのプロビジョニング、クレジットの管理
- [DEPLOYMENT.md](DEPLOYMENT.md) -- 自分の Stratoclave デプロイメントを立ち上げる
- [ARCHITECTURE.md](ARCHITECTURE.md) -- 各要素がどう組み合わさっているか
- [COWORK_INTEGRATION.md](COWORK_INTEGRATION.md) -- Claude Desktop Cowork のゲートウェイとして Stratoclave を使う

---

## 8. トラブルシューティング

| 症状 | 対処 |
|---------|-----|
| `API endpoint not configured` | `STRATOCLAVE_API_ENDPOINT` を設定するか、`stratoclave setup <url>` を再実行して書き込み成功を確認する。[Export `STRATOCLAVE_API_ENDPOINT`](#export-stratoclave_api_endpoint) 参照。 |
| `stratoclave setup` が `/.well-known/stratoclave-config` で `HTTP 404` を返す | デプロイメントがディスカバリエンドポイントを持たない古いバージョンである。管理者にバックエンドのアップグレードを依頼する。 |
| `stratoclave setup` が `Could not reach ...` で失敗する | 管理者と URL を再確認する。VPN や社内プロキシが CloudFront をブロックする場合もある。 |
| `auth login` が HTTP 400 と `NotAuthorizedException` のようなエラーで失敗する | パスワードが間違っているか、一時パスワードが失効している。管理者に `aws cognito-idp admin-set-user-password --no-permanent` で再発行を依頼する。 |
| `auth login` が `USER_NOT_FOUND` で失敗する | メールアドレスがプロビジョニングされていない。管理者に `stratoclave admin user create` を実行してもらう。 |
| `auth sso` が拒否される | [セクション 3](#option-b-aws-sso-passwordless) の SSO エラー表を参照。 |
| リクエストが `401 Unauthorized` で失敗する | アクセストークンは 1 時間で失効する。`stratoclave auth login` または `stratoclave auth sso` を再実行する。 |
| リクエストが `402 Payment Required` / `credit_exhausted` で失敗する | テナントクレジットを使い切っている。管理者に `stratoclave admin user set-credit <user_id> --total N --reset-used` でクレジットを増やしてもらう。 |
| リクエストが `400 invalid_model` で失敗する | 要求したモデルがデプロイメントの許可リストにない。[CLI_GUIDE.md](CLI_GUIDE.md#supported-model-ids) のモデル表を参照し、必要なモデルがなければ管理者に相談する。 |
| リクエストが `422 max_tokens exceeds 32768` で失敗する | バックエンドはリクエストごとに `max_tokens` を 32768 に制限している。SDK 呼び出しの値を減らす。 |
| `ui open` が古いページを表示する | CloudFront のキャッシュが原因。`Cmd+Shift+R` / `Ctrl+Shift+R` でハードリロードする。 |
| `stratoclave claude` が `Failed to spawn claude` を返す | `claude` バイナリが `PATH` に無い。[公式ドキュメント](https://docs.claude.com/en/docs/claude-code/overview) から Claude Code をインストールする。 |

それでも解決しない場合は、[`littlemex/stratoclave`](https://github.com/littlemex/stratoclave/issues) に Issue を立てること。正確なコマンド、CLI のバージョン (`stratoclave --version`)、エラー出力全体を添える。

---

## アンインストール

CLI とローカル状態をすべて削除するには次を実行する。

```bash
rm -f /usr/local/bin/stratoclave
rm -rf ~/.stratoclave
# ソースからビルドしてディスクを取り戻したい場合:
rm -rf /path/to/your/clone/target
```

`rm -rf ~/.stratoclave` はトークンファイル (`mvp_tokens.json`) と `config.toml` を削除するため、次回実行時には再度 `stratoclave setup` が必要になる。サーバー側で Cognito セッションも無効化したい場合は、管理者に自分のメール宛に `aws cognito-idp admin-user-global-sign-out` を実行してもらう。

Stratoclave のデプロイメント全体を取り壊す (運用者専用) 場合は、[DEPLOYMENT.md](DEPLOYMENT.md#teardown) を参照。
