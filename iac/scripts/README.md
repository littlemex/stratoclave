# Stratoclave IAC Scripts

This directory contains scripts for managing Stratoclave's infrastructure and
validating deployments.

## Script index

### validate-cognito-config.sh

Validates the Cognito User Pool Client configuration.

**Checks**:
1. Whether the CallbackURLs include the CloudFront URL
2. Whether the LogoutURLs include the CloudFront URL
3. Whether the User Pool Domain is correct
4. Whether `cognito.domain` in config.json matches the User Pool Domain
5. Whether `cognito.client_id` in config.json is valid
6. Whether the OAuth flows are configured correctly
7. Whether the OAuth scopes are configured correctly

**Usage**:

```bash
export USER_POOL_ID=us-east-1_XXXXXXXXX
export CLIENT_ID=<YOUR_CLIENT_ID>
export CLOUDFRONT_DOMAIN=<YOUR_DISTRIBUTION>.cloudfront.net
export AWS_REGION=us-east-1
export CONFIG_S3_URL=https://<YOUR_DISTRIBUTION>.cloudfront.net/config.json

./validate-cognito-config.sh
```

### post-deploy-validation.sh

Runs automatically after a CDK deploy to validate the overall configuration.

**Usage**:

```bash
# Automatically resolves values from the CloudFormation stacks and validates
./post-deploy-validation.sh
```

**Deployment flow**:

```bash
# 1. CDK deploy
cd <PROJECT_ROOT>/iac
npx cdk deploy --all

# 2. Build and deploy the frontend
aws codebuild start-build --project-name stratoclave-frontend-build

# 3. Post-deploy validation (required)
./scripts/post-deploy-validation.sh
```

## Troubleshooting

### Error: CloudFront callback URL is NOT registered

**Cause**: The CloudFront URL is not registered in the Cognito User Pool Client's
CallbackURLs.

**Fix**:

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

Or fix it in CDK:

```typescript
// iac/bin/iac.ts
const cloudFrontDomainName = 'xxxxxxxxxx.cloudfront.net';

const cognitoStack = new CognitoStack(app, 'StratoclaveCognitoStack', {
  cloudFrontDomainName, // set this value correctly
});
```

Then redeploy:

```bash
npx cdk deploy StratoclaveCognitoStack
```

### Error: config.json cognito domain mismatch

**Cause**: config.json still holds an old Cognito domain.

**Fix**:

```bash
# Rebuild the frontend to regenerate config.json
aws codebuild start-build --project-name stratoclave-frontend-build

# Once the build completes, validate again
./scripts/post-deploy-validation.sh
```

### Error: invalid_request (Cognito Hosted UI)

**Cause**: One of the following:
1. The current origin is not registered in CallbackURLs
2. The client_id is wrong
3. The Cognito domain is wrong

**Diagnosis**:

```bash
# 1. Inspect the current configuration
./scripts/validate-cognito-config.sh

# 2. Inspect config.json
curl https://<CLOUDFRONT_DOMAIN>/config.json | jq .

# 3. Inspect the Cognito User Pool Client
aws cognito-idp describe-user-pool-client \
  --user-pool-id <USER_POOL_ID> \
  --client-id <CLIENT_ID>
```

## CI/CD integration

A GitHub Actions workflow is provided:

```yaml
# .github/workflows/validate-cognito.yml

# Validates automatically on pull requests
# Validates after merges to the main branch
# Can also be triggered manually
```

**Manual trigger**:

GitHub Actions → "Validate Cognito Configuration" → "Run workflow"

## Best practices

1. **Always run the validation script after a deploy**
   ```bash
   npx cdk deploy --all && ./scripts/post-deploy-validation.sh
   ```

2. **Always update Cognito when the CloudFront domain changes**
   ```bash
   # Set the new domain in iac/bin/iac.ts
   # Redeploy the Cognito stack
   npx cdk deploy StratoclaveCognitoStack
   ```

3. **Keep config.json current**
   ```bash
   # Always rebuild the frontend after CDK changes
   aws codebuild start-build --project-name stratoclave-frontend-build
   ```

4. **Run E2E tests regularly**
   ```bash
   cd <PROJECT_ROOT>/frontend
   npm test -- tests/cognito-oauth-flow.spec.ts
   ```

## Related documentation

- [Cognito OAuth 2.0 Flow](https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html)
- [PKCE for Public Clients](https://tools.ietf.org/html/rfc7636)
- [AWS CDK Cognito Construct](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_cognito-readme.html)
