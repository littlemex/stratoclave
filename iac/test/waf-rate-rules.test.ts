import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { WafStack, WafStackProps } from '../lib/waf-stack';
import { impliedRatePer5Min } from '../lib/_common';

function synth(overrides: Partial<WafStackProps> = {}): Template {
  const app = new cdk.App();
  const stack = new WafStack(app, 'TestWaf', {
    env: { account: '123456789012', region: 'us-east-1' },
    prefix: 'stratoclave',
    ...overrides,
  });
  return Template.fromStack(stack);
}

function rateRules(t: Template): any[] {
  const acls = Object.values(t.findResources('AWS::WAFv2::WebACL'));
  const rules = (acls[0].Properties.Rules ?? []) as any[];
  return rules.filter((r) => r.Statement?.RateBasedStatement);
}

describe('WAF rate rules', () => {
  test('authenticated and unattributable traffic are counted separately', () => {
    // One ceiling for all traffic meant an aggregator in front of the gateway —
    // one IP carrying many users — was blocked at a rate the service served
    // comfortably. The two classes have different controls available, so they get
    // different ceilings.
    const rules = rateRules(synth());
    expect(rules).toHaveLength(2);
    for (const rule of rules) {
      expect(rule.Statement.RateBasedStatement.ScopeDownStatement).toBeDefined();
      expect(rule.Statement.RateBasedStatement.AggregateKeyType).toBe('IP');
    }
  });

  test('the authenticated ceiling is scoped to requests that carry a credential', () => {
    const rule = rateRules(synth()).find((r) => r.Name === 'RateLimitPerIp');
    const constraint =
      rule.Statement.RateBasedStatement.ScopeDownStatement.SizeConstraintStatement;
    expect(constraint.ComparisonOperator).toBe('GT');
    expect(constraint.Size).toBe(0);
    // CloudFormation's casing, not the construct's: `singleHeader` is typed `any`
    // and passed through, so camelCase here synthesises fine and fails at deploy.
    expect(constraint.FieldToMatch).toEqual({ SingleHeader: { Name: 'authorization' } });
  });

  test('the tight ceiling applies to requests with no usable credential', () => {
    // With no user to charge, the address is the only key, so this is the only
    // thing bounding that traffic.
    const rule = rateRules(synth()).find(
      (r) => r.Name === 'RateLimitPerIpUnauthenticated',
    );
    expect(rule.Statement.RateBasedStatement.Limit).toBe(300);
    expect(
      rule.Statement.RateBasedStatement.ScopeDownStatement.NotStatement,
    ).toBeDefined();
  });

  test('each ceiling is configurable on its own', () => {
    const rules = rateRules(
      synth({ rateLimitPer5Min: 500000, unauthenticatedRateLimitPer5Min: 150 }),
    );
    const byName = Object.fromEntries(rules.map((r) => [r.Name, r]));
    expect(byName.RateLimitPerIp.Statement.RateBasedStatement.Limit).toBe(500000);
    expect(
      byName.RateLimitPerIpUnauthenticated.Statement.RateBasedStatement.Limit,
    ).toBe(150);
  });
});

describe('impliedRatePer5Min', () => {
  test('a client at the target must not be rate-limited by its own success', () => {
    // 1024 in flight, each request no faster than the fastest p50 measured.
    expect(impliedRatePer5Min(1024)).toBe(614400);
  });

  test('a slower assumed request means a lower implied rate', () => {
    expect(impliedRatePer5Min(1024, 5)).toBe(61440);
  });

  test('the old 300 is far below what a target of 1024 implies', () => {
    // Which is why a sweep at the target was blocked with 403 while the service
    // was serving 60 req/s.
    expect(impliedRatePer5Min(1024)).toBeGreaterThan(300);
  });
});
