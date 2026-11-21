"""The language-model officers: Oliver, Ivy and Daniel.

Owner: Tony (C). Each returns a decision (see common.make_decision), or None if there is no
API key or the call fails. main.py then falls back to Tom, so a missing LLM never loses a case.

- oliver: independent three-way verdict, given Tom's findings as context.
- ivy:    CLOSE ruled out. ESCALATE or RFI? ("Does anything here look criminal?")
- daniel: ESCALATE ruled out. RFI or CLOSE? ("Does the evidence on file explain every alert?")

The prompts below are a first working version for Tony to improve.
"""
import json

import llm
from common import REASON_CODES, make_decision

RULES_OF_THE_TRACK = """You are a financial-crime analyst triaging transaction-monitoring alerts.
Give one verdict for the whole case:
- CLOSE: the evidence on file explains EVERY alert and fits what the institution knows about the customer.
- RFI: nothing is suspicious yet, and one specific document or answer from the customer would settle it.
- ESCALATE: the activity is unexplained or shows signs of crime. Once there is any suspicion, escalate
  rather than ask the customer (asking could tip them off).
A wrongful CLOSE is by far the worst mistake. Never close a case you are unsure about.
Follow the institution's POLICY below exactly."""

ANSWER_FORMAT = """Reply with JSON only:
{"verdict": "...", "reason_code": "...", "probs": {"CLOSE": p, "RFI": p, "ESCALATE": p},
 "reason": "2 to 4 sentences naming this case's own documents, people and amounts. For an RFI, say exactly what to ask for.",
 "evidence": ["IDs exactly as they appear in the case, e.g. document, transaction or list entry IDs"]}
Allowed reason codes: """


def case_text(bundle):
    """The case as text for the prompt. Transactions are limited to the review window and alerted ones."""
    window = bundle["case"].get("review_window") or {}
    alerted = {t for a in bundle["alerts"] for t in a.get("transaction_ids", [])}
    txns = [t for t in bundle["transactions"]
            if t["transaction_id"] in alerted
            or window.get("from", "") <= t["booked_at"][:10] <= window.get("to", "9999")]
    parts = {
        "case": bundle["case"],
        "customer": bundle["customer"],
        "alerts": bundle["alerts"],
        "transactions": txns,
        "previous_reviews": bundle["previous_reviews"],
        "internal_list_entries": bundle["list_entries"],
    }
    text = json.dumps(parts, indent=1, ensure_ascii=False)
    for doc in bundle["evidence"]:
        text += "\n\n--- Document " + doc["doc_id"] + " ---\n" + doc["text"]
    return text


def _ask(bundle, officer, task, allowed_verdicts, tom):
    """Shared prompt for all three officers. Returns a decision, or None."""
    if not llm.available():
        return None
    codes = {v: REASON_CODES[v] for v in allowed_verdicts}
    system_prompt = (RULES_OF_THE_TRACK + "\n\nPOLICY:\n" + bundle["policy"] + "\n\n"
                     + task + "\n\n" + ANSWER_FORMAT + json.dumps(codes))
    user_prompt = ("Rules engine (Officer Tom) said: " + tom["verdict"] + " / " + tom["reason_code"]
                   + ". " + tom["reason"] + "\n\nCASE:\n" + case_text(bundle))
    try:
        answer = llm.ask_json(system_prompt, user_prompt)
        verdict = answer["verdict"]
        if verdict not in allowed_verdicts:
            return None
        return make_decision(verdict, answer["reason_code"], answer["reason"],
                             answer.get("evidence", []), answer.get("probs", {}), officer)
    except Exception:
        return None


def oliver(bundle, tom):
    task = "Decide independently between CLOSE, RFI and ESCALATE. Check Tom's call; do not just copy it."
    return _ask(bundle, "oliver", task, ["CLOSE", "RFI", "ESCALATE"], tom)


def ivy(bundle, tom, oliver_decision):
    task = ("CLOSE has been ruled out. Decide between ESCALATE and RFI. "
            "Your key question: does anything here look criminal? If so, ESCALATE.")
    return _ask(bundle, "ivy", task, ["ESCALATE", "RFI"], tom)


def daniel(bundle, tom, oliver_decision):
    task = ("ESCALATE has been ruled out. Decide between RFI and CLOSE. "
            "Your key question: does the evidence on file explain every alert? Only then CLOSE.")
    return _ask(bundle, "daniel", task, ["RFI", "CLOSE"], tom)
