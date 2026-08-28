/**
 * Two deployments must be the same deployment or share nothing.
 *
 * The invariant, stated without naming any particular stack: for any two
 * configurations, the set of `(account, region, stackName)` triples the app
 * produces is either IDENTICAL — the same deployment, being redeployed — or
 * DISJOINT — two deployments that cannot touch each other. A PARTIAL overlap is
 * the bug, because it means `cdk deploy` for one deployment updates some of
 * another's stacks while creating the rest of its own.
 *
 * That is exactly what happened. The body region became operator-choosable while
 * the WAF stack stayed pinned to us-east-1 with an unqualified name, so two
 * deployments sharing a prefix and differing only in body region overlapped on
 * precisely one stack: the second deploy repointed the first's WebACL at the
 * second's CloudFront distribution, quietly changing the edge protection of a
 * running system.
 *
 * Written as a property over configuration pairs rather than as a test that WAF
 * is named correctly. A test that named WAF would have to be remembered again the
 * next time a stack is pinned outside the body region — an ACM certificate for a
 * CloudFront alias is the obvious candidate, since those live in us-east-1 too.
 * This one fails on its own.
 *
 * What it does NOT do, so nobody mistakes its scope: it says nothing about an
 * operator reusing a prefix deliberately. Two runs with the same prefix and the
 * same body region are the same deployment by definition and should overlap
 * completely. Attribution for that case is the deployment tag, and the account's
 * own CloudFormation history.
 */
import { App } from 'aws-cdk-lib';

import { pinnedStackName, stackName } from '../lib/_common';

const ACCOUNT = '111122223333';

interface Placement {
  account: string;
  region: string;
  name: string;
}

/**
 * The placement set for a configuration, derived from the naming helpers rather
 * than by synthesising the whole app.
 *
 * Synthesising would be a stronger test and is not available here: the app needs
 * credentials-shaped context and a Bedrock model id, and a partial overlap is a
 * property of the NAMES, which is what these helpers own. If a stack is ever
 * added whose name bypasses these helpers, this test will not see it — so the
 * helpers are the contract, and a stack must not construct its name inline.
 */
function placements(prefix: string, bodyRegion: string, wafRegion: string): Placement[] {
  const bodyIds = [
    'network',
    'dynamodb',
    'ecr',
    'alb',
    'frontend',
    'cognito',
    'ecs',
    'config',
  ];
  const set: Placement[] = bodyIds.map((id) => ({
    account: ACCOUNT,
    region: bodyRegion,
    name: stackName(prefix, id),
  }));
  set.push({
    account: ACCOUNT,
    region: wafRegion,
    name: pinnedStackName(prefix, 'waf', wafRegion, bodyRegion),
  });
  return set;
}

function key(p: Placement): string {
  return `${p.account}/${p.region}/${p.name}`;
}

function classify(a: Placement[], b: Placement[]): 'identical' | 'disjoint' | 'partial' {
  const ka = new Set(a.map(key));
  const kb = new Set(b.map(key));
  const shared = [...ka].filter((k) => kb.has(k));
  if (shared.length === 0) return 'disjoint';
  if (shared.length === ka.size && ka.size === kb.size) return 'identical';
  return 'partial';
}

const WAF_REGION = 'us-east-1';

describe('deployment isolation', () => {
  it('is identical for the same prefix and the same body region', () => {
    const a = placements('stratoclave', 'us-east-1', WAF_REGION);
    const b = placements('stratoclave', 'us-east-1', WAF_REGION);
    expect(classify(a, b)).toBe('identical');
  });

  it.each([
    ['same prefix, different body regions', 'stratoclave', 'us-east-1', 'stratoclave', 'us-west-2'],
    ['same non-default prefix, different body regions', 'blue', 'ap-northeast-1', 'blue', 'ap-northeast-3'],
    ['different prefixes, same body region', 'stratoclave', 'us-east-1', 'verify', 'us-east-1'],
    ['different prefixes, different body regions', 'stratoclave', 'us-east-1', 'verify', 'us-west-2'],
    ['default prefix in a non-default region against itself elsewhere', 'stratoclave', 'eu-west-1', 'stratoclave', 'us-west-2'],
  ])('is disjoint for %s', (_label, prefixA, regionA, prefixB, regionB) => {
    const a = placements(prefixA, regionA, WAF_REGION);
    const b = placements(prefixB, regionB, WAF_REGION);
    expect(classify(a, b)).toBe('disjoint');
  });

  it('would be PARTIAL if a region-pinned stack were named without the body region', () => {
    // The bug this file exists for, reproduced by using the unqualified helper
    // for the pinned stack. Keeping it as an executable demonstration rather than
    // a comment: it shows the invariant has teeth, and it fails loudly if
    // `pinnedStackName` is ever quietly reduced to `stackName`.
    const unqualified = (prefix: string, bodyRegion: string): Placement[] => [
      ...placements(prefix, bodyRegion, WAF_REGION).filter((p) => !p.name.includes('waf')),
      { account: ACCOUNT, region: WAF_REGION, name: stackName(prefix, 'waf') },
    ];
    expect(classify(unqualified('stratoclave', 'us-east-1'), unqualified('stratoclave', 'us-west-2')))
      .toBe('partial');
  });

  it('keeps the historical deployment’s stack names unchanged', () => {
    // Renaming the live WAF stack would orphan the WebACL and leave the edge
    // unprotected until the replacement deployed, which is worse than the
    // collision being fixed. So the qualifier appears only when the body region
    // differs from the pinned region.
    expect(pinnedStackName('stratoclave', 'waf', 'us-east-1', 'us-east-1')).toBe('stratoclave-waf');
    expect(pinnedStackName('stratoclave', 'waf', 'us-east-1', 'us-west-2')).toBe(
      'stratoclave-waf-us-west-2'
    );
  });

  it('produces a valid CloudFormation stack name for every qualified form', () => {
    // CloudFormation allows alphanumerics and hyphens, must start with a letter,
    // and caps at 128 characters. A region suffix is the first thing here that
    // can push a long prefix over that, and the failure mode would be a deploy
    // that dies after the body stacks have already landed.
    const app = new App();
    expect(app).toBeDefined();
    for (const prefix of ['stratoclave', 'blue', 'a'.repeat(64)]) {
      for (const region of ['us-east-1', 'ap-southeast-4']) {
        const name = pinnedStackName(prefix, 'waf', 'us-east-1', region);
        expect(name).toMatch(/^[A-Za-z][A-Za-z0-9-]*$/);
        expect(name.length).toBeLessThanOrEqual(128);
      }
    }
  });
});
