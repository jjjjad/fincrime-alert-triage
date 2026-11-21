"""Hand-label cases, one at a time, in the terminal.

Usage (from the repo root): python3 tools/label.py payments_fx [REASON_CODE]

Shows Tom's CLOSE cases first (the riskiest: a wrongful close costs 10), one per kind of case
(rules fired + document types), skipping cases already labelled. Optionally only cases where
Tom gave REASON_CODE. You decide first; Tom's answer is shown afterwards.
Saves one line per case to labels/<institution>.jsonl. Press Ctrl+C to stop.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "system"))
from common import REASON_CODES  # noqa: E402
from loader import load_folder, read_jsonl  # noqa: E402

inst = sys.argv[1]
only_code = sys.argv[2] if len(sys.argv) > 2 else None
labels_path = f"labels/{inst}.jsonl"
done = {r["case_id"] for r in read_jsonl(labels_path)} if os.path.exists(labels_path) else set()
tom = {r["case_id"]: r for r in read_jsonl(f"output/{inst}.jsonl")}

# Order: Tom's closes first, and the first case of each kind before any repeats.
bundles = [b for b in load_folder("institutions/" + inst) if b["case"]["case_id"] not in done
           and (only_code is None or tom[b["case"]["case_id"]]["reason_code"] == only_code)]
seen = set()


def priority(b):
    kind = (tuple(sorted(a["rule_id"] for a in b["alerts"])), tuple(sorted(d["type"] for d in b["evidence"])))
    first = kind not in seen
    seen.add(kind)
    return (tom[b["case"]["case_id"]]["verdict"] != "CLOSE", not first)


bundles.sort(key=priority)

for b in bundles:
    case, cust = b["case"], b["customer"]
    txns = {t["transaction_id"]: t for t in b["transactions"]}
    print("\n" + "=" * 100)
    print(f"CASE {case['case_id']}  window {case['review_window']['from']} to {case['review_window']['to']}"
          f"  held payment: {case['held_payment_id']}")
    print(f"CUSTOMER {cust.get('name')} ({cust.get('kind')}, {cust.get('occupation') or cust.get('business_activity')})"
          f"  declared: {cust.get('declared')}")
    for a in b["alerts"]:
        print(f"\nALERT {a['rule_id']}: {a['message']}")
        for i in a["transaction_ids"]:
            t = txns[i]
            print(f"   {i} {t['booked_at'][:10]} {t['direction']:3} {t['channel']:15} £{t['amount_gbp']:>13,.2f}  "
                  f"{t['counterparty_name']}  {t['counterparty_account'] or ''} {t['counterparty_country'] or ''}  "
                  f"ref: {t['reference']}")
    for r in b["previous_reviews"]:
        print(f"\nEARLIER REVIEW {r['review_id']} {r['reviewed_on']} {r['rule_id']} {r['verdict']}: {r['note']}")
    for e in b["list_entries"]:
        print(f"\nLIST ENTRY {e}")
    for d in b["evidence"]:
        print(f"\n--- {d['doc_id']} ({d['type']}) ---\n{d['text'].strip()}")

    print("\n" + "-" * 100)
    verdict = {"c": "CLOSE", "r": "RFI", "e": "ESCALATE"}.get(input("Verdict? c=CLOSE r=RFI e=ESCALATE s=skip: ").strip())
    if not verdict:
        continue
    codes = REASON_CODES[verdict]
    print("  ".join(f"{n}={c}" for n, c in enumerate(codes)))
    code = codes[int(input("Reason code number: "))]
    note = input("Note (why, a few words): ")
    with open(labels_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"case_id": case["case_id"], "verdict": verdict, "reason_code": code,
                            "note": note, "by": "hand"}) + "\n")
    t = tom[case["case_id"]]
    print(f"Saved. Tom said {t['verdict']} / {t['reason_code']}: {t['reason']}")
