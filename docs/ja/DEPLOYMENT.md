<!-- Last updated: 2026-04-27 -->
<!-- Applies to: Stratoclave main @ 48b9533 (or later) -->

# デプロイメントガイド

本ガイドは、自分の AWS アカウントに Stratoclave をデプロイする手順を示す。対象は AWS CLI, AWS CDK v2, コンテナランタイムに慣れたプラットフォームエンジニアである。

既存の Stratoclave デプロイメントを利用するだけなら、本ガイドは不要である。代わりに [GETTING_STARTED.md](GETTING_STARTED.md) を参照。

---

## 目次

1. [前提条件](#前提条件)
2. [デプロイされるもの](#デプロイされるもの)
3. [クイックデプロイ](#クイックデプロイ)
4. [デプロイ後: 最初の管理者](#デプロイ後-最初の管理者)
5. [デプロイ後: コンテナイメージ](#デプロイ後-コンテナイメージ)
6. [リージョン制約](#リージョン制約)
7. [コスト見積もり](#コスト見積もり)
8. [更新と再デプロイ](#更新と再デプロイ)
9. [ローカル開発](#ローカル開発)
10. [バックエンド環境変数](#バックエンド環境変数)
11. [削除](#削除)
12. [トラブルシューティング](#トラブルシューティング)

---

## 前提条件

### AWS アカウント

- VPC、ECS サービス、IAM ロール、Cognito User Pool、DynamoDB テーブル、S3 バケット、CloudFront ディストリビューション、ALB を作成する権限を持つ AWS アカウント。最初のデプロイを通すには `AdministratorAccess` が最も簡単である。以降の更新では必要に応じて最小権限ロールに絞り込むとよい。
- サービスしたいすべてのモデル (Claude Opus / Sonnet / Haiku の推論プロファイル) について、**`us-east-1` で Amazon Bedrock のモデルアクセスが有効化されている**こと。Bedrock のアクセスはアカウント単位のオプトインである。Bedrock コンソールの **Model access** を参照。
- AWS CDK v2 を対象アカウントとリージョンでブートストラップ済みであること:

  ```bash
  npx cdk bootstrap aws://<ACCOUNT_ID>/us-east-1
  ```

### ローカルツール

| ツール              | 最低バージョン | 備考 |
| ----------------- | --------------- | ----- |
| AWS CLI           | v2.15+          | 対象アカウント向けに SSO またはクレデンシャルが設定されていること。 |
| Node.js           | 20 LTS          | CDK v2 を実行する。 |
| Python            | 3.11+           | バックエンドイメージをローカルでリビルドする場合のみ必要。 |
| Rust              | 1.75+           | `stratoclave` CLI をリビルドする場合のみ必要。 |
| Container runtime | finch または Docker | macOS では finch を推奨。Docker Desktop は任意の OS で動作する。 |
| jq                | 任意 | いくつかのヘルパースクリプトで使う。 |

検証:

```bash
aws --version
node --version
python3 --version
docker --version   # または: finch --version
```

### リポジトリ

```bash
git clone https://github.com/littlemex/stratoclave.git
cd stratoclave
```

---

## デプロイされるもの

`deploy-all.sh` が成功すると、依存関係順に **8 つの CloudFormation スタック**がプロビジョニングされる。スタック名の接頭辞は `STRATOCLAVE_PREFIX` (デフォルト: `stratoclave`) で制御される。以下の `<Prefix>` は PascalCase 形式である。

| # | スタック                 | リソース                                                             | 目的 |
| - | --------------------- | --------------------------------------------------------------------- | ------- |
| 1 | `<Prefix>NetworkStack`  | VPC, パブリックサブネット 2 つ, セキュリティグループ                                | パブリックサブネットのみのネットワーク (NAT なし、最小コスト)。 |
| 2 | `<Prefix>DynamodbStack` | 12 個の DynamoDB テーブル (`users`, `user-tenants`, `tenants`, `permissions`, `trusted-accounts`, ...) | すべての永続状態、`PAY_PER_REQUEST`。 |
| 3 | `<Prefix>EcrStack`      | プライベート ECR リポジトリ                                                | バックエンドのコンテナイメージを保持する。 |
| 4 | `<Prefix>AlbStack`      | インターネット向け ALB, ターゲットグループ, :80 の HTTP リスナー               | バックエンドのパブリックエントリポイント。 |
| 5 | `<Prefix>FrontendStack` | S3 バケット, CloudFront ディストリビューション, SPA フォールバック関数             | 静的 Web UI ホスティング。 |
| 6 | `<Prefix>CognitoStack`  | User Pool, app client, hosted-UI ドメイン                               | 認証。CloudFront ドメインをインポートしてコールバック URL を配線する。 |
| 7 | `<Prefix>EcsStack`      | ECS クラスター, Fargate サービス (1 タスク), タスクロール, タスク定義     | バックエンドランタイム。すべての環境変数がここに注入される。 |
| 8 | `<Prefix>ConfigStack`   | SSM Parameter Store のエントリ                                           | バックエンドとヘルパースクリプトが消費する静的ランタイム値。 |

アーカイブされたスタック (RDS, Redis, WAF, CodeBuild, Verified Permissions) は `iac/lib/_archived/` にあり、デフォルトでは**デプロイされない**。参考として残されている。

---

## クイックデプロイ

リポジトリルートから:

```bash
# 1. クレデンシャルと環境を設定する。
export AWS_PROFILE=your-profile
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
export CDK_DEFAULT_REGION=us-east-1
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export STRATOCLAVE_PREFIX=stratoclave    # short, lower-kebab-case

# 2. CDK 依存関係をインストールする (初回のみ)。
cd iac && npm install && cd ..

# 3. すべてのスタックをデプロイし、フロントエンドをビルドして CloudFront に公開する。
./iac/scripts/deploy-all.sh
```

このスクリプトは冪等である。部分失敗後に再実行すると、CloudFormation が中断した箇所から続行する。コールドスタートでフルデプロイには **15 ~ 20 分**かかり、CloudFront ディストリビューション作成が支配的である。

スクリプト完了時に次が表示される。

```
[SUCCESS] Deployment completed
  Frontend URL:  https://<your-deployment>.cloudfront.net          # example: your deployment URL
  ALB endpoint:  http://stratoclave-alb-xxxxxxxx.us-east-1.elb.amazonaws.com
  User Pool ID:  us-east-1_EXAMPLE
```

### オプション

| フラグ             | 効果 |
| ---------------- | ------ |
| `--dry-run`      | スタック順を表示して、デプロイせずに終了する。 |
| `--skip-build`   | CDK スタックだけをデプロイする。フロントエンドバンドルはリビルドも再アップロードもしない。 |

### Docker の代わりに finch を使う

ヘルパースクリプトは `docker` という名前を呼び出す。finch しかインストールされていない場合、`PATH` にシムを追加する。

```bash
mkdir -p /tmp/docker-shim
cat > /tmp/docker-shim/docker <<'EOF'
#!/usr/bin/env bash
exec finch "$@"
EOF
chmod +x /tmp/docker-shim/docker
export PATH="/tmp/docker-shim:$PATH"
```

その後、`./iac/scripts/deploy-all.sh` を再実行する。

---

## デプロイ後: 最初の管理者

`deploy-all.sh` はユーザーを作成**しない**。最初の管理者を鋳造するには次を行う。

```bash
# バックエンドが到達可能で、この一発のために ALLOW_ADMIN_CREATION が true である必要がある。
# CDK のデフォルトは 'false' である。最初のデプロイでは環境に設定し、その後戻す
# (下記の「ロックダウン」参照)。
export ALLOW_ADMIN_CREATION=true
./iac/scripts/deploy-all.sh        # 環境変数が ECS に届くように再デプロイする

./scripts/bootstrap-admin.sh --email admin@example.com
```

スクリプトは 3 つの冪等なステップを実行する。

1. `email_verified=true` の Cognito ユーザーを作成する (もしくは再利用する)。
2. 恒久パスワードを設定する (生成、または `--password` で渡したもの)。
3. `POST /api/mvp/admin/users` でバックエンドに DynamoDB の `admin` 行を書き込ませる。

デフォルトの `default-org` テナントと `admin` / `team_lead` / `user` のパーミッション行は、バックエンド起動時に自動的にシードされる (`backend/bootstrap/seed.py` 参照)。DynamoDB を事前に埋める必要は**ない**。

サンプル出力:

```
[INFO] Resolving deployment outputs in region us-east-1...
[INFO] User Pool : us-east-1_EXAMPLE
[INFO] API       : https://<your-deployment>.cloudfront.net
[INFO] Email     : admin@example.com
[STEP 1/3] Ensuring Cognito user exists
[OK]   Created Cognito user.
[STEP 2/3] Setting permanent password
[OK]   Password set (permanent).
[STEP 3/3] Granting admin role via backend
[OK]   Admin role granted.
============================================
 Bootstrap complete
============================================
  Email:     admin@example.com
  Password:  <generated-once, 20+ chars>
  Login URL: https://<your-deployment>.cloudfront.net
```

### ブートストラップ後のロックダウン

管理者がログインできるようになったら、**ブートストラップエンドポイントを無効化**すること。

1. CDK 実行環境で `ALLOW_ADMIN_CREATION` を unset する (または `false` に設定する)。
2. 再デプロイ:

   ```bash
   cd iac && npx cdk deploy <Prefix>EcsStack
   ```

3. タスクが反映したかを確認:

   ```bash
   aws ecs describe-task-definition \
     --task-definition <PREFIX>-backend \
     --query 'taskDefinition.containerDefinitions[0].environment[?name==`ALLOW_ADMIN_CREATION`]'
   ```

以降の管理者昇格はすべて、認証済み API を経由しなければならない。

---

## デプロイ後: コンテナイメージ

`deploy-all.sh` はインフラをデプロイするが、**バックエンドイメージはビルドしない**。ECS サービスは ECR に既にある `latest` タグで起動する。初回デプロイではリポジトリは空なので、イメージをプッシュするまで ECS タスクはヘルスチェックに失敗し続ける。

ビルドとプッシュ:

```bash
cd iac
./scripts/build-and-push.sh
```

動作:

1. `<Prefix>EcrStack` の出力から ECR リポジトリ URI を取得する。
2. ECR にログインする (`aws ecr get-login-password`)。
3. `docker build -t stratoclave-backend:latest backend/`。
4. `latest` とタイムスタンプ付きタグ (`YYYYMMDD-HHMMSS`) をプッシュする。
5. 完全なイメージ URI を表示する。

ECS サービスに新しいイメージを反映させる:

```bash
aws ecs update-service \
  --cluster <PREFIX>-cluster \
  --service <PREFIX>-backend \
  --force-new-deployment
```

---

## リージョン制約

Stratoclave は現時点で **`us-east-1`** のみを必要とする。これは `iac/bin/iac.ts` の冒頭で強制される。

```ts
if (cdkRegion !== 'us-east-1') {
  throw new Error('CDK_DEFAULT_REGION must be "us-east-1" for Stratoclave ...');
}
```

主な理由は、Cognito hosted UI、Bedrock モデル推論プロファイル、フロントエンド CloudFront ディストリビューションと Cognito User Pool 間のクロススタック参照が、すべて同一リージョンに存在する必要があるためである。追加リージョン (例: `ap-northeast-1`, `eu-west-1`) のサポートはロードマップにある。コントリビューション歓迎。

---

## コスト見積もり

`us-east-1` で低トラフィックの単一チームデプロイメントの代表的な月額費用。単位は USD で、**Bedrock のトークン使用料は含まない** (AWS が別途請求する)。

| 項目                                              | 月額 |
| ------------------------------------------------- | ------- |
| ECS Fargate (0.25 vCPU / 0.5 GiB, 1 タスク)         | ~$12 |
| Application Load Balancer                         | ~$17 |
| CloudFront (~1 GiB egress, 最初の 10 GB は無料)      | <$1 |
| DynamoDB (PAY_PER_REQUEST, 小規模な書き込み量)    | ~$1 |
| S3 + CloudWatch Logs + SSM Parameter Store        | ~$1 |
| **小計**                                      | **~$30 ~ $35** |

実際には Bedrock のリクエストごとのコストが支配的である。Web UI の Admin Usage ページ (モデル別トークン数) と AWS Cost Explorer の **Bedrock** サービスフィルタで追跡すること。

---

## 更新と再デプロイ

CDK スタックは自由に再適用できるように設計されている。典型的な更新フロー:

| 変更                                           | コマンド |
| ------------------------------------------------ | ------- |
| フロントエンドコードのみ                               | `./iac/scripts/deploy-all.sh` (フロントエンドのリビルドもパイプラインの一部)。 |
| バックエンドコードのみ                                | `./iac/scripts/build-and-push.sh` の後、`aws ecs update-service --force-new-deployment`。 |
| IaC の変更 (新しい DynamoDB テーブル、環境変数など)    | `cd iac && npx cdk diff --all` の後、`./scripts/deploy-all.sh`。 |
| 特定のスタック                                 | `cd iac && npx cdk deploy <Prefix>EcsStack` (または別のスタック名)。 |

> **警告: UserPoolClient の置換**: `npx cdk diff` が Cognito アプリクライアントの更新ではなく**置換**を示したら、停止すること。置換はクライアント ID をローテートし、全アクティブユーザーセッションを即座に無効化する。最も一般的な原因は `callbackUrls` の並べ替えである。元の順序に戻し、デプロイ前に `diff` を再実行すること。

### 非推奨のヘルパースクリプト

`iac/scripts/` 内の以下のスクリプトは過去のイテレーションの残り物であり、サポートされるフローの一部ではない。将来のリリースで削除される予定である。新しいドキュメントやランブックに追加しないこと。

- `validate-config.sh`
- `deploy-with-update.sh`
- `cloud-build.sh`

インフラ + フロントエンドには `iac/scripts/deploy-all.sh`、バックエンドイメージには `iac/scripts/build-and-push.sh` を排他的に使うこと。

---

## ローカル開発

> **注記**: Stratoclave は完結したローカル開発スタックを同梱していない。Cognito, DynamoDB, Bedrock, CloudFront にはオフライン等価物がない。フロントエンドやバックエンドを反復開発するサポートされたワークフローは、ローカルプロセスを AWS 上の実デプロイメントに向けることである。`./iac/scripts/deploy-all.sh` で一度デプロイし、実稼働のバッキングサービスに対して開発する。

### フロントエンド

```bash
cd frontend
npm ci
npm run dev
# Opens http://localhost:3003
```

Vite の開発サーバーは `/api/*` と `/v1/*` をデプロイ済みの ALB にプロキシするので、UI コードを反復するのにローカルバックエンドは**不要**である。Cognito コールバック URL `http://localhost:3003/callback` は `iac/lib/cognito-stack.ts` で事前登録済みなので、hosted UI 経由のログインは開発サーバーに透過的にリダイレクトする。

### バックエンド

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

ENVIRONMENT=development \
AWS_REGION=us-east-1 \
COGNITO_USER_POOL_ID=$(aws cloudformation describe-stacks --stack-name <Prefix>CognitoStack --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' --output text) \
COGNITO_CLIENT_ID=$(aws cloudformation describe-stacks --stack-name <Prefix>CognitoStack --query 'Stacks[0].Outputs[?OutputKey==`UserPoolClientId`].OutputValue' --output text) \
OIDC_ISSUER_URL=$(aws cloudformation describe-stacks --stack-name <Prefix>CognitoStack --query 'Stacks[0].Outputs[?OutputKey==`OidcIssuerUrl`].OutputValue' --output text) \
OIDC_AUDIENCE=$COGNITO_CLIENT_ID \
DYNAMODB_USERS_TABLE=<PREFIX>-users \
DYNAMODB_USER_TENANTS_TABLE=<PREFIX>-user-tenants \
DYNAMODB_USAGE_LOGS_TABLE=<PREFIX>-usage-logs \
DYNAMODB_TENANTS_TABLE=<PREFIX>-tenants \
DYNAMODB_PERMISSIONS_TABLE=<PREFIX>-permissions \
DYNAMODB_TRUSTED_ACCOUNTS_TABLE=<PREFIX>-trusted-accounts \
DYNAMODB_SSO_PRE_REGISTRATIONS_TABLE=<PREFIX>-sso-pre-registrations \
DYNAMODB_API_KEYS_TABLE=<PREFIX>-api-keys \
CORS_ORIGINS=http://localhost:3003 \
uvicorn main:app --reload --port 8000
```

その後、`frontend/vite.config.ts` で Vite プロキシを `http://localhost:8000` に向け、AWS リソースに対してフルスタックを動かす。

### CLI

```bash
cd cli
cargo build --release
./target/release/stratoclave --help
```

フルの使い方は [CLI_GUIDE.md](CLI_GUIDE.md) を参照。

---

## バックエンド環境変数

デプロイ時に `iac/bin/iac.ts` が自動注入する。ローカルで再現したり、値欠落のバグを診断するためにここに列挙する。

| 変数                                 | 本番環境で必須? | CDK が設定? | 備考 |
| ---------------------------------------- | ----------------------- | ----------- | ----- |
| `ENVIRONMENT`                            | はい                     | はい (`production`) | |
| `COGNITO_USER_POOL_ID`                   | はい                     | はい         | |
| `COGNITO_CLIENT_ID`                      | はい                     | はい         | |
| `COGNITO_DOMAIN`                         | はい                     | はい         | hosted-UI のフル URL。 |
| `COGNITO_REGION`                         | はい                     | はい         | |
| `OIDC_ISSUER_URL`                        | はい                     | はい         | |
| `OIDC_AUDIENCE`                          | はい                     | はい (クライアント ID と同値) | |
| `BEDROCK_REGION`                         | はい                     | はい         | |
| `DEFAULT_BEDROCK_MODEL`                  | はい                     | はい         | |
| `STRATOCLAVE_API_ENDPOINT`               | はい                     | はい         | `/.well-known/stratoclave-config` に公開される。 |
| `STRATOCLAVE_PREFIX`                     | はい                     | はい (`stratoclave`) | DynamoDB, Secrets, SSM キーの接頭辞。 |
| `DYNAMODB_USERS_TABLE`                   | はい                     | はい         | |
| `DYNAMODB_USER_TENANTS_TABLE`            | はい                     | はい         | |
| `DYNAMODB_USAGE_LOGS_TABLE`              | はい                     | はい         | |
| `DYNAMODB_TENANTS_TABLE`                 | はい                     | はい         | |
| `DYNAMODB_PERMISSIONS_TABLE`             | はい                     | はい         | |
| `DYNAMODB_TRUSTED_ACCOUNTS_TABLE`        | はい                     | はい         | |
| `DYNAMODB_SSO_PRE_REGISTRATIONS_TABLE`   | はい                     | はい         | |
| `DYNAMODB_API_KEYS_TABLE`                | はい                     | はい         | |
| `CORS_ORIGINS`                           | はい (`localhost` 不可)    | はい         | |
| `STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL`      | 任意                | いいえ          | 設定すると、管理者が存在しない初回起動時にバックエンドがこのメールアドレスを admin として自動プロビジョニングする。冪等。 |
| `ALLOW_ADMIN_CREATION`                   | ブートストラップ時のみ          | はい (デフォルト `false`) | [ブートストラップ後のロックダウン](#ブートストラップ後のロックダウン) 参照。 |
| `EXPOSE_TEMPORARY_PASSWORD`              | 任意                | いいえ          | `true` にすると `admin user create` がレスポンスに一時パスワードを含める。デフォルト `false` (レスポンスフィールドは `null`)。本番環境では非推奨。 |

バックエンドが起動しない、かつ CloudWatch ログが `environment variable X is required` と言ったら、タスク定義の `environment` 配列を本リストと比較すること。

---

## 削除

```bash
cd iac
npx cdk destroy --all --profile "$AWS_PROFILE"
```

> **破壊的**: `cdk destroy --all` は対象アカウント内のすべての Stratoclave CloudFormation スタックを削除する。Stratoclave のすべての DynamoDB テーブルが削除され、すべてのユーザー、テナント、API キー、使用量ログが失われる。本当にデプロイメントを消したい場合のみ実行すること。

`cdk destroy` 後に**残り**、完全に空のアカウントにしたい場合に手動クリーンアップが必要なもの:

- **CloudWatch Logs** グループ (保持期間は設定されるがスタックと一緒には削除されない)。
- `RemovalPolicy.RETAIN` でマークされた **S3 バケット**。フロントエンドのバケットは `DESTROY` である。カスタマイズしている場合はスタックを確認すること。
- **Cognito ドメインプレフィックス**は削除後 **24 時間**使えない。同じ `STRATOCLAVE_PREFIX` ですぐに再デプロイすると失敗する。1 日待つか、新しいプレフィックスを選ぶ。
- リポジトリ内の **ECR イメージ**。スタックはデフォルトでリポジトリを削除するが、それを無効にしていた場合はイメージを先に削除すること。

クライアント側の CLI もアンインストールするには、[GETTING_STARTED.md -> アンインストール](GETTING_STARTED.md#uninstall) を参照。

---

## トラブルシューティング

### `CDK_DEFAULT_REGION must be "us-east-1"`

3 つのリージョン変数のいずれかをエクスポートし忘れたか、`~/.aws/config` のデフォルトがそれらを上書きしている。3 つすべてをエクスポートする。

```bash
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
export CDK_DEFAULT_REGION=us-east-1
```

### 起動時のバックエンドログに `[seed_bootstrap_failed]`

初回起動では通常無害である。バックエンドが `default-org` とパーミッション行をシードしようとしたとき、DynamoDB テーブルがまだ完全には `ACTIVE` になっていない。シードは冪等で、再起動のたびにリトライする。次のタスクでは成功するはずである。何度再起動してもこのメッセージが続くなら、タスクロールが `permissions` テーブルと `tenants` テーブルに対して `dynamodb:PutItem`, `dynamodb:GetItem`, `dynamodb:Query` を持っていることを確認すること。

### ECS タスクが再起動し続ける (`STOPPED (Task failed ELB health checks)`)

1. CloudWatch Logs の `/ecs/<prefix>-backend` で起動時の Python スタックトレースを確認する。
2. `curl http://<YOUR_ALB_DNS>/health` が `{"status": "healthy"}` を返すはず。502 になるなら、タスクがポート 8000 に bind できていない。
3. タスクロールが Bedrock を呼び出せるか (`bedrock:InvokeModel`) を確認する。パーミッション欠落はモデル呼び出しで 500 になるが、起動時には出ない。

### ALB が 503 `Service Unavailable` を返す

ヘルシーなターゲットが無い。ECS タスクがまだ起動していない (`build-and-push.sh` の後 2 ~ 3 分待つ) か、ヘルスチェックが失敗している。前項参照。

### フロントエンドが赤い "Configuration Error: config.json missing" 画面を表示する

S3 バケットに `/config.json` が無いか、CloudFront が古いバンドルを参照する古いキャッシュ済み `index.html` を返している。再実行する:

```bash
./iac/scripts/deploy-all.sh        # スタック出力から dist/config.json を再ビルドする
aws cloudfront create-invalidation \
  --distribution-id <YOUR_DISTRIBUTION_ID> \
  --paths '/*'
```

### `stratoclave setup <url>` が 404 を返す

URL が `/.well-known/stratoclave-config` エンドポイントより古いバックエンドを指している。[`littlemex/stratoclave`](https://github.com/littlemex/stratoclave) から最新の `main` を取り、バックエンドイメージをリビルド & プッシュし、ECS の新デプロイを強制する。

### `bootstrap-admin.sh` がバックエンドから `401 / 403` を返す

実行中のタスク定義で `ALLOW_ADMIN_CREATION` が `true` になっていない。`ALLOW_ADMIN_CREATION=true` をエクスポートし、ECS スタックを再デプロイし、新タスクが `RUNNING` になるのを待ってから `bootstrap-admin.sh` を再実行する。その後フラグを無効に戻すこと ([ブートストラップ後のロックダウン](#ブートストラップ後のロックダウン) 参照)。

### `bootstrap-admin.sh` が `UsernameExistsException` で失敗する

そのメールアドレスの Cognito ユーザーが既に存在する。スクリプトは step 1 でこれをソフト成功として扱い、続行するが、生のエラーが見える場合は別のコードパスから来ている。古いユーザーを削除して再試行する。

```bash
aws cognito-idp admin-delete-user \
  --user-pool-id <YOUR_USER_POOL_ID> \
  --username 'admin@example.com' \
  --region us-east-1
```

### CDK diff が UserPoolClient の置換を提案する

[更新と再デプロイ](#更新と再デプロイ) の警告を参照。diff が *update* で *replacement* でなくなるまでデプロイしないこと。

### Cognito ドメインプレフィックスが既に存在する

他の AWS ユーザーが既にそのプレフィックスを使っているか、最近同じプレフィックスで Stratoclave デプロイメントを破棄したばかりである。24 時間待つか、新しい `STRATOCLAVE_PREFIX` を選ぶ。

---

## 関連ドキュメント

- [GETTING_STARTED.md](GETTING_STARTED.md) -- エンドユーザーのオンボーディング。
- [ADMIN_GUIDE.md](ADMIN_GUIDE.md) -- 稼働中のデプロイメントの管理。
- [ARCHITECTURE.md](ARCHITECTURE.md) -- スタック内部と設計理由。
- [CONTRIBUTING.md](../CONTRIBUTING.md) -- 開発ワークフローと PR プロセス。
- [SECURITY.md](../SECURITY.md) -- 脆弱性の報告。
