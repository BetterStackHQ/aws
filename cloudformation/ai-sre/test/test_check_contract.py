"""The contract check passes on the template and fails on each change the contract forbids, each made on a copy."""

import contextlib
import io
import os
import tempfile
import unittest

import check_contract

with open(check_contract.DEFAULT_TEMPLATE, encoding="utf-8") as f:
    TEMPLATE = f.read()

LAST_PARAMETER = """  PermissionsBoundaryArn:
    Type: String
    Description: Optional. A permissions boundary your organization requires on new roles.
    Default: ''
"""

ASSUME_STATEMENT_START = "          - Sid: AssumeWithExternalId\n"
SOURCE_STATEMENT_START = "          - Sid: SetSourceIdentity\n"
POLICIES_START = "      Policies:\n"
PATH_CONDITION = """              ArnLike:
                aws:PrincipalArn: !Sub 'arn:aws:iam::476114145753:*/better-stack-ai-sre/${BetterStackEnvironment}/*'
"""
SOURCE_IDENTITY_CONDITION = """              StringLike:
                sts:SourceIdentity: 'bs-*'
"""
MERGED_TRUST = """          - Sid: AssumeWithExternalId
            Effect: Allow
            Principal:
              AWS: 'arn:aws:iam::476114145753:root'
            Action:
              - sts:AssumeRole
              - sts:SetSourceIdentity
            Condition:
              StringEquals:
                sts:ExternalId: !Ref ExternalId
              ArnLike:
                aws:PrincipalArn: !Sub 'arn:aws:iam::476114145753:*/better-stack-ai-sre/${BetterStackEnvironment}/*'
              StringLike:
                sts:SourceIdentity: 'bs-*'
"""


def trust_statement(start):
    """The text of one trust statement, from its first line to the next statement or the policies."""
    begin = TEMPLATE.index(start)
    ends = [i for i in (TEMPLATE.find("          - Sid:", begin + 1), TEMPLATE.index(POLICIES_START)) if i > begin]
    return TEMPLATE[begin:min(ends)]


def in_statement(start, old, new):
    """The template with OLD replaced by NEW inside one trust statement only."""
    statement = trust_statement(start)
    assert statement.count(old) == 1, f"the statement should contain this once: {old!r}"
    return TEMPLATE.replace(statement, statement.replace(old, new))


def replace(old, new):
    assert TEMPLATE.count(old) == 1, f"the template should contain this once: {old!r}"
    return TEMPLATE.replace(old, new)


