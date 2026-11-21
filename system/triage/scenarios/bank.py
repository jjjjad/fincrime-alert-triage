"""Scenario logic for the bank institution (see scenarios/__init__.py).

The bank's cases fall into recurring patterns. Each pattern is recognised from
generic, policy-grounded features (document types, what the documents say, and
whether amounts, dates, names and account numbers in them match the alerted
transactions), never from case IDs or names.

Order of checks:
 1. Signs of crime that decide the case on their own (fraud reports from paying
    banks, a customer asking how to avoid cash reporting, payers that do not
    exist, contradictory explanations, unrelated people funding payments to a
    higher-risk country, money passed through to companies the director owns).
 2. Internal-list matches (TM22): different party -> CLOSE, controlled by the
    listed party -> ESCALATE, nothing on file -> RFI.
 3. Structuring (TM03): close only when takings / collection records reconcile
    deposit by deposit, otherwise escalate.
 4. Everything else: every alerted transaction must be explained by a matching
    document (or be routine salary/takings). Own-account moves at 5x or more of
    the declared income go to RFI for an updated profile (policy setting).
 5. Unexplained but not suspicious -> RFI asking for the one document that would
    settle it.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
_DATE_RE = re.compile(r"\b(\d{1,2}) (January|February|March|April|May|June|July|August|September|"
                      r"October|November|December) (\d{4})\b", re.I)
_MONTH_YEAR_RE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|"
                            r"October|November|December) (\d{4})\b", re.I)
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_GBP_RE = re.compile(r"£\s?(\d[\d,]*(?:\.\d{1,2})?)")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def gbp(x) -> str:
    if x is None:
        return "an unknown amount"
    return f"£{x:,.2f}"


def dstr(dt) -> str:
    if dt is None:
        return "an unknown date"
    return f"{dt.day} {dt.strftime('%B %Y')}"


def norm(s) -> str:
    s = str(s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(s.split())


def same_name(a, b) -> bool:
    a, b = norm(a), norm(b)
    return bool(a) and a == b


def name_in(name, text) -> bool:
    n = norm(name)
    return bool(n) and re.search(rf"\b{re.escape(n)}\b", norm(text)) is not None


def doc_amounts(text: str) -> list[float]:
    return [float(m.group(1).replace(",", "")) for m in _GBP_RE.finditer(text or "")]


def has_amount(text: str, amt: float, tol: float = 0.01) -> bool:
    return any(abs(a - amt) <= tol for a in doc_amounts(text))


def parse_dates(text: str) -> list[datetime]:
    out = []
    for m in _DATE_RE.finditer(text or ""):
        try:
            out.append(datetime(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1))))
        except ValueError:
            pass
    for m in _ISO_RE.finditer(text or ""):
        try:
            out.append(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    return out


def first_date(text: str):
    ds = parse_dates(text)
    return ds[0] if ds else None


def month_year(text: str):
    m = _MONTH_YEAR_RE.search(text or "")
    if not m:
        return None
    return (int(m.group(2)), MONTHS[m.group(1).lower()])


def field(doc, *keys):
    for k in keys:
        v = doc.fields.get(k.lower())
        if v:
            return v.strip()
    return None


def tables(text: str) -> list[list[dict]]:
    """Markdown tables as lists of row dicts keyed by lower-cased header."""
    out, cur, header = [], [], None
    for line in (text or "").split("\n"):
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            if header is None:
                header = [c.lower() for c in cells]
            else:
                cur.append({header[i] if i < len(header) else str(i): c for i, c in enumerate(cells)})
        else:
            if header is not None:
                out.append(cur)
            cur, header = [], None
    if header is not None:
        out.append(cur)
    return out


def body(doc) -> str:
    """Document text without the title line and the ID/date header fields."""
    lines = []
    for l in (doc.text or "").split("\n"):
        if l.startswith("# ") or l.startswith("**Document ID") or l.startswith("**Dated"):
            continue
        lines.append(l)
    return "\n".join(lines)


def raw_channel(t) -> str:
    return str(t.raw.get("channel") or "").lower()


def is_cash_in(t) -> bool:
    return t.direction == "in" and (t.channel == "cash" or "cash" in raw_channel(t))


def is_cash_out(t) -> bool:
    return t.direction == "out" and (t.channel == "cash" or "cash" in raw_channel(t))


def location(t) -> str:
    return str(t.raw.get("location") or "")


def acct_tail(s: str) -> str:
    m = re.search(r"(\d{4})\s*$", str(s or ""))
    return m.group(1) if m else ""


class B:
    """Per-case view with the helpers the handlers share."""

    def __init__(self, ctx, alerts):
        self.ctx = ctx
        self.alerts = alerts
        self.rules = {a.rule for a in alerts}
        self.cust = ctx.customer
        self.name = ctx.customer.name
        self.kind = ctx.customer.kind
        raw = ctx.customer.raw or {}
        self.employer = raw.get("employer") or ""
        self.declared = raw.get("declared") or {}
        self.income = ctx.customer.expected_monthly_in
        self.countries = [c.lower() for c in ctx.customer.countries]
        self.docs = ctx.docs
        self.by_type: dict[str, list] = {}
        for d in ctx.docs:
            self.by_type.setdefault(d.type.lower(), []).append(d)
        seen, at = set(), []
        for a in alerts:
            for i in a.txn_ids:
                t = ctx.folder.txns.get(i)
                if t is not None and i not in seen:
                    seen.add(i)
                    at.append(t)
        at.sort(key=lambda t: (t.ts or datetime.min, t.id))
        self.atx = at

    def explain_cache(self, t):
        if not hasattr(self, "_xc"):
            self._xc = {}
        if t.id not in self._xc:
            self._xc[t.id] = explain_in(self, t) if t.direction == "in" else explain_out(self, t)
        return self._xc[t.id]

    def of(self, *types):
        out = []
        for t in types:
            out += self.by_type.get(t, [])
        return out

    def alert(self, rule):
        for a in self.alerts:
            if a.rule == rule:
                return a
        return None

    def multiple(self):
        a = self.alert("TM01")
        if a is None:
            return None
        m = a.measured.get("multiple")
        try:
            return float(m)
        except (TypeError, ValueError):
            pass
        rec = a.measured.get("received_gbp")
        base = a.measured.get("baseline_gbp") or self.income
        try:
            return float(rec) / float(base)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    def routine_in(self, t) -> bool:
        """Salary, allowance or card-takings settlements: within the declared profile."""
        ref = (t.reference or "").upper()
        if t.direction != "in":
            return False
        if "SALARY" in ref or "PAYROLL" in ref:
            return True
        if self.employer and same_name(t.counterparty, self.employer):
            return True
        if "ALLOWANCE" in ref and self.income and t.amount <= 1.5 * self.income:
            return True
        if "CARD SETTLEMENT" in ref and self.kind == "business":
            return True
        return False

    def routine_out(self, t) -> bool:
        """Card spending and household bills (direct debits, standing orders such as rent)."""
        if t.direction != "out":
            return False
        return t.channel == "card" or raw_channel(t) in ("direct_debit", "standing_order", "card")

    def routine(self, t) -> bool:
        return self.routine_in(t) or self.routine_out(t)

    def is_hr(self, country) -> bool:
        return bool(country) and country.strip().lower() in self.ctx.high_risk

    def undeclared(self, country) -> bool:
        return bool(country) and country.strip().lower() not in self.countries

    def result(self, verdict, code, sentences, evidence):
        reason = " ".join(s.strip() for s in sentences if s and s.strip())
        ev = []
        for i in evidence:
            if i and i not in ev:
                ev.append(i)
        return {"verdict": verdict, "reason_code": code, "reason": reason, "evidence": ev}


def tdesc(t) -> str:
    if is_cash_in(t):
        loc = location(t)
        return f"the {gbp(t.amount)} cash deposit on {dstr(t.ts)}" + (f" at {loc}" if loc else "") + f" ({t.id})"
    if is_cash_out(t):
        return f"the {gbp(t.amount)} cash withdrawal on {dstr(t.ts)} ({t.id})"
    if t.direction == "in":
        return f"the {gbp(t.amount)} received from {t.counterparty or 'an unnamed payer'} on {dstr(t.ts)} ({t.id})"
    return f"the {gbp(t.amount)} paid to {t.counterparty or 'an unnamed payee'} on {dstr(t.ts)} ({t.id})"


def _and(xs) -> str:
    xs = list(xs)
    if len(xs) <= 1:
        return "".join(xs)
    return ", ".join(xs[:-1]) + " and " + xs[-1]


def cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


# --------------------------------------------------------------------------
# 1. signs of crime
# --------------------------------------------------------------------------

def h_fraud_reports(b: B):
    reps = [d for d in b.docs if "fraud_report" in d.type.lower() or
            re.search(r"push payment scam|scam: request to freeze|fraud report", d.text, re.I)]
    if not reps:
        return None
    victims = []
    for d in reps:
        m = re.search(r"Our customer ([A-Z][\w'-]+(?: [A-Z][\w'-]+)+) sent £", d.text)
        if m:
            victims.append(m.group(1))
    ins = [t for t in b.ctx.win if t.direction == "in" and not b.routine_in(t)]
    victim_tx = [t for t in ins if any(same_name(t.counterparty, v) for v in victims)]
    outs = [t for t in b.ctx.win if t.direction == "out" and t.channel != "card" and
            raw_channel(t) not in ("direct_debit", "standing_order")]
    top = Counter(t.counterparty for t in outs if t.counterparty).most_common(1)
    payers = sorted({t.counterparty for t in ins if t.counterparty})
    chat = [d for d in b.of("support_chat") if re.search(r"crypto|keep \d+%|send it on|remote job", d.text, re.I)]
    s1 = (f"{len(reps)} fraud report{'s' if len(reps) > 1 else ''} from paying banks "
          f"({', '.join(d.id for d in reps[:4])}) say that "
          f"{_and(victims[:3])}{' and others' if len(victims) > 3 else ''} "
          f"{'was' if len(victims) == 1 else 'were'} scammed into paying this account.")
    s2 = (f"{len(payers)} different people paid in {gbp(sum(t.amount for t in ins))} in the window and the money "
          f"left again within hours" +
          (f", mostly to {top[0][0]} ({top[0][1]} payments)" if top else "") + ".")
    s3 = ""
    if chat:
        s3 = (f"In {chat[0].id} the customer says they receive other people's money for an online 'employer' "
              f"and forward it, keeping a cut: the account is being used as a money mule.")
    ev = [d.id for d in reps] + [d.id for d in chat] + [t.id for t in victim_tx[:6]] + \
         [t.id for t in outs if top and t.counterparty == top[0][0]][:4]
    return b.result("ESCALATE", "ESC_MULE_ACTIVITY", [s1, s2, s3], ev)


def h_threshold_questions(b: B):
    """A customer asking how to keep cash deposits under the reporting threshold."""
    notes = [d for d in b.of("staff_note", "contact_note", "support_chat")
             if re.search(r"aggregat|reporting (requirement|threshold)|below £10,000|under £10,000|"
                          r"thresholds?\b", d.text, re.I)
             and re.search(r"cash", d.text, re.I)]
    if not notes:
        return None
    cash = [t for t in b.atx if is_cash_in(t)]
    sub = [t for t in cash if 9000 <= t.amount < 10000] or cash
    total = sum(t.amount for t in sub)
    branches = sorted({location(t) for t in sub if location(t)})
    n = notes[0]
    s1 = (f"{len(sub)} cash deposits of between {gbp(min(t.amount for t in sub))} and "
          f"{gbp(max(t.amount for t in sub))}, totalling {gbp(total)}, were paid in between "
          f"{dstr(sub[0].ts)} and {dstr(sub[-1].ts)} at {len(branches)} different branches, each just under the "
          f"£10,000.00 reporting threshold." if sub else "")
    s2 = (f"Staff note {n.id} ({dstr(n.dated)}) records the customer asking whether deposits at different "
          f"branches are aggregated and whether keeping each under £10,000 avoids reporting.")
    s3 = ""
    acc = [d for d in b.of("management_accounts")]
    if acc:
        to = field(acc[0], "turnover")
        if to:
            s3 = f"The deposits come close to the whole annual turnover of {to} in management accounts {acc[0].id}."
    return b.result("ESCALATE", "ESC_STRUCTURING", [s1, s2, s3],
                    [n.id] + [d.id for d in acc] + [t.id for t in sub])


def h_dissolved_payer(b: B):
    """Large credits from a company the register shows as dissolved."""
    regs = [d for d in b.of("company_register_extract") if re.search(r"dissolved", d.text, re.I)]
    hits = []
    for d in regs:
        comp = field(d, "company") or d.title.replace("Company register extract for", "").strip()
        tx = [t for t in b.atx if t.direction == "in" and same_name(t.counterparty, comp)]
        if tx:
            hits.append((d, comp, tx))
    if not hits:
        return None
    d, comp, tx = hits[0]
    status = field(d, "status") or "Dissolved"
    total = sum(t.amount for t in tx)
    first = min(tx, key=lambda t: t.ts or datetime.min)
    outs = [t for t in b.ctx.win if t.direction == "out" and t.ts and first.ts and
            t.ts >= first.ts and t.amount >= 0.3 * total and t.channel != "card"]
    chat = [c for c in b.of("support_chat")]
    s1 = (f"The account received {gbp(total)} from {comp} in {len(tx)} payment{'s' if len(tx) > 1 else ''} "
          f"referenced '{tx[0].reference}' from {dstr(first.ts)}, but register extract {d.id} shows the company "
          f"as {status[0].lower() + status[1:]}.")
    s2 = ""
    if chat:
        m = re.search(r"\*\*Customer:\*\*\s*(.+)", chat[0].text)
        claim = m.group(1).strip() if m else ""
        s2 = (f"In {chat[0].id} the customer calls it a loan from a friend, which contradicts both the reference "
              f"and the payer." if re.search(r"loan|friend", claim, re.I) else
              f"The customer's explanation in {chat[0].id} does not fit a dissolved company paying consultancy fees.")
    s3 = ""
    if outs:
        o = outs[0]
        s3 = (f"{cap(tdesc(o))}" + (f" to {o.cp_country}" if o.cp_country and o.cp_country != "United Kingdom" else "")
              + " moved the money straight on.")
    return b.result("ESCALATE", "ESC_UNEXPLAINED_FUNDS", [s1, s2, s3],
                    [d.id] + [c.id for c in chat] + [t.id for t in tx] + [o.id for o in outs[:3]])


def h_conflicting_cash_story(b: B):
    """Cash paid in and sent abroad at once, with explanations that contradict each other."""
    cash = [t for t in b.atx if is_cash_in(t)]
    if not cash or "TM02" not in b.rules:
        return None
    outs = [t for t in b.atx if t.direction == "out" and t.channel != "card" and t.amount >= 0.5 * sum(
        c.amount for c in cash)]
    if not outs:
        return None
    chats = b.of("support_chat")
    sources = []
    for c in chats:
        txt = c.text.lower()
        for kw, label in (("gift", "a gift"), ("selling my car", "the sale of a car"), ("sold my car", "the sale of a car"),
                          ("inherit", "an inheritance"), ("loan", "a loan"), ("savings", "savings"),
                          ("motorbike", "the sale of a motorbike"), ("winnings", "winnings")):
            if kw in txt:
                sources.append((label, c))
    labels = {s for s, _ in sources}
    abroad = [o for o in outs if o.cp_country and o.cp_country.lower() != "united kingdom"]
    if len(labels) < 2 and not abroad:
        return None
    total = sum(t.amount for t in cash)
    o = (abroad or outs)[0]
    s1 = (f"{len(cash)} cash deposit{'s' if len(cash) > 1 else ''} totalling {gbp(total)} "
          f"({', '.join(t.id for t in cash)}) were followed by {tdesc(o)}" +
          (f" to {o.cp_country}" if o.cp_country and o.cp_country != "United Kingdom" else "") +
          (f", against declared income of {gbp(b.income)} a month" if b.income else "") + ".")
    if len(labels) >= 2:
        s2 = ("The customer gave conflicting explanations for the cash: " +
              "; ".join(f"{lab} in {c.id}" for lab, c in sources[:3]) + ", with no document supporting any of them.")
    else:
        s2 = "Nothing on file explains where the cash came from or why it was sent abroad at once."
    s3 = ""
    if o.reference and re.search(r"family", o.reference, re.I) and not name_in(b.name.split()[-1], o.counterparty):
        s3 = f"The payment is labelled '{o.reference}' but goes to {o.counterparty}, who does not share the customer's family name."
    return b.result("ESCALATE", "ESC_UNEXPLAINED_FUNDS", [s1, s2, s3],
                    [c.id for _, c in sources] + [t.id for t in cash] + [o.id])


def h_hr_third_party(b: B):
    """Unrelated people fund the account and the money goes on to several payees in a higher-risk country."""
    if "TM06" not in b.rules:
        return None
    a = b.alert("TM06")
    outs = [t for t in b.ctx.txns if t.id in a.txn_ids and t.direction == "out" and b.is_hr(t.cp_country)]
    if len(outs) < 2:
        return None
    payees = {norm(t.counterparty) for t in outs}
    ins = [t for t in b.ctx.win if t.direction == "in" and not b.routine_in(t) and t.ts and outs[0].ts and
           t.ts <= outs[-1].ts and not same_name(t.counterparty, b.name)]
    third = [t for t in ins if t.counterparty]
    surname = b.name.split()[-1].lower()
    related = [t for t in outs if surname in norm(t.counterparty)]
    invoices = [d for d in b.docs if any(has_amount(d.text, t.amount) and name_in(t.counterparty, d.text) for t in outs)]
    if len(payees) < 2 or invoices or len(related) == len(outs):
        return None
    country = outs[0].cp_country
    chat = b.of("support_chat")
    s1 = (f"{len(outs)} payments totalling {gbp(sum(t.amount for t in outs))} went to {len(payees)} different "
          f"people in {country} ({', '.join(t.counterparty.split(' account')[0] for t in outs[:4])}) between "
          f"{dstr(outs[0].ts)} and {dstr(outs[-1].ts)}; {country} is a higher-risk country the customer did not declare.")
    s2 = (f"They were funded by {len(third)} transfers totalling {gbp(sum(t.amount for t in third))} from unrelated "
          f"people ({', '.join(sorted({t.counterparty for t in third})[:3])}) days earlier." if third else "")
    s3 = ""
    if chat:
        s3 = (f"The customer's only explanation ({chat[0].id}) is that the money helps family, but none of the payees "
              f"shares their family name and nothing documents the relationship or the source.")
    return b.result("ESCALATE", "ESC_HIGH_RISK_CORRIDOR", [s1, s2, s3],
                    [c.id for c in chat] + [t.id for t in outs] + [t.id for t in third])


def h_pass_through_linked(b: B):
    """Client money passed straight to new companies controlled by the customer's own director."""
    if b.kind != "business" or "TM02" not in b.rules:
        return None
    own_reg = [d for d in b.of("company_register_extract") if name_in(b.name, d.title) or
               same_name(field(d, "company"), b.name)]
    controllers = set()
    for d in own_reg:
        for tb in tables(d.text):
            for r in tb:
                for k in ("officer", "person with significant control", "person"):
                    if r.get(k):
                        controllers.add(norm(r[k]))
    if not controllers:
        return None
    linked = []
    for d in b.of("company_register_extract"):
        if d in own_reg:
            continue
        comp = field(d, "company") or d.title.replace("Company register extract for", "").strip()
        people = {norm(r.get("officer") or r.get("person with significant control") or "")
                  for tb in tables(d.text) for r in tb}
        if people & controllers:
            linked.append((d, comp))
    if not linked:
        return None
    outs = [t for t in b.atx if t.direction == "out" and any(same_name(t.counterparty, c) for _, c in linked)]
    if not outs:
        return None
    ins = [t for t in b.atx if t.direction == "in" and not b.routine_in(t)]
    contracts = b.of("consulting_contract", "contract", "invoice")
    covered = [t for t in ins if any(name_in(t.counterparty, d.text) and has_amount(d.text, t.amount) for d in contracts)]
    if ins and len(covered) == len(ins):
        return None
    director = next(iter(sorted(controllers)))
    dname = director.title()
    inc = []
    for d, comp in linked:
        inc_d = first_date(field(d, "incorporated") or "")
        inc.append(f"{comp} ({d.id}, incorporated {dstr(inc_d)})" if inc_d else f"{comp} ({d.id})")
    s1 = (f"{len(ins)} payments totalling {gbp(sum(t.amount for t in ins))} from "
          f"{', '.join(sorted({t.counterparty for t in ins})[:3])} were paid straight out again: "
          f"{gbp(sum(t.amount for t in outs))} went within hours to {', '.join(inc)}.")
    s2 = f"Both recipients are new companies owned and run by {dname}, the customer's own director."
    s3 = ""
    if contracts:
        c = contracts[0]
        s3 = (f"The only contract on file ({c.id}) is with {field(c, 'client') or 'a different client'} for "
              f"{field(c, 'fee') or 'a small fee'}, and the earlier review covered only that, so nothing explains "
              f"the new receipts or the onward payments.")
    rv = [str(r.get("review_id")) for r in b.ctx.reviews if contracts and
          name_in(field(contracts[0], "client") or "zzz", r.get("note", ""))]
    return b.result("ESCALATE", "ESC_PASS_THROUGH", [s1, s2, s3],
                    [d.id for d, _ in linked] + [c.id for c in contracts] + rv + [t.id for t in ins] + [t.id for t in outs])


