#!/usr/bin/env python3
"""Checks cloudformation/ai-sre/read-only-access-review.tsv, the reviewed list of what the AI SRE role can read.

The list has two header lines, `# ReadOnlyAccess <version id>` and `# service reference read <YYYY-MM-DD>`, then one
line per action: `<prefix>:<ActionName><TAB><verdict>`, with an optional third TAB column holding a short note on
`denied` and `configuration-value` lines only. The action is spelled as AWS's service reference spells it, the lines
are sorted by the lowercased action, and none repeats. A verdict is `allowed`, `denied` (a Deny statement of the
template matches it in both settings) or `configuration-value` (readable in the default setting by design).

--static checks the list's format, and that every `denied` line is matched by a Deny statement the role has in both
settings, unconditional and on every resource. A Deny with a condition or a resource scope (the S3 objects outside
AWSLogs/, say) counts only for a line whose note says what stays readable. Every `configuration-value` line must be
denied or not granted with AllowConfigurationValues=false, on the same terms.
--policy also expands AWS's ReadOnlyAccess (the policy document given, as `iam get-policy-version` returns it) and
every Allow pattern in the template's inline policies against the service reference, and fails on every action
that has no line: one AWS added to ReadOnlyAccess, or one an inline wildcard such as cloudwatch:Get* gained since
the list was reviewed. It also fails on a template action pattern the service reference has no action for.

Usage: check_read_only_access_review.py --static [--review TSV] [--template TEMPLATE]
       check_read_only_access_review.py --policy POLICY.json [--policy-version v191] [--service-reference URL|DIR]
                                         [--cache-dir DIR] [--review TSV] [--template TEMPLATE]
"""

import argparse
import json
import os
import re
import sys

import check_contract
import template_policies
from service_reference import DEFAULT_SOURCE, ServiceReference, glob_matches

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REVIEW = os.path.join(HERE, "..", "read-only-access-review.tsv")
VERDICTS = ("allowed", "denied", "configuration-value")
ACTION = re.compile(r"^[a-z0-9-]+:[A-Za-z0-9]+$")
VERSION_HEADER = re.compile(r"^# ReadOnlyAccess (v\d+)$")
DATE_HEADER = re.compile(r"^# service reference read (\d{4}-\d{2}-\d{2})$")


def _as_list(value):
    return [value] if isinstance(value, str) else list(value)


def parse_review(text):
    """(the ReadOnlyAccess version the list was reviewed against, {action: (verdict, note)}, [format errors])."""
    errors = []
    if "\r" in text:
        errors.append("the list has CR characters: it must use LF line endings")
    if text and not text.endswith("\n"):
        errors.append("the list does not end with a newline")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    headers = [line for line in lines if line.startswith("# ")]
    version = next((m.group(1) for m in map(VERSION_HEADER.match, headers) if m), None)
    if version is None:
        errors.append("the header line '# ReadOnlyAccess <version id>' is missing")
    if not any(DATE_HEADER.match(h) for h in headers):
        errors.append("the header line '# service reference read <YYYY-MM-DD>' is missing")
    entries = {}
    previous = None
    seen = set()
    for number, line in enumerate(lines, 1):
        if line.startswith("# "):
            if entries:
                errors.append(f"line {number}: header lines come before the actions")
            continue
        fields = line.split("\t")
        if len(fields) not in (2, 3):
            errors.append(f"line {number}: expected '<action><TAB><verdict>[<TAB><note>]', got {line!r}")
            continue
        action, verdict = fields[0], fields[1]
        if not ACTION.match(action):
            errors.append(f"line {number}: {action!r} is not a prefix:ActionName action")
        if verdict not in VERDICTS:
            errors.append(f"line {number}: {action} has verdict {verdict!r}, not one of {', '.join(VERDICTS)}")
        if len(fields) == 3 and verdict == "allowed":
            errors.append(f"line {number}: {action} is allowed, and only denied or configuration-value lines carry a note")
        key = action.lower()
        if key in seen:
            errors.append(f"line {number}: {action} is listed twice")
        elif previous is not None and key < previous:
            errors.append(f"line {number}: {action} is out of order (sort by the lowercased action)")
        seen.add(key)
        previous = key
        entries[action] = (verdict, fields[2] if len(fields) == 3 else "")
    return version, entries, errors


def deny_statements(template):
    """The Deny statements the role has in both settings."""
    in_false = template_policies.role_statements(False, template)
    return [s for s in template_policies.role_statements(True, template) if s["Effect"] == "Deny" and s in in_false]


def deny_patterns(template):
    return sorted({p for s in deny_statements(template) for p in _as_list(s["Action"])})


def _unconditional(statement):
    return "Condition" not in statement and "NotResource" not in statement and "*" in _as_list(statement.get("Resource", []))


