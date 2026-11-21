"""End-to-end tests on the synthetic fixtures: python3 -m unittest discover tests"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, "system", "run")
FIX = os.path.join(ROOT, "tests", "fixtures")

EXPECTED = {
    "bank": {
        "CS1": ("CLOSE", "BUSINESS_RECORDS_RECONCILE"),
        "CS2": ("RFI", "RFI_SOURCE_OF_FUNDS"),
        "CS3": ("ESCALATE", "ESC_MULE_ACTIVITY"),
        "CS4": ("CLOSE", "LIST_MATCH_DIFFERENT_PARTY"),
        "CS5": ("RFI", "RFI_UPDATED_PROFILE"),
        "CS6": ("CLOSE", "RETURNING_CUSTOMER"),
    },
    "payments_fx": {
        "PC1": ("CLOSE", "INVOICE_MATCHES"),
        "PC2": ("RFI", "RFI_PURPOSE_DOCUMENTS"),
    },
}


def run(inst, out, cwd):
    env = dict(os.environ, TRIAGE_GENERIC_ONLY="1")  # fixtures test the generic engine, not the templates
    subprocess.run([RUN, os.path.join(FIX, inst), out], check=True, cwd=cwd, capture_output=True, env=env)
    with open(out) as f:
        return f.read()


class SystemTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run([sys.executable, os.path.join(ROOT, "tests", "make_fixtures.py")], check=True,
                       capture_output=True)

    def test_verdicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            for inst, exp in EXPECTED.items():
                rows = {r["case_id"]: r for r in map(json.loads, run(inst, f"{tmp}/{inst}.jsonl", tmp).splitlines())}
                self.assertEqual(set(rows), set(exp))
                for cid, (verdict, code) in exp.items():
                    self.assertEqual((rows[cid]["verdict"], rows[cid]["reason_code"]), (verdict, code), cid)

    def test_valid_and_reproducible_from_any_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            for inst in EXPECTED:
                a = run(inst, f"{tmp}/a.jsonl", os.path.join(ROOT, "system"))
                b = run(inst, f"{tmp}/b.jsonl", "/")
                self.assertEqual(a, b)
                v = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "validate.py"),
                                    os.path.join(FIX, inst), f"{tmp}/a.jsonl"], capture_output=True, text=True)
                self.assertEqual(v.returncode, 0, v.stdout)


if __name__ == "__main__":
    unittest.main()