# --------------------------------------------------------------------------
# 2. internal list (TM22)
# --------------------------------------------------------------------------

def _list_entry(b: B, a):
    lid = a.measured.get("list_entry") or ""
    for e in b.ctx.folder.list_entries:
        if str(e.get("entry_id")) == str(lid):
            return e
    return None


def _person_rows(b: B, cname: str):
    """(name, born, start, end, source doc, role) for officers and PSCs of a company."""
    rows = []
    for d in b.docs:
        if d.type.lower() not in ("company_register_extract", "significant_control_register"):
            continue
        if cname and not (name_in(cname, d.title) or name_in(cname, d.text)):
            continue
        for tb in tables(d.text):
            for r in tb:
                nm = r.get("officer") or r.get("person") or r.get("person with significant control")
                if not nm:
                    continue
                start = first_date(r.get("appointed") or r.get("notified") or "")
                end = first_date(r.get("resigned") or r.get("ceased") or "")
                role = r.get("role") or r.get("control") or ""
                rows.append((nm, r.get("born") or "", start, end, d, role))
    return rows


def h_list_match(b: B):
    a = b.alert("TM22")
    if a is None or len(b.rules) > 1:
        return None
    e = _list_entry(b, a)
    t = b.atx[0] if b.atx else None
    if e is None or t is None:
        return None
    matched_on = str(a.measured.get("matched_on") or "")
    cp = t.counterparty
    lname = e.get("name") or ""
    ldob = str(e.get("date_of_birth") or "")
    lmy = (int(ldob[:4]), int(ldob[5:7])) if re.match(r"\d{4}-\d{2}", ldob) else None
    rows = _person_rows(b, cp)
    paper = [d for d in b.docs if d.type.lower() in ("invoice", "receipt") and
             (name_in(cp, d.text) or name_in(cp, d.title)) and has_amount(d.text, t.amount)]
    lid = e.get("entry_id")
    reason_l = e.get("reason", "")

    # the listed person appears among the company's officers / controllers
    listed_rows = [r for r in rows if same_name(r[0], lname)]
    if listed_rows:
        active = [r for r in listed_rows if r[3] is None or (t.ts and r[3] >= t.ts.replace(hour=0, minute=0))]
        if active:
            r = max(active, key=lambda r: (r[3] or datetime.max))
            ctrl = [x for x in active if x[4].type.lower() == "significant_control_register" or "share" in x[5].lower()]
            rr = ctrl[0] if ctrl else r
            res = [x for x in listed_rows if x[4].type.lower() == "company_register_extract" and x[3]]
            s1 = (f"{cap(tdesc(t))} matched list entry {lid} for {lname} (born {dstr(datetime.strptime(ldob, '%Y-%m-%d'))[2:].strip() if lmy else 'unknown'}), "
                  f"listed because: {reason_l.rstrip('.')}.")
            if ctrl:
                s2 = (f"Register {rr[4].id} shows {lname} ({rr[1]}) still held '{rr[5]}' of {cp} on the payment date"
                      + (f", ceasing only on {dstr(rr[3])}" if rr[3] else "") + ".")
            else:
                s2 = f"Register {rr[4].id} shows {lname} ({rr[1]}) as {rr[5] or 'an officer'} of {cp} on the payment date."
            s3 = ""
            if res:
                s3 = (f"The resignation as director on {dstr(res[0][3])} ({res[0][4].id}), a day before the payment, "
                      f"does not end that control, so the listed party controls the payee.")
            ev = [x[4].id for x in active] + [x[4].id for x in res] + [d.id for d in paper] + [t.id, lid]
            return b.result("ESCALATE", "ESC_LISTED_PARTY", [s1, s2, s3], ev)
        # the listed person left before the payment
        if paper:
            last = max(r[3] for r in listed_rows)
            others = sorted({r[0] for r in rows if not same_name(r[0], lname)})
            s1 = (f"{cap(tdesc(t))} matched list entry {lid} for {lname}, a former officer of {cp}.")
            s2 = (f"The registers ({', '.join(sorted({r[4].id for r in listed_rows}))}) show {lname} ceased all roles on "
                  f"{dstr(last)}, before the payment, and {cp} is now run by {', '.join(others[:2]) or 'other people'}.")
            s3 = f"The payment matches {paper[0].id} for {gbp(t.amount)}."
            return b.result("CLOSE", "LIST_MATCH_DIFFERENT_PARTY", [s1, s2, s3],
                            [r[4].id for r in listed_rows] + [paper[0].id, t.id, lid])

    # company documents on file and the listed person is not among them
    if rows:
        reg_docs = sorted({r[4].id for r in rows})
        ro_doc = next((r[4] for r in rows if field(r[4], "registered office")), None)
        ro = field(ro_doc, "registered office") if ro_doc else ""
        laddr = e.get("address") or ""
        addr_same = bool(ro) and norm(ro) == norm(laddr)
        dob_clash = any(lmy and month_year(r[1]) == lmy and same_name(r[0], lname) for r in rows)
        if not addr_same and not dob_clash and paper:
            officers = sorted({f"{r[0]} (born {r[1]})" if r[1] else r[0] for r in rows})
            s1 = (f"{cap(tdesc(t))} matched list entry {lid} ({lname}) only on {matched_on}.")
            s2 = (f"Register extract {', '.join(reg_docs)} gives {cp}'s registered office as {ro or 'a different address'}"
                  + (f", not {laddr} where {lname} operated" if laddr else "") +
                  f", and its only officer and controller is {', '.join(officers[:2])}, not {lname}.")
            s3 = f"{paper[0].id} confirms the {gbp(t.amount)} paid for an ordinary purchase from {cp}."
            return b.result("CLOSE", "LIST_MATCH_DIFFERENT_PARTY", [s1, s2, s3],
                            reg_docs + [paper[0].id, t.id, lid])

    # nothing on file settles it either way
    s1 = (f"{cap(tdesc(t))}, referenced '{t.reference}', matched list entry {lid} for {lname} on {matched_on} only; "
          f"the entry reads: {reason_l.rstrip('.')}.")
    s2 = ("A name match alone does not prove it is the same person, and nothing on file shows who the payee is"
          " or confirms or rules out the listed party.")
    s3 = (f"Ask the customer who {cp} is (full name, address and how they know them) and for the invoice or "
          f"quote for the '{t.reference}' work they paid {gbp(t.amount)} for.")
    return b.result("RFI", "RFI_COUNTERPARTY_DETAILS", [s1, s2, s3], [t.id, lid])


