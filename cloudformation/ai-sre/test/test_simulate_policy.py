"""simulate_policy.py, offline: a stubbed SimulatePrincipalPolicy that evaluates the template's own policies.

The stub decides each request from better-stack-ai-sre.yaml's policies, plus AWS's ReadOnlyAccess (as
fixtures/read-only-access-policy.json holds it) where the template attaches it: explicit deny, then allow, else
implicit deny; Action, Resource and NotResource wildcards; StringEquals, StringLike and StringNotLike on the
request's context. So the script's expectations are checked against the template itself in both
AllowConfigurationValues settings, and against the lists of the allow and deny tables below.
"""

import io
import json
import os
import unittest

import simulate_policy as sim
import template_policies
from service_reference import glob_matches

READ_ONLY_ACCESS_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "read-only-access-policy.json")

ACCOUNT = "123456789012"
EXTERNAL_ID = "bs-ai-sre-1a2b3c4d-0000-4000-8000-00000000abcd"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/better-stack-ai-sre-role-1a2b3c4d"

# The explicit denies, action by action.
DENIED_IN_EVERY_SETTING = [
    "s3:GetObject", "secretsmanager:GetSecretValue", "ssm:GetParameter", "lambda:GetFunction", "lambda:InvokeFunction",
    "dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query", "dynamodb:Scan", "sqs:ReceiveMessage",
    "kinesis:GetRecords", "ec2:GetPasswordData", "ec2:DescribeVpnConnections", "logs:Unmask",
    "dynamodb:PartiQLSelect", "dynamodb:GetRecords", "kinesis:SubscribeToShard",
    "cloudwatch:GetSpaceCredentials", "cloudwatch:GetSpaceCredentialsForOrganization", "cloudwatch:GetSpaceDataAccess",
    "cloudwatch:GetTelemetryQueryResults", "xray:GetSamplingTargets",
    # Credentials and tokens other services hand out.
    "ecr:GetAuthorizationToken", "codeartifact:GetAuthorizationToken", "cognito-idp:ListUserPoolClientSecrets",
    "cognito-idp:GetClientToken", "wafv2:GetDecryptedAPIKey", "appsync:ListApiKeys", "s3express:CreateSession",
    "secretsmanager:BatchGetSecretValue", "redshift:GetClusterCredentials", "directconnect:DescribeVirtualInterfaces",
    # Stored content, end users and code.
    "athena:GetQueryResults", "cassandra:Select", "dax:Query", "qldb:GetBlock", "timestream:Select",
    "glacier:GetJobOutput", "es:ESHttpGet", "kinesisvideo:GetMedia", "healthlake:ReadResource",
    "cognito-idp:AdminGetUser", "cognito-idp:ListUsers", "lambda:GetLayerVersion", "ecr:BatchGetImage",
    "codecommit:GetFile", "codeartifact:GetPackageVersionAsset",
]
DENIED_API_GATEWAY_PATHS = ["/apikeys", "/apikeys/key123", "/usageplans/plan123/keys", "/usageplans/plan123/keys/key123"]
DENIED_WITHOUT_CONFIGURATION_VALUES = [
    "lambda:ListFunctions", "lambda:ListVersionsByFunction", "lambda:GetFunctionConfiguration",
    "ecs:DescribeTaskDefinition", "ecs:DescribeTasks", "batch:DescribeJobDefinitions", "batch:DescribeJobs",
    "ec2:DescribeInstanceAttribute", "ec2:DescribeLaunchTemplateVersions", "ec2:DescribeSpotFleetRequests",
    "ec2:DescribeSpotInstanceRequests", "cloudformation:DescribeEvents",
    "autoscaling:DescribeLaunchConfigurations", "cloudformation:DescribeStacks", "cloudformation:DescribeChangeSet",
    "cloudformation:DescribeStackEvents", "cloudformation:ListExports", "events:ListTargetsByRule",
    "firehose:DescribeDeliveryStream", "cloudformation:DescribeStackResourceDrifts",
    "cloudformation:ListStackInstanceResourceDrifts", "cloudformation:DescribeStackSet",
    "cloudformation:DescribeStackInstance", "eks:DescribeAddon",
    "ecs:DescribeExpressGatewayService", "ecs:DescribeDaemonTaskDefinition", "batch:DescribeServiceJob",
    "lambda:ListMicrovmImageVersions", "cloudformation:DescribeStackResource", "cloudformation:ListResources",
]
STAGE_PATHS = ["/restapis/abc123/stages", "/restapis/abc123/stages/prod", "/apis/abc123/stages", "/apis/abc123/stages/prod"]
# Readable in both settings: the investigation reads, the self-read and the MCP token.
ALLOWED_IN_EVERY_SETTING = [
    "cloudwatch:DescribeAlarms", "logs:StartQuery", "logs:StopQuery", "cloudtrail:LookupEvents",
    "xray:GetTraceSummaries", "xray:BatchGetTraces", "cloudformation:DescribeStackResources", "cloudformation:ListStacks",
    "application-autoscaling:DescribeScalableTargets", "iam:ListAccountAliases", "account:ListRegions",
]
NEVER_ALLOWED = ["signin:AuthorizeOAuth2Access"]
# Only ReadOnlyAccess grants these: readable with true, not with false.
READ_ONLY_ACCESS_ONLY = ["codedeploy:GetDeployment", "iam:ListRoles", "logs:TestMetricFilter", "organizations:DescribeOrganization"]
# BetterStackAiSreBroadAccess, with true only.
BROAD_ACCESS = [
    "cloudtrail:StartQuery", "cloudtrail:CancelQuery", "pi:GetPerformanceAnalysisReport",
    "pi:ListPerformanceAnalysisReports", "eks-mcp:InvokeMcp", "eks-mcp:CallReadOnlyTool", "ecs-mcp:UseMcp",
    "ecs-mcp:InvokeReadOnlyTools", "application-signals-mcp:InvokeMcp", "application-signals-mcp:CallReadOnlyTool",
]


