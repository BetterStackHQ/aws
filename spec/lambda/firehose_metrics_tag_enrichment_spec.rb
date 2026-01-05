# frozen_string_literal: true

require 'spec_helper'
require_relative '../../lambda/firehose_metrics_tag_enrichment'

RSpec.describe 'Firehose Metrics Tag Enrichment' do
  before do
    # Reset all caches between tests
    TAG_CACHE.clear
    EC2_CACHE[:instances] = {}
    EC2_CACHE[:volumes] = {}
    EC2_CACHE[:loaded] = false
    EC2_CACHE[:expires] = Time.at(0)
    RDS_CACHE[:instances] = {}
    RDS_CACHE[:loaded] = false
    RDS_CACHE[:expires] = Time.at(0)
    LAMBDA_CACHE[:functions] = {}
    LAMBDA_CACHE[:loaded] = false
    LAMBDA_CACHE[:expires] = Time.at(0)

    # Stub AWS clients to prevent real API calls
    stub_tagging_client
    stub_ec2_client
    stub_rds_client
    stub_lambda_client
  end

  def stub_tagging_client
    TAGGING_CLIENT.stub_responses(:get_resources, {
      resource_tag_mapping_list: []
    })
  end

  def stub_ec2_client
    EC2_CLIENT.stub_responses(:describe_instances, {
      reservations: []
    })
    EC2_CLIENT.stub_responses(:describe_volumes, {
      volumes: []
    })
  end

  def stub_rds_client
    RDS_CLIENT.stub_responses(:describe_db_instances, {
      db_instances: []
    })
  end

  def stub_lambda_client
    LAMBDA_CLIENT.stub_responses(:list_functions, {
      functions: []
    })
    LAMBDA_CLIENT.stub_responses(:get_function, 'ResourceNotFoundException')
  end

  describe '#extract_ec2_instance_properties' do
    it 'extracts all instance properties' do
      instance = double(
        instance_type: 't3.medium',
        architecture: 'x86_64',
        placement: double(availability_zone: 'us-east-1a'),
        platform_details: 'Linux/UNIX',
        image_id: 'ami-12345678',
        instance_lifecycle: nil
      )

      props = extract_ec2_instance_properties(instance)

      expect(props['InstanceType']).to eq('t3.medium')
      expect(props['InstanceFamily']).to eq('t3')
      expect(props['InstanceSize']).to eq('medium')
      expect(props['Architecture']).to eq('x86_64')
      expect(props['AvailabilityZone']).to eq('us-east-1a')
      expect(props['Platform']).to eq('Linux/UNIX')
      expect(props['ImageId']).to eq('ami-12345678')
      expect(props['InstanceLifecycle']).to eq('on-demand')
    end

    it 'handles spot instances' do
      instance = double(
        instance_type: 'c5.xlarge',
        architecture: 'x86_64',
        placement: double(availability_zone: 'us-east-1b'),
        platform_details: 'Linux/UNIX',
        image_id: 'ami-87654321',
        instance_lifecycle: 'spot'
      )

      props = extract_ec2_instance_properties(instance)

      expect(props['InstanceLifecycle']).to eq('spot')
    end
  end

  describe '#extract_ebs_volume_properties' do
    it 'extracts all volume properties' do
      volume = double(
        volume_type: 'gp3',
        size: 100,
        availability_zone: 'us-east-1a',
        iops: 3000,
        throughput: 125,
        encrypted: true
      )

      props = extract_ebs_volume_properties(volume)

      expect(props['VolumeType']).to eq('gp3')
      expect(props['Size']).to eq(100)
      expect(props['AvailabilityZone']).to eq('us-east-1a')
      expect(props['Iops']).to eq(3000)
      expect(props['Throughput']).to eq(125)
      expect(props['Encrypted']).to eq(true)
    end
  end

  describe '#extract_rds_instance_properties' do
    it 'extracts all RDS properties' do
      db = double(
        db_instance_class: 'db.t3.medium',
        engine: 'postgres',
        engine_version: '14.9',
        availability_zone: 'us-east-1a',
        multi_az: false,
        storage_type: 'gp2',
        allocated_storage: 100
      )

      props = extract_rds_instance_properties(db)

      expect(props['DBInstanceClass']).to eq('db.t3.medium')
      expect(props['Engine']).to eq('postgres')
      expect(props['EngineVersion']).to eq('14.9')
      expect(props['AvailabilityZone']).to eq('us-east-1a')
      expect(props['MultiAZ']).to eq(false)
      expect(props['StorageType']).to eq('gp2')
      expect(props['AllocatedStorage']).to eq(100)
    end
  end

  describe '#extract_lambda_function_properties' do
    it 'extracts all Lambda properties' do
      func = double(
        runtime: 'ruby3.2',
        memory_size: 512,
        timeout: 30,
        architectures: ['arm64'],
        package_type: 'Zip'
      )

      props = extract_lambda_function_properties(func)

      expect(props['Runtime']).to eq('ruby3.2')
      expect(props['MemorySize']).to eq(512)
      expect(props['Timeout']).to eq(30)
      expect(props['Architecture']).to eq('arm64')
      expect(props['PackageType']).to eq('Zip')
    end
  end

  describe '#load_ec2_properties' do
    it 'loads running instances into cache' do
      EC2_CLIENT.stub_responses(:describe_instances, {
        reservations: [{
          instances: [{
            instance_id: 'i-1234567890abcdef0',
            instance_type: 't3.micro',
            architecture: 'x86_64',
            placement: { availability_zone: 'us-east-1a' },
            platform_details: 'Linux/UNIX',
            image_id: 'ami-12345678',
            instance_lifecycle: nil
          }]
        }]
      })
      EC2_CLIENT.stub_responses(:describe_volumes, {
        volumes: [{
          volume_id: 'vol-1234567890abcdef0',
          volume_type: 'gp3',
          size: 50,
          availability_zone: 'us-east-1a',
          iops: 3000,
          throughput: 125,
          encrypted: false
        }]
      })

      load_ec2_properties

      expect(EC2_CACHE[:loaded]).to be true
      expect(EC2_CACHE[:instances]).to have_key('i-1234567890abcdef0')
      expect(EC2_CACHE[:volumes]).to have_key('vol-1234567890abcdef0')
    end

    it 'sets short TTL on error' do
      EC2_CLIENT.stub_responses(:describe_instances, 'ServiceUnavailable')

      load_ec2_properties

      expect(EC2_CACHE[:loaded]).to be true
      expect(EC2_CACHE[:expires]).to be_within(5).of(Time.now + 60)
    end
  end

  describe '#backfill_ec2_instance' do
    before do
      EC2_CACHE[:loaded] = true
      EC2_CACHE[:expires] = Time.now + 600
    end

    it 'caches instance on successful backfill' do
      EC2_CLIENT.stub_responses(:describe_instances, {
        reservations: [{
          instances: [{
            instance_id: 'i-newinstance',
            instance_type: 't3.small',
            architecture: 'x86_64',
            placement: { availability_zone: 'us-east-1b' },
            platform_details: 'Linux/UNIX',
            image_id: 'ami-11111111',
            instance_lifecycle: nil
          }]
        }]
      })

      backfill_ec2_instance('i-newinstance')

      expect(EC2_CACHE[:instances]).to have_key('i-newinstance')
      expect(EC2_CACHE[:instances]['i-newinstance']['InstanceType']).to eq('t3.small')
    end

    it 'caches empty hash as sentinel on error' do
      EC2_CLIENT.stub_responses(:describe_instances, 'InvalidInstanceID.NotFound')

      backfill_ec2_instance('i-terminated')

      expect(EC2_CACHE[:instances]).to have_key('i-terminated')
      expect(EC2_CACHE[:instances]['i-terminated']).to eq({})
    end

    it 'does not re-fetch already cached instances' do
      EC2_CACHE[:instances]['i-cached'] = { 'InstanceType' => 't3.large' }

      expect(EC2_CLIENT).not_to receive(:describe_instances)

      backfill_ec2_instance('i-cached')
    end
  end

  describe '#get_properties_for_metric' do
    before do
      EC2_CACHE[:loaded] = true
      EC2_CACHE[:expires] = Time.now + 600
      EC2_CACHE[:instances]['i-test'] = { 'InstanceType' => 't3.nano' }
    end

    it 'returns EC2 properties for AWS/EC2 namespace' do
      payload = {
        'namespace' => 'AWS/EC2',
        'dimensions' => { 'InstanceId' => 'i-test' }
      }

      props = get_properties_for_metric(payload)

      expect(props['InstanceType']).to eq('t3.nano')
    end

    it 'returns nil for unknown namespace' do
      payload = {
        'namespace' => 'AWS/Unknown',
        'dimensions' => {}
      }

      props = get_properties_for_metric(payload)

      expect(props).to be_nil
    end
  end

  describe '#enrich_record' do
    before do
      EC2_CACHE[:loaded] = true
      EC2_CACHE[:expires] = Time.now + 600
      EC2_CACHE[:instances]['i-enriched'] = {
        'InstanceType' => 't3.medium',
        'InstanceFamily' => 't3',
        'InstanceSize' => 'medium'
      }

      TAG_CACHE['arn:aws:ec2:us-east-1:123456789012:instance/i-enriched'] = {
        tags: { 'Name' => 'test-server', 'Environment' => 'test' },
        expires: Time.now + 600
      }
    end

    it 'adds both tags and properties to payload' do
      payload = {
        'namespace' => 'AWS/EC2',
        'metric_name' => 'CPUUtilization',
        'dimensions' => { 'InstanceId' => 'i-enriched' }
      }
      resource_arn = 'arn:aws:ec2:us-east-1:123456789012:instance/i-enriched'

      result = enrich_record(payload, resource_arn)

      expect(result['tags']).to eq({ 'Name' => 'test-server', 'Environment' => 'test' })
      expect(result['resource_name']).to eq('test-server')
      expect(result['environment']).to eq('test')
      expect(result['properties']['InstanceType']).to eq('t3.medium')
    end
  end

  describe '#extract_resource_arn' do
    it 'extracts EC2 instance ARN' do
      payload = {
        'namespace' => 'AWS/EC2',
        'dimensions' => { 'InstanceId' => 'i-1234567890abcdef0' }
      }

      arn = extract_resource_arn(payload)

      expect(arn).to eq('arn:aws:ec2:us-east-1:123456789012:instance/i-1234567890abcdef0')
    end

    it 'extracts EBS volume ARN' do
      payload = {
        'namespace' => 'AWS/EBS',
        'dimensions' => { 'VolumeId' => 'vol-1234567890abcdef0' }
      }

      arn = extract_resource_arn(payload)

      expect(arn).to eq('arn:aws:ec2:us-east-1:123456789012:volume/vol-1234567890abcdef0')
    end

    it 'extracts RDS instance ARN' do
      payload = {
        'namespace' => 'AWS/RDS',
        'dimensions' => { 'DBInstanceIdentifier' => 'my-database' }
      }

      arn = extract_resource_arn(payload)

      expect(arn).to eq('arn:aws:rds:us-east-1:123456789012:db:my-database')
    end

    it 'extracts Lambda function ARN' do
      payload = {
        'namespace' => 'AWS/Lambda',
        'dimensions' => { 'FunctionName' => 'my-function' }
      }

      arn = extract_resource_arn(payload)

      expect(arn).to eq('arn:aws:lambda:us-east-1:123456789012:function:my-function')
    end

    it 'returns nil for missing dimensions' do
      payload = {
        'namespace' => 'AWS/EC2',
        'dimensions' => {}
      }

      arn = extract_resource_arn(payload)

      expect(arn).to be_nil
    end
  end
end
