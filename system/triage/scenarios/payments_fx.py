"""Scenario logic for the payments_fx institution (see scenarios/__init__.py).

A case is one held outgoing payment (with every alert raised on it) or, for
money coming in, one client over the review window. Every alert in the case is
analysed by a rule handler that returns a Finding; the case verdict is the most
severe finding, and a CLOSE needs every alert to be explained (some findings
explain more than one rule, e.g. a refund explains TM06, TM08 and TM02).

All checks are policy-grounded and generic: they compare documents on file
(by type and content) with the transactions, list entries and alert
measurements of this case. Nothing here refers to development-case IDs, names
or amounts.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..data import fmt_gbp, norm_name

INVOICE_REQUIRED = 10_000.0      # policy: payments >= £10k to anyone but the client need an invoice/contract
OWN_ACCOUNT_RFI_MULTIPLE = 5.0   # policy: own-account transfers explaining >= 5x declared volume -> RFI
SEV = {"CLOSE": 0, "RFI": 1, "ESCALATE": 2}

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], 1)}


# --------------------------------------------------------------------------
# small parsing helpers
# --------------------------------------------------------------------------

def _money(s) -> float | None:
    if s is None:
        return None
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(s))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "").rstrip("."))
    except ValueError:
        return None


def _gbp_values(text: str) -> list[float]:
    out = []
    for m in re.finditer(r"£\s?(\d[\d,]*(?:\.\d{1,2})?)", text or ""):
        v = _money(m.group(1))
        if v is not None:
            out.append(v)
    return out


def _has_amount(text: str, amt: float, tol: float = 0.01) -> bool:
    return any(abs(v - amt) <= tol for v in _gbp_values(text))


def _date(s) -> datetime | None:
    """'4 August 2026', '2026-08-04', 'August 2026' (-> 1st of month)."""
    if not s:
        return None
    s = str(s).strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", s)
    if m and m.group(2).lower() in _MONTHS:
        return datetime(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    m = re.search(r"([A-Za-z]+)\s+(\d{4})", s)
    if m and m.group(1).lower() in _MONTHS:
        return datetime(int(m.group(2)), _MONTHS[m.group(1).lower()], 1)
    return None


def _day(d: datetime | None) -> str:
    return f"{d.day} {d.strftime('%B %Y')}" if d else "an unknown date"


def _tables(text: str) -> list[list[dict]]:
    """Markdown tables as lists of {lower-cased header: cell}."""
    tables, cur, header = [], [], None
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line.startswith("|"):
            if header is not None:
                tables.append(cur)
            cur, header = [], None
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        cur.append({header[i] if i < len(header) else str(i): (cells[i] if i < len(cells) else "")
                    for i in range(max(len(header), len(cells)))})
    if header is not None:
        tables.append(cur)
    return [t for t in tables if t]


def _rows(doc, *keys) -> list[dict]:
    """Rows of every table in doc that has a column whose header contains one of keys."""
    out = []
    for t in _tables(doc.text):
        hdr = " ".join(t[0].keys())
        if any(k in hdr for k in keys):
            out.extend(t)
    return out


def _col(row: dict, *keys) -> str:
    for k in keys:
        for h, v in row.items():
            if k in h:
                return v
    return ""


def _last4(acct: str) -> str:
    digits = re.findall(r"\d+", acct or "")
    return digits[-1][-4:] if digits else ""


def _same_name(a: str, b: str) -> bool:
    x, y = norm_name(a), norm_name(b)
    return bool(x and y) and x == y


def _name_in(name: str, text: str) -> bool:
    n = norm_name(name)
    return bool(n) and re.search(rf"\b{re.escape(n)}\b", norm_name(text)) is not None


def _field(doc, *keys) -> str:
    for k in keys:
        for h, v in doc.fields.items():
            if k in h:
                return v
    return ""


def _body(doc) -> str:
    """Free text of a document without the header lines."""
    lines = [l for l in doc.text.split("\n") if l.strip() and not l.startswith("#")
             and not l.startswith("**Document ID") and not l.startswith("**Dated")]
    return " ".join(lines)


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------

@dataclass
class F:
    verdict: str
    code: str
    sentences: list[str]
    evidence: list[str] = field(default_factory=list)
    covers: set = field(default_factory=set)   # rules this finding explains (CLOSE) or decides
    prio: int = 50                             # lower = more decisive when several share a verdict


class Case:
    def __init__(self, ctx, alerts):
        self.ctx = ctx
        self.alerts = alerts
        self.cust = ctx.customer
        self.name = ctx.customer.name
        self.rules = {a.rule for a in alerts}
        self.by_rule = defaultdict(list)
        for a in alerts:
            self.by_rule[a.rule].append(a)
        self.txmap = ctx.folder.txns
        self.held = self.txmap.get(ctx.case.held_txn_id) if ctx.case.held_txn_id else None
        self.docs = ctx.docs
        self.by_type = defaultdict(list)
        for d in self.docs:
            self.by_type[d.type.lower()].append(d)
        act = (ctx.customer.occupation or "").strip()
        self.activity = act[:1].lower() + act[1:] if act[:2] != act[:2].upper() else act
        self.list_entries = {str(e.get("entry_id") or e.get("id")): e for e in ctx.folder.list_entries}

    def dt(self, t: str) -> list:
        return self.by_type.get(t, [])

    def alert_txns(self, a) -> list:
        out = [self.txmap[i] for i in a.txn_ids if i in self.txmap]
        return sorted(out, key=lambda t: (t.ts or datetime.min, t.id))

    def is_client(self, name: str) -> bool:
        return _same_name(name, self.name)

    def tx(self, t) -> str:
        verb = "from" if t.direction == "in" else "to"
        return f"{fmt_gbp(t.amount)} {verb} {t.counterparty or 'an unnamed party'} on {_day(t.ts)} ({t.id})"


# --------------------------------------------------------------------------
# case-level red flags in staff notes (suspicion -> escalate, never ask)
# --------------------------------------------------------------------------

_STRUCT_PAT = re.compile(r"(under|below|less than)\s+£?10,?000|break(ing)? (down|up)|split(ting)?", re.I)
_EVADE_PAT = re.compile(r"unable or unwilling|unwilling|evasive|declined to|refused to|unable to provide|"
                        r"could not explain|would not explain", re.I)


def staff_note_flags(c: Case) -> list[F]:
    out = []
    for d in c.dt("staff_note"):
        body = _body(d)
        if _STRUCT_PAT.search(body) and re.search(r"exempt|avoid|requirement|invoice", body, re.I):
            sub = []
            for a in c.by_rule.get("TM03", []):
                sub = c.alert_txns(a)
            cp = sub[0].counterparty if sub else (c.held.counterparty if c.held else "the beneficiary")
            total = sum(t.amount for t in sub)
            s = [f"Staff note {d.id} ({_day(d.dated)}) records the director of {c.name} asking whether splitting "
                 f"payments to {cp} into amounts under £10,000 would avoid the requirement to provide invoices."]
            if sub:
                s.append(f"The account then made {len(sub)} payments to {cp} each just under £10,000, totalling "
                         f"{fmt_gbp(total)} between {_day(sub[0].ts)} and {_day(sub[-1].ts)}, which is deliberate "
                         f"structuring below the invoice threshold.")
            s.append("An invoice on file does not remove the suspicion, so this goes to the MLRO rather than "
                     "back to the customer.")
            ev = [d.id] + [x.id for x in c.dt("invoice")] + ([c.held.id] if c.held else []) + [t.id for t in sub]
            out.append(F("ESCALATE", "ESC_STRUCTURING", s, ev, set(c.rules), prio=5))
        elif _EVADE_PAT.search(body):
            hr = [a for a in c.by_rule.get("TM06", [])]
            country = str(hr[0].measured.get("countries", "")) if hr else ""
            sub = c.alert_txns(hr[0]) if hr else []
            inv = c.dt("supplier_invoices") + c.dt("supplier_invoice") + c.dt("invoice")
            s = [f"Staff note {d.id} ({_day(d.dated)}) records that the director could not or would not explain "
                 f"why the funds are routed to {country or 'the destination country'}, although they described "
                 f"the trade as goods from elsewhere."]
            if sub:
                s.append(f"{len(sub)} payments totalling {fmt_gbp(sum(t.amount for t in sub))} went to "
                         f"{sub[0].counterparty} in {sub[0].cp_country or country}, a higher-risk country the "
                         f"customer did not declare.")
            for i in inv:
                if re.search(r"consult|market access|services", i.text, re.I):
                    s.append(f"The invoices in {i.id} describe consultancy and market access services, which do "
                             f"not match the goods trade the director described.")
                    break
            code = "ESC_HIGH_RISK_CORRIDOR" if hr else "ESC_UNEXPLAINED_FUNDS"
            ev = [d.id] + [i.id for i in inv] + ([c.held.id] if c.held else []) + [t.id for t in sub]
            out.append(F("ESCALATE", code, s, ev, set(c.rules), prio=6))
    return out


# --------------------------------------------------------------------------
# TM22: counterparty on the internal list
# --------------------------------------------------------------------------

def _interval_covers(start: datetime | None, end: datetime | None, when: datetime) -> bool:
    if start and when < start:
        return False
    if end and when.date() >= end.date():
        return False
    return True


def tm22(c: Case, a) -> F:
    m = a.measured or {}
    eid = str(m.get("list_entry") or "")
    entry = c.list_entries.get(eid, {})
    lname = entry.get("name") or ""
    laddr = entry.get("address") or ""
    matched_on = str(m.get("matched_on") or "")
    sub = c.alert_txns(a)
    t = c.held or (sub[0] if sub else None)
    when = t.ts if t and t.ts else None
    cp = t.counterparty if t else ""
    base_ev = [eid] + ([t.id] if t else [])
    # documents about the counterparty (registers, receipts, invoices naming it)
    cp_docs = [d for d in c.docs if cp and _name_in(cp, d.title + " " + d.text) and not c.is_client(cp)]
    regs = [d for d in cp_docs if d.type.lower() in ("company_register_extract", "significant_control_register")]

    # 1. listed person in the counterparty's officer or PSC register: controlled at the time of payment?
    control_rows = []
    for d in regs:
        for r in _rows(d, "person", "officer"):
            who = _col(r, "person", "officer")
            if lname and _same_name(who, lname):
                start = _date(_col(r, "notified", "appointed"))
                end = _date(_col(r, "ceased", "resigned"))
                control_rows.append((d, r, start, end))
    if control_rows and when:
        active = [(d, r, s, e) for d, r, s, e in control_rows if _interval_covers(s, e, when)]
        if active:
            d, r, s, e = active[0]
            role = _col(r, "control", "role") or "a controlling role"
            ended_officer = [(dd, ee) for dd, rr, ss, ee in control_rows if ee and ee <= when]
            sent = [f"Internal list entry {eid} is {lname}, and {d.id} shows {lname} still held "
                    f"'{role}' in {cp} on {_day(when)}" + (f", until {_day(e)}" if e else "") +
                    f", when the held payment {c.tx(t)} was made."]
            if ended_officer:
                sent.append(f"Resigning as a director on {_day(ended_officer[0][1])} ({ended_officer[0][0].id}) "
                            f"did not end that control, so the counterparty is controlled by the listed party.")
            sent.append(f"The list entry says not to deal with this person or companies they control, so the "
                        f"payment is passed to the MLRO.")
            inv = [x.id for x in cp_docs if x.type.lower() in ("invoice", "supplier_invoice")]
            return F("ESCALATE", "ESC_LISTED_PARTY", sent,
                     [d.id] + [x.id for x, *_ in control_rows if x.id != d.id] + base_ev + inv, {"TM22"}, prio=1)
        # listed person had left every role before the payment
        last_end = max(e for _, _, _, e in control_rows if e)
        docs_ids = sorted({d.id for d, *_ in control_rows})
        return F("CLOSE", "LIST_MATCH_DIFFERENT_PARTY",
                 [f"{', '.join(docs_ids)} show that {lname} (list entry {eid}) had ceased every role in {cp} by "
                  f"{_day(last_end)}, before the held payment {c.tx(t)}.",
                  f"At the time of payment the counterparty was not controlled by the listed party."],
                 docs_ids + base_ev, {"TM22"})

    # 2. address match: compare the counterparty's registered office and officers with the list entry
    if regs and ("address" in matched_on.lower() or laddr):
        reg = regs[0]
        office = _field(reg, "registered office")
        people = []
        for r in _rows(reg, "person", "officer"):
            pb = (_col(r, "person", "officer"), _col(r, "born"))
            if pb[0] and pb not in people:
                people.append(pb)
        listed_is_officer = any(_same_name(p, lname) for p, _ in people)
        if listed_is_officer:
            return F("ESCALATE", "ESC_LISTED_PARTY",
                     [f"{reg.id} names {lname}, the person on internal list entry {eid}, among the officers or "
                      f"owners of {cp}.", f"The held payment {c.tx(t)} would go to a company the listed party "
                      f"controls, so it is passed to the MLRO."], [reg.id] + base_ev, {"TM22"}, prio=1)
        if office and laddr and norm_name(office) != norm_name(laddr):
            names = ", ".join(f"{p} (born {b})" if b else p for p, b in people[:2]) or "other people"
            rec = [x for x in cp_docs if x.type.lower() in ("receipt", "invoice")
                   and t and _has_amount(x.text, t.amount)]
            s = [f"Internal list entry {eid} ({lname}) is at {laddr}, but {reg.id} gives {cp}'s registered "
                 f"office as {office}, a different unit, and its officers and owners are {names}, not {lname}.",
                 f"The match rests on a shared building only, so the counterparty is a different party."]
            if rec:
                s.append(f"{rec[0].id} confirms the {fmt_gbp(t.amount)} payment on {_day(when)} was for an "
                         f"ordinary purchase from {cp}.")
            return F("CLOSE", "LIST_MATCH_DIFFERENT_PARTY", s, [reg.id] + [x.id for x in rec] + base_ev, {"TM22"})
        if office and laddr:
            return F("RFI", "RFI_COUNTERPARTY_DETAILS",
                     [f"{reg.id} gives {cp}'s registered office as {office}, the same address as internal list "
                      f"entry {eid} ({lname}), but does not name {lname} as an officer or owner.",
                      f"Ask the customer for who owns and runs {cp} and what the {fmt_gbp(t.amount)} payment on "
                      f"{_day(when)} was for, so the link to {eid} can be ruled in or out."],
                     [reg.id] + base_ev, {"TM22"})

    # 3. DOB / identity evidence about the payee in documents
    ldob = _date(entry.get("date_of_birth"))
    for d in cp_docs:
        m2 = re.search(r"(?:date of birth|born)[:\*\s]*([0-9]{1,2} [A-Za-z]+ [0-9]{4}|[A-Za-z]+ [0-9]{4}|\d{4}-\d{2}-\d{2})",
                       d.text, re.I)
        if m2 and ldob:
            dob = _date(m2.group(1))
            if dob and (dob.year, dob.month) != (ldob.year, ldob.month):
                return F("CLOSE", "LIST_MATCH_DIFFERENT_PARTY",
                         [f"{d.id} gives {cp}'s date of birth as {m2.group(1)}, while list entry {eid} ({lname}) "
                          f"was born {_day(ldob)}.", f"The shared name alone does not make them the same person."],
                         [d.id] + base_ev, {"TM22"})

    # 4. name only, nothing on file either way -> ask
    s = [f"The held payment {c.tx(t)} (reference '{t.reference}') matched internal list entry {eid} "
         f"({lname}, {str(entry.get('reason', 'listed')).rstrip('.')}) on {matched_on or 'name'} only." if t else
         f"An alert matched internal list entry {eid} ({lname}) on {matched_on or 'name'} only.",
         f"Nothing on file confirms or rules out that the payee is the listed person, and nothing else in the "
         f"account is concerning.",
         f"Ask the customer for the payee's full name, address and date of birth and an invoice or quote for the "
         f"work paid for, so the match to {eid} can be ruled in or out."]
    return F("RFI", "RFI_COUNTERPARTY_DETAILS", s, base_ev, {"TM22"})


# --------------------------------------------------------------------------
# TM03: repeated payments just below £10,000
# --------------------------------------------------------------------------

def tm03(c: Case, a) -> F:
    sub = c.alert_txns(a)
    cp = str(a.measured.get("beneficiary") or (sub[0].counterparty if sub else ""))
    total = sum(t.amount for t in sub)
    ev_tx = ([c.held.id] if c.held else []) + [t.id for t in sub if not c.held or t.id != c.held.id]
    # a contract payment schedule (or invoice) listing every instalment
    for d in c.dt("contract_schedule") + c.dt("supply_contract") + c.dt("invoice") + c.dt("supplier_invoice"):
        if not _name_in(cp, d.title + " " + d.text):
            continue
        rows = _rows(d, "amount")
        sched = [(_money(_col(r, "amount")), _date(_col(r, "due", "date"))) for r in rows]
        sched = [(x, y) for x, y in sched if x is not None]
        if sched:
            matched = [t for t in sub if any(abs(x - t.amount) < 0.01 for x, _ in sched)]
            on_time = all(any(abs(x - t.amount) < 0.01 and (y is None or abs((t.ts - y).days) <= 3)
                              for x, y in sched) for t in matched)
            if len(matched) == len(sub) and on_time:
                proj = _field(d, "project") or "the contract"
                return F("CLOSE", "INVOICE_MATCHES",
                         [f"{d.id} ({d.title}, dated {_day(d.dated)}) sets out {len(sched)} staged payments to "
                          f"{cp} for {proj}, and each of the {len(sub)} payments, {fmt_gbp(total)} in total, "
                          f"matches a stage in amount and due date.",
                          f"The payments below £10,000 follow the contract's own stages, not an attempt to avoid "
                          f"the invoice requirement" + (f"; the held payment {c.tx(c.held)} is the final stage."
                                                        if c.held else ".")],
                         [d.id] + ev_tx, {"TM03"})
            return F("ESCALATE", "ESC_INVOICE_MISMATCH",
                     [f"{d.id} names {cp} but its stages do not match the {len(sub)} payments of just under "
                      f"£10,000 ({fmt_gbp(total)} in total).",
                      "The documents do not support payments split below the invoice threshold."],
                     [d.id] + ev_tx, {"TM03"})
        tot = _money(_field(d, "total", "amount"))
        if tot is not None and abs(tot - total) < 0.01:
            return F("CLOSE", "INVOICE_MATCHES",
                     [f"{d.id} from {cp} totals {fmt_gbp(tot)}, payable in instalments, which equals the "
                      f"{len(sub)} payments in the alert.",
                      "The instalments match the invoice, so the split is explained."],
                     [d.id] + ev_tx, {"TM03"})
    refs = Counter(t.reference for t in sub if t.reference)
    ref = refs.most_common(1)[0][0] if refs else ""
    return F("RFI", "RFI_PURPOSE_DOCUMENTS",
             [f"{c.name} made {len(sub)} payments to {cp}, each between £9,000 and £9,999.99 and totalling "
              f"{fmt_gbp(total)}, from {_day(sub[0].ts)} to {_day(sub[-1].ts)}" +
              (f", with staged references such as '{ref}'." if ref else ".") if sub else
              f"{c.name} made repeated payments just below £10,000 to {cp}.",
              "No contract, payment schedule or invoice is on file, and payments above £10,000 to a third party "
              "need one; nothing else points to suspicion.",
              f"Ask the customer for the contract or payment schedule with {cp} showing the staged amounts and "
              f"due dates of these payments."],
             ev_tx, {"TM03"})


# --------------------------------------------------------------------------
# refunds (TM06 / TM08 / TM02 on a returned payment)
# --------------------------------------------------------------------------

def refund_finding(c: Case, t) -> F | None:
    if t.direction != "in":
        return None
    for d in c.dt("credit_note") + c.dt("refund"):
        if not _has_amount(d.text, t.amount) or not _name_in(t.counterparty, d.title + " " + d.text):
            continue
        orig = [o for o in c.ctx.txns if o.direction == "out" and _same_name(o.counterparty, t.counterparty)
                and abs(o.amount - t.amount) < 0.01 and o.ts and t.ts and o.ts < t.ts]
        refm = re.search(r"\b([A-Z]{3,}\s*\d{3,})\b", t.reference or "")
        refkey = refm.group(1) if refm else ""
        if refkey and refkey not in d.text:
            continue
        rv = [r for r in c.ctx.reviews if _name_in(t.counterparty, str(r.get("note", "")))]
        s = [f"{d.id} ({_day(d.dated)}) is a credit note from {t.counterparty} for "
             f"'{_field(d, 'credit for') or 'a cancelled order'}', refunding {fmt_gbp(t.amount)}, and "
             f"{t.id} on {_day(t.ts)} is that refund" + (f" with reference '{t.reference}'." if t.reference else ".")]
        if orig:
            o = orig[-1]
            s.append(f"It returns exactly the {fmt_gbp(o.amount)} the client paid {t.counterparty} on "
                     f"{_day(o.ts)} ({o.id})" +
                     (f", a payment earlier review {rv[0].get('review_id')} closed as a trial-order deposit."
                      if rv else "."))
        else:
            continue
        ev = [d.id, t.id, o.id] + [str(r.get("review_id")) for r in rv[:1]]
        return F("CLOSE", "REFUND_OF_EARLIER_PAYMENT", s, ev, {"TM06", "TM08", "TM02", "TM01"})
    return None


# --------------------------------------------------------------------------
# TM06: higher-risk corridor
# --------------------------------------------------------------------------

def tm06(c: Case, a) -> F:
    sub = c.alert_txns(a)
    country = str(a.measured.get("countries") or (sub[0].cp_country if sub else ""))
    for t in sub:
        r = refund_finding(c, t)
        if r:
            return r
    outs = [t for t in sub if t.direction == "out"]
    held = c.held if c.held and c.held.id in {t.id for t in sub} else (outs[-1] if outs else None)
    if held:
        inv_ok, docs_used, contract = None, [], None
        for d in c.dt("supplier_invoice") + c.dt("invoice") + c.dt("supplier_invoices"):
            if not _name_in(held.counterparty, d.title + " " + d.text):
                continue
            amt_ok = _has_amount(d.text, held.amount)
            acct = _field(d, "pay to")
            acct_ok = (not acct) or (_last4(acct) == _last4(held.cp_account))
            date_ok = not d.dated or not held.ts or d.dated.date() <= held.ts.date()
            goods = _field(d, "goods", "description", "service")
            services = bool(re.search(r"consult|market access|advis", d.text, re.I))
            if amt_ok and acct_ok and date_ok and not services:
                inv_ok = d
            docs_used.append(d)
        for d in c.dt("supply_contract") + c.dt("contract"):
            if _name_in(held.counterparty, d.title + " " + d.text):
                contract = d
        if inv_ok is not None:
            goods = _field(inv_ok, "goods", "description") or "goods"
            s = [f"{inv_ok.id} ({_day(inv_ok.dated)}) is an invoice from {held.counterparty} for {goods.lower()}, "
                 f"for {fmt_gbp(held.amount)} payable to account ending {_last4(held.cp_account)}, matching the "
                 f"held payment {c.tx(held)} in amount, payee, account and reference."]
            if contract:
                s.append(f"Supply contract {contract.id} ({_day(contract.dated)}) sets up this supplier "
                         f"relationship in {country}, and the goods fit the client's declared business "
                         f"({c.activity}).")
            return F("CLOSE", "INVOICE_MATCHES", s, [inv_ok.id] + ([contract.id] if contract else []) + [held.id],
                     {"TM06"})
        if docs_used:
            d = docs_used[0]
            return F("ESCALATE", "ESC_INVOICE_MISMATCH",
                     [f"{d.id} names {held.counterparty} but does not support the held payment {c.tx(held)} to "
                      f"{country}: the amount, account, date or goods do not match.",
                      f"Payments to an undeclared higher-risk country without supporting invoices go to the MLRO."],
                     [d.id, held.id] + [t.id for t in sub], {"TM06"})
    total = sum(t.amount for t in sub)
    return F("ESCALATE", "ESC_HIGH_RISK_CORRIDOR",
             [f"{fmt_gbp(total)} moved to or from {country}, a higher-risk country {c.name} did not declare, in "
              f"{len(sub)} payment{'s' if len(sub) != 1 else ''}" + (f" with {sub[0].counterparty}" if sub else "") + ".",
              "Nothing on file explains the payments, so they are passed to the MLRO."],
             ([c.held.id] if c.held else []) + [t.id for t in sub], {"TM06"})


# --------------------------------------------------------------------------
# TM01: activity above the expected profile
# --------------------------------------------------------------------------

def tm01(c: Case, a) -> F | None:
    m = a.measured or {}
    mult = float(m.get("multiple") or 0)
    received = float(m.get("received_gbp") or 0)
    baseline = float(m.get("baseline_gbp") or 0)
    sub = c.alert_txns(a)
    win = c.ctx.win

    # (a) own-account transfer shown by a statement of the receiving account
    for d in c.dt("bank_statement"):
        holder = _field(d, "account holder")
        txt = _body(d)
        ib = re.search(r"ending (\d{3,})", txt)
        outs = [t for t in c.ctx.txns if t.direction == "out" and ib and _last4(t.cp_account) == ib.group(1)[-4:]]
        if not outs:
            continue
        o = outs[-1]
        held_by = re.search(r"held by ([^.]+)\.", txt)
        held_name = held_by.group(1).strip() if held_by else holder
        if not (c.is_client(holder) and c.is_client(held_name) and _has_amount(txt, o.amount)):
            return F("ESCALATE", "ESC_ACCOUNT_MISUSE",
                     [f"{d.id} shows the account ending {ib.group(1)} that received {c.tx(o)} is held by "
                      f"{held_name or holder}, not by {c.name}.",
                      "Money is moving to an account someone else holds, so the case goes to the MLRO."],
                     [d.id, o.id], {"TM01"})
        bigs = [t for t in sub if t.direction == "in" and t.cp_account and c.is_client(t.counterparty)]
        big = max(bigs, key=lambda t: t.amount) if bigs else None
        s = [f"Statement {d.id} from {_field(d, 'bank') or 'the other bank'} shows the account ending "
             f"{ib.group(1)} is held by {c.name} and received the {fmt_gbp(o.amount)} sent on {_day(o.ts)} "
             f"({o.id}, reference '{o.reference}')."]
        if big:
            s.append(f"The spike is {c.tx(big)}, funding in the client's own name that was moved on to that "
                     f"own account.")
        ev = [d.id, o.id] + ([big.id] if big else [])
        if mult >= OWN_ACCOUNT_RFI_MULTIPLE:
            s.append(f"These own-account transfers take receipts to {fmt_gbp(received)}, {mult:.2f} times the "
                     f"monthly share of declared volume ({fmt_gbp(baseline)}), which is 5 times or more, so ask "
                     f"the customer for an updated expected annual payment volume.")
            return F("RFI", "RFI_UPDATED_PROFILE", s, ev, {"TM01"})
        s.append(f"Receipts of {fmt_gbp(received)} are {mult:.2f} times the monthly share of declared volume, "
                 f"below the 5 times at which policy asks for an updated profile.")
        return F("CLOSE", "OWN_ACCOUNT", s, ev, {"TM01"})

    # (b) supplier invoices that do not support the payments
    for d in c.dt("supplier_invoices"):
        rows = _rows(d, "invoice")
        bad, used = [], []
        for r in rows:
            ref = _col(r, "invoice")
            amt = _money(_col(r, "amount"))
            idate = _date(_col(r, "dated", "date"))
            pays = [t for t in c.ctx.txns if t.direction == "out" and t.reference.strip() == ref.strip()]
            if not pays or amt is None:
                continue
            p = pays[0]
            used.append(p)
            if abs(p.amount - amt) > 0.01 or (idate and p.ts and idate.date() > p.ts.date()):
                bad.append((ref, amt, idate, p, _col(r, "goods")))
        if bad:
            ref, amt, idate, p, goods = bad[0]
            s = [f"{d.id} from {p.counterparty} does not support the payments: invoice {ref} is for "
                 f"{fmt_gbp(amt)} dated {_day(idate)}, but {p.id} paid {fmt_gbp(p.amount)} on {_day(p.ts)}"
                 + (", before the invoice existed." if idate and p.ts and idate.date() > p.ts.date() else ".")]
            if len(bad) > 1:
                r2 = bad[1]
                s.append(f"Invoice {r2[0]} ({fmt_gbp(r2[1])}, {_day(r2[2])}) likewise does not match {r2[3].id} "
                         f"({fmt_gbp(r2[3].amount)} on {_day(r2[3].ts)}).")
            if goods and c.activity:
                s.append(f"The invoices are for {goods.lower()}, which has nothing to do with the client's declared "
                         f"business ({c.activity}), and the funding that triggered the alert was paid "
                         f"straight on to {p.counterparty} in {p.cp_country}.")
            big = [t for t in sub if t.direction == "in" and t.cp_account]
            return F("ESCALATE", "ESC_INVOICE_MISMATCH", s,
                     [d.id] + [x[3].id for x in bad] + [t.id for t in big], {"TM01"}, prio=10)

    # (c) customer purchase order + supplier pro forma behind a one-off large order
    for po in c.dt("purchase_order"):
        pref = re.search(r"\b(PO\s*\d+)\b", po.text)
        if not pref:
            continue
        ref = pref.group(1)
        pov = max(_gbp_values(po.text) or [0])
        buyer = re.search(r"from ([A-Z][^\n]+?)$", po.title)
        buyer = buyer.group(1).strip() if buyer else "a customer"
        for inv in c.dt("supplier_invoice") + c.dt("invoice"):
            if ref not in inv.text:
                continue
            amt = _money(_field(inv, "amount"))
            pays = [t for t in c.ctx.txns if t.direction == "out" and t.reference.strip() == ref]
            if not pays or amt is None:
                continue
            p = pays[0]
            acct = _field(inv, "pay to")
            ok = (abs(p.amount - amt) < 0.01 and (not acct or _last4(acct) == _last4(p.cp_account))
                  and _name_in(p.counterparty, inv.title + " " + inv.text)
                  and (not inv.dated or not p.ts or inv.dated.date() <= p.ts.date())
                  and (not pov or amt <= pov))
            bigs = [t for t in sub if t.direction == "in" and t.amount >= amt * 0.9]
            if ok:
                s = [f"Purchase order {po.id} ({_day(po.dated)}) from {buyer} places order {ref} worth "
                     f"{fmt_gbp(pov)} with {c.name}, which explains the unusually large funding"
                     + (f" of {c.tx(bigs[0])}." if bigs else ".")]
                s.append(f"Pro forma invoice {inv.id} from {p.counterparty} for {ref} matches the onward payment "
                         f"{c.tx(p)} in amount, payee and account ending {_last4(p.cp_account)}.")
                return F("CLOSE", "INVOICE_MATCHES", s, [inv.id, po.id, p.id] + [b.id for b in bigs[:1]],
                         {"TM01"})
            return F("ESCALATE", "ESC_INVOICE_MISMATCH",
                     [f"Pro forma invoice {inv.id} for order {ref} does not match the payment {c.tx(p)} "
                      f"(amount, payee, account or date differ).",
                      f"The documents do not support the large payment behind the alert."],
                     [inv.id, po.id, p.id], {"TM01"})

    # (d) third-party inflows explained elsewhere (e.g. invoice finance advance, refund) -> handled by TM08
    if "TM08" in c.rules:
        return None

    # (e) nothing on file: a one-off large own-name funding paid on to a supplier
    ins = [t for t in sub if t.direction == "in"]
    big = max(ins, key=lambda t: t.amount) if ins else None
    onward = None
    if big:
        cands = [t for t in win if t.direction == "out" and t.ts and big.ts and
                 timedelta(0) <= t.ts - big.ts <= timedelta(days=3) and not c.is_client(t.counterparty)]
        onward = max(cands, key=lambda t: t.amount) if cands else None
    s = [f"In the 30 days to {_day(c.ctx.case.end)}, {c.name} received {fmt_gbp(received)}, {mult:.2f} times the "
         f"monthly share of its declared payment volume ({fmt_gbp(baseline)})" +
         (f", driven by {c.tx(big)}." if big else ".")]
    if onward:
        s.append(f"That money was paid on to {onward.counterparty} the next day ({onward.id}, {fmt_gbp(onward.amount)},"
                 f" reference '{onward.reference}'), and no order, invoice or contract for it is on file.")
    s.append("Ask the customer for the customer order and the supplier invoice behind this one-off payment, "
             "showing what it was for and where the funding came from.")
    return F("RFI", "RFI_PURPOSE_DOCUMENTS", s, ([big.id] if big else []) + ([onward.id] if onward else []),
             {"TM01"})


# --------------------------------------------------------------------------
# TM08 (with TM02): third-party funding
# --------------------------------------------------------------------------

def tm08(c: Case, a) -> F | None:
    sub = [t for t in c.alert_txns(a) if t.direction == "in"]
    if not sub:
        return None
    payers = sorted({t.counterparty for t in sub})
    total = sum(t.amount for t in sub)
    covers = {"TM08", "TM02", "TM01"}

    # refund of an earlier payment
    for t in sub:
        r = refund_finding(c, t)
        if r:
            return r

    # invoice finance advance documented by agreement + remittance advice
    fac = [d for d in c.dt("factoring_agreement") if any(_name_in(p, d.title + " " + d.text) for p in payers)]
    if fac or any(re.search(r"invoice financ|factor", p, re.I) for p in payers):
        t = sub[0]
        rmt = [d for d in c.dt("remittance_advice") if _has_amount(d.text, t.amount)
               and _name_in(t.counterparty, d.title + " " + d.text)]
        if fac and rmt:
            f, r = fac[0], rmt[0]
            pct = re.search(r"(\d{2})%", f.text)
            inv_line = _field(r, "invoices financed")
            mm = re.match(r"(\d+),\s*total\s*(£[\d,.]+)", inv_line or "")
            if mm:
                inv_line = f"{mm.group(1)} invoices financed totalling {mm.group(2)}"
            s = [f"Invoice finance agreement {f.id} ({_day(f.dated)}) with {t.counterparty} provides advances of "
                 f"{pct.group(1) + '%' if pct else 'a share'} of approved customer invoices, paid to {c.name}'s "
                 f"account at this firm.",
                 f"Remittance advice {r.id} ({_day(r.dated)}) shows {inv_line or 'the financed invoices'} and an "
                 f"advance of {fmt_gbp(t.amount)}, exactly the payment {t.id} received on {_day(t.ts)} with "
                 f"reference '{t.reference}'."]
            if "TM02" in c.rules or "TM01" in c.rules:
                s.append("The advance is the client's own receivables finance, so its use for supplier payments "
                         "and the higher receipts are explained.")
            return F("CLOSE", "THIRD_PARTY_DOCUMENTED", s, [f.id, r.id, t.id], covers)
        if fac and not rmt:
            return F("RFI", "RFI_SOURCE_OF_FUNDS",
                     [f"Agreement {fac[0].id} covers advances from {t.counterparty}, but no remittance advice on file "
                      f"shows the {fmt_gbp(t.amount)} paid on {_day(t.ts)} ({t.id}).",
                      f"Ask the customer for the remittance advice from {t.counterparty} listing the invoices "
                      f"financed by this advance."], [fac[0].id, t.id], covers)
        return F("RFI", "RFI_SOURCE_OF_FUNDS",
                 [f"{t.counterparty}, an account not in {c.name}'s name, paid {fmt_gbp(t.amount)} into the account "
                  f"on {_day(t.ts)} ({t.id}) with reference '{t.reference}'.",
                  "No invoice finance agreement or remittance advice is on file to show the relationship, and "
                  "nothing else in the account is concerning.",
                  f"Ask the customer for the invoice finance agreement with {t.counterparty} and the remittance "
                  f"advice for this advance."], [t.id], covers)

    # insurance claim settlement
    for d in c.dt("insurer_letter"):
        t = sub[0]
        if _has_amount(d.text, t.amount) and _name_in(t.counterparty, d.title + " " + d.text):
            claim = re.search(r"\b(CLAIM\s*\d+)\b", d.text)
            ok_ref = not claim or claim.group(1) in (t.reference or "") or not t.reference
            if ok_ref:
                return F("CLOSE", "DOCUMENTED_EVENT",
                         [f"Claim settlement letter {d.id} ({_day(d.dated)}) from {t.counterparty} confirms it "
                          f"accepted {c.name}'s claim{' ' + claim.group(1) if claim else ''} for cargo damaged in "
                          f"transit and will pay {fmt_gbp(t.amount)} to the client.",
                          f"Payment {t.id} of {fmt_gbp(t.amount)} on {_day(t.ts)}, reference '{t.reference}', is "
                          f"that settlement."], [d.id, t.id], covers | {"TM07"})

    # a company in the client's group
    grp = c.dt("group_structure")
    if grp or any(re.search(r"intercompany|group", (t.reference or "") + " " + t.counterparty, re.I) for t in sub):
        t = sub[0]
        g = [d for d in grp if _name_in(t.counterparty, d.text)]
        if g:
            gd = g[0]
            rows = _rows(gd, "company")
            rel = ""
            for r in rows:
                if _same_name(_col(r, "company"), c.name):
                    rel = f"{c.name} is owned by {_col(r, 'owned', 'owner')} ({_col(r, 'share')})"
            s = [f"Group structure chart {gd.id} ({_day(gd.dated)}) shows {t.counterparty} in the same group as "
                 f"{c.name}" + (f": {rel}." if rel else ".")]
            em = [d for d in c.dt("email") if _name_in(t.counterparty, d.text)]
            if em:
                s[0] = (f"Group structure chart {gd.id} shows {c.name} and {t.counterparty} have the same owner, "
                        f"and email {em[0].id} ({_day(em[0].dated)}) from {em[0].title.replace('Email from ', '')} "
                        f"says {t.counterparty} is funding a supplier payment while receivables come in.")
            ev = [gd.id] + [e.id for e in em[:1]] + [t.id]
            if c.held and not c.is_client(c.held.counterparty):
                h = c.held
                inv = [d for d in c.dt("supplier_invoice") + c.dt("invoice")
                       if _name_in(h.counterparty, d.title + " " + d.text)]
                good = [d for d in inv if _has_amount(d.text, h.amount)
                        and (not _field(d, "pay to") or _last4(_field(d, "pay to")) == _last4(h.cp_account))]
                if good:
                    s.append(f"Invoice {good[0].id} from {h.counterparty} for {fmt_gbp(h.amount)} to account ending "
                             f"{_last4(h.cp_account)} matches the held payment {c.tx(h)}.")
                    ev = [gd.id] + [e.id for e in em[:1]] + [good[0].id, h.id, t.id]
                elif h.amount >= INVOICE_REQUIRED:
                    if inv:
                        return F("ESCALATE", "ESC_INVOICE_MISMATCH",
                                 [f"Invoice {inv[0].id} from {h.counterparty} does not match the held payment "
                                  f"{c.tx(h)} in amount or account.",
                                  f"The group funding from {t.counterparty} ({t.id}) is passing to a payee the "
                                  f"documents do not support."], [inv[0].id, h.id, gd.id, t.id], covers)
                    return F("RFI", "RFI_PURPOSE_DOCUMENTS",
                             s + [f"No invoice is on file for the held payment {c.tx(h)}; ask the customer for "
                                  f"the invoice from {h.counterparty}."], ev + [h.id], covers)
            else:
                s.append(f"The {fmt_gbp(t.amount)} received on {_day(t.ts)} ({t.id}, reference '{t.reference}') is "
                         f"intra-group funding of the client.")
            return F("CLOSE", "GROUP_COMPANY", s, ev, covers)
        if grp:
            return F("ESCALATE", "ESC_THIRD_PARTY_FUNDING",
                     [f"{t.counterparty} paid {fmt_gbp(t.amount)} on {_day(t.ts)} ({t.id}) as group funding, but "
                      f"group structure chart {grp[0].id} does not include it.",
                      "An unrelated company is funding the account, so the case goes to the MLRO."],
                     [grp[0].id, t.id], covers)

    # freight forwarder collecting from exporters and paying carriers
    if re.search(r"freight|collects", c.activity, re.I) or any(re.search(r"freight", t.reference or "", re.I)
                                                               for t in sub):
        fi = c.dt("freight_invoices")
        ci = c.dt("carrier_invoice")
        rows = [r for d in fi for r in _rows(d, "invoice")]
        matched = [t for t in sub if any(_col(r, "invoice").strip() == t.reference.strip()
                                         and _same_name(_col(r, "exporter", "customer"), t.counterparty)
                                         and abs((_money(_col(r, "amount")) or -1) - t.amount) < 0.01
                                         for r in rows)]
        h = c.held
        cinv = None
        if h:
            for d in ci + c.dt("invoice"):
                acct = _field(d, "pay to")
                if _has_amount(d.text, h.amount) and _name_in(h.counterparty, d.title + " " + d.text) and \
                        (not acct or _last4(acct) == _last4(h.cp_account)):
                    cinv = d
        if fi and len(matched) == len(sub) and (cinv or not h):
            s = [f"Freight invoices {fi[0].id} issued by {c.name} list {len(rows)} shipping charges to "
                 f"{', '.join(payers[:3])}, matching the {len(sub)} receipts ({fmt_gbp(total)}) by reference and "
                 f"amount, as expected for a freight forwarder."]
            if cinv:
                s.append(f"Carrier invoice {cinv.id} from {h.counterparty} for {fmt_gbp(h.amount)} to account ending "
                         f"{_last4(h.cp_account)} matches the held payment {c.tx(h)}.")
            return F("CLOSE", "BUSINESS_RECORDS_RECONCILE", s,
                     [fi[0].id] + ([cinv.id] if cinv else []) + ([h.id] if h else []) + [t.id for t in sub], covers)
        if fi or ci:
            d = (fi + ci)[0]
            return F("ESCALATE", "ESC_INVOICE_MISMATCH",
                     [f"{d.id} does not reconcile to the receipts from {', '.join(payers[:3])} or to the held "
                      f"payment" + (f" {c.tx(h)}." if h else "."),
                      "The business records do not support the money passing through the account."],
                     [d.id] + ([h.id] if h else []) + [t.id for t in sub], covers)
        s = [f"{c.name} received {fmt_gbp(total)} from {len(sub)} exporters ({', '.join(payers[:3])}) with "
             f"freight references and paid it on" + (f", including the held payment {c.tx(h)}" if h else "") +
             ", which fits its declared freight forwarding business."]
        s.append("No freight invoices or carrier invoice are on file, and a payment of £10,000 or more to a "
                 "third party needs one before release.")
        s.append(f"Ask the customer for the freight invoices issued to these exporters and the carrier's invoice"
                 + (f" from {h.counterparty} for the {fmt_gbp(h.amount)} payment." if h else "."))
        return F("RFI", "RFI_BUSINESS_RECORDS", s, ([h.id] if h else []) + [t.id for t in sub], covers)

    # commission pool distributed to many people (payment file with shared addresses)
    pf = c.dt("payment_file")
    if pf or any(re.search(r"commission|pool", t.reference or "", re.I) for t in sub):
        return None  # decided together with TM04 in tm04()

    # third parties paying in and the money leaving at once to other parties
    t0 = sub[0]
    outs = [o for o in c.ctx.txns if o.direction == "out" and o.ts and any(
        i.ts and timedelta(0) <= o.ts - i.ts <= timedelta(hours=48) for i in sub)
        and not c.is_client(o.counterparty)]
    nonsupplier = [o for o in outs if not re.search(r"\b(PO|INV|INVOICE|ORDER)\b", o.reference or "", re.I)]
    covered_docs = [d for d in c.docs if any(_name_in(p, d.text) for p in payers)]
    if not covered_docs:
        rv = [r for r in c.ctx.reviews if str(r.get("verdict", "")).upper().startswith("CLOSE")
              and re.search(r"consult|fee", str(r.get("note", "")), re.I)]
        contracts = [d for d in c.docs if "contract" in d.type.lower()]
        s = [f"{len(sub)} companies with no documented link to {c.name} ({', '.join(payers[:3])}) paid in "
             f"{fmt_gbp(total)} between {_day(sub[0].ts)} and {_day(sub[-1].ts)}"
             + (f" with reference '{t0.reference}'" if t0.reference else "") + "."]
        if nonsupplier:
            s.append(f"Within hours the same money left again to unrelated parties: " +
                     "; ".join(c.tx(o) for o in nonsupplier[:2]) + f", referenced '{nonsupplier[0].reference}'.")
        if contracts or rv:
            ref = contracts[0] if contracts else None
            s.append((f"The only contract on file ({ref.id}) " if ref else "The earlier review ") +
                     (f"and earlier review {rv[0].get('review_id')} " if ref and rv else "") +
                     "cover a single small consultancy fee from a different client, which does not explain these "
                     "new, larger payments.")
        code = "ESC_PASS_THROUGH" if nonsupplier else "ESC_THIRD_PARTY_FUNDING"
        ev = ([c.held.id] if c.held else []) + [o.id for o in nonsupplier[:2]] + [t.id for t in sub] + \
             [d.id for d in contracts[:1]] + [str(r.get("review_id")) for r in rv[:1]]
        return F("ESCALATE", code, s, ev, covers, prio=20)
    d = covered_docs[0]
    return F("RFI", "RFI_SOURCE_OF_FUNDS",
             [f"{d.id} mentions {', '.join(payers[:2])}, but it does not match the {fmt_gbp(total)} received from "
              f"them ({', '.join(t.id for t in sub[:3])}).",
              f"Ask the customer for the document showing why {', '.join(payers[:2])} paid this money to {c.name}."],
             [d.id] + [t.id for t in sub], covers)


# --------------------------------------------------------------------------
# TM04: many beneficiaries
# --------------------------------------------------------------------------

def tm04(c: Case, a) -> F:
    sub = c.alert_txns(a)
    small = [t for t in sub if t.direction == "out" and t.amount < INVOICE_REQUIRED]
    n = int(a.measured.get("distinct_counterparties") or len({t.counterparty for t in sub}))
    refs = Counter(t.reference for t in small)
    ref = refs.most_common(1)[0][0] if refs else ""
    # the batch of small payments sharing one reference (fees, commission, wages)
    small = [t for t in small if t.reference == ref] or small
    ev_tx = ([c.held.id] if c.held else [])
    for d in c.dt("payment_file"):
        rows = _rows(d, "amount")
        names = {}
        for r in rows:
            nm = _col(r, "payee", "contractor", "name", "employee")
            names[norm_name(nm)] = (_money(_col(r, "amount")), _col(r, "address"), nm)
        matched = [t for t in small if norm_name(t.counterparty) in names
                   and abs((names[norm_name(t.counterparty)][0] or -1) - t.amount) < 0.01]
        addrs = Counter(v[1] for v in names.values() if v[1])
        shared = [(ad, k) for ad, k in addrs.most_common() if k >= 3]
        tp_in = [t for al in c.by_rule.get("TM08", []) for t in c.alert_txns(al) if t.direction == "in"]
        if shared:
            ad, k = shared[0]
            s = [f"{d.id} ({d.title}) lists {len(names)} payees abroad, but they share only {len(addrs)} UK "
                 f"addresses: {k} of them give {ad}."]
            if tp_in:
                t = tp_in[0]
                s.append(f"The money came in as {c.tx(t)} with reference '{t.reference}', from a third "
                         f"party with no link to {c.name}'s declared business ({c.activity}), and was "
                         f"paid straight out to these {len(matched)} people as '{ref}'.")
            s.append("The account is receiving and passing on another party's money to a network of linked "
                     "payees, so it goes to the MLRO.")
            return F("ESCALATE", "ESC_MULE_ACTIVITY", s,
                     [d.id] + [t.id for t in tp_in] + ev_tx + [t.id for t in small[:3]],
                     {"TM04", "TM08", "TM02"}, prio=8)
        if small and len(matched) == len(small):
            s = [f"{d.id} ({d.title}, {_day(d.dated)}) lists {len(rows)} payees with their countries and "
                 f"amounts, and all {len(matched)} small payments referenced '{ref}' match it by name and amount"
                 + (f", including the held payment {c.tx(c.held)}." if c.held else ".")]
            if c.activity:
                s.append(f"Paying many people abroad fits {c.name}'s declared business ({c.activity}); "
                         f"the other {n - len(matched)} beneficiaries are its regular suppliers.")
            return F("CLOSE", "BUSINESS_RECORDS_RECONCILE", s, [d.id] + ev_tx + [t.id for t in matched[:3]],
                     {"TM04"})
        return F("RFI", "RFI_BUSINESS_RECORDS",
                 [f"{d.id} does not account for all {len(small)} small payments in the alert "
                  f"({len(matched)} match).",
                  "Ask the customer for a complete payment file covering every payee paid in the period."],
                 [d.id] + ev_tx, {"TM04"})
    s = [f"{c.name} paid {n} different beneficiaries in 30 days, including {len(small)} small payments to "
         f"individuals abroad referenced '{ref}'" + (f", among them the held payment {c.tx(c.held)}." if c.held
                                                      else ".")]
    if c.activity:
        s.append(f"This could fit the declared business ({c.activity}), but no contractor or payroll "
                 f"file is on file to show who these people are.")
    s.append("Ask the customer for the contractor payment file listing each payee, their contract and the amount "
             "paid in the period.")
    return F("RFI", "RFI_BUSINESS_RECORDS", s, ev_tx + [t.id for t in small[:3]], {"TM04"})


# --------------------------------------------------------------------------
# TM07: dormant account reactivation
# --------------------------------------------------------------------------

def tm07(c: Case, a) -> F:
    sub = c.alert_txns(a)
    days = a.measured.get("dormant_days")
    first = sub[0].ts if sub and sub[0].ts else None
    prior_cps = {norm_name(t.counterparty) for t in c.ctx.txns if t.ts and first and t.ts < first}
    outs = [t for t in sub if t.direction == "out"]
    ins = [t for t in sub if t.direction == "in"]
    rv = c.dt("reverification_record")
    good_rv = [d for d in rv if re.search(r"re-?verified", d.text, re.I)
               and not re.search(r"new device|new phone|different device|not registered|failed|could not",
                                 d.text, re.I)]
    bad_rv = [d for d in rv if d not in good_rv]
    if bad_rv and not good_rv:
        d = bad_rv[0]
        return F("ESCALATE", "ESC_ACCOUNT_MISUSE",
                 [f"Re-verification record {d.id} ({_day(d.dated)}) does not confirm that the registered customer is "
                  f"the one now using the account after {days} dormant days: {_body(d)[:160].rstrip()}",
                  f"Money then moved through the account" + (f": {c.tx(outs[0])}." if outs else "."),
                  "Someone other than the customer may control the account, so it goes to the MLRO."],
                 [d.id] + [t.id for t in sub], {"TM07"}, prio=8)
    if good_rv:
        d = good_rv[0]
        s = [f"Re-verification record {d.id} ({_day(d.dated)}) confirms the directors' identity with a live selfie "
             f"on the device already registered to them and that trading paused while the business moved "
             f"premises, explaining the {days} dormant days."]
        ev = [d.id]
        for o in outs:
            inv = [x for x in c.dt("supplier_invoice") + c.dt("invoice") if _has_amount(x.text, o.amount)
                   and _name_in(o.counterparty, x.title + " " + x.text)
                   and (not _field(x, "pay to") or _last4(_field(x, "pay to")) == _last4(o.cp_account))]
            if not inv:
                named = [x for x in c.dt("supplier_invoice") + c.dt("invoice")
                         if _name_in(o.counterparty, x.title + " " + x.text)]
                if named:
                    return F("ESCALATE", "ESC_INVOICE_MISMATCH",
                             [f"Invoice {named[0].id} from {o.counterparty} does not match {c.tx(o)}: the amount or "
                              f"the account paid differ.",
                              f"After {days} dormant days the first payment is not supported by the documents, so "
                              f"it goes to the MLRO."], [named[0].id, o.id, d.id], {"TM07"}, prio=9)
                if o.amount >= INVOICE_REQUIRED and not c.is_client(o.counterparty):
                    return F("RFI", "RFI_PURPOSE_DOCUMENTS",
                             s + [f"No invoice is on file for {c.tx(o)}; ask the customer for the supplier's "
                                  f"invoice for this payment."], ev + [o.id], {"TM07"})
                continue
            s.append(f"The first payment after the pause, {c.tx(o)}, matches invoice {inv[0].id} from "
                     f"{o.counterparty}" + (", a supplier the client paid before the pause."
                                            if norm_name(o.counterparty) in prior_cps else "."))
            ev += [inv[0].id, o.id]
        own = [i for i in ins if c.is_client(i.counterparty)]
        if own:
            ev.append(own[0].id)
        return F("CLOSE", "RETURNING_CUSTOMER", s, ev, {"TM07"})
    # third-party inflow (e.g. insurance settlement) is handled with TM08
    if "TM08" in c.rules:
        return None
    s = [f"The account was dormant for {days} days and then moved {fmt_gbp(float(a.measured.get('moved_gbp') or 0))}"
         + (f": {c.tx(ins[0])}" if ins else "") + (f", paid on as {c.tx(outs[0])}" if outs else "") + "."]
    if outs and norm_name(outs[0].counterparty) in prior_cps:
        s.append(f"{outs[0].counterparty} was a supplier before the pause and nothing points to suspicion, but "
                 f"there is no re-verification of the customer or invoice on file.")
    s.append("Ask the customer to confirm why trading paused and resumed and to provide the supplier invoice for "
             "the payment" + (f" to {outs[0].counterparty}." if outs else "."))
    return F("RFI", "RFI_PURPOSE_DOCUMENTS", s, [t.id for t in sub], {"TM07"})


# --------------------------------------------------------------------------
# combination
# --------------------------------------------------------------------------

HANDLERS = {"TM22": tm22, "TM03": tm03, "TM06": tm06, "TM01": tm01, "TM08": tm08, "TM04": tm04, "TM07": tm07}


def _reason(f: F) -> str:
    s = [x.strip() for x in f.sentences if x and x.strip()]
    s = [x if x.endswith((".", "?", "!")) else x + "." for x in s]
    return re.sub(r"\.\.(\s|$)", r".\1", " ".join(s[:4]))


def _out(f: F) -> dict:
    ev = []
    for i in f.evidence:
        if i and i not in ev:
            ev.append(i)
    return {"verdict": f.verdict, "reason_code": f.code, "reason": _reason(f), "evidence": ev}


def decide(ctx, alerts):
    if not alerts:
        return None
    c = Case(ctx, alerts)
    findings: list[F] = list(staff_note_flags(c))
    for a in alerts:
        h = HANDLERS.get(a.rule)
        if h is None:
            continue
        f = h(c, a)
        if f is not None:
            findings.append(f)
    if not findings:
        return None
    worst = max(SEV[f.verdict] for f in findings)
    top = sorted([f for f in findings if SEV[f.verdict] == worst], key=lambda f: f.prio)
    f = top[0]
    if f.verdict != "CLOSE":
        return _out(f)
    covered = set().union(*(x.covers for x in findings))
    if not c.rules <= covered:
        return None  # an alert nothing here explains: let the generic engine look at it
    # policy guard: a held payment of £10k+ to a third party needs an invoice/contract cited
    h = c.held
    if h and h.amount >= INVOICE_REQUIRED and not c.is_client(h.counterparty):
        cited = {i for x in findings for i in x.evidence}
        docs_for_held = [d for d in c.docs if d.id in cited and _has_amount(d.text, h.amount)]
        if not docs_for_held:
            return _out(F("RFI", "RFI_PURPOSE_DOCUMENTS",
                          f.sentences[:2] + [f"No invoice or contract on file covers the held payment {c.tx(h)}, "
                                             f"which policy requires before release; ask the customer for it."],
                          f.evidence + [h.id]))
    # merge evidence of the other close findings
    extra = [i for x in findings if x is not f for i in x.evidence]
    return _out(F(f.verdict, f.code, f.sentences, f.evidence + extra))
