/**
 * A synth-time credential-free environment, shared by every test file that
 * spawns a real `cdk synth` subprocess against `bin/iac.ts`.
 *
 * Load-bearing, not cosmetic: without neutralising ambient AWS credentials,
 * the `cdk` CLI resolves its own "default account" from whatever real
 * credentials happen to be ambient in the shell that runs the test and
 * OVERWRITES `CDK_DEFAULT_ACCOUNT` with that real account before spawning
 * the app subprocess — regardless of what a caller's own `env` object
 * passes. Measured directly: with ambient credentials left alone, a
 * synthesized template contained a real AWS account id, baked into literal
 * `Account` properties, ARNs, and bucket names, and that id reached a
 * committed snapshot once already. Neutralising credentials makes the `cdk`
 * CLI unable to resolve anything beyond what the caller explicitly passes,
 * so an explicit `CDK_DEFAULT_ACCOUNT` is the value every downstream
 * `this.account` resolves to.
 */
export function hermeticSynthEnv(extra: Record<string, string> = {}): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env };
  delete env.AWS_PROFILE;
  delete env.AWS_DEFAULT_PROFILE;
  delete env.AWS_SESSION_TOKEN;
  delete env.AWS_REGION;
  delete env.AWS_DEFAULT_REGION;
  delete env.AWS_CONTAINER_CREDENTIALS_RELATIVE_URI;
  delete env.AWS_CONTAINER_CREDENTIALS_FULL_URI;
  delete env.AWS_WEB_IDENTITY_TOKEN_FILE;
  return {
    ...env,
    AWS_ACCESS_KEY_ID: 'AKIAFAKEFAKEFAKEFAKE',
    AWS_SECRET_ACCESS_KEY: 'fakefakefakefakefakefakefakefakefakefake',
    AWS_EC2_METADATA_DISABLED: 'true',
    ...extra,
  };
}
