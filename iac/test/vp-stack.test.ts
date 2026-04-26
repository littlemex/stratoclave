import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { VerifiedPermissionsStack } from '../lib/vp-stack';

describe('VerifiedPermissionsStack', () => {
  let app: cdk.App;
  let stack: VerifiedPermissionsStack;
  let template: Template;

  beforeAll(() => {
    app = new cdk.App();

    stack = new VerifiedPermissionsStack(app, 'TestVpStack', {
      env: { account: '123456789012', region: 'us-west-2' },
      cognitoUserPoolArn: 'arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_XXXXXXXXX',
      cognitoClientId: '1234567890abcdefghijklmnop',
    });

    template = Template.fromStack(stack);
  });

  // VP-01: Policy Store が作成されること (P0)
  test('Policy Store が作成されること', () => {
    template.hasResourceProperties('AWS::VerifiedPermissions::PolicyStore', {
      ValidationSettings: Match.anyValue(),
      Schema: Match.anyValue(),
    });
  });

  // VP-02: Cognito Identity Source が設定されること (P0)
  test('Cognito Identity Source が設定されること', () => {
    template.hasResourceProperties('AWS::VerifiedPermissions::IdentitySource', {
      Configuration: {
        CognitoUserPoolConfiguration: {
          UserPoolArn: 'arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_XXXXXXXXX',
          ClientIds: ['1234567890abcdefghijklmnop'],
        },
      },
      PrincipalEntityType: Match.anyValue(),
    });
  });
});
