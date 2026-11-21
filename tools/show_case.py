"""Print everything about one case (or the Nth case) for manual review.
usage: python3 tools/show_case.py <institution_folder> <case_id|index> [--tx N]"""
import json, os, sys

folder, key = sys.argv[1], sys.argv[2]
ntx = int(sys.argv[sys.argv.index("--tx") + 1]) if "--tx" in sys.argv else 15
J = lambda n: [json.loads(l) for l in open(os.path.join(folder, n)) if l.strip()]
cases = J("cases.jsonl")
case = cases[int(key)] if key.isdigit() else next(c for c in cases if c["case_id"] == key)
cid = case["customer_id"]
print("CASE", json.dumps(case))
cust = next(c for c in J("customers.jsonl") if c["customer_id"] == cid)
print("CUSTOMER", json.dumps(cust))
alerts = {a["alert_id"]: a for a in J("alerts.jsonl")}
txmap = {t["transaction_id"]: t for t in J("transactions.jsonl") if t["customer_id"] == cid}
for aid in case["alert_ids"]:
    a = alerts[aid]
    print(f"\nALERT {aid} {a['rule_id']}: {a['message']}")
    for t in a["transaction_ids"][:ntx]:
        x = txmap.get(t, {})
        print(f"   {t} {x.get('booked_at','')[:16]} {x.get('direction')} {x.get('channel')} £{x.get('amount_gbp')} "
              f"{x.get('counterparty_name')} [{x.get('counterparty_account')}] {x.get('counterparty_country')} ref={x.get('reference')}")
    if len(a["transaction_ids"]) > ntx: print(f"   ... {len(a['transaction_ids'])} txns")
for r in J("previous_reviews.jsonl"):
    if r["customer_id"] == cid: print("REVIEW", json.dumps(r))
for e in J("evidence.jsonl"):
    if e["customer_id"] != cid: continue
    print(f"\n--- DOC {e['doc_id']} ({e['type']}, {e['dated']}) {e['title']}")
    print(open(os.path.join(folder, e["file"])).read().strip()[:1500])