def statements(allow_configuration_values):
    """The role's inline statements and, where the template attaches it, ReadOnlyAccess's."""
    found = template_policies.role_statements(allow_configuration_values)
    for arn in template_policies.managed_policy_arns(allow_configuration_values):
        assert arn == template_policies.READ_ONLY_ACCESS_ARN, f"the stub knows no managed policy {arn}"
        with open(READ_ONLY_ACCESS_FIXTURE, encoding="utf-8") as f:
            found += json.load(f)["PolicyDocument"]["Statement"]
    return found


def glob(pattern, value, ignore_case=False):
    return glob_matches(pattern, value, ignore_case)


def as_list(value):
    return [value] if isinstance(value, str) else value


def conditions_hold(statement, context):
    for operator, keys in statement.get("Condition", {}).items():
        for key, patterns in keys.items():
            value = context.get(key)
            if operator == "StringEquals":
                if value not in as_list(patterns):
                    return False
                continue
            hit = value is not None and any(glob(p, value) for p in as_list(patterns))
            if operator == "StringLike" and not hit:
                return False
            if operator == "StringNotLike" and hit:
                return False
            if operator not in ("StringLike", "StringNotLike"):
                raise AssertionError(f"the stub does not evaluate {operator}")
    return True


def decide(policy_statements, action, resource, context):
    def applies(s):
        if "NotResource" in s:
            resource_ok = not any(glob(p, resource) for p in as_list(s["NotResource"]))
        else:
            resource_ok = any(glob(p, resource) for p in as_list(s["Resource"]))
        return (
            any(glob(p, action, ignore_case=True) for p in as_list(s["Action"]))
            and resource_ok
            and conditions_hold(s, context)
        )

    matching = [s for s in policy_statements if applies(s)]
    if any(s["Effect"] == "Deny" for s in matching):
        return sim.DENY
    return sim.ALLOW if matching else sim.NONE


class StubIam:
    """Answers SimulatePrincipalPolicy from the template's policies, two results per page."""

    def __init__(self, allow_configuration_values, override=None):
        self.statements = statements(allow_configuration_values)
        self.override = override or {}
        self.calls = []

    def get_paginator(self, name):
        assert name == "simulate_principal_policy"
        return self

    def paginate(self, PolicySourceArn, ActionNames, ResourceArns, ContextEntries=()):
        assert PolicySourceArn == ROLE_ARN
        self.calls.append({"actions": ActionNames, "resources": ResourceArns, "context": list(ContextEntries)})
        context = {e["ContextKeyName"]: e["ContextKeyValues"][0] for e in ContextEntries}
        results = [
            {
                "EvalActionName": action,
                "EvalResourceName": ResourceArns[0],
                "EvalDecision": self.override.get((action, ResourceArns[0]), decide(self.statements, action, ResourceArns[0], context)),
            }
            for action in ActionNames
        ]
        for i in range(0, len(results), 2):
            yield {"EvaluationResults": results[i:i + 2]}


