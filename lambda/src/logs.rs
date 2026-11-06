//! Enriches CloudWatch Logs with the tags of the resource that wrote them. Each Firehose record is
//! one gzipped CloudWatch Logs subscription message:
//!
//! ```json
//! {
//!   "messageType": "DATA_MESSAGE",
//!   "owner": "123456789012",
//!   "logGroup": "/ec2/cloudwatch-test/syslog",
//!   "logStream": "i-0123456789abcdef0",
//!   "subscriptionFilters": ["betterstack-logs"],
//!   "logEvents": [{ "id": "...", "timestamp": 1234567890123, "message": "log message" }]
//! }
//! ```
//!
//! The resource comes from the log group or stream name, and its tags are added as `tags`, plus
//! `resource_name`, `environment` and `team` from the common ones.

use std::io::{Read, Write};
use std::sync::LazyLock;

use base64::Engine;
use base64::engine::general_purpose::STANDARD as BASE64;
use flate2::Compression;
use flate2::read::GzDecoder;
use flate2::write::GzEncoder;
use regex::Regex;
use serde_json::Value;

use crate::config::Config;
use crate::firehose::{Event, InputRecord, OutputRecord, Response};
use crate::tags::{TagApi, TagCache, add_tags, unique};
use crate::{Error, Fields};

/// An EC2 instance id such as i-0123456789abcdef0, as a whole word.
static INSTANCE_ID: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)(?-u:\b)(i-[0-9a-f]{8,17})(?-u:\b)").expect("valid regex"));

pub struct LogsEnrichment {
    pub config: Config,
    pub tags: TagCache,
}

enum Decoded {
    Done(OutputRecord),
    Enrich {
        record: InputRecord,
        payload: Fields,
        arn: Option<String>,
    },
}

impl LogsEnrichment {
    pub fn new(config: Config) -> Self {
        Self {
            config,
            tags: TagCache::new(),
        }
    }

    pub async fn handle(&mut self, api: &impl TagApi, event: Event) -> Response {
        self.config.log(format!("Invoked with {} records", event.records.len()));
        let decoded: Vec<Decoded> = event.records.into_iter().map(|record| self.decode(record)).collect();

        let arns = unique(decoded.iter().map(|d| match d {
            Decoded::Enrich { arn, .. } => arn.clone(),
            Decoded::Done(_) => None,
        }));
        self.config.log(format!("Fetching tags for {} unique ARNs", arns.len()));
        self.tags.prefetch(api, &arns, &self.config).await;

        let records = decoded
            .into_iter()
            .map(|d| match d {
                Decoded::Done(output) => output,
                Decoded::Enrich {
                    record,
                    mut payload,
                    arn,
                } => {
                    add_tags(&mut payload, self.tags.get(arn.as_deref()));
                    match encode(&payload) {
                        Ok(data) => OutputRecord::ok(&record.record_id, data),
                        Err(e) => {
                            self.config
                                .log(format!("Error enriching record {}: {e}", record.record_id));
                            OutputRecord::unchanged(&record)
                        }
                    }
                }
            })
            .collect();
        Response { records }
    }

    fn decode(&self, record: InputRecord) -> Decoded {
        let payload = match decode(&record.data) {
            Ok(payload) => payload,
            Err(e) => {
                // Not a gzipped JSON object: passed through unchanged
                self.config
                    .log(format!("Passing record {} through: {e}", record.record_id));
                return Decoded::Done(OutputRecord::unchanged(&record));
            }
        };
        if payload.get("messageType").and_then(Value::as_str) == Some("CONTROL_MESSAGE") {
            self.config.log("Skipping control message");
            return Decoded::Done(OutputRecord::dropped(&record));
        }
        let arn = extract_resource_arn(&self.config, &payload);
        Decoded::Enrich { record, payload, arn }
    }
}

