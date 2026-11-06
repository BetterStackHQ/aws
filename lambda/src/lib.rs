//! Firehose transformation Lambdas that enrich CloudWatch metrics and logs with AWS resource tags
//! and properties before delivery to Better Stack. Both run on the `provided.al2023` runtime:
//! each binary in `src/bin/` is zipped as `bootstrap`.

pub mod aws;
pub mod config;
pub mod firehose;
pub mod logs;
pub mod metrics;
pub mod tags;

#[cfg(test)]
mod fake;

pub type Error = Box<dyn std::error::Error + Send + Sync>;

/// Tags or properties of one resource, in the order AWS returned them.
pub type Fields = serde_json::Map<String, serde_json::Value>;
