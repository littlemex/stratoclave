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

  // COG-01: User Pool is created (P0)
  test('User Pool is created', () => {
    template.hasResourceProperties('AWS::Cognito::UserPool', {
      UserPoolName: 'stratoclave-user-pool',
      AdminCreateUserConfig: {
        AllowAdminCreateUserOnly: true, // selfSignUpEnabled: false
      },
    });
  });

  // COG-02: selfSignUpEnabled is false (P0)
  test('selfSignUpEnabled is false', () => {
    template.hasResourceProperties('AWS::Cognito::UserPool', {
      AdminCreateUserConfig: {
        AllowAdminCreateUserOnly: true,
      },
    });
  });

  // COG-03: password policy (minLength=12 since 2026-06 hardening, full charset required) (P1)
  test('Password policy is configured correctly', () => {
    template.hasResourceProperties('AWS::Cognito::UserPool', {
      Policies: {
        PasswordPolicy: {
          MinimumLength: 12,
          RequireLowercase: true,
          RequireUppercase: true,
          RequireNumbers: true,
          RequireSymbols: true,
        },
      },
    });
  });

  // COG-04: OAuth flow (authorizationCodeGrant only) (P1)
  test('OAuth flow is Authorization Code Grant only', () => {
    template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      AllowedOAuthFlows: ['code'],
      AllowedOAuthScopes: ['openid', 'email', 'profile'],
    });
  });

  // COG-05: At least 4 CfnOutputs are exported (P2)
  test('At least 4 CfnOutputs are exported', () => {
    template.hasOutput('UserPoolId', {});
    template.hasOutput('UserPoolClientId', {});
    template.hasOutput('CognitoDomain', {});
    template.hasOutput('OidcIssuerUrl', {});
  });
});