fn decode(data: &str) -> Result<Fields, Error> {
    let compressed = BASE64.decode(data)?;
    let mut json = Vec::new();
    GzDecoder::new(compressed.as_slice()).read_to_end(&mut json)?;
    match serde_json::from_slice(&json)? {
        Value::Object(payload) => Ok(payload),
        _ => Err("not a JSON object".into()),
    }
}

fn encode(payload: &Fields) -> Result<String, Error> {
    Ok(BASE64.encode(gzip_compress(&serde_json::to_vec(payload)?)?))
}

pub fn gzip_compress(data: &[u8]) -> std::io::Result<Vec<u8>> {
    let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
    encoder.write_all(data)?;
    encoder.finish()
}

pub fn extract_resource_arn(config: &Config, payload: &Fields) -> Option<String> {
    let log_group = payload.get("logGroup").and_then(Value::as_str).unwrap_or("");
    let log_stream = payload.get("logStream").and_then(Value::as_str).unwrap_or("");
    let Config { region, account_id, .. } = config;

    // RDS Enhanced Monitoring has the DB identifier inside the message body
    if log_group == "RDSOSMetrics" {
        return extract_rds_enhanced_monitoring_arn(config, payload);
    }
    // The instance id is most often the log stream
    if let Some(instance_id) = INSTANCE_ID
        .captures(log_stream)
        .or_else(|| INSTANCE_ID.captures(log_group))
    {
        return Some(format!(
            "arn:aws:ec2:{region}:{account_id}:instance/{}",
            &instance_id[1]
        ));
    }
    if let Some(function_name) = log_group.strip_prefix("/aws/lambda/") {
        return Some(format!("arn:aws:lambda:{region}:{account_id}:function:{function_name}"));
    }
    if let Some(db_instance) = log_group.strip_prefix("/aws/rds/instance/").and_then(first_segment) {
        return Some(format!("arn:aws:rds:{region}:{account_id}:db:{db_instance}"));
    }
    if let Some(cluster) = log_group.strip_prefix("/ecs/").and_then(first_segment) {
        return Some(format!("arn:aws:ecs:{region}:{account_id}:cluster/{cluster}"));
    }
    if log_group.starts_with("/aws/api-gateway/") || log_group.starts_with("/aws/http-api/") {
        let api_id = log_group.trim_end_matches('/').rsplit('/').next().unwrap_or("");
        return Some(format!("arn:aws:apigateway:{region}::/restapis/{api_id}"));
    }
    config.log(format!(
        "No resource ARN extracted from logGroup={log_group}, logStream={log_stream}"
    ));
    None
}

/// "db/postgresql" is "db". Nothing after the prefix is no resource.
fn first_segment(rest: &str) -> Option<&str> {
    (!rest.is_empty()).then(|| rest.split('/').next().unwrap_or(""))
}

