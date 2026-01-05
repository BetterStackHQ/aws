# frozen_string_literal: true

require 'json'
require 'base64'
require 'aws-sdk-resourcegroupstaggingapi'
require 'aws-sdk-ec2'
require 'aws-sdk-rds'
require 'aws-sdk-lambda'

#
# Firehose Tag Enrichment Lambda
#
# Enriches CloudWatch metric records with AWS resource tags before delivery.
# Receives batches of records from Firehose, looks up tags for each resource,
# and returns enriched records.
#
# CloudWatch Metric Streams send data as newline-delimited JSON (NDJSON),
# where each Firehose record contains multiple metrics separated by newlines.
#
# Uses the Resource Groups Tagging API which works across all AWS services.
# Tags are cached in-memory to minimize API calls.
#

# In-memory tag cache: { resource_arn => { tags: {...}, expires: Time } }
TAG_CACHE = {}
CACHE_TTL_MINUTES = Integer(ENV.fetch('CACHE_TTL_MINUTES', '10'))
ACCOUNT_ID = ENV.fetch('ACCOUNT_ID', '')
REGION = ENV.fetch('AWS_REGION', 'us-east-1')
DEBUG = ENV.fetch('DEBUG', 'false') == 'true'

# Initialize clients outside handler for connection reuse
TAGGING_CLIENT = Aws::ResourceGroupsTaggingAPI::Client.new
EC2_CLIENT = Aws::EC2::Client.new
RDS_CLIENT = Aws::RDS::Client.new
LAMBDA_CLIENT = Aws::Lambda::Client.new

# In-memory property caches: { resource_id => properties_hash }
# Each cache tracks whether initial bulk load has been done
EC2_CACHE = { instances: {}, volumes: {}, loaded: false, expires: Time.at(0) }
RDS_CACHE = { instances: {}, loaded: false, expires: Time.at(0) }
LAMBDA_CACHE = { functions: {}, loaded: false, expires: Time.at(0) }

def log(message)
  puts "[TagEnrichment] #{message}" if DEBUG
end

#
# Firehose transformation handler
#
# Receives: { "records" => [{ "recordId" => "...", "data" => "base64 NDJSON..." }, ...] }
# Returns:  { "records" => [{ "recordId" => "...", "result" => "Ok", "data" => "base64 NDJSON..." }, ...] }
#
def lambda_handler(event:, context:)
  log "Invoked with #{event['records'].size} records"
  log "Account ID: #{ACCOUNT_ID}, Region: #{REGION}"

  output_records = []

  # First pass: collect all metrics and their ARNs for batch tag lookup
  all_metrics = []

  event['records'].each do |record|
    begin
      # Decode base64 data - contains newline-delimited JSON metrics
      raw_data = Base64.decode64(record['data'])
      log "Record #{record['recordId']}: decoded #{raw_data.bytesize} bytes"

      # Parse each line as a separate metric
      lines = raw_data.split("\n")
      log "Record #{record['recordId']}: found #{lines.size} lines"

      metrics = lines.filter_map do |line|
        next if line.strip.empty?
        JSON.parse(line)
      rescue JSON::ParserError => e
        log "JSON parse error: #{e.message} for line: #{line[0..100]}"
        nil
      end

      log "Record #{record['recordId']}: parsed #{metrics.size} metrics"

      metrics.each do |metric|
        resource_arn = extract_resource_arn(metric)
        log "Metric #{metric['metric_name']} (#{metric['namespace']}): ARN=#{resource_arn || 'nil'}"
        all_metrics << {
          record_id: record['recordId'],
          metric: metric,
          resource_arn: resource_arn
        }
      end
    rescue StandardError => e
      log "Error processing record #{record['recordId']}: #{e.class} - #{e.message}"
      # If we can't process the record at all, pass through unchanged
      output_records << {
        'recordId' => record['recordId'],
        'result' => 'Ok',
        'data' => record['data']
      }
    end
  end

  # Batch fetch tags for all unique ARNs
  unique_arns = all_metrics.map { |m| m[:resource_arn] }.compact.uniq
  log "Fetching tags for #{unique_arns.size} unique ARNs"
  prefetch_tags(unique_arns)

  # Group metrics back by record_id and enrich
  metrics_by_record = all_metrics.group_by { |m| m[:record_id] }

  metrics_by_record.each do |record_id, items|
    begin
      # Enrich each metric in this record
      enriched_lines = items.map do |item|
        enriched = enrich_record(item[:metric], item[:resource_arn])
        JSON.generate(enriched)
      end

      # Rejoin as newline-delimited JSON
      enriched_data = enriched_lines.join("\n") + "\n"

      output_records << {
        'recordId' => record_id,
        'result' => 'Ok',
        'data' => Base64.strict_encode64(enriched_data)
      }
    rescue StandardError => e
      log "Error enriching record #{record_id}: #{e.class} - #{e.message}"
      # On error, find original record and pass through unchanged
      original = event['records'].find { |r| r['recordId'] == record_id }
      output_records << {
        'recordId' => record_id,
        'result' => 'Ok',
        'data' => original['data']
      }
    end
  end

  log "Returning #{output_records.size} records"
  { 'records' => output_records }
