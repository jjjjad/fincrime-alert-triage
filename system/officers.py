"""The language-model officers: Oliver, Ivy and Daniel.

Owner: Tony (C). Each returns a decision (see common.make_decision), or None if there is no
API key or the call fails. main.py then falls back to Tom, so a missing LLM never loses a case.
No officer ever raises.

- oliver: independent three-way verdict. He sees Tom's findings as leads to verify, never Tom's verdict.
- ivy:    CLOSE ruled out. ESCALATE or RFI? ("Does anything here look criminal?")
- daniel: ESCALATE ruled out. RFI or CLOSE? ("Does the evidence on file explain every alert?")

Every answer is checked before it is returned: the verdict must be one the officer may give, the
reason code must fit it, the probabilities are normalised, evidence IDs that do not exist in the
case are dropped, and the reason is kept to 2-4 sentences.
"""
import json
import re

import llm
from common import REASON_CODES, VERDICTS, make_decision

# ---------------------------------------------------------------- prompt size limits
MAX_WINDOW_TXNS = 250    # transactions inside the review window (alerted ones are always kept)
MAX_HISTORY_TXNS = 60    # most recent transactions before the window (dormancy, refunds, usual pattern)
MAX_DOC_CHARS = 6000     # per document

# ---------------------------------------------------------------- prompts
ROLE = """You are a financial-crime analyst at a regulated firm, triaging transaction-monitoring alerts.
A case groups one or more alerts on one customer. You give ONE verdict for the whole case:

- CLOSE: the evidence on file explains EVERY alert in the case and fits what the firm knows about the customer.
- RFI: nothing is suspicious yet, and one specific document or answer from the customer would settle it.
- ESCALATE: the activity is unexplained or shows signs of crime. Once there is any real suspicion,
  escalate rather than ask the customer, because asking could tip them off.

How to work:
1. Take each alert in turn: which rule fired, what it measured, which transactions it fired on.
2. Look for a document, register entry, earlier review or declared-profile fact that explains exactly
   that activity. A document explains a payment only if the amount, the parties and the dates fit.
   Read the documents themselves; a title alone proves nothing.
3. Apply the institution's POLICY below exactly, including its limits and thresholds. Where the policy
   and your general knowledge differ, the policy wins.

Traps:
- A wrongful CLOSE is by far the worst mistake (cost 10; any other mistake costs 2 or 3). Close only
  when every alert is explained. One unexplained alert means the case cannot be closed.
- An earlier review covers only the activity it describes, never new, different or larger activity.
- A name match on the internal list is not proof. Compare date of birth, address and identifiers
  with the registers and documents on file.
- Round amounts, a shared device, IP or contact detail, a newly added recipient, or a crypto or
  gambling counterparty are context. None of them is a sign of crime on its own, and none of them
  is explained away by silence either.
- Use only what is in the case. Never invent a document, a name, an amount or an ID."""

ANSWER_FORMAT = """Reply with one JSON object and nothing else, with these keys in this order:
{"checks": [{"alert_id": "...", "explained": true or false, "by": "the ID that explains it, or what is missing"}],
 "verdict": "...",
 "reason_code": "...",
 "probs": {"CLOSE": p, "RFI": p, "ESCALATE": p},
 "reason": "...",
 "evidence": ["..."]}

- "checks": one entry for every alert in the case. Fill this in first; the verdict must follow from it.
- "probs": your honest probability that each verdict is the right one. They sum to 1. Put 0.9 or
  more on a verdict only when the policy and the evidence clearly settle it.
- "reason": 2 to 4 full sentences about THIS case. Name the deciding documents by ID, the people or
  firms, the amounts with currency and the dates. For CLOSE, say how each alert is explained. For RFI,
  say exactly which document or answer to ask the customer for. For ESCALATE, say what is unexplained
  or looks criminal. No generic wording that would fit any case, no bullet points, and no
  abbreviations that end in a full stop (write "Ltd", not "Ltd.").
- "evidence": the IDs the verdict rests on, copied exactly from the case: documents, transactions,
  earlier reviews or internal-list entries. Always include the deciding documents.
- "reason_code": the main ground for the verdict. Allowed codes and when to use each:
"""

