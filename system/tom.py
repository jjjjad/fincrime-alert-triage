"""Officer Tom: the deterministic rules engine, written from each institution's POLICY.md.

Owner: Hamid (B).

decide(bundle) runs a list of checks in order. Each check looks at the bundle and either
returns a decision (see common.make_decision) or None if it doesn't apply. The first check
that returns a decision wins. If no check applies, Tom says he is unsure.

Tom's "probs" are his confidence. Only put a high probability (0.9 or more) on a verdict when a
policy rule clearly settles the case: the gate in main.py lets a confident Tom + Oliver
agreement stand without asking a specialist.
"""
from common import make_decision


# ---------------------------------------------------------------- checks
# Write one function per check, e.g. invoice_matches_payment(bundle), own_account_5x(bundle),
# list_match_different_party(bundle), payments_fx_10k_needs_invoice(bundle).
# Each returns make_decision(...) or None. Then add it to CHECKS below.
# bundle["institution"] tells you which POLICY.md applies, since limits differ per institution.

CHECKS = [
    # Signs of crime first (they must win over any explanation), then explanations, then RFIs.
]


# ---------------------------------------------------------------- entry point
def decide(bundle):
    for check in CHECKS:
        decision = check(bundle)
        if decision is not None:
            return decision
    return unsure(bundle)


def unsure(bundle):
    """No check applied. Tom splits his confidence between RFI and ESCALATE and never closes."""
    rules = ", ".join(sorted({a["rule_id"] for a in bundle["alerts"]}))
    name = bundle["customer"].get("name", "the customer")
    alerted_txns = [t for a in bundle["alerts"] for t in a.get("transaction_ids", [])]
    reason = (f"Rules {rules} fired on {name}'s account. "
              f"No check in the rules engine explains this activity, so it cannot be closed.")
    return make_decision("ESCALATE", "ESC_UNEXPLAINED_FUNDS", reason, alerted_txns,
                         {"CLOSE": 0.0, "RFI": 0.5, "ESCALATE": 0.5}, "tom")