end

#
# Extract AWS resource ARN from metric dimensions.
#
# Different namespaces use different dimension names:
# - AWS/EC2: InstanceId
# - AWS/RDS: DBInstanceIdentifier
# - AWS/Lambda: FunctionName
# - AWS/ELB: LoadBalancerName
# - AWS/ApplicationELB: LoadBalancer
# - AWS/DynamoDB: TableName
# - AWS/S3: BucketName
# - AWS/SQS: QueueName
# - AWS/SNS: TopicName
#
def extract_resource_arn(payload)
  namespace = payload['namespace'] || ''
  dimensions = payload['dimensions'] || {}

  log "  extract_resource_arn: namespace=#{namespace}, dimensions=#{dimensions.inspect}"

  case namespace
  when 'AWS/EC2'
    instance_id = dimensions['InstanceId']
    if instance_id
      arn = "arn:aws:ec2:#{REGION}:#{ACCOUNT_ID}:instance/#{instance_id}"
      log "  -> EC2 ARN: #{arn}"
      return arn
    end

  when 'AWS/RDS'
    db_id = dimensions['DBInstanceIdentifier']
    return "arn:aws:rds:#{REGION}:#{ACCOUNT_ID}:db:#{db_id}" if db_id

  when 'AWS/Lambda'
    func_name = dimensions['FunctionName']
    return "arn:aws:lambda:#{REGION}:#{ACCOUNT_ID}:function:#{func_name}" if func_name

  when 'AWS/DynamoDB'
    table_name = dimensions['TableName']
    return "arn:aws:dynamodb:#{REGION}:#{ACCOUNT_ID}:table/#{table_name}" if table_name

  when 'AWS/SQS'
    queue_name = dimensions['QueueName']
    return "arn:aws:sqs:#{REGION}:#{ACCOUNT_ID}:#{queue_name}" if queue_name

  when 'AWS/SNS'
    topic_name = dimensions['TopicName']
    return "arn:aws:sns:#{REGION}:#{ACCOUNT_ID}:#{topic_name}" if topic_name

  when 'AWS/S3'
    bucket_name = dimensions['BucketName']
    return "arn:aws:s3:::#{bucket_name}" if bucket_name

  when 'AWS/EBS'
    volume_id = dimensions['VolumeId']
    return "arn:aws:ec2:#{REGION}:#{ACCOUNT_ID}:volume/#{volume_id}" if volume_id

  when 'AWS/ELB', 'AWS/ApplicationELB', 'AWS/NetworkELB'
    lb_name = dimensions['LoadBalancer'] || dimensions['LoadBalancerName']
    if lb_name
      return "arn:aws:elasticloadbalancing:#{REGION}:#{ACCOUNT_ID}:loadbalancer/#{lb_name}"
    end
  end

  log "  -> No ARN extracted for namespace #{namespace}"
  nil
end

