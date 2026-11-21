"""Officer Tom: the deterministic rules engine, written from each institution's POLICY.md.

Owner: Hamid (B).

WHAT TOM RECEIVES
The data has inputs only, no answers (no labels). Each case is a question: "review this
customer for these alerts". Tom gets one `bundle` (built by loader.py) holding:

  bundle["case"]              the line from cases.jsonl: case_id, customer_id, review_window,
                              alert_ids, held_payment_id (payments_fx only: the payment on hold)
  bundle["alerts"]            WHY it was flagged. A case can have several alerts; one verdict must
                              cover them all. Each alert: rule_id (TM01..TM22), message (plain
                              English), measured (the numbers), transaction_ids (what it fired on)
  bundle["customer"]          who they are and what they declared at onboarding
                              (customer["declared"]: income/turnover, expected countries, ...)
  bundle["transactions"]      all of the customer's transactions
  bundle["evidence"]          WHAT MIGHT EXPLAIN IT: documents already on file (invoices, ID checks,
                              company registers...). Each has doc_id, type, title, dated and
                              text (the Markdown, with lines like "**Amount due:** £2,400.00")
  bundle["previous_reviews"]  earlier decisions on OLD alerts (cover only what they describe)
  bundle["list_entries"]      internal-list entries named by TM22 alerts
  bundle["institution"]       bank, emoney, investment or payments_fx

Tom's question for every case: does the evidence on file explain EVERY alert?
  yes, fully                                         -> CLOSE
  no, nothing looks criminal, one document would settle it -> RFI
  no, and something looks wrong                      -> ESCALATE
The rules' limits differ per institution, but the alerts have already applied them, so Tom
doesn't re-check limits. The policy settings Tom applies are the same at all four, plus one
extra at payments_fx (a payment of £10,000+ to anyone but the client needs an invoice/contract).

WHAT TOM OUTPUTS
A decision (common.make_decision) whose "options" are the verdicts still possible:
  ["CLOSE"]              sure: a document explains every alert, no red flags
  ["CLOSE", "RFI"]       explained, but the proof is soft             -> Daniel decides
  ["RFI"]                sure: the policy names RFI (own accounts at 5x the profile or more)
  ["RFI", "ESCALATE"]    something is unexplained / nothing settles it -> Ivy decides
  ["ESCALATE"]           sure: a red flag (fraud report, staff note, listed party in control...)
Oliver checks every case; a sure Tom stands only if Oliver agrees (see main.py). "verdict" is
Tom's best guess, used when there is no LLM. Nothing can close unless CLOSE is in Tom's options.
"""
import re
from datetime import datetime

from common import make_decision

# Documents that never prove where money came from or went: routine onboarding papers,
# registers (used only for list checks) and the customer's own words (a claim is not proof).
NOT_PROOF = {"due_diligence_summary", "proof_of_address", "contact_note", "support_chat",
             "customer_email", "customer_letter", "company_register_extract",
             "significant_control_register"}

# The CLOSE reason code each kind of explaining document gives. Any other type -> DOCUMENTED_EVENT.
INVOICE_TYPES = {"invoice", "dealer_invoice", "hospital_invoice", "supplier_invoice", "supplier_invoices",
                 "consulting_contract", "contract_schedule", "supply_contract", "purchase_order",
                 "payment_file", "carrier_invoice", "freight_invoices", "receipt"}
CLOSE_CODE = {
    "booking_confirmation": "COLLECTION_DOCUMENTED",
    "takings_book": "BUSINESS_RECORDS_RECONCILE", "cash_collection_reports": "BUSINESS_RECORDS_RECONCILE",
    "cash_reconciliation": "BUSINESS_RECORDS_RECONCILE", "management_accounts": "BUSINESS_RECORDS_RECONCILE",
    "ticket_report": "BUSINESS_RECORDS_RECONCILE",
    "bank_statement": "OWN_ACCOUNT",
    "group_structure": "GROUP_COMPANY",
    "gift_letter": "THIRD_PARTY_DOCUMENTED", "factoring_agreement": "THIRD_PARTY_DOCUMENTED",
    "remittance_advice": "THIRD_PARTY_DOCUMENTED", "email": "THIRD_PARTY_DOCUMENTED",
    "credit_note": "REFUND_OF_EARLIER_PAYMENT", "venue_email": "REFUND_OF_EARLIER_PAYMENT",
    "deposit_return_letter": "REFUND_OF_EARLIER_PAYMENT", "cancellation_confirmation": "REFUND_OF_EARLIER_PAYMENT",
    "reverification_record": "RETURNING_CUSTOMER",
}
CLOSE_CODE.update({t: "INVOICE_MATCHES" for t in INVOICE_TYPES})

