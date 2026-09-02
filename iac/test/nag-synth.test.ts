import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

/**
 * Runs the REAL `bin/iac.ts` entrypoint through `cdk synth`, with every
 * context-gated stack turned on, and asserts cdk-nag reports zero unsuppressed
 * `AwsSolutions-*` findings.
 *
 * This closes a gap `bin/iac.ts` used to document about itself in a comment: a
 * stack missing from the `NagSuppressions.addStackSuppressions` block only
 * failed at the point somebody actually deployed it, because `cdk synth` was
 * not part of the jest suite. `quotaReconcilerStack` was exactly that stack —
 * three `AwsSolutions-IAM4`/`AwsSolutions-IAM5` findings surfaced only on a
 * real from-scratch deploy of a fresh account, well after this repo's own
 * unit tests (including `quota-reconciler-stack.test.ts`, which synthesizes
 * the stack in isolation and never runs the `AwsSolutionsChecks` Aspect) had
 * all passed. This file exercises the Aspect the way a real deploy does: via
 * the app entrypoint, with the flag combination a real deploy needs.
 *
 * Each test gets its OWN `-o` output directory via `fs.mkdtempSync`.
 * `region-decoupling.test.ts` records that an earlier subprocess-`cdk-synth`
 * approach was removed because it raced on a SHARED `cdk.out` under parallel
 * jest workers; a unique temp directory per invocation is what avoids
 * repeating that failure mode here.
 */
describe('cdk synth (real bin/iac.ts) — every context-gated stack, cdk-nag must report zero errors', () => {
  const iacDir = path.resolve(__dirname, '..');

  function synth(
    contextFlags: string[],
    extraEnv: Record<string, string> = {},
  ): { failed: boolean; output: string } {
    const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'stratoclave-nag-synth-'));
    const env: NodeJS.ProcessEnv = {
      ...process.env,
      CDK_DEFAULT_ACCOUNT: '123456789012',
      CDK_DEFAULT_REGION: 'us-east-1',
      STRATOCLAVE_REGION: 'us-east-1',
      STRATOCLAVE_PREFIX: 'nagtest',
      ...extraEnv,
    };
    const args = ['cdk', 'synth', '--all', '-o', outDir, '--quiet'];
    for (const flag of contextFlags) args.push('-c', flag);
    try {
      const stdout = execFileSync('npx', args, {
        cwd: iacDir,
        env,
        encoding: 'utf-8',
        stdio: 'pipe',
      });
      return { failed: false, output: stdout };
    } catch (e: unknown) {
      const err = e as { stdout?: string; stderr?: string };
      return { failed: true, output: `${err.stdout ?? ''}\n${err.stderr ?? ''}` };
    } finally {
      fs.rmSync(outDir, { recursive: true, force: true });
    }
  }

  test(
    'all four scheduled-Lambda stacks enabled: synth succeeds with zero AwsSolutions errors',
    () => {
      const result = synth(
        [
          'quotaReconciler=true',
          'quotaGrants=true',
          'ledgerProjector=true',
          'certificateScheduler=true',
        ],
        { LAMBDA_IMAGE_TAG: 'lambda-nag-synth-test' },
      );
      const errorLines = result.output
        .split('\n')
        .filter((line) => line.includes('[Error at') && line.includes('AwsSolutions-'));
      expect(errorLines).toEqual([]);
      expect(result.failed).toBe(false);
    },
    120_000,
  );

  test(
    'quota-reconciler / quota-grants refuse to synth without an explicit LAMBDA_IMAGE_TAG',
    () => {
      const result = synth(['quotaReconciler=true', 'quotaGrants=true']);
      expect(result.failed).toBe(true);
      expect(result.output).toMatch(/LAMBDA_IMAGE_TAG must be set/);
    },
    60_000,
  );

  test(
    "quota-reconciler refuses to synth when LAMBDA_IMAGE_TAG collides with the backend's IMAGE_TAG",
    () => {
      const result = synth(['quotaReconciler=true'], { LAMBDA_IMAGE_TAG: 'latest' });
      expect(result.failed).toBe(true);
      expect(result.output).toMatch(/must not equal the ECS backend's IMAGE_TAG/);
    },
    60_000,
  );
});