# When to use each reason code (from the hackathon README).
CODE_MEANING = {
    "COLLECTION_DOCUMENTED": "a documented collection made on behalf of named people explains the payments",
    "BUSINESS_RECORDS_RECONCILE": "business records, such as takings or collection reports, reconcile to the activity",
    "DOCUMENTED_EVENT": "a documented one-off event, such as a sale, an inheritance or a payout, explains the funds",
    "INVOICE_MATCHES": "an invoice, contract or payment file matches the amount, payee and date",
    "OWN_ACCOUNT": "the money moved between accounts the customer is shown to hold",
    "GROUP_COMPANY": "the counterparty is a verified company in the same group",
    "THIRD_PARTY_DOCUMENTED": "a documented relationship explains a third-party payer",
    "REFUND_OF_EARLIER_PAYMENT": "the money is a refund of an earlier payment",
    "RETURNING_CUSTOMER": "a verified customer resumed using a dormant account",
    "LIST_MATCH_DIFFERENT_PARTY": "the evidence shows the counterparty is not the listed party",
    "WITHIN_PROFILE": "the activity fits the customer's declared profile or instructions",
    "RFI_PURPOSE_DOCUMENTS": "ask for documents showing what the payments were for",
    "RFI_BUSINESS_RECORDS": "ask for the business records behind the activity",
    "RFI_SOURCE_OF_FUNDS": "ask for proof of where the money came from",
    "RFI_COUNTERPARTY_DETAILS": "ask who the counterparty is and what the payment was for",
    "RFI_UPDATED_PROFILE": "ask for an updated expected income or volume",
    "ESC_MULE_ACTIVITY": "the account receives and passes on other people's money for someone else",
    "ESC_STRUCTURING": "transactions were split to stay under a threshold",
    "ESC_UNEXPLAINED_FUNDS": "the money has no credible explanation and was moved on",
    "ESC_PASS_THROUGH": "money passes through with no credible purpose, often to linked parties",
    "ESC_INVOICE_MISMATCH": "the invoices do not support the payments",
    "ESC_HIGH_RISK_CORRIDOR": "unexplained payments to or from a higher-risk country",
    "ESC_ACCOUNT_MISUSE": "someone other than the customer appears to control or use the account",
    "ESC_THIRD_PARTY_FUNDING": "unrelated third parties fund the account or receive money from it",
    "ESC_EARLY_REDEMPTION": "fund units are bought and quickly sold back to move money",
    "ESC_LISTED_PARTY": "the counterparty is the listed party or is controlled by it",
}

DEFAULT_CODE = {"CLOSE": "WITHIN_PROFILE", "RFI": "RFI_PURPOSE_DOCUMENTS", "ESCALATE": "ESC_UNEXPLAINED_FUNDS"}

OLIVER_TASK = """YOUR TASK: decide between CLOSE, RFI and ESCALATE.
You are the independent second pair of eyes. Reach your own verdict from the policy and the evidence.
If notes from the automated rules check are given, treat them as leads to verify against the
documents, not as a verdict: they can be incomplete or wrong.
Choose RFI over CLOSE whenever a document you would need is not on file. Choose ESCALATE over RFI
whenever there is a real sign of crime.
Always ESCALATE, never RFI (an RFI would tip the customer off), when there is: a fraud or scam report
from another bank or a victim; a mule pattern (many unrelated payers in, money passed on quickly to
one destination or withdrawn as cash, especially if the customer says they were paid to forward
money); third-party money that no document on file explains; large funds far above the declared
income that arrive (cash or third parties) and leave quickly; or customer explanations that
conflict with each other or with the documents. Asking such a customer for more detail is an RFI that tips them off.
RETURNING_CUSTOMER (dormant account reactivated) needs a document on file showing the customer was
re-verified or re-identified around the reactivation, and new activity that fits their profile. A
known payee or the customer's own name on a funding payment is not enough. If you cannot name that
document by ID, never use RETURNING_CUSTOMER: answer RFI."""

