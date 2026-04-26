import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { CognitoStack } from '../lib/cognito-stack';

describe('CognitoStack', () => {
  let app: cdk.App;
  let stack: CognitoStack;
  let template: Template;

  beforeAll(() => {
    app = new cdk.App();
    stack = new CognitoStack(app, 'TestCognitoStack', {
      env: { account: '123456789012', region: 'us-east-1' },
      prefix: 'stratoclave',
      domainPrefix: 'test-stratoclave',
    });
    template = Template.fromStack(stack);
  });

  // COG-01: User Pool が作成されること (P0)
  test('User Pool が作成されること', () => {
    template.hasResourceProperties('AWS::Cognito::UserPool', {
      UserPoolName: 'stratoclave-user-pool',
      AdminCreateUserConfig: {
        AllowAdminCreateUserOnly: true, // selfSignUpEnabled: false
      },
    });
  });

  // COG-02: selfSignUpEnabled が false であること (P0)
  test('selfSignUpEnabled が false であること', () => {
    template.hasResourceProperties('AWS::Cognito::UserPool', {
      AdminCreateUserConfig: {
        AllowAdminCreateUserOnly: true,
      },
    });
  });

  // COG-03: パスワードポリシー (minLength=8, 全文字種必須) (P1)
  test('パスワードポリシーが正しく設定されていること', () => {
    template.hasResourceProperties('AWS::Cognito::UserPool', {
      Policies: {
        PasswordPolicy: {
          MinimumLength: 8,
          RequireLowercase: true,
          RequireUppercase: true,
          RequireNumbers: true,
          RequireSymbols: true,
        },
      },
    });
  });

  // COG-04: OAuth フロー (authorizationCodeGrant のみ) (P1)
  test('OAuth フローが Authorization Code Grant のみであること', () => {
    template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      AllowedOAuthFlows: ['code'],
      AllowedOAuthScopes: ['openid', 'email', 'profile'],
    });
  });

  // COG-05: CfnOutput が 4 つエクスポートされること (P2)
  test('CfnOutput が 4 つ以上エクスポートされること', () => {
    template.hasOutput('UserPoolId', {});
    template.hasOutput('UserPoolClientId', {});
    template.hasOutput('CognitoDomain', {});
    template.hasOutput('OidcIssuerUrl', {});
  });
});