def _restricted_errors(entries, template):
    """configuration-value lines granted with AllowConfigurationValues=false and not denied there."""
    statements = template_policies.role_statements(False, template)
    allows = [s for s in statements if s["Effect"] == "Allow"]
    denies = [s for s in statements if s["Effect"] == "Deny"]
    errors = []
    for action, (verdict, note) in entries.items():
        if verdict != "configuration-value":
            continue
        if not any(any(glob_matches(p, action) for p in _as_list(s["Action"])) for s in allows):
            continue
        matching = [s for s in denies if any(glob_matches(p, action) for p in _as_list(s["Action"]))]
        if any(_unconditional(s) for s in matching) or (matching and note):
            continue
        errors.append(
            f"{action} is a configuration value, and AllowConfigurationValues=false grants it without denying it"
            + (": only a conditional or resource-scoped Deny matches, and the line has no note" if matching else "")
        )
    return errors


def static_errors(entries, template):
    statements = deny_statements(template)
    errors = _restricted_errors(entries, template)
    for action, (verdict, note) in entries.items():
        if verdict != "denied":
            continue
        matching = [s for s in statements if any(glob_matches(p, action) for p in _as_list(s["Action"]))]
        if not matching:
            errors.append(f"{action} is listed as denied, and no Deny statement of the template matches it")
        elif not any(_unconditional(s) for s in matching) and not note:
            errors.append(
                f"{action} is listed as denied, and only a conditional or resource-scoped Deny matches it: "
                "deny it on every resource, or say in a note what stays readable"
            )
    return errors


def allow_patterns(template):
    """The action patterns of the template's inline Allow statements, in either setting."""
    found = set()
    for allow in (True, False):
        for statement in template_policies.role_statements(allow, template):
            if statement["Effect"] == "Allow":
                found.update(_as_list(statement["Action"]))
    return sorted(found)


def template_patterns(template):
    found = set()
    for allow in (True, False):
        for statement in template_policies.role_statements(allow, template):
            found.update(_as_list(statement["Action"]))
    return sorted(found)


def drift_errors(entries, policy_document, template, reference):
    """Every expanded action the list has no line for, and every template pattern the reference has nothing for."""
    errors = []
    for pattern in template_patterns(template):
        if not reference.expand(pattern):
            errors.append(f"the template's {pattern} matches no action in the service reference")
    expected = set()
    statements = policy_document["Statement"]
    for statement in [statements] if isinstance(statements, dict) else statements:
        if "NotAction" in statement:
            errors.append(f"ReadOnlyAccess statement {statement.get('Sid')} uses NotAction, which this check can't expand")
            continue
        if statement.get("Effect") == "Allow":
            for pattern in _as_list(statement["Action"]):
                expected |= reference.expand(pattern)
    for pattern in allow_patterns(template):
        expected |= reference.expand(pattern)
    spelled = {a.lower(): a for a in entries}
    for action in sorted(expected - set(entries)):
        if action.lower() in spelled:
            errors.append(f"{spelled[action.lower()]} is spelled {action} in the service reference: use its spelling")
        else:
            errors.append(f"{action} has no line in the reviewed list")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--static", action="store_true", help="check the list's format and its denied lines only")
    mode.add_argument("--policy", help="ReadOnlyAccess's policy document, as JSON")
    parser.add_argument("--policy-version", help="the version id of that document, such as v191")
    parser.add_argument("--service-reference", default=DEFAULT_SOURCE)
    parser.add_argument("--cache-dir")
    parser.add_argument("--review", default=DEFAULT_REVIEW)
    parser.add_argument("--template", default=check_contract.DEFAULT_TEMPLATE)
    args = parser.parse_args(argv)

    if not os.path.exists(args.review):
        print(f"FAIL {os.path.relpath(args.review)} does not exist: the reviewed ReadOnlyAccess list must be committed")
        return 1
    with open(args.review, encoding="utf-8", newline="") as f:
        version, entries, errors = parse_review(f.read())
    errors += static_errors(entries, args.template)
    if args.policy:
        with open(args.policy, encoding="utf-8") as f:
            document = json.load(f)
        reference = ServiceReference(args.service_reference, cache_dir=args.cache_dir)
        errors += drift_errors(entries, document, args.template, reference)
        if args.policy_version and version and args.policy_version != version:
            print(f"note: the list was reviewed against ReadOnlyAccess {version}; its default version is now {args.policy_version}")
    for error in errors:
        print(f"FAIL {error}")
    if errors:
        print(f"{len(errors)} problem(s) with the reviewed ReadOnlyAccess list. Review each new action and add its line.")
        return 1
    print(f"{os.path.relpath(args.review)}: {len(entries)} actions, every one reviewed" + ("" if args.static else " and none missing"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
