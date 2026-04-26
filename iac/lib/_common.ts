import * as cdk from 'aws-cdk-lib';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

/**
 * Shared helpers used by all Stratoclave CDK stacks.
 *
 * Keeps stack names, SSM Parameter Store paths, and tagging consistent so
 * that ops tooling (scripts) can compose paths deterministically from the
 * prefix alone.
 */

/** Returns the Stratoclave resource name prefix (default `stratoclave`). */
export function getPrefix(): string {
  return process.env.STRATOCLAVE_PREFIX || 'stratoclave';
}

/**
 * Generates a CloudFormation stack name from the prefix and a short id.
 *
 * Example: stackName('stratoclave', 'network') -> 'stratoclave-network'
 *
 * The format is `<prefix>-<id>` (kebab-case). This must match exactly the
 * names of the CloudFormation stacks already deployed in an account so that
 * `cdk diff` / `cdk deploy` addresses the same resources.
 */
export function stackName(prefix: string, id: string): string {
  return `${prefix}-${id}`;
}

/**
 * Composes an SSM Parameter Store path under `/<prefix>/`.
 *
 * Example: paramPath('stratoclave', 'network/vpc-id') -> '/stratoclave/network/vpc-id'
 *          paramPath('stratoclave', '')               -> '/stratoclave/'
 */
export function paramPath(prefix: string, relativePath: string): string {
  const cleaned = relativePath.replace(/^\/+/, '');
  return cleaned ? `/${prefix}/${cleaned}` : `/${prefix}/`;
}

export interface PutStringParameterProps {
  /** Resource name prefix (used to namespace the parameter path). */
  prefix: string;
  /** Path under `/<prefix>/`, e.g. `network/vpc-id`. */
  relativePath: string;
  /** Parameter value. */
  value: string;
  /** Optional human-readable description. */
  description?: string;
}

/**
 * Creates an SSM Parameter Store entry with a conventional path and tags.
 *
 * Wraps `ssm.StringParameter` so every stack uses the same naming convention
 * without repeating boilerplate.
 */
export function putStringParameter(
  scope: Construct,
  id: string,
  props: PutStringParameterProps
): ssm.StringParameter {
  return new ssm.StringParameter(scope, id, {
    parameterName: paramPath(props.prefix, props.relativePath),
    stringValue: props.value,
    description: props.description,
    tier: ssm.ParameterTier.STANDARD,
  });
}

/**
 * Applies the common tag set (Project, Prefix, Stack) to every resource in
 * the given scope.
 */
export function applyCommonTags(
  scope: Construct,
  prefix: string,
  stackTag: string
): void {
  cdk.Tags.of(scope).add('Project', 'Stratoclave');
  cdk.Tags.of(scope).add('Prefix', prefix);
  cdk.Tags.of(scope).add('Stack', stackTag);
}
