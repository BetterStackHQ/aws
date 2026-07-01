# Better Stack CloudWatch Integration

Stream AWS CloudWatch metrics, logs, and optionally X-Ray traces and CloudTrail audit logs to Better Stack via Kinesis Data Firehose.

See the [main README](../../README.md) for an overview or the [Lambda documentation](../../lambda/README.md) for details on deployed lambdas.

## Prerequisites

1. **Better Stack Account** - Sign up at [betterstack.com](https://betterstack.com)
2. **Cluster ID** - Your Better Stack cluster identifier (e.g., `g1`, `s1234`, `acme`)
3. **Source Token** - Authentication token from Better Stack
4. **Source ID** - Source ID from Better Stack for resource matching
5. **AWS CLI** - Configured with appropriate permissions

## Quick Start

### First Region Deployment

```bash
aws cloudformation deploy \
  --template-file better-stack-full.yaml \
  --stack-name better-stack-full \
  --parameter-overrides \
    ClusterId=YOUR_CLUSTER_ID \
    SourceToken=YOUR_SOURCE_TOKEN \
    SourceId=YOUR_SOURCE_ID \
  --capabilities CAPABILITY_NAMED_IAM
```

### Additional Regions

**Deploy the first region before any additional region.** The first-region deployment (`CreateGlobalResources=true`) provisions the account-wide IAM roles that *every* region relies on. It creates **all** of the shared roles — metric-stream, tag-enrichment, Firehose, logs-subscription, and bucket-manager — regardless of which features that first region itself has enabled. This means an additional region can safely turn on any feature (e.g. `EnableMetricStream=true`) even if the first region runs in a different mode.

When deploying to additional regions, set `CreateGlobalResources=false` to reuse those IAM roles:

```bash
aws cloudformation deploy \
  --template-file better-stack-full.yaml \
  --stack-name better-stack-full \
  --region us-west-2 \
  --parameter-overrides \
    ClusterId=YOUR_CLUSTER_ID \
    SourceToken=YOUR_SOURCE_TOKEN \
    SourceId=YOUR_SOURCE_ID \
    CreateGlobalResources=false \
  --capabilities CAPABILITY_NAMED_IAM
```

## Data Flows

| Data Type | Flow |
|-----------|------|
| Metrics (default) | CloudWatch Metric Stream (JSON, per-namespace opt-in) -> Firehose -> Lambda (enrichment) -> Better Stack |
| Metrics (`EnableMetricStream=false`) | Better Stack pulls metrics via the CloudWatch API (`GetMetricData`) using the integration role |
| Logs | CloudWatch Logs -> Subscription Filter -> Firehose -> Lambda (enrichment) -> Better Stack |
| Traces | X-Ray -> CloudWatch Logs (`aws/spans`) -> Subscription Filter -> Better Stack |
| Audit | CloudTrail -> CloudWatch Logs -> Subscription Filter -> Better Stack |

## Parameters

### Required Parameters

| Parameter | Description |
|-----------|-------------|
| `ClusterId` | Your Better Stack cluster ID |
| `SourceToken` | Better Stack source token for authentication |
| `SourceId` | Better Stack source ID for resource matching |

### Deployment Options

| Parameter | Default | Description |
|-----------|---------|-------------|
| `CreateGlobalResources` | `true` | Create IAM roles. Set to `false` for secondary regions. |
| `FirehoseRetryDurationSeconds` | `3600` | How long (seconds) Firehose retries delivery before writing failed records to the S3 backup bucket. Max `7200`; `0` disables retries. |

### Feature Toggles

| Parameter | Default | Description |
|-----------|---------|-------------|
| `EnableMetricStream` | `true` | Stream metrics via a CloudWatch Metric Stream + Firehose. Set to `false` to use legacy API polling (see below). |
| `EnableTagEnrichment` | `true` | Enable Lambda-based tag enrichment for metrics and logs |
| `EnableCloudTrail` | `true` | Create a new CloudTrail trail and forward to Better Stack |
| `EnableXRayTransactionSearch` | `true` | Enable X-Ray Transaction Search and forward spans |

## Metric Collection: Stream vs. Legacy API Polling

By default (`EnableMetricStream=true`) metrics are streamed to Better Stack in near
real-time through a CloudWatch Metric Stream and Kinesis Data Firehose.

Set `EnableMetricStream=false` to switch to the **legacy** approach, where Better Stack
periodically pulls metrics from the CloudWatch API (`GetMetricData` / `ListMetrics`)
using the integration role. This is useful where metric streaming is undesirable (e.g.
to avoid per-region streaming resources or Firehose ingestion cost). When disabled, the
template does **not** create the metric stream, the metrics Firehose, or the metrics
tag-enrichment Lambda — logs, traces, and CloudTrail are unaffected.

The `better-stack-integration-role` already grants `cloudwatch:Get*` / `cloudwatch:List*`
on all resources, so Better Stack can read every metric via the API without any extra
permissions.

### Metric namespace subscription (explicit opt-in)

When streaming is enabled, the metric stream is created with an `IncludeFilters`
allowlist seeded with a single placeholder namespace (`BetterStack/Unsubscribed`) that
has no metrics. **Nothing is streamed until you subscribe to specific namespaces in
Better Stack**, which updates the stream's `IncludeFilters` via `cloudwatch:PutMetricStream`.
This is deliberate: an *empty* `IncludeFilters` would stream **every** namespace, so the
placeholder is what enforces opt-in.

> **Re-deploys reset subscriptions.** Because CloudFormation owns the stream, running
> `cloudformation deploy` again resets `IncludeFilters` back to the placeholder, dropping
> the namespaces Better Stack added until it re-syncs your subscriptions. (Previously a
> re-deploy reset the stream to "all namespaces" instead — the new default fails closed.)

```bash
aws cloudformation deploy \
  --template-file better-stack-full.yaml \
  --stack-name better-stack-full \
  --parameter-overrides \
    ClusterId=YOUR_CLUSTER_ID \
    SourceToken=YOUR_SOURCE_TOKEN \
    SourceId=YOUR_SOURCE_ID \
    EnableMetricStream=false \
  --capabilities CAPABILITY_NAMED_IAM
```

## CloudTrail Notes

Most AWS accounts already have CloudTrail enabled. Before enabling `EnableCloudTrail=true`, check for existing trails:

```bash
aws cloudtrail describe-trails
```

If a trail already exists, you may want to:
1. Keep `EnableCloudTrail=false` and manually subscribe the existing CloudTrail log group
2. Or enable it to create a dedicated trail for Better Stack

CloudTrail logs have a 1-day retention in CloudWatch (they are forwarded immediately to Better Stack).

## X-Ray Transaction Search

When `EnableXRayTransactionSearch=true`, the template automatically:
1. Creates a CloudWatch Logs resource policy allowing X-Ray to write spans
2. Enables Transaction Search via `AWS::XRay::TransactionSearchConfig`
3. X-Ray sends trace segments to the `aws/spans` CloudWatch Logs group

No manual steps required - fully automated via CloudFormation.

## Better Stack Integration Role

The template creates a `better-stack-integration-role` IAM role that allows Better Stack to:
- Read CloudWatch metrics and logs
- Manage metric streams and log subscriptions
- Discover AWS resources for context enrichment

After deployment, provide these values to Better Stack:
- **Role ARN**: From stack output `IntegrationRoleArn`
- **External ID**: From stack output `ExternalId`

## Tag Enrichment

When `EnableTagEnrichment=true`, [Lambda functions](../../lambda/README.md) enrich metrics and logs with AWS resource tags.

**Metrics enrichment** adds tags from:
- EC2 instances
- RDS databases
- Lambda functions
- DynamoDB tables
- S3 buckets
- ELB/ALB/NLB load balancers
- SQS queues
- SNS topics

**Logs enrichment** extracts resource info from log group/stream names:
- `/aws/lambda/{function-name}` -> Lambda tags
- `/aws/rds/instance/{db-instance}/{type}` -> RDS tags
- `RDSOSMetrics` (Enhanced Monitoring) -> RDS tags from message body
- `/ecs/{cluster}/...` -> ECS tags
- `/aws/api-gateway/{api-id}` -> API Gateway tags

See the [Lambda README](../../lambda/README.md) for implementation details. Lambda code is deployed from regional S3 buckets (`better-stack-lambda-{region}`).

## Stack Outputs

When `CreateGlobalResources=true`, the stack outputs:

| Output | Description |
|--------|-------------|
| `IntegrationRoleArn` | STS role ARN to provide to Better Stack |
| `ExternalId` | External ID to provide to Better Stack |

Better Stack will discover all other resources (Firehose streams, Metric Streams, etc.) via the STS integration role.

## Troubleshooting

### Check Firehose Delivery Errors

```bash
aws logs filter-log-events \
  --log-group-name /aws/kinesisfirehose/better-stack-metrics \
  --start-time $(date -d '1 hour ago' +%s000)
```

### Check Lambda Errors

```bash
aws logs filter-log-events \
  --log-group-name /aws/lambda/better-stack-metrics \
  --start-time $(date -d '1 hour ago' +%s000)
```

### Verify Metric Stream Status

Only applicable when `EnableMetricStream=true` (the default). With legacy API polling there is no metric stream to inspect.

```bash
aws cloudwatch get-metric-stream --name better-stack-metric-stream
```

## Cleanup

To delete the stack:

```bash
aws cloudformation delete-stack --stack-name better-stack-full
```

**Note on S3 buckets**: The Firehose backup and CloudTrail buckets are **retained** on stack deletion, so any failed-delivery data and CloudTrail logs are preserved. You do **not** need to empty or delete them before re-deploying — the stack creates each bucket, or adopts the existing one if a previous deployment left it behind, so a delete/re-deploy cycle just re-uses the same buckets.

Everything else (Lambdas, IAM roles, log groups, Firehose streams, etc.) is removed with the stack as normal.

If you want to remove the retained buckets permanently, delete them manually after deleting the stack:

```bash
# Delete Firehose backup bucket
aws s3 rb s3://better-stack-firehose-ACCOUNT_ID-REGION --force

# Delete CloudTrail bucket (if CloudTrail was enabled)
aws s3 rb s3://better-stack-cloudtrail-ACCOUNT_ID-REGION --force
```

## Support

For issues or questions, please reach out at hello@betterstack.com
