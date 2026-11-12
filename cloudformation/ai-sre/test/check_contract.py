#!/usr/bin/env python3
"""Checks better-stack-ai-sre.yaml against its contract, recorded in contract.json.

Fails when a parameter is removed or renamed, a parameter has no default (only ExternalId may lack one), a parameter
is not recorded in the contract yet, a recorded parameter's type, default, pattern or allowed values change, the
RoleName expression or the ExternalId pattern change, the version tag key changes, the trust is no longer the one
recorded statement (sts:AssumeRole alone, with the external ID and the callers' path conditions), the role's logical ID
or the IntegrationRoleArn output go, or the Release mapping loses the line the Release workflow stamps.

A change to any of these is a change of contract: make it on purpose, in contract.json, in the same pull request.

Usage: check_contract.py [TEMPLATE] [--contract CONTRACT]
"""

import argparse
import json
import os
import re
import sys

import cfn_yaml

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE = os.path.join(HERE, "..", "better-stack-ai-sre.yaml")
DEFAULT_CONTRACT = os.path.join(HERE, "..", "contract.json")

# The one parameter every setup link passes and that has no sensible default.
REQUIRED_PARAMETERS = {"ExternalId"}
RECORDED_PARAMETER_FIELDS = ("Type", "Default", "AllowedPattern", "AllowedValues")
# The line release.yml rewrites with sed; the release fails without it.
STAMP_LINE = re.compile(r"^      Version: 0\.0\.0-dev$", re.MULTILINE)
SAMPLE_EXTERNAL_ID = "bs-ai-sre-1a2b3c4d-0000-4000-8000-00000000abcd"
SAMPLE_ROLE_NAME = "better-stack-ai-sre-role-1a2b3c4d"


def _actions(statement):
    action = statement.get("Action", [])
    return {action} if isinstance(action, str) else set(action)


def _conditions(statement):
    """{(operator, key): value} of a statement's conditions."""
    return {(op, key): value for op, keys in statement.get("Condition", {}).items() for key, value in keys.items()}


def _evaluate(expr, refs):
    """Evaluates the few intrinsic functions a role name can use, for a sample parameter value."""
    if isinstance(expr, str):
        return expr
    if isinstance(expr, dict) and len(expr) == 1:
        (fn, arg), = expr.items()
        if fn == "Ref":
            return refs[arg]
        if fn == "Fn::Join":
            return arg[0].join(_evaluate(part, refs) for part in arg[1])
        if fn == "Fn::Split":
            return _evaluate(arg[1], refs).split(arg[0])
        if fn == "Fn::Select":
            return _evaluate(arg[1], refs)[int(arg[0])]
    raise ValueError(f"cannot evaluate {json.dumps(expr)}")


def check_parameters(template, contract):
    errors = []
    parameters = template.get("Parameters", {})
    recorded = contract["parameters"]
    for name, fields in recorded.items():
        if name not in parameters:
            errors.append(f"parameter {name} was removed or renamed: setup links already sent pass it")
            continue
        for field in RECORDED_PARAMETER_FIELDS:
            if parameters[name].get(field) != fields.get(field):
                errors.append(
                    f"parameter {name}: {field} is {json.dumps(parameters[name].get(field))}, "
                    f"the contract records {json.dumps(fields.get(field))}"
                )
    for name, fields in parameters.items():
        if name not in REQUIRED_PARAMETERS and "Default" not in fields:
            errors.append(f"parameter {name} has no Default: every setup link already sent would fail to create")
        if name not in recorded:
            errors.append(f"parameter {name} is not recorded in contract.json: add it there, with its fields")
    return errors


def check_role(template, contract):
    errors = []
    resources = template.get("Resources", {})
    role = resources.get(contract["role_logical_id"])
    if not role or role.get("Type") != "AWS::IAM::Role":
        return [f"resource {contract['role_logical_id']} (AWS::IAM::Role) is missing: stacks would replace the role"]
    props = role.get("Properties", {})

    if props.get("RoleName") != contract["role_name"]:
        errors.append(
            f"RoleName changed to {json.dumps(props.get('RoleName'))}, the contract records "
            f"{json.dumps(contract['role_name'])}: an update would replace the role with one Better Stack does not know"
        )
    else:
        name = _evaluate(props["RoleName"], {"ExternalId": SAMPLE_EXTERNAL_ID})
        if name != SAMPLE_ROLE_NAME:
            errors.append(f"RoleName gives {name} for {SAMPLE_EXTERNAL_ID}, not {SAMPLE_ROLE_NAME}")

    tags = {tag.get("Key"): tag.get("Value") for tag in props.get("Tags", [])}
    stamped = {"Fn::FindInMap": ["Release", "Current", "Version"]}
    if tags.get(contract["version_tag_key"]) != stamped:
        errors.append(
            f"the role has no tag {contract['version_tag_key']} with the stamped release version "
            f"(found keys {sorted(tags)}): Better Stack reads the version from that key"
        )

    trust = props.get("AssumeRolePolicyDocument", {})
    errors += check_trust_shape(trust.get("Statement", []))
    if trust != contract["trust"]:
        errors.append("the trust differs from the one recorded in contract.json")
    return errors


def check_trust_shape(statements):
    """One statement allowing sts:AssumeRole alone, under the external ID and the callers' path, whatever contract.json
    says."""
    errors = []
    if len(statements) != 1:
        errors.append(f"the trust has {len(statements)} statements, not 1")
    other_sts = sorted({a for s in statements for a in _actions(s) if a.lower().startswith("sts:") and a != "sts:AssumeRole"})
    if other_sts:
        errors.append(f"the trust allows {other_sts}: it may allow sts:AssumeRole only")
    for statement in statements:
        if "sts:AssumeRole" not in _actions(statement):
            continue
        conditions = _conditions(statement)
        if conditions.get(("StringEquals", "sts:ExternalId")) is None:
            errors.append("the trust statement has no StringEquals sts:ExternalId condition")
        if conditions.get(("ArnLike", "aws:PrincipalArn")) is None:
            errors.append("the trust statement has no ArnLike aws:PrincipalArn condition")
    return errors


def check_outputs_and_stamp(template, text, contract):
    errors = []
    for name in contract["outputs"]:
        if name not in template.get("Outputs", {}):
            errors.append(f"output {name} was removed")
    if len(STAMP_LINE.findall(text)) != 1:
        errors.append("the template needs exactly one '      Version: 0.0.0-dev' line, the one release.yml stamps")
    return errors


def check(template, text, contract):
    return (
        check_parameters(template, contract)
        + check_role(template, contract)
        + check_outputs_and_stamp(template, text, contract)
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("template", nargs="?", default=DEFAULT_TEMPLATE)
    parser.add_argument("--contract", default=DEFAULT_CONTRACT)
    args = parser.parse_args(argv)
    with open(args.template, encoding="utf-8") as f:
        text = f.read()
    with open(args.contract, encoding="utf-8") as f:
        contract = json.load(f)
    errors = check(cfn_yaml.load(args.template), text, contract)
    for error in errors:
        print(f"FAIL {error}")
    if errors:
        print(f"{len(errors)} contract violation(s). If the change is intended, change contract.json on purpose.")
        return 1
    print(f"{os.path.relpath(args.template)} keeps its contract ({len(contract['parameters'])} parameters, role name, trust, version tag)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
