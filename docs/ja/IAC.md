# Stratoclave Infrastructure as Code (CDK)

Lambda Function URL から ALB + ECS Fargate への移行を実現する CDK プロジェクトです。

## アーキテクチャ

```
Client → ALB (Internet-facing) → ECS Fargate (Private Subnet) → Bedrock
         ↓
      Target Group
         ↓
    ECS Service (Auto Scaling)
```

## スタック構成

| スタック | 説明 | リソース |
|---------|------|---------|
| **CognitoStack** | 認証基盤 | Cognito User Pool、Client、Domain |
| **NetworkStack** | ネットワークインフラ | VPC、サブネット、セキュリティグループ |
| **EcrStack** | コンテナレジストリ | ECR リポジトリ |
| **AlbStack** | ロードバランサー | ALB、ターゲットグループ、リスナー |
| **RdsStack** | データベース | RDS PostgreSQL、Secrets Manager |
| **RedisStack** | キャッシュ | ElastiCache Redis |
| **VerifiedPermissionsStack** | 認可基盤 | AVP Policy Store（Cedar） |
| **EcsStack** | コンテナオーケストレーション | ECS クラスタ、Fargate タスク定義、サービス |
| **CodeBuildStack** | バックエンドビルド | S3、CodeBuild、IAM ロール |
| **FrontendStack** | フロントエンド配信 | S3、CloudFront、OAI |
| **FrontendCodeBuildStack** | フロントエンドビルド | S3、CodeBuild、IAM ロール |
| **WafStack** | WAF v2 | WebACL、ALB 関連付け |

## 前提条件

- Node.js 20.x 以上
- AWS CLI 設定済み（`aws configure` 実行済み）
- CDK CLI インストール済み（`npm install -g aws-cdk`）
- **Docker は不要**: CodeBuild でクラウドビルドを実行

## デプロイ手順

### 1. 環境変数の設定

```bash
export AWS_REGION=us-west-2  # お好みのリージョン
export AWS_PROFILE=default   # 使用する AWS プロファイル
```

### 2. CDK のブートストラップ（初回のみ）

```bash
cd iac
npx cdk bootstrap aws://$(aws sts get-caller-identity --query Account --output text)/$AWS_REGION
```

### 3. すべてのスタックをデプロイ

```bash
./scripts/deploy.sh --all
```

デプロイには約 5-10 分かかります。

### 4. Docker イメージのビルドと ECR プッシュ（クラウドビルド）

**推奨**: ローカルに Docker がない場合、CodeBuild でクラウドビルドを実行：

```bash
cd iac
npx cdk deploy StratoclaveCodeBuildStack  # 初回のみ
./scripts/cloud-build.sh
```

**または**: ローカルに Docker がある場合、従来の方法も使用可能：

```bash
./scripts/build-and-push.sh
```

### 6. デプロイの確認

ALB のエンドポイントを取得：

```bash
ALB_DNS=$(aws cloudformation describe-stacks \
  --stack-name StratoclaveAlbStack \
  --query 'Stacks[0].Outputs[?OutputKey==`AlbDnsName`].OutputValue' \
  --output text \
  --region $AWS_REGION)

echo "ALB Endpoint: http://$ALB_DNS"
```

Health check：

```bash
curl http://$ALB_DNS/health
# 期待される出力: {"status":"healthy"}
```

## スタック個別デプロイ

特定のスタックのみをデプロイする場合：

```bash
# Network スタックのみ
./scripts/deploy.sh --network

# ECR スタックのみ
./scripts/deploy.sh --ecr

# ALB スタックのみ
./scripts/deploy.sh --alb

# ECS スタックのみ
./scripts/deploy.sh --ecs
```

## スタック削除

**注意**: 削除は逆順で行う必要があります。

```bash
npx cdk destroy StratoclaveEcsStack
npx cdk destroy StratoclaveAlbStack
npx cdk destroy StratoclaveEcrStack
npx cdk destroy StratoclaveNetworkStack
```

## トラブルシューティング

### ECS タスクが起動しない

**原因**: Docker イメージが ECR にプッシュされていない

**解決策**:
```bash
./scripts/build-and-push.sh
aws ecs update-service --cluster stratoclave-cluster --service stratoclave-backend --force-new-deployment --region $AWS_REGION
```

### ALB のヘルスチェックが失敗する

**原因**: ECS タスクの `/health` エンドポイントが応答していない

**確認方法**:
```bash
# ECS タスクのログを確認
aws logs tail /ecs/stratoclave-backend --follow --region $AWS_REGION

# セキュリティグループを確認
aws ec2 describe-security-groups \
  --filters "Name=tag:Stack,Values=Network" \
  --region $AWS_REGION
```

### Docker イメージのビルドが失敗する

**原因**: backend/requirements.txt の依存関係エラー