# When nothing explains an alert, the ESCALATE code that best fits the rule that fired.
UNEXPLAINED_CODE = {"TM03": "ESC_STRUCTURING", "TM06": "ESC_HIGH_RISK_CORRIDOR",
                    "TM08": "ESC_THIRD_PARTY_FUNDING", "TM10": "ESC_EARLY_REDEMPTION",
                    "TM02": "ESC_PASS_THROUGH"}


# ---------------------------------------------------------------- small helpers
def money(text):
    """All £ amounts in a text, as numbers: "£2,400.00" -> 2400.0"""
    return {float(m.replace(",", "")) for m in re.findall(r"£([\d,]+\.\d\d)", text)}


def dates(text):
    """All full dates like "7 August 2026" in a text, in order."""
    return [datetime.strptime(d, "%d %B %Y") for d in re.findall(r"\b\d{1,2} [A-Z][a-z]+ \d{4}\b", text)]


def day(txn):
    return datetime.strptime(txn["booked_at"][:10], "%Y-%m-%d")


def describe(txn):
    """e.g. "£2,400.00 paid to Rayhurst Consulting Ltd on 8 August 2026" """
    who = txn["counterparty_name"] or txn["channel"].replace("_", " ")
    way = "received from" if txn["direction"] == "in" else "paid to"
    return f"£{txn['amount_gbp']:,.2f} {way} {who} on {day(txn).day} {day(txn):%B %Y}"


def sentence_with(text, pattern):
    """The first sentence of a document's body that matches a pattern (else the first sentence)."""
    body = " ".join(l for l in text.splitlines() if l.strip() and not l.startswith(("#", "**", "|")))
    sentences = re.split(r"(?<=[.!?])\s+", body)
    return next((s for s in sentences if re.search(pattern, s, re.I)), sentences[0]).strip()


def decision(verdict, code, sentences, ids, options=None):
    """Tom's decision. `options` are the verdicts still possible (default: only `verdict`, i.e. sure)."""
    # dict.fromkeys drops repeats (two alerts can flag the same transaction) but keeps the order.
    return make_decision(verdict, code, " ".join(dict.fromkeys(sentences)), dict.fromkeys(ids), {}, "tom",
                         options)


# ---------------------------------------------------------------- entry point
def decide(bundle):
    name = bundle["customer"].get("name", "the customer")
    txns = {t["transaction_id"]: t for t in bundle["transactions"]}
    alerted = [i for a in bundle["alerts"] for i in a["transaction_ids"] if i in txns]
    rules = {a["rule_id"] for a in bundle["alerts"]}

    # 1. Red flags: signs of crime win over any explanation.
    for d in bundle["evidence"]:
        ids = [d["doc_id"]] + alerted
        if d["type"] == "sending_bank_fraud_report":
            return decision("ESCALATE", "ESC_MULE_ACTIVITY", [
                f"{d['title']} ({d['doc_id']}) reports a scam: {sentence_with(d['text'], 'sent £')}",
                f"{name}'s account received victims' money and passed it on, so it goes to the MLRO."], ids)
        if d["type"] == "staff_note":
            code = ("ESC_STRUCTURING" if re.search(r"threshold|under £|below £", d["text"])
                    else "ESC_HIGH_RISK_CORRIDOR" if "TM06" in rules else "ESC_UNEXPLAINED_FUNDS")
            return decision("ESCALATE", code, [
                f"Staff note {d['doc_id']}: {sentence_with(d['text'], 'threshold|under £|below £|unwilling|refused')}",
                "This is a sign of crime, so the case is escalated rather than asking the customer."], ids)
        if d["type"] == "device_export":
            row = [c.strip() for c in d["text"].strip().splitlines()[-1].strip("|").split("|")]
            if "," in row[2] or row[3] != "United Kingdom":
                return decision("ESCALATE", "ESC_ACCOUNT_MISUSE", [
                    f"Device export {d['doc_id']} shows device {row[0]} logging in to customer account(s) "
                    f"{row[2]} from {row[3]}.",
                    f"Someone other than {name} appears to use the account."], ids)

    # 2. Check each alert. Each finding is (verdict or None if unexplained, code, sentence, ids).
    findings = [check_alert(bundle, a, txns, name) for a in bundle["alerts"]]
    ids = [i for f in findings for i in f[3]]
    for f in findings:
        if f[0] == "ESCALATE":
            return decision("ESCALATE", f[1], [f[2], "The case is escalated to the MLRO."], ids)
    for f in findings:
        if f[0] is None:
            return unsure(bundle, txns, name, f)
    for f in findings:
        if f[0] == "RFI":
            return decision("RFI", f[1], [f[2], "Nothing else on file is suspicious, so the customer is asked."],
                            ids, ["RFI", "ESCALATE"])  # Ivy checks whether anything else is concerning

    # 3. Every alert is explained. Policy guard: own-account transfers at 5x the profile or more -> RFI.
    multiple = max([a["measured"].get("multiple", 0) for a in bundle["alerts"] if a["rule_id"] == "TM01"] or [0])
    if any(f[1] == "OWN_ACCOUNT" for f in findings) and multiple >= 5:
        return decision("RFI", "RFI_UPDATED_PROFILE", [
            findings[0][2],
            f"These own-account transfers bring activity to {multiple:.1f} times {name}'s declared profile, "
            f"so ask {name} for an updated expected income or volume."], ids)
    sentences = [f[2] for f in findings][:3]
    return decision("CLOSE", findings[0][1], sentences + ["Every alert is explained by documents on file."], ids)


