import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { hermeticSynthEnv } from './_hermetic-synth';

/**
 * `STRATOCLAVE_ENTRY` unset (or `cloudfront`) must synthesize the same
 * CloudFormation templates as before this feature existed. That one-time
 * before/after proof was run once, by hand, with `cdk synth --all` against
 * the pre-change and post-change trees (the `.template.json` files were
 * byte-identical; only `.metadata.json` trace line numbers moved, because
 * the source files grew lines — construct traces are not part of what gets
 * deployed). That proof cannot be re-run by a future CI job because there is
 * no "before" tree once this commit lands.
 *
 * What CAN be re-run forever is this: pin the EXACT resources this feature's
 * entry-mode branching touches — the ALB security-group ingress rule and the
 * CloudFront prefix-list lookup (network), the ALB load balancer and its
 * listeners (alb), the UserPoolClient's callback/logout URLs (cognito), and
 * the two ECS task-env entries that changed (`CORS_ORIGINS`,
 * `STRATOCLAVE_API_ENDPOINT`) — and fail the moment a future change to
 * entry-config.ts, network-stack.ts, alb-stack.ts, or bin/iac.ts's own
 * branching moves any of them by so much as one byte.
 *
 * Deliberately NOT a snapshot of the whole network/alb/cognito templates:
 * those stacks also carry plenty this feature never touches (VPC CIDR, flow
 * log retention, the Cognito password policy, the ALB's target group health
 * check, ...), and a whole-template snapshot would churn — and invite a
 * reflexive `-u`, defeating the point of pinning anything — on every one of
 * those unrelated edits. "Pinned" here means exactly these resources, not
 * "byte-for-byte identical to the historical templates" (which the one-time
 * hand-run proof above already established, for the templates in full, once).
 *
 * Synth is run with the ambient AWS credentials NEUTRALISED (see
 * `test/_hermetic-synth.ts` for why that is load-bearing, not cosmetic: a
 * real AWS account id reached a committed snapshot here once already,
 * because the `cdk` CLI resolves its own "default account" from ambient
 * credentials and overwrites `CDK_DEFAULT_ACCOUNT` with it, regardless of
 * what this file passes).
 */
