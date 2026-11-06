//! Resource tags through the Resource Groups Tagging API, which works across all AWS services,
//! cached in memory between invocations of a warm Lambda.

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::time::{Duration, Instant};

use serde_json::Value;

use crate::config::Config;
use crate::{Error, Fields};

/// GetResources allows up to 100 ARNs per request.
const BATCH_SIZE: usize = 100;
/// After an API error the batch is cached empty this long, to avoid a retry storm.
const ERROR_TTL: Duration = Duration::from_secs(60);

pub trait TagApi {
    /// The tags of each ARN that exists and has tags. Others are left out.
    fn get_resources(&self, arns: &[String]) -> impl Future<Output = Result<Vec<(String, Fields)>, Error>> + Send;
}

pub struct TagCache {
    entries: HashMap<String, Entry>,
}

struct Entry {
    tags: Fields,
    expires: Instant,
}

impl TagCache {
    pub fn new() -> Self {
        Self {
            entries: HashMap::new(),
        }
    }

    /// Fetches the tags of every ARN that is not cached or has expired, in batches.
    pub async fn prefetch(&mut self, api: &impl TagApi, arns: &[String], config: &Config) {
        let now = Instant::now();
        let to_fetch: Vec<String> = arns
            .iter()
            .filter(|arn| self.entries.get(*arn).is_none_or(|entry| entry.expires < now))
            .cloned()
            .collect();
        config.log(format!(
            "Cache status: {} ARNs, {} need fetching",
            arns.len(),
            to_fetch.len()
        ));

        for batch in to_fetch.chunks(BATCH_SIZE) {
            config.log(format!("Calling GetResources for {} ARNs", batch.len()));
            match api.get_resources(batch).await {
                Ok(resources) => {
                    config.log(format!("GetResources returned {} resources", resources.len()));
                    let found: HashSet<String> = resources.iter().map(|(arn, _)| arn.clone()).collect();
                    for (arn, tags) in resources {
                        self.insert(arn, tags, now + config.cache_ttl);
                    }
                    // ARNs that don't exist or have no tags are cached empty too
                    for arn in batch.iter().filter(|arn| !found.contains(*arn)) {
                        self.insert(arn.clone(), Fields::new(), now + config.cache_ttl);
                    }
                }
                Err(e) => {
                    config.log(format!("GetResources error: {e}"));
                    for arn in batch {
                        self.insert(arn.clone(), Fields::new(), now + ERROR_TTL);
                    }
                }
            }
        }
    }

    pub fn insert(&mut self, arn: String, tags: Fields, expires: Instant) {
        self.entries.insert(arn, Entry { tags, expires });
    }

    /// Cached tags of the ARN, empty when there are none or they have expired.
    pub fn get(&self, arn: Option<&str>) -> Fields {
        arn.and_then(|arn| self.entries.get(arn))
            .filter(|entry| entry.expires > Instant::now())
            .map(|entry| entry.tags.clone())
            .unwrap_or_default()
    }

    #[cfg(test)]
    pub fn contains(&self, arn: &str) -> bool {
        self.entries.contains_key(arn)
    }
}

impl Default for TagCache {
    fn default() -> Self {
        Self::new()
    }
}

/// Adds the tags as `tags`, and the common ones as top-level fields for easier querying.
pub fn add_tags(payload: &mut Fields, tags: Fields) {
    if tags.is_empty() {
        return;
    }
    let common: Vec<(&str, Value)> = [
        ("Name", "resource_name"),
        ("Environment", "environment"),
        ("Team", "team"),
    ]
    .into_iter()
    .filter_map(|(tag, field)| tags.get(tag).map(|value| (field, value.clone())))
    .collect();
    payload.insert("tags".to_string(), Value::Object(tags));
    for (field, value) in common {
        payload.insert(field.to_string(), value);
    }
}

