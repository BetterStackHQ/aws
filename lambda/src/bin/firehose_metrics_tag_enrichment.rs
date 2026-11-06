use lambda_runtime::{Error, LambdaEvent, run, service_fn};
use tag_enrichment::aws::Clients;
use tag_enrichment::config::Config;
use tag_enrichment::firehose::Event;
use tag_enrichment::metrics::MetricsEnrichment;
use tokio::sync::Mutex;

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Error> {
    // Clients and caches live outside the handler, so a warm Lambda reuses them
    let sdk = aws_config::load_from_env().await;
    let api = &Clients::new(&sdk);
    let enrichment = &Mutex::new(MetricsEnrichment::new(Config::from_env("TagEnrichment")));
    run(service_fn(move |event: LambdaEvent<Event>| async move {
        Ok::<_, Error>(enrichment.lock().await.handle(api, event.payload).await)
    }))
    .await
}
