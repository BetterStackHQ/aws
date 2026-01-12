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

When deploying to additional regions, set `CreateGlobalResources=false` to reuse the IAM roles created in the first region:

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
| Metrics | CloudWatch Metric Stream (JSON) -> Firehose -> Lambda (enrichment) -> Better Stack |
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

### Feature Toggles

| Parameter | Default | Description |
|-----------|---------|-------------|
| `EnableTagEnrichment` | `true` | Enable Lambda-based tag enrichment for metrics and logs |
| `EnableCloudTrail` | `true` | Create a new CloudTrail trail and forward to Better Stack |
| `EnableXRayTransactionSearch` | `true` | Enable X-Ray Transaction Search and forward spans |

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

```bash
aws cloudwatch get-metric-stream --name better-stack-metric-stream
```

## Cleanup

To delete the stack:

```bash
aws cloudformation delete-stack --stack-name better-stack-full
```

**Note**: S3 buckets have `DeletionPolicy: Retain` to preserve any failed delivery data and CloudTrail logs. Delete manually if needed:

```bash
# Delete Firehose backup bucket
aws s3 rb s3://better-stack-firehose-ACCOUNT_ID-REGION --force

# Delete CloudTrail bucket (if CloudTrail was enabled)
aws s3 rb s3://better-stack-cloudtrail-ACCOUNT_ID-REGION --force
```

## Support

For issues or questions, please reach out at hello@betterstack.com