# --------------------------------------------------------------------------
# 3. structuring (TM03)
# --------------------------------------------------------------------------

def _record_rows(d):
    rows = []
    for tb in tables(d.text):
        for r in tb:
            vals = list(r.values())
            amt = None
            for k in ("total", "cash counted", "amount", "banked amount"):
                if r.get(k) and doc_amounts(r[k]):
                    amt = doc_amounts(r[k])[0]
                    break
            if amt is None:
                continue
            dates = [x for v in vals for x in parse_dates(v)]
            rows.append({"amt": amt, "dates": dates, "label": vals[0], "row": r})
    return rows


def _procedure_branches(b: B):
    out = {}
    for d in b.of("cash_handling_procedure"):
        for m in re.finditer(r"^([^:\n]+?):[^\n]*?pay it in[^\n]*? at ([^.\n]+? branch)", d.text, re.M | re.I):
            out[norm(m.group(1))] = (m.group(2).strip(), d)
    return out


def h_structuring(b: B):
    a = b.alert("TM03")
    if a is None:
        return None
    deps = [t for t in b.ctx.folder.txns.values() if t.id in a.txn_ids]
    deps.sort(key=lambda t: (t.ts or datetime.min, t.id))
    recs = [d for d in b.docs if d.type.lower() in ("cash_collection_reports", "cash_reconciliation",
                                                     "takings_report", "till_report", "collection_report")]
    matched, used, problems = [], set(), []
    for t in deps:
        hit = None
        for d in recs:
            for i, r in enumerate(_record_rows(d)):
                if (d.id, i) in used or abs(r["amt"] - t.amount) > 0.01:
                    continue
                if r["dates"] and t.ts and not any(timedelta(0) <= t.ts.replace(hour=0, minute=0) - x <= timedelta(days=3)
                                                   for x in r["dates"]):
                    continue
                hit = (d, i, r)
                break
            if hit:
                break
        if hit:
            used.add((hit[0].id, hit[1]))
            matched.append((t, hit[0], hit[2]))
    proc = _procedure_branches(b)
    for t, d, r in matched:
        site = norm(r["label"])
        if site in proc and location(t) and norm(proc[site][0]) != norm(location(t)):
            problems.append(f"{t.id} was paid in at {location(t)}, not at {proc[site][0]} as the procedure says")
    events = b.of("ticket_report", "event_report")
    for ev_d in events:
        ds = parse_dates(body(ev_d))
        if ds and deps and deps[0].ts and max(ds) > deps[0].ts:
            problems.append(f"{ev_d.id} dates the event after the deposits")
    total = sum(t.amount for t in deps)
    if deps and len(matched) == len(deps) and not problems:
        docs = sorted({d.id for _, d, _ in matched})
        d0 = matched[0][1]
        s1 = (f"Alert {a.id} flagged {len(deps)} cash deposits just under £10,000.00 totalling {gbp(total)} between "
              f"{dstr(deps[0].ts)} and {dstr(deps[-1].ts)}.")
        parts = [f"{gbp(t.amount)} ({r['label']})" for t, _, r in matched[:4]]
        s2 = (f"{', '.join(docs)} records each one separately, counted by staff and banked as takings: "
              f"{', '.join(parts)}, so the amounts are the business's real takings, not split cash.")
        s3 = ""
        if proc:
            pd = next(iter(proc.values()))[1]
            s3 = f"Procedure {pd.id} says each site banks its own takings at its own branch, which is what the deposits show."
        elif events:
            s3 = f"{events[0].id} confirms the event ran just before the takings were banked."
        rv = [str(r.get("review_id")) for r in b.ctx.reviews if "TM03" == r.get("rule_id")]
        return b.result("CLOSE", "BUSINESS_RECORDS_RECONCILE", [s1, s2, s3],
                        docs + [p[1].id for p in proc.values()][:1] + [e.id for e in events] + rv +
                        [t.id for t in deps])
    s1 = (f"Alert {a.id} flagged {len(deps)} cash deposits each just under £10,000.00, totalling {gbp(total)}, "
          f"between {dstr(deps[0].ts) if deps else ''} and {dstr(deps[-1].ts) if deps else ''}"
          f" at {len({location(t) for t in deps})} branches.")
    if recs:
        s2 = (f"The records on file ({', '.join(d.id for d in recs)}) reconcile only {len(matched)} of the "
              f"{len(deps)} deposits" + (f"; {problems[0]}" if problems else "") + ".")
    else:
        s2 = "No takings or collection records on file account for the deposits, which sit just below the threshold."
    return b.result("ESCALATE", "ESC_STRUCTURING", [s1, s2], [d.id for d in recs] + [t.id for t in deps])


