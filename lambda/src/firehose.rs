//! The Firehose data transformation contract: a batch of base64 records in, one result per record
//! out, matched by record id.

use serde::{Deserialize, Serialize};

#[derive(Debug, Deserialize)]
pub struct Event {
    pub records: Vec<InputRecord>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct InputRecord {
    pub record_id: String,
    pub data: String,
}

#[derive(Debug, PartialEq, Serialize)]
pub struct Response {
    pub records: Vec<OutputRecord>,
}

#[derive(Debug, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct OutputRecord {
    pub record_id: String,
    pub result: &'static str,
    pub data: String,
}

impl OutputRecord {
    pub fn ok(record_id: &str, data: String) -> Self {
        Self {
            record_id: record_id.to_string(),
            result: "Ok",
            data,
        }
    }

    /// The record goes on to Better Stack unchanged.
    pub fn unchanged(record: &InputRecord) -> Self {
        Self::ok(&record.record_id, record.data.clone())
    }

    pub fn dropped(record: &InputRecord) -> Self {
        Self {
            record_id: record.record_id.clone(),
            result: "Dropped",
            data: record.data.clone(),
        }
    }
}
