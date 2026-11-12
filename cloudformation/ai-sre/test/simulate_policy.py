#!/usr/bin/env python3
"""Asserts what a deployed Better Stack AI SRE role may and may not do, with IAM's policy simulator.

Every read the role must allow, every explicit deny, and every read AllowConfigurationValues=false must deny is
listed in CHECKS below with the decision expected under each setting of AllowConfigurationValues. The script asks
iam:SimulatePrincipalPolicy for each one on the given role and fails on any decision that differs. The simulator
evaluates the role's own policies and its permissions boundary, not service control policies or resource policies.

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
MCP = "arn:aws:signin:{region}:{account}:service-principal/aws-mcp.amazonaws.com"


def apigw(path):
    return "arn:aws:apigateway:{region}::" + path


def via(service):
    return (("kms:ViaService", service + ".{region}.amazonaws.com"),)


def both(decision):
    return decision, decision


CONFIG = (ALLOW, DENY)  # readable by default, denied with AllowConfigurationValues=false

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
    # The role's own description and policies, and no other role's.
    ("iam:GetRole", SELF, (), *both(ALLOW)),
    ("iam:ListRolePolicies", SELF, (), *both(ALLOW)),
    ("iam:GetRolePolicy", SELF, (), *both(ALLOW)),
    ("iam:ListAttachedRolePolicies", SELF, (), *both(ALLOW)),
    ("iam:GetRole", OTHER_ROLE, (), *both(NONE)),
    ("iam:ListRolePolicies", OTHER_ROLE, (), *both(NONE)),
    ("iam:GetRolePolicy", OTHER_ROLE, (), *both(NONE)),
    ("iam:ListAttachedRolePolicies", OTHER_ROLE, (), *both(NONE)),
    # Log groups under a customer managed key, through CloudWatch Logs only.
    ("kms:Decrypt", ANY, via("logs"), *both(ALLOW)),
    ("kms:DescribeKey", ANY, via("logs"), *both(ALLOW)),
    # Lambda decrypts environments as the caller: not denied, so default-key environments read, and not allowed, so
    # environments under a customer managed key stay unreadable.
    ("kms:Decrypt", ANY, via("lambda"), *both(NONE)),
    ("kms:Decrypt", ANY, via("s3"), *both(DENY)),
    ("kms:Decrypt", ANY, via("secretsmanager"), *both(DENY)),
    ("kms:Decrypt", ANY, (), *both(DENY)),
    ("kms:DescribeKey", ANY, (), *both(NONE)),
    # The MCP token: CreateOAuth2Token alone, for aws-mcp alone.
    ("signin:CreateOAuth2Token", MCP, (), *both(ALLOW)),
    ("signin:AuthorizeOAuth2Access", MCP, (), *both(NONE)),
    # Dropped from ingestion's read access.
    ("logs:TestMetricFilter", ANY, (), *both(NONE)),
    # Secrets and customer data, denied explicitly in every setting.
    ("lambda:GetFunction", ANY, (), *both(DENY)),
    ("lambda:InvokeFunction", ANY, (), *both(DENY)),
    ("s3:GetObject", ANY, (), *both(DENY)),
    ("s3:GetObjectVersion", ANY, (), *both(DENY)),
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
    return role_arn if value == SELF else value.format(account=parse_role_arn(role_arn), region=region)


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
