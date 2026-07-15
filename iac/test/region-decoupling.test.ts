import { execFileSync } from 'child_process';
import * as path from 'path';

/**
 * App-level (bin/iac.ts) tests for region decoupling + residency (v2.2).
 *
 * These synth the WHOLE app via `cdk synth` in a subprocess so they exercise the
 * real entrypoint logic (region resolution, residency analysis, the normalized
 * CODEX_ENABLED pass-through) — not a hand-constructed stack. Synth output is
 * observed from the emitted CloudFormation templates / stderr.
 */
const IAC_DIR = path.resolve(__dirname, '..');

interface SynthResult {
  code: number;
  stdout: string;
  stderr: string;
}

// Region/residency vars whose undefined-vs-empty distinction matters
// (e.g. STRATOCLAVE_RESIDENCY='' would flip residencyIntent on). We DELETE these
// from the child env for hermetic cases, then apply only the case's overrides —
// setting them to '' would wrongly count as "defined".
const HERMETIC_KEYS = [
  'STRATOCLAVE_REGION',
  'CDK_DEFAULT_REGION',
  'BEDROCK_PRIMARY_REGION',
  'STRATOCLAVE_FAILOVER_REGIONS',
  'STRATOCLAVE_RESIDENCY',
  'CODEX_ENABLED',
  'DEFAULT_BEDROCK_MODEL',
  'OPENAI_BEDROCK_REGIONS',
  'STRATOCLAVE_ALLOW_GEO_INFERENCE',
];