#
# Batch fetch tags for multiple ARNs, respecting cache.
#
def prefetch_tags(arns)
  now = Time.now

  # Filter to ARNs not in cache or expired
  arns_to_fetch = arns.select do |arn|
    !TAG_CACHE.key?(arn) || TAG_CACHE[arn][:expires] < now
  end

  log "Cache status: #{arns.size} ARNs, #{arns_to_fetch.size} need fetching"

  return if arns_to_fetch.empty?

  # Resource Groups Tagging API allows up to 100 ARNs per request
  arns_to_fetch.each_slice(100) do |batch|
    begin
      log "Calling GetResources for #{batch.size} ARNs: #{batch.inspect}"
      response = TAGGING_CLIENT.get_resources(resource_arn_list: batch)

      log "GetResources returned #{response.resource_tag_mapping_list.size} resources"

      response.resource_tag_mapping_list.each do |resource|
        arn = resource.resource_arn
        tags = resource.tags.to_h { |tag| [tag.key, tag.value] }
        log "  #{arn}: #{tags.inspect}"
        TAG_CACHE[arn] = {
          tags: tags,
          expires: now + (CACHE_TTL_MINUTES * 60)
        }
      end

      # Cache empty result for ARNs not found (may not have tags)
      found_arns = response.resource_tag_mapping_list.map(&:resource_arn).to_set
      batch.each do |arn|
        next if found_arns.include?(arn)

        log "  #{arn}: not found in response (no tags or doesn't exist)"
        TAG_CACHE[arn] = {
          tags: {},
          expires: now + (CACHE_TTL_MINUTES * 60)
        }
      end

    rescue StandardError => e
      log "GetResources error: #{e.class} - #{e.message}"
      log e.backtrace.first(5).join("\n")
      # On API error, cache empty to avoid repeated failures
      batch.each do |arn|
        TAG_CACHE[arn] = {
          tags: {},
          expires: now + 60  # Short TTL on error
        }
      end
    end
  end
end

#
# Get tags for a resource ARN from cache.
#
def get_tags(resource_arn)
  return {} unless resource_arn

  cached = TAG_CACHE[resource_arn]
  return cached[:tags] if cached && cached[:expires] > Time.now

  {}
end

# =============================================================================
# Property Enrichment - Bulk Load Functions
# =============================================================================

#
# Load all running EC2 instances and in-use EBS volumes into cache.
#
def load_ec2_properties
  return if EC2_CACHE[:loaded] && EC2_CACHE[:expires] > Time.now

  log 'Loading EC2 instances and EBS volumes'

  # Build new caches in temporary variables to avoid data loss on error
  new_instances = {}
  new_volumes = {}

  # Load running instances
  EC2_CLIENT.describe_instances(
    filters: [{ name: 'instance-state-name', values: ['running'] }]
  ).each_page do |page|
    page.reservations.each do |reservation|
      reservation.instances.each do |instance|
        new_instances[instance.instance_id] = extract_ec2_instance_properties(instance)
      end
    end
  end

  # Load in-use volumes
  EC2_CLIENT.describe_volumes(
    filters: [{ name: 'status', values: ['in-use'] }]
  ).each_page do |page|
    page.volumes.each do |volume|
      new_volumes[volume.volume_id] = extract_ebs_volume_properties(volume)
    end
  end

  # Atomic swap - only update cache after successful completion
  EC2_CACHE[:instances] = new_instances
  EC2_CACHE[:volumes] = new_volumes
  EC2_CACHE[:loaded] = true
  EC2_CACHE[:expires] = Time.now + (CACHE_TTL_MINUTES * 60)
  log "Cached #{new_instances.size} EC2 instances, #{new_volumes.size} EBS volumes"
rescue StandardError => e
  log "Error loading EC2 properties: #{e.class} - #{e.message}"
  # Mark as loaded with short TTL to prevent retry storms
  EC2_CACHE[:loaded] = true
  EC2_CACHE[:expires] = Time.now + 60
end

#
# Load all RDS instances into cache.
#
def load_rds_properties
  return if RDS_CACHE[:loaded] && RDS_CACHE[:expires] > Time.now

  log 'Loading RDS instances'

  # Build new cache in temporary variable to avoid data loss on error
  new_instances = {}

  RDS_CLIENT.describe_db_instances.each_page do |page|
    page.db_instances.each do |db|
      new_instances[db.db_instance_identifier] = extract_rds_instance_properties(db)
    end
  end

  # Atomic swap - only update cache after successful completion
  RDS_CACHE[:instances] = new_instances
  RDS_CACHE[:loaded] = true
  RDS_CACHE[:expires] = Time.now + (CACHE_TTL_MINUTES * 60)
  log "Cached #{new_instances.size} RDS instances"
