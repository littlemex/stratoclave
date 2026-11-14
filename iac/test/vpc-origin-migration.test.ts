import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { Template } from 'aws-cdk-lib/assertions';
import { hermeticSynthEnv } from './_hermetic-synth';
import { checkExportTransition, isSafeTransition, type StackSet } from './_cross-stack-export-transition';

/**
 * `STRATOCLAVE_CLOUDFRONT_ORIGIN`'s three migration states
 * (`public` -> `vpc-expand` -> `vpc-switch` -> `vpc`), synthesized through
 * the real `bin/iac.ts` entrypoint under ONE shared prefix — the same
 * prefix an operator would reuse across the three real deploys.
 *
 * What this file proves, each backed by a real `cdk synth`, not just a
 * reading of the source:
 *
 *   1. The public ALB's logical ids (`Alb...`, `BackendTargetGroup...`,
 *      `HttpListener...`) are IDENTICAL between `public` and `vpc-expand` —
 *      the expand step only ADDS, it never touches the existing resources
 *      (a real `cdk deploy` would see these as unchanged, not updated).
 *   2. `vpc-expand` keeps BOTH ALBs/target groups and registers the ECS
 *      service with both target groups. `AWS::ECS::Service.LoadBalancers`
 *      is documented by AWS as "Update requires: No interruption" — this
 *      file does not re-verify that against a live account (no mutating
 *      AWS calls), but records the citation plainly instead of restating
 *      it as a bare assumption:
 *      https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-ecs-service.html
 *      (`LoadBalancers`, *Update requires*: No interruption).
 *   3. `vpc-expand` declares the VPC origin but no behaviour references it
 *      yet (`vpc-switch` is the one that flips `targetOriginId`).
 *   4. `vpc` (contract) removes the public ALB's resources and the
 *      `AlbDnsName` export entirely.
 *
 * Synth runs with ambient AWS credentials neutralised — see
 * `test/_hermetic-synth.ts`.
 */
