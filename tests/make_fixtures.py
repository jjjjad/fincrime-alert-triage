"""Build small synthetic institution folders in tests/fixtures/ that follow the
brief's file layout. Field names are guesses at the real schema; they exist to
exercise the engine end to end, not to stand in for the real data."""
from __future__ import annotations

import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))


def write(folder, name, rows):
    with open(os.path.join(folder, name), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def doc(folder, did, cust, dtype, title, body):
    with open(os.path.join(folder, "evidence", f"{did}.md"), "w") as f:
        f.write(f"# {title}\n\n{body}\n")
    return {"doc_id": did, "customer_id": cust, "type": dtype, "title": title}


def tx(i, cust, date, amt, direction, cp="", channel="transfer", country="GB", **kw):
    return {"txn_id": i, "customer_id": cust, "timestamp": date, "amount_gbp": amt, "direction": direction,
            "channel": channel, "counterparty": cp, "counterparty_country": country, **kw}


def bank():
    f = os.path.join(HERE, "fixtures", "bank")
    shutil.rmtree(f, ignore_errors=True)
    os.makedirs(os.path.join(f, "evidence"))
    open(os.path.join(f, "POLICY.md"), "w").write(
        "# Bank SOP (fixture)\n\nHigher-risk countries: Iran, Syria, Myanmar, Russia\n")
    win = {"window_start": "2025-09-01", "window_end": "2025-09-30"}
    customers = [
        {"customer_id": "C1", "name": "Fernbrook Bakery Ltd", "customer_type": "business",
         "occupation": "bakery", "expected_monthly_turnover": 20000},
        {"customer_id": "C2", "name": "Fernbrook Two Ltd", "customer_type": "business",
         "occupation": "bakery", "expected_monthly_turnover": 20000},
        {"customer_id": "C3", "name": "Fernbrook Three Ltd", "customer_type": "business",
         "occupation": "bakery", "expected_monthly_turnover": 20000},
        {"customer_id": "C4", "name": "Alex Moor", "customer_type": "individual", "declared_annual_income": 48000},
        {"customer_id": "C5", "name": "Priya Shah", "customer_type": "individual", "declared_annual_income": 36000},
        {"customer_id": "C6", "name": "Tom Reed", "customer_type": "individual", "declared_annual_income": 30000},
    ]
    t, e, a, cases, lst = [], [], [], [], []
    # C1: large cash deposit reconciled by takings report -> CLOSE
    t.append(tx("T1", "C1", "2025-09-05", 12000, "in", channel="cash"))
    e.append(doc(f, "BUS-1", "C1", "business_records", "Weekly takings report",
                 "Shop takings for the week to 5 Sep 2025 banked as cash: £12,000.00"))
    a.append({"alert_id": "A1", "rule_id": "TM05", "transaction_ids": ["T1"]})
    cases.append({"case_id": "CS1", "customer_id": "C1", "alert_ids": ["A1"], **win})
    # C2: £25k from a private person, nothing on file -> RFI source of funds
    t.append(tx("T2", "C2", "2025-09-16", 25000, "in", "Jordan Fernley"))
    a.append({"alert_id": "A2", "rule_id": "TM01", "transaction_ids": ["T2"]})
    cases.append({"case_id": "CS2", "customer_id": "C2", "alert_ids": ["A2"], **win})
    # C3: 12 payers then straight out abroad, scam report on file -> ESCALATE
    for k in range(12):
        t.append(tx(f"T3-{k}", "C3", f"2025-09-2{2 + k % 2}T1{k % 10}:00:00", 400 + k * 50, "in", f"Payer {chr(65 + k)} Smith"))
    t.append(tx("T3-OUT", "C3", "2025-09-23T20:00:00", 8700, "out", "Unknown Holdings", country="AE"))
    e.append(doc(f, "SCAM-1", "C3", "external_report", "Sender bank notification",
                 "Two sending banks report their customers were the victim of a scam. The customer says the "
                 "payments were catering orders but there are no orders on file."))
    a += [{"alert_id": "A3a", "rule_id": "TM04", "transaction_ids": [f"T3-{k}" for k in range(12)]},
          {"alert_id": "A3b", "rule_id": "TM02", "transaction_ids": [f"T3-{k}" for k in range(12)] + ["T3-OUT"]}]
    cases.append({"case_id": "CS3", "customer_id": "C3", "alert_ids": ["A3a", "A3b"], **win})
    # C4: list match on name, DOB differs -> CLOSE different party
    t.append(tx("T4", "C4", "2025-09-10", 900, "out", "Daniel Black"))
    e.append(doc(f, "KYC-4", "C4", "kyc", "Payee details",
                 "Payee Daniel Black, date of birth: 1991-04-02, address 4 High St, LS1 4AB"))
    lst.append({"entry_id": "L1", "name": "Daniel Black", "date_of_birth": "1967-11-30", "address": "9 Mill Rd, M1 2CD"})
    a.append({"alert_id": "A4", "rule_id": "TM22", "transaction_ids": ["T4"], "list_entry_id": "L1"})
    cases.append({"case_id": "CS4", "customer_id": "C4", "alert_ids": ["A4"], **win})
    # C5: own-account transfers at >5x declared -> RFI updated profile
    t.append(tx("T5", "C5", "2025-09-12", 16000, "in", "Priya Shah", cp_account="12345678"))
    e.append(doc(f, "STMT-5", "C5", "bank_statement", "Savings account statement",
                 "Bank statement. Account holder: Priya Shah. Account number 12345678."))
    a.append({"alert_id": "A5", "rule_id": "TM01", "transaction_ids": ["T5"]})
    cases.append({"case_id": "CS5", "customer_id": "C5", "alert_ids": ["A5"], **win})
    # C6: dormant account resumes, KYC refreshed -> CLOSE returning customer
    t.append(tx("T6", "C6", "2025-09-03", 1200, "in", "Acme Payroll"))
    e.append(doc(f, "KYC-6", "C6", "kyc", "Customer due diligence refresh",
                 "Identity re-verified on 2025-08-30. Customer returned from working abroad; salary from Acme."))
    a.append({"alert_id": "A6", "rule_id": "TM07", "transaction_ids": ["T6"]})
    cases.append({"case_id": "CS6", "customer_id": "C6", "alert_ids": ["A6"], **win})

    write(f, "customers.jsonl", customers)
    write(f, "transactions.jsonl", t)
    write(f, "evidence.jsonl", e)
    write(f, "alerts.jsonl", a)
    write(f, "cases.jsonl", cases)
    write(f, "internal_list.jsonl", lst)
    write(f, "previous_reviews.jsonl", [])


def payments_fx():
    f = os.path.join(HERE, "fixtures", "payments_fx")
    shutil.rmtree(f, ignore_errors=True)
    os.makedirs(os.path.join(f, "evidence"))
    open(os.path.join(f, "POLICY.md"), "w").write("# Payments and FX SOP (fixture)\n")
    customers = [{"customer_id": "P1", "name": "Oakline Imports Ltd", "customer_type": "business",
                  "expected_monthly_volume": 50000, "expected_countries": ["DE", "FR"]}]
    t = [tx("PT1", "P1", "2025-10-02", 14500, "out", "Rhein Werkzeug GmbH", country="DE"),
         tx("PT2", "P1", "2025-10-03", 22000, "out", "Lyon Textiles SA", country="FR")]
    e = [doc(f, "INV-1", "P1", "invoice", "Invoice RW-889",
             "Rhein Werkzeug GmbH invoice dated 2025-09-28. Total due: £14,500.00")]
    a = [{"alert_id": "PA1", "rule_id": "TM01", "transaction_ids": ["PT1"]},
         {"alert_id": "PA2", "rule_id": "TM01", "transaction_ids": ["PT2"]}]
    cases = [{"case_id": "PC1", "customer_id": "P1", "alert_ids": ["PA1"], "held_payment_id": "PT1"},
             {"case_id": "PC2", "customer_id": "P1", "alert_ids": ["PA2"], "held_payment_id": "PT2"}]
    write(f, "customers.jsonl", customers)
    write(f, "transactions.jsonl", t)
    write(f, "evidence.jsonl", e)
    write(f, "alerts.jsonl", a)
    write(f, "cases.jsonl", cases)
    write(f, "internal_list.jsonl", [])
    write(f, "previous_reviews.jsonl", [])


if __name__ == "__main__":
    bank()
    payments_fx()
    print("fixtures written")
