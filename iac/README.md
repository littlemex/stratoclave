# Stratoclave Infrastructure (AWS CDK v2)

This directory contains the AWS CDK v2 application that provisions the
entire Stratoclave deployment — VPC, DynamoDB tables, ECR, ALB, WAFv2,
CloudFront + S3 frontend, Cognito User Pool, and the Fargate service —
into a single AWS region in your own account.

> The Japanese translation of this document lives at
> [`../docs/ja/IAC.md`](../docs/ja/IAC.md).

## Architecture

```
Client → CloudFront (TLS, WAFv2) → ALB (internal) → ECS Fargate → Bedrock
                                     │
                                     ▼
                              Target Group (HTTP /health)
                                     │
                                     ▼
                             ECS Service (Fargate, Auto Scaling)
```

## Stacks

| Stack | Purpose | Key resources |
|-------|---------|---------------|
| **CognitoStack** | Authentication | Cognito User Pool, app client, hosted-UI domain |
| **NetworkStack** | Networking | VPC (with Flow Logs), subnets, security groups |
| **EcrStack** | Container registry | ECR repository with lifecycle policy |
| **AlbStack** | Load balancer | Internal ALB, target group, listener |
| **VerifiedPermissionsStack** | Authorization | AVP policy store (Cedar) |
| **EcsStack** | Container orchestration | ECS cluster, Fargate task definition, service |
| **CodeBuildStack** | Backend build | S3 source bucket, CodeBuild project, IAM role |
| **FrontendStack** | Frontend delivery | S3 + CloudFront (OAC) |
| **FrontendCodeBuildStack** | Frontend build | S3 source bucket, CodeBuild project, IAM role |
| **WafStack** | Edge protection | WAFv2 WebACL (CloudFront scope) |

## Prerequisites

- Node.js 20 LTS or later
- AWS CLI configured (`aws configure`)
- CDK CLI (`npm install -g aws-cdk`)
- **Docker is not required** — container images are built in the cloud
  via AWS CodeBuild

## Deployment

### 1. Configure environment

```bash
export AWS_REGION=us-east-1        # us-east-1 is the currently supported region
export AWS_PROFILE=your-profile    # the AWS CLI profile to deploy from
```

### 2. Bootstrap CDK (once per account/region)

```bash
cd iac
npx cdk bootstrap \
  aws://$(aws sts get-caller-identity --query Account --output text)/$AWS_REGION
```

### 3. Deploy every stack

```bash
./scripts/deploy.sh --all
```

A clean deploy takes roughly 5–10 minutes.

### 4. Build and push the backend container image (cloud build)

If Docker is not available locally, use CodeBuild:

```bash
cd iac
npx cdk deploy StratoclaveCodeBuildStack   # first-time only
./scripts/cloud-build.sh
```

Alternatively, when Docker is available locally:

```bash
./scripts/build-and-push.sh
```

### 5. Verify the deployment

```bash
ALB_DNS=$(aws cloudformation describe-stacks \
  --stack-name StratoclaveAlbStack \
  --query 'Stacks[0].Outputs[?OutputKey==`AlbDnsName`].OutputValue' \
  --output text \
  --region $AWS_REGION)

curl http://$ALB_DNS/health
# expected output: {"status":"healthy"}
```

## Targeted stack deploys

```bash
./scripts/deploy.sh --network
./scripts/deploy.sh --ecr
./scripts/deploy.sh --alb
./scripts/deploy.sh --ecs
```

## Tearing the deployment down

Destroy in reverse dependency order:

```bash
npx cdk destroy StratoclaveEcsStack
npx cdk destroy StratoclaveAlbStack
npx cdk destroy StratoclaveEcrStack
npx cdk destroy StratoclaveNetworkStack
```

## Troubleshooting

### ECS tasks refuse to start

Most commonly: the backend image is missing from ECR.

```bash
./scripts/build-and-push.sh
aws ecs update-service \
  --cluster stratoclave-cluster \
  --service stratoclave-backend \
  --force-new-deployment \
  --region $AWS_REGION
```

### ALB health checks fail

The `/health` endpoint on the ECS task is not responding. Inspect the
task logs and the security-group configuration:

```bash
aws logs tail /ecs/stratoclave-backend --follow --region $AWS_REGION

aws ec2 describe-security-groups \
  --filters "Name=tag:Stack,Values=Network" \
  --region $AWS_REGION
```

