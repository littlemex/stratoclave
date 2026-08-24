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
  /** In-flight requests one server PROCESS admits. */
  perProcessRequests: number;
  /** Server processes per task (uvicorn workers). */
  workersPerTask: number;
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
  // Processes are the unit that matters, not tasks: latency tracks requests in
  // flight per process, so a task with four workers admits four times what one
  // with a single worker does at the same per-process ceiling.
  const perTask = input.workersPerTask * input.perProcessRequests;
  const immediate = input.minTasks * perTask;
  const sustained = input.maxTasks * perTask;
  const notes: string[] = [
    `concurrency target ${input.target}: ${immediate} in flight immediately ` +
      `(${input.minTasks} tasks x ${input.workersPerTask} workers x ` +
      `${input.perProcessRequests}), ${sustained} after scale-out ` +
      `(${input.maxTasks} tasks)`,
  ];
  const warnings: string[] = [];

  if (sustained < input.target) {
    warnings.push(
      `the fleet cannot reach its concurrency target: ${input.maxTasks} tasks x ` +
        `${input.workersPerTask} workers x ${input.perProcessRequests} requests = ` +
        `${sustained}, target ${input.target}. Raise BACKEND_MAX_TASKS, or the task ` +
        'CPU so it can run more workers. Raising GATEWAY_SYNC_ROUTE_THREADS instead ' +
        'buys admission at the cost of latency, which is what it was measured to do.',
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

/**
 * The per-IP, per-5-minute rate a client at the concurrency target can legitimately
 * produce.
 *
 * A rate rule and a concurrency target are the same kind of knob-pair trap as the
 * thread ceilings: state them separately and one silently defeats the other. One
 * client holding `target` requests in flight, each taking at least
 * `fastestRequestSeconds`, issues at most this many requests per window. The
 * default 0.5 s is below the fastest p50 measured on 2026-08-24 (306 ms direct,
 * 597 ms through the gateway), so the result is a ceiling legitimate traffic does
 * not reach rather than an average it might.
 */
export function impliedRatePer5Min(
  target: number,
  fastestRequestSeconds = 0.5,
): number {
  const windowSeconds = 300;
  return Math.ceil((target * windowSeconds) / fastestRequestSeconds);
}

/** Fargate CPU units in one vCPU. */
export const CPU_UNITS_PER_VCPU = 1024;

/**
 * How many server processes a task of this size can actually run.
 *
 * Each process has its own GIL, which is what makes more than one worthwhile:
 * measured on 2026-08-25, latency tracked requests in flight per PROCESS (390 ms
 * at 4, 1331 ms at 32, 7706 ms at 128) while task CPU stayed under 70%. But a
 * worker with no core to run on gains nothing — N processes on one vCPU still
 * execute one bytecode stream at a time — so the count follows the task's vCPU
 * rather than the concurrency target.
 */
export function workersForCpuUnits(cpuUnits: number): number {
  return Math.max(1, Math.floor(cpuUnits / CPU_UNITS_PER_VCPU));
}
