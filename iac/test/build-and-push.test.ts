import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

/**
 * `scripts/build-and-push.sh` run against stand-ins for `aws` and the container CLI,
 * which record every call. The repository's tags are IMMUTABLE, so the order of the
 * pushes is the behaviour: the build tag is the record of the build and what a deploy
 * names, and `latest` is best-effort.
 */
const SCRIPT = path.resolve(__dirname, '..', 'scripts', 'build-and-push.sh');

interface Run {
  status: number | null;
  output: string;
  calls: string[];
}

function run(opts: { latestFails?: boolean } = {}): Run {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'build-and-push-'));
  const log = path.join(dir, 'calls.log');
  const stub = (name: string, body: string) => {
    const file = path.join(dir, name);
    fs.writeFileSync(file, `#!/bin/bash\necho "${name} $*" >> "${log}"\n${body}\n`);
    fs.chmodSync(file, 0o755);
  };
  stub(
    'aws',
    [
      'case "$1 $2" in',
      '  "cloudformation describe-stacks") echo stratoclave-backend ;;',
      '  "sts get-caller-identity") echo 123456789012 ;;',
      '  "ecr get-login-password") echo token ;;',
      'esac',
      'exit 0',
    ].join('\n'),
  );
  stub(
    'fakecli',
    opts.latestFails ? 'case "$*" in "push "*:latest) exit 1 ;; esac\nexit 0' : 'exit 0',
  );
  const result = spawnSync('bash', [SCRIPT], {
    env: {
      ...process.env,
      PATH: `${dir}:${process.env.PATH}`,
      CONTAINER_CLI: 'fakecli',
      AWS_REGION: 'us-east-1',
      BUILD_LAMBDA_IMAGE: 'false',
      BUILD_TAG: 'test-build-tag',
    },
    encoding: 'utf8',
  });
  const calls = fs.existsSync(log) ? fs.readFileSync(log, 'utf8').trim().split('\n') : [];
  fs.rmSync(dir, { recursive: true, force: true });
  return { status: result.status, output: `${result.stdout}${result.stderr}`, calls };
}

const pushes = (calls: string[]) => calls.filter((c) => c.startsWith('fakecli push '));

describe('build-and-push.sh', () => {
  test('builds for linux/amd64 regardless of the host', () => {
    const { calls } = run();
    expect(calls.some((c) => c.startsWith('fakecli build --platform linux/amd64 '))).toBe(true);
  });

  test('pushes the build tag before latest', () => {
    const { status, calls } = run();
    expect(status).toBe(0);
    expect(pushes(calls)).toEqual([
      'fakecli push 123456789012.dkr.ecr.us-east-1.amazonaws.com/stratoclave-backend:test-build-tag',
      'fakecli push 123456789012.dkr.ecr.us-east-1.amazonaws.com/stratoclave-backend:latest',
    ]);
  });

  test('a latest that cannot move leaves the build tag pushed and the run successful', () => {
    const { status, output, calls } = run({ latestFails: true });
    expect(status).toBe(0);
    expect(pushes(calls)[0]).toMatch(/:test-build-tag$/);
    expect(output).toContain('Could not move :latest');
  });

  test('prints the deploy command that names the build tag', () => {
    const { output } = run();
    expect(output).toContain('IMAGE_TAG=test-build-tag npx cdk deploy stratoclave-ecs');
  });
});
