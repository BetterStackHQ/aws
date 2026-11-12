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

SECOND_STATEMENT = """          - Sid: SourceIdentityAndSessionTags
            Effect: Allow
            Principal:
              AWS: 'arn:aws:iam::476114145753:root'
            Action:
              - sts:SetSourceIdentity
              - sts:TagSession
            Condition:
              ArnLike:
                aws:PrincipalArn: !Sub 'arn:aws:iam::476114145753:*/better-stack-ai-sre/${BetterStackEnvironment}/*'
"""

PATH_CONDITION = """              ArnLike:
                aws:PrincipalArn: !Sub 'arn:aws:iam::476114145753:*/better-stack-ai-sre/${BetterStackEnvironment}/*'
"""


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

    def test_a_second_trust_statement_fails(self):
        self.assertFails(replace("      Policies:\n", SECOND_STATEMENT + "      Policies:\n"), "the trust has 2 statements, not 1")

    def test_another_sts_action_in_the_trust_fails(self):
        self.assertFails(
            replace("            Action: sts:AssumeRole\n", "            Action:\n              - sts:AssumeRole\n              - sts:TagSession\n"),
            "the trust allows ['sts:TagSession']: it may allow sts:AssumeRole only",
        )

    def test_dropping_the_external_id_condition_fails(self):
        self.assertFails(
            replace("              StringEquals:\n                sts:ExternalId: !Ref ExternalId\n", ""),
            "the trust statement has no StringEquals sts:ExternalId condition",
        )

    def test_dropping_the_caller_path_condition_fails(self):
        self.assertFails(replace(PATH_CONDITION, ""), "the trust statement has no ArnLike aws:PrincipalArn condition")

    def test_a_template_release_yml_cannot_stamp_fails(self):
        self.assertFails(
            replace("      Version: 0.0.0-dev\n", "      Version: '0.0.0-dev'\n"),
            "exactly one '      Version: 0.0.0-dev' line",
        )


if __name__ == "__main__":
    unittest.main()
