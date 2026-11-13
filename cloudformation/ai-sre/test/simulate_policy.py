#!/usr/bin/env python3
"""Asserts what a deployed Better Stack AI SRE role may and may not do, with IAM's policy simulator.

Every read the role must allow, every explicit deny, and every read AllowConfigurationValues=false must deny is
listed in CHECKS below with the decision expected under each setting of AllowConfigurationValues. With true the role
also has AWS's ReadOnlyAccess, so reads only that policy grants are allowed there and not with false. The script asks
iam:SimulatePrincipalPolicy for each one on the given role and fails on any decision that differs. The simulator
evaluates the role's own policies (inline and attached) and its permissions boundary, not service control policies or
resource policies.

Usage: simulate_policy.py --role-arn ARN --allow-configuration-values true|false [--region us-east-1]
Needs iam:SimulatePrincipalPolicy on the role. Calls nothing else.
"""

import argparse
import sys
from collections import defaultdict

ALLOW, DENY, NONE = "allowed", "explicitDeny", "implicitDeny"

# Resources, filled in for the role's account and region by fill().
ANY = "*"
SELF = "{self}"
OTHER_ROLE = "arn:aws:iam::{account}:role/some-other-role"
# The stack's own deny policies, named from the role's suffix, and a policy that isn't the stack's.
DENY_DATA_POLICY = "arn:aws:iam::{account}:policy/better-stack-ai-sre-deny-data-{suffix}"
DENY_STORED_DATA_POLICY = "arn:aws:iam::{account}:policy/better-stack-ai-sre-deny-stored-data-{suffix}"
OTHER_POLICY = "arn:aws:iam::{account}:policy/some-other-policy"
MCP = "arn:aws:signin:{region}:{account}:service-principal/aws-mcp.amazonaws.com"
ACCESS_POINT_LOG_OBJECT = "arn:aws:s3:{region}:{account}:accesspoint/ap/object/AWSLogs/{account}/x"
OBJECT_LAMBDA_LOG_OBJECT = "arn:aws:s3-object-lambda:{region}:{account}:accesspoint/olap/object/AWSLogs/{account}/x"


def apigw(path):
    return "arn:aws:apigateway:{region}::" + path


def via(service):
    return (("kms:ViaService", service + ".{region}.amazonaws.com"),)


def both(decision):
    return decision, decision


CONFIG = (ALLOW, DENY)  # readable by default, denied with AllowConfigurationValues=false
BROAD = (ALLOW, NONE)  # granted by ReadOnlyAccess or BetterStackAiSreBroadAccess, so only with true
# kms:ViaService values the default setting may decrypt through, besides CloudWatch Logs (allowed in both).
KMS_BROAD_SERVICES = ["lambda", "s3", "xray", "rds", "states", "glue"]

