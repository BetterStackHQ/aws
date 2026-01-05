# frozen_string_literal: true

require 'json'
require 'base64'
require 'zlib'
require 'stringio'
require 'aws-sdk-resourcegroupstaggingapi'
require 'aws-sdk-ec2'

#
# Firehose Logs Tag Enrichment Lambda
#
# Enriches CloudWatch Logs with AWS resource tags before delivery.
# Receives batches of log records from Firehose, extracts resource identifiers
# from log group/stream names, looks up tags, and adds them to each log event.
#
# CloudWatch Logs subscription data format (gzip compressed):
# {
#   "messageType": "DATA_MESSAGE",
#   "owner": "123456789012",
#   "logGroup": "/ec2/cloudwatch-test/syslog",
#   "logStream": "i-0123456789abcdef0",
#   "subscriptionFilters": ["betterstack-logs"],
#   "logEvents": [
#     { "id": "...", "timestamp": 1234567890123, "message": "log message" }
#   ]
# }
#
# After enrichment:
# {
#   ...
#   "tags": { "Name": "web-server-1", "Environment": "production" },
#   "resource_name": "web-server-1",
#   "logEvents": [...]
# }
#

# In-memory tag cache
TAG_CACHE = {}
CACHE_TTL_MINUTES = Integer(ENV.fetch('CACHE_TTL_MINUTES', '10'))
ACCOUNT_ID = ENV.fetch('ACCOUNT_ID', '')
REGION = ENV.fetch('AWS_REGION', 'us-east-1')
DEBUG = ENV.fetch('DEBUG', 'false') == 'true'

# Initialize clients outside handler for connection reuse
TAGGING_CLIENT = Aws::ResourceGroupsTaggingAPI::Client.new
EC2_CLIENT = Aws::EC2::Client.new

# Pattern to extract instance ID from log group or log stream
# Matches: i-0123456789abcdef0 or i-abcdef0123456789
INSTANCE_ID_PATTERN = /\b(i-[0-9a-f]{8,17})\b/i

def log(message)
  puts "[LogsTagEnrichment] #{message}" if DEBUG
end

def lambda_handler(event:, context:)
  log "Invoked with #{event['records'].size} records"

  output_records = []

  # Collect all resource ARNs for batch lookup
  records_data = []

  event['records'].each do |record|
    begin
      # Decode and decompress the data
      compressed_data = Base64.decode64(record['data'])
      uncompressed = Zlib::GzipReader.new(StringIO.new(compressed_data)).read
      payload = JSON.parse(uncompressed)

      log "Record #{record['recordId']}: logGroup=#{payload['logGroup']}, logStream=#{payload['logStream']}"

      # Skip control messages
      if payload['messageType'] == 'CONTROL_MESSAGE'
        log "Skipping control message"
        output_records << {
          'recordId' => record['recordId'],
          'result' => 'Dropped',
          'data' => record['data']
        }
        next
      end

      # Extract resource ARN from log group/stream
      resource_arn = extract_resource_arn(payload)

      records_data << {
        record: record,
        payload: payload,
        resource_arn: resource_arn
      }
    rescue Zlib::GzipFile::Error => e
      log "Not gzip data, passing through: #{e.message}"
      # Not gzip compressed, pass through unchanged
      output_records << {
        'recordId' => record['recordId'],
        'result' => 'Ok',
        'data' => record['data']
      }
    rescue StandardError => e
      log "Error processing record #{record['recordId']}: #{e.class} - #{e.message}"
      output_records << {
        'recordId' => record['recordId'],
        'result' => 'Ok',
        'data' => record['data']
      }
    end
  end

  # Batch fetch tags for all unique ARNs
  unique_arns = records_data.map { |r| r[:resource_arn] }.compact.uniq
  log "Fetching tags for #{unique_arns.size} unique ARNs"
  prefetch_tags(unique_arns)

  # Enrich each record
  records_data.each do |item|
    begin
      enriched = enrich_log_record(item[:payload], item[:resource_arn])

      # Re-compress the enriched data
      compressed = gzip_compress(JSON.generate(enriched))

      output_records << {
        'recordId' => item[:record]['recordId'],
        'result' => 'Ok',
        'data' => Base64.strict_encode64(compressed)
      }
    rescue StandardError => e
      log "Error enriching record #{item[:record]['recordId']}: #{e.class} - #{e.message}"
      output_records << {
        'recordId' => item[:record]['recordId'],
        'result' => 'Ok',
        'data' => item[:record]['data']
      }
    end
  end

  log "Returning #{output_records.size} records"
  { 'records' => output_records }
end

def gzip_compress(data)
  io = StringIO.new
  io.set_encoding('BINARY')
  gz = Zlib::GzipWriter.new(io)
  gz.write(data)
  gz.close
  io.string
end

