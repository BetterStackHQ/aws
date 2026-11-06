//! The AWS SDK behind `TagApi` and `MetricsApi`, and the properties kept for each resource type.

use aws_config::SdkConfig;
use aws_sdk_ec2::types::{Filter, Instance, Volume};
use aws_sdk_lambda::types::FunctionConfiguration;
use aws_sdk_rds::types::DbInstance;
use serde_json::Value;

use crate::metrics::MetricsApi;
use crate::tags::TagApi;
use crate::{Error, Fields};

/// The SDK's error with its cause chain, which plain `Display` leaves out.
macro_rules! sdk_error {
    ($crate_name:ident) => {
        |e| Error::from($crate_name::error::DisplayErrorContext(e).to_string())
    };
}

pub struct Tagging {
    client: aws_sdk_resourcegroupstagging::Client,
}

impl Tagging {
    pub fn new(sdk: &SdkConfig) -> Self {
        Self {
            client: aws_sdk_resourcegroupstagging::Client::new(sdk),
        }
    }
}

impl TagApi for Tagging {
    async fn get_resources(&self, arns: &[String]) -> Result<Vec<(String, Fields)>, Error> {
        let output = self
            .client
            .get_resources()
            .set_resource_arn_list(Some(arns.to_vec()))
            .send()
            .await
            .map_err(sdk_error!(aws_sdk_resourcegroupstagging))?;
        Ok(output
            .resource_tag_mapping_list()
            .iter()
            .filter_map(|resource| {
                let tags = resource
                    .tags()
                    .iter()
                    .map(|tag| (tag.key().to_string(), Value::from(tag.value())))
                    .collect();
                Some((resource.resource_arn()?.to_string(), tags))
            })
            .collect())
    }
}

pub struct Clients {
    tagging: Tagging,
    ec2: aws_sdk_ec2::Client,
    rds: aws_sdk_rds::Client,
    lambda: aws_sdk_lambda::Client,
}

impl Clients {
    pub fn new(sdk: &SdkConfig) -> Self {
        Self {
            tagging: Tagging::new(sdk),
            ec2: aws_sdk_ec2::Client::new(sdk),
            rds: aws_sdk_rds::Client::new(sdk),
            lambda: aws_sdk_lambda::Client::new(sdk),
        }
    }
}

impl TagApi for Clients {
    async fn get_resources(&self, arns: &[String]) -> Result<Vec<(String, Fields)>, Error> {
        self.tagging.get_resources(arns).await
    }
}

impl MetricsApi for Clients {
    async fn describe_instances(&self, id: Option<&str>) -> Result<Vec<(String, Fields)>, Error> {
        let request = match id {
            Some(id) => self.ec2.describe_instances().instance_ids(id),
            None => self.ec2.describe_instances().filters(
                Filter::builder()
                    .name("instance-state-name")
                    .values("running")
                    .build(),
            ),
        };
        let mut pages = request.into_paginator().send();
        let mut instances = Vec::new();
        while let Some(page) = pages.next().await {
            for reservation in page.map_err(sdk_error!(aws_sdk_ec2))?.reservations() {
                for instance in reservation.instances() {
                    if let Some(id) = instance.instance_id() {
                        instances.push((id.to_string(), ec2_instance_properties(instance)));
                    }
                }
            }
        }
        Ok(instances)
    }

    async fn describe_volumes(&self, id: Option<&str>) -> Result<Vec<(String, Fields)>, Error> {
        let request = match id {
            Some(id) => self.ec2.describe_volumes().volume_ids(id),
            None => self
                .ec2
                .describe_volumes()
                .filters(Filter::builder().name("status").values("in-use").build()),
        };
        let mut pages = request.into_paginator().send();
        let mut volumes = Vec::new();
        while let Some(page) = pages.next().await {
            for volume in page.map_err(sdk_error!(aws_sdk_ec2))?.volumes() {
                if let Some(id) = volume.volume_id() {
                    volumes.push((id.to_string(), ebs_volume_properties(volume)));
                }
            }
        }
        Ok(volumes)
    }