# (action, resource, context, decision with true, decision with false)
CHECKS = [
    # Copied from the read access of better-stack-full.yaml.
    ("cloudwatch:DescribeAlarms", ANY, (), *both(ALLOW)),
    ("cloudwatch:GetMetricData", ANY, (), *both(ALLOW)),
    ("cloudwatch:ListMetrics", ANY, (), *both(ALLOW)),
    ("logs:DescribeLogGroups", ANY, (), *both(ALLOW)),
    ("logs:GetLogEvents", ANY, (), *both(ALLOW)),
    ("logs:ListTagsForResource", ANY, (), *both(ALLOW)),
    ("logs:FilterLogEvents", ANY, (), *both(ALLOW)),
    ("ec2:DescribeInstances", ANY, (), *both(ALLOW)),
    ("ec2:DescribeSecurityGroups", ANY, (), *both(ALLOW)),
    ("tag:GetResources", ANY, (), *both(ALLOW)),
    ("autoscaling:DescribeAutoScalingGroups", ANY, (), *both(ALLOW)),
    ("ecs:DescribeServices", ANY, (), *both(ALLOW)),
    ("ecs:ListClusters", ANY, (), *both(ALLOW)),
    ("eks:DescribeCluster", ANY, (), *both(ALLOW)),
    ("eks:ListClusters", ANY, (), *both(ALLOW)),
    ("batch:DescribeJobQueues", ANY, (), *both(ALLOW)),
    ("batch:ListJobs", ANY, (), *both(ALLOW)),
    ("lambda:ListEventSourceMappings", ANY, (), *both(ALLOW)),
    ("rds:DescribeDBInstances", ANY, (), *both(ALLOW)),
    ("rds:ListTagsForResource", ANY, (), *both(ALLOW)),
    ("dynamodb:DescribeTable", ANY, (), *both(ALLOW)),
    ("dynamodb:ListTables", ANY, (), *both(ALLOW)),
    ("elasticache:DescribeCacheClusters", ANY, (), *both(ALLOW)),
    ("s3:ListAllMyBuckets", ANY, (), *both(ALLOW)),
    ("s3:GetBucketLocation", ANY, (), *both(ALLOW)),
    ("s3:GetBucketTagging", ANY, (), *both(ALLOW)),
    ("elasticfilesystem:DescribeFileSystems", ANY, (), *both(ALLOW)),
    ("elasticfilesystem:DescribeTags", ANY, (), *both(ALLOW)),
    ("elasticloadbalancing:DescribeLoadBalancers", ANY, (), *both(ALLOW)),
    ("elasticloadbalancing:DescribeTargetHealth", ANY, (), *both(ALLOW)),
    ("apigateway:GET", apigw("/restapis"), (), *both(ALLOW)),
    ("apigateway:GET", apigw("/restapis/abc123/resources"), (), *both(ALLOW)),
    ("apigateway:GET", apigw("/apis"), (), *both(ALLOW)),
    ("route53:ListHostedZones", ANY, (), *both(ALLOW)),
    ("sqs:ListQueues", ANY, (), *both(ALLOW)),
    ("sqs:GetQueueAttributes", ANY, (), *both(ALLOW)),
    ("sns:ListTopics", ANY, (), *both(ALLOW)),
    ("sns:GetTopicAttributes", ANY, (), *both(ALLOW)),
    ("kinesis:DescribeStream", ANY, (), *both(ALLOW)),
    ("kinesis:ListStreams", ANY, (), *both(ALLOW)),
    ("firehose:ListDeliveryStreams", ANY, (), *both(ALLOW)),
    ("states:DescribeStateMachine", ANY, (), *both(ALLOW)),
    ("states:ListStateMachines", ANY, (), *both(ALLOW)),
    ("events:ListRules", ANY, (), *both(ALLOW)),
    ("events:DescribeRule", ANY, (), *both(ALLOW)),
    ("cloudtrail:DescribeTrails", ANY, (), *both(ALLOW)),
    ("cloudtrail:GetTrailStatus", ANY, (), *both(ALLOW)),
    ("cloudtrail:ListTrails", ANY, (), *both(ALLOW)),
    ("health:DescribeEvents", ANY, (), *both(ALLOW)),
    # Investigation reads.
    ("logs:StartQuery", ANY, (), *both(ALLOW)),
    ("logs:StopQuery", ANY, (), *both(ALLOW)),
    ("logs:GetQueryResults", ANY, (), *both(ALLOW)),
    ("cloudtrail:LookupEvents", ANY, (), *both(ALLOW)),
    ("xray:GetTraceSummaries", ANY, (), *both(ALLOW)),
    ("xray:BatchGetTraces", ANY, (), *both(ALLOW)),
    ("cloudformation:DescribeStackResources", ANY, (), *both(ALLOW)),
    ("cloudformation:ListStacks", ANY, (), *both(ALLOW)),
    ("application-autoscaling:DescribeScalableTargets", ANY, (), *both(ALLOW)),
    ("iam:ListAccountAliases", ANY, (), *both(ALLOW)),
    ("account:ListRegions", ANY, (), *both(ALLOW)),
    # The role's own description and policies; other roles' only through ReadOnlyAccess.
    ("iam:GetRole", SELF, (), *both(ALLOW)),
    ("iam:ListRolePolicies", SELF, (), *both(ALLOW)),
    ("iam:GetRolePolicy", SELF, (), *both(ALLOW)),
    ("iam:ListAttachedRolePolicies", SELF, (), *both(ALLOW)),
    ("iam:GetRole", OTHER_ROLE, (), *BROAD),
    ("iam:ListRolePolicies", OTHER_ROLE, (), *BROAD),
    ("iam:GetRolePolicy", OTHER_ROLE, (), *BROAD),
    ("iam:ListAttachedRolePolicies", OTHER_ROLE, (), *BROAD),
    # The stack's own deny policies, in both settings; other policies only through ReadOnlyAccess.
    *[(action, policy, (), *both(ALLOW)) for action in ("iam:GetPolicy", "iam:GetPolicyVersion")
      for policy in (DENY_DATA_POLICY, DENY_STORED_DATA_POLICY)],
    ("iam:GetPolicy", OTHER_POLICY, (), *BROAD),
    ("iam:GetPolicyVersion", OTHER_POLICY, (), *BROAD),
    # A customer managed key: through CloudWatch Logs in both settings, through the named services with true only
    # (not denied with false, but nothing allows it), never directly or through any other service.
    ("kms:Decrypt", ANY, via("logs"), *both(ALLOW)),
    ("kms:DescribeKey", ANY, via("logs"), *both(ALLOW)),
    *[("kms:Decrypt", ANY, via(service), *BROAD) for service in KMS_BROAD_SERVICES],
    ("kms:Decrypt", ANY, via("secretsmanager"), *both(DENY)),
    ("kms:Decrypt", ANY, via("cloudtrail"), *both(DENY)),
    ("kms:Decrypt", ANY, (), *both(DENY)),
    ("kms:DescribeKey", ANY, (), *BROAD),
    # The MCP token: CreateOAuth2Token alone, for aws-mcp alone.
    ("signin:CreateOAuth2Token", MCP, (), *both(ALLOW)),
    ("signin:AuthorizeOAuth2Access", MCP, (), *both(NONE)),
    # Dropped from ingestion's read access; ReadOnlyAccess grants it.
    ("logs:TestMetricFilter", ANY, (), *BROAD),
    # Secrets and customer data, denied explicitly in every setting.
    ("lambda:GetFunction", ANY, (), *both(DENY)),
    ("lambda:InvokeFunction", ANY, (), *both(DENY)),
    ("s3:GetObject", ANY, (), *both(DENY)),
    ("s3:GetObjectVersion", ANY, (), *both(DENY)),
    # S3 objects: only the logs AWS services deliver under an AWSLogs/ key, and only with true.
    ("s3:GetObject", "arn:aws:s3:::b/AWSLogs/1/x", (), *BROAD),
    ("s3:GetObject", "arn:aws:s3:::b/p/AWSLogs/1/x", (), *BROAD),
    ("s3:GetObjectVersion", "arn:aws:s3:::b/AWSLogs/1/x", (), *BROAD),
    ("s3:GetObject", "arn:aws:s3:::b/data/x", (), *both(DENY)),
    ("s3:GetObject", "arn:aws:s3:::b/AWSLogs", (), *both(DENY)),
    ("s3:GetObject", ACCESS_POINT_LOG_OBJECT, (), *both(DENY)),
    ("s3:GetObject", OBJECT_LAMBDA_LOG_OBJECT, (), *both(DENY)),
    ("s3-object-lambda:GetObject", OBJECT_LAMBDA_LOG_OBJECT, (), *both(DENY)),
    ("s3:ListBucket", "arn:aws:s3:::b", (), *BROAD),
    ("secretsmanager:GetSecretValue", ANY, (), *both(DENY)),
    ("ssm:GetParameter", ANY, (), *both(DENY)),
    ("ssm:GetParameters", ANY, (), *both(DENY)),
    ("ssm:GetParametersByPath", ANY, (), *both(DENY)),
    ("dynamodb:GetItem", ANY, (), *both(DENY)),
    ("dynamodb:BatchGetItem", ANY, (), *both(DENY)),
    ("dynamodb:Query", ANY, (), *both(DENY)),
    ("dynamodb:Scan", ANY, (), *both(DENY)),
    ("sqs:ReceiveMessage", ANY, (), *both(DENY)),
    ("kinesis:GetRecords", ANY, (), *both(DENY)),
    ("ec2:GetPasswordData", ANY, (), *both(DENY)),
    ("ec2:DescribeVpnConnections", ANY, (), *both(DENY)),
    ("logs:Unmask", ANY, (), *both(DENY)),
    ("apigateway:GET", apigw("/apikeys"), (), *both(DENY)),
    ("apigateway:GET", apigw("/apikeys/key123"), (), *both(DENY)),
    ("apigateway:GET", apigw("/usageplans/plan123/keys"), (), *both(DENY)),
    ("apigateway:GET", apigw("/usageplans/plan123/keys/key123"), (), *both(DENY)),
    ("apigateway:GET", apigw("/usageplans/plan123"), (), *both(ALLOW)),
    ("dynamodb:PartiQLSelect", ANY, (), *both(DENY)),
    ("dynamodb:GetRecords", ANY, (), *both(DENY)),
    ("kinesis:SubscribeToShard", ANY, (), *both(DENY)),
    ("cloudwatch:GetSpaceCredentials", ANY, (), *both(DENY)),
    ("cloudwatch:GetSpaceCredentialsForOrganization", ANY, (), *both(DENY)),
    ("cloudwatch:GetSpaceDataAccess", ANY, (), *both(DENY)),
    ("cloudwatch:GetTelemetryQueryResults", ANY, (), *both(DENY)),
    ("xray:GetSamplingTargets", ANY, (), *both(DENY)),
    # Credentials and tokens other services hand out.
    *[(action, ANY, (), *both(DENY)) for action in [
        "ecr:GetAuthorizationToken", "ecr-public:GetAuthorizationToken", "codeartifact:GetAuthorizationToken",
        "connect:GetFederationToken", "cognito-idp:ListUserPoolClientSecrets", "cognito-idp:DescribeUserPoolClient",
        "cognito-idp:GetClientToken", "cognito-idp:GetTokensFromRefreshToken",
        "cognito-idp:GetUserAttributeVerificationCode", "cognito-identity:GetOpenIdToken",
        "cognito-identity:GetOpenIdTokenForDeveloperIdentity", "wafv2:GetDecryptedAPIKey", "wafv2:ListAPIKeys",
        "appsync:ListApiKeys", "storagegateway:DescribeChapCredentials",
        "chime:ListVoiceConnectorTerminationCredentials", "lightsail:GetBucketAccessKeys", "lightsail:GetKeyPair",
        "lightsail:GetKeyPairs", "lightsail:GetRelationalDatabaseMasterUserPassword",
        "lightsail:GetInstanceAccessDetails", "lightsail:DownloadDefaultKeyPair", "gamelift:GetComputeAuthToken",
        "gamelift:GetInstanceAccess", "gamelift:GetComputeAccess", "acm:GetAcmeExternalAccountBindingCredentials",
        "ssm:GetAccessToken", "license-manager:GetAccessToken", "bedrock:CallWithBearerToken",
        "bedrock-mantle:CallWithBearerToken", "bedrock-agentcore:GetApiKeyCredentialProvider",
        "bedrock-agentcore:GetOauth2CredentialProvider", "s3express:CreateSession",
        "secretsmanager:BatchGetSecretValue", "redshift:GetClusterCredentials",
        "redshift:GetClusterCredentialsWithIAM", "redshift-serverless:GetCredentials", "ivs:GetStreamKey",
        "ivs:BatchGetStreamKey", "lakeformation:GetDataAccess", "sns:GetSubscriptionAttributes",
        "directconnect:DescribeVirtualInterfaces", "ec2:GetVpnConnectionDeviceSampleConfiguration",
        "ec2:ExportClientVpnClientConfiguration",
    ]],
    # Query results and stored content, end users of the customer's product, and code.
    *[(action, ANY, (), *both(DENY)) for action in [
        "athena:GetQueryResults", "athena:GetQueryResultsStream", "cassandra:Select", "dax:GetItem",
        "dax:BatchGetItem", "dax:Query", "dax:Scan", "dynamodb:ReadDataForReplication", "qldb:GetBlock",
        "qldb:GetRevision", "sdb:Select", "sdb:GetAttributes", "timestream:Select",
        "redshift-data:GetStatementResult", "glacier:GetJobOutput", "mediastore:GetObject",
        "s3-object-lambda:GetObjectVersion", "s3vectors:GetVectors", "s3vectors:QueryVectors",
        "s3tables:GetTableData", "ebs:GetSnapshotBlock", "ebs:ListSnapshotBlocks", "es:ESHttpGet", "es:ESHttpPost",
        "aoss:APIAccessAll", "aoss:DashboardsAccessAll", "kinesisvideo:GetMedia", "kinesisvideo:GetClip",
        "healthlake:ReadResource", "healthlake:SearchWithGet", "healthlake:GetHistoryByResourceId",
        "medical-imaging:GetImageFrame", "medical-imaging:GetImageSetMetadata", "omics:GetReadSet",
        "iot:GetThingShadow", "iotsitewise:GetAssetPropertyValue", "iotsitewise:GetAssetPropertyValueHistory",
        "iotanalytics:GetDatasetContent", "iotanalytics:SampleChannelData", "sagemaker:GetRecord",
        "sagemaker:BatchGetRecord", "bedrock:GetAgentMemory", "bedrock:Retrieve", "bedrock:AgenticRetrieveStream",
        "bedrock:GetDocumentContent", "bedrock-agentcore:GetMemoryRecord", "bedrock-agentcore:RetrieveMemoryRecords",
        "bedrock-agentcore:ListMemoryRecords", "bedrock-agentcore:GetEvent", "bedrock-agentcore:ListEvents",
        "workdocs:GetDocument", "workdocs:GetDocumentVersion", "chime:GetChannelMessage", "chime:ListChannelMessages",
        "cognito-idp:AdminGetUser", "cognito-idp:AdminListDevices", "cognito-idp:ListUsers",
        "cognito-idp:ListUsersInGroup", "lambda:GetLayerVersion", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
        "codecommit:GitPull", "codecommit:GetFile", "codecommit:GetFolder", "codecommit:GetBlob",
        "codeartifact:ReadFromRepository", "codeartifact:GetPackageVersionAsset",
    ]],
    # Configuration values: readable by default, denied with AllowConfigurationValues=false.
    ("lambda:ListFunctions", ANY, (), *CONFIG),
    ("lambda:ListVersionsByFunction", ANY, (), *CONFIG),
    ("lambda:GetFunctionConfiguration", ANY, (), *CONFIG),
    ("ecs:DescribeTaskDefinition", ANY, (), *CONFIG),
    ("ecs:DescribeTasks", ANY, (), *CONFIG),
    ("ecs:DescribeExpressGatewayService", ANY, (), *CONFIG),
    ("ecs:DescribeDaemonTaskDefinition", ANY, (), *CONFIG),
    ("batch:DescribeJobDefinitions", ANY, (), *CONFIG),
    ("batch:DescribeJobs", ANY, (), *CONFIG),
    ("batch:DescribeServiceJob", ANY, (), *CONFIG),
    ("lambda:ListMicrovmImageVersions", ANY, (), *CONFIG),
    ("cloudformation:DescribeStackResource", ANY, (), *CONFIG),
    ("ec2:DescribeInstanceAttribute", ANY, (), *CONFIG),
    ("ec2:DescribeLaunchTemplateVersions", ANY, (), *CONFIG),
    ("ec2:DescribeSpotFleetRequests", ANY, (), *CONFIG),
    ("ec2:DescribeSpotInstanceRequests", ANY, (), *CONFIG),
    ("autoscaling:DescribeLaunchConfigurations", ANY, (), *CONFIG),
    ("cloudformation:DescribeStacks", ANY, (), *CONFIG),
    ("cloudformation:DescribeChangeSet", ANY, (), *CONFIG),
    ("cloudformation:DescribeStackEvents", ANY, (), *CONFIG),
    ("cloudformation:DescribeEvents", ANY, (), *CONFIG),
    ("cloudformation:ListExports", ANY, (), *CONFIG),
    ("cloudformation:DescribeStackResourceDrifts", ANY, (), *CONFIG),
    ("cloudformation:ListStackInstanceResourceDrifts", ANY, (), *CONFIG),
    ("cloudformation:DescribeStackSet", ANY, (), *CONFIG),
    ("cloudformation:DescribeStackInstance", ANY, (), *CONFIG),
    ("eks:DescribeAddon", ANY, (), *CONFIG),
    ("events:ListTargetsByRule", ANY, (), *CONFIG),
    ("firehose:DescribeDeliveryStream", ANY, (), *CONFIG),
    ("apigateway:GET", apigw("/restapis/abc123/stages"), (), *CONFIG),
    ("apigateway:GET", apigw("/restapis/abc123/stages/prod"), (), *CONFIG),
    ("apigateway:GET", apigw("/apis/abc123/stages"), (), *CONFIG),
    ("apigateway:GET", apigw("/apis/abc123/stages/prod"), (), *CONFIG),
    # Reviewed in read-only-access-review.tsv: more credentials and tokens other services hand out.
    *[(action, ANY, (), *both(DENY)) for action in [
        "amplify:GetApp", "amplify:GetBranch", "amplify:ListApps", "amplify:ListBranches",
        "athena:GetSessionEndpoint", "chime:GetAttendee", "chime:GetBot", "chime:ListAttendees", "chime:ListBots",
        "cloudhsm:DescribeClusters", "codepipeline:GetJobDetails", "codepipeline:GetThirdPartyJobDetails",
        "cognito-identity:GetCredentialsForIdentity", "cognito-idp:DescribeIdentityProvider",
        "cognito-idp:GetIdentityProviderByIdentifier", "directconnect:DescribeRouterConfiguration",
        "emr-serverless:GetDashboardForJobRun", "gamelift:GetPlayerConnectionDetails",
        "gameliftstreams:GetStreamUrl", "gameliftstreams:ListStreamUrls", "interconnect:GetConnection",
        "iotwireless:GetWirelessDevice", "ivs:BatchGetChannel", "ivs:GetChannel", "ivs:GetIngestConfiguration",
        "ivs:GetStreamSession", "mediapackage:DescribeChannel", "mediapackage:ListChannels",
        "mobiletargeting:GetBaiduChannel", "mobiletargeting:GetGcmChannel", "opsworks:DescribeApps",
        "s3:GetDataAccess", "snowball:GetJobManifest", "snowball:GetJobUnlockCode", "sns:GetEndpointAttributes",
        "sns:ListEndpointsByPlatformApplication", "workspaces-web:GetIdentityProvider",
    ]],
    # Reviewed in read-only-access-review.tsv: more stored content, workflow payloads, end users and code.
    *[(action, ANY, (), *both(DENY)) for action in [
        "aidevops:ListJournalRecords", "aidevops:ListPendingMessages", "bedrock:GetInvocationStep",
        "bedrock:ListFlowExecutionEvents", "clouddirectory:BatchRead", "clouddirectory:GetLinkAttributes",
        "clouddirectory:GetObjectAttributes", "clouddirectory:ListAttachedIndices",
        "clouddirectory:ListIncomingTypedLinks", "clouddirectory:ListIndex", "clouddirectory:ListObjectAttributes",
        "clouddirectory:ListOutgoingTypedLinks", "cloudfront-keyvaluestore:GetKey",
        "cloudfront-keyvaluestore:ListKeys", "cloudfront:GetConnectionFunction", "cloudfront:GetFunction",
        "codecommit:DescribeMergeConflicts", "codecommit:GetBlobDifferences", "cognito-sync:ListRecords",
        "connect:GetContactAttributes", "connect:ListDataTablePrimaryValues", "connect:ListDataTableValues",
        "connect:ListRealtimeContactAnalysisSegments", "connect:ListRealtimeContactAnalysisSegmentsV2",
        "dataexchange:GetAsset", "dataexchange:GetJob", "dataexchange:ListJobs", "dataexchange:ListRevisionAssets",
        "dynamodb:SearchVectors", "ec2:DescribeConversionTasks", "forecast:QueryForecast",
        "forecast:QueryWhatIfForecast", "frauddetector:GetEvent", "frauddetector:GetEventPredictionMetadata",
        "glue:GetStatement", "glue:ListStatements", "greengrass:GetComponentVersionArtifact",
        "importexport:GetStatus", "inspector2:BatchGetCodeSnippet", "iot:GetRetainedMessage", "iot:GetTopicRule",
        "iotsitewise:GetCaptureData", "iotsitewise:GetQueryResults", "kendra:GetQuerySuggestions", "kendra:Query",
        "kinesisanalytics:DiscoverInputSchema", "lambda:GetDurableExecution", "lambda:GetDurableExecutionHistory",
        "lambda:GetDurableExecutionState", "lex:GetSession", "lex:GetUtterancesView", "lookoutmetrics:GetSampleData",
        "mediapackagev2:GetObject", "mobiletargeting:GetEndpoint", "mobiletargeting:GetUserEndpoints",
        "omics:GetReference", "personalize:GetPersonalizedRanking", "personalize:GetRecommendations",
        "rekognition:GetTextDetection", "s3-outposts:GetObject", "s3-outposts:GetObjectVersion",
        "s3-outposts:GetObjectVersionForReplication", "securityagent:GetArtifact", "ses:GetArchiveMessage",
        "ses:GetArchiveMessageContent", "ses:GetArchiveSearchResults", "ses:GetContact", "ses:ListContacts",
        "ses:ListMembersOfAddressList", "ses:ListSuppressedDestinations", "sms-voice:DescribeOptedOutNumbers",
        "sns:ListPhoneNumbersOptedOut", "states:DescribeExecution", "states:GetExecutionHistory",
        "support:DescribeAttachment", "swf:GetWorkflowExecutionHistory", "tax:GetTaxRegistrationDocument",
        "tnb:GetSolFunctionPackageContent", "transcribe:GetCallAnalyticsJob", "transcribe:GetTranscriptionJob",
        "translate:GetParallelData", "translate:GetTerminology", "workdocs:DescribeComments",
        "workdocs:DescribeDocumentVersions",
    ]],
    # With true: a sample read in each service family only ReadOnlyAccess grants, and the reads it lacks that
    # BetterStackAiSreBroadAccess adds. With false: none of them.
    *[(action, ANY, (), *BROAD) for action in [
        "codedeploy:GetDeployment", "codebuild:BatchGetProjects", "config:DescribeConfigRules",
        "ecr:DescribeRepositories", "acm:DescribeCertificate", "servicequotas:GetServiceQuota",
        "cloudfront:GetDistribution", "wafv2:GetWebACL", "es:DescribeDomain", "kafka:DescribeCluster",
        "mq:DescribeBroker", "redshift:DescribeClusters", "iam:ListRoles", "synthetics:DescribeCanaries",
        "organizations:DescribeOrganization",
        "cloudtrail:StartQuery", "cloudtrail:CancelQuery", "pi:GetPerformanceAnalysisReport",
        "pi:ListPerformanceAnalysisReports", "eks-mcp:InvokeMcp", "eks-mcp:CallReadOnlyTool", "ecs-mcp:UseMcp",
        "ecs-mcp:InvokeReadOnlyTools", "application-signals-mcp:InvokeMcp",
        "application-signals-mcp:CallReadOnlyTool",
    ]],
    # In BetterStackAiSreBroadAccess too, but the curated logs:List* already grants it in both settings.
    ("logs:ListLogGroups", ANY, (), *both(ALLOW)),
    # Nothing that changes anything.
    ("ec2:RunInstances", ANY, (), *both(NONE)),
    ("s3:PutObject", ANY, (), *both(NONE)),
    ("iam:PassRole", ANY, (), *both(NONE)),
    ("lambda:UpdateFunctionConfiguration", ANY, (), *both(NONE)),
    ("cloudformation:UpdateStack", ANY, (), *both(NONE)),
]


