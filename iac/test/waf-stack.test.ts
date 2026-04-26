import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { WafStack } from '../lib/waf-stack';

describe('WafStack', () => {
  let app: cdk.App;
  let stack: WafStack;
  let template: Template;

  beforeAll(() => {
    app = new cdk.App();

    stack = new WafStack(app, 'TestWafStack', {
      env: { account: '123456789012', region: 'us-west-2' },
      albArn: 'arn:aws:elasticloadbalancing:us-west-2:123456789012:loadbalancer/app/test-alb/1234567890abcdef',
    });

    template = Template.fromStack(stack);
  });

  // WAF-01: Web ACL が REGIONAL スコープで作成されること (P0)
  test('Web ACL が REGIONAL スコープで作成されること', () => {
    template.hasResourceProperties('AWS::WAFv2::WebACL', {
      Scope: 'REGIONAL',
      DefaultAction: Match.anyValue(),
      Rules: Match.anyValue(),
    });
  });

  // WAF-02: AWSManagedRulesCommonRuleSet が含まれること (P0)
  test('AWSManagedRulesCommonRuleSet が含まれること', () => {
    template.hasResourceProperties('AWS::WAFv2::WebACL', {
      Rules: Match.arrayWith([
        Match.objectLike({
          Name: Match.stringLikeRegexp('[Cc]ommon'),
          Statement: {
            ManagedRuleGroupStatement: {
              VendorName: 'AWS',
              Name: 'AWSManagedRulesCommonRuleSet',
            },
          },
        }),
      ]),
    });
  });
});
