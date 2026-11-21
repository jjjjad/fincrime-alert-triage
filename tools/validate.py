"""Checks an output file against the rules of the brief.

Owner: Jad (A). Usage: python3 tools/validate.py institutions/bank output/bank.jsonl
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "system"))
from common import REASON_CODES  # noqa: E402
from loader import read_jsonl  # noqa: E402

FIELDS = ["case_id", "alert_ids", "verdict", "reason_code", "reason", "evidence"]


def main(folder, out_path):
    cases = {c["case_id"]: c for c in read_jsonl(os.path.join(folder, "cases.jsonl"))}

    # Every ID that appears anywhere in the folder.
    known_ids = set()
    for name, key in [("alerts", "alert_id"), ("transactions", "transaction_id"), ("evidence", "doc_id"),
                      ("previous_reviews", "review_id"), ("internal_list", "entry_id")]:
        known_ids.update(r[key] for r in read_jsonl(os.path.join(folder, name + ".jsonl")))

    errors = []
    seen = set()
    for n, row in enumerate(read_jsonl(out_path), start=1):
        cid = row.get("case_id")
        if list(row.keys()) != FIELDS:
            errors.append(f"line {n}: fields should be {FIELDS}")
        if cid not in cases:
            errors.append(f"line {n}: unknown case {cid}")
            continue
        if cid in seen:
            errors.append(f"{cid}: appears twice")
        seen.add(cid)
        if row.get("alert_ids") != cases[cid]["alert_ids"]:
            errors.append(f"{cid}: alert_ids differ from cases.jsonl")
        if row.get("reason_code") not in REASON_CODES.get(row.get("verdict"), []):
            errors.append(f"{cid}: reason code {row.get('reason_code')} does not fit verdict {row.get('verdict')}")
        sentences = len(re.findall(r"[.!?](\s|$)", row.get("reason", "")))
        if not 2 <= sentences <= 4:
            errors.append(f"{cid}: reason has {sentences} sentences (should be 2 to 4)")
        if not row.get("evidence"):
            errors.append(f"{cid}: no evidence IDs")
        for i in row.get("evidence", []):
            if i not in known_ids:
                errors.append(f"{cid}: evidence ID {i} does not exist")

    for cid in cases:
        if cid not in seen:
            errors.append(f"{cid}: missing from output")

    for e in errors[:50]:
        print(e)
    print(f"{out_path}: {len(seen)}/{len(cases)} cases, {len(errors)} problems")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
