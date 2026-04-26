# Stratoclave IAC Scripts

このディレクトリには、Stratoclave のインフラストラクチャ管理とデプロイ検証のためのスクリプトが含まれています。

## スクリプト一覧

### validate-cognito-config.sh

Cognito User Pool Client の設定を検証します。

**チェック項目**:
1. CallbackURLs に CloudFront URL が含まれているか
2. LogoutURLs に CloudFront URL が含まれているか
3. User Pool Domain が正しいか
4. config.json の cognito.domain と User Pool Domain が一致しているか
5. config.json の cognito.client_id が有効か
6. OAuth flows が正しく設定されているか
7. OAuth scopes が正しく設定されているか

**使用方法**:

```bash
export USER_POOL_ID=us-east-1_XXXXXXXXX
export CLIENT_ID=<YOUR_CLIENT_ID>
export CLOUDFRONT_DOMAIN=<YOUR_DISTRIBUTION>.cloudfront.net
export AWS_REGION=us-east-1
export CONFIG_S3_URL=https://<YOUR_DISTRIBUTION>.cloudfront.net/config.json

./validate-cognito-config.sh
```

### post-deploy-validation.sh

CDK デプロイ後に自動実行し、全体の設定を検証します。

**使用方法**:

```bash
# CloudFormation スタックから自動的に値を取得して検証
./post-deploy-validation.sh
```

**デプロイフロー**:

```bash
# 1. CDK デプロイ
cd <PROJECT_ROOT>/iac
npx cdk deploy --all

# 2. フロントエンドビルド・デプロイ
aws codebuild start-build --project-name stratoclave-frontend-build

# 3. デプロイ後の検証 (必須)
./scripts/post-deploy-validation.sh
```

## トラブルシューティング

### エラー: CloudFront callback URL is NOT registered

**原因**: Cognito User Pool Client の CallbackURLs に CloudFront URL が登録されていない。

**修正方法**:

```bash
aws cognito-idp update-user-pool-client \
  --user-pool-id <USER_POOL_ID> \
  --client-id <CLIENT_ID> \
  --callback-urls \
    "http://127.0.0.1:18080/callback" \
    "http://localhost:3003/callback" \
    "https://<CLOUDFRONT_DOMAIN>/callback" \
  --logout-urls \
    "http://127.0.0.1:18080" \
    "http://localhost:3003" \
    "https://<CLOUDFRONT_DOMAIN>" \
  --allowed-o-auth-flows "code" \
  --allowed-o-auth-scopes "openid" "email" "profile" \
  --allowed-o-auth-flows-user-pool-client \
  --supported-identity-providers "COGNITO"
```

または、CDK で修正：

```typescript
// iac/bin/iac.ts
const cloudFrontDomainName = 'xxxxxxxxxx.cloudfront.net';

const cognitoStack = new CognitoStack(app, 'StratoclaveCognitoStack', {
  cloudFrontDomainName, // この値を正しく設定
});
```

その後、再デプロイ：

```bash
npx cdk deploy StratoclaveCognitoStack
```

### エラー: config.json cognito domain mismatch

**原因**: config.json に古い Cognito ドメインが残っている。

**修正方法**:

```bash
# フロントエンドを再ビルドして config.json を再生成
aws codebuild start-build --project-name stratoclave-frontend-build

# ビルド完了後、再度検証
./scripts/post-deploy-validation.sh
```

### エラー: invalid_request (Cognito Hosted UI)

**原因**: 以下のいずれか：
1. CallbackURLs に現在のオリジンが登録されていない
2. client_id が間違っている
3. Cognito ドメインが間違っている

**診断方法**:

```bash
# 1. 現在の設定を確認
./scripts/validate-cognito-config.sh

# 2. config.json を確認
curl https://<CLOUDFRONT_DOMAIN>/config.json | jq .

# 3. Cognito User Pool Client を確認
aws cognito-idp describe-user-pool-client \
  --user-pool-id <USER_POOL_ID> \
  --client-id <CLIENT_ID>
```

## CI/CD 統合

GitHub Actions ワークフローが用意されています：

```yaml
# .github/workflows/validate-cognito.yml

# Pull Request 時に自動検証
# main ブランチへのマージ後に検証
# 手動トリガーも可能
```

**手動トリガー**:

GitHub Actions → "Validate Cognito Configuration" → "Run workflow"

## ベストプラクティス

1. **デプロイ後は必ず検証スクリプトを実行**
   ```bash
   npx cdk deploy --all && ./scripts/post-deploy-validation.sh
   ```

2. **CloudFront ドメインが変わった場合は必ず Cognito を更新**
   ```bash
   # 新しいドメインを iac/bin/iac.ts に設定
   # Cognito Stack を再デプロイ
   npx cdk deploy StratoclaveCognitoStack
   ```

3. **config.json は常に最新に保つ**
   ```bash
   # CDK 変更後は必ずフロントエンドを再ビルド
   aws codebuild start-build --project-name stratoclave-frontend-build
   ```

4. **E2E テストを定期的に実行**
   ```bash
   cd <PROJECT_ROOT>/frontend
   npm test -- tests/cognito-oauth-flow.spec.ts
   ```

## 関連ドキュメント

- [Cognito OAuth 2.0 Flow](https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html)
- [PKCE for Public Clients](https://tools.ietf.org/html/rfc7636)
- [AWS CDK Cognito Construct](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_cognito-readme.html)