    async fn describe_db_instances(
        &self,
        id: Option<&str>,
    ) -> Result<Vec<(String, Fields)>, Error> {
        let mut pages = self
            .rds
            .describe_db_instances()
            .set_db_instance_identifier(id.map(String::from))
            .into_paginator()
            .send();
        let mut db_instances = Vec::new();
        while let Some(page) = pages.next().await {
            for db in page.map_err(sdk_error!(aws_sdk_rds))?.db_instances() {
                if let Some(id) = db.db_instance_identifier() {
                    db_instances.push((id.to_string(), rds_instance_properties(db)));
                }
            }
        }
        Ok(db_instances)
    }

    async fn list_functions(&self) -> Result<Vec<(String, Fields)>, Error> {
        let mut pages = self.lambda.list_functions().into_paginator().send();
        let mut functions = Vec::new();
        while let Some(page) = pages.next().await {
            for function in page.map_err(sdk_error!(aws_sdk_lambda))?.functions() {
                if let Some(name) = function.function_name() {
                    functions.push((name.to_string(), lambda_function_properties(function)));
                }
            }
        }
        Ok(functions)
    }

    async fn get_function(&self, name: &str) -> Result<Fields, Error> {
        let output = self
            .lambda
            .get_function()
            .function_name(name)
            .send()
            .await
            .map_err(sdk_error!(aws_sdk_lambda))?;
        Ok(output
            .configuration()
            .map(lambda_function_properties)
            .unwrap_or_default())
    }
}

fn properties<const N: usize>(pairs: [(&str, Value); N]) -> Fields {
    pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect()
}

/// A missing value is JSON null, so every resource of a type has the same keys.
fn text(value: Option<&str>) -> Value {
    value.map_or(Value::Null, Value::from)
}

pub fn ec2_instance_properties(instance: &Instance) -> Fields {
    let instance_type = instance
        .instance_type()
        .map(|t| t.as_str())
        .filter(|t| !t.is_empty());
    properties([
        ("InstanceType", text(instance_type)),
        (
            "InstanceFamily",
            text(instance_type.and_then(|t| t.split('.').next())),
        ),
        (
            "InstanceSize",
            text(instance_type.and_then(|t| t.split('.').next_back())),
        ),
        (
            "Architecture",
            text(instance.architecture().map(|a| a.as_str())),
        ),
        (
            "AvailabilityZone",
            text(instance.placement().and_then(|p| p.availability_zone())),
        ),
        ("Platform", text(instance.platform_details())),
        ("ImageId", text(instance.image_id())),
        (
            "InstanceLifecycle",
            Value::from(
                instance
                    .instance_lifecycle()
                    .map_or("on-demand", |l| l.as_str()),
            ),
        ),
    ])
}

pub fn ebs_volume_properties(volume: &Volume) -> Fields {
    properties([
        ("VolumeType", text(volume.volume_type().map(|t| t.as_str()))),
        ("Size", Value::from(volume.size())),
        ("AvailabilityZone", text(volume.availability_zone())),
        ("Iops", Value::from(volume.iops())),
        ("Throughput", Value::from(volume.throughput())),
        ("Encrypted", Value::from(volume.encrypted())),
    ])
}

pub fn rds_instance_properties(db: &DbInstance) -> Fields {
    properties([
        ("DBInstanceClass", text(db.db_instance_class())),
        ("Engine", text(db.engine())),
        ("EngineVersion", text(db.engine_version())),
        ("AvailabilityZone", text(db.availability_zone())),
        ("MultiAZ", Value::from(db.multi_az())),
        ("StorageType", text(db.storage_type())),
        ("AllocatedStorage", Value::from(db.allocated_storage())),
    ])
}