describe('STRATOCLAVE_ENTRY unset (default): the resources this feature branches on are pinned', () => {
  const iacDir = path.resolve(__dirname, '..');
  const PREFIX = 'defaultentrytest';
  const DUMMY_ACCOUNT = '123456789012';

  // Exactly the nine stacks a default (no context flags) deploy has always
  // produced. A ratchet: if entry-mode branching ever added or removed one
  // of these for the DEFAULT mode, this list — and the stack-list assertion
  // below — catches it independently of the pinned-resource snapshots.
  const DEFAULT_STACK_IDS = [
    'network',
    'dynamodb',
    'ecr',
    'alb',
    'waf',
    'frontend',
    'cognito',
    'ecs',
    'config',
  ];

  function hermeticEnv(extra: Record<string, string> = {}): NodeJS.ProcessEnv {
    const env = hermeticSynthEnv({
      CDK_DEFAULT_ACCOUNT: DUMMY_ACCOUNT,
      CDK_DEFAULT_REGION: 'us-east-1',
      STRATOCLAVE_REGION: 'us-east-1',
      STRATOCLAVE_PREFIX: PREFIX,
      CDK_NAG: 'off',
      ...extra,
    });
    // STRATOCLAVE_ENTRY must be deliberately ABSENT: this is the precise
    // condition ("unset") the default-mode guarantee covers. A `delete`
    // here, not a spread of `undefined` into `hermeticSynthEnv`'s `extra` —
    // whether Node's child_process treats an `undefined` env value as
    // "unset" or as the literal string "undefined" is not a contract this
    // file should depend on.
    delete env.STRATOCLAVE_ENTRY;
    return env;
  }

  /**
   * Replace content that is expected to change for reasons unrelated to
   * this feature (an asset's content hash moves when ANY file bundled into
   * it changes, including ones this feature does not touch) with a fixed
   * placeholder, so the snapshot only breaks on changes that matter to it.
   */
  function normalizeAssetHashes(value: unknown): unknown {
    const text = JSON.stringify(value).replace(/[0-9a-f]{64}/g, '<ASSET_HASH>');
    return JSON.parse(text);
  }

  /** Every resource of one of the given CloudFormation types, by logical id. */
  function pickResourcesByType(
    template: Record<string, any>,
    types: string[],
  ): Record<string, any> {
    const out: Record<string, any> = {};
    for (const [id, res] of Object.entries(template.Resources ?? {})) {
      if (types.includes((res as any).Type)) out[id] = res;
    }
    return out;
  }

  function pickUserPoolClientUrls(template: Record<string, any>): {
    CallbackURLs: unknown;
    LogoutURLs: unknown;
  } {
    const client = Object.values(template.Resources ?? {}).find(
      (r: any) => r.Type === 'AWS::Cognito::UserPoolClient',
    ) as any;
    return {
      CallbackURLs: client?.Properties?.CallbackURLs,
      LogoutURLs: client?.Properties?.LogoutURLs,
    };
  }

  let outDir: string;
  let rawTemplates: Record<string, Record<string, any>>;

  beforeAll(() => {
    outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'stratoclave-default-entry-'));
    execFileSync('npx', ['cdk', 'synth', '--all', '-o', outDir, '--quiet'], {
      cwd: iacDir,
      env: hermeticEnv(),
      encoding: 'utf-8',
      stdio: 'pipe',
    });
    rawTemplates = {};
    for (const id of DEFAULT_STACK_IDS) {
      const templatePath = path.join(outDir, `${PREFIX}-${id}.template.json`);
      rawTemplates[id] = JSON.parse(fs.readFileSync(templatePath, 'utf-8'));
    }
  }, 120_000);

  afterAll(() => {
    if (outDir) fs.rmSync(outDir, { recursive: true, force: true });
  });

  test('synthesizes exactly the nine default-mode stacks — no private-link stack, no entry-mode stack list change', () => {
    const produced = fs
      .readdirSync(outDir)
      .filter((f) => f.endsWith('.template.json'))
      .map((f) => f.replace(/\.template\.json$/, ''))
      .sort();
    const expected = DEFAULT_STACK_IDS.map((id) => `${PREFIX}-${id}`).sort();
    expect(produced).toEqual(expected);
  });

  test('network: the ALB security-group ingress rule(s) and the CloudFront prefix-list lookup are pinned', () => {
    const picked = pickResourcesByType(rawTemplates.network, [
      'AWS::EC2::SecurityGroupIngress',
      'Custom::AWS',
    ]);
    expect(normalizeAssetHashes(picked)).toMatchSnapshot();
  });

  test('alb: the load balancer and its listeners are pinned', () => {
    const picked = pickResourcesByType(rawTemplates.alb, [
      'AWS::ElasticLoadBalancingV2::LoadBalancer',
      'AWS::ElasticLoadBalancingV2::Listener',
    ]);
    expect(normalizeAssetHashes(picked)).toMatchSnapshot();
  });

  test('cognito: the UserPoolClient callback/logout URLs are pinned', () => {
    expect(pickUserPoolClientUrls(rawTemplates.cognito)).toMatchSnapshot();
  });

  test('the ECS task env entries this feature branches on are pinned', () => {
    const taskDefs = Object.values(rawTemplates.ecs.Resources || {}).filter(
      (r: any) => r.Type === 'AWS::ECS::TaskDefinition',
    );
    expect(taskDefs).toHaveLength(1);
    const containers = (taskDefs[0] as any).Properties.ContainerDefinitions as Array<{
      Name: string;
      Environment: Array<{ Name: string; Value: string }>;
    }>;
    const backend = containers.find((c) => /backend/i.test(c.Name));
    expect(backend).toBeDefined();
    const env: Record<string, string> = {};
    for (const pair of backend!.Environment) env[pair.Name] = pair.Value;
    expect({
      CORS_ORIGINS: env.CORS_ORIGINS,
      STRATOCLAVE_API_ENDPOINT: env.STRATOCLAVE_API_ENDPOINT,
    }).toMatchSnapshot();
  });

  test('no 12-digit number other than the dummy account appears anywhere in any default-mode template', () => {
    const offenders: Record<string, string[]> = {};
    for (const id of DEFAULT_STACK_IDS) {
      const text = JSON.stringify(rawTemplates[id]);
      const found = Array.from(new Set(text.match(/\b\d{12}\b/g) ?? [])).filter(
        (n) => n !== DUMMY_ACCOUNT,
      );
      if (found.length > 0) offenders[id] = found;
    }
    // A failure here means a real AWS account id (or some other unrelated
    // 12-digit number) reached the synthesized output — the exact class of
    // leak this file's hermetic env exists to prevent. See the file doc.
    expect(offenders).toEqual({});
  });
});
