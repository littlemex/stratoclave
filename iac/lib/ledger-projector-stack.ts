import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { DynamoEventSource, SqsDlq } from 'aws-cdk-lib/aws-lambda-event-sources';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import { Construct } from 'constructs';
import { applyCommonTags } from './_common';

/**
 * Ledger Streams projector + reconciler (two-item migration, step 1).
 *
 * Moves the RESERVE ledger event OFF the synchronous reserve transaction (one of
 * the four items driving the single-pool-row contention tail) to an async
 * projector that derives it from the HOLD row's DynamoDB stream record. The
 * projector is byte-faithful to the synchronous builder (it imports the SAME
 * backend code via Dockerfile.lambda), writes idempotently under
 * attribute_not_exists, and in step 1 writes to a SHADOW# sk namespace. A
 * scheduled reconciler diffs shadow vs synchronous and emits a divergence metric
 * the migration is gated on (must be 0 before the async cut-over).
 *
 * Nothing here touches the hot path: enabling the stream + attaching a shadow
 * consumer is inert to reserve/settle. The synchronous RESERVE event is still
 * written until a later step removes it.
 */
export interface LedgerProjectorStackProps extends cdk.StackProps {
  prefix: string;
  /** ECR repo holding the Lambda image (built from backend/Dockerfile.lambda). */
  lambdaRepository: ecr.IRepository;
  /** Immutable tag of the Lambda image. */
  lambdaImageTag: string;
  /** Source stream: the tenant-budgets table (HOLD rows live here). */
  tenantBudgetsTable: dynamodb.ITable;
  /** Sink: the credit-ledger table (RESERVE/SHADOW events written here). */
  creditLedgerTable: dynamodb.ITable;
  /** SHADOW# mode (step 1). Default true; the async cut-over flips it off. */
  shadow?: boolean;
}

export class LedgerProjectorStack extends cdk.Stack {
  public readonly projector: lambda.Function;
  public readonly reconciler: lambda.Function;
  public readonly dlq: sqs.Queue;

  constructor(scope: Construct, id: string, props: LedgerProjectorStackProps) {
    super(scope, id, props);
    const { prefix, lambdaRepository, lambdaImageTag, tenantBudgetsTable, creditLedgerTable } = props;
    const shadow = props.shadow ?? true;

    const commonEnv = { LEDGER_PROJECTOR_SHADOW: String(shadow) };
    // Same image, different entrypoint per function: the CMD override lives on
    // DockerImageCode (image config), not the function props.
    const projectorCode = lambda.DockerImageCode.fromEcr(lambdaRepository, {
      tagOrDigest: lambdaImageTag,
      cmd: ['billing.ledger_projector.handler'],
    });
    const reconcilerCode = lambda.DockerImageCode.fromEcr(lambdaRepository, {
      tagOrDigest: lambdaImageTag,
      cmd: ['billing.ledger_reconciler.handler'],
    });

    // Permanent-failure DLQ: a record the projector cannot process after retries
    // lands here (alarmed) rather than halting the shard forever. The audit
    // projection is degraded, never billing.
    this.dlq = new sqs.Queue(this, 'ProjectorDlq', {
      queueName: `${prefix}-ledger-projector-dlq`,
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
    });

    // --- Projector: budgets stream (HOLD INSERT) -> RESERVE event ---
    this.projector = new lambda.DockerImageFunction(this, 'Projector', {
      functionName: `${prefix}-ledger-projector`,
      code: projectorCode,
      memorySize: 256,
      timeout: cdk.Duration.seconds(30),
      environment: commonEnv,
      description: 'Derives RESERVE ledger events from HOLD stream records (shadow in step 1).',
    });
    // Idempotent conditional Put into the ledger; no reads of the budgets table
    // beyond the stream. Least privilege: write to the ledger only.
    creditLedgerTable.grantWriteData(this.projector);

    this.projector.addEventSource(new DynamoEventSource(tenantBudgetsTable, {
      startingPosition: lambda.StartingPosition.LATEST,
      batchSize: 100,
      maxBatchingWindow: cdk.Duration.seconds(5),
      // Per-hold order is preserved within a shard; parallelize across shards.
      parallelizationFactor: 2,
      retryAttempts: 5,
      bisectBatchOnError: true,
      reportBatchItemFailures: true,
      onFailure: new SqsDlq(this.dlq),
    }));

    // --- Reconciler: scheduled shadow-vs-synchronous diff ---
    this.reconciler = new lambda.DockerImageFunction(this, 'Reconciler', {
      functionName: `${prefix}-ledger-reconciler`,
      code: reconcilerCode,
      memorySize: 256,
      timeout: cdk.Duration.minutes(5),
      environment: commonEnv,
      description: 'Diffs shadow RESERVE projections vs synchronous events; emits divergence metric.',
    });
    creditLedgerTable.grantReadData(this.reconciler);

    new events.Rule(this, 'ReconcilerSchedule', {
      ruleName: `${prefix}-ledger-reconciler-schedule`,
      schedule: events.Schedule.rate(cdk.Duration.minutes(15)),
      targets: [new targets.LambdaFunction(this.reconciler)],
    });

    // --- Alarms: divergence must be 0; DLQ must stay empty ---
    const divergenceMetric = new cloudwatch.Metric({
      namespace: 'Stratoclave/Ledger',
      metricName: 'ReserveShadowDivergence',
      statistic: 'Maximum',
      period: cdk.Duration.minutes(15),
    });
    new cloudwatch.Alarm(this, 'ReserveShadowDivergenceAlarm', {
      alarmName: `${prefix}-ledger-reserve-shadow-divergence`,
      metric: divergenceMetric,
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      // MISSING data is BREACHING (Fable review finding 4): "no divergence metric"
      // must NOT read as healthy — a reconciler that stopped emitting (crash, EMF
      // drop, disabled schedule) would otherwise silently green-light the cut-over.
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      alarmDescription: 'Shadow RESERVE projection diverged (or the reconciler stopped emitting) — block cut-over.',
    });
    new cloudwatch.Alarm(this, 'ProjectorDlqAlarm', {
      alarmName: `${prefix}-ledger-projector-dlq-depth`,
      metric: this.dlq.metricApproximateNumberOfMessagesVisible({ period: cdk.Duration.minutes(5) }),
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription: 'A record permanently failed projection — audit projection degraded.',
    });

    applyCommonTags(this, prefix, 'ledger-projector');
  }
}
