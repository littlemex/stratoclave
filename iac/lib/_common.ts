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

/**
 * A positive integer from the environment, or `fallback`.
 *
 * Deploy-time sizing knobs (task CPU, task ceiling, per-target request budget)
 * read through here so a typo cannot quietly shrink a service: a value that is
 * not a positive integer is rejected loudly at synth time rather than silently
 * replaced, because a fleet sized from `NaN` fails in production, not in CI.
 */
export function positiveIntFromEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw === '') {
    return fallback;
  }
  return parsePositiveInt(name, raw);
}

/**
 * Like `positiveIntFromEnv`, but `undefined` when the variable is unset.
 *
 * For knobs whose only defensible value comes from a measurement: shipping a
 * plausible-looking default would put a number nobody measured into production,
 * and the consumer can tell "not configured" from "configured to n".
 */
export function optionalPositiveIntFromEnv(name: string): number | undefined {
  const raw = process.env[name];
  if (raw === undefined || raw === '') {
    return undefined;
  }
  return parsePositiveInt(name, raw);
}

/**
 * Decimal digits only, deliberately.
 *
 * `Number()` would accept `1e3`, `0x80`, `1.0` and `" 128 "`, none of which the
 * Python side's `int()` reads the same way. A sizing knob that means one thing at
 * synth and another in the container is worse than one that is rejected, so this
 * takes the narrower of the two input languages.
 */
function parsePositiveInt(name: string, raw: string): number {
  if (!/^[1-9][0-9]*$/.test(raw)) {
    throw new Error(
      `${name} must be a positive decimal integer when set (got ${JSON.stringify(raw)})`,
    );
  }
  return Number(raw);
}

/**
 * What a fleet configuration can actually hold, against what it was asked to hold.
 *
 * The concurrency target is a product statement ("hold 1024 requests"); the knobs
 * are per-task threads, a floor and a ceiling. Two different numbers come out of
 * them and conflating them is how a fleet ends up nominally sized for a target it
 * never reaches:
 *
 *   - `immediate` is floor x per-task. It is what serves a burst, because
 *     autoscaling observes a metric, waits for an alarm and then pays a cold start.
 *   - `sustained` is ceiling x per-task, reachable only if some policy actually
 *     grows the fleet.
 */
export interface CapacityPlanInput {
  target: number;
  minTasks: number;
  maxTasks: number;
  perTaskRequests: number;
  /** Requests/min/task budget for the ALB policy, or undefined when unset. */
  requestsPerTarget?: number;
}

export interface CapacityPlan {
  immediate: number;
  sustained: number;
  /** Configuration facts worth stating at synth time. */
  notes: string[];
  /** Ways this configuration will not meet its target. */
  warnings: string[];
}

export function capacityPlan(input: CapacityPlanInput): CapacityPlan {
  const immediate = input.minTasks * input.perTaskRequests;
  const sustained = input.maxTasks * input.perTaskRequests;
  const notes: string[] = [
    `concurrency target ${input.target}: ${immediate} in flight immediately ` +
      `(${input.minTasks} x ${input.perTaskRequests}), ${sustained} after scale-out ` +
      `(${input.maxTasks} x ${input.perTaskRequests})`,
  ];
  const warnings: string[] = [];

  if (sustained < input.target) {
    warnings.push(
      `the fleet cannot reach its concurrency target: ${input.maxTasks} tasks x ` +
        `${input.perTaskRequests} requests = ${sustained}, target ${input.target}. ` +
        'Raise BACKEND_MAX_TASKS or GATEWAY_SYNC_ROUTE_THREADS.',
    );
  }
  if (input.maxTasks > input.minTasks && input.requestsPerTarget === undefined) {
    warnings.push(
      'the fleet may grow but nothing will grow it except CPU tracking, and this ' +
        'workload leaves CPU low while it saturates (measured 25-29% average at ' +
        'the throughput ceiling). Set BACKEND_REQUESTS_PER_TARGET from a sweep.',
    );
  }
  if (immediate < input.target) {
    notes.push(
      `a burst beyond ${immediate} in flight waits for a scale-out; raise ` +
        'BACKEND_MIN_TASKS to absorb the full target immediately, at the cost of ' +
        'idle tasks.',
    );
  }
  return { immediate, sustained, notes, warnings };
}
