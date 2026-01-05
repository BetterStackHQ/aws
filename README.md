# Better Stack AWS Integration

This repository contains all Better Stack CloudFormation stacks and Lambdas for the Better Stack AWS Integration.

## Getting Started

To get started with Better Stack on AWS, [create a new AWS Source in Better Stack Telemetry](https://telemetry.betterstack.com/team/t0/sources/new?platform=aws).

## Ingested Data

When you deploy our CloudFormation stack you get:

- Automatic ingestion of all CloudWatch metrics into Better Stack.
  - Support for RDS Enhanced Metrics (RDSOS)
- Detection and per-log-group optional ingestion of all Cloudwatch log groups.
- Automatic integration with AWS X-Ray, ingest traces into Better Stack Telemetry and view your traces.