pub fn lambda_function_properties(function: &FunctionConfiguration) -> Fields {
    properties([
        ("Runtime", text(function.runtime().map(|r| r.as_str()))),
        ("MemorySize", Value::from(function.memory_size())),
        ("Timeout", Value::from(function.timeout())),
        (
            "Architecture",
            text(function.architectures().first().map(|a| a.as_str())),
        ),
        (
            "PackageType",
            text(function.package_type().map(|p| p.as_str())),
        ),
    ])
}

#[cfg(test)]
mod tests {
    use aws_sdk_ec2::types::{
        ArchitectureValues, InstanceLifecycleType, InstanceType, Placement, VolumeType,
    };
    use aws_sdk_lambda::types::{Architecture, PackageType, Runtime};

    use super::*;

    fn json(fields: Fields) -> String {
        Value::Object(fields).to_string()
    }

    #[test]
    fn extracts_all_instance_properties() {
        let instance = Instance::builder()
            .instance_type(InstanceType::from("t3.medium"))
            .architecture(ArchitectureValues::X8664)
            .placement(Placement::builder().availability_zone("us-east-1a").build())
            .platform_details("Linux/UNIX")
            .image_id("ami-12345678")
            .build();

        assert_eq!(
            json(ec2_instance_properties(&instance)),
            r#"{"InstanceType":"t3.medium","InstanceFamily":"t3","InstanceSize":"medium","Architecture":"x86_64","AvailabilityZone":"us-east-1a","Platform":"Linux/UNIX","ImageId":"ami-12345678","InstanceLifecycle":"on-demand"}"#
        );
    }

    #[test]
    fn keeps_the_lifecycle_of_spot_instances() {
        let instance = Instance::builder()
            .instance_type(InstanceType::from("c5.xlarge"))
            .instance_lifecycle(InstanceLifecycleType::Spot)
            .build();

        assert_eq!(
            ec2_instance_properties(&instance)["InstanceLifecycle"],
            "spot"
        );
    }

    #[test]
    fn extracts_all_volume_properties() {
        let volume = Volume::builder()
            .volume_type(VolumeType::Gp3)
            .size(100)
            .availability_zone("us-east-1a")
            .iops(3000)
            .throughput(125)
            .encrypted(true)
            .build();

        assert_eq!(
            json(ebs_volume_properties(&volume)),
            r#"{"VolumeType":"gp3","Size":100,"AvailabilityZone":"us-east-1a","Iops":3000,"Throughput":125,"Encrypted":true}"#
        );
    }

    #[test]
    fn writes_null_for_missing_volume_properties() {
        let volume = Volume::builder()
            .volume_type(VolumeType::Standard)
            .size(8)
            .availability_zone("us-east-1a")
            .encrypted(false)
            .build();

        assert_eq!(
            json(ebs_volume_properties(&volume)),
            r#"{"VolumeType":"standard","Size":8,"AvailabilityZone":"us-east-1a","Iops":null,"Throughput":null,"Encrypted":false}"#
        );
    }

    #[test]
    fn extracts_all_rds_properties() {
        let db = DbInstance::builder()
            .db_instance_class("db.t3.medium")
            .engine("postgres")
            .engine_version("14.9")
            .availability_zone("us-east-1a")
            .multi_az(false)
            .storage_type("gp2")
            .allocated_storage(100)
            .build();

        assert_eq!(
            json(rds_instance_properties(&db)),
            r#"{"DBInstanceClass":"db.t3.medium","Engine":"postgres","EngineVersion":"14.9","AvailabilityZone":"us-east-1a","MultiAZ":false,"StorageType":"gp2","AllocatedStorage":100}"#
        );
    }

    #[test]
    fn extracts_all_lambda_properties() {
        let function = FunctionConfiguration::builder()
            .runtime(Runtime::from("ruby3.2"))
            .memory_size(512)
            .timeout(30)
            .architectures(Architecture::Arm64)
            .package_type(PackageType::Zip)
            .build();

        assert_eq!(
            json(lambda_function_properties(&function)),
            r#"{"Runtime":"ruby3.2","MemorySize":512,"Timeout":30,"Architecture":"arm64","PackageType":"Zip"}"#
        );
    }
}
