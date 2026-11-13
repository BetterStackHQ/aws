"""The AI SRE role's policies as CloudFormation would create them, for either setting of AllowConfigurationValues."""

import json
import re

import cfn_yaml
import check_contract

SAMPLE_REFS = {
    "AWS::AccountId": "123456789012",
    "AWS::Partition": "aws",
    "ExternalId": "bs-ai-sre-1a2b3c4d-0000-4000-8000-00000000abcd",
    "BetterStackEnvironment": "production",
}
READ_ONLY_ACCESS_ARN = "arn:aws:iam::aws:policy/ReadOnlyAccess"
NO_VALUE = object()


def resolve(value, refs, conditions):
    """VALUE with the intrinsic functions the role uses resolved, and AWS::NoValue dropped."""
    if isinstance(value, list):
        return [r for r in (resolve(v, refs, conditions) for v in value) if r is not NO_VALUE]
    if isinstance(value, dict):
        if len(value) == 1:
            (fn, arg), = value.items()
            if fn == "Ref":
                return NO_VALUE if arg == "AWS::NoValue" else refs[arg]
            if fn == "Fn::If":
                return resolve(arg[1] if conditions[arg[0]] else arg[2], refs, conditions)
            if fn == "Fn::Sub":
                return re.sub(r"\$\{([^}]+)\}", lambda m: refs[m.group(1)], arg)
            if fn in ("Fn::Join", "Fn::Select", "Fn::Split"):
                return check_contract._evaluate(value, refs)
        resolved = {k: resolve(v, refs, conditions) for k, v in value.items()}
        return {k: v for k, v in resolved.items() if v is not NO_VALUE}
    return value


def role(allow_configuration_values, template=check_contract.DEFAULT_TEMPLATE, refs=None):
    """The role's resolved properties."""
    props = cfn_yaml.load(template)["Resources"]["AiSreRole"]["Properties"]
    conditions = {
        "DenyConfigurationValues": not allow_configuration_values,
        "HasPermissionsBoundary": False,
        "HasBetterStackTeam": False,
    }
    return resolve(props, refs or SAMPLE_REFS, conditions)


def inline_policies(allow_configuration_values, template=check_contract.DEFAULT_TEMPLATE):
    return role(allow_configuration_values, template).get("Policies", [])


def inline_statements(allow_configuration_values, template=check_contract.DEFAULT_TEMPLATE):
    return [s for p in inline_policies(allow_configuration_values, template) for s in p["PolicyDocument"]["Statement"]]


def managed_policy_arns(allow_configuration_values, template=check_contract.DEFAULT_TEMPLATE):
    return role(allow_configuration_values, template).get("ManagedPolicyArns", [])


def inline_size(allow_configuration_values, template=check_contract.DEFAULT_TEMPLATE):
    """The role's inline policy size as IAM counts it against its 10,240-character quota: white space not counted."""
    total = 0
    for policy in inline_policies(allow_configuration_values, template):
        text = json.dumps(policy["PolicyDocument"], separators=(",", ":"))
        total += len(re.sub(r"\s", "", text))
    return total