### Docker image build fails

Usually a dependency mismatch in `backend/requirements.txt`:

```bash
cd ../backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Cost estimate

| Resource | Monthly cost (approx.) |
|----------|-----------------------|
| ALB | $16–22 |
| ECS Fargate (0.25 vCPU, 0.5 GiB) | $9–15 |
| NAT Gateway | $32 |
| CloudWatch Logs | $5 |
| **Total** | **$62–74 / month** |

Estimate is for a minimal single-task deployment. Auto Scaling will
increase this under load.

## Cloud Build (AWS CodeBuild)

Stratoclave ships with a CodeBuild pipeline so that deployments work
without a local Docker toolchain.

### Flow

```
local → S3 (encrypted) → CodeBuild → ECR → ECS Fargate
```

### One-time setup

```bash
cd iac
npx cdk deploy StratoclaveCodeBuildStack
```

### Build and deploy (single command)

```bash
cd iac
./scripts/cloud-build.sh
```

The script:

1. Packages `backend/` into a `tar.gz`.
2. Uploads it to S3 (SSE-S3).
3. Starts a CodeBuild job that builds and pushes the image to ECR.
4. Triggers a rolling ECS deployment.

### Security

- **Least-privilege IAM**: CodeBuild is limited to ECR push and ECS
  update.
- **S3 encryption**: SSE-S3 with HTTPS enforcement.
- **Lifecycle**: source archives are deleted after 7 days.
- **Logging**: CloudWatch Logs, retained for 14 days.

### Cost

- CodeBuild: ~$0.80 / month (~30 builds)
- S3: < $0.01 / month

## Frontend build and deploy (CodeBuild + S3 + CloudFront)

The frontend is built by CodeBuild; environment variables are pulled
from CDK outputs so no manual `.env.production` file is needed.

### Flow

```
frontend source → S3 (source bucket) → CodeBuild
      ↓
  npm ci && npm run build   (env vars injected from CDK outputs)
      ↓
S3 (frontend bucket) → CloudFront → users
```

### One-time setup

```bash
cd iac
npx cdk deploy StratoclaveFrontendStack
npx cdk deploy StratoclaveFrontendCodeBuildStack
```

### Build and deploy (single command)

```bash
cd iac
./scripts/deploy-frontend.sh
```

The script:

1. Packages `frontend/` (excluding `node_modules` and `dist`) into a
   `tar.gz`.
2. Uploads it to the source S3 bucket.
3. Kicks off CodeBuild:
   - Generates `.env.production` from CDK outputs.
   - Runs `npm ci && npm run build`.
   - Syncs `dist/` to the frontend S3 bucket.
   - Invalidates the CloudFront distribution.

### Injected environment variables

CodeBuild injects the following variables from CDK outputs:

- `VITE_COGNITO_CLIENT_ID`
- `VITE_COGNITO_USER_POOL_ID`
- `VITE_COGNITO_DOMAIN`
- `VITE_API_ENDPOINT`
- `VITE_CLOUDFRONT_URL`

### Access the frontend

```bash
CLOUDFRONT_URL=$(aws cloudformation describe-stacks \
  --stack-name StratoclaveFrontendStack \
  --query 'Stacks[0].Outputs[?OutputKey==`FrontendUrl`].OutputValue' \
  --output text \
  --region $AWS_REGION)

echo "Frontend URL: $CLOUDFRONT_URL"
```

### Build logs

```bash
aws logs tail /codebuild/stratoclave-frontend --follow
```

### Cost

- CodeBuild: ~$0.50 / month (~20 builds)
- S3: < $0.01 / month
- CloudFront: $1–5 / month depending on traffic

## Next steps

- ACM certificate + HTTPS on a custom domain
- Route 53 DNS for the custom domain
- WAF rule tuning beyond the managed rule groups
- CloudWatch alarms on ECS service / ALB / Cognito
- Auto Scaling policy tuning
- Secrets Manager-backed rotation of API keys

## References

- [AWS CDK documentation](https://docs.aws.amazon.com/cdk/latest/guide/home.html)
- [ECS Fargate documentation](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/AWS_Fargate.html)
- [Application Load Balancer documentation](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html)
- [AWS CodeBuild documentation](https://docs.aws.amazon.com/codebuild/latest/userguide/welcome.html)