describe('STRATOCLAVE_CLOUDFRONT_ORIGIN migration states', () => {
  const iacDir = path.resolve(__dirname, '..');
  const PREFIX = 'migrationtest';
  const REGION = 'us-east-1';

  function synth(
    cloudfrontOrigin: string,
  ): { outDir: string; raw: Record<string, Record<string, any>>; allStacks: StackSet } {
    const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'stratoclave-vpc-migration-'));
    const env = hermeticSynthEnv({
      CDK_DEFAULT_ACCOUNT: '123456789012',
      CDK_DEFAULT_REGION: REGION,
      STRATOCLAVE_REGION: REGION,
      STRATOCLAVE_PREFIX: PREFIX,
      CDK_NAG: 'off',
      STRATOCLAVE_CLOUDFRONT_ORIGIN: cloudfrontOrigin,
    });
    execFileSync('npx', ['cdk', 'synth', '--all', '-o', outDir, '--quiet'], {
      cwd: iacDir,
      env,
      encoding: 'utf-8',
      stdio: 'pipe',
    });
    const raw: Record<string, Record<string, any>> = {};
    for (const id of ['alb', 'frontend', 'ecs', 'network']) {
      raw[id] = JSON.parse(fs.readFileSync(path.join(outDir, `${PREFIX}-${id}.template.json`), 'utf-8'));
    }
    // The cross-stack export/import checker needs EVERY synthesized stack,
    // not just the four this file's other tests read directly — EcsStack
    // alone legitimately imports exports from DynamoDBStack, CognitoStack,
    // EcrStack and (when WAF is on) WafStack, none of which ever changes
    // shape across a `cloudfrontOrigin` transition. Restricting the
    // checker to only `raw`'s four stacks made every import INTO one of
    // them from an OUTSIDE stack look unresolved — a false positive this
    // file's own fix found had nothing to do with the migration design.
    const allStacks: StackSet = {};
    for (const file of fs.readdirSync(outDir)) {
      if (file.endsWith('.template.json')) {
        allStacks[file] = JSON.parse(fs.readFileSync(path.join(outDir, file), 'utf-8'));
      }
    }
    return { outDir, raw, allStacks };
  }

  function albLogicalIds(raw: Record<string, any>): string[] {
    return Object.keys(raw.Resources || {}).filter((id) =>
      /^(Alb|BackendTargetGroup|HttpListener)[A-F0-9]+$/.test(id),
    );
  }

  let publicOut: { outDir: string; raw: Record<string, Record<string, any>>; allStacks: StackSet };
  let expandOut: { outDir: string; raw: Record<string, Record<string, any>>; allStacks: StackSet };
  let switchOut: { outDir: string; raw: Record<string, Record<string, any>>; allStacks: StackSet };
  let contractOut: { outDir: string; raw: Record<string, Record<string, any>>; allStacks: StackSet };
  let vpcOut: { outDir: string; raw: Record<string, Record<string, any>>; allStacks: StackSet };

  beforeAll(() => {
    publicOut = synth('public');
    expandOut = synth('vpc-expand');
    switchOut = synth('vpc-switch');
    contractOut = synth('vpc-contract');
    vpcOut = synth('vpc');
  }, 240_000);

  afterAll(() => {
    for (const o of [publicOut, expandOut, switchOut, contractOut, vpcOut]) {
      if (o?.outDir) fs.rmSync(o.outDir, { recursive: true, force: true });
    }
  });

  // The actual deadlock detector round-1 implementation review's blocker
  // B1 asked for — see `./_cross-stack-export-transition.ts`'s own doc for
  // why it is checked against the OLD state's imports, not the NEW state's.
  // `cross-stack-export-transition.test.ts` proves the checker itself
  // catches the pre-vpc-contract shape of this bug with hand-made
  // templates; THIS describe block proves the actual, real `cdk synth`
  // output of every state this feature can reach — forward AND the
  // documented rollback direction — never hits it.
  describe('cross-stack export/import safety across every migration step, forward and rollback', () => {
    const FORWARD: Array<[string, () => { raw: Record<string, Record<string, any>> }]> = [
      ['public', () => publicOut],
      ['vpc-expand', () => expandOut],
      ['vpc-switch', () => switchOut],
      ['vpc-contract', () => contractOut],
      ['vpc', () => vpcOut],
    ];

    for (let i = 0; i < FORWARD.length - 1; i++) {
      const [fromName, fromOut] = FORWARD[i];
      const [toName, toOut] = FORWARD[i + 1];
      test(`forward: ${fromName} -> ${toName}`, () => {
        const problems = checkExportTransition(fromOut().allStacks, toOut().allStacks);
        expect(problems.unresolvedImports).toEqual([]);
        expect(problems.unsafeRemovals).toEqual([]);
        expect(isSafeTransition(problems)).toBe(true);
      });
      // Rollback is the SAME pair of states, checked in the OPPOSITE
      // direction — a real `cdk deploy --all` for the ROLLBACK step reads
      // the "to" state as OLD (currently deployed) and the "from" state as
      // NEW (what the rollback redeploys).
      test(`rollback: ${toName} -> ${fromName}`, () => {
        const problems = checkExportTransition(toOut().allStacks, fromOut().allStacks);
        expect(problems.unresolvedImports).toEqual([]);
        expect(problems.unsafeRemovals).toEqual([]);
        expect(isSafeTransition(problems)).toBe(true);
      });
    }

    // The one rollback step docs/DEPLOYMENT.md's "Rollback" section
    // documents as UNSAFE for a single `cdk deploy --all` -- vpc-expand's
    // consumers (FrontendStack's VPC origin, EcsStack's internal
    // target-group registration) still import the internal ALB's exports,
    // the same shape vpc-contract exists to solve on the way up. This test
    // is the inverse of every other one in this block: it PROVES the
    // checker still correctly flags the one transition this migration
    // design does NOT make safe in one step, rather than silently passing
    // everything and only being trusted on the strength of the OTHER
    // (safe) results above.
    test('rollback vpc-expand -> public in ONE deploy is correctly flagged UNSAFE (this is why docs/DEPLOYMENT.md documents a two-deploy `--exclusively` workaround instead)', () => {
      const problems = checkExportTransition(expandOut.allStacks, publicOut.allStacks);
      expect(isSafeTransition(problems)).toBe(false);
    });
  });

  test('public -> vpc-expand: the public ALB keeps the EXACT SAME logical ids (expand only adds)', () => {
    const publicIds = albLogicalIds(publicOut.raw.alb).sort();
    const expandIds = albLogicalIds(expandOut.raw.alb).sort();
    expect(expandIds).toEqual(expect.arrayContaining(publicIds));
    // And the properties of each shared id are untouched too, not merely
    // the id — a same-id, different-properties resource would still be an
    // "update", not the "unchanged" this step promises.
    for (const id of publicIds) {
      expect(expandOut.raw.alb.Resources[id]).toEqual(publicOut.raw.alb.Resources[id]);
    }
  });

  test('vpc-expand: BOTH ALBs/target groups exist, and the ECS service has two LoadBalancers entries', () => {
    const tpl = Template.fromJSON(expandOut.raw.alb);
    tpl.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 2);
    tpl.resourceCountIs('AWS::ElasticLoadBalancingV2::TargetGroup', 2);

    const ecsTpl = Template.fromJSON(expandOut.raw.ecs);
    const services = ecsTpl.findResources('AWS::ECS::Service');
    const svc = Object.values(services)[0] as any;
    expect(svc.Properties.LoadBalancers).toHaveLength(2);
  });

  test('vpc-expand: the VPC origin exists in the distribution but NOTHING targets it yet', () => {
    const dist = Object.values(expandOut.raw.frontend.Resources).find(
      (r: any) => r.Type === 'AWS::CloudFront::Distribution',
    ) as any;
    const origins = dist.Properties.DistributionConfig.Origins;
    const ids = origins.map((o: any) => o.Id);
    expect(ids).toEqual(expect.arrayContaining(['S3Origin', 'ALBOrigin', 'ALBVpcOrigin']));
    for (const b of dist.Properties.DistributionConfig.CacheBehaviors) {
      expect(b.TargetOriginId).toBe('ALBOrigin'); // still the OLD origin
    }
  });

  test('vpc-switch: the SAME two ALBs still exist, but every behaviour now targets the VPC origin', () => {
    const tpl = Template.fromJSON(switchOut.raw.alb);
    tpl.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 2);

    const switchIds = albLogicalIds(switchOut.raw.alb).sort();
    const expandIds = albLogicalIds(expandOut.raw.alb).sort();
    expect(switchIds).toEqual(expandIds); // nothing new, nothing removed

    const dist = Object.values(switchOut.raw.frontend.Resources).find(
      (r: any) => r.Type === 'AWS::CloudFront::Distribution',
    ) as any;
    for (const b of dist.Properties.DistributionConfig.CacheBehaviors) {
      expect(b.TargetOriginId).toBe('ALBVpcOrigin');
    }
    // ALBOrigin is still DECLARED (the one-line-revert safety net) even
    // though nothing references it any more.
    const ids = dist.Properties.DistributionConfig.Origins.map((o: any) => o.Id);
    expect(ids).toContain('ALBOrigin');
  });

  test('vpc-contract: the public ALB is STILL declared, but nothing imports it any more — the consumer-first step', () => {
    // AlbStack itself is UNCHANGED from vpc-switch: same logical ids, same
    // properties. This is the step's whole point (lib/entry-config.ts's
    // CloudfrontOriginMode doc) — only the CONSUMERS move.
    const switchIds = albLogicalIds(switchOut.raw.alb).sort();
    const contractIds = albLogicalIds(contractOut.raw.alb).sort();
    expect(contractIds).toEqual(switchIds);
    for (const id of switchIds) {
      expect(contractOut.raw.alb.Resources[id]).toEqual(switchOut.raw.alb.Resources[id]);
    }
    expect(Object.keys(contractOut.raw.alb.Outputs || {})).toContain('AlbDnsName');

    // FrontendStack stops declaring the public origin entirely (not merely
    // retargeting away from it, the way vpc-switch itself does to
    // ALBOrigin).
    const dist = Object.values(contractOut.raw.frontend.Resources).find(
      (r: any) => r.Type === 'AWS::CloudFront::Distribution',
    ) as any;
    const ids = dist.Properties.DistributionConfig.Origins.map((o: any) => o.Id);
    expect(ids).not.toContain('ALBOrigin');

    // EcsStack stops registering the backend service with the public
    // target group — exactly one LoadBalancers entry (the internal one),
    // not two.
    const ecsTpl = Template.fromJSON(contractOut.raw.ecs);
    const services = ecsTpl.findResources('AWS::ECS::Service');
    const svc = Object.values(services)[0] as any;
    expect(svc.Properties.LoadBalancers).toHaveLength(1);
  });

  test('vpc (contract): the public ALB and its export are gone entirely', () => {
    const publicIds = albLogicalIds(publicOut.raw.alb);
    const remainingIds = Object.keys(vpcOut.raw.alb.Resources || {});
    for (const id of publicIds) {
      expect(remainingIds).not.toContain(id);
    }
    expect(Object.keys(vpcOut.raw.alb.Outputs || {})).not.toContain('AlbDnsName');

    const dist = Object.values(vpcOut.raw.frontend.Resources).find(
      (r: any) => r.Type === 'AWS::CloudFront::Distribution',
    ) as any;
    const ids = dist.Properties.DistributionConfig.Origins.map((o: any) => o.Id);
    expect(ids).not.toContain('ALBOrigin'); // the bypass's origin is gone, not just unreferenced
  });

  // Round-1 implementation review: the ORIGINAL version of this test
  // compared the network stack's WHOLE `AWS::EC2::SecurityGroupIngress`
  // list byte-for-byte across every state, which only held because every
  // state shared ONE security group. Splitting the public and internal
  // ALBs onto separate groups (round-1 implementation review's SG-sharing
  // blocker fix) means the TOTAL list legitimately differs from state to
  // state (the internal group's own rules only exist once that group
  // does) — so this test now isolates the ONE thing that is still
  // genuinely invariant: the PUBLIC ALB's own security group's own rule
  // never changes across every state where that group still exists.
  test("the public ALB's security group ingress rule stays byte-identical across every state it exists in", () => {
    function publicAlbSgIngress(raw: Record<string, any>): any[] {
      const sgEntry = Object.entries(raw.Resources || {}).find(
        ([, r]: [string, any]) =>
          r.Type === 'AWS::EC2::SecurityGroup' &&
          /^Security group for \S+ ALB$/.test(r.Properties?.GroupDescription ?? ''),
      );
      expect(sgEntry).toBeDefined();
      const [sgLogicalId] = sgEntry as [string, any];
      return Object.values(raw.Resources || {}).filter(
        (r: any) =>
          r.Type === 'AWS::EC2::SecurityGroupIngress' &&
          JSON.stringify(r.Properties?.GroupId ?? {}).includes(sgLogicalId),
      );
    }
    const base = JSON.stringify(publicAlbSgIngress(publicOut.raw.network));
    for (const out of [expandOut, switchOut, contractOut]) {
      expect(JSON.stringify(publicAlbSgIngress(out.raw.network))).toBe(base);
    }
    // vpc: the public ALB's security group no longer admits anything at
    // all — `needsPublicAlbSg` is false once `cloudfrontOrigin === 'vpc'`
    // (lib/network-stack.ts), so there is nothing left to compare.
    expect(publicAlbSgIngress(vpcOut.raw.network)).toHaveLength(0);
  });

  test("bin/iac.ts's own idle timeout / drop_invalid_header_fields / desync_mitigation_mode never drift across migration states", () => {
    const albAttrs = (raw: Record<string, any>, scheme: string) => {
      const albs = Object.values(raw.Resources || {}).filter(
        (r: any) => r.Type === 'AWS::ElasticLoadBalancingV2::LoadBalancer' && r.Properties.Scheme === scheme,
      ) as any[];
      expect(albs).toHaveLength(1);
      const out: Record<string, string> = {};
      for (const a of albs[0].Properties.LoadBalancerAttributes) out[a.Key] = a.Value;
      return out;
    };
    for (const out of [expandOut, switchOut, contractOut]) {
      const pub = albAttrs(out.raw.alb, 'internet-facing');
      const internal = albAttrs(out.raw.alb, 'internal');
      for (const key of [
        'idle_timeout.timeout_seconds',
        'routing.http.drop_invalid_header_fields.enabled',
        'routing.http.desync_mitigation_mode',
      ]) {
        expect(pub[key]).toBe(internal[key]);
      }
    }
  });
});