function synth(env: Record<string, string>): SynthResult {
  // Capture stdout+stderr separately regardless of exit code. `cdk synth`
  // writes CDK Annotation warnings to stderr on the SUCCESS path too, so we
  // must not discard stderr when the command succeeds.
  const { spawnSync } = require('child_process');
  const childEnv: Record<string, string | undefined> = {
    ...process.env,
    CDK_NAG: 'off',
    CDK_DEFAULT_ACCOUNT: '111122223333',
  };
  for (const k of HERMETIC_KEYS) delete childEnv[k];
  Object.assign(childEnv, env); // apply only this case's overrides
  const res = spawnSync('npx', ['cdk', 'synth', '--quiet'], {
    cwd: IAC_DIR,
    env: childEnv,
    encoding: 'utf-8',
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  return {
    code: res.status ?? 1,
    stdout: res.stdout ?? '',
    stderr: res.stderr ?? '',
  };
}
// Silence unused-import lint now that spawnSync is used instead.
void execFileSync;

function ecsTaskEnv(): Record<string, string> {
  // Read the synthesized ECS task-def env for THIS test's stack. The tests set
  // no STRATOCLAVE_PREFIX, so the entrypoint uses the default 'stratoclave'
  // prefix → 'stratoclave-ecs'. Pin that exact file: cdk.out is shared and may
  // also hold scverify-ecs / scveu-ecs templates from other synths, so picking
  // the "first *-ecs file" would read a stale, wrong-prefix template.
  const fs = require('fs');
  const tpl = JSON.parse(
    fs.readFileSync(
      path.join(IAC_DIR, 'cdk.out', 'stratoclave-ecs.template.json'),
      'utf-8',
    ),
  );
  const out: Record<string, string> = {};
  for (const r of Object.values<any>(tpl.Resources)) {
    if (r.Type === 'AWS::ECS::TaskDefinition') {
      for (const cd of r.Properties.ContainerDefinitions) {
        for (const e of cd.Environment ?? []) {
          if (typeof e.Value === 'string') out[e.Name] = e.Value;
        }
      }
    }
  }
  return out;
}

describe('bin/iac.ts region decoupling + residency', () => {
  jest.setTimeout(180_000);

  test('us-east-1 default synths and is residency-silent', () => {
    const r = synth({ STRATOCLAVE_REGION: 'us-east-1' });
    expect(r.code).toBe(0);
    expect(r.stderr).not.toMatch(/\[residency\]/);
  });

  test('NEW-8: CODEX_ENABLED=false is normalized in the task-def to "false"', () => {
    // The residency analysis and the container must agree. The task-def value
    // must be the normalized boolean, not the raw operator string.
    const r = synth({ STRATOCLAVE_REGION: 'us-east-1', CODEX_ENABLED: 'false' });
    expect(r.code).toBe(0);
    expect(ecsTaskEnv().CODEX_ENABLED).toBe('false');
  });

  test('NEW-8: mixed-case CODEX_ENABLED=FALSE normalizes to "false" (analysis == container)', () => {
    const r = synth({ STRATOCLAVE_REGION: 'us-east-1', CODEX_ENABLED: 'FALSE' });
    expect(r.code).toBe(0);
    expect(ecsTaskEnv().CODEX_ENABLED).toBe('false');
  });

  test('CODEX_ENABLED unset defaults to "true" in the task-def', () => {
    const r = synth({ STRATOCLAVE_REGION: 'us-east-1' });
    expect(r.code).toBe(0);
    expect(ecsTaskEnv().CODEX_ENABLED).toBe('true');
  });

  test('BEDROCK_REGION is the model primary, independent of AWS_REGION', () => {
    const r = synth({
      STRATOCLAVE_REGION: 'eu-west-1',
      BEDROCK_PRIMARY_REGION: 'us-east-1',
    });
    expect(r.code).toBe(0);
    const envMap = ecsTaskEnv();
    expect(envMap.AWS_REGION).toBe('eu-west-1');
    expect(envMap.BEDROCK_REGION).toBe('us-east-1');
  });

  test('missing BEDROCK_PRIMARY_REGION when body != us-east-1 throws', () => {
    const r = synth({ STRATOCLAVE_REGION: 'eu-west-1' });
    expect(r.code).not.toBe(0);
    expect(r.stderr).toMatch(/BEDROCK_PRIMARY_REGION must be set/);
  });

  test('NEW-9: strict + geo-profile default model throws', () => {
    // Default model us.anthropic.* is a US-geo profile; strict cannot certify
    // eu-west-1 residency for it.
    const r = synth({
      STRATOCLAVE_REGION: 'eu-west-1',
      BEDROCK_PRIMARY_REGION: 'eu-west-1',
      STRATOCLAVE_FAILOVER_REGIONS: 'disabled',
      CODEX_ENABLED: 'false',
      STRATOCLAVE_RESIDENCY: 'strict',
    });
    expect(r.code).not.toBe(0);
    expect(r.stderr).toMatch(/geo (cross-region )?inference profile/i);
  });

  test('NEW-9: geo-profile + escape hatch downgrades to warning (strict passes)', () => {
    const r = synth({
      STRATOCLAVE_REGION: 'eu-west-1',
      BEDROCK_PRIMARY_REGION: 'eu-west-1',
      STRATOCLAVE_FAILOVER_REGIONS: 'disabled',
      CODEX_ENABLED: 'false',
      STRATOCLAVE_RESIDENCY: 'strict',
      STRATOCLAVE_ALLOW_GEO_INFERENCE: 'true',
    });
    expect(r.code).toBe(0);
    expect(r.stderr).toMatch(/geo cross-region inference profile/i);
  });

  test('full EU residency with a directly-hosted model is strict-clean', () => {
    // A region-specific (non-geo) model id + all knobs pinned → strict passes,
    // no residency annotations.
    const r = synth({
      STRATOCLAVE_REGION: 'eu-west-1',
      BEDROCK_PRIMARY_REGION: 'eu-west-1',
      STRATOCLAVE_FAILOVER_REGIONS: 'disabled',
      CODEX_ENABLED: 'false',
      STRATOCLAVE_RESIDENCY: 'strict',
      DEFAULT_BEDROCK_MODEL: 'anthropic.claude-sonnet-4-6',
    });
    expect(r.code).toBe(0);
    expect(r.stderr).not.toMatch(/\[residency\]/);
  });

  test('NEW-1: codex still enabled defeats residency even with everything else pinned', () => {
    // The exact recipe that used to falsely pass: OPENAI_BEDROCK_REGIONS pinned
    // to EU but codex enabled → strict must still throw (codex is registry-pinned
    // to us-west-2/us-east-2).
    const r = synth({
      STRATOCLAVE_REGION: 'eu-west-1',
      BEDROCK_PRIMARY_REGION: 'eu-west-1',
      STRATOCLAVE_FAILOVER_REGIONS: 'disabled',
      DEFAULT_BEDROCK_MODEL: 'anthropic.claude-sonnet-4-6',
      STRATOCLAVE_RESIDENCY: 'strict',
      // codex left enabled (default true), OPENAI_BEDROCK_REGIONS is a no-op hint
    });
    expect(r.code).not.toBe(0);
    expect(r.stderr).toMatch(/us-west-2\(codex\)|us-east-2\(codex\)/);
  });

  test('NEW-6: invalid STRATOCLAVE_RESIDENCY value throws', () => {
    const r = synth({ STRATOCLAVE_REGION: 'us-east-1', STRATOCLAVE_RESIDENCY: 'strickt' });
    expect(r.code).not.toBe(0);
    expect(r.stderr).toMatch(/STRATOCLAVE_RESIDENCY must be/);
  });
});
