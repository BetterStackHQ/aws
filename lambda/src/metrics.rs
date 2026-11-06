//! Enriches CloudWatch Metric Stream records with the tags and properties of the resource they
//! measure. Each Firehose record is newline-delimited JSON, one metric per line. Tags come from the
//! Resource Groups Tagging API; properties (instance type, engine, runtime, ...) for EC2, EBS, RDS
//! and Lambda come from bulk listings cached in memory, with one-off lookups for resources the
//! listing missed.

use std::collections::HashMap;
use std::future::Future;
use std::time::{Duration, Instant};

use base64::Engine;
use base64::engine::general_purpose::STANDARD as BASE64;
use serde_json::Value;

use crate::config::Config;
use crate::firehose::{Event, InputRecord, OutputRecord, Response};
use crate::tags::{TagApi, TagCache, add_tags, unique};
use crate::{Error, Fields};

/// After a listing fails it is not retried for this long, to avoid a retry storm.
const ERROR_TTL: Duration = Duration::from_secs(60);

/// Each call returns `(resource id, properties)` pairs. `id: None` lists every running instance,
/// in-use volume or DB instance; `Some` looks up that one resource.
pub trait MetricsApi: TagApi {
    fn describe_instances(&self, id: Option<&str>)
    -> impl Future<Output = Result<Vec<(String, Fields)>, Error>> + Send;
    fn describe_volumes(&self, id: Option<&str>) -> impl Future<Output = Result<Vec<(String, Fields)>, Error>> + Send;
    fn describe_db_instances(
        &self,
        id: Option<&str>,
    ) -> impl Future<Output = Result<Vec<(String, Fields)>, Error>> + Send;
    fn list_functions(&self) -> impl Future<Output = Result<Vec<(String, Fields)>, Error>> + Send;
    fn get_function(&self, name: &str) -> impl Future<Output = Result<Fields, Error>> + Send;
}

/// Properties by resource id. A failed one-off lookup caches empty properties, so it is not repeated.
#[derive(Default)]
pub struct PropertyCache {
    pub items: HashMap<String, Fields>,
    pub loaded: bool,
    pub expires: Option<Instant>,
}

impl PropertyCache {
    fn fresh(&self) -> bool {
        self.loaded && self.expires.is_some_and(|expires| expires > Instant::now())
    }
}

pub struct MetricsEnrichment {
    pub config: Config,
    pub tags: TagCache,
    /// Instances and volumes are listed together and share `loaded` and `expires`, kept on `instances`.
    pub instances: PropertyCache,
    pub volumes: PropertyCache,
    pub db_instances: PropertyCache,
    pub functions: PropertyCache,
}

enum Decoded {
    Done(OutputRecord),
    Enrich {
        record: InputRecord,
        metrics: Vec<(Fields, Option<String>)>,
    },
}

impl MetricsEnrichment {
    pub fn new(config: Config) -> Self {
        Self {
            config,
            tags: TagCache::new(),
            instances: PropertyCache::default(),
            volumes: PropertyCache::default(),
            db_instances: PropertyCache::default(),
            functions: PropertyCache::default(),
        }
    }

    pub async fn handle(&mut self, api: &impl MetricsApi, event: Event) -> Response {
        self.config.log(format!("Invoked with {} records", event.records.len()));
        let decoded: Vec<Decoded> = event.records.into_iter().map(|record| self.decode(record)).collect();

        let arns = unique(decoded.iter().flat_map(|d| match d {
            Decoded::Enrich { metrics, .. } => metrics.iter().map(|(_, arn)| arn.clone()).collect(),
            Decoded::Done(_) => Vec::new(),
        }));
        self.config.log(format!("Fetching tags for {} unique ARNs", arns.len()));
        self.tags.prefetch(api, &arns, &self.config).await;

        let mut records = Vec::with_capacity(decoded.len());
        for d in decoded {
            records.push(match d {
                Decoded::Done(output) => output,
                Decoded::Enrich { record, metrics } => {
                    let mut lines = String::new();
                    for (mut metric, arn) in metrics {
                        self.enrich_record(api, &mut metric, arn.as_deref()).await;
                        lines.push_str(&Value::Object(metric).to_string());
                        lines.push('\n');
                    }
                    OutputRecord::ok(&record.record_id, BASE64.encode(lines))
                }
            });
        }
        Response { records }
    }