**解決策**:
```bash
cd ../backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## コスト試算

| リソース | 月額コスト（概算） |
|---------|-----------------|
| ALB | $16-22 |
| ECS Fargate (0.25 vCPU, 0.5 GB) | $9-15 |
| NAT Gateway | $32 |
| CloudWatch Logs | $5 |
| **合計** | **$62-74/月** |

最小構成（1 タスク）での概算です。トラフィック増加時は Auto Scaling により追加コストが発生します。

## Lambda Function URL からの移行

### 移行手順

1. **新しい ALB + ECS 環境をデプロイ**（このドキュメントの手順に従う）
2. **並行稼働期間**:
   - 既存の Lambda Function URL はそのまま稼働
   - 新しい ALB エンドポイントで動作確認
3. **段階的切り替え**:
   - DNS または API Gateway で加重ルーティング
   - トラフィックを 10% → 50% → 100% と段階移行
4. **旧 Lambda 環境の削除**:
   - 1 週間程度様子を見てから Lambda を削除

### 認証の移行

Lambda Function URL の認証 `none` 制約は、ALB + ECS では以下で回避できます：

- **セキュリティグループ**: 特定 IP/CIDR からのみアクセス許可
- **ALB 認証**: Cognito または OIDC 認証を ALB レベルで追加
- **API Key 認証**: FastAPI ミドルウェア（既存）がそのまま動作

## Cloud Build（AWS CodeBuild）

ローカルに Docker がない環境でも、AWS CodeBuild でクラウド側でビルドできます。

### アーキテクチャ

```
ローカル → S3 (暗号化) → CodeBuild → ECR → ECS Fargate
```

### 初回セットアップ

```bash
cd iac
npx cdk deploy StratoclaveCodeBuildStack
```

### ビルドとデプロイ（ワンコマンド）

```bash
cd iac
./scripts/cloud-build.sh
```

このスクリプトは以下を自動実行します：

1. `backend/` ディレクトリを tar.gz に圧縮
2. S3 にアップロード（SSE-S3 暗号化）
3. CodeBuild でビルド開始
4. Docker イメージを ECR にプッシュ
5. ECS Service を自動更新

### セキュリティ

- **IAM 最小権限**: CodeBuild は ECR プッシュと ECS 更新のみ許可
- **S3 暗号化**: SSE-S3 + HTTPS 強制
- **ライフサイクル**: ソースアーカイブは 7 日で自動削除
- **ログ**: CloudWatch Logs で 14 日間保持

### コスト

- CodeBuild: 約 $0.80/月（30 ビルド想定）
- S3: $0.01 以下/月
- 合計: 約 $0.80/月（既存インフラの 1%）

## Frontend Build and Deploy（CodeBuild + S3 + CloudFront）

フロントエンドは CodeBuild で自動ビルドされ、環境変数が CDK Outputs から注入されます。

### アーキテクチャ

```
フロントエンドソース → S3 (Source Bucket) → CodeBuild
  ↓
  npm ci && npm run build (env vars from CDK)
  ↓
S3 (Frontend Bucket) → CloudFront → ユーザー
```

### 初回セットアップ

```bash
cd iac
npx cdk deploy StratoclaveFrontendStack
npx cdk deploy StratoclaveFrontendCodeBuildStack
```

### フロントエンドのデプロイ（ワンコマンド）

```bash
cd iac
./scripts/deploy-frontend.sh
```

このスクリプトは以下を自動実行します：

1. `frontend/` ディレクトリを tar.gz に圧縮（node_modules と dist は除外）
2. S3 Source Bucket にアップロード
3. CodeBuild でビルド開始：
   - `.env.production` を CDK Outputs から生成
   - `npm ci && npm run build`
   - `dist/` を S3 Frontend Bucket に同期
   - CloudFront キャッシュを無効化

### 環境変数の自動注入

CodeBuild は以下の環境変数を CDK Outputs から自動注入します：

- `VITE_COGNITO_CLIENT_ID`: Cognito クライアント ID
- `VITE_COGNITO_USER_POOL_ID`: Cognito ユーザープール ID
- `VITE_COGNITO_DOMAIN`: Cognito ホストド UI ドメイン
- `VITE_API_ENDPOINT`: ALB エンドポイント
- `VITE_CLOUDFRONT_URL`: CloudFront ディストリビューション URL

**重要**: 手動で `.env.production` を作成する必要はありません。すべて IaC で管理されます。

### フロントエンドへのアクセス

```bash
CLOUDFRONT_URL=$(aws cloudformation describe-stacks \
  --stack-name StratoclaveFrontendStack \
  --query 'Stacks[0].Outputs[?OutputKey==`FrontendUrl`].OutputValue' \
  --output text \
  --region $AWS_REGION)

echo "Frontend URL: $CLOUDFRONT_URL"
```

### ビルドログの確認

```bash
aws logs tail /codebuild/stratoclave-frontend --follow
```

### セキュリティ

- **S3 暗号化**: SSE-S3 + HTTPS 強制
- **ライフサイクル**: ソースアーカイブは 7 日で自動削除
- **ログ**: CloudWatch Logs で 14 日間保持
- **S3 パブリックアクセス**: BLOCK_ALL（CloudFront OAI 経由のみ）

### コスト

- CodeBuild: 約 $0.50/月（20 ビルド想定）
- S3: $0.01 以下/月
- CloudFront: $1-5/月（トラフィック依存）
- 合計: 約 $1.50-5.50/月

## 次のステップ

- ACM 証明書の作成と HTTPS 対応
- Route 53 での独自ドメイン設定
- WAF の追加（DDoS 対策）
- CloudWatch Alarms の設定
- Auto Scaling ポリシーの調整
- Secrets Manager での API Key 管理

## 参考リンク

- [AWS CDK ドキュメント](https://docs.aws.amazon.com/cdk/latest/guide/home.html)
- [ECS Fargate ドキュメント](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/AWS_Fargate.html)
- [ALB ドキュメント](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html)
- [CodeBuild ドキュメント](https://docs.aws.amazon.com/codebuild/latest/userguide/welcome.html)
