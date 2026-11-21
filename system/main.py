"""Entry point: system/run <institution folder> <output file>.

Owner: Jad (A). Loads the cases, runs the officers, applies the gate, checks each answer and
writes one JSON line per case. Any error on a case gives a safe ESCALATE line, never a gap.
"""
import json
import os
import sys

# Let `import common` etc. work whatever folder we are run from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import officers  # noqa: E402
import tom  # noqa: E402
from common import REASON_CODES, VERDICTS, cheapest_verdict  # noqa: E402
from loader import load_folder, read_jsonl  # noqa: E402

NEAR_CERTAIN = 0.9      # Tom and Oliver both this sure and agreeing: their verdict stands
ESCALATE_ROUTE = 0.2    # average P(escalate) at or above this: Ivy decides, otherwise Daniel


# ---------------------------------------------------------------- the pipeline
def decide_case(bundle):
    tom_d = tom.decide(bundle)
    oliver_d = officers.oliver(bundle, tom_d)
    if oliver_d is None:
        return tom_d  # no LLM available: the rules engine decides alone

    # Gate: agreement with near certainty stands. Tom's wording is used (rules give exact facts).
    if (tom_d["verdict"] == oliver_d["verdict"]
            and tom_d["probs"].get(tom_d["verdict"], 0) >= NEAR_CERTAIN
            and oliver_d["probs"].get(oliver_d["verdict"], 0) >= NEAR_CERTAIN):
        return tom_d

    # Otherwise a specialist decides between the two verdicts still in play.
    average = {v: (tom_d["probs"].get(v, 0) + oliver_d["probs"].get(v, 0)) / 2 for v in VERDICTS}
    if average["ESCALATE"] >= ESCALATE_ROUTE:
        final = officers.ivy(bundle, tom_d, oliver_d)
    else:
        final = officers.daniel(bundle, tom_d, oliver_d)
    if final is None:  # specialist failed: take the cheaper verdict by expected cost
        final = tom_d if tom_d["verdict"] == cheapest_verdict(average) else oliver_d

    # Safety rule: a language model may make a verdict safer, never less safe.
    if final["verdict"] == "CLOSE" and tom_d["verdict"] != "CLOSE":
        final = tom_d
    return final


# ---------------------------------------------------------------- output line
def finish(bundle, decision):
    """Turn a decision into the output line, fixing anything that would break the format."""
    case = bundle["case"]
    verdict = decision["verdict"] if decision["verdict"] in REASON_CODES else "ESCALATE"
    code = decision["reason_code"]
    if code not in REASON_CODES[verdict]:
        code = {"CLOSE": "WITHIN_PROFILE", "RFI": "RFI_PURPOSE_DOCUMENTS",
                "ESCALATE": "ESC_UNEXPLAINED_FUNDS"}[verdict]
    # Keep only evidence IDs that exist in this case's files, without duplicates.
    evidence = []
    for i in decision["evidence"]:
        if i in bundle["valid_ids"] and i not in evidence:
            evidence.append(i)
    return {
        "case_id": case["case_id"],
        "alert_ids": case["alert_ids"],
        "verdict": verdict,
        "reason_code": code,
        "reason": decision["reason"],
        "evidence": evidence,
    }


def fallback(case, error):
    """Safe line for a case that crashed: escalate (a missing case counts as one anyway)."""
    return {
        "case_id": case["case_id"],
        "alert_ids": case["alert_ids"],
        "verdict": "ESCALATE",
        "reason_code": "ESC_UNEXPLAINED_FUNDS",
        "reason": "The automated review of this case could not be completed (" + type(error).__name__
                  + "). It is escalated so an analyst reviews it.",
        "evidence": list(case["alert_ids"]),
    }


# ---------------------------------------------------------------- main
def main():
    if len(sys.argv) != 3:
        print("usage: system/run <institution folder> <output file>", file=sys.stderr)
        return 2
    folder = os.path.abspath(sys.argv[1])
    out_path = os.path.abspath(sys.argv[2])

    rows = []
    try:
        bundles = load_folder(folder)
    except Exception as error:
        # Loading failed: still write a line for every case we can find.
        print("load failed:", repr(error), file=sys.stderr)
        for case in read_jsonl(os.path.join(folder, "cases.jsonl")):
            rows.append(fallback(case, error))
        bundles = []

    for bundle in bundles:
        try:
            rows.append(finish(bundle, decide_case(bundle)))
        except Exception as error:
            print(bundle["case"]["case_id"], "failed:", repr(error), file=sys.stderr)
            rows.append(fallback(bundle["case"], error))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    counts = {v: sum(r["verdict"] == v for r in rows) for v in VERDICTS}
    print(len(rows), "cases ->", out_path, counts, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