/// Enhanced Monitoring messages look like `{ "engine": "POSTGRES", "instanceID": "my-db", ... }`.
fn extract_rds_enhanced_monitoring_arn(config: &Config, payload: &Fields) -> Option<String> {
    let first = payload.get("logEvents")?.as_array()?.first()?;
    let message = match first.get("message")? {
        Value::String(text) => match serde_json::from_str::<Value>(text) {
            Ok(parsed) => parsed,
            Err(e) => {
                config.log(format!("Failed to parse RDSOSMetrics message: {e}"));
                return None;
            }
        },
        parsed => parsed.clone(),
    };
    let instance_id = match message.get("instanceID")? {
        Value::Null | Value::Bool(false) => return None,
        Value::String(id) => id.clone(),
        other => other.to_string(),
    };
    Some(format!(
        "arn:aws:rds:{}:{}:db:{instance_id}",
        config.region, config.account_id
    ))
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, Instant};

    use serde_json::json;

    use super::*;
    use crate::config::test_config;
    use crate::fake::{FakeApi, fields};

    fn arn_of(payload: Value) -> Option<String> {
        extract_resource_arn(&test_config(), payload.as_object().unwrap())
    }

    #[test]
    fn extracts_instance_id_from_log_stream() {
        assert_eq!(
            arn_of(json!({ "logGroup": "/ec2/syslog", "logStream": "i-1234567890abcdef0" })).as_deref(),
            Some("arn:aws:ec2:us-east-1:123456789012:instance/i-1234567890abcdef0")
        );
    }

    #[test]
    fn extracts_instance_id_from_log_group() {
        assert_eq!(
            arn_of(json!({ "logGroup": "/ec2/i-abcdef1234567890/syslog", "logStream": "some-stream" })).as_deref(),
            Some("arn:aws:ec2:us-east-1:123456789012:instance/i-abcdef1234567890")
        );
    }

    #[test]
    fn ignores_instance_ids_inside_longer_words() {
        assert_eq!(
            arn_of(json!({ "logGroup": "/custom", "logStream": "xi-1234567890abcdef0" })),
            None
        );
        assert_eq!(
            arn_of(json!({ "logGroup": "/custom", "logStream": "i-1234567890abcdef0123" })),
            None
        );
    }

    #[test]
    fn extracts_lambda_function_from_log_group() {
        assert_eq!(
            arn_of(json!({ "logGroup": "/aws/lambda/my-function", "logStream": "2024/01/01/[$LATEST]abc123" }))
                .as_deref(),
            Some("arn:aws:lambda:us-east-1:123456789012:function:my-function")
        );
    }

    #[test]
    fn extracts_rds_instance_from_log_group() {
        assert_eq!(
            arn_of(json!({ "logGroup": "/aws/rds/instance/my-database/postgresql", "logStream": "some-stream" }))
                .as_deref(),
            Some("arn:aws:rds:us-east-1:123456789012:db:my-database")
        );
    }

    #[test]
    fn extracts_ecs_cluster_from_log_group() {
        assert_eq!(
            arn_of(json!({ "logGroup": "/ecs/my-cluster/my-service", "logStream": "ecs/container/abc123" })).as_deref(),
            Some("arn:aws:ecs:us-east-1:123456789012:cluster/my-cluster")
        );
    }

    #[test]
    fn extracts_api_gateway_from_log_group() {
        assert_eq!(
            arn_of(json!({ "logGroup": "/aws/api-gateway/abc123xyz", "logStream": "some-stream" })).as_deref(),
            Some("arn:aws:apigateway:us-east-1::/restapis/abc123xyz")
        );
    }

    #[test]
    fn returns_none_when_no_resource_can_be_extracted() {
        assert_eq!(
            arn_of(json!({ "logGroup": "/custom/application/logs", "logStream": "app-stream" })),
            None
        );
    }

    #[test]
    fn extracts_rds_instance_id_from_enhanced_monitoring_message() {
        let payload = json!({
            "logGroup": "RDSOSMetrics",
            "logStream": "db-ABC123",
            "logEvents": [{ "id": "123", "timestamp": 1_704_067_200_000_u64, "message": r#"{"engine":"POSTGRES","instanceID":"my-database","uptime":"1234"}"# }]
        });

        assert_eq!(
            arn_of(payload).as_deref(),
            Some("arn:aws:rds:us-east-1:123456789012:db:my-database")
        );
    }

    #[test]
    fn returns_none_when_enhanced_monitoring_message_is_not_json() {
        let payload = json!({
            "logGroup": "RDSOSMetrics",
            "logStream": "db-ABC123",
            "logEvents": [{ "id": "123", "timestamp": 1_704_067_200_000_u64, "message": "not json" }]
        });

        assert_eq!(arn_of(payload), None);
    }

    #[test]
    fn returns_none_without_enhanced_monitoring_events() {
        assert_eq!(
            arn_of(json!({ "logGroup": "RDSOSMetrics", "logStream": "db-ABC123", "logEvents": [] })),
            None
        );
    }

    #[test]
    fn gzip_round_trips() {
        let compressed = gzip_compress(br#"{"test": "data"}"#).unwrap();

        let mut decompressed = String::new();
        GzDecoder::new(compressed.as_slice())
            .read_to_string(&mut decompressed)
            .unwrap();
        assert_eq!(decompressed, r#"{"test": "data"}"#);
    }

    fn record(id: &str, payload: &str) -> InputRecord {
        InputRecord {
            record_id: id.to_string(),
            data: BASE64.encode(gzip_compress(payload.as_bytes()).unwrap()),
        }
    }

    fn decoded(output: &OutputRecord) -> String {
        let mut json = String::new();
        GzDecoder::new(BASE64.decode(&output.data).unwrap().as_slice())
            .read_to_string(&mut json)
            .unwrap();
        json
    }

    #[tokio::test]
    async fn enriches_records_drops_control_messages_and_passes_the_rest_through() {
        let api = FakeApi::default();
        api.tags(
            "arn:aws:ec2:us-east-1:123456789012:instance/i-0123456789abcdef0",
            &[("Name", "web-1"), ("Environment", "production"), ("Team", "platform")],
        );
        let mut enrichment = LogsEnrichment::new(test_config());
        let not_gzip = InputRecord {
            record_id: "3".to_string(),
            data: BASE64.encode("plain text"),
        };
        let event = Event {
            records: vec![
                record(
                    "1",
                    r#"{"messageType":"DATA_MESSAGE","logGroup":"/ec2/syslog","logStream":"i-0123456789abcdef0","logEvents":[{"id":"a","timestamp":1,"message":"hello"}]}"#,
                ),
                record(
                    "2",
                    r#"{"messageType":"CONTROL_MESSAGE","logGroup":"","logStream":"","logEvents":[]}"#,
                ),
                not_gzip.clone(),
                record(
                    "4",
                    r#"{"messageType":"DATA_MESSAGE","logGroup":"/custom/app","logStream":"app","logEvents":[]}"#,
                ),
            ],
        };

        let response = enrichment.handle(&api, event).await;

        let results: Vec<(&str, &str)> = response
            .records
            .iter()
            .map(|r| (r.record_id.as_str(), r.result))
            .collect();
        assert_eq!(results, [("1", "Ok"), ("2", "Dropped"), ("3", "Ok"), ("4", "Ok")]);
        assert_eq!(
            decoded(&response.records[0]),
            r#"{"messageType":"DATA_MESSAGE","logGroup":"/ec2/syslog","logStream":"i-0123456789abcdef0","logEvents":[{"id":"a","timestamp":1,"message":"hello"}],"tags":{"Name":"web-1","Environment":"production","Team":"platform"},"resource_name":"web-1","environment":"production","team":"platform"}"#
        );
        assert_eq!(response.records[2].data, not_gzip.data);
        assert_eq!(
            decoded(&response.records[3]),
            r#"{"messageType":"DATA_MESSAGE","logGroup":"/custom/app","logStream":"app","logEvents":[]}"#
        );
        assert_eq!(api.calls("get_resources"), 1);
    }

    #[tokio::test]
    async fn uses_cached_tags_on_the_next_invocation() {
        let api = FakeApi::default();
        let mut enrichment = LogsEnrichment::new(test_config());
        enrichment.tags.insert(
            "arn:aws:lambda:us-east-1:123456789012:function:my-function".to_string(),
            fields(&[("Team", "payments")]),
            Instant::now() + Duration::from_secs(600),
        );
        let event = Event {
            records: vec![record(
                "1",
                r#"{"logGroup":"/aws/lambda/my-function","logStream":"s","logEvents":[]}"#,
            )],
        };

        let response = enrichment.handle(&api, event).await;

        assert_eq!(
            decoded(&response.records[0]),
            r#"{"logGroup":"/aws/lambda/my-function","logStream":"s","logEvents":[],"tags":{"Team":"payments"},"team":"payments"}"#
        );
        assert_eq!(api.calls("get_resources"), 0);
    }
}
