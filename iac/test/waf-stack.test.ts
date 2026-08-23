import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { WafStack } from '../lib/waf-stack';

/**
 * The group's real rule names, captured from `describe-managed-rule-group` (the
 * command and date are in the file). Checking the overrides against this, rather
 * than against a second hand-written list, is what catches a name AWS does not
 * have — it accepts such an override and ignores it, so nothing else would.
 */
const knownBadInputsRules: string[] = JSON.parse(
  readFileSync(join(__dirname, 'fixtures-known-bad-inputs.json'), 'utf8'),
).rules;

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

  // A blocked body-injection rule rejects ordinary agent traffic: a coding prompt
  // contains relative paths and HTML, and the caller only sees CloudFront's
  // generic 403 page. Measured on a live distribution: a body with
  // `../../etc/passwd` or `<script>` returned 403 while the same request with
  // plain text reached the application. The relaxation must stay confined to the
  // data plane, so pin both halves of that split.
  // The path is decoded and normalised before matching, so `/openai/../api/...`
  // cannot masquerade as data-plane traffic and skip CommonRuleSet.
  const dataPlaneMatch = (regex: string) =>
    Match.objectLike({
      RegexMatchStatement: Match.objectLike({
        FieldToMatch: { UriPath: {} },
        RegexString: regex,
        TextTransformations: [
          { Priority: 0, Type: 'URL_DECODE' },
          { Priority: 1, Type: 'NORMALIZE_PATH' },
        ],
      }),
    });

  test.each([
    ['AWSManagedRulesCommonRuleSet'],
    ['AWSManagedRulesKnownBadInputsRuleSet'],
  ])('%s is scoped OFF the data plane (browser traffic keeps full strength)', (ruleName) => {
    template.hasResourceProperties('AWS::WAFv2::WebACL', {
      Rules: Match.arrayWith([
        Match.objectLike({
          Name: ruleName,
          Statement: {
            ManagedRuleGroupStatement: Match.objectLike({
              ScopeDownStatement: {
                NotStatement: {
                  Statement: dataPlaneMatch('^(/v1/|/openai/)'),
                },
              },
            }),
          },
        }),
      ]),
    });
  });

  test('data plane keeps KnownBadInputs with body sub-rules counted', () => {
    template.hasResourceProperties('AWS::WAFv2::WebACL', {
      Rules: Match.arrayWith([
        Match.objectLike({
          Name: 'KnownBadInputsDataPlane',
          Statement: {
            ManagedRuleGroupStatement: Match.objectLike({
              Name: 'AWSManagedRulesKnownBadInputsRuleSet',
              ScopeDownStatement: dataPlaneMatch('^(/v1/|/openai/)'),
              RuleActionOverrides: Match.arrayWith([
                Match.objectLike({ Name: 'Log4JRCE_BODY', ActionToUse: { Count: {} } }),
                Match.objectLike({
                  Name: 'JavaDeserializationRCE_BODY',
                  ActionToUse: { Count: {} },
                }),
              ]),
            }),
          },
        }),
      ]),
    });
  });

  // AWS silently ignores an override that names a rule the group does not ship, so
  // a typo (`Log4JRFI_BODY` for `Log4JRCE_BODY`) leaves the data plane blocking
  // while every other assertion still passes. Check the names against a snapshot
  // of the group's real body rules rather than against themselves.
  test('every counted override names a rule the group actually ships', () => {
    const acl = Object.values(template.findResources('AWS::WAFv2::WebACL'))[0] as {
      Properties: { Rules: Array<Record<string, unknown>> };
    };
    const dataPlane = acl.Properties.Rules.find(
      (r) => (r as { Name?: string }).Name === 'KnownBadInputsDataPlane',
    ) as {
      Statement: { ManagedRuleGroupStatement: { RuleActionOverrides: Array<{ Name: string }> } };
    };
    const counted = dataPlane.Statement.ManagedRuleGroupStatement.RuleActionOverrides.map(
      (o) => o.Name,
    );
    expect(counted.length).toBeGreaterThan(0);
    for (const name of counted) {
      expect(knownBadInputsRules).toContain(name);
    }
  });

  // The relaxation is scoped to body inspection. URI / query / header / method
  // rules must keep blocking everywhere, so no override may name one.
  test('only body-scoped sub-rules are ever downgraded', () => {
    const acl = Object.values(template.findResources('AWS::WAFv2::WebACL'))[0] as {
      Properties: { Rules: Array<Record<string, unknown>> };
    };
    const overrides = acl.Properties.Rules.flatMap((rule) => {
      const stmt = rule.Statement as
        | { ManagedRuleGroupStatement?: { RuleActionOverrides?: Array<{ Name: string }> } }
        | undefined;
      return stmt?.ManagedRuleGroupStatement?.RuleActionOverrides ?? [];
    });
    expect(overrides.length).toBeGreaterThan(0);
    for (const o of overrides) {
      expect(o.Name.endsWith('_BODY')).toBe(true);
    }
  });

  // The fixture is a snapshot, so it can go stale when AWS revises the group.
  // Run this against the live API to find out (needs credentials):
  //   SC_WAF_LIVE_RULE_CHECK=1 npx jest test/waf-stack.test.ts
  const liveCheck = process.env.SC_WAF_LIVE_RULE_CHECK ? test : test.skip;
  liveCheck('the rule-name fixture still matches AWS', () => {
    const out = execFileSync(
      'aws',
      [
        'wafv2',
        'describe-managed-rule-group',
        '--vendor-name',
        'AWS',
        '--name',
        'AWSManagedRulesKnownBadInputsRuleSet',
        '--scope',
        'CLOUDFRONT',
        '--region',
        'us-east-1',
        '--query',
        'Rules[].Name',
        '--output',
        'json',
      ],
      { encoding: 'utf8' },
    );
    expect([...(JSON.parse(out) as string[])].sort()).toEqual(knownBadInputsRules);
  });

  test('a configured prefix set becomes one anchored regex', () => {
    const single = new WafStack(new cdk.App(), 'SinglePrefixWafStack', {
      env: { account: '123456789012', region: 'us-east-1' },
      prefix: 'stratoclave-test',
      dataPlanePathPrefixes: ['/v1/'],
    });
    Template.fromStack(single).hasResourceProperties('AWS::WAFv2::WebACL', {
      Rules: Match.arrayWith([
        Match.objectLike({
          Name: 'KnownBadInputsDataPlane',
          Statement: {
            ManagedRuleGroupStatement: Match.objectLike({
              ScopeDownStatement: dataPlaneMatch('^(/v1/)'),
            }),
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