rescue StandardError => e
  log "Error loading RDS properties: #{e.class} - #{e.message}"
  # Mark as loaded with short TTL to prevent retry storms
  RDS_CACHE[:loaded] = true
  RDS_CACHE[:expires] = Time.now + 60
end

#
# Load all Lambda functions into cache.
#
def load_lambda_properties
  return if LAMBDA_CACHE[:loaded] && LAMBDA_CACHE[:expires] > Time.now

  log 'Loading Lambda functions'

  # Build new cache in temporary variable to avoid data loss on error
  new_functions = {}

  LAMBDA_CLIENT.list_functions.each_page do |page|
    page.functions.each do |func|
      new_functions[func.function_name] = extract_lambda_function_properties(func)
    end
  end

  # Atomic swap - only update cache after successful completion
  LAMBDA_CACHE[:functions] = new_functions
  LAMBDA_CACHE[:loaded] = true
  LAMBDA_CACHE[:expires] = Time.now + (CACHE_TTL_MINUTES * 60)
  log "Cached #{new_functions.size} Lambda functions"
rescue StandardError => e
  log "Error loading Lambda properties: #{e.class} - #{e.message}"
  # Mark as loaded with short TTL to prevent retry storms
  LAMBDA_CACHE[:loaded] = true
  LAMBDA_CACHE[:expires] = Time.now + 60
end

# =============================================================================
# Property Enrichment - Helper Functions
# =============================================================================

def extract_ec2_instance_properties(instance)
  instance_type = instance.instance_type
  {
    'InstanceType' => instance_type,
    'InstanceFamily' => instance_type&.split('.')&.first,
    'InstanceSize' => instance_type&.split('.')&.last,
    'Architecture' => instance.architecture,
    'AvailabilityZone' => instance.placement&.availability_zone,
    'Platform' => instance.platform_details,
    'ImageId' => instance.image_id,
    'InstanceLifecycle' => instance.instance_lifecycle || 'on-demand'
  }
end

def extract_ebs_volume_properties(volume)
  {
    'VolumeType' => volume.volume_type,
    'Size' => volume.size,
    'AvailabilityZone' => volume.availability_zone,
    'Iops' => volume.iops,
    'Throughput' => volume.throughput,
    'Encrypted' => volume.encrypted
  }
end

def extract_rds_instance_properties(db)
  {
    'DBInstanceClass' => db.db_instance_class,
    'Engine' => db.engine,
    'EngineVersion' => db.engine_version,
    'AvailabilityZone' => db.availability_zone,
    'MultiAZ' => db.multi_az,
    'StorageType' => db.storage_type,
    'AllocatedStorage' => db.allocated_storage
  }
end

def extract_lambda_function_properties(func)
  {
    'Runtime' => func.runtime,
    'MemorySize' => func.memory_size,
    'Timeout' => func.timeout,
    'Architecture' => func.architectures&.first,
    'PackageType' => func.package_type
  }
end

# =============================================================================
# Property Enrichment - Backfill Functions (for cache misses)
# =============================================================================

def backfill_ec2_instance(instance_id)
  return if EC2_CACHE[:instances].key?(instance_id)

  log "Backfilling EC2 instance #{instance_id}"
  response = EC2_CLIENT.describe_instances(instance_ids: [instance_id])
  response.reservations.each do |reservation|
    reservation.instances.each do |instance|
      EC2_CACHE[:instances][instance.instance_id] = extract_ec2_instance_properties(instance)
    end
  end
rescue StandardError => e
  log "Error backfilling EC2 instance #{instance_id}: #{e.class} - #{e.message}"
  # Cache empty hash as sentinel to prevent repeated API calls
  EC2_CACHE[:instances][instance_id] = {}
end

def backfill_ebs_volume(volume_id)
  return if EC2_CACHE[:volumes].key?(volume_id)

  log "Backfilling EBS volume #{volume_id}"
  response = EC2_CLIENT.describe_volumes(volume_ids: [volume_id])
  response.volumes.each do |volume|
    EC2_CACHE[:volumes][volume.volume_id] = extract_ebs_volume_properties(volume)
  end
