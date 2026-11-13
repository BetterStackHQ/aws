"""check_read_only_access_review.py, offline, on the small fixtures in fixtures/drift/: a two-service reference, a
ReadOnlyAccess stand-in, a template with one Allow wildcard and one Deny, and the reviewed list for them."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

import check_read_only_access_review as review

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "drift")


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        shutil.copytree(FIXTURES, self.tmp, dirs_exist_ok=True)
        self.path = lambda *parts: os.path.join(self.tmp, *parts)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_check(self, static=False):
        argv = ["--review", self.path("read-only-access-review.tsv"), "--template", self.path("template.yaml")]
        argv += ["--static"] if static else [
            "--policy", self.path("read-only-access-policy.json"),
            "--service-reference", self.path("service-reference"),
        ]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = review.main(argv)
        return code, out.getvalue()

    def edit_json(self, name, change):
        with open(self.path(*name), encoding="utf-8") as f:
            data = json.load(f)
        change(data)
        with open(self.path(*name), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def write_review(self, text):
        with open(self.path("read-only-access-review.tsv"), "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def read_review(self):
        with open(self.path("read-only-access-review.tsv"), encoding="utf-8", newline="") as f:
            return f.read()

    def assertFails(self, message, static=False):
        code, out = self.run_check(static)
        self.assertEqual(code, 1, out)
        self.assertIn(message, out)

    def test_a_complete_list_passes(self):
        for static in (True, False):
            code, out = self.run_check(static)
            self.assertEqual(code, 0, out)

    def test_an_action_aws_adds_to_read_only_access_fails_until_reviewed(self):
        self.edit_json(("read-only-access-policy.json",), lambda d: d["Statement"][0]["Action"].append("beta:Update*"))
        self.assertFails("FAIL beta:UpdateWidget has no line in the reviewed list")

    def test_an_action_an_inline_wildcard_gains_fails_until_reviewed(self):
        # alpha:List* in the template's own allow list now also matches a new action.
        self.edit_json(("service-reference", "alpha.json"), lambda d: d["Actions"].append({"Name": "ListThingSecrets"}))
        self.assertFails("FAIL alpha:ListThingSecrets has no line in the reviewed list")

    def test_a_template_pattern_that_matches_nothing_fails(self):
        with open(self.path("template.yaml"), encoding="utf-8") as f:
            text = f.read()
        with open(self.path("template.yaml"), "w", encoding="utf-8") as f:
            f.write(text.replace("- beta:Get*", "- beta:Gte*"))
        self.assertFails("FAIL the template's beta:Gte* matches no action in the service reference")

    def test_a_denied_line_no_deny_matches_fails(self):
        self.write_review(self.read_review().replace("alpha:GetThing\tallowed", "alpha:GetThing\tdenied"))
        self.assertFails("FAIL alpha:GetThing is listed as denied, and no Deny statement of the template matches it", static=True)

    def test_a_configuration_deny_does_not_make_a_line_denied(self):
        # beta:GetWidget is denied only with AllowConfigurationValues=false, so it isn't denied in both settings.
        self.write_review(self.read_review().replace("beta:GetWidget\tconfiguration-value", "beta:GetWidget\tdenied"))
        self.assertFails("FAIL beta:GetWidget is listed as denied", static=True)

    def scope_the_deny(self, extra):
        """The fixture template with its alpha:GetSecret* Deny given EXTRA (a condition or a resource scope)."""
        with open(self.path("template.yaml"), encoding="utf-8") as f:
            text = f.read()
        old = "                  - alpha:GetSecret*\n                Resource: '*'\n"
        assert text.count(old) == 1
        with open(self.path("template.yaml"), "w", encoding="utf-8") as f:
            f.write(text.replace(old, "                  - alpha:GetSecret*\n" + extra))

    def test_a_resource_scoped_deny_needs_a_note(self):
        self.scope_the_deny("                NotResource: 'arn:aws:alpha:::thing/public/*'\n")
        code, out = self.run_check(static=True)
        self.assertEqual(code, 0, out)  # the fixture's line carries a note
        self.write_review(self.read_review().replace("alpha:GetSecretValue\tdenied\tthe secret itself", "alpha:GetSecretValue\tdenied"))
        self.assertFails("FAIL alpha:GetSecretValue is listed as denied, and only a conditional or resource-scoped Deny matches it", static=True)

    def test_a_conditional_deny_needs_a_note(self):
        self.scope_the_deny("                Resource: '*'\n                Condition:\n                  StringNotLike:\n                    aws:SourceVpc: vpc-1\n")
        self.write_review(self.read_review().replace("alpha:GetSecretValue\tdenied\tthe secret itself", "alpha:GetSecretValue\tdenied"))
        self.assertFails("only a conditional or resource-scoped Deny matches it", static=True)

    def test_a_deny_on_some_resources_only_needs_a_note(self):
        self.scope_the_deny("                Resource: 'arn:aws:alpha:::thing/private/*'\n")
        self.write_review(self.read_review().replace("alpha:GetSecretValue\tdenied\tthe secret itself", "alpha:GetSecretValue\tdenied"))
        self.assertFails("only a conditional or resource-scoped Deny matches it", static=True)

    def test_an_unconditional_deny_needs_no_note(self):
        self.write_review(self.read_review().replace("alpha:GetSecretValue\tdenied\tthe secret itself", "alpha:GetSecretValue\tdenied"))
        code, out = self.run_check(static=True)
        self.assertEqual(code, 0, out)

    def test_an_action_spelled_differently_from_the_service_reference_fails(self):
        self.write_review(self.read_review().replace("alpha:GetThing\tallowed", "alpha:Getthing\tallowed"))
        self.assertFails("FAIL alpha:Getthing is spelled alpha:GetThing in the service reference: use its spelling")

    def test_format_errors_fail(self):
        lines = self.read_review().split("\n")
        cases = {
            "out of order": "\n".join(lines[:2] + [lines[3], lines[2]] + lines[4:]),
            "listed twice": self.read_review() + "beta:GetWidget\tallowed\n",
            "verdict 'readable'": self.read_review().replace("alpha:GetThing\tallowed", "alpha:GetThing\treadable"),
            "only denied or configuration-value lines carry a note": self.read_review().replace(
                "alpha:GetThing\tallowed", "alpha:GetThing\tallowed\ta note"),
            "'# ReadOnlyAccess <version id>' is missing": self.read_review().replace("# ReadOnlyAccess v7\n", ""),
            "'# service reference read <YYYY-MM-DD>' is missing": self.read_review().replace(
                "# service reference read 2026-11-12\n", ""),
            "LF line endings": self.read_review().replace("\n", "\r\n"),
            "does not end with a newline": self.read_review().rstrip("\n"),
            "is not a prefix:ActionName action": self.read_review().replace("alpha:GetThing", "alpha:Get Thing"),
        }
        original = self.read_review()
        for message, text in cases.items():
            with self.subTest(message):
                self.write_review(text)
                self.assertFails(message, static=True)
                self.write_review(original)

    def test_a_missing_list_fails(self):
        os.remove(self.path("read-only-access-review.tsv"))
        self.assertFails("does not exist", static=True)

    def test_the_real_template_has_denies_to_check_against(self):
        patterns = review.deny_patterns(review.check_contract.DEFAULT_TEMPLATE)
        self.assertIn("secretsmanager:GetSecretValue", patterns)
        self.assertIn("s3:GetObject*", patterns)
        self.assertNotIn("lambda:ListFunctions", patterns, "the configuration deny applies in one setting only")


if __name__ == "__main__":
    unittest.main()
