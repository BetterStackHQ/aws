#!/usr/bin/env python3
"""Checks cloudformation/ai-sre/restricted-reads.txt, every action the role's Allow statements match with
AllowConfigurationValues=false, against AWS's service reference.

The restricted role's read list is written with wildcards such as cloudwatch:Get*, so it grows when AWS adds an action
one of them matches. The list records what they matched when it was last reviewed: one action per line, spelled as the
service reference spells it and sorted, followed by a TAB and `denied` when a Deny statement of that setting matches
the action on every resource without a condition, or `denied on some resources` when only a narrower Deny does.

Fails on every action the wildcards match now that the list lacks (review what it returns, deny it in the template if
it returns secrets, stored data or configuration values, then run with --update), on every line that is out of date,
and on every action pattern of the template, in either setting, that matches no action at all (a misspelled name).

It reads AWS's public service reference, so it fails when AWS changes something, not only when this repository does.
Run it by hand before a release; it is not part of the pull request checks.

Usage: check_restricted_reads.py [--update] [--service-reference URL|DIR] [--cache-dir DIR] [--list FILE]
                                 [--template TEMPLATE]
"""

import argparse
import os
import sys

import check_contract
import template_policies
from service_reference import DEFAULT_SOURCE, ServiceReference, glob_matches

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LIST = os.path.join(HERE, "..", "restricted-reads.txt")


def _as_list(value):
    return [value] if isinstance(value, str) else list(value)


def _everywhere(statement):
    return "Condition" not in statement and "NotResource" not in statement and "*" in _as_list(statement.get("Resource", []))


def unmatched_patterns(template, reference):
    """The template's action patterns, in either setting, that match no action in the service reference."""
    patterns = set()
    for allow_configuration_values in (True, False):
        for statement in template_policies.role_statements(allow_configuration_values, template):
            patterns.update(_as_list(statement["Action"]))
    return sorted(p for p in patterns if not reference.expand(p))


def restricted_reads(template, reference):
    """The lines of the list, as the template and the service reference give them now."""
    statements = template_policies.role_statements(False, template)
    denies = [s for s in statements if s["Effect"] == "Deny"]
    actions = set()
    for statement in statements:
        if statement["Effect"] == "Allow":
            for pattern in _as_list(statement["Action"]):
                actions |= reference.expand(pattern)
    lines = []
    for action in sorted(actions, key=str.lower):
        matching = [s for s in denies if any(glob_matches(p, action) for p in _as_list(s["Action"]))]
        if any(_everywhere(s) for s in matching):
            action += "\tdenied"
        elif matching:
            action += "\tdenied on some resources"
        lines.append(action)
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--update", action="store_true", help="rewrite the list instead of checking it")
    parser.add_argument("--service-reference", default=DEFAULT_SOURCE)
    parser.add_argument("--cache-dir")
    parser.add_argument("--list", default=DEFAULT_LIST)
    parser.add_argument("--template", default=check_contract.DEFAULT_TEMPLATE)
    args = parser.parse_args(argv)

    reference = ServiceReference(args.service_reference, cache_dir=args.cache_dir)
    errors = [f"the template's {p} matches no action in the service reference" for p in unmatched_patterns(args.template, reference)]
    current = restricted_reads(args.template, reference)
    if args.update:
        with open(args.list, "w", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in current))
    else:
        recorded = []
        if os.path.exists(args.list):
            with open(args.list, encoding="utf-8") as f:
                recorded = f.read().splitlines()
        recorded_actions = {line.split("\t")[0] for line in recorded}
        for line in current:
            if line.split("\t")[0] not in recorded_actions:
                errors.append(f"{line.replace(chr(9), ', ')} is not in the list: the restricted role's wildcards match it now")
            elif line not in recorded:
                errors.append(f"the list's line for {line.split(chr(9))[0]} is out of date, it is now: {line.replace(chr(9), ', ')}")
        current_actions = {line.split("\t")[0] for line in current}
        errors += [f"{a} is in the list, and the restricted role no longer matches it" for a in sorted(recorded_actions - current_actions)]
    for error in errors:
        print(f"FAIL {error}")
    if errors:
        print(f"{len(errors)} problem(s). Review each action, change the template if one must be denied, then run with --update.")
        return 1
    print(f"{os.path.relpath(args.list)}: {len(current)} actions" + (" written" if args.update else ", all current"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