# --------------------------------------------------------------------------
# 4. explaining individual transactions
# --------------------------------------------------------------------------

def _near(doc_dt, t, before_days=60, after_days=14):
    if doc_dt is None or t.ts is None:
        return True
    delta = (t.ts.replace(hour=0, minute=0) - doc_dt).days
    return -after_days <= delta <= before_days


def explain_in(b: B, t):
    """(code, [doc ids], sentence) or None."""
    amt = t.amount
    # solicitor / estate
    for d in b.of("solicitor_letter", "completion_statement", "probate"):
        firm = d.title.replace("Letter from", "").strip()
        if has_amount(d.text, amt) and (name_in(firm, t.counterparty) or norm(firm) in norm(t.counterparty)) \
                and _near(d.dated, t):
            est = re.search(r"estate of (?:the late )?([A-Z][\w'-]+ [A-Z][\w'-]+)", d.text)
            return ("DOCUMENTED_EVENT", [d.id],
                    f"{d.id} from {firm} confirms an inheritance of {gbp(amt)}"
                    + (f" from the estate of {est.group(1)}" if est else "") +
                    f", paid from the firm's client account as {tdesc(t)}")
    # insurer settlement
    for d in b.of("insurer_letter", "insurance_settlement", "claim_letter"):
        ins = d.title.split(" from ")[-1].strip() if " from " in d.title else ""
        if has_amount(d.text, amt) and (name_in(ins, t.counterparty) or name_in(t.counterparty, d.text)) \
                and _near(d.dated, t) and _addressed_to_customer(b, d):
            claim = re.search(r"CLAIM \d+", d.text)
            return ("DOCUMENTED_EVENT", [d.id],
                    f"{d.id} from {ins or t.counterparty} confirms a {gbp(amt)} claim settlement"
                    + (f" ({claim.group(0)})" if claim else "") + f" to {b.name}, received as {tdesc(t)}")
    # loan
    for d in b.of("loan_agreement"):
        lender = field(d, "lender") or ""
        borrower = field(d, "borrower") or ""
        if has_amount(d.text, amt) and same_name(lender, t.counterparty) and same_name(borrower, b.name) \
                and _near(d.dated, t, 30, 3):
            purpose = field(d, "loan purpose", "stated purpose", "purpose") or ""
            return ("DOCUMENTED_EVENT", [d.id],
                    f"loan agreement {d.id} with {lender} for {gbp(amt)}"
                    + (f" ('{purpose.lower()}')" if purpose else "") + f" explains {tdesc(t)}")
    # own account elsewhere
    st = _statement_for(b, t)
    if st:
        return ("OWN_ACCOUNT", [st.id],
                f"statement {st.id} from {field(st, 'bank')} shows account {re.sub(r'^account ', '', field(st, 'account') or '')} is held by "
                f"{b.name} and sent {tdesc(t)}")
    # refund from a venue / supplier of an earlier payment
    for d in b.docs:
        if not re.search(r"refund", d.text, re.I) or not has_amount(d.text, amt):
            continue
        if not (name_in(t.counterparty, d.text) or name_in(t.counterparty, d.title)):
            continue
        orig = [o for o in b.ctx.txns if o.direction == "out" and same_name(o.counterparty, t.counterparty)
                and o.ts and t.ts and o.ts < t.ts and o.amount + 0.01 >= amt]
        rv = [r for r in b.ctx.reviews if name_in(t.counterparty, r.get("note", ""))]
        if orig:
            o = orig[-1]
            return ("REFUND_OF_EARLIER_PAYMENT", [d.id, o.id] + [str(r.get("review_id")) for r in rv],
                    f"{d.id} from {t.counterparty} confirms a refund of {gbp(amt)}"
                    f"{' (' + t.reference + ')' if t.reference else ''}, returning the deposit of "
                    f"{gbp(o.amount)} the customer paid on {dstr(o.ts)} ({o.id})"
                    + (f", which earlier review {rv[0].get('review_id')} saw against the booking contract" if rv else ""))
    # cash from a private vehicle sale
    if is_cash_in(t):
        for d in b.of("vehicle_sale_receipt", "sale_receipt"):
            seller = field(d, "seller") or ""
            reg = re.search(r"registration ([A-Z]{2}\d{2} ?[A-Z]{3})", d.text)
            owned = [i for i in b.of("insurance_schedule", "vehicle_registration", "v5c")
                     if reg and reg.group(1) in i.text and name_in(b.name, i.text)]
            if has_amount(d.text, amt) and name_in(b.name, seller) and reg and owned and _near(d.dated, t, 7, 0):
                buyer = field(d, "buyer") or "the buyer"
                return ("DOCUMENTED_EVENT", [d.id, owned[0].id],
                        f"receipt {d.id} records {buyer} paying {b.name} {gbp(amt)} in cash for the car "
                        f"{reg.group(1)} on {dstr(d.dated)}, and insurance schedule {owned[0].id} shows the customer "
                        f"owned that car, which explains {tdesc(t)}")
        # re-deposit of cash withdrawn earlier
        wds = [o for o in b.ctx.txns if is_cash_out(o) and abs(o.amount - amt) <= 0.01 and o.ts and t.ts
               and timedelta(0) < t.ts - o.ts <= timedelta(days=200)]
        chats = [c for c in b.of("support_chat", "contact_note")
                 if has_amount(c.text, amt) and re.search(r"withdr[ae]w", c.text, re.I)]
        if wds and chats:
            o = wds[-1]
            rv = [r for r in b.ctx.reviews if r.get("rule_id") == "TM05" and
                  (lambda d: d is not None and o.ts and timedelta(0) <= d - o.ts.replace(hour=0, minute=0) <= timedelta(days=20))(
                      _to_dt(r.get("reviewed_on")))]
            why = ""
            m = re.search(r"(car (?:purchase|sale)|purchase|sale)[^.]*fell through", chats[0].text, re.I)
            if m:
                why = "; the customer says the private car purchase fell through"
            return ("DOCUMENTED_EVENT", [chats[0].id, o.id] + [str(r.get("review_id")) for r in rv],
                    f"{tdesc(t)} is the same {gbp(amt)} the customer withdrew in cash on {dstr(o.ts)} ({o.id})"
                    + (f", reviewed and closed in {rv[0].get('review_id')} as cash to buy a car privately" if rv else "")
                    + f"; chat {chats[0].id} confirms it is that cash being paid back in{why}")
    return None