def expected(action, resource=sim.ANY):
    return [(t, f) for a, r, c, t, f in sim.CHECKS if a == action and r == resource and not c]


class SimulatePolicyTest(unittest.TestCase):
    def run_script(self, iam, allow):
        out = io.StringIO()
        return sim.run(iam, ROLE_ARN, allow, "us-east-1", out=out), out.getvalue()

    def test_every_expectation_matches_the_template_with_configuration_values(self):
        failures, out = self.run_script(StubIam(True), True)
        self.assertEqual(failures, 0, out)

    def test_every_expectation_matches_the_template_without_configuration_values(self):
        failures, out = self.run_script(StubIam(False), False)
        self.assertEqual(failures, 0, out)

    def test_a_decision_that_differs_fails_and_is_named(self):
        iam = StubIam(True, override={("cloudwatch:DescribeAlarms", "*"): sim.NONE})
        failures, out = self.run_script(iam, True)
        self.assertEqual(failures, 1)
        self.assertIn("FAIL cloudwatch:DescribeAlarms on *: implicitDeny, expected allowed", out)

    def test_a_role_deployed_with_true_fails_every_check_that_differs_with_false(self):
        # The configuration deny and the broad reads are what the setting switches; checked as if it were false, a
        # role deployed with true fails exactly the checks whose two decisions differ.
        failures, out = self.run_script(StubIam(True), False)
        self.assertEqual(failures, sum(1 for _, _, _, t, f in sim.CHECKS if t != f), out)
        self.assertIn("FAIL codedeploy:GetDeployment on *: allowed, expected implicitDeny", out)

    def test_the_restricted_role_has_no_read_only_access(self):
        self.assertEqual(template_policies.managed_policy_arns(False), [])
        self.assertEqual(template_policies.managed_policy_arns(True), [template_policies.READ_ONLY_ACCESS_ARN])
        for action in READ_ONLY_ACCESS_ONLY + BROAD_ACCESS:
            self.assertEqual(expected(action), [sim.BROAD], action)

    def test_s3_objects_are_readable_under_aws_logs_only_and_only_with_true(self):
        self.assertEqual(expected("s3:GetObject", "arn:aws:s3:::b/AWSLogs/1/x"), [sim.BROAD])
        self.assertEqual(expected("s3:GetObject", "arn:aws:s3:::b/p/AWSLogs/1/x"), [sim.BROAD])
        for resource in ("arn:aws:s3:::b/data/x", sim.ANY, sim.ACCESS_POINT_LOG_OBJECT, sim.OBJECT_LAMBDA_LOG_OBJECT):
            self.assertEqual(expected("s3:GetObject", resource), [(sim.DENY, sim.DENY)], resource)
        self.assertEqual(expected("s3:ListBucket", "arn:aws:s3:::b"), [sim.BROAD])

    def test_kms_decrypt_through_each_named_service_with_true_only(self):
        for service in sim.KMS_BROAD_SERVICES:
            rows = [(t, f) for a, _, c, t, f in sim.CHECKS if a == "kms:Decrypt" and c == sim.via(service)]
            self.assertEqual(rows, [sim.BROAD], service)
        for service in ("secretsmanager", "cloudtrail"):
            rows = [(t, f) for a, _, c, t, f in sim.CHECKS if a == "kms:Decrypt" and c == sim.via(service)]
            self.assertEqual(rows, [(sim.DENY, sim.DENY)], service)

    def test_the_policies_fit_iam_quotas_in_both_settings(self):
        for allow in (True, False):
            self.assertLess(template_policies.inline_size(allow), 10240, f"inline, AllowConfigurationValues={allow}")
            sizes = template_policies.stack_policy_sizes(allow)
            self.assertEqual(len(sizes), 2, sizes)
            for name, size in sizes.items():
                self.assertLess(size, 6144, f"{name}, AllowConfigurationValues={allow}")
            attached = len(sizes) + len(template_policies.managed_policy_arns(allow))
            self.assertLessEqual(attached, 20, "a role takes 20 managed policies by default")

    def test_kms_checks_send_the_via_service_context(self):
        iam = StubIam(True)
        self.run_script(iam, True)
        contexts = [c["context"] for c in iam.calls if c["context"]]
        self.assertIn([{"ContextKeyName": "kms:ViaService", "ContextKeyValues": ["lambda.us-east-1.amazonaws.com"], "ContextKeyType": "string"}], contexts)

    def test_resources_are_filled_in_for_the_role(self):
        iam = StubIam(True)
        self.run_script(iam, True)
        resources = {r for c in iam.calls for r in c["resources"]}
        self.assertIn(ROLE_ARN, resources)
        self.assertIn(f"arn:aws:iam::{ACCOUNT}:role/some-other-role", resources)
        self.assertIn(f"arn:aws:signin:us-east-1:{ACCOUNT}:service-principal/aws-mcp.amazonaws.com", resources)
        self.assertIn("arn:aws:apigateway:us-east-1::/apis/abc123/stages", resources)

    def test_no_check_is_listed_twice(self):
        keys = [(a, r, c) for a, r, c, _, _ in sim.CHECKS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_the_deny_lists_are_checked_entry_by_entry(self):
        for action in DENIED_IN_EVERY_SETTING:
            self.assertEqual(expected(action), [(sim.DENY, sim.DENY)], action)
        for path in DENIED_API_GATEWAY_PATHS:
            self.assertEqual(expected("apigateway:GET", sim.apigw(path)), [(sim.DENY, sim.DENY)], path)
        for action in DENIED_WITHOUT_CONFIGURATION_VALUES:
            self.assertEqual(expected(action), [(sim.ALLOW, sim.DENY)], action)
        for path in STAGE_PATHS:
            self.assertEqual(expected("apigateway:GET", sim.apigw(path)), [(sim.ALLOW, sim.DENY)], path)
        self.assertEqual(
            sorted(a for a, _, _, t, f in sim.CHECKS if (t, f) == sim.CONFIG and not a.startswith("apigateway:")),
            sorted(DENIED_WITHOUT_CONFIGURATION_VALUES),
        )

    def test_the_allow_list_is_checked_entry_by_entry(self):
        for action in ALLOWED_IN_EVERY_SETTING:
            self.assertEqual(expected(action), [(sim.ALLOW, sim.ALLOW)], action)
        for action in NEVER_ALLOWED:
            self.assertIn(sim.NONE, [t for a, _, _, t, _ in sim.CHECKS if a == action], action)
        self.assertEqual(expected("signin:CreateOAuth2Token", sim.MCP), [(sim.ALLOW, sim.ALLOW)])
        for action in ("iam:GetRole", "iam:ListRolePolicies", "iam:GetRolePolicy", "iam:ListAttachedRolePolicies"):
            self.assertEqual(expected(action, sim.SELF), [(sim.ALLOW, sim.ALLOW)], action)
            self.assertEqual(expected(action, sim.OTHER_ROLE), [sim.BROAD], action)

    def test_every_deny_in_the_template_has_a_check(self):
        # A deny added to the template without a CHECKS entry would never reach the policy simulator.
        checks = [(a, sim.fill(r, ROLE_ARN, "us-east-1"), t, f) for a, r, _, t, f in sim.CHECKS]
        missing = []
        for statement in statements(allow_configuration_values=False):
            if statement["Effect"] != "Deny":
                continue
            for action in as_list(statement["Action"]):
                if "NotResource" in statement:
                    # A check on a resource outside the NotResource patterns, which the deny then covers.
                    excluded = as_list(statement["NotResource"])
                    covered = any(
                        glob(action, a, ignore_case=True) and not any(glob(p, r) for p in excluded) and sim.DENY in (t, f)
                        for a, r, t, f in checks
                    )
                    if not covered:
                        missing.append(f"{statement['Sid']}: {action} outside {excluded}")
                    continue
                for resource in as_list(statement["Resource"]):
                    if not any(
                        glob(action, a, ignore_case=True) and glob(resource, r) and sim.DENY in (t, f)
                        for a, r, t, f in checks
                    ):
                        missing.append(f"{statement['Sid']}: {action} on {resource}")
        self.assertEqual(missing, [], "denies without a CHECKS entry in simulate_policy.py expecting explicitDeny")

    def test_a_role_arn_is_required(self):
        with self.assertRaises(ValueError):
            sim.parse_role_arn("arn:aws:iam::123456789012:user/someone")


if __name__ == "__main__":
    unittest.main()