# ---------------------------------------------------------------- per alert
def check_alert(bundle, alert, txns, name):
    rule = alert["rule_id"]
    docs = bundle["evidence"]
    flagged = [txns[i] for i in alert["transaction_ids"] if i in txns]

    if rule == "TM22":
        return list_check(bundle, alert, flagged[0])

    if rule == "TM07":  # dormant account: was the returning customer verified?
        for d in docs:
            if d["type"] == "reverification_record":
                if re.search(r"fail|did not match|could not|unable", d["text"], re.I):
                    return ("ESCALATE", "ESC_ACCOUNT_MISUSE",
                            f"Re-verification {d['doc_id']} failed: {sentence_with(d['text'], 'fail|match|could not|unable')}",
                            [d["doc_id"]])
                return ("CLOSE", "RETURNING_CUSTOMER",
                        f"{name} was re-verified when the dormant account reactivated "
                        f"({d['doc_id']}): {sentence_with(d['text'], 're-verified')}", [d["doc_id"]])

    if rule == "TM10":  # early fund redemption: a documented reason to sell?
        for d in docs:
            if d["type"] == "redundancy_letter":
                return ("CLOSE", "DOCUMENTED_EVENT",
                        f"{name} sold fund units early after being made redundant ({d['doc_id']}).", [d["doc_id"]])

    # Every flagged transaction needs a document that explains it, except routine ones: alerts like
    # TM01 list all money in the window, including salary or bills: money with someone the customer
    # already dealt with the same way before the review window, and not much bigger than before.
    start = bundle["case"]["review_window"]["from"]
    earlier = [t for t in bundle["transactions"] if t["booked_at"][:10] < start]

    def routine(t):
        before = [u["amount_gbp"] for u in earlier
                  if u["counterparty_name"] == t["counterparty_name"] and u["direction"] == t["direction"]]
        return t["counterparty_name"] and before and t["amount_gbp"] <= 1.5 * max(before)

    flagged = [t for t in flagged if not routine(t)]
    if not flagged:
        return (None, UNEXPLAINED_CODE.get(rule, "ESC_UNEXPLAINED_FUNDS"),
                f"Only routine transactions were flagged by {rule}, which needs a closer look.",
                alert["transaction_ids"][:3])
    used = []
    for t in flagged:
        d = explaining_doc(bundle, t, name)
        if d is None:
            return (None, UNEXPLAINED_CODE.get(rule, "ESC_UNEXPLAINED_FUNDS"),
                    f"Nothing on file explains the {describe(t)} flagged by {rule}.", [t["transaction_id"]])
        if d not in used:
            used.append(d)
    titles = " and ".join(f"{d['title']} ({d['doc_id']})" for d in used)
    what = describe(flagged[0]) if len(flagged) == 1 else f"{len(flagged)} transactions flagged by {rule}"
    return ("CLOSE", CLOSE_CODE.get(used[0]["type"], "DOCUMENTED_EVENT"),
            f"The {what} match{'es' if len(flagged) == 1 else ''} {titles}.",
            [d["doc_id"] for d in used] + [t["transaction_id"] for t in flagged])


def explaining_doc(bundle, txn, name):
    """A document that explains a transaction, or None:
    - own account: money to/from the customer's own name, and a statement on file in their name
    - group company: a group chart linking the counterparty and the customer
    - otherwise: a document showing the same amount and naming the counterparty"""
    cp = txn["counterparty_name"]
    held = bundle["case"].get("held_payment_id") == txn["transaction_id"]
    for d in bundle["evidence"]:
        text = d["title"] + "\n" + d["text"]
        if d["type"] in NOT_PROOF:
            continue
        if d["type"] == "bank_statement":
            if cp == name and f"**Account holder:** {name}" in text:
                return d
            continue
        if d["type"] == "group_structure":
            owner = {}  # company -> owner, from the table rows "| company | owner | share |"
            for line in text.splitlines():
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if line.startswith("|") and len(cells) >= 2:
                    owner[cells[0]] = cells[1]
            if cp in owner and name in owner and (owner[cp] == owner[name] or owner[name] == cp or owner[cp] == name):
                return d
            continue
        if round(txn["amount_gbp"], 2) not in money(text):
            continue
        short = " ".join(cp.split()[:2]).lower() if cp else ""  # "Wenbrook Solicitors LLP client account"
        if cp and cp != name and short not in text.lower():
            continue  # the document is about someone else
        if held and txn["amount_gbp"] >= 10000 and cp != name and d["type"] not in INVOICE_TYPES:
            continue  # payments_fx: £10,000+ to a third party needs an invoice or contract
        return d
    return None