/// The ARNs once each, in the order they first appear.
pub fn unique(arns: impl IntoIterator<Item = Option<String>>) -> Vec<String> {
    let mut seen = HashSet::new();
    arns.into_iter()
        .flatten()
        .filter(|arn| seen.insert(arn.clone()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::test_config;
    use crate::fake::{FakeApi, fields};

    #[tokio::test]
    async fn fetches_tags_for_uncached_arns() {
        let api = FakeApi::default();
        api.tags(
            "arn:aws:ec2:us-east-1:123456789012:instance/i-new",
            &[("Name", "new-server"), ("Environment", "staging")],
        );
        let mut cache = TagCache::new();

        cache
            .prefetch(
                &api,
                &["arn:aws:ec2:us-east-1:123456789012:instance/i-new".to_string()],
                &test_config(),
            )
            .await;

        assert_eq!(
            cache.get(Some("arn:aws:ec2:us-east-1:123456789012:instance/i-new")),
            fields(&[("Name", "new-server"), ("Environment", "staging")])
        );
    }

    #[tokio::test]
    async fn caches_empty_tags_for_arns_not_found() {
        let api = FakeApi::default();
        let mut cache = TagCache::new();

        cache
            .prefetch(
                &api,
                &["arn:aws:ec2:us-east-1:123456789012:instance/i-notfound".to_string()],
                &test_config(),
            )
            .await;

        assert!(cache.contains("arn:aws:ec2:us-east-1:123456789012:instance/i-notfound"));
        assert_eq!(
            cache.get(Some("arn:aws:ec2:us-east-1:123456789012:instance/i-notfound")),
            Fields::new()
        );
        assert_eq!(api.calls("get_resources"), 1);
    }

    #[tokio::test]
    async fn does_not_refetch_cached_arns() {
        let api = FakeApi::default();
        let mut cache = TagCache::new();
        cache.insert(
            "arn:aws:ec2:us-east-1:123456789012:instance/i-cached".to_string(),
            fields(&[("Name", "cached")]),
            Instant::now() + Duration::from_secs(600),
        );

        cache
            .prefetch(
                &api,
                &["arn:aws:ec2:us-east-1:123456789012:instance/i-cached".to_string()],
                &test_config(),
            )
            .await;

        assert_eq!(api.calls("get_resources"), 0);
    }

    #[tokio::test]
    async fn caches_empty_tags_for_a_minute_on_api_error() {
        let api = FakeApi::default();
        api.fail("get_resources");
        let mut cache = TagCache::new();

        cache
            .prefetch(
                &api,
                &["arn:aws:ec2:us-east-1:123456789012:instance/i-err".to_string()],
                &test_config(),
            )
            .await;

        assert!(cache.contains("arn:aws:ec2:us-east-1:123456789012:instance/i-err"));
        assert_eq!(
            cache.get(Some("arn:aws:ec2:us-east-1:123456789012:instance/i-err")),
            Fields::new()
        );
    }

    #[tokio::test]
    async fn fetches_in_batches_of_100() {
        let api = FakeApi::default();
        let mut cache = TagCache::new();
        let arns: Vec<String> = (0..250)
            .map(|i| format!("arn:aws:sqs:us-east-1:123456789012:queue-{i}"))
            .collect();

        cache.prefetch(&api, &arns, &test_config()).await;

        assert_eq!(api.calls("get_resources"), 3);
    }

    #[test]
    fn returns_cached_tags() {
        let mut cache = TagCache::new();
        cache.insert(
            "arn:aws:ec2:us-east-1:123456789012:instance/i-test".to_string(),
            fields(&[("Name", "test")]),
            Instant::now() + Duration::from_secs(600),
        );

        assert_eq!(
            cache.get(Some("arn:aws:ec2:us-east-1:123456789012:instance/i-test")),
            fields(&[("Name", "test")])
        );
    }

    #[test]
    fn returns_no_tags_for_expired_cache() {
        let mut cache = TagCache::new();
        cache.insert(
            "arn:aws:ec2:us-east-1:123456789012:instance/i-expired".to_string(),
            fields(&[("Name", "expired")]),
            Instant::now() - Duration::from_secs(1),
        );

        assert_eq!(
            cache.get(Some("arn:aws:ec2:us-east-1:123456789012:instance/i-expired")),
            Fields::new()
        );
    }

    #[test]
    fn returns_no_tags_without_an_arn() {
        assert_eq!(TagCache::new().get(None), Fields::new());
    }

    #[test]
    fn adds_tags_and_common_fields() {
        let mut payload = fields(&[("logGroup", "/ec2/syslog")]);

        add_tags(
            &mut payload,
            fields(&[
                ("Name", "test-server"),
                ("Environment", "production"),
                ("Team", "platform"),
            ]),
        );

        assert_eq!(
            serde_json::to_string(&payload).unwrap(),
            r#"{"logGroup":"/ec2/syslog","tags":{"Name":"test-server","Environment":"production","Team":"platform"},"resource_name":"test-server","environment":"production","team":"platform"}"#
        );
    }

    #[test]
    fn adds_nothing_without_tags() {
        let mut payload = fields(&[("logGroup", "/ec2/syslog")]);

        add_tags(&mut payload, Fields::new());

        assert_eq!(payload, fields(&[("logGroup", "/ec2/syslog")]));
    }
}
