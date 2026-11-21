"""Shared definitions: the contract every officer follows.

Owner: Jad (A). Everyone imports from here. Tell the team before changing anything in this file.
"""

# The three verdicts, safest first. Where two verdicts tie, the code picks the earlier (safer) one.
VERDICTS = ["ESCALATE", "RFI", "CLOSE"]

# Reason codes allowed for each verdict (from the hackathon README).
REASON_CODES = {
    "CLOSE": [
        "COLLECTION_DOCUMENTED", "BUSINESS_RECORDS_RECONCILE", "DOCUMENTED_EVENT", "INVOICE_MATCHES",
        "OWN_ACCOUNT", "GROUP_COMPANY", "THIRD_PARTY_DOCUMENTED", "REFUND_OF_EARLIER_PAYMENT",
        "RETURNING_CUSTOMER", "LIST_MATCH_DIFFERENT_PARTY", "WITHIN_PROFILE",
    ],
    "RFI": [
        "RFI_PURPOSE_DOCUMENTS", "RFI_BUSINESS_RECORDS", "RFI_SOURCE_OF_FUNDS",
        "RFI_COUNTERPARTY_DETAILS", "RFI_UPDATED_PROFILE",
    ],
    "ESCALATE": [
        "ESC_MULE_ACTIVITY", "ESC_STRUCTURING", "ESC_UNEXPLAINED_FUNDS", "ESC_PASS_THROUGH",
        "ESC_INVOICE_MISMATCH", "ESC_HIGH_RISK_CORRIDOR", "ESC_ACCOUNT_MISUSE",
        "ESC_THIRD_PARTY_FUNDING", "ESC_EARLY_REDEMPTION", "ESC_LISTED_PARTY",
    ],
}

# Cost of each mistake, from the brief: COST[right verdict][verdict we sent].
COST = {
    "CLOSE":    {"CLOSE": 0,  "RFI": 3, "ESCALATE": 3},
    "RFI":      {"CLOSE": 10, "RFI": 0, "ESCALATE": 2},
    "ESCALATE": {"CLOSE": 10, "RFI": 3, "ESCALATE": 0},
}


def make_decision(verdict, reason_code, reason, evidence, probs, officer, options=None):
    """Build a decision dict. Every officer returns one of these.

    verdict:     "CLOSE", "RFI" or "ESCALATE" (for Tom when unsure: his best guess, used if no LLM)
    reason_code: one of REASON_CODES[verdict]
    reason:      2 to 4 sentences naming this case's own documents, people and amounts
    evidence:    list of IDs exactly as they appear in the files
    probs:       LLM officers: how likely each verdict is, e.g. {"CLOSE": 0.9, "RFI": 0.1, "ESCALATE": 0.0}.
                 Tom leaves it empty and uses `options` instead.
    officer:     who decided: "tom", "oliver", "ivy" or "daniel"
    options:     the verdicts still possible. Tom gives one of:
                 ["CLOSE"], ["CLOSE", "RFI"], ["RFI"], ["RFI", "ESCALATE"], ["ESCALATE"].
                 LLM officers leave it out (it defaults to [verdict]).
    """
    return {
        "verdict": verdict,
        "reason_code": reason_code,
        "reason": reason,
        "evidence": list(evidence),
        "probs": probs,
        "officer": officer,
        "options": options or [verdict],
    }


def expected_cost(probs, sent):
    """Average cost of sending `sent`, if the right answer follows the probabilities `probs`."""
    return sum(probs.get(right, 0.0) * COST[right][sent] for right in COST)


def cheapest_verdict(probs, allowed=VERDICTS):
    """The verdict with the lowest expected cost (not simply the most likely one).

    Example: with P(close) = 0.7, P(rfi) = 0.3, CLOSE costs 0.3 * 10 = 3.0 on average and
    RFI costs 0.7 * 3 = 2.1, so RFI is cheaper even though CLOSE is more likely.
    """
    return min(allowed, key=lambda v: expected_cost(probs, v))
