//! An in-memory stand-in for the AWS APIs the Lambdas call, for the tests.

use std::collections::{HashMap, HashSet};
use std::sync::Mutex;

use serde_json::Value;

use crate::metrics::MetricsApi;
use crate::tags::TagApi;
use crate::{Error, Fields};

pub fn fields(pairs: &[(&str, &str)]) -> Fields {
    pairs
        .iter()
        .map(|(k, v)| (k.to_string(), Value::from(*v)))
        .collect()
}

/// Every API answers empty until a test sets a response; `fail` makes one return an error.
#[derive(Default)]
pub struct FakeApi {
    state: Mutex<State>,
}

#[derive(Default)]
struct State {
    tags: Vec<(String, Fields)>,
    /// Keyed by the id asked for, None for the bulk listing.
    instances: HashMap<Option<String>, Vec<(String, Fields)>>,
    volumes: HashMap<Option<String>, Vec<(String, Fields)>>,
    db_instances: HashMap<Option<String>, Vec<(String, Fields)>>,
    functions: Vec<(String, Fields)>,
    function: HashMap<String, Fields>,
    failing: HashSet<&'static str>,
    calls: HashMap<&'static str, usize>,
}

impl FakeApi {
    pub fn tags(&self, arn: &str, tags: &[(&str, &str)]) {
        self.state
            .lock()
            .unwrap()
            .tags
            .push((arn.to_string(), fields(tags)));
    }

    pub fn instances(&self, id: Option<&str>, instances: Vec<(String, Fields)>) {
        self.state
            .lock()
            .unwrap()
            .instances
            .insert(id.map(String::from), instances);
    }

    pub fn volumes(&self, id: Option<&str>, volumes: Vec<(String, Fields)>) {
        self.state
            .lock()
            .unwrap()
            .volumes
            .insert(id.map(String::from), volumes);
    }

    pub fn function(&self, name: &str, properties: Fields) {
        self.state
            .lock()
            .unwrap()
            .function
            .insert(name.to_string(), properties);
    }

    pub fn fail(&self, api: &'static str) {
        self.state.lock().unwrap().failing.insert(api);
    }

    pub fn calls(&self, api: &'static str) -> usize {
        self.state
            .lock()
            .unwrap()
            .calls
            .get(api)
            .copied()
            .unwrap_or(0)
    }

    fn call(&self, api: &'static str) -> Result<std::sync::MutexGuard<'_, State>, Error> {
        let mut state = self.state.lock().unwrap();
        *state.calls.entry(api).or_default() += 1;
        if state.failing.contains(api) {
            return Err(format!("{api} failed").into());
        }
        Ok(state)
    }
}

impl TagApi for FakeApi {
    async fn get_resources(&self, arns: &[String]) -> Result<Vec<(String, Fields)>, Error> {
        let state = self.call("get_resources")?;
        Ok(state
            .tags
            .iter()
            .filter(|(arn, _)| arns.contains(arn))
            .cloned()
            .collect())
    }
}

impl MetricsApi for FakeApi {
    async fn describe_instances(&self, id: Option<&str>) -> Result<Vec<(String, Fields)>, Error> {
        let state = self.call("describe_instances")?;
        Ok(state
            .instances
            .get(&id.map(String::from))
            .cloned()
            .unwrap_or_default())
    }

    async fn describe_volumes(&self, id: Option<&str>) -> Result<Vec<(String, Fields)>, Error> {
        let state = self.call("describe_volumes")?;
        Ok(state
            .volumes
            .get(&id.map(String::from))
            .cloned()
            .unwrap_or_default())
    }

    async fn describe_db_instances(
        &self,
        id: Option<&str>,
    ) -> Result<Vec<(String, Fields)>, Error> {
        let state = self.call("describe_db_instances")?;
        Ok(state
            .db_instances
            .get(&id.map(String::from))
            .cloned()
            .unwrap_or_default())
    }

    async fn list_functions(&self) -> Result<Vec<(String, Fields)>, Error> {
        let state = self.call("list_functions")?;
        Ok(state.functions.clone())
    }

    async fn get_function(&self, name: &str) -> Result<Fields, Error> {
        let state = self.call("get_function")?;
        state
            .function
            .get(name)
            .cloned()
            .ok_or_else(|| format!("ResourceNotFoundException: {name}").into())
    }
}