def _addressed_to_customer(b: B, d) -> bool:
    """A letter naming its addressee must name the customer; an unnamed one ('Dear Client') is accepted."""
    m = re.search(r"^Dear ([^,\n]+),", d.text, re.M)
    who = m.group(1).strip() if m else ""
    if who and who.lower() not in ("client", "customer", "beneficiary", "sir or madam", "sir/madam", "policyholder"):
        return same_name(who, b.name)
    if name_in(b.name, d.text):
        return True
    payee = re.search(r"\b(?:paid|payment of £[\d,.]+|issued|transferred)\b[^.\n]*?\bto ([A-Z][a-z]+ [A-Z][a-z]+)\b", d.text)
    if payee and not re.search(r"\b(Ltd|plc|LLP|Bank)\b", payee.group(1)):
        return False
    return "your" in d.text.lower()


def _to_dt(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _statement_for(b: B, t):
    for d in b.of("bank_statement"):
        holder = field(d, "account holder") or ""
        acc = field(d, "account") or ""
        if not same_name(holder, b.name):
            continue
        acc_ok = (t.cp_account and norm(t.cp_account) == norm(acc)) or \
                 (acct_tail(acc) and acct_tail(acc) == acct_tail(t.counterparty or t.cp_account)
                  and "ending" in acc.lower())
        if acc_ok and has_amount(d.text, t.amount) and same_name(t.counterparty.split(" account")[0], b.name):
            return d
    return None


def explain_out(b: B, t):
    amt = t.amount
    st = _statement_for(b, t)
    if st:
        return ("OWN_ACCOUNT", [st.id],
                f"statement {st.id} from {field(st, 'bank')} shows {tdesc(t)} arriving in {b.name}'s own savings "
                f"account {field(st, 'account')}")
    for d in b.of("dealer_invoice", "invoice", "hospital_invoice"):
        payee = (d.title.split(" from ")[-1].strip() if " from " in d.title else "") or field(d, "pay to") or ""
        payee = re.sub(r"^Invoice\s+\S+\s+", "", payee)
        pay_to = field(d, "pay to") or ""
        if not has_amount(d.text, amt):
            continue
        if not (same_name(payee, t.counterparty) or name_in(payee, t.counterparty) or
                name_in(pay_to.split(",")[0], t.counterparty)):
            continue
        if not _near(d.dated, t, 30, 3):
            continue
        bill = field(d, "customer", "bill to") or ""
        patient = field(d, "patient") or ""
        if d.type.lower() == "hospital_invoice" or patient:
            return ("INVOICE_MATCHES", [d.id],
                    f"invoice {d.id} from {payee or pay_to} for {patient or 'the patient'}'s "
                    f"{(field(d, 'treatment') or 'treatment').lower()} is for exactly {gbp(amt)}, matching {tdesc(t)}")
        if bill and not same_name(bill, b.name):
            continue
        item = field(d, "vehicle", "description") or ""
        return ("INVOICE_MATCHES", [d.id],
                f"invoice {d.id} from {payee} to {b.name}" + (f" for a {item}" if item else "") +
                f" matches {tdesc(t)} on amount, payee and date")
    for d in b.of("booking_confirmation"):
        acc = re.search(r"account (\d{2}-\d{2}-\d{2} \d{8})", d.text)
        if has_amount(d.text, amt) and (name_in(t.counterparty, d.title) or name_in(t.counterparty, d.text)) and \
                (not acc or not t.cp_account or acc.group(1) == t.cp_account):
            return ("COLLECTION_DOCUMENTED", [d.id], f"{tdesc(t)} pays booking {d.id}")
    return None


# --------------------------------------------------------------------------
# 4b. case-level patterns that close
# --------------------------------------------------------------------------

def h_group_collection(b: B):
    """Friends paying their share of a group booking that the customer then pays."""
    if "TM04" not in b.rules and "TM02" not in b.rules:
        return None
    ins = [t for t in b.atx if t.direction == "in" and not b.routine_in(t)]
    outs = [t for t in b.atx if t.direction == "out"]
    if len(ins) < 3:
        return None
    small = all(t.amount <= 1000 for t in ins)
    trip = sum(1 for t in ins if re.search(r"trip|holiday|booking|travel", t.reference or "", re.I)
               or (t.reference and len({x.reference.split()[0].lower() for x in ins if x.reference}) <= 4))
    if not small:
        return None
    bookings = b.of("booking_confirmation")
    pay = max([t for t in outs if t.channel != "card"] or [None], key=lambda t: t.amount if t else 0)
    for d in bookings:
        trav = re.findall(r"^\s*\d+\.\s+(.+?)\s*$", d.text, re.M)
        pp = re.search(r"£([\d,]+\.\d\d) per person", d.text)
        pp = float(pp.group(1).replace(",", "")) if pp else None
        listed = [t for t in ins if any(same_name(t.counterparty, x) for x in trav)]
        right = [t for t in listed if pp is None or abs(t.amount - pp) <= 0.01]
        acc = re.search(r"account (\d{2}-\d{2}-\d{2} \d{8})", d.text)
        paid = pay is not None and has_amount(d.text, pay.amount) and \
            (acc is None or not pay.cp_account or acc.group(1) == pay.cp_account)
        lead = field(d, "lead traveller", "lead passenger", "booked by", "customer")
        if lead and not same_name(lead, b.name):
            paid = False
        if len(right) == len(ins) and paid:
            ref = field(d, "booking reference") or ""
            tripd = field(d, "trip") or "a group trip"
            s1 = (f"{len(ins)} people each paid {gbp(pp) if pp else 'their share'} into the account between "
                  f"{dstr(ins[0].ts)} and {dstr(ins[-1].ts)}, and {tdesc(pay)} then paid booking {ref}.")
            s2 = (f"Booking confirmation {d.id} from {pay.counterparty} ({tripd}) names {b.name} as lead traveller "
                  f"and lists every payer among its {len(trav)} travellers at {gbp(pp) if pp else 'the per-person price'}"
                  f" each, totalling {gbp(pay.amount)} to account {acc.group(1) if acc else pay.cp_account}.")
            s3 = "The money in and out is a documented collection for named people, not other people's money passing through."
            return b.result("CLOSE", "COLLECTION_DOCUMENTED", [s1, s2, s3], [d.id, pay.id] + [t.id for t in ins])
        if bookings and (len(right) < len(ins) or not paid):
            missing = [t for t in ins if t not in right]
            s1 = (f"{len(ins)} people paid {gbp(sum(t.amount for t in ins))} in and {tdesc(pay) if pay else 'money'} went out, "
                  f"but booking {d.id} does not support it: {len(missing)} payer(s) are not on the traveller list or paid "
                  f"a different amount" + ("" if paid else ", and the outgoing payment does not match the booking") + ".")
            s2 = "Third parties are funding the account without a documented reason."
            return b.result("ESCALATE", "ESC_THIRD_PARTY_FUNDING", [s1, s2],
                            [d.id] + [t.id for t in missing[:6]] + ([pay.id] if pay else []))
    if bookings or pay is None or not trip:
        return None
    # same pattern, no booking on file -> ask for it
    refs = Counter(re.sub(r"\b[A-Z]{2,}\b$", "", (t.reference or "")).strip() for t in ins).most_common(1)
    amts = Counter(t.amount for t in ins).most_common(1)
    s1 = (f"{len(ins)} different people paid {gbp(amts[0][0]) if amts else ''} each"
          f" ({gbp(sum(t.amount for t in ins))} in total, references such as '{ins[0].reference}') between "
          f"{dstr(ins[0].ts)} and {dstr(ins[-1].ts)}, and {tdesc(pay)} referenced '{pay.reference}' followed within hours.")
    s2 = ("It looks like the customer collecting friends' shares of a group trip, which is not suspicious, but no "
          "booking is on file to show it.")
    s3 = (f"Ask the customer for the booking confirmation from {pay.counterparty} for {pay.reference} showing the "
          f"price, the payment account and the list of travellers who paid them.")
    return b.result("RFI", "RFI_PURPOSE_DOCUMENTS", [s1, s2, s3], [pay.id] + [t.id for t in ins])


def h_explained(b: B):
    """Close when every alerted (non-routine) transaction is explained by a matching document."""
    subj = [t for t in b.atx if not b.routine(t)]
    if not subj:
        return None
    exps, unexplained = [], []
    for t in subj:
        e = b.explain_cache(t)
        if e:
            exps.append((t, e))
        else:
            unexplained.append(t)
    if unexplained or not exps:
        return None
    codes = [e[0] for _, e in exps]
    evid = []
    for _, e in exps:
        evid += e[1]
    evid += [t.id for t, _ in exps]
    mult = b.multiple()
    sentences = []
    a1 = b.alert("TM01")
    if a1 is not None:
        sentences.append(f"Alert {a1.id} flagged {gbp(_f(a1.measured.get('received_gbp')))} received in 30 days, "
                         f"{mult:.1f} times the declared monthly income of {gbp(b.income)}." if mult else "")
    for t, e in exps[:3]:
        sentences.append(cap(e[2]) + ".")

    # policy: own-account transfers explaining activity at 5x or more -> updated profile
    if "OWN_ACCOUNT" in codes and mult is not None and mult >= 5:
        own = [t for t, e in exps if e[0] == "OWN_ACCOUNT"]
        s = [sentences[0] if sentences else "",
             f"{cap(exps[0][1][2])}, so the money is the customer's own.",
             f"But it takes activity to {mult:.1f} times the declared income, at or above the 5 times policy limit for "
             f"own-account transfers.",
             f"Ask the customer for an updated expected monthly income and account activity, with the reason for "
             f"moving {gbp(sum(t.amount for t in own))} into this account."]
        return b.result("RFI", "RFI_UPDATED_PROFILE", s, evid)

    # dormant account: a verified customer coming back
    if "TM07" in b.rules:
        rv = [d for d in b.of("reverification_record", "identity_reverification")
              if re.search(r"re-?verified", d.text, re.I) and not re.search(r"(fail|not match|could not|new device)", d.text, re.I)]
        a7 = b.alert("TM07")
        if rv and "OWN_ACCOUNT" in codes:
            note = re.search(r"The customer said ([^.]+)\.", rv[0].text)
            s = [f"Alert {a7.id} flagged {gbp(_f(a7.measured.get('moved_gbp')))} moving after "
                 f"{a7.measured.get('dormant_days')} dormant days.",
                 f"Identity re-verification {rv[0].id} on {dstr(first_date(rv[0].text) or rv[0].dated)} passed with a "
                 f"live selfie on the customer's registered device" + (f"; the customer said {note.group(1)}" if note else "") + ".",
                 cap(exps[0][1][2]) + ", so the money is the customer's own savings."]
            return b.result("CLOSE", "RETURNING_CUSTOMER", s, [rv[0].id] + evid)
        if "OWN_ACCOUNT" in codes and not rv:
            return None   # own-name account but the customer has not been re-verified
        if "DOCUMENTED_EVENT" not in codes:
            return None
        sentences.insert(1, f"Alert {a7.id} flagged the account waking after {a7.measured.get('dormant_days')} dormant days.")

    order = ["COLLECTION_DOCUMENTED", "REFUND_OF_EARLIER_PAYMENT", "OWN_ACCOUNT", "DOCUMENTED_EVENT", "INVOICE_MATCHES"]
    code = Counter(codes).most_common()
    code = sorted(code, key=lambda x: (-x[1], order.index(x[0]) if x[0] in order else 99))[0][0]
    if "DOCUMENTED_EVENT" in codes and "INVOICE_MATCHES" in codes:
        code = "DOCUMENTED_EVENT"
    if any(t.direction == "in" and e[0] == "REFUND_OF_EARLIER_PAYMENT" for t, e in exps):
        code = "REFUND_OF_EARLIER_PAYMENT"
    if "TM06" in b.rules:
        hr = [t for t in b.atx if b.is_hr(t.cp_country)]
        if hr:
            sentences.append(f"The {hr[0].cp_country} link is therefore explained by the documents, not unexplained "
                             f"higher-risk activity.")
    sentences = [s for s in sentences if s][:4]
    return b.result("CLOSE", code, sentences, evid)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

# --------------------------------------------------------------------------
# 4c. documents that contradict the activity -> suspicion
# --------------------------------------------------------------------------

def h_contradictions(b: B):
    subj = [t for t in b.atx if not b.routine(t)]
    for t in subj:
        if b.explain_cache(t):
            continue
        # cash said to be a re-deposit of an earlier withdrawal that does not exist
        if is_cash_in(t):
            chats = [c for c in b.of("support_chat", "contact_note")
                     if re.search(r"withdr[ae]w", c.text, re.I) and re.search(r"same cash|back into|paid (it )?back|re-?deposit",
                                                                              c.text, re.I)]
            if chats:
                wds = [o for o in b.ctx.txns if is_cash_out(o) and o.ts and t.ts and o.ts < t.ts]
                s1 = (f"{cap(tdesc(t))} is described in {chats[0].id} as cash the customer had withdrawn earlier and "
                      f"kept at home.")
                s2 = (f"The account shows no earlier cash withdrawal of {gbp(t.amount)}" +
                      (f" (the only withdrawals are {', '.join(gbp(o.amount) + ' on ' + dstr(o.ts) for o in wds[:2])})"
                       if wds else "") + ", so the explanation is contradicted by the transactions.")
                return b.result("ESCALATE", "ESC_UNEXPLAINED_FUNDS", [s1, s2],
                                [chats[0].id, t.id] + [o.id for o in wds[:2]])
            for d in b.of("vehicle_sale_receipt", "sale_receipt"):
                bad = []
                if not has_amount(d.text, t.amount):
                    bad.append(f"it is for {_and([gbp(x) for x in doc_amounts(d.text)[:1]]) or 'a different amount'}, not {gbp(t.amount)}")
                if not name_in(b.name, field(d, "seller") or ""):
                    bad.append(f"the seller is {field(d, 'seller') or 'someone else'}, not the customer")
                reg = re.search(r"registration ([A-Z]{2}\d{2} ?[A-Z]{3})", d.text)
                owned = [i for i in b.of("insurance_schedule", "vehicle_registration", "v5c")
                         if reg and reg.group(1) in i.text and name_in(b.name, i.text)]
                if not owned:
                    bad.append("nothing on file shows the customer owned the car it describes")
                if d.dated and t.ts and d.dated > t.ts:
                    bad.append(f"it is dated {dstr(d.dated)}, after the deposit")
                if bad:
                    s1 = f"{cap(tdesc(t))} is said to be the proceeds of a private car sale, receipt {d.id}."
                    s2 = "The receipt does not support it: " + "; ".join(bad) + "."
                    return b.result("ESCALATE", "ESC_UNEXPLAINED_FUNDS", [s1, s2],
                                    [d.id] + [i.id for i in b.of("insurance_schedule")] + [t.id])
        # an invoice or agreement for this counterparty that does not match the payment
        for d in b.of("dealer_invoice", "invoice", "hospital_invoice", "loan_agreement"):
            named = name_in(t.counterparty, d.text) or name_in(t.counterparty, d.title) or \
                (t.reference and t.reference in d.title)
            if not named:
                continue
            bad = []
            if not has_amount(d.text, t.amount):
                got = doc_amounts(d.text)
                bad.append(f"it is for {gbp(got[0]) if got else 'a different amount'}, not {gbp(t.amount)}")
            who = field(d, "customer", "bill to", "borrower")
            if who and not same_name(who, b.name) and d.type.lower() != "hospital_invoice":
                bad.append(f"it is made out to {who}, not the customer")
            if d.dated and t.ts and (t.ts - d.dated).days < -3:
                bad.append(f"it is dated {dstr(d.dated)}, after the payment")
            if bad:
                kind = "loan agreement" if "loan" in d.type.lower() else "invoice"
                s1 = f"{cap(tdesc(t))} is said to be covered by {kind} {d.id}."
                s2 = f"The {kind} does not support the payment: " + "; ".join(bad) + "."
                return b.result("ESCALATE", "ESC_INVOICE_MISMATCH", [s1, s2], [d.id, t.id])
        # a statement for this account in somebody else's name
        for d in b.of("bank_statement"):
            acc = field(d, "account") or ""
            holder = field(d, "account holder") or ""
            if acc and t.cp_account and norm(acc) == norm(t.cp_account) and holder and not same_name(holder, b.name):
                s1 = f"{cap(tdesc(t))} is presented as a transfer between the customer's own accounts."
                s2 = (f"But statement {d.id} shows account {acc} is held by {holder}, not by {b.name}, so a third party "
                      f"is moving money through the account.")
                return b.result("ESCALATE", "ESC_THIRD_PARTY_FUNDING", [s1, s2], [d.id, t.id])
    # dormant account woken up by someone who failed re-verification
    if "TM07" in b.rules:
        for d in b.of("reverification_record", "identity_reverification"):
            if re.search(r"fail|did not match|could not be (verified|matched)|new device|different device|unregistered",
                         d.text, re.I):
                a7 = b.alert("TM07")
                s1 = (f"Alert {a7.id} flagged {gbp(_f(a7.measured.get('moved_gbp')))} moving after "
                      f"{a7.measured.get('dormant_days')} dormant days.")
                s2 = (f"Re-verification record {d.id} does not confirm the customer is the person using the account "
                      f"(it records a failed check or a new device), so someone else may be controlling it.")
                return b.result("ESCALATE", "ESC_ACCOUNT_MISUSE", [s1, s2], [d.id] + [t.id for t in subj])
    return None


def h_hr_unexplained(b: B):
    hr = [t for t in b.atx if b.is_hr(t.cp_country) and b.undeclared(t.cp_country) and not b.explain_cache(t)]
    if not hr:
        return None
    total = sum(t.amount for t in hr)
    c = hr[0].cp_country
    s1 = (f"{len(hr)} payment{'s' if len(hr) > 1 else ''} totalling {gbp(total)} moved to or from {c}, a higher-risk "
          f"country the customer did not declare, including {tdesc(hr[0])}.")
    s2 = "No invoice, contract or other document on file explains the payments or the relationship with the counterparty."
    return b.result("ESCALATE", "ESC_HIGH_RISK_CORRIDOR", [s1, s2], [t.id for t in hr])


# --------------------------------------------------------------------------
# 5. not explained, not suspicious -> RFI
# --------------------------------------------------------------------------

def h_rfi(b: B):
    subj = [t for t in b.atx if not b.routine(t)]
    ins = [t for t in subj if t.direction == "in"]
    outs = [t for t in subj if t.direction == "out"]
    if not ins:
        return None
    big = max(ins, key=lambda t: t.amount)
    mult = b.multiple()
    lead = ""
    a1 = b.alert("TM01")
    if a1 is not None and mult:
        lead = (f"Alert {a1.id} flagged {gbp(_f(a1.measured.get('received_gbp')))} received in 30 days, "
                f"{mult:.1f} times the declared monthly income of {gbp(b.income)}.")
    # high-risk / undeclared onward payment of unexplained money -> escalate rather than ask
    hr_out = [t for t in outs if b.is_hr(t.cp_country)]
    if hr_out:
        return None
    ref = (big.reference or "").upper()

    # inheritance with no solicitor's letter
    m = re.match(r"ESTATE OF (.+)", ref)
    if m:
        est = m.group(1).title()
        s2 = (f"{cap(tdesc(big))} is referenced 'ESTATE OF {m.group(1)}' and comes from a solicitors' client "
              f"account, which points to an inheritance, but no letter from the solicitors or the executors is on file.")
        s3 = (f"Ask the customer for the letter from {big.counterparty.replace(' client account', '')} (or the estate "
              f"accounts) confirming their entitlement of {gbp(big.amount)} from the estate of {est}.")
        return b.result("RFI", "RFI_SOURCE_OF_FUNDS", [lead, s2, s3], [big.id])

    # loan paid straight to a dealer with no loan agreement / invoice
    if "LOAN" in ref and outs:
        o = max(outs, key=lambda t: t.amount)
        s2 = (f"{cap(tdesc(big))} referenced '{big.reference}' was followed by {tdesc(o)} referenced "
              f"'{o.reference}', which looks like a car bought with a personal loan.")
        s3 = (f"Nothing on file shows it: ask the customer for the loan agreement with {big.counterparty} for "
              f"{gbp(big.amount)} and the {o.counterparty} invoice {o.reference.replace('INV ', '') if o.reference else ''} "
              f"for the vehicle.")
        return b.result("RFI", "RFI_SOURCE_OF_FUNDS", [lead, s2, s3], [big.id, o.id])

    # money from an own-name account elsewhere that no document shows the customer holds
    if same_name(big.counterparty.split(" account")[0], b.name):
        a7 = b.alert("TM07")
        s1 = (f"Alert {a7.id} flagged {gbp(_f(a7.measured.get('moved_gbp')))} moving after "
              f"{a7.measured.get('dormant_days')} days without activity." if a7 else lead)
        where = f" in {big.cp_country}" if big.cp_country and big.cp_country != "United Kingdom" else " elsewhere"
        s2 = (f"{cap(tdesc(big))} comes from an account{where} in the customer's own name, but no statement on file "
              f"shows the customer holds it" + (" and the customer has not been re-verified since the account went dormant"
                                               if a7 and not b.of("reverification_record") else "") + ".")
        s3 = (f"Ask the customer for a recent statement of the account{' ending ' + acct_tail(big.counterparty) if acct_tail(big.counterparty) else ''}"
              f" in their name showing the {gbp(big.amount)} transfer and where the savings came from.")
        return b.result("RFI", "RFI_SOURCE_OF_FUNDS", [s1, s2, s3], [big.id])

    # cash with only the customer's word for it
    if is_cash_in(big):
        a5 = b.alert("TM05")
        chat = [c for c in b.of("support_chat", "contact_note") if re.search(r"cash|paid in|deposit", c.text, re.I)
                and (c.dated is None or big.ts is None or abs((c.dated - big.ts.replace(hour=0, minute=0)).days) <= 5)]
        claim = ""
        if chat:
            mm = re.search(r"It'?s from ([^,.]+)", chat[0].text) or re.search(r"from (selling [^,.]+)", chat[0].text)
            claim = mm.group(1).strip() if mm else ""
        s1 = (f"Alert {a5.id} flagged {tdesc(big)}, above the £10,000.00 cash limit for individuals" if a5 else cap(tdesc(big))) + \
             (f", against declared income of {gbp(b.income)} a month." if b.income else ".")
        if chat and claim:
            s2 = (f"In chat {chat[0].id} the customer says the cash is from '{claim}', but nothing on file supports "
                  f"that and there is no earlier withdrawal it could be.")
            s3 = (f"Ask the customer for evidence of the sale: a signed receipt or bill of sale naming the buyer, "
                  f"the date and {gbp(big.amount)}, and the registration document showing they owned the vehicle.")
        else:
            s2 = "Nothing on file says where the cash came from."
            s3 = f"Ask the customer for proof of where the {gbp(big.amount)} in cash came from."
        return b.result("RFI", "RFI_SOURCE_OF_FUNDS", [s1, s2, s3], [c.id for c in chat] + [big.id])

    # generic
    s2 = f"Nothing on file explains {tdesc(big)}, and nothing about it suggests crime."
    s3 = (f"Ask the customer for documents showing where the {gbp(big.amount)} from {big.counterparty or 'the payer'} "
          f"came from and what it was for.")
    return b.result("RFI", "RFI_SOURCE_OF_FUNDS", [lead, s2, s3], [big.id] + [t.id for t in outs[:2]])


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def h_last(b: B):
    subj = [t for t in b.atx if not b.routine(t)] or b.atx
    if not subj:
        return None
    rules = ", ".join(sorted(b.rules))
    big = max(subj, key=lambda t: t.amount)
    if "TM02" in b.rules:
        s1 = f"The alerts ({rules}) flagged money moving straight through the account, including {tdesc(big)}."
        s2 = "Nothing on file explains where it came from or why it was moved on so quickly."
        return b.result("ESCALATE", "ESC_UNEXPLAINED_FUNDS", [s1, s2], [t.id for t in subj])
    s1 = f"The alerts ({rules}) flagged {tdesc(big)} and {len(subj) - 1} other transaction(s) that nothing on file explains."
    s2 = (f"Ask the customer for documents showing what the payments to and from {big.counterparty or 'the counterparty'} "
          f"were for, such as invoices or contracts.")
    return b.result("RFI", "RFI_PURPOSE_DOCUMENTS", [s1, s2], [t.id for t in subj])


HANDLERS = [
    h_fraud_reports,
    h_threshold_questions,
    h_dissolved_payer,
    h_conflicting_cash_story,
    h_hr_third_party,
    h_pass_through_linked,
    h_list_match,
    h_structuring,
    h_group_collection,
    h_explained,
    h_contradictions,
    h_hr_unexplained,
    h_rfi,
    h_last,
]


def decide(ctx, alerts):
    if not alerts:
        return None
    b = B(ctx, alerts)
    for h in HANDLERS:
        out = h(b)
        if out is not None:
            out["_handler"] = h.__name__
            return out
    return None
