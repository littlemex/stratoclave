/**
 * The two default-ceiling knobs must reach a deployed task, or the change is
 * inert in production no matter what the application default says.
 *
 * `DEFAULT_TENANT_CREDIT` used to be hardcoded to '100000' in bin/iac.ts, so
 * raising the application default alone would have left every deployment on the
 * old per-user token ceiling. `STRATOCLAVE_SEAT_MONTHLY_USD` was not forwarded
 * at all, so an operator could not change the seat price without editing code.
 *
 * See docs/design/limits.md and CONTRACTS.md C14.
 */
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as fs from 'fs';
import * as path from 'path';

const BIN = fs.readFileSync(path.join(__dirname, '..', 'bin', 'iac.ts'), 'utf8');

describe('the default-ceiling knobs reach a deployment', () => {
  test('DEFAULT_TENANT_CREDIT is not hardcoded to the old ceiling', () => {
    expect(BIN).not.toMatch(/DEFAULT_TENANT_CREDIT:\s*'100000'/);
  });

  test('DEFAULT_TENANT_CREDIT is forwarded from the deploy environment, defaulting to 10,000,000', () => {
    expect(BIN).toMatch(
      /DEFAULT_TENANT_CREDIT:\s*String\(\s*optionalPositiveIntFromEnv\('DEFAULT_TENANT_CREDIT'\)\s*\?\?\s*10_000_000\s*\)/,
    );
  });

  test('STRATOCLAVE_SEAT_MONTHLY_USD is forwarded, defaulting to 200', () => {
    expect(BIN).toMatch(
      /STRATOCLAVE_SEAT_MONTHLY_USD:\s*String\(\s*\n?\s*optionalPositiveIntFromEnv\('STRATOCLAVE_SEAT_MONTHLY_USD'\)\s*\?\?\s*200\s*\)/,
    );
  });

  test('both knobs are integers, so a token count can never be read as dollars', () => {
    // The two ceilings are denominated differently (tokens and whole USD), and
    // both are forwarded through the positive-int helper rather than as free
    // strings, so a malformed value fails the synth instead of reaching a task.
    const uses = BIN.match(/optionalPositiveIntFromEnv\('(DEFAULT_TENANT_CREDIT|STRATOCLAVE_SEAT_MONTHLY_USD)'\)/g);
    expect(uses).toHaveLength(2);
  });
});