IVY_TASK = """YOUR TASK: CLOSE has been ruled out for this case. Decide between ESCALATE and RFI only.
Your one question: does anything here look criminal?
- ESCALATE if there is any real suspicion: the pattern fits a typology in the policy (for example
  money passed straight on, payments split under a limit, unrelated third parties funding the account,
  documents that contradict the payments, a true match to the internal list, someone else using the
  account), or the activity cannot be squared with what the customer declared. An RFI would tip them off.
- RFI only if nothing is suspicious and one specific, named document or answer from the customer
  would settle the case. Say exactly what to ask for.
If you cannot say what single document would settle it, ESCALATE.
Put probability 0 on CLOSE."""

DANIEL_TASK = """YOUR TASK: ESCALATE has been ruled out for this case. Decide between RFI and CLOSE only.
Your one question: does the evidence on file explain every alert?
- CLOSE only if each alert is explained by a specific document, register entry, earlier review or
  declared-profile fact that fits on amount, parties and dates, and the policy allows closing on it.
  Name what explains each alert.
- RFI if any alert is left without an explanation on file, or the policy asks for a document that
  is missing. Say exactly what to ask the customer for.
A wrongful CLOSE costs more than three times a needless RFI, so answer CLOSE only if you are at
least 80% sure. Put probability 0 on ESCALATE.
Never CLOSE if there is a fraud or scam report from another bank or a victim, a mule pattern (many
unrelated payers in, money passed on quickly to one destination or withdrawn as cash, especially if
the customer says they were paid to forward money), or third-party money that no document on file
explains. Answer RFI with a very low P(CLOSE) and name that sign in your reason.
RETURNING_CUSTOMER (dormant account reactivated) needs a document on file showing the customer was
re-verified or re-identified around the reactivation, and new activity that fits their profile. A
known payee or the customer's own name on a funding payment is not enough. If you cannot name that
document by ID, never use RETURNING_CUSTOMER: answer RFI."""


