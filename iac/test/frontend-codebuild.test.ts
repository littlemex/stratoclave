/**
 * Frontend CodeBuild Stack Tests
 *
 * buildspec の内容を検証し、よくある問題を防ぐ
 */
import { App } from 'aws-cdk-lib'
import { Template } from 'aws-cdk-lib/assertions'
import { FrontendCodeBuildStack } from '../lib/frontend-codebuild-stack'
import * as s3 from 'aws-cdk-lib/aws-s3'

describe('Frontend CodeBuild Stack', () => {
  let app: App
  let template: Template

  beforeAll(() => {
    app = new App()

    // Mock dependencies
    const mockBucket = {
      bucketName: 'test-bucket',
      bucketArn: 'arn:aws:s3:::test-bucket',
      grantReadWrite: jest.fn(),
    } as any

    const stack = new FrontendCodeBuildStack(app, 'TestStack', {
      frontendBucket: mockBucket,
      cloudfrontDistributionId: 'TEST123',
      cognitoClientId: 'test-client-id',
      cognitoUserPoolId: 'us-east-1_TEST',
      cognitoDomain: 'https://test.auth.us-east-1.amazoncognito.com',
      albDnsName: 'test-alb.elb.amazonaws.com',
      cloudfrontDomainName: 'test.cloudfront.net',
    })

    template = Template.fromStack(stack)
  })

  test('buildspec should not contain cd frontend command', () => {
    // CodeBuild Project の BuildSpec を取得
    template.hasResourceProperties('AWS::CodeBuild::Project', {
      Source: {
        BuildSpec: (buildSpec: string) => {
          const spec = JSON.parse(buildSpec)

          // pre_build, build, post_build の全コマンドをチェック
          const allCommands = [
            ...(spec.phases.pre_build?.commands || []),
            ...(spec.phases.build?.commands || []),
            ...(spec.phases.post_build?.commands || []),
          ]

          // 'cd frontend' コマンドが存在しないことを確認
          const hasCdFrontend = allCommands.some((cmd: string) =>
            cmd.includes('cd frontend')
          )

          if (hasCdFrontend) {
            throw new Error(
              'buildspec contains "cd frontend" command. ' +
              'Source tarball is already extracted from frontend/ directory root.'
            )
          }

          return true
        },
      },
    })
  })

  test('buildspec should generate config.json in post_build', () => {
    template.hasResourceProperties('AWS::CodeBuild::Project', {
      Source: {
        BuildSpec: (buildSpec: string) => {
          const spec = JSON.parse(buildSpec)
          const postBuildCommands = spec.phases.post_build?.commands || []

          // config.json 生成コマンドが存在することを確認
          const hasConfigGeneration = postBuildCommands.some((cmd: string) =>
            cmd.includes('config.json')
          )

          if (!hasConfigGeneration) {
            throw new Error(
              'buildspec does not generate config.json in post_build phase'
            )
          }

          return true
        },
      },
    })
  })

  test('buildspec should deploy to S3 and invalidate CloudFront', () => {
    template.hasResourceProperties('AWS::CodeBuild::Project', {
      Source: {
        BuildSpec: (buildSpec: string) => {
          const spec = JSON.parse(buildSpec)
          const postBuildCommands = spec.phases.post_build?.commands || []

          // S3 sync コマンドが存在することを確認
          const hasS3Sync = postBuildCommands.some((cmd: string) =>
            cmd.includes('aws s3 sync')
          )

          // CloudFront invalidation コマンドが存在することを確認
          const hasCfInvalidation = postBuildCommands.some((cmd: string) =>
            cmd.includes('aws cloudfront create-invalidation')
          )

          if (!hasS3Sync || !hasCfInvalidation) {
            throw new Error(
              'buildspec missing S3 sync or CloudFront invalidation'
            )
          }

          return true
        },
      },
    })
  })

  test('buildspec should have correct environment variables', () => {
    template.hasResourceProperties('AWS::CodeBuild::Project', {
      Environment: {
        EnvironmentVariables: [
          { Name: 'FRONTEND_BUCKET', Type: 'PLAINTEXT' },
          { Name: 'CLOUDFRONT_DISTRIBUTION_ID', Type: 'PLAINTEXT' },
          { Name: 'COGNITO_CLIENT_ID', Type: 'PLAINTEXT' },
          { Name: 'COGNITO_USER_POOL_ID', Type: 'PLAINTEXT' },
          { Name: 'COGNITO_DOMAIN', Type: 'PLAINTEXT' },
          { Name: 'API_ENDPOINT', Type: 'PLAINTEXT' },
          { Name: 'CLOUDFRONT_URL', Type: 'PLAINTEXT' },
        ],
      },
    })
  })
})