def list_check(bundle, alert, txn):
    """TM22: is the counterparty really the listed party? Compare the list entry with documents."""
    entry = next(e for e in bundle["list_entries"] if e["entry_id"] == alert["measured"]["list_entry"])
    listed, paid, cp = entry["name"], day(txn), txn["counterparty_name"]
    ids = [entry["entry_id"], txn["transaction_id"]]
    left_before = None
    for d in bundle["evidence"]:
        # a. The listed person in a register table: still in a role on the payment date?
        for line in d["text"].splitlines():
            if line.startswith(f"| {listed} |"):
                start_end = dates(line)  # [appointed/notified, resigned/ceased if any]
                if len(start_end) < 2 or start_end[1] >= paid:
                    return ("ESCALATE", "ESC_LISTED_PARTY",
                            f"{d['title']} ({d['doc_id']}) shows {listed}, who is on the internal list "
                            f"({entry['entry_id']}), still had a role in {cp} at the time of the {describe(txn)}.",
                            ids + [d["doc_id"]])
                left_before = d
        # b. A date of birth for the listed name: same person or not?
        m = re.search(re.escape(listed) + r", born (\d{1,2} [A-Z][a-z]+ \d{4})", d["text"])
        if m and entry.get("date_of_birth"):
            born = datetime.strptime(m.group(1), "%d %B %Y")
            if born.strftime("%Y-%m-%d") == entry["date_of_birth"]:
                return ("ESCALATE", "ESC_LISTED_PARTY",
                        f"{d['title']} ({d['doc_id']}) gives {listed}'s date of birth as {m.group(1)}, the same as "
                        f"internal list entry {entry['entry_id']}.", ids + [d["doc_id"]])
            if d["type"] == "gift_letter":  # written by the payer themselves: not independent proof
                return ("RFI", "RFI_COUNTERPARTY_DETAILS",
                        f"The {describe(txn)} matched internal list entry {entry['entry_id']} ({listed}, born "
                        f"{entry['date_of_birth']}) on name; only the payer's own letter ({d['doc_id']}) gives a "
                        f"different date of birth, {m.group(1)}. Ask for independent proof of {listed}'s identity, "
                        f"such as a passport or driving licence.", ids + [d["doc_id"]])
            return ("CLOSE", "LIST_MATCH_DIFFERENT_PARTY",
                    f"{listed} shares a name with internal list entry {entry['entry_id']}, but {d['title']} "
                    f"({d['doc_id']}) gives a date of birth of {m.group(1)}, not {entry['date_of_birth']}.",
                    ids + [d["doc_id"]])
    if left_before:
        return ("CLOSE", "LIST_MATCH_DIFFERENT_PARTY",
                f"{left_before['title']} ({left_before['doc_id']}) shows {listed} (internal list entry "
                f"{entry['entry_id']}) had left {cp} before the {describe(txn)}.", ids + [left_before["doc_id"]])
    # c. Address match: the counterparty's register shows a different office and none of the listed people.
    for d in bundle["evidence"]:
        if (d["type"] == "company_register_extract" and cp and cp in d["title"] and entry.get("address")
                and entry["address"] not in d["text"] and listed not in d["text"]):
            office = re.search(r"\*\*Registered office:\*\* (.+)", d["text"])
            return ("CLOSE", "LIST_MATCH_DIFFERENT_PARTY",
                    f"Internal list entry {entry['entry_id']} is {listed} at {entry['address']}, but "
                    f"{d['title']} ({d['doc_id']}) shows {cp} at {office.group(1) if office else 'another address'}"
                    f" with different officers.", ids + [d["doc_id"]])
    # d. Nothing settles it.
    return ("RFI", "RFI_COUNTERPARTY_DETAILS",
            f"The {describe(txn)} matched internal list entry {entry['entry_id']} ({listed}) on "
            f"{alert['measured']['matched_on']} only; nothing on file shows whether it is the same party. "
            f"Ask the customer who {cp} is and what the payment was for.", ids)


def unsure(bundle, txns, name, finding):
    """Something is unexplained: RFI or ESCALATE (Ivy decides). Best guess without an LLM: ESCALATE."""
    return decision("ESCALATE", finding[1], [finding[2], f"The rules engine cannot explain {name}'s "
                    "activity from the documents on file, so the case cannot be closed."],
                    finding[3], ["RFI", "ESCALATE"])
