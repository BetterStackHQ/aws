# Better Stack AWS Integration

This repository contains CloudFormation stacks and Lambda functions for streaming AWS CloudWatch data to Better Stack.

## Repository Structure

| Directory | Description |
|-----------|-------------|
| [cloudformation/full/](cloudformation/full/README.md) | CloudFormation template and deployment guide |
| [lambda/](lambda/README.md) | Firehose transformation Lambda functions |

## Getting Started

To get started with Better Stack on AWS, [create a new AWS Source in Better Stack](https://telemetry.betterstack.com/team/t0/sources/new?platform=aws).

For deployment instructions, see the [CloudFormation deployment guide](cloudformation/full/README.md).

## Ingested Data

When you deploy our CloudFormation stack you get:

- Automatic ingestion of all CloudWatch metrics into Better Stack.
  - Support for RDS Enhanced Metrics (RDSOS)
- Detection and per-log-group optional ingestion of all CloudWatch log groups.
- Automatic integration with AWS X-Ray for trace ingestion.
- Optional CloudTrail audit log forwarding.