def extract_resource_arn(payload)
  log_group = payload['logGroup'] || ''
  log_stream = payload['logStream'] || ''

  # Special case: RDS Enhanced Monitoring (logGroup = "RDSOSMetrics")
  # The DB identifier is inside the JSON message body, not in log group/stream
  if log_group == 'RDSOSMetrics'
    return extract_rds_enhanced_monitoring_arn(payload)
  end

  # Try to find instance ID in log stream first (most common pattern)
  if (match = log_stream.match(INSTANCE_ID_PATTERN))
    instance_id = match[1]
    arn = "arn:aws:ec2:#{REGION}:#{ACCOUNT_ID}:instance/#{instance_id}"
    log "  Found instance ID in log stream: #{instance_id} -> #{arn}"
    return arn
  end

  # Try to find instance ID in log group
  if (match = log_group.match(INSTANCE_ID_PATTERN))
    instance_id = match[1]
    arn = "arn:aws:ec2:#{REGION}:#{ACCOUNT_ID}:instance/#{instance_id}"
    log "  Found instance ID in log group: #{instance_id} -> #{arn}"
    return arn
  end

  # Check for Lambda function pattern: /aws/lambda/{function-name}
  if log_group.start_with?('/aws/lambda/')
    function_name = log_group.sub('/aws/lambda/', '')
    arn = "arn:aws:lambda:#{REGION}:#{ACCOUNT_ID}:function:#{function_name}"
    log "  Found Lambda function: #{function_name} -> #{arn}"
    return arn
  end

  # Check for RDS pattern: /aws/rds/instance/{db-instance}/{log-type}
  if log_group.start_with?('/aws/rds/instance/')
    parts = log_group.sub('/aws/rds/instance/', '').split('/')
    db_instance = parts[0]
    if db_instance
      arn = "arn:aws:rds:#{REGION}:#{ACCOUNT_ID}:db:#{db_instance}"
      log "  Found RDS instance: #{db_instance} -> #{arn}"
      return arn
    end
  end

  # Check for ECS pattern: /ecs/{cluster}/{service}
  if log_group.start_with?('/ecs/')
    parts = log_group.sub('/ecs/', '').split('/')
    cluster = parts[0]
    if cluster
      arn = "arn:aws:ecs:#{REGION}:#{ACCOUNT_ID}:cluster/#{cluster}"
      log "  Found ECS cluster: #{cluster} -> #{arn}"
      return arn
    end
  end

  # Check for API Gateway pattern: /aws/api-gateway/{api-id} or /aws/http-api/{api-id}
  if log_group.match?(%r{^/aws/(api-gateway|http-api)/})
    api_id = log_group.split('/').last
    arn = "arn:aws:apigateway:#{REGION}::/restapis/#{api_id}"
    log "  Found API Gateway: #{api_id} -> #{arn}"
    return arn
  end

  log "  No resource ARN extracted from logGroup=#{log_group}, logStream=#{log_stream}"
  nil
end

# Extract RDS ARN from Enhanced Monitoring logs
# These logs have logGroup="RDSOSMetrics" and the DB identifier is in the message body
# Message format: { "engine": "POSTGRES", "instanceID": "cloudwatch-test-postgres", ... }
def extract_rds_enhanced_monitoring_arn(payload)
  log_events = payload['logEvents'] || []
  return nil if log_events.empty?

  # Parse the first log event's message (it's JSON)
  first_event = log_events.first
  message = first_event['message']

  # Message might be a string (JSON) or already parsed
  if message.is_a?(String)
    begin
      message = JSON.parse(message)
    rescue JSON::ParserError => e
      log "  Failed to parse RDSOSMetrics message: #{e.message}"
      return nil
    end
  end

  instance_id = message['instanceID']
  unless instance_id
    log "  No instanceID found in RDSOSMetrics message"
    return nil
  end

  arn = "arn:aws:rds:#{REGION}:#{ACCOUNT_ID}:db:#{instance_id}"
  log "  Found RDS Enhanced Monitoring: #{instance_id} -> #{arn}"
  arn
end

def prefetch_tags(arns)
  now = Time.now

  arns_to_fetch = arns.select do |arn|
    !TAG_CACHE.key?(arn) || TAG_CACHE[arn][:expires] < now
  end

  log "Cache status: #{arns.size} ARNs, #{arns_to_fetch.size} need fetching"

  return if arns_to_fetch.empty?

  arns_to_fetch.each_slice(100) do |batch|
    begin
      log "Calling GetResources for #{batch.size} ARNs"
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

      found_arns = response.resource_tag_mapping_list.map(&:resource_arn).to_set
      batch.each do |arn|
        next if found_arns.include?(arn)

        log "  #{arn}: not found (no tags or doesn't exist)"
        TAG_CACHE[arn] = {
          tags: {},
          expires: now + (CACHE_TTL_MINUTES * 60)
        }
      end

    rescue StandardError => e
      log "GetResources error: #{e.class} - #{e.message}"
      batch.each do |arn|
        TAG_CACHE[arn] = {
          tags: {},
          expires: now + 60
        }
      end
    end
  end
end

def get_tags(resource_arn)
  return {} unless resource_arn

  cached = TAG_CACHE[resource_arn]
  return cached[:tags] if cached && cached[:expires] > Time.now

  {}
end

def enrich_log_record(payload, resource_arn)
  tags = get_tags(resource_arn)

  if tags.any?
    log "Enriching log record with #{tags.size} tags"
    payload['tags'] = tags
    payload['resource_name'] = tags['Name'] if tags['Name']
    payload['environment'] = tags['Environment'] if tags['Environment']
    payload['team'] = tags['Team'] if tags['Team']
  else
    log "No tags to add for #{resource_arn}"
  end

  payload
end