    fn decode(&self, record: InputRecord) -> Decoded {
        let raw = match BASE64.decode(&record.data) {
            Ok(raw) => raw,
            Err(e) => {
                self.config
                    .log(format!("Passing record {} through: {e}", record.record_id));
                return Decoded::Done(OutputRecord::unchanged(&record));
            }
        };
        let mut metrics = Vec::new();
        for line in raw.split(|b| *b == b'\n') {
            if line.iter().all(|b| b.is_ascii_whitespace() || *b == 0) {
                continue;
            }
            match serde_json::from_slice::<Value>(line) {
                Ok(Value::Object(metric)) => {
                    let arn = extract_resource_arn(&self.config, &metric);
                    metrics.push((metric, arn));
                }
                Ok(_) => {
                    self.config.log(format!(
                        "Passing record {} through: a line is not a JSON object",
                        record.record_id
                    ));
                    return Decoded::Done(OutputRecord::unchanged(&record));
                }
                // A line that isn't JSON is left out, as CloudWatch never sends one
                Err(e) => self.config.log(format!("JSON parse error: {e}")),
            }
        }
        if metrics.is_empty() {
            return Decoded::Done(OutputRecord::unchanged(&record));
        }
        Decoded::Enrich { record, metrics }
    }

    /// Adds `tags` with the common ones as top-level fields, and the resource's `properties`.
    pub async fn enrich_record(&mut self, api: &impl MetricsApi, metric: &mut Fields, arn: Option<&str>) {
        add_tags(metric, self.tags.get(arn));
        if let Some(properties) = self.properties_for_metric(api, metric).await.filter(|p| !p.is_empty()) {
            metric.insert("properties".to_string(), Value::Object(properties));
        }
    }

    pub async fn properties_for_metric(&mut self, api: &impl MetricsApi, metric: &Fields) -> Option<Fields> {
        let namespace = metric.get("namespace").and_then(Value::as_str).unwrap_or("");
        let dimension = |name: &str| {
            metric
                .get("dimensions")
                .and_then(|d| d.get(name))
                .and_then(Value::as_str)
                .map(String::from)
        };
        match namespace {
            "AWS/EC2" => {
                let id = dimension("InstanceId")?;
                self.load_ec2(api).await;
                self.backfill_instance(api, &id).await;
                self.instances.items.get(&id).cloned()
            }
            "AWS/EBS" => {
                let id = dimension("VolumeId")?;
                self.load_ec2(api).await;
                self.backfill_volume(api, &id).await;
                self.volumes.items.get(&id).cloned()
            }
            "AWS/RDS" => {
                let id = dimension("DBInstanceIdentifier")?;
                self.load_rds(api).await;
                self.backfill_db_instance(api, &id).await;
                self.db_instances.items.get(&id).cloned()
            }
            "AWS/Lambda" => {
                let name = dimension("FunctionName")?;
                self.load_functions(api).await;
                self.backfill_function(api, &name).await;
                self.functions.items.get(&name).cloned()
            }
            _ => None,
        }
    }

    /// Lists running instances and in-use volumes, replacing the caches only when both succeed.
    pub async fn load_ec2(&mut self, api: &impl MetricsApi) {
        if self.instances.fresh() {
            return;
        }
        self.config.log("Loading EC2 instances and EBS volumes");
        let listed = async { Ok::<_, Error>((api.describe_instances(None).await?, api.describe_volumes(None).await?)) };
        match listed.await {
            Ok((instances, volumes)) => {
                self.config.log(format!(
                    "Cached {} EC2 instances, {} EBS volumes",
                    instances.len(),
                    volumes.len()
                ));
                self.instances.items = instances.into_iter().collect();
                self.volumes.items = volumes.into_iter().collect();
                self.instances.expires = Some(Instant::now() + self.config.cache_ttl);
            }
            Err(e) => {
                self.config.log(format!("Error loading EC2 properties: {e}"));
                self.instances.expires = Some(Instant::now() + ERROR_TTL);
            }
        }
        self.instances.loaded = true;
    }

