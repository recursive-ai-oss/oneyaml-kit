//! Typed models for the typed-load task: Rust structs that hold every field of the
//! workload, so the serialised result can be compared with the whole expected value.

use crate::model::{runner, OwnedInput, Product, Runner};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// A library's typed entry point, usable for any model.
pub trait TypedParser: Copy + 'static {
    fn parse<T: DeserializeOwned>(&self, src: &str) -> Result<T, String>;
}

/// The runner for a typed model, or None when the workload names an unknown model.
pub fn typed_runner<P: TypedParser>(
    p: P,
    model: &str,
    src: &'static str,
) -> Option<Box<dyn Runner>> {
    match model {
        "gha" => Some(go::<P, Workflow>(p, src)),
        "records" => Some(go::<P, Vec<Record>>(p, src)),
        _ => None,
    }
}

fn go<P: TypedParser, T: DeserializeOwned + Serialize + 'static>(
    p: P,
    src: &'static str,
) -> Box<dyn Runner> {
    runner(
        move || OwnedInput::new(src),
        move |own| p.parse::<T>(own.as_str()),
        |t| match serde_json::to_value(&t) {
            Ok(v) => Product::Value(v),
            Err(e) => Product::Value(serde_json::Value::String(format!("cannot serialise: {e}"))),
        },
    )
}

// ---- records ---------------------------------------------------------------------------

#[derive(Debug, Deserialize, Serialize)]
pub struct Record {
    pub id: u64,
    pub name: String,
    pub email: String,
    pub active: bool,
    pub score: f64,
    pub tags: Vec<String>,
    pub address: Address,
    pub note: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Address {
    pub city: String,
    pub zip: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub geo: Option<Geo>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Geo {
    pub lat: f64,
    pub lng: f64,
}

// ---- a GitHub Actions workflow ---------------------------------------------------------

#[derive(Debug, Deserialize, Serialize)]
pub struct Workflow {
    pub name: String,
    pub on: On,
    pub jobs: BTreeMap<String, Job>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct On {
    pub pull_request: Option<String>,
    pub push: Push,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Push {
    pub branches: Vec<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Job {
    #[serde(rename = "runs-on")]
    pub runs_on: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub strategy: Option<Strategy>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub container: Option<Container>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub services: Option<BTreeMap<String, Service>>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub env: Option<BTreeMap<String, String>>,
    pub steps: Vec<Step>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Strategy {
    pub matrix: Matrix,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Matrix {
    #[serde(rename = "runs-on")]
    pub runs_on: Vec<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(untagged)]
pub enum Container {
    Image(String),
    Full {
        image: String,
        options: Option<String>,
    },
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Service {
    pub image: String,
    pub ports: Vec<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(untagged)]
pub enum Scalar {
    Bool(bool),
    Text(String),
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Step {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub name: Option<String>,
    #[serde(rename = "if", skip_serializing_if = "Option::is_none", default)]
    pub condition: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub uses: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub run: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub shell: Option<String>,
    #[serde(rename = "with", skip_serializing_if = "Option::is_none", default)]
    pub with: Option<BTreeMap<String, Scalar>>,
}