def parse_role_arn(role_arn):
    parts = role_arn.split(":")
    if len(parts) != 6 or parts[0] != "arn" or parts[2] != "iam" or not parts[5].startswith("role/"):
        raise ValueError(f"not an IAM role ARN: {role_arn}")
    return parts[4]


def fill(value, role_arn, region):
    suffix = role_arn.rsplit("-", 1)[-1]
    return role_arn if value == SELF else value.format(account=parse_role_arn(role_arn), region=region, suffix=suffix)


def expectations(role_arn, allow_configuration_values, region):
    """{(resource, context): {action: decision}}, the calls the simulator gets."""
    grouped = defaultdict(dict)
    for action, resource, context, when_true, when_false in CHECKS:
        key = (fill(resource, role_arn, region), tuple((k, fill(v, role_arn, region)) for k, v in context))
        grouped[key][action] = when_true if allow_configuration_values else when_false
    return grouped


def simulate(iam, role_arn, actions, resource, context):
    """{action: decision} for ACTIONS on RESOURCE, as the simulator decides them."""
    params = {"PolicySourceArn": role_arn, "ActionNames": actions, "ResourceArns": [resource]}
    if context:
        params["ContextEntries"] = [
            {"ContextKeyName": key, "ContextKeyValues": [value], "ContextKeyType": "string"} for key, value in context
        ]
    decisions = {}
    for page in iam.get_paginator("simulate_principal_policy").paginate(**params):
        for result in page["EvaluationResults"]:
            decisions[result["EvalActionName"]] = result["EvalDecision"]
    return decisions


def run(iam, role_arn, allow_configuration_values, region, out=sys.stdout):
    """Prints a line per check and returns the number of checks whose decision differs."""
    failures = 0
    for (resource, context), expected in expectations(role_arn, allow_configuration_values, region).items():
        actions = sorted(expected)
        decisions = {}
        for i in range(0, len(actions), 50):
            decisions.update(simulate(iam, role_arn, actions[i:i + 50], resource, context))
        where = resource + "".join(f" with {k}={v}" for k, v in context)
        for action in actions:
            got = decisions.get(action, "not evaluated")
            ok = got == expected[action]
            failures += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {action} on {where}: {got}" + ("" if ok else f", expected {expected[action]}"), file=out)
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--role-arn", required=True)
    parser.add_argument("--allow-configuration-values", required=True, choices=["true", "false"])
    parser.add_argument("--region", default="us-east-1", help="the region the resource ARNs name (default us-east-1)")
    args = parser.parse_args(argv)
    import boto3

    allow = args.allow_configuration_values == "true"
    failures = run(boto3.client("iam"), args.role_arn, allow, args.region)
    print(f"{len(CHECKS) - failures} of {len(CHECKS)} decisions as expected with AllowConfigurationValues={args.allow_configuration_values}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