rescue StandardError => e
  log "Error backfilling EBS volume #{volume_id}: #{e.class} - #{e.message}"
  # Cache empty hash as sentinel to prevent repeated API calls
  EC2_CACHE[:volumes][volume_id] = {}
end

def backfill_rds_instance(db_identifier)
  return if RDS_CACHE[:instances].key?(db_identifier)

  log "Backfilling RDS instance #{db_identifier}"
  response = RDS_CLIENT.describe_db_instances(db_instance_identifier: db_identifier)
  response.db_instances.each do |db|
    RDS_CACHE[:instances][db.db_instance_identifier] = extract_rds_instance_properties(db)
  end
rescue StandardError => e
  log "Error backfilling RDS instance #{db_identifier}: #{e.class} - #{e.message}"
  # Cache empty hash as sentinel to prevent repeated API calls
  RDS_CACHE[:instances][db_identifier] = {}
end

def backfill_lambda_function(function_name)
  return if LAMBDA_CACHE[:functions].key?(function_name)

  log "Backfilling Lambda function #{function_name}"
  func = LAMBDA_CLIENT.get_function(function_name: function_name).configuration
  LAMBDA_CACHE[:functions][function_name] = extract_lambda_function_properties(func)
rescue StandardError => e
  log "Error backfilling Lambda function #{function_name}: #{e.class} - #{e.message}"
  # Cache empty hash as sentinel to prevent repeated API calls
  LAMBDA_CACHE[:functions][function_name] = {}
end

# =============================================================================
# Property Enrichment - Lookup Functions
# =============================================================================

def get_ec2_properties(instance_id)
  load_ec2_properties
  backfill_ec2_instance(instance_id) unless EC2_CACHE[:instances].key?(instance_id)
  EC2_CACHE[:instances][instance_id]
end

def get_ebs_properties(volume_id)
  load_ec2_properties
  backfill_ebs_volume(volume_id) unless EC2_CACHE[:volumes].key?(volume_id)
  EC2_CACHE[:volumes][volume_id]
end

def get_rds_properties(db_identifier)
  load_rds_properties
  backfill_rds_instance(db_identifier) unless RDS_CACHE[:instances].key?(db_identifier)
  RDS_CACHE[:instances][db_identifier]
end

def get_lambda_properties(function_name)
  load_lambda_properties
  backfill_lambda_function(function_name) unless LAMBDA_CACHE[:functions].key?(function_name)
  LAMBDA_CACHE[:functions][function_name]
end

#
# Add tags and properties to a metric record.
#
# Adds tags as a 'tags' field and resource properties as a 'properties' field.
#
def enrich_record(payload, resource_arn)
  tags = get_tags(resource_arn)

  if tags.any?
    log "Enriching metric with #{tags.size} tags"
    payload['tags'] = tags

    # Also add common tags as top-level fields for easier querying
    payload['resource_name'] = tags['Name'] if tags['Name']
    payload['environment'] = tags['Environment'] if tags['Environment']
    payload['team'] = tags['Team'] if tags['Team']
  else
    log "No tags to add for #{resource_arn}"
  end

  # Add resource properties based on namespace
  properties = get_properties_for_metric(payload)
  payload['properties'] = properties if properties&.any?

  payload
end

#
# Get resource properties based on metric namespace and dimensions.
#
def get_properties_for_metric(payload)
  namespace = payload['namespace'] || ''
  dimensions = payload['dimensions'] || {}

  case namespace
  when 'AWS/EC2'
    instance_id = dimensions['InstanceId']
    return get_ec2_properties(instance_id) if instance_id

  when 'AWS/EBS'
    volume_id = dimensions['VolumeId']
    return get_ebs_properties(volume_id) if volume_id

  when 'AWS/RDS'
    db_identifier = dimensions['DBInstanceIdentifier']
    return get_rds_properties(db_identifier) if db_identifier

  when 'AWS/Lambda'
    function_name = dimensions['FunctionName']
    return get_lambda_properties(function_name) if function_name
  end

  nil
end
