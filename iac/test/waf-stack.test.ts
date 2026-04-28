import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { WafStack } from '../lib/waf-stack';

describe('WafStack (P1-2, CloudFront scope)', () => {
  let app: cdk.App;
  let stack: WafStack;
  let template: Template;

  beforeAll(() => {
    app = new cdk.App();

    stack = new WafStack(app, 'TestWafStack', {
      env: { account: '123456789012', region: 'us-east-1' },
      prefix: 'stratoclave-test',
    });

    template = Template.fromStack(stack);
  });

  test('Web ACL is CLOUDFRONT-scoped', () => {
    template.hasResourceProperties('AWS::WAFv2::WebACL', {
      Scope: 'CLOUDFRONT',
      DefaultAction: { Allow: {} },
      Rules: Match.anyValue(),
    });
  });

  test('AWSManagedRulesCommonRuleSet is present', () => {
    template.hasResourceProperties('AWS::WAFv2::WebACL', {
      Rules: Match.arrayWith([
        Match.objectLike({
          Name: 'AWSManagedRulesCommonRuleSet',
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

  test('KnownBadInputs + IpReputation managed rules are present', () => {
    template.hasResourceProperties('AWS::WAFv2::WebACL', {
      Rules: Match.arrayWith([
        Match.objectLike({
          Statement: {
            ManagedRuleGroupStatement: {
              VendorName: 'AWS',
              Name: 'AWSManagedRulesKnownBadInputsRuleSet',
            },
          },
        }),
        Match.objectLike({
          Statement: {
            ManagedRuleGroupStatement: {
              VendorName: 'AWS',
              Name: 'AWSManagedRulesAmazonIpReputationList',
            },
          },
        }),
      ]),
    });
  });

  test('Rate-based rule uses per-IP aggregation with the configured limit', () => {
    template.hasResourceProperties('AWS::WAFv2::WebACL', {
      Rules: Match.arrayWith([
        Match.objectLike({
          Name: 'RateLimitPerIp',
          Action: { Block: {} },
          Statement: {
            RateBasedStatement: {
              AggregateKeyType: 'IP',
              Limit: 300,
            },
          },
        }),
      ]),
    });
  });

  test('WebACL ARN is exported to SSM for cross-stack wiring', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/stratoclave-test/waf/cloudfront-acl-arn',
      Type: 'String',
    });
  });
});
