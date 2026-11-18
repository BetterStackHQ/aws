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


def _stack_policy_arns(resources):
    """{logical id: ARN} of the stack's managed policies."""
    return {
        name: f"arn:{SAMPLE_REFS['AWS::Partition']}:iam::{SAMPLE_REFS['AWS::AccountId']}:policy/"
        + check_contract._evaluate(r["Properties"]["ManagedPolicyName"], SAMPLE_REFS)
        for name, r in resources.items()
        if r.get("Type") == "AWS::IAM::ManagedPolicy"
    }


def _resolved(allow_configuration_values, template, logical_id):
    resources = cfn_yaml.load(template)["Resources"]
    refs = dict(SAMPLE_REFS)
    # A !Ref to one of the stack's managed policies is its ARN, as CloudFormation returns it.
    refs.update(_stack_policy_arns(resources))
    conditions = {
        "DenyConfigurationValues": not allow_configuration_values,
        "HasPermissionsBoundary": False,
        "HasBetterStackTeam": False,
    }
    return resolve(resources[logical_id]["Properties"], refs, conditions)


def role(allow_configuration_values, template=check_contract.DEFAULT_TEMPLATE):
    """The role's resolved properties."""
    return _resolved(allow_configuration_values, template, "AiSreRole")


def inline_policies(allow_configuration_values, template=check_contract.DEFAULT_TEMPLATE):
    return role(allow_configuration_values, template).get("Policies", [])


def inline_statements(allow_configuration_values, template=check_contract.DEFAULT_TEMPLATE):
    return [s for p in inline_policies(allow_configuration_values, template) for s in p["PolicyDocument"]["Statement"]]


def managed_policy_arns(allow_configuration_values, template=check_contract.DEFAULT_TEMPLATE):
    """The AWS managed policies attached to the role; the stack's own ones are in stack_policies()."""
    stack = set(_stack_policy_arns(cfn_yaml.load(template)["Resources"]).values())
    return [a for a in role(allow_configuration_values, template).get("ManagedPolicyArns", []) if a not in stack]


def stack_policies(allow_configuration_values, template=check_contract.DEFAULT_TEMPLATE):
    """{logical id: resolved properties} of the stack's managed policies attached to the role."""
    by_arn = {arn: i for i, arn in _stack_policy_arns(cfn_yaml.load(template)["Resources"]).items()}
    arns = role(allow_configuration_values, template).get("ManagedPolicyArns", [])
    return {by_arn[a]: _resolved(allow_configuration_values, template, by_arn[a]) for a in arns if a in by_arn}


def role_statements(allow_configuration_values, template=check_contract.DEFAULT_TEMPLATE):
    """Every statement of the role's inline policies and of the stack's managed policies attached to it."""
    stack = [s for p in stack_policies(allow_configuration_values, template).values() for s in p["PolicyDocument"]["Statement"]]
    return inline_statements(allow_configuration_values, template) + stack


def stack_policy_sizes(allow_configuration_values, template=check_contract.DEFAULT_TEMPLATE):
    """{managed policy name: size}, counted as IAM counts against its 6,144-character quota, white space not counted."""
    return {
        p["ManagedPolicyName"]: len(re.sub(r"\s", "", json.dumps(p["PolicyDocument"], separators=(",", ":"))))
        for p in stack_policies(allow_configuration_values, template).values()
    }


def inline_size(allow_configuration_values, template=check_contract.DEFAULT_TEMPLATE):
    """The role's inline policy size as IAM counts it against its 10,240-character quota: white space not counted."""
    total = 0
    for policy in inline_policies(allow_configuration_values, template):
        text = json.dumps(policy["PolicyDocument"], separators=(",", ":"))
        total += len(re.sub(r"\s", "", text))
    return total
