"""Score an output against hand labels using the brief's cost matrix.

usage: python3 tools/score.py <labels.jsonl> <output.jsonl>
labels.jsonl: {"case_id": ..., "verdict": "CLOSE|RFI|ESCALATE", "reason_code": optional}
The development cases ship without answers, so label a sample yourself.
"""
from __future__ import annotations

import json
import sys
from collections import Counter

# COST[right][sent]
COST = {
    "CLOSE": {"CLOSE": 0, "RFI": 3, "ESCALATE": 3},
    "RFI": {"CLOSE": 10, "RFI": 0, "ESCALATE": 2},
    "ESCALATE": {"CLOSE": 10, "RFI": 3, "ESCALATE": 0},
}


def load(p):
    with open(p, encoding="utf-8") as f:
        return {r["case_id"]: r for r in (json.loads(l) for l in f if l.strip())}


def main(lp, op):
    labels, out = load(lp), load(op)
    cost, conf, code_ok, n = 0, Counter(), 0, 0
    for cid, lab in labels.items():
        sent = out.get(cid, {}).get("verdict", "ESCALATE")  # missing counts as escalation
        right = lab["verdict"]
        cost += COST[right][sent]
        conf[(right, sent)] += 1
        n += 1
        if sent == right and lab.get("reason_code") and out.get(cid, {}).get("reason_code") == lab["reason_code"]:
            code_ok += 1
    worst = sum(max(COST[l["verdict"]].values()) for l in labels.values())
    esc_all = sum(COST[l["verdict"]]["ESCALATE"] for l in labels.values())
    print(f"cases {n}   total cost {cost}   mean {cost / max(n, 1):.3f}   "
          f"(escalate-everything baseline {esc_all}, worst {worst})")
    print("right \\ sent   CLOSE   RFI   ESCALATE")
    for r in ("CLOSE", "RFI", "ESCALATE"):
        print(f"{r:<13}" + "".join(f"{conf[(r, s)]:>8}" for s in ("CLOSE", "RFI", "ESCALATE")))
    print(f"wrongful closes: {conf[('RFI', 'CLOSE')] + conf[('ESCALATE', 'CLOSE')]}")
    print(f"reason code agrees (where labelled and verdict right): {code_ok}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1], sys.argv[2])
