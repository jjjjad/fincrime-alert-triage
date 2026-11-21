"""Reading documents: what kind of document is it, and what does it mention."""
from __future__ import annotations

import re
from datetime import datetime

from .data import Doc, norm_name, pick, to_amount, to_dt

_KIND_PATTERNS: dict[str, list[str]] = {
    "invoice": [r"\binvoice\b", r"\bcontract\b", r"purchase order", r"payment file", r"pro ?forma",
                r"^inv[-_]", r"^con[-_]", r"^pf[-_]", r"bill of lading", r"statement of work"],
    "kyc": [r"\bkyc\b", r"due diligence", r"\bcdd\b", r"\bedd\b", r"identity verif", r"onboarding",
            r"^kyc[-_]", r"proof of address", r"re-?verif", r"refresh"],
    "own_account": [r"own account", r"account (is )?(held )?in the name of", r"bank statement",
                    r"account holder", r"joint account", r"savings account", r"^stmt[-_]",
                    r"^bst[-_]", r"isa transfer", r"pension transfer"],
    "group": [r"group structure", r"subsidiar", r"parent company", r"same group", r"holding company",
              r"intra-?group", r"group company", r"organisation chart", r"^grp[-_]"],
    "event": [r"completion statement", r"sale of (the )?(property|house|flat|car|vehicle|business)",
              r"inheritance", r"probate", r"grant of representation", r"\bwill\b", r"executor",
              r"redundancy", r"settlement agreement", r"payout", r"maturity", r"bonus letter",
              r"compensation", r"insurance (claim|settlement)", r"lottery", r"prize", r"divorce",
              r"gift letter", r"dividend", r"^evt[-_]"],
    "loan": [r"loan agreement", r"loan facility", r"director'?s loan", r"mortgage offer",
             r"^loan[-_]"],
    "business_records": [r"takings", r"\btill\b", r"z-?report", r"collection report", r"sales report",
                         r"sales ledger", r"management accounts", r"daily sales", r"cash book",
                         r"cashing[- ]up", r"^bus[-_]", r"^rec[-_]", r"payroll", r"wage"],
    "collection": [r"on behalf of", r"collection (for|of)", r"whip-?round", r"club (subs|fees)",
                   r"syndicate", r"fundrais", r"members'? contributions", r"^col[-_]", r"pooled"],
    "relationship": [r"\bspouse\b", r"\bwife\b", r"\bhusband\b", r"\bparent\b", r"\bfather\b",
                     r"\bmother\b", r"\bson\b", r"\bdaughter\b", r"\bsibling\b", r"\bbrother\b",
                     r"\bsister\b", r"\bpartner\b", r"relationship", r"family", r"guardian",
                     r"employer", r"^rel[-_]", r"third[- ]party (payer|funding) (declaration|form)"],
    "refund": [r"\brefund", r"\breturn(ed)? (of|payment)", r"chargeback", r"reversal",
               r"cancell?ed order", r"^ref[-_]"],
}

# Phrases that turn a case into suspicion on their own (escalate, never RFI:
# asking questions at this point risks tipping off).
_RED_FLAG_PATTERNS = [
    r"reported (as )?(a |the )?scam", r"scam (victim|report)", r"victim of (a )?(scam|fraud)",
    r"fraud report", r"recall request", r"authori[sz]ed push payment", r"\bapp fraud\b",
    r"funds? (were|was) (reported )?stolen", r"money mule", r"no (such )?orders? (exist|were|on)",
    r"there are no orders", r"could not (be )?verif", r"(invoice|document) (appears|is) (altered|forged|fake)",
    r"does not (exist|match) (at|on) companies house", r"law enforcement", r"production order",
    r"customer (refused|declined) to (explain|provide)",
]
_NEGATED = re.compile(r"\b(no|not|without|none|negative)\b[^.\n]{0,40}$", re.I)

_GBP_RE = re.compile(r"(?:£|gbp\s?)\s?(\d[\d,]*(?:\.\d{1,2})?)", re.I)
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})\b")
_DOB_RE = re.compile(r"(date of birth|d\.?o\.?b\.?|born)\s*[:\-]?\s*(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|\d{1,2} \w+ \d{4})",
                     re.I)
_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2})\b")


def kinds(doc: Doc) -> set[str]:
    b = doc.blob
    head = f"{doc.id} {doc.type} {doc.title}".lower()
    out = set()
    for k, pats in _KIND_PATTERNS.items():
        for p in pats:
            target = doc.id.lower() if p.startswith("^") else (b if k != "kyc" else head + " " + b[:600])
            if re.search(p, target):
                out.add(k)
                break
    t = doc.type.lower()
    for k in _KIND_PATTERNS:
        if k.replace("_", " ") in t or k in t:
            out.add(k)
    return out


def amounts(doc: Doc) -> list[float]:
    vals = [float(m.group(1).replace(",", "")) for m in _GBP_RE.finditer(doc.text + " " + doc.title)]
    for k in ("amount", "amount_gbp", "total", "value"):
        a = to_amount(doc.raw.get(k))
        if a:
            vals.append(a)
    return vals


def mentions_amount(doc: Doc, amt: float, tol: float = 0.01) -> bool:
    return any(abs(a - amt) <= max(1.0, amt * tol) for a in amounts(doc))


def mentions_name(doc: Doc, name: str) -> bool:
    n = norm_name(name)
    if not n:
        return False
    blob = norm_name(doc.blob + " " + str(pick(doc.raw, "counterparty", "payee", "party", default="")))
    if n in blob:
        return True
    toks = [t for t in n.split() if len(t) > 2]
    return len(toks) >= 2 and all(re.search(rf"\b{re.escape(t)}\b", blob) for t in toks)


def dates(doc: Doc) -> list[datetime]:
    out = []
    for m in _DATE_RE.finditer(doc.text):
        d = to_dt(m.group(1))
        if d:
            out.append(d)
    d = to_dt(pick(doc.raw, "date", "issued", "issue_date"))
    if d:
        out.append(d)
    return out


def near_date(doc: Doc, when: datetime | None, days: int = 60) -> bool:
    if when is None:
        return True
    ds = dates(doc)
    return not ds or any(abs((d - when).days) <= days for d in ds)


def red_flags(doc: Doc) -> list[str]:
    hits = []
    text = f"{doc.title}\n{doc.text}"
    for p in _RED_FLAG_PATTERNS:
        for m in re.finditer(p, text, re.I):
            before = text[max(0, m.start() - 60):m.start()]
            if _NEGATED.search(before.split(".")[-1]):
                continue
            hits.append(m.group(0))
            break
    return hits


def dob_values(text: str) -> list[datetime]:
    out = []
    for m in _DOB_RE.finditer(text):
        d = to_dt(m.group(2))
        if d is None:
            try:
                d = datetime.strptime(m.group(2), "%d %B %Y")
            except ValueError:
                d = None
        if d:
            out.append(d)
    return out


def postcodes(text: str) -> set[str]:
    return {p.replace(" ", "").upper() for p in _POSTCODE_RE.findall(text or "")}