    pub async fn load_rds(&mut self, api: &impl MetricsApi) {
        let config = self.config.clone();
        load(
            &mut self.db_instances,
            &config,
            "RDS instances",
            api.describe_db_instances(None),
        )
        .await;
    }

    pub async fn load_functions(&mut self, api: &impl MetricsApi) {
        let config = self.config.clone();
        load(&mut self.functions, &config, "Lambda functions", api.list_functions()).await;
    }

    pub async fn backfill_instance(&mut self, api: &impl MetricsApi, id: &str) {
        let config = self.config.clone();
        backfill(&mut self.instances, &config, id, |id| api.describe_instances(Some(id))).await;
    }

    pub async fn backfill_volume(&mut self, api: &impl MetricsApi, id: &str) {
        let config = self.config.clone();
        backfill(&mut self.volumes, &config, id, |id| api.describe_volumes(Some(id))).await;
    }

    pub async fn backfill_db_instance(&mut self, api: &impl MetricsApi, id: &str) {
        let config = self.config.clone();
        backfill(&mut self.db_instances, &config, id, |id| {
            api.describe_db_instances(Some(id))
        })
        .await;
    }

    pub async fn backfill_function(&mut self, api: &impl MetricsApi, name: &str) {
        let config = self.config.clone();
        backfill(&mut self.functions, &config, name, |name| async move {
            Ok(vec![(name.to_string(), api.get_function(name).await?)])
        })
        .await;
    }
}

/// Replaces the cache with a fresh listing, unless it is still fresh. On error the old entries stay.
async fn load(
    cache: &mut PropertyCache,
    config: &Config,
    what: &str,
    listing: impl Future<Output = Result<Vec<(String, Fields)>, Error>>,
) {
    if cache.fresh() {
        return;
    }
    config.log(format!("Loading {what}"));
    match listing.await {
        Ok(items) => {
            config.log(format!("Cached {} {what}", items.len()));
            cache.items = items.into_iter().collect();
            cache.expires = Some(Instant::now() + config.cache_ttl);
        }
        Err(e) => {
            config.log(format!("Error loading {what}: {e}"));
            cache.expires = Some(Instant::now() + ERROR_TTL);
        }
    }
    cache.loaded = true;
}

