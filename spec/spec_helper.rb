# frozen_string_literal: true

# Set test environment variables before requiring Lambda code
ENV['ACCOUNT_ID'] = '123456789012'
ENV['AWS_REGION'] = 'us-east-1'
ENV['CACHE_TTL_MINUTES'] = '10'
ENV['DEBUG'] = 'false'

# Configure AWS SDK for testing - must be done before requiring any SDK code
require 'aws-sdk-core'
Aws.config.update(
  stub_responses: true,
  region: 'us-east-1',
  credentials: Aws::Credentials.new('test', 'test')
)

# Suppress constant redefinition warnings when loading both lambda files
# (they share some constant names like TAG_CACHE, REGION, etc.)
$VERBOSE = nil

RSpec.configure do |config|
  config.expect_with :rspec do |expectations|
    expectations.include_chain_clauses_in_custom_matcher_descriptions = true
  end

  config.mock_with :rspec do |mocks|
    mocks.verify_partial_doubles = true
  end

  config.shared_context_metadata_behavior = :apply_to_host_groups
  config.filter_run_when_matching :focus
  config.disable_monkey_patching!
  config.order = :random
  Kernel.srand config.seed
end
