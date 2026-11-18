"""check_restricted_reads.py, offline, on the small fixtures in fixtures/restricted-reads/: a two-service reference
and a template whose restricted setting allows alpha:List* and beta:Get* and denies beta:GetWidget."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

import check_restricted_reads as reads

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "restricted-reads")


class RestrictedReadsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        shutil.copytree(FIXTURES, self.tmp, dirs_exist_ok=True)
        self.list = os.path.join(self.tmp, "restricted-reads.txt")
        self.template = os.path.join(self.tmp, "template.yaml")
        self.assertEqual(self.run_check("--update")[0], 0)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_check(self, *flags):
        argv = ["--list", self.list, "--template", self.template, "--service-reference", os.path.join(self.tmp, "service-reference"), *flags]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = reads.main(argv)
        return code, out.getvalue()

    def add_action(self, service, name):
        path = os.path.join(self.tmp, "service-reference", f"{service}.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["Actions"].append({"Name": name})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_update_writes_what_the_wildcards_match_and_the_check_then_passes(self):
        with open(self.list, encoding="utf-8") as f:
            self.assertEqual(f.read(), "alpha:ListThings\nbeta:GetWidget\tdenied\n")
        self.assertEqual(self.run_check()[0], 0)

    def test_an_action_a_wildcard_gains_fails_until_the_list_is_updated(self):
        self.add_action("alpha", "ListSecrets")
        code, out = self.run_check()
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL alpha:ListSecrets is not in the list", out)
        self.assertEqual(self.run_check("--update")[0], 0)
        self.assertEqual(self.run_check()[0], 0)

    def test_a_deny_added_for_a_listed_action_makes_its_line_out_of_date(self):
        with open(self.template, encoding="utf-8") as f:
            text = f.read()
        with open(self.template, "w", encoding="utf-8") as f:
            f.write(text.replace("- alpha:GetSecret*", "- alpha:GetSecret*\n                  - alpha:ListThings"))
        code, out = self.run_check()
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL the list's line for alpha:ListThings is out of date, it is now: alpha:ListThings, denied", out)

    def test_a_template_pattern_that_matches_nothing_fails(self):
        with open(self.template, encoding="utf-8") as f:
            text = f.read()
        with open(self.template, "w", encoding="utf-8") as f:
            f.write(text.replace("alpha:GetSecret*", "alpha:GetSecrt*"))
        code, out = self.run_check()
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL the template's alpha:GetSecrt* matches no action in the service reference", out)


if __name__ == "__main__":
    unittest.main()
