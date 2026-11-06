use std::fmt::Display;
use std::time::Duration;

/// Settings from the Lambda's environment, set by the CloudFormation template.
#[derive(Clone, Debug)]
pub struct Config {
    pub account_id: String,
    pub region: String,
    pub cache_ttl: Duration,
    pub debug: bool,
    /// Prefixes every debug line, so the two Lambdas' logs tell themselves apart.
    pub log_prefix: &'static str,
}

impl Config {
    pub fn from_env(log_prefix: &'static str) -> Self {
        let var = |name: &str, default: &str| std::env::var(name).unwrap_or_else(|_| default.to_string());
        let minutes: u64 = var("CACHE_TTL_MINUTES", "10")
            .parse()
            .expect("CACHE_TTL_MINUTES must be a whole number of minutes");
        Self {
            account_id: var("ACCOUNT_ID", ""),
            region: var("AWS_REGION", "us-east-1"),
            cache_ttl: Duration::from_secs(minutes * 60),
            debug: var("DEBUG", "false") == "true",
            log_prefix,
        }
    }

    pub fn log(&self, message: impl Display) {
        if self.debug {
            println!("[{}] {message}", self.log_prefix);
        }
    }
}

#[cfg(test)]
pub fn test_config() -> Config {
    Config {
        account_id: "123456789012".to_string(),
        region: "us-east-1".to_string(),
        cache_ttl: Duration::from_secs(600),
        debug: false,
        log_prefix: "Test",
    }
}
