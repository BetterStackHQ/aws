# Lambda Functions

Firehose transformation Lambda functions that enrich CloudWatch data with AWS resource tags and properties before delivery to Better Stack. They are written in Rust and run on the OS-only `provided.al2023` runtime on arm64: each binary is zipped as `bootstrap`.

See the [main README](../README.md) for an overview or the [CloudFormation deployment guide](../cloudformation/full/README.md) for deployment instructions.

## Functions

### firehose_metrics_tag_enrichment

Enriches CloudWatch Metric Stream data with resource tags and properties.

**Input:** Base64-encoded NDJSON (newline-delimited JSON) from Firehose
**Output:** Enriched NDJSON with `tags` and `properties` fields added

**Supported Namespaces:**
- `AWS/EC2` - Instance tags + properties (type, family, size, architecture, AZ, lifecycle)
- `AWS/EBS` - Volume tags + properties (type, size, IOPS, throughput)
- `AWS/RDS` - Database tags + properties (class, engine, version, storage type)
- `AWS/Lambda` - Function tags + properties (runtime, memory, timeout, architecture)
- `AWS/DynamoDB` - Table tags
- `AWS/SQS` - Queue tags
- `AWS/SNS` - Topic tags
- `AWS/S3` - Bucket tags
- `AWS/ELB`, `AWS/ApplicationELB`, `AWS/NetworkELB` - Load balancer tags

### firehose_logs_tag_enrichment

Enriches CloudWatch Logs with resource tags extracted from log group/stream names.

**Input:** Base64-encoded gzip-compressed CloudWatch Logs subscription data
**Output:** Enriched logs with `tags`, `resource_name`, `environment`, and `team` fields

**Supported Log Patterns:**
- `/aws/lambda/{function-name}` - Lambda function tags
- `/aws/rds/instance/{db-instance}/{type}` - RDS instance tags
- `RDSOSMetrics` - RDS Enhanced Monitoring (extracts `instanceID` from message body)
- `/ecs/{cluster}/...` - ECS cluster tags
- `/aws/api-gateway/{api-id}` - API Gateway tags
- Log streams containing `i-xxxxxxxxx` - EC2 instance tags

## Configuration

Environment variables (set via CloudFormation):

| Variable | Default | Description |
|----------|---------|-------------|
| `CACHE_TTL_MINUTES` | `10` | How long to cache tags/properties before refreshing |
| `ACCOUNT_ID` | (required) | AWS account ID for ARN construction |
| `AWS_REGION` | `us-east-1` | AWS region (auto-set by Lambda runtime) |
| `DEBUG` | `false` | Enable verbose logging |

## Architecture

```
CloudWatch Metrics ─┐
                    ├─> Firehose ─> Lambda (enrichment) ─> Firehose -> Better Stack
CloudWatch Logs ────┘
```

Both functions:
1. Receive batched records from Kinesis Firehose
2. Extract resource identifiers (ARNs) from the data
3. Batch-fetch tags via the Resource Groups Tagging API (max 100 ARNs per call)
4. Cache tags in-memory to minimize API calls
5. Return enriched records to Firehose for delivery

## Local Development

The crate is `lambda/`: `src/logs.rs` and `src/metrics.rs` hold each function's logic, `src/tags.rs` the shared tag cache, `src/aws.rs` the AWS SDK calls, and `src/bin/` the two Lambda entry points.

### Running Tests

```bash
cd lambda
cargo test
```

The tests replace the AWS APIs with an in-memory fake (`src/fake.rs`), so they need no credentials.

### Building the zips

```bash
lambda/build.sh
```

It builds both functions inside Amazon Linux 2023 on an arm64 host with Docker, so the binaries link against the glibc the Lambda runtime has, and writes `lambda/dist/firehose_logs_tag_enrichment.zip` and `lambda/dist/firehose_metrics_tag_enrichment.zip`. The Tests workflow builds them on every pull request and keeps them as the `lambda-zips` artifact.

## Deployment

The template takes the code from `rust/<function>.zip` in the regional S3 bucket `better-stack-lambda-{region}`, or from the bucket in its `LambdaCodeBucket` parameter when that is set. Releasing new code means uploading both zips under `rust/` to every regional bucket before publishing a template that needs them. The E2E workflow uploads the zips it builds to a bucket of its own and passes it as `LambdaCodeBucket`. See the [CloudFormation README](../cloudformation/full/README.md) for deployment commands.
