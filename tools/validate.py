"""Check an output file against the brief's rules.

usage: python3 tools/validate.py <institution_folder> <output.jsonl>
Exits non-zero on any hard error (missing case, bad enum, reason code in the
wrong verdict, evidence ID not in the case files).
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "system"))
from triage.data import load, pick, read_jsonl  # noqa: E402
from triage.policy import CODES  # noqa: E402


def main(folder: str, out: str) -> int:
    f = load(folder)
    rows = read_jsonl(out)
    errors, warnings = [], []
    valid_ids = set(f.docs) | set(f.txns) | {str(pick(e, "entry_id", "list_id", "id")) for e in f.list_entries} \
        | {str(pick(r, "review_id", "id")) for rs in f.reviews_by_customer.values() for r in rs}
    by_id = {r.get("case_id"): r for r in rows}
    for c in f.cases:
        r = by_id.get(c.id)
        if r is None:
            errors.append(f"{c.id}: missing from output (counts as ESCALATE)")
            continue
        if set(r.get("alert_ids", [])) != set(c.alert_ids) and c.alert_ids:
            errors.append(f"{c.id}: alert_ids differ from cases.jsonl")
        v, code = r.get("verdict"), r.get("reason_code")
        if v not in CODES:
            errors.append(f"{c.id}: bad verdict {v!r}")
        elif code not in CODES[v]:
            errors.append(f"{c.id}: reason_code {code} does not belong to {v}")
        n = len(re.findall(r"[.!?](\s|$)", r.get("reason", "")))
        if not 2 <= n <= 4:
            warnings.append(f"{c.id}: reason has {n} sentences (want 2-4)")
        bad = [i for i in r.get("evidence", []) if i not in valid_ids]
        if bad:
            errors.append(f"{c.id}: evidence IDs not in files: {bad}")
        if not r.get("evidence"):
            warnings.append(f"{c.id}: empty evidence list")
    extra = set(by_id) - {c.id for c in f.cases}
    if extra:
        errors.append(f"{len(extra)} case_ids in output are not in cases.jsonl")
    for w in warnings[:20]:
        print("WARN ", w)
    for e in errors[:50]:
        print("ERROR", e)
    print(f"{len(f.cases)} cases, {len(errors)} errors, {len(warnings)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