# ---------------------------------------------------------------- the case as text
def _cell(value):
    """One table cell: plain text for simple values, compact JSON for lists and dicts."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value).replace("\n", " ").replace("|", "/")


def _table(rows, alerted):
    """Transactions as a header line plus one line each, so field names are not repeated."""
    columns = []
    for row in rows:
        for key in row:
            if key != "customer_id" and key not in columns:
                columns.append(key)
    lines = ["alerted | " + " | ".join(columns)]
    for row in rows:
        mark = "ALERTED" if row.get("transaction_id") in alerted else ""
        lines.append(mark + " | " + " | ".join(_cell(row.get(c)) for c in columns))
    return "\n".join(lines)


def _compact(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def case_text(bundle):
    """The case as text for the prompt.

    All alerted transactions and those in the review window are included (up to MAX_WINDOW_TXNS),
    plus the most recent MAX_HISTORY_TXNS before the window, so the model can see dormancy, earlier
    payments that a refund relates to, and the customer's usual pattern.
    """
    window = bundle["case"].get("review_window") or {}
    start, end = str(window.get("from") or ""), str(window.get("to") or "9999")
    alerted = {t for a in bundle["alerts"] for t in a.get("transaction_ids") or []}

    def day(t):
        return str(t.get("booked_at") or "")[:10]

    txns = sorted(bundle["transactions"], key=lambda t: (str(t.get("booked_at") or ""), str(t.get("transaction_id"))))
    in_window = [t for t in txns if t.get("transaction_id") in alerted or start <= day(t) <= end]
    history = [t for t in txns if t.get("transaction_id") not in alerted and day(t) < start]
    later = sum(1 for t in txns if t.get("transaction_id") not in alerted and day(t) > end)

    dropped_window = 0
    if len(in_window) > MAX_WINDOW_TXNS:
        keep_alerted = [t for t in in_window if t.get("transaction_id") in alerted]
        others = [t for t in in_window if t.get("transaction_id") not in alerted]
        room = max(0, MAX_WINDOW_TXNS - len(keep_alerted))
        dropped_window = len(others) - room
        kept = keep_alerted + (others[-room:] if room else [])
        in_window = sorted(kept, key=lambda t: (str(t.get("booked_at") or ""), str(t.get("transaction_id"))))
    dropped_history = max(0, len(history) - MAX_HISTORY_TXNS)
    history = history[-MAX_HISTORY_TXNS:] if MAX_HISTORY_TXNS else []

    parts = [
        "INSTITUTION: " + str(bundle.get("institution")),
        "CASE: " + _compact(bundle["case"]),
        "CUSTOMER: " + _compact(bundle["customer"]),
        "ALERTS:\n" + "\n".join(_compact(a) for a in bundle["alerts"]),
    ]

    note = ""
    if dropped_window:
        note = f" ({dropped_window} older non-alerted transactions in the window not shown)"
    parts.append(f"TRANSACTIONS IN THE REVIEW WINDOW{note}:\n" + _table(in_window, alerted))
    if history:
        note = f" (the {len(history)} most recent; {dropped_history} older ones not shown)" if dropped_history else ""
        parts.append(f"EARLIER TRANSACTIONS, BEFORE THE WINDOW{note}:\n" + _table(history, alerted))
    else:
        parts.append("EARLIER TRANSACTIONS, BEFORE THE WINDOW: none on file")
    if later:
        parts.append(f"({later} transactions after the review window are not shown.)")

    reviews = bundle["previous_reviews"]
    parts.append("PREVIOUS REVIEWS:\n" + ("\n".join(_compact(r) for r in reviews) if reviews else "none"))
    entries = bundle["list_entries"]
    parts.append("INTERNAL LIST ENTRIES NAMED BY THE ALERTS:\n"
                 + ("\n".join(_compact(e) for e in entries) if entries else "none"))

    docs = bundle["evidence"]
    if not docs:
        parts.append("DOCUMENTS ON FILE: none")
    for doc in docs:
        meta = {k: v for k, v in doc.items() if k not in ("text", "file", "customer_id")}
        text = doc.get("text") or ""
        if len(text) > MAX_DOC_CHARS:
            text = text[:MAX_DOC_CHARS] + "\n[document cut here]"
        parts.append("--- DOCUMENT " + str(doc.get("doc_id")) + " " + _compact(meta) + " ---\n" + text.strip())
    return "\n\n".join(parts)


def _system_prompt(bundle, task, allowed):
    codes = "\n".join(f"  {v}: {code} = {CODE_MEANING.get(code, '')}" for v in allowed for code in REASON_CODES[v])
    return (ROLE + "\n\n=== POLICY (" + str(bundle.get("institution")) + ") ===\n" + bundle["policy"].strip()
            + "\n=== END OF POLICY ===\n\n" + task + "\n\n" + ANSWER_FORMAT + codes)


def _tom_findings(tom):
    """What Oliver may see of Tom: his findings and the IDs he relied on, never his verdict."""
    if not tom or len(tom.get("options") or []) != 1:  # Tom unsure: no findings to pass on
        return "NOTES FROM THE AUTOMATED RULES CHECK: no rule settled this case."
    return ("NOTES FROM THE AUTOMATED RULES CHECK (leads to verify, not a verdict): " + str(tom.get("reason"))
            + " IDs it relied on: " + ", ".join(str(i) for i in tom.get("evidence") or []) + ".")


def _earlier_views(tom, oliver_decision):
    """What Ivy and Daniel see: both earlier views, to weigh and check, not to copy."""
    lines = ["Two earlier reviewers looked at this case and it was not settled. "
             "Weigh their points against the documents; either can be wrong."]
    for name, d in (("Rules check", tom), ("First analyst", oliver_decision)):
        if d:
            lines.append(f"- {name}: {d.get('verdict')} / {d.get('reason_code')}. {d.get('reason')}")
    return "\n".join(lines)


# ---------------------------------------------------------------- checking the answer
SENTENCE_END = re.compile(r"[.!?](?=\s|$)")  # same count as tools/validate.py


def _fit_reason(reason, bundle, verdict):
    """Keep the reason to 2-4 sentences, as counted by the validator."""
    reason = " ".join(str(reason or "").split())
    if reason and reason[-1] not in ".!?":
        reason += "."
    ends = [m.end() for m in SENTENCE_END.finditer(reason)]
    if len(ends) > 4:
        reason = reason[:ends[3]]
    elif len(ends) < 2:
        rules = ", ".join(sorted({str(a.get("rule_id")) for a in bundle["alerts"]}))
        name = bundle["customer"].get("name") or "the customer"
        extra = f"This {verdict} verdict covers the alerts under {rules} on the account of {name}."
        reason = (reason + " " + extra).strip() if ends else extra + " No fuller reason was given by the reviewing officer."
    return reason


def _fit_probs(raw, verdict, allowed):
    """Probabilities over all three verdicts, 0 on any the officer may not give, summing to 1."""
    probs = {}
    for v in VERDICTS:
        try:
            p = float((raw or {}).get(v, 0))
        except (TypeError, ValueError):
            p = 0.0
        probs[v] = p if v in allowed and 0 <= p <= 1 else 0.0
    total = sum(probs.values())
    if total <= 0:  # nothing usable: a modest default that will not pass the 0.9 gate
        others = [v for v in allowed if v != verdict]
        probs = {v: 0.0 for v in VERDICTS}
        probs[verdict] = 0.7 if others else 1.0
        for v in others:
            probs[v] = 0.3 / len(others)
        return probs
    return {v: round(p / total, 4) for v, p in probs.items()}


def _fit_evidence(raw, reason, bundle):
    """Valid IDs only, in order, without duplicates. Adds valid IDs the reason names; never empty."""
    valid = bundle["valid_ids"]
    evidence = []
    candidates = [i.strip() for i in (raw if isinstance(raw, list) else []) if isinstance(i, str)]
    candidates += re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-]*", reason)
    for i in candidates:
        if i in valid and i not in evidence:
            evidence.append(i)
    if not evidence:  # fall back to what the alerts fired on
        for a in bundle["alerts"]:
            for i in a.get("transaction_ids") or []:
                if i in valid and i not in evidence:
                    evidence.append(i)
    if not evidence:
        evidence = list(bundle["case"].get("alert_ids") or [])
    return evidence


def _to_decision(answer, bundle, officer, allowed):
    """Turn the model's JSON into a decision, or None if the verdict is unusable."""
    verdict = str(answer.get("verdict") or "").strip().upper()
    if verdict not in allowed:
        return None
    code = str(answer.get("reason_code") or "").strip().upper()
    if code not in REASON_CODES[verdict]:
        code = DEFAULT_CODE[verdict]
    reason = _fit_reason(answer.get("reason"), bundle, verdict)
    return make_decision(verdict, code, reason, _fit_evidence(answer.get("evidence"), reason, bundle),
                         _fit_probs(answer.get("probs"), verdict, allowed), officer)


def _ask(bundle, officer, task, allowed, context):
    """Shared call for all three officers. Returns a decision, or None. Never raises."""
    try:
        if not llm.available():
            return None
        system_prompt = _system_prompt(bundle, task, allowed)
        user_prompt = context + "\n\n=== CASE ===\n" + case_text(bundle)
        answer = llm.ask_json(system_prompt, user_prompt, label=officer)
        return _to_decision(answer, bundle, officer, allowed)
    except Exception:
        return None


# ---------------------------------------------------------------- the officers
def oliver(bundle, tom):
    return _ask(bundle, "oliver", OLIVER_TASK, ["ESCALATE", "RFI", "CLOSE"], _tom_findings(tom))


def ivy(bundle, tom, oliver_decision):
    return _ask(bundle, "ivy", IVY_TASK, ["ESCALATE", "RFI"], _earlier_views(tom, oliver_decision))


def daniel(bundle, tom, oliver_decision):
    return _ask(bundle, "daniel", DANIEL_TASK, ["RFI", "CLOSE"], _earlier_views(tom, oliver_decision))
