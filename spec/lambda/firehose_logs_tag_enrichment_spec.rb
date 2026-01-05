# frozen_string_literal: true

require 'spec_helper'
require_relative '../../lambda/firehose_logs_tag_enrichment'

RSpec.describe 'Firehose Logs Tag Enrichment' do
  before do
    # Reset cache between tests
    TAG_CACHE.clear

    # Stub AWS clients
    TAGGING_CLIENT.stub_responses(:get_resources, {
      resource_tag_mapping_list: []
    })
    EC2_CLIENT.stub_responses(:describe_instances, {
      reservations: []
    })
  end

  describe '#extract_resource_arn' do
    it 'extracts instance ID from log stream' do
      payload = {
        'logGroup' => '/ec2/syslog',
        'logStream' => 'i-1234567890abcdef0'
      }

      arn = extract_resource_arn(payload)

      expect(arn).to eq('arn:aws:ec2:us-east-1:123456789012:instance/i-1234567890abcdef0')
    end

    it 'extracts instance ID from log group' do
      payload = {
        'logGroup' => '/ec2/i-abcdef1234567890/syslog',
        'logStream' => 'some-stream'
      }

      arn = extract_resource_arn(payload)

      expect(arn).to eq('arn:aws:ec2:us-east-1:123456789012:instance/i-abcdef1234567890')
    end

    it 'extracts Lambda function from log group' do
      payload = {
        'logGroup' => '/aws/lambda/my-function',
        'logStream' => '2024/01/01/[$LATEST]abc123'
      }

      arn = extract_resource_arn(payload)

      expect(arn).to eq('arn:aws:lambda:us-east-1:123456789012:function:my-function')
    end

    it 'extracts RDS instance from log group' do
      payload = {
        'logGroup' => '/aws/rds/instance/my-database/postgresql',
        'logStream' => 'some-stream'
      }

      arn = extract_resource_arn(payload)

      expect(arn).to eq('arn:aws:rds:us-east-1:123456789012:db:my-database')
    end

    it 'extracts ECS cluster from log group' do
      payload = {
        'logGroup' => '/ecs/my-cluster/my-service',
        'logStream' => 'ecs/container/abc123'
      }

      arn = extract_resource_arn(payload)

      expect(arn).to eq('arn:aws:ecs:us-east-1:123456789012:cluster/my-cluster')
    end

    it 'extracts API Gateway from log group' do
      payload = {
        'logGroup' => '/aws/api-gateway/abc123xyz',
        'logStream' => 'some-stream'
      }

      arn = extract_resource_arn(payload)

      expect(arn).to eq('arn:aws:apigateway:us-east-1::/restapis/abc123xyz')
    end

    it 'returns nil when no resource can be extracted' do
      payload = {
        'logGroup' => '/custom/application/logs',
        'logStream' => 'app-stream'
      }

      arn = extract_resource_arn(payload)

      expect(arn).to be_nil
    end
  end

  describe '#extract_rds_enhanced_monitoring_arn' do
    it 'extracts RDS instance ID from Enhanced Monitoring message' do
      payload = {
        'logGroup' => 'RDSOSMetrics',
        'logStream' => 'db-ABC123',
        'logEvents' => [{
          'id' => '123',
          'timestamp' => 1_704_067_200_000,
          'message' => '{"engine":"POSTGRES","instanceID":"my-database","uptime":"1234"}'
        }]
      }

      arn = extract_resource_arn(payload)

      expect(arn).to eq('arn:aws:rds:us-east-1:123456789012:db:my-database')
    end

    it 'returns nil when message is not valid JSON' do
      payload = {
        'logGroup' => 'RDSOSMetrics',
        'logStream' => 'db-ABC123',
        'logEvents' => [{
          'id' => '123',
          'timestamp' => 1_704_067_200_000,
          'message' => 'not json'
        }]
      }

      arn = extract_resource_arn(payload)

      expect(arn).to be_nil
    end

    it 'returns nil when no log events' do
      payload = {
        'logGroup' => 'RDSOSMetrics',
        'logStream' => 'db-ABC123',
        'logEvents' => []
      }

      arn = extract_resource_arn(payload)

      expect(arn).to be_nil
    end
  end

  describe '#enrich_log_record' do
    before do
      TAG_CACHE['arn:aws:ec2:us-east-1:123456789012:instance/i-test'] = {
        tags: { 'Name' => 'test-server', 'Environment' => 'production', 'Team' => 'platform' },
        expires: Time.now + 600
      }
    end

    it 'adds tags to payload' do
      payload = {
        'logGroup' => '/ec2/syslog',
        'logStream' => 'i-test',
        'logEvents' => []
      }
      resource_arn = 'arn:aws:ec2:us-east-1:123456789012:instance/i-test'

      result = enrich_log_record(payload, resource_arn)

      expect(result['tags']).to eq({
        'Name' => 'test-server',
        'Environment' => 'production',
        'Team' => 'platform'
      })
      expect(result['resource_name']).to eq('test-server')
      expect(result['environment']).to eq('production')
      expect(result['team']).to eq('platform')
    end

    it 'does not add fields when no tags exist' do
      payload = {
        'logGroup' => '/ec2/syslog',
        'logStream' => 'i-unknown',
        'logEvents' => []
      }

      result = enrich_log_record(payload, 'arn:aws:ec2:us-east-1:123456789012:instance/i-unknown')

      expect(result).not_to have_key('tags')
      expect(result).not_to have_key('resource_name')
    end
  end

  describe '#gzip_compress' do
    it 'compresses data with gzip' do
      data = '{"test": "data"}'

      compressed = gzip_compress(data)

      # Verify it's valid gzip by decompressing
      decompressed = Zlib::GzipReader.new(StringIO.new(compressed)).read
      expect(decompressed).to eq(data)
    end
  end

  describe '#prefetch_tags' do
    it 'fetches tags for uncached ARNs' do
      TAGGING_CLIENT.stub_responses(:get_resources, {
        resource_tag_mapping_list: [{
          resource_arn: 'arn:aws:ec2:us-east-1:123456789012:instance/i-new',
          tags: [
            { key: 'Name', value: 'new-server' },
            { key: 'Environment', value: 'staging' }
          ]
        }]
      })

      prefetch_tags(['arn:aws:ec2:us-east-1:123456789012:instance/i-new'])

      expect(TAG_CACHE).to have_key('arn:aws:ec2:us-east-1:123456789012:instance/i-new')
      expect(TAG_CACHE['arn:aws:ec2:us-east-1:123456789012:instance/i-new'][:tags]).to eq({
        'Name' => 'new-server',
        'Environment' => 'staging'
      })
    end

    it 'caches empty tags for ARNs not found' do
      TAGGING_CLIENT.stub_responses(:get_resources, {
        resource_tag_mapping_list: []
      })

      prefetch_tags(['arn:aws:ec2:us-east-1:123456789012:instance/i-notfound'])

      expect(TAG_CACHE).to have_key('arn:aws:ec2:us-east-1:123456789012:instance/i-notfound')
      expect(TAG_CACHE['arn:aws:ec2:us-east-1:123456789012:instance/i-notfound'][:tags]).to eq({})
    end

    it 'does not re-fetch cached ARNs' do
      TAG_CACHE['arn:aws:ec2:us-east-1:123456789012:instance/i-cached'] = {
        tags: { 'Name' => 'cached' },
        expires: Time.now + 600
      }

      expect(TAGGING_CLIENT).not_to receive(:get_resources)

      prefetch_tags(['arn:aws:ec2:us-east-1:123456789012:instance/i-cached'])
    end
  end

  describe '#get_tags' do
    it 'returns cached tags' do
      TAG_CACHE['arn:aws:ec2:us-east-1:123456789012:instance/i-test'] = {
        tags: { 'Name' => 'test' },
        expires: Time.now + 600
      }

      tags = get_tags('arn:aws:ec2:us-east-1:123456789012:instance/i-test')

      expect(tags).to eq({ 'Name' => 'test' })
    end

    it 'returns empty hash for expired cache' do
      TAG_CACHE['arn:aws:ec2:us-east-1:123456789012:instance/i-expired'] = {
        tags: { 'Name' => 'expired' },
        expires: Time.now - 1
      }

      tags = get_tags('arn:aws:ec2:us-east-1:123456789012:instance/i-expired')

      expect(tags).to eq({})
    end

    it 'returns empty hash for nil ARN' do
      tags = get_tags(nil)

      expect(tags).to eq({})
    end
  end
end
