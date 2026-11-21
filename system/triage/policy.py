"""Institution settings and the reason-code catalogue from the brief.

Thresholds here are the published policy settings (from the brief / each
POLICY.md), not anything learned from development cases.
"""
from __future__ import annotations

import os
import re

CLOSE_CODES = {
    "COLLECTION_DOCUMENTED", "BUSINESS_RECORDS_RECONCILE", "DOCUMENTED_EVENT",
    "INVOICE_MATCHES", "OWN_ACCOUNT", "GROUP_COMPANY", "THIRD_PARTY_DOCUMENTED",
    "REFUND_OF_EARLIER_PAYMENT", "RETURNING_CUSTOMER", "LIST_MATCH_DIFFERENT_PARTY",
    "WITHIN_PROFILE",
}
RFI_CODES = {
    "RFI_PURPOSE_DOCUMENTS", "RFI_BUSINESS_RECORDS", "RFI_SOURCE_OF_FUNDS",
    "RFI_COUNTERPARTY_DETAILS", "RFI_UPDATED_PROFILE",
}
ESC_CODES = {
    "ESC_MULE_ACTIVITY", "ESC_STRUCTURING", "ESC_UNEXPLAINED_FUNDS", "ESC_PASS_THROUGH",
    "ESC_INVOICE_MISMATCH", "ESC_HIGH_RISK_CORRIDOR", "ESC_ACCOUNT_MISUSE",
    "ESC_THIRD_PARTY_FUNDING", "ESC_EARLY_REDEMPTION", "ESC_LISTED_PARTY",
}
CODES = {"CLOSE": CLOSE_CODES, "RFI": RFI_CODES, "ESCALATE": ESC_CODES}
SEVERITY = {"CLOSE": 0, "RFI": 1, "ESCALATE": 2}

# Own-account transfers that push activity to this multiple of declared -> RFI.
OWN_ACCOUNT_MULTIPLE = 5.0

INSTITUTIONS = {
    "bank": dict(
        rules={"TM01", "TM02", "TM03", "TM04", "TM05", "TM06", "TM07", "TM22"},
        structuring_limit=10_000, structuring_band=0.10, cash_limit=10_000, cash_limit_business=25_000,
        many_parties=6, rapid_hours=48, invoice_required=None,
    ),
    "payments_fx": dict(
        rules={"TM01", "TM02", "TM03", "TM04", "TM06", "TM07", "TM08", "TM22"},
        structuring_limit=10_000, structuring_band=0.10, cash_limit=None,
        many_parties=25, rapid_hours=48, invoice_required=10_000,
    ),
    "emoney": dict(
        rules={"TM01", "TM02", "TM03", "TM04", "TM05", "TM06", "TM07", "TM22"},
        structuring_limit=1_000, structuring_band=0.20, cash_limit=1_000, cash_limit_business=5_000,
        many_parties=6, rapid_hours=48, invoice_required=None,
    ),
    "investment": dict(
        rules={"TM01", "TM02", "TM06", "TM07", "TM08", "TM10", "TM22"},
        structuring_limit=None, structuring_band=0.10, cash_limit=None,
        many_parties=None, rapid_hours=24 * 10, invoice_required=None, redemption_days=90,
    ),
}

# Fallback only; parse_high_risk() prefers the list in POLICY.md when present.
DEFAULT_HIGH_RISK = {
    "iran", "ir", "north korea", "dprk", "kp", "myanmar", "mm", "syria", "sy", "russia", "ru",
    "belarus", "by", "afghanistan", "af", "yemen", "ye", "south sudan", "ss", "venezuela", "ve",
    "haiti", "ht", "nigeria", "ng", "democratic republic of the congo", "cd", "mali", "ml",
    "lebanon", "lb", "monaco", "mc", "laos", "la", "algeria", "dz", "cameroon", "cm",
}


def detect_institution(folder: str, policy_text: str = "") -> str:
    name = os.path.basename(os.path.normpath(folder)).lower()
    for key, needles in (("payments_fx", ("fx", "payment")), ("emoney", ("emoney", "e-money", "e_money")),
                         ("investment", ("invest",)), ("bank", ("bank",))):
        if any(n in name for n in needles):
            return key
    head = policy_text[:2000].lower()
    if "fx" in head or "foreign exchange" in head:
        return "payments_fx"
    if "e-money" in head or "emoney" in head or "electronic money" in head:
        return "emoney"
    if "invest" in head or "fund" in head:
        return "investment"
    return "bank"


def parse_high_risk(policy_text: str) -> set[str]:
    """Pull a 'higher-risk countries: A, B, C' style list out of POLICY.md."""
    found: set[str] = set()
    for m in re.finditer(r"(higher|high)[- ]risk (countries|jurisdictions)[^\n:]*[:\n]+((?:[^\n]*\n?){1,12})",
                         policy_text, re.I):
        block = m.group(3)
        for line in block.splitlines():
            line = line.strip(" -*\t")
            if not line or line.startswith("#"):
                if found:
                    break
                continue
            for part in re.split(r"[,;]| and ", line):
                p = re.sub(r"\(.*?\)", "", part).strip(" .`*").lower()
                if 1 < len(p) < 40 and not any(w in p for w in ("countr", "list", "risk", "the ")):
                    found.add(p)
    return found or set(DEFAULT_HIGH_RISK)
