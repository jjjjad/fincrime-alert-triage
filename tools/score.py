"""Scores an output file against our hand labels with the brief's cost table.

Owner: Jad (A). Usage: python3 tools/score.py labels/bank.jsonl output/bank.jsonl
A labels file has one line per labelled case: {"case_id": "...", "verdict": "...", "reason_code": "..."}
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "system"))
from common import COST, VERDICTS  # noqa: E402
from loader import read_jsonl  # noqa: E402


def main(labels_path, out_path):
    labels = {r["case_id"]: r for r in read_jsonl(labels_path)}
    sent = {r["case_id"]: r for r in read_jsonl(out_path)}

    cost = 0              # our total cost
    cost_escalate_all = 0  # what escalating every case would cost (the brief's baseline)
    table = {right: {s: 0 for s in VERDICTS} for right in VERDICTS}
    code_right = 0
    for cid, label in labels.items():
        right = label["verdict"]
        ours = sent[cid]["verdict"] if cid in sent else "ESCALATE"  # missing counts as escalate
        cost += COST[right][ours]
        cost_escalate_all += COST[right]["ESCALATE"]
        table[right][ours] += 1
        if ours == right and sent.get(cid, {}).get("reason_code") == label.get("reason_code"):
            code_right += 1

    print("right \\ sent   " + "  ".join(f"{s:>8}" for s in VERDICTS))
    for right in VERDICTS:
        print(f"{right:<14} " + "  ".join(f"{table[right][s]:>8}" for s in VERDICTS))
    n = len(labels)
    wrongful = table["RFI"]["CLOSE"] + table["ESCALATE"]["CLOSE"]
    saved = 1 - cost / cost_escalate_all if cost_escalate_all else 0
    print(f"\n{n} labelled cases | wrongful closes: {wrongful} | cost per case: {cost / n:.2f}")
    print(f"verdict score (share of escalate-all cost saved): {saved:.1%} | right reason code: {code_right}/{n}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
