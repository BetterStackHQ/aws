# Lambda Functions

This directory contains AWS Lambda functions for Kinesis Firehose data transformation.

## Functions

| Function | Description |
|----------|-------------|
| `firehose_logs_tag_enrichment.rb` | Enriches CloudWatch Logs with AWS resource tags before delivery |
| `firehose_metrics_tag_enrichment.rb` | Enriches CloudWatch Metrics with AWS resource tags before delivery |

## Deployment

These Lambda functions are automatically provisioned by the CloudFormation stack when `EnableTagEnrichment` is set to `true`. The stack handles packaging, deployment, and IAM permissions required for the Resource Groups Tagging API integration.

Lambda packages are hosted in regional S3 buckets following the pattern:

```
s3://better-stack-lambda-${AWS::Region}/<function>.zip
```

For example, in `us-east-1`:
- `s3://better-stack-lambda-us-east-1/firehose_metrics_tag_enrichment.zip`
- `s3://better-stack-lambda-us-east-1/firehose_logs_tag_enrichment.zip`
