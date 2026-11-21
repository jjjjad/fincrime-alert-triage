"""Reads one institution folder and builds one "case bundle" per case.

Owner: Jad (A). A bundle is everything about one case in one dict, so the officers never
read files themselves. The bundle's keys are part of the team contract (see docs/ARCHITECTURE.md).
"""
import json
import os


def read_jsonl(path):
    """Read a file with one JSON object per line into a list of dicts."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def group_by(rows, key):
    """Turn a list of dicts into {value of key: [rows with that value]}."""
    groups = {}
    for row in rows:
        groups.setdefault(row.get(key), []).append(row)
    return groups


def load_folder(folder):
    """Return a list of bundles, one per line of cases.jsonl, in file order."""
    institution = os.path.basename(os.path.normpath(folder))

    def path(name):
        return os.path.join(folder, name)

    with open(path("POLICY.md"), encoding="utf-8") as f:
        policy = f.read()

    cases = read_jsonl(path("cases.jsonl"))
    alerts_by_id = {a["alert_id"]: a for a in read_jsonl(path("alerts.jsonl"))}
    customers_by_id = {c["customer_id"]: c for c in read_jsonl(path("customers.jsonl"))}
    txns_by_customer = group_by(read_jsonl(path("transactions.jsonl")), "customer_id")
    docs_by_customer = group_by(read_jsonl(path("evidence.jsonl")), "customer_id")
    reviews_by_customer = group_by(read_jsonl(path("previous_reviews.jsonl")), "customer_id")
    list_by_id = {e["entry_id"]: e for e in read_jsonl(path("internal_list.jsonl"))}

    bundles = []
    for case in cases:
        customer_id = case["customer_id"]
        alerts = [alerts_by_id[a] for a in case["alert_ids"] if a in alerts_by_id]

        # Documents on file, each with its Markdown text added under "text".
        evidence = []
        for doc in docs_by_customer.get(customer_id, []):
            doc = dict(doc)
            with open(path(doc["file"]), encoding="utf-8") as f:
                doc["text"] = f.read()
            evidence.append(doc)

        # Internal list entries that this case's TM22 alerts point to.
        list_entries = []
        for a in alerts:
            entry_id = (a.get("measured") or {}).get("list_entry")
            if entry_id in list_by_id:
                list_entries.append(list_by_id[entry_id])

        customer = customers_by_id.get(customer_id, {})
        transactions = txns_by_customer.get(customer_id, [])
        reviews = reviews_by_customer.get(customer_id, [])

        # Every ID this case may cite as evidence.
        valid_ids = set(case["alert_ids"])
        valid_ids.update(t["transaction_id"] for t in transactions)
        valid_ids.update(d["doc_id"] for d in evidence)
        valid_ids.update(r["review_id"] for r in reviews)
        valid_ids.update(e["entry_id"] for e in list_entries)

        bundles.append({
            "institution": institution,
            "policy": policy,
            "case": case,
            "alerts": alerts,
            "customer": customer,
            "transactions": transactions,
            "evidence": evidence,
            "previous_reviews": reviews,
            "list_entries": list_entries,
            "valid_ids": valid_ids,
        })
    return bundles