def run(text):
    """check_contract's exit code and output on a copy of the template holding TEXT."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "better-stack-ai-sre.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check_contract.main([path])
        return code, out.getvalue()


class ContractTest(unittest.TestCase):
    def assertFails(self, text, message):
        code, out = run(text)
        self.assertEqual(code, 1, out)
        self.assertIn(message, out)

    def test_the_template_keeps_its_contract(self):
        code, out = run(TEMPLATE)
        self.assertEqual(code, 0, out)

    def test_a_removed_parameter_fails(self):
        self.assertFails(replace(LAST_PARAMETER, ""), "parameter PermissionsBoundaryArn was removed or renamed")

    def test_a_renamed_parameter_fails(self):
        self.assertFails(
            TEMPLATE.replace("AllowConfigurationValues", "AllowConfigValues"),
            "parameter AllowConfigurationValues was removed or renamed",
        )

    def test_a_new_parameter_without_a_default_fails(self):
        self.assertFails(
            replace(LAST_PARAMETER, LAST_PARAMETER + "\n  ExtraRegion:\n    Type: String\n"),
            "parameter ExtraRegion has no Default",
        )

    def test_a_new_parameter_must_be_recorded_in_the_contract(self):
        self.assertFails(
            replace(LAST_PARAMETER, LAST_PARAMETER + "\n  ExtraRegion:\n    Type: String\n    Default: ''\n"),
            "parameter ExtraRegion is not recorded in contract.json",
        )

    def test_a_changed_default_fails(self):
        self.assertFails(
            replace("    Default: production\n", "    Default: staging\n"),
            "parameter BetterStackEnvironment: Default is \"staging\"",
        )

    def test_a_changed_role_name_expression_fails(self):
        self.assertFails(
            replace("RoleName: !Join ['', ['better-stack-ai-sre-role-', !Select [3,", "RoleName: !Join ['', ['better-stack-ai-sre-', !Select [3,"),
            "RoleName changed",
        )

    def test_a_changed_external_id_pattern_fails(self):
        self.assertFails(
            replace("AllowedPattern: '^bs-ai-sre-[0-9a-f]{8}-", "AllowedPattern: '^bs-ai-sre-[0-9a-fA-F]{8}-"),
            "parameter ExternalId: AllowedPattern is",
        )

    def test_a_changed_tag_key_fails(self):
        self.assertFails(
            replace("- Key: BetterStackAiSreTemplateVersion", "- Key: BetterStackTemplateVersion"),
            "the role has no tag BetterStackAiSreTemplateVersion",
        )

    def test_merged_trust_statements_fail(self):
        start = TEMPLATE.index(ASSUME_STATEMENT_START)
        code, out = run(TEMPLATE[:start] + MERGED_TRUST + TEMPLATE[TEMPLATE.index(POLICIES_START):])
        self.assertEqual(code, 1, out)
        self.assertIn("the trust has 1 statements, not 2", out)
        self.assertIn("trust statement 1 merges sts:AssumeRole and sts:SetSourceIdentity", out)

    def test_a_third_trust_statement_fails(self):
        self.assertFails(
            replace(POLICIES_START, trust_statement(SOURCE_STATEMENT_START) + POLICIES_START),
            "the trust has 3 statements, not 2",
        )

    def test_another_sts_action_in_the_trust_fails(self):
        self.assertFails(
            in_statement(
                SOURCE_STATEMENT_START,
                "            Action: sts:SetSourceIdentity\n",
                "            Action:\n              - sts:SetSourceIdentity\n              - sts:TagSession\n",
            ),
            "the trust allows ['sts:TagSession']: only sts:AssumeRole and sts:SetSourceIdentity",
        )

    def test_dropping_the_external_id_condition_fails(self):
        self.assertFails(
            in_statement(ASSUME_STATEMENT_START, "              StringEquals:\n                sts:ExternalId: !Ref ExternalId\n", ""),
            "the sts:AssumeRole statement has no StringEquals sts:ExternalId condition",
        )

    def test_dropping_the_caller_path_condition_fails(self):
        self.assertFails(
            in_statement(ASSUME_STATEMENT_START, PATH_CONDITION, ""),
            "the sts:AssumeRole statement has no ArnLike aws:PrincipalArn condition",
        )
        self.assertFails(
            in_statement(SOURCE_STATEMENT_START, PATH_CONDITION, ""),
            "the sts:SetSourceIdentity statement has no ArnLike aws:PrincipalArn condition",
        )

    def test_dropping_the_bs_source_identity_condition_fails(self):
        self.assertFails(
            in_statement(SOURCE_STATEMENT_START, SOURCE_IDENTITY_CONDITION, ""),
            "the sts:SetSourceIdentity statement has no StringLike sts:SourceIdentity 'bs-*' condition",
        )

    def test_widening_the_bs_source_identity_condition_fails(self):
        self.assertFails(
            in_statement(SOURCE_STATEMENT_START, "sts:SourceIdentity: 'bs-*'", "sts:SourceIdentity: '*'"),
            "the sts:SetSourceIdentity statement has no StringLike sts:SourceIdentity 'bs-*' condition",
        )

    def test_the_external_id_on_the_source_identity_statement_fails(self):
        self.assertFails(
            in_statement(
                SOURCE_STATEMENT_START,
                "            Condition:\n",
                "            Condition:\n              StringEquals:\n                sts:ExternalId: !Ref ExternalId\n",
            ),
            "the sts:SetSourceIdentity statement requires sts:ExternalId",
        )

    def test_a_template_release_yml_cannot_stamp_fails(self):
        self.assertFails(
            replace("      Version: 0.0.0-dev\n", "      Version: '0.0.0-dev'\n"),
            "exactly one '      Version: 0.0.0-dev' line",
        )


if __name__ == "__main__":
    unittest.main()