/// Looks up one resource the listing missed. A failure caches empty properties.
async fn backfill<'a, F, Fut>(cache: &mut PropertyCache, config: &Config, id: &'a str, lookup: F)
where
    F: FnOnce(&'a str) -> Fut,
    Fut: Future<Output = Result<Vec<(String, Fields)>, Error>>,
{
    if cache.items.contains_key(id) {
        return;
    }
    config.log(format!("Backfilling {id}"));
    match lookup(id).await {
        Ok(found) => cache.items.extend(found),
        Err(e) => {
            config.log(format!("Error backfilling {id}: {e}"));
            cache.items.insert(id.to_string(), Fields::new());
        }
    }
}

/// The ARN of the resource a metric measures, from the dimension its namespace uses.
pub fn extract_resource_arn(config: &Config, metric: &Fields) -> Option<String> {
    let namespace = metric.get("namespace").and_then(Value::as_str).unwrap_or("");
    let dimension = |name: &str| {
        metric
            .get("dimensions")
            .and_then(|d| d.get(name))
            .and_then(Value::as_str)
    };
    let Config { region, account_id, .. } = config;
    let arn = match namespace {
        "AWS/EC2" => dimension("InstanceId").map(|id| format!("arn:aws:ec2:{region}:{account_id}:instance/{id}")),
        "AWS/RDS" => dimension("DBInstanceIdentifier").map(|id| format!("arn:aws:rds:{region}:{account_id}:db:{id}")),
        "AWS/Lambda" => {
            dimension("FunctionName").map(|name| format!("arn:aws:lambda:{region}:{account_id}:function:{name}"))
        }
        "AWS/DynamoDB" => {
            dimension("TableName").map(|name| format!("arn:aws:dynamodb:{region}:{account_id}:table/{name}"))
        }
        "AWS/SQS" => dimension("QueueName").map(|name| format!("arn:aws:sqs:{region}:{account_id}:{name}")),
        "AWS/SNS" => dimension("TopicName").map(|name| format!("arn:aws:sns:{region}:{account_id}:{name}")),
        "AWS/S3" => dimension("BucketName").map(|name| format!("arn:aws:s3:::{name}")),
        "AWS/EBS" => dimension("VolumeId").map(|id| format!("arn:aws:ec2:{region}:{account_id}:volume/{id}")),
        "AWS/ELB" | "AWS/ApplicationELB" | "AWS/NetworkELB" => dimension("LoadBalancer")
            .or_else(|| dimension("LoadBalancerName"))
            .map(|name| format!("arn:aws:elasticloadbalancing:{region}:{account_id}:loadbalancer/{name}")),
        _ => None,
    };
    if arn.is_none() {
        config.log(format!("No ARN extracted for namespace {namespace}"));
    }
    arn
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::config::test_config;
    use crate::fake::{FakeApi, fields};

    fn metric(value: Value) -> Fields {
        value.as_object().unwrap().clone()
    }

    fn fresh(items: &[(&str, Fields)]) -> PropertyCache {
        PropertyCache {
            items: items.iter().map(|(id, p)| (id.to_string(), p.clone())).collect(),
            loaded: true,
            expires: Some(Instant::now() + Duration::from_secs(600)),
        }
    }

    #[tokio::test]
    async fn loads_running_instances_and_in_use_volumes() {
        let api = FakeApi::default();
        api.instances(
            None,
            vec![(
                "i-1234567890abcdef0".to_string(),
                fields(&[("InstanceType", "t3.micro")]),
            )],
        );
        api.volumes(
            None,
            vec![("vol-1234567890abcdef0".to_string(), fields(&[("VolumeType", "gp3")]))],
        );
        let mut enrichment = MetricsEnrichment::new(test_config());

        enrichment.load_ec2(&api).await;

        assert!(enrichment.instances.loaded);
        assert_eq!(
            enrichment.instances.items["i-1234567890abcdef0"],
            fields(&[("InstanceType", "t3.micro")])
        );
        assert_eq!(
            enrichment.volumes.items["vol-1234567890abcdef0"],
            fields(&[("VolumeType", "gp3")])
        );
    }

    #[tokio::test]
    async fn sets_a_short_ttl_when_loading_fails() {
        let api = FakeApi::default();
        api.fail("describe_instances");
        let mut enrichment = MetricsEnrichment::new(test_config());

        enrichment.load_ec2(&api).await;

        assert!(enrichment.instances.loaded);
        let left = enrichment.instances.expires.unwrap() - Instant::now();
        assert!(
            left > Duration::from_secs(55) && left <= Duration::from_secs(60),
            "{left:?}"
        );
    }

    #[tokio::test]
    async fn does_not_reload_a_fresh_listing() {
        let api = FakeApi::default();
        let mut enrichment = MetricsEnrichment::new(test_config());
        enrichment.instances = fresh(&[]);

        enrichment.load_ec2(&api).await;

        assert_eq!(api.calls("describe_instances"), 0);
    }

    #[tokio::test]
    async fn caches_a_backfilled_instance() {
        let api = FakeApi::default();
        api.instances(
            Some("i-newinstance"),
            vec![("i-newinstance".to_string(), fields(&[("InstanceType", "t3.small")]))],
        );
        let mut enrichment = MetricsEnrichment::new(test_config());
        enrichment.instances = fresh(&[]);

        enrichment.backfill_instance(&api, "i-newinstance").await;

        assert_eq!(
            enrichment.instances.items["i-newinstance"],
            fields(&[("InstanceType", "t3.small")])
        );
    }

    #[tokio::test]
    async fn caches_empty_properties_when_a_backfill_fails() {
        let api = FakeApi::default();
        api.fail("describe_instances");
        let mut enrichment = MetricsEnrichment::new(test_config());
        enrichment.instances = fresh(&[]);

        enrichment.backfill_instance(&api, "i-terminated").await;

        assert_eq!(enrichment.instances.items["i-terminated"], Fields::new());
    }

    #[tokio::test]
    async fn does_not_refetch_cached_instances() {
        let api = FakeApi::default();
        let mut enrichment = MetricsEnrichment::new(test_config());
        enrichment.instances = fresh(&[("i-cached", fields(&[("InstanceType", "t3.large")]))]);

        enrichment.backfill_instance(&api, "i-cached").await;

        assert_eq!(api.calls("describe_instances"), 0);
    }

    #[tokio::test]
    async fn backfills_a_function_the_listing_missed() {
        let api = FakeApi::default();
        api.function("my-function", fields(&[("Runtime", "provided.al2023")]));
        let mut enrichment = MetricsEnrichment::new(test_config());

        let properties = enrichment
            .properties_for_metric(
                &api,
                &metric(json!({ "namespace": "AWS/Lambda", "dimensions": { "FunctionName": "my-function" } })),
            )
            .await;

        assert_eq!(properties, Some(fields(&[("Runtime", "provided.al2023")])));
        assert_eq!(api.calls("list_functions"), 1);
    }

    #[tokio::test]
    async fn returns_ec2_properties_for_the_ec2_namespace() {
        let api = FakeApi::default();
        let mut enrichment = MetricsEnrichment::new(test_config());
        enrichment.instances = fresh(&[("i-test", fields(&[("InstanceType", "t3.nano")]))]);

        let properties = enrichment
            .properties_for_metric(
                &api,
                &metric(json!({ "namespace": "AWS/EC2", "dimensions": { "InstanceId": "i-test" } })),
            )
            .await;

        assert_eq!(properties, Some(fields(&[("InstanceType", "t3.nano")])));
    }

    #[tokio::test]
    async fn returns_no_properties_for_an_unknown_namespace() {
        let api = FakeApi::default();
        let mut enrichment = MetricsEnrichment::new(test_config());

        let properties = enrichment
            .properties_for_metric(&api, &metric(json!({ "namespace": "AWS/Unknown", "dimensions": {} })))
            .await;

        assert_eq!(properties, None);
    }

    #[tokio::test]
    async fn adds_both_tags_and_properties() {
        let api = FakeApi::default();
        let mut enrichment = MetricsEnrichment::new(test_config());
        enrichment.instances = fresh(&[(
            "i-enriched",
            fields(&[
                ("InstanceType", "t3.medium"),
                ("InstanceFamily", "t3"),
                ("InstanceSize", "medium"),
            ]),
        )]);
        enrichment.tags.insert(
            "arn:aws:ec2:us-east-1:123456789012:instance/i-enriched".to_string(),
            fields(&[("Name", "test-server"), ("Environment", "test")]),
            Instant::now() + Duration::from_secs(600),
        );
        let mut payload = metric(
            json!({ "namespace": "AWS/EC2", "metric_name": "CPUUtilization", "dimensions": { "InstanceId": "i-enriched" } }),
        );

        enrichment
            .enrich_record(
                &api,
                &mut payload,
                Some("arn:aws:ec2:us-east-1:123456789012:instance/i-enriched"),
            )
            .await;

        assert_eq!(
            Value::Object(payload).to_string(),
            r#"{"namespace":"AWS/EC2","metric_name":"CPUUtilization","dimensions":{"InstanceId":"i-enriched"},"tags":{"Name":"test-server","Environment":"test"},"resource_name":"test-server","environment":"test","properties":{"InstanceType":"t3.medium","InstanceFamily":"t3","InstanceSize":"medium"}}"#
        );
    }

    fn arn_of(value: Value) -> Option<String> {
        extract_resource_arn(&test_config(), &metric(value))
    }

    #[test]
    fn extracts_arns_from_dimensions() {
        assert_eq!(
            arn_of(json!({ "namespace": "AWS/EC2", "dimensions": { "InstanceId": "i-1234567890abcdef0" } })).as_deref(),
            Some("arn:aws:ec2:us-east-1:123456789012:instance/i-1234567890abcdef0")
        );
        assert_eq!(
            arn_of(json!({ "namespace": "AWS/EBS", "dimensions": { "VolumeId": "vol-1234567890abcdef0" } })).as_deref(),
            Some("arn:aws:ec2:us-east-1:123456789012:volume/vol-1234567890abcdef0")
        );
        assert_eq!(
            arn_of(json!({ "namespace": "AWS/RDS", "dimensions": { "DBInstanceIdentifier": "my-database" } }))
                .as_deref(),
            Some("arn:aws:rds:us-east-1:123456789012:db:my-database")
        );
        assert_eq!(
            arn_of(json!({ "namespace": "AWS/Lambda", "dimensions": { "FunctionName": "my-function" } })).as_deref(),
            Some("arn:aws:lambda:us-east-1:123456789012:function:my-function")
        );
        assert_eq!(
            arn_of(json!({ "namespace": "AWS/S3", "dimensions": { "BucketName": "my-bucket" } })).as_deref(),
            Some("arn:aws:s3:::my-bucket")
        );
        assert_eq!(
            arn_of(
                json!({ "namespace": "AWS/NetworkELB", "dimensions": { "LoadBalancer": "net/my-lb/50dc6c495c0c9188" } })
            )
            .as_deref(),
            Some("arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/net/my-lb/50dc6c495c0c9188")
        );
    }

    #[test]
    fn returns_none_for_missing_dimensions() {
        assert_eq!(arn_of(json!({ "namespace": "AWS/EC2", "dimensions": {} })), None);
    }

    #[tokio::test]
    async fn enriches_every_line_and_passes_undecodable_records_through() {
        let api = FakeApi::default();
        api.tags("arn:aws:sqs:us-east-1:123456789012:jobs", &[("Team", "payments")]);
        let mut enrichment = MetricsEnrichment::new(test_config());
        let lines = concat!(
            r#"{"namespace":"AWS/SQS","metric_name":"NumberOfMessagesSent","dimensions":{"QueueName":"jobs"},"value":{"max":1.0}}"#,
            "\n",
            r#"{"namespace":"Custom","metric_name":"x","dimensions":{},"value":{"max":2}}"#,
            "\n"
        );
        let blank = InputRecord {
            record_id: "2".to_string(),
            data: BASE64.encode("\n \n"),
        };
        let event = Event {
            records: vec![
                InputRecord {
                    record_id: "1".to_string(),
                    data: BASE64.encode(lines),
                },
                blank.clone(),
            ],
        };

        let response = enrichment.handle(&api, event).await;

        assert_eq!(response.records[0].result, "Ok");
        assert_eq!(
            String::from_utf8(BASE64.decode(&response.records[0].data).unwrap()).unwrap(),
            concat!(
                r#"{"namespace":"AWS/SQS","metric_name":"NumberOfMessagesSent","dimensions":{"QueueName":"jobs"},"value":{"max":1.0},"tags":{"Team":"payments"},"team":"payments"}"#,
                "\n",
                r#"{"namespace":"Custom","metric_name":"x","dimensions":{},"value":{"max":2}}"#,
                "\n"
            )
        );
        assert_eq!(response.records[1], OutputRecord::unchanged(&blank));
    }
}
