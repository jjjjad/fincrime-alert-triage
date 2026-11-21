"""Scenario logic for the investment institution (see scenarios/__init__.py).

A UK broker / fund platform: customers fund the account from their own bank
account, buy and sell fund units and withdraw to an account in their own name.
Everything below is driven by POLICY.md and by what the documents on file show
about the specific transactions the alerts fired on:

1. Internal-list matches (TM22) are settled by comparing the listed party's
   date of birth / address / identifiers with the documents on file: a match
   escalates, a difference closes (if the rest is explained), nothing on file
   is an RFI for the counterparty's details.
2. Signs of crime escalate before anything else (no RFI: tipping off):
   payers that are dissolved companies, fund units bought and sold back with
   the proceeds paid to other people, other people's money received and
   passed on, unrelated payers in higher-risk countries investing "through"
   the account, payouts to third parties.
3. Otherwise every non-routine transaction must be explained by a document
   that matches it on party, amount and date (completion statement, insurer
   letter, gift letter, bank statement of an account the customer holds, ISA
   cancellation confirmation, redundancy letter, re-verification record).
   A matching document closes; a document that contradicts the payment
   escalates; a missing document is an RFI for exactly that document.
   Own-account transfers explaining activity at 5x declared income or more are
   an RFI for an updated expected income (policy setting).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from ..data import fmt_gbp, norm_name

ROUTINE_DOCS = {"due_diligence_summary", "proof_of_address"}
MONTHS = ("january february march april may june july august september october november december").split()

_GBP = re.compile(r"£\s?(\d[\d,]*(?:\.\d{1,2})?)")
_DATE_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)? (%s) (\d{4})|(\d{4})-(\d{2})-(\d{2})|(\d{1,2})/(\d{1,2})/(\d{4})"
                      % "|".join(MONTHS), re.I)
_DOB_CUE = re.compile(r"\b(born|date of birth|d\.?o\.?b\.?)\b", re.I)
_DISSOLVED = re.compile(r"\b(dissolved|struck off|in liquidation|liquidated|ceased trading|strike[- ]off)\b", re.I)
_ON_BEHALF = re.compile(r"(invest(ing)? (for|on behalf of)|through mine|through my account|for them|on their behalf|"
                        r"cannot open an account|can't open an account|for (a few|some) (people|friends)|"
                        r"friends? (sent|gave)|money back|sending it on|send(ing)? it back)", re.I)
_MISTAKE = re.compile(r"(accident|mistak|in error|by error|wrong account|incorrect(ly)? transfer|intended for|reversal|"
                      r"\breverse\b|return of (these|the) funds)", re.I)
_SALE_REF = re.compile(r"(sale proceeds|completion|conveyanc|solicitor)", re.I)
_GIFT_REF = re.compile(r"\bgift", re.I)
_CLAIM_REF = re.compile(r"(claim|insur|assurance|settlement|payout)", re.I)
_CANCEL_REF = re.compile(r"(cancel|cooling)", re.I)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _n(s) -> str:
    return norm_name(s)


def _day(dt: datetime | None) -> str:
    return f"{dt.day} {dt.strftime('%B %Y')}" if dt else "an unknown date"


def _amounts(text: str) -> list[float]:
    return [float(m.group(1).replace(",", "")) for m in _GBP.finditer(text or "")]


def _has_amount(text: str, amt: float, tol: float = 0.02) -> bool:
    return any(abs(a - amt) <= tol for a in _amounts(text))


def _parse_date(m: re.Match) -> datetime | None:
    try:
        if m.group(1):
            return datetime(int(m.group(3)), MONTHS.index(m.group(2).lower()) + 1, int(m.group(1)))
        if m.group(4):
            return datetime(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        if m.group(7):
            return datetime(int(m.group(9)), int(m.group(8)), int(m.group(7)))
    except ValueError:
        return None
    return None


def _dobs(text: str) -> list[datetime]:
    """Dates of birth written in a document (a date shortly after 'born' / 'DOB')."""
    out = []
    for cue in _DOB_CUE.finditer(text or ""):
        seg = text[cue.end(): cue.end() + 40]
        m = _DATE_RE.search(seg)
        if m:
            d = _parse_date(m)
            if d and d not in out:
                out.append(d)
    return out


def _to_date(v) -> datetime | None:
    if not v:
        return None
    m = _DATE_RE.search(str(v))
    return _parse_date(m) if m else None


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _acct_match(doc_acct: str, txn_acct: str) -> bool:
    a, b = _digits(doc_acct), _digits(txn_acct)
    if not a or not b:
        return False
    if a == b:
        return True
    tail = min(len(a), len(b), 8)
    return tail >= 4 and (a.endswith(b[-tail:]) or b.endswith(a[-tail:])) and min(len(a), len(b)) <= 8


def _body(doc) -> str:
    return doc.text or ""


def _mentions(doc, name: str) -> bool:
    n = _n(name)
    return bool(n) and n in _n(f"{doc.title} {doc.text}")


def _date_ok(doc, t, slack_days: int = 1) -> bool:
    """The document is dated on or before the payment (a little slack for same-day paperwork)."""
    if doc.dated is None or t.ts is None:
        return True
    return doc.dated.date() <= (t.ts + timedelta(days=slack_days)).date()


def _sentences(parts: list[str], limit: int = 4) -> str:
    out = []
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        p = p[0].upper() + p[1:]
        if not p.endswith("."):
            p += "."
        out.append(p)
    return " ".join(out[:limit])


def _res(verdict, code, parts, evidence):
    ev = []
    for i in evidence:
        if i and i not in ev:
            ev.append(i)
    return {"verdict": verdict, "reason_code": code, "reason": _sentences(parts), "evidence": ev}


# --------------------------------------------------------------------------
# case features
# --------------------------------------------------------------------------

class _C:
    def __init__(self, ctx, alerts):
        self.ctx = ctx
        self.alerts = alerts
        self.rules = {}
        for a in alerts:
            self.rules.setdefault(a.rule, a)
        self.cust = ctx.customer
        self.name = ctx.customer.name
        self.docs = [d for d in ctx.docs]
        self.special = [d for d in self.docs if d.type not in ROUTINE_DOCS]
        alert_ids = {i for a in alerts for i in a.txn_ids}
        seen = set()
        txns = []
        for t in ctx.win + [ctx.folder.txns[i] for i in alert_ids if i in ctx.folder.txns]:
            if t.id not in seen:
                seen.add(t.id)
                txns.append(t)
        txns.sort(key=lambda t: (t.ts or datetime.min, t.id))
        self.txns = txns
        self.alert_txn_ids = alert_ids
        self.flow_txn_ids = {i for a in alerts if a.rule != "TM10" for i in a.txn_ids}
        xfer = [t for t in txns if t.channel == "transfer"]
        self.ins = [t for t in xfer if t.direction == "in" and not self.routine(t)]
        self.outs = [t for t in xfer if t.direction == "out"]
        self.tp_in = [t for t in self.ins if not self.own(t)]
        self.tp_out = [t for t in self.outs if not self.own(t)]
        self.sells = [t for t in txns if t.channel == "fund_sell"]

    def own(self, t) -> bool:
        a, b = _n(t.counterparty), _n(self.name)
        return bool(a and b) and a == b

    def routine(self, t) -> bool:
        """Regular contributions from the customer's linked account (no other account named)."""
        return self.own(t) and not t.cp_account and t.direction == "in"

    def of_type(self, *types) -> list:
        return [d for d in self.special if d.type in types]

    def text_docs(self) -> list:
        return [d for d in self.special if d.type in ("support_chat", "customer_email", "customer_letter",
                                                      "contact_note")] or []

    def high_risk(self, country: str) -> bool:
        return bool(country) and country.strip().lower() in self.ctx.high_risk

    def declared(self, country: str) -> bool:
        return any(country.strip().lower() == c.strip().lower() for c in self.cust.countries)

    def tm01_multiple(self) -> float | None:
        a = self.rules.get("TM01")
        if not a:
            return None
        m = a.measured.get("multiple")
        try:
            return float(m)
        except (TypeError, ValueError):
            return None

    def surname(self) -> str:
        parts = self.name.split()
        return parts[-1] if parts else ""


def _desc(t) -> str:
    verb = "received from" if t.direction == "in" else "paid to"
    return f"{fmt_gbp(t.amount)} {verb} {t.counterparty or 'an unnamed account'} on {_day(t.ts)} ({t.id})"


def _chat_quote(c: _C) -> tuple[str, list[str]]:
    for d in c.of_type("support_chat", "customer_email"):
        m = re.search(r"\*\*Customer:\*\*\s*(.+)", d.text)
        if m:
            q = m.group(1).strip().rstrip(".")
            if len(q) > 180:
                q = q[:177].rsplit(" ", 1)[0] + "..."
            return f"in {d.id} the customer said \"{q}\"", [d.id]
    return "", []


# --------------------------------------------------------------------------
# documents that explain a single transaction
# --------------------------------------------------------------------------

def _statement_holders(doc) -> list[str]:
    f = doc.fields
    raw = f.get("account holders") or f.get("account holder") or f.get("account name") or ""
    return [x.strip() for x in re.split(r",| and | & ", raw) if x.strip()]


def _statement_for(c: _C, t) -> tuple[object | None, str]:
    """A bank statement on file that shows the customer holds the account behind t."""
    mismatch = None
    for d in c.of_type("bank_statement"):
        acct = d.fields.get("account", "")
        holders = _statement_holders(d)
        holder_ok = any(_n(h) == _n(c.name) for h in holders)
        acct_ok = _acct_match(acct, t.cp_account) or (t.cp_account and t.cp_account in d.text)
        if not acct_ok:
            continue
        if holder_ok:
            return d, ""
        mismatch = d
    if mismatch is not None:
        return None, mismatch.id
    return None, ""


def _completion_for(c: _C, t):
    for d in c.of_type("completion_statement"):
        client = d.fields.get("client", "")
        if client and _n(client) != _n(c.name):
            return d, f"{d.id} is a completion statement for {client}, not the customer"
        paid = d.fields.get("paid to client's investment account") or ""
        amt_ok = _has_amount(paid, t.amount) if paid else _has_amount(d.text, t.amount)
        firm = re.sub(r"\s+client account$", "", t.counterparty, flags=re.I)
        firm_ok = _mentions(d, firm) or not firm
        if not amt_ok:
            return d, f"{d.id} shows {paid or 'a different amount'} paid to the customer, not {fmt_gbp(t.amount)}"
        if not firm_ok:
            return d, f"{d.id} was issued by a different firm from the payer {t.counterparty}"
        if not _date_ok(d, t, 3):
            return d, f"{d.id} is dated {_day(d.dated)}, after the payment"
        return d, ""
    return None, ""


def _insurer_for(c: _C, t):
    for d in c.of_type("insurer_letter"):
        if not (_mentions(d, t.counterparty) or (t.reference and t.reference.lower() in d.text.lower())):
            continue
        if not _has_amount(d.text, t.amount):
            return d, f"{d.id} confirms a different settlement amount from the {fmt_gbp(t.amount)} received"
        if not _mentions(d, c.name) and "to you" not in d.text.lower():
            return d, f"{d.id} does not name the customer as the payee"
        if not _date_ok(d, t, 3):
            return d, f"{d.id} is dated after the payment"
        return d, ""
    return None, ""


def _gift_donor(d) -> str:
    m = re.search(r"\bI,\s+([A-Z][\w'-]+(?: [A-Z][\w'-]+)+)", d.text)
    return m.group(1).strip() if m else ""


def _gift_for(c: _C, t):
    letters = c.of_type("gift_letter")
    for d in letters:
        donor = _gift_donor(d)
        if donor and _n(donor) != _n(t.counterparty) and not _mentions(d, t.counterparty):
            continue
        if not _has_amount(d.text, t.amount):
            return d, f"{d.id} from {donor or t.counterparty} gives a different amount from the {fmt_gbp(t.amount)} received"
        if not _mentions(d, c.name):
            return d, f"{d.id} does not name the customer as the recipient"
        if not _date_ok(d, t, 1):
            return d, f"{d.id} is dated {_day(d.dated)}, after the payment"
        return d, ""
    if letters:
        d = letters[0]
        return d, f"{d.id} is signed by {_gift_donor(d) or 'someone else'}, not by the payer {t.counterparty}"
    return None, ""


def _relationship(d, name: str) -> str:
    m = re.search(r"to my (\w+)", d.text)
    return m.group(1) if m else ""


def _cancellation_for(c: _C, t):
    for d in c.of_type("cancellation_confirmation"):
        if not _has_amount(d.text, t.amount):
            continue
        if t.cp_account and not (t.cp_account in d.text or _acct_match(d.text, t.cp_account)):
            return d, f"{d.id} returns the subscription to a different account from {t.cp_account}"
        return d, ""
    return None, ""


def _mistake_email(c: _C, t):
    for d in c.of_type("customer_email", "support_chat", "customer_letter"):
        if _MISTAKE.search(d.text) and _has_amount(d.text, t.amount):
            return d
    return None


# --------------------------------------------------------------------------
# 1. internal list matches
# --------------------------------------------------------------------------

def _list_entry(c: _C, a):
    eid = str(a.measured.get("list_entry") or "")
    if not eid:
        m = re.search(r"\b([A-Z]{2}-L\d+)\b", str(a.raw.get("message", "")))
        eid = m.group(1) if m else ""
    for e in c.ctx.folder.list_entries:
        if str(e.get("entry_id") or e.get("id")) == eid:
            return eid, e
    return eid, None


def _listed(c: _C):
    a = c.rules.get("TM22")
    if a is None:
        return None
    eid, entry = _list_entry(c, a)
    party = str(a.measured.get("matched_party") or (entry or {}).get("name") or "")
    on = str(a.measured.get("matched_on") or "name")
    party_txns = [c.ctx.folder.txns[i] for i in a.txn_ids if i in c.ctx.folder.txns] or \
        [t for t in c.txns if _n(t.counterparty) == _n(party)]
    tdesc = "; ".join(_desc(t) for t in party_txns[:2]) or f"activity with {party}"
    reason_txt = (entry or {}).get("reason", "")
    reason_short = reason_txt.split(":")[0].strip().lower() if reason_txt else "an earlier internal case"

    pdocs = [d for d in c.docs if d.type not in ROUTINE_DOCS and _mentions(d, party)]
    confirm, differ = [], []
    l_dob = _to_date((entry or {}).get("date_of_birth"))
    l_addr = str((entry or {}).get("address") or "")
    l_ids = [str(v) for v in ((entry or {}).get("identifiers") or {}).values() if v]
    for d in pdocs:
        if l_dob:
            for dob in _dobs(d.text):
                (confirm if dob.date() == l_dob.date() else differ).append(
                    (d, f"{d.id} gives {party}'s date of birth as {_day(dob)}, "
                        f"{'the same as' if dob.date() == l_dob.date() else 'not'} the {_day(l_dob)} on list entry {eid}"))
        if l_addr:
            first = l_addr.split(",")[0].strip().lower()
            if first and first in d.text.lower():
                confirm.append((d, f"{d.id} gives {party}'s address as {l_addr}, the address on list entry {eid}"))
        for ident in l_ids:
            if ident and ident in d.text:
                confirm.append((d, f"{d.id} shows identifier {ident}, which is on list entry {eid}"))

    rest = _escalations(c) or _explain(c, skip_rule="TM22")
    if confirm:
        d, why = confirm[0]
        return _res("ESCALATE", "ESC_LISTED_PARTY",
                    [f"{tdesc} matched list entry {eid} ({party}, {reason_short}) on {on}",
                     why + ", so the counterparty is the listed party",
                     "The customer is not asked for more, to avoid tipping off"],
                    [d.id, eid] + [t.id for t in party_txns])
    if differ:
        d, why = differ[0]
        lead = [f"{tdesc} matched list entry {eid} ({party}) on {on} only", why + ", so it is a different person"]
        if rest and rest["verdict"] == "CLOSE":
            return _res("CLOSE", "LIST_MATCH_DIFFERENT_PARTY", lead + [rest["reason"].split(". ")[0]],
                        [d.id, eid] + rest["evidence"] + [t.id for t in party_txns])
        if rest:
            rest = dict(rest)
            rest["reason"] = _sentences([lead[1]] + [rest["reason"]], 4)
            rest["evidence"] = rest["evidence"] + [d.id, eid]
            return rest
    if rest and rest["verdict"] == "ESCALATE":
        rest = dict(rest)
        rest["evidence"] = rest["evidence"] + [eid]
        return rest
    ev = [eid] + [t.id for t in party_txns] + [d.id for d in pdocs]
    return _res("RFI", "RFI_COUNTERPARTY_DETAILS",
                [f"{tdesc} matched list entry {eid} ({party}, {reason_short}) on {on} alone, which does not "
                 f"prove it is the same person",
                 f"Nothing on file gives {party}'s date of birth or address, so the match can be neither "
                 f"confirmed nor ruled out, and nothing else in the case is concerning",
                 f"Ask the customer for {party}'s full name, date of birth and address, how they are related to "
                 f"the customer and what the {fmt_gbp(party_txns[0].amount) if party_txns else ''} payment was for"],
                ev)


# --------------------------------------------------------------------------
# 2. signs of crime -> escalate
# --------------------------------------------------------------------------

def _escalations(c: _C):
    quote, qids = _chat_quote(c)

    # a. payer is a company the register shows as dissolved
    for d in c.special:
        status = d.fields.get("status", "")
        company = d.fields.get("company", "") or d.fields.get("company name", "")
        if not company or not _DISSOLVED.search(status):
            continue
        paid = [t for t in c.tp_in if _n(t.counterparty) == _n(company)]
        if not paid:
            continue
        total = sum(t.amount for t in paid)
        onward = [t for t in c.tp_out if paid[0].ts and t.ts and t.ts >= paid[0].ts]
        refs = sorted({t.reference for t in paid if t.reference})
        parts = [f"{len(paid)} payment{'s' if len(paid) > 1 else ''} totalling {fmt_gbp(total)} came from "
                 f"{company}{' with reference ' + '/'.join(refs) if refs else ''}, but register extract {d.id} "
                 f"shows the company {status[0].lower() + status[1:]}"]
        if quote:
            parts.append(f"The explanation does not fit the payer: {quote}")
        if onward:
            parts.append(f"{fmt_gbp(sum(t.amount for t in onward))} was then paid on to "
                         f"{', '.join(sorted({t.counterparty for t in onward}))} ({', '.join(t.id for t in onward)})")
        return _res("ESCALATE", "ESC_UNEXPLAINED_FUNDS", parts,
                    [d.id] + qids + [t.id for t in paid] + [t.id for t in onward])

    # b. fund units bought and sold back, proceeds paid to other people
    paid_on = []
    for s in c.sells:
        for o in c.tp_out:
            if s.ts and o.ts and timedelta(0) <= o.ts - s.ts <= timedelta(days=5) and abs(o.amount - s.amount) <= max(
                    1.0, s.amount * 0.02):
                paid_on.append((s, o))
    if paid_on and ("TM10" in c.rules or c.tp_in):
        hist = [t for t in c.ctx.txns if t.channel == "fund_sell"]
        outs3 = [o for o in c.ctx.txns if o.channel == "transfer" and o.direction == "out" and not c.own(o)]
        ins3 = [t for t in c.ctx.txns if t.channel == "transfer" and t.direction == "in" and not c.own(t)
                and not c.routine(t)]
        a10 = c.rules.get("TM10")
        lead = ""
        if a10:
            m = a10.measured
            lead = (f"Units in {m.get('fund', 'a fund')} were sold back after {m.get('held_days', '?')} days "
                    f"({a10.id}) and the {fmt_gbp(paid_on[-1][0].amount)} proceeds were paid to "
                    f"{paid_on[-1][1].counterparty} ({paid_on[-1][1].id})")
        else:
            lead = f"Fund proceeds {_desc(paid_on[-1][0])} were paid on to {paid_on[-1][1].counterparty}"
        parts = [lead,
                 f"The account shows {len(hist)} quick buy-and-sell cycles funded by {len({t.counterparty for t in ins3})} "
                 f"different third parties and paid out to {len({o.counterparty for o in outs3})} others"
                 if len(hist) > 1 else
                 "The units were funded by a third party and the proceeds went to someone other than the customer"]
        if quote:
            parts.append(f"Fund units are being used to move other people's money: {quote}")
        ev = qids + [paid_on[-1][0].id, paid_on[-1][1].id] + [t.id for t in ins3[-3:]] + [o.id for o in outs3[-3:]]
        return _res("ESCALATE", "ESC_EARLY_REDEMPTION", parts, ev)

    # c. unrelated payers in higher-risk countries investing through the account
    hr_in = [t for t in c.tp_in if c.high_risk(t.cp_country) and not c.declared(t.cp_country)]
    if hr_in:
        explained = [t for t in hr_in if _explains_tp_in(c, t)[0] == "ok"]
        hr_in = [t for t in hr_in if t not in explained]
    if hr_in:
        countries = sorted({t.cp_country for t in hr_in})
        payers = sorted({t.counterparty for t in hr_in})
        nominee = any(_ON_BEHALF.search(d.text) for d in c.text_docs())
        parts = [f"{len(hr_in)} payment{'s' if len(hr_in) > 1 else ''} totalling {fmt_gbp(sum(t.amount for t in hr_in))} "
                 f"came from {', '.join(payers)} in {', '.join(countries)}, a higher-risk country the customer did "
                 f"not declare ({', '.join(t.id for t in hr_in)})"]
        if nominee:
            parts.append(f"Other people are using the customer's account to invest: {quote}" if quote else
                         "The customer says other people are investing through the account")
            return _res("ESCALATE", "ESC_ACCOUNT_MISUSE", parts, qids + [t.id for t in hr_in])
        parts.append("Nothing on file explains who the payers are or why they are funding the account")
        return _res("ESCALATE", "ESC_HIGH_RISK_CORRIDOR", parts, qids + [t.id for t in hr_in])

    # d. other people's money received and passed on
    if c.tp_in and c.tp_out:
        unexpl = [t for t in c.tp_in if _explains_tp_in(c, t)[0] != "ok"]
        first = min((t.ts for t in unexpl if t.ts), default=None)
        onward = [o for o in c.tp_out if first and o.ts and o.ts >= first]
        if unexpl and onward:
            payers = sorted({t.counterparty for t in unexpl})
            tin = sum(t.amount for t in unexpl)
            tout = sum(o.amount for o in onward)
            parts = [f"{fmt_gbp(tin)} came in from {', '.join(payers)} ({', '.join(t.id for t in unexpl[:4])}) and "
                     f"{fmt_gbp(tout)} was paid out to {', '.join(sorted({o.counterparty for o in onward}))} "
                     f"({', '.join(o.id for o in onward[:3])}) within days, with almost nothing invested"]
            if quote:
                parts.append(f"The account is passing on other people's money: {quote}")
            code = "ESC_MULE_ACTIVITY" if len(payers) >= 2 or (quote and _ON_BEHALF.search(quote)) else "ESC_PASS_THROUGH"
            return _res("ESCALATE", code, parts, qids + [t.id for t in unexpl] + [o.id for o in onward])

    # e. payouts to someone other than the customer
    if c.tp_out:
        outs = [o for o in c.tp_out if o.id in c.alert_txn_ids] or c.tp_out
        parts = [f"{'; '.join(_desc(o) for o in outs[:3])} went to an account not in the customer's name, although "
                 f"withdrawals should go to the customer's own bank account",
                 f"Nothing on file shows who {outs[0].counterparty} is or why they are being paid"]
        if quote:
            parts.append(quote)
        return _res("ESCALATE", "ESC_THIRD_PARTY_FUNDING", parts, qids + [o.id for o in outs])
    return None


# --------------------------------------------------------------------------
# 3. explanations per transaction
# --------------------------------------------------------------------------

def _explains_tp_in(c: _C, t):
    """('ok'|'mismatch'|'missing', code_or_rfi_code, doc, text)."""
    ref = t.reference or ""
    for fn, code in ((_completion_for, "DOCUMENTED_EVENT"), (_insurer_for, "DOCUMENTED_EVENT"),
                     (_gift_for, "THIRD_PARTY_DOCUMENTED")):
        d, bad = fn(c, t)
        if d is not None and not bad:
            return "ok", code, d, ""
        if d is not None and bad:
            return "mismatch", code, d, bad
    st, bad_id = _statement_for(c, t)
    if st is not None:
        return "ok", "OWN_ACCOUNT", st, ""
    if bad_id:
        return "mismatch", "OWN_ACCOUNT", c.ctx.folder.docs.get(bad_id), \
            f"statement {bad_id} for the paying account does not show the customer as an account holder"
    # nothing on file: decide what to ask for
    cp = t.counterparty or ""
    if _SALE_REF.search(ref) or _SALE_REF.search(cp):
        return "missing", "RFI_SOURCE_OF_FUNDS", None, \
            (f"the completion statement from {re.sub(r' client account$', '', cp, flags=re.I)} for the sale that "
             f"produced the {fmt_gbp(t.amount)} paid on {_day(t.ts)}, showing the customer as the seller and the net "
             f"proceeds paid to this account")
    if _GIFT_REF.search(ref):
        return "missing", "RFI_SOURCE_OF_FUNDS", None, \
            (f"a signed gift letter from {cp} confirming the {fmt_gbp(t.amount)} gift of {_day(t.ts)}, their "
             f"relationship to the customer and where the money came from")
    if _CLAIM_REF.search(ref) or _CLAIM_REF.search(cp):
        return "missing", "RFI_SOURCE_OF_FUNDS", None, \
            f"the settlement letter from {cp} for the {fmt_gbp(t.amount)} paid on {_day(t.ts)}"
    sur = c.surname().lower()
    if "&" in cp or " and " in cp.lower() or (sur and sur in cp.lower()):
        return "missing", "RFI_SOURCE_OF_FUNDS", None, \
            (f"a recent statement for the account {cp} ({t.cp_account or 'paying account'}) showing the customer is "
             f"one of its holders, as it sent {fmt_gbp(t.amount)} on {_day(t.ts)}")
    return "missing", "RFI_COUNTERPARTY_DETAILS", None, \
        f"who {cp} is, their relationship to the customer and what the {fmt_gbp(t.amount)} paid on {_day(t.ts)} was for"


def _explain(c: _C, skip_rule: str | None = None):
    found = []      # (code, doc_ids, txn, text)
    rfis = []       # (code, ask, txn, evidence)
    bad = []        # (code, doc, txn, text)
    own_used = False

    # third-party payers
    for t in c.tp_in:
        st, code, d, txt = _explains_tp_in(c, t)
        if st == "ok":
            found.append((code, [d.id], t, _ok_text(c, code, d, t)))
            own_used |= code == "OWN_ACCOUNT"
        elif st == "mismatch":
            bad.append((code, d, t, txt))
        else:
            rfis.append((code, txt, t, [t.id]))

    # money from accounts in the customer's own name elsewhere
    own_in = [t for t in c.ins if c.own(t)]
    refunded = set()
    for t in own_in:
        back = [o for o in c.outs if c.own(o) and o.ts and t.ts and o.ts >= t.ts and abs(o.amount - t.amount) <= 0.01
                and (not o.cp_account or not t.cp_account or o.cp_account == t.cp_account)]
        if back:
            o = back[0]
            refunded.add(o.id)
            canc, cbad = _cancellation_for(c, t)
            if canc is not None and not cbad:
                found.append(("REFUND_OF_EARLIER_PAYMENT", [canc.id, o.id], t,
                              f"{canc.id} confirms the ISA was cancelled in the cooling-off period and the "
                              f"{fmt_gbp(t.amount)} subscription received on {_day(t.ts)} ({t.id}) was returned to "
                              f"the same account in the customer's name on {_day(o.ts)} ({o.id})"))
                continue
            if canc is not None and cbad:
                bad.append(("REFUND_OF_EARLIER_PAYMENT", canc, t, cbad))
                continue
            em = _mistake_email(c, t)
            st, _ = _statement_for(c, t)
            if em is not None and st is not None:
                own_used = True
                found.append(("OWN_ACCOUNT", [st.id, em.id, o.id], t,
                              f"{em.id} asked for the {fmt_gbp(t.amount)} sent in error on {_day(t.ts)} ({t.id}) to be "
                              f"returned, and statement {st.id} shows the customer holds account {t.cp_account} that "
                              f"sent it and received it back on {_day(o.ts)} ({o.id})"))
                continue
            if st is not None:
                own_used = True
                found.append(("OWN_ACCOUNT", [st.id], t,
                              f"statement {st.id} shows the {fmt_gbp(t.amount)} ({t.id}) came from and went back to "
                              f"the customer's own account {t.cp_account}"))
                continue
            if _CANCEL_REF.search(o.reference or "") or _CANCEL_REF.search(t.reference or ""):
                rfis.append(("RFI_PURPOSE_DOCUMENTS",
                             f"the ISA cancellation confirmation or cancellation request for the {fmt_gbp(t.amount)} "
                             f"subscription received on {_day(t.ts)} ({t.id}) and returned on {_day(o.ts)} ({o.id}), "
                             f"as no cancellation record is on file", t, [t.id, o.id]))
            else:
                rfis.append(("RFI_PURPOSE_DOCUMENTS",
                             f"why the {fmt_gbp(t.amount)} paid in on {_day(t.ts)} ({t.id}) was withdrawn again on "
                             f"{_day(o.ts)} ({o.id}) without being invested, with a statement for account "
                             f"{t.cp_account}", t, [t.id, o.id]))
            continue
        st, bad_id = _statement_for(c, t)
        if st is not None:
            own_used = True
            found.append(("OWN_ACCOUNT", [st.id], t,
                          f"statement {st.id} from {st.fields.get('bank', 'the other bank')} shows the customer holds "
                          f"the account {st.fields.get('account', t.cp_account)} that sent {fmt_gbp(t.amount)} on "
                          f"{_day(t.ts)} ({t.id})"))
        elif bad_id:
            bad.append(("OWN_ACCOUNT", c.ctx.folder.docs.get(bad_id), t,
                        f"statement {bad_id} for account {t.cp_account} is not in the customer's name"))
        elif t.id in c.flow_txn_ids:
            rfis.append(("RFI_SOURCE_OF_FUNDS",
                         f"a recent statement for the account {t.cp_account or ''} at the sending bank, in the "
                         f"customer's name, showing the {fmt_gbp(t.amount)} sent on {_day(t.ts)} ({t.id}) and where "
                         f"that money came from", t, [t.id]))

    # withdrawals to the customer's own name abroad (higher-risk corridor)
    for o in c.outs:
        if not c.own(o) or o.id in refunded:
            continue
        if c.high_risk(o.cp_country) and not c.declared(o.cp_country):
            st, bad_id = _statement_for(c, o)
            letters = [d for d in c.of_type("customer_letter", "customer_email", "employment_contract")
                       if _date_ok(d, o, 0) and (o.cp_account and _digits(o.cp_account)[-4:] in d.text or True)]
            if st is not None:
                own_used = True
                why = f" and {letters[0].id} explains the move" if letters else ""
                found.append(("OWN_ACCOUNT", [st.id] + [d.id for d in letters[:1]], o,
                              f"statement {st.id} from {st.fields.get('bank', 'the bank')} in {o.cp_country} shows the "
                              f"customer holds the account {st.fields.get('account', o.cp_account)} that received "
                              f"{fmt_gbp(o.amount)} on {_day(o.ts)} ({o.id}){why}"))
            else:
                rfis.append(("RFI_SOURCE_OF_FUNDS",
                             f"a statement for the account {o.cp_account} in {o.cp_country} showing the customer holds "
                             f"it and received the {fmt_gbp(o.amount)} sent on {_day(o.ts)}", o, [o.id]))

    # own-named inflows from higher-risk countries are covered by the statement logic above; add context
    hr_note = ""
    for code, ids, t, txt in found:
        if t is not None and t.direction == "in" and c.high_risk(t.cp_country) and not c.declared(t.cp_country):
            emp = [d for d in c.of_type("employment_contract", "customer_letter") if _date_ok(d, t, 0)]
            if emp:
                ids.append(emp[0].id)
                hr_note = (f"{emp[0].id} shows the customer works in {t.cp_country}, which explains money from there")

    # early redemption with the proceeds to the customer
    a10 = c.rules.get("TM10")
    if a10 is not None:
        sells = [c.ctx.folder.txns[i] for i in a10.txn_ids if i in c.ctx.folder.txns
                 and c.ctx.folder.txns[i].channel == "fund_sell"] or c.sells
        s = sells[-1] if sells else None
        m = a10.measured
        red = [d for d in c.of_type("redundancy_letter") if s is None or _date_ok(d, s, 0)]
        red_ok = [d for d in red if not c.cust.raw.get("employer") or _mentions(d, c.cust.raw.get("employer"))]
        req = [d for d in c.of_type("customer_email", "support_chat", "customer_letter")
               if re.search(r"(redundan|sell|redeem|withdraw)", d.text, re.I)]
        out_own = [o for o in c.outs if c.own(o) and s is not None and o.ts and s.ts and o.ts >= s.ts]
        if red_ok:
            d = red_ok[0]
            found.append(("DOCUMENTED_EVENT", [d.id] + [x.id for x in req[:1]] + ([s.id] if s else [])
                          + [o.id for o in out_own[:1]],
                          s or (c.txns[0] if c.txns else None),
                          f"{d.id} from {c.cust.raw.get('employer') or 'the employer'} confirms the customer was made "
                          f"redundant{' and ' + req[0].id + ' asked to sell the fund to cover living costs' if req else ''}, "
                          f"which explains selling {m.get('fund', 'the units')} after {m.get('held_days', '?')} days "
                          f"for {fmt_gbp(s.amount) if s else ''}"
                          + (f" and withdrawing it to the customer's own account ({out_own[0].id})" if out_own else "")))
        elif red:
            d = red[0]
            bad.append(("DOCUMENTED_EVENT", d, s, f"{d.id} is from a different employer from the customer's"))
        else:
            rfis.append(("RFI_PURPOSE_DOCUMENTS",
                         f"why the {m.get('fund', 'fund')} units bought on {_day(_to_date(a10.raw.get('message', '')))} "
                         f"were sold back after {m.get('held_days', '?')} days for "
                         f"{fmt_gbp(s.amount) if s else fmt_gbp(m.get('redeemed_gbp'))}, with any supporting document",
                         s, [x for x in [s.id if s else None] + [o.id for o in out_own[:1]] if x]))

    # dormant reactivation
    a07 = c.rules.get("TM07")
    reverif = c.of_type("reverification_record")
    if a07 is not None and not bad and not rfis and found:
        if reverif and any(code == "OWN_ACCOUNT" for code, *_ in found):
            r = reverif[0]
            code0, ids0, t0, txt0 = found[0]
            return _res("CLOSE", "RETURNING_CUSTOMER",
                        [f"The account was dormant for {a07.measured.get('dormant_days', '?')} days before "
                         f"{fmt_gbp(t0.amount)} arrived on {_day(t0.ts)} ({t0.id})",
                         f"{r.id} re-verified the customer's identity on the registered device"
                         + (f" ({_short(r.text)})" if _short(r.text) else ""),
                         txt0],
                        [r.id] + ids0 + [t0.id])

    # combine
    if bad:
        code, d, t, txt = bad[0]
        esc = {"THIRD_PARTY_DOCUMENTED": "ESC_THIRD_PARTY_FUNDING", "OWN_ACCOUNT": "ESC_THIRD_PARTY_FUNDING",
               "DOCUMENTED_EVENT": "ESC_UNEXPLAINED_FUNDS", "REFUND_OF_EARLIER_PAYMENT": "ESC_UNEXPLAINED_FUNDS"}
        return _res("ESCALATE", esc.get(code, "ESC_UNEXPLAINED_FUNDS"),
                    [f"{_desc(t)} is not supported by the document on file: {txt}" if t else txt,
                     "A document that contradicts the payment is a sign the explanation is not genuine, so the "
                     "customer is not asked again"],
                    ([d.id] if d else []) + ([t.id] if t else []))
    if rfis:
        code, ask, t, ev = rfis[0]
        what = _alert_summary(c, skip_rule)
        parts = [f"{what}" if what else "",
                 _nothing_text(c, code, t),
                 f"Ask the customer for {ask}"]
        return _res("RFI", code, parts, ev + [d.id for d in c.special if d.type in ("support_chat", "customer_email")][:1])
    if not found:
        return None

    # own-account money at 5x declared income or more -> updated profile
    mult = c.tm01_multiple()
    if own_used and mult is not None and mult >= 5.0 and all(code == "OWN_ACCOUNT" for code, *_ in found):
        code, ids, t, txt = found[0]
        inc = c.cust.expected_monthly_in
        return _res("RFI", "RFI_UPDATED_PROFILE",
                    [txt,
                     f"The account received {fmt_gbp(c.rules['TM01'].measured.get('received_gbp'))} in 30 days, "
                     f"{mult:.1f} times the declared monthly income of {fmt_gbp(inc)}; policy asks for an updated "
                     f"profile when own-account transfers explain activity at 5 times or more",
                     "Ask the customer for an updated expected monthly income and expected deposit volume, with the "
                     "source of the savings being moved"],
                    ids + [t.id])

    order = ["DOCUMENTED_EVENT", "THIRD_PARTY_DOCUMENTED", "REFUND_OF_EARLIER_PAYMENT", "OWN_ACCOUNT"]
    found.sort(key=lambda f: (-(f[2].amount if f[2] is not None else 0)))
    main = found[0]
    code = main[0]
    parts = [main[3]]
    for f in found[1:2]:
        parts.append(f[3])
    if hr_note:
        parts.append(hr_note)
    parts.append(_covered_text(c))
    ev = []
    for f in found:
        ev += f[1]
    for f in found:
        if f[2] is not None:
            ev.append(f[2].id)
    return _res("CLOSE", code, parts, ev)


def _short(text: str) -> str:
    m = re.search(r"(The customer said[^.]*)\.", text)
    return (m.group(1)[0].lower() + m.group(1)[1:]) if m else ""


def _ok_text(c: _C, code: str, d, t) -> str:
    if d.type == "completion_statement":
        prop = d.fields.get("property sold", "a property")
        return (f"completion statement {d.id} for the sale of {prop} shows net proceeds of {fmt_gbp(t.amount)} paid "
                f"to {c.name}'s investment account, matching the payment from {t.counterparty} on {_day(t.ts)} ({t.id})")
    if d.type == "insurer_letter":
        return (f"{d.id} from {t.counterparty} confirms a settled claim ({t.reference}) of {fmt_gbp(t.amount)} payable "
                f"to {c.name}, matching the payment on {_day(t.ts)} ({t.id})")
    if d.type == "gift_letter":
        rel = _relationship(d, c.name)
        return (f"gift letter {d.id}, dated {_day(d.dated)}, from {t.counterparty}"
                f"{' to their ' + rel if rel else ''} {c.name} covers the {fmt_gbp(t.amount)} gift received on "
                f"{_day(t.ts)} ({t.id}), with no repayment or interest retained")
    if d.type == "bank_statement":
        holders = d.fields.get("account holders") or d.fields.get("account holder") or c.name
        return (f"statement {d.id} shows account {d.fields.get('account', t.cp_account)} ({t.counterparty}) is held by "
                f"{holders}, so the {fmt_gbp(t.amount)} received on {_day(t.ts)} ({t.id}) came from the customer's own account")
    return f"{d.id} explains {_desc(t)}"


def _alert_summary(c: _C, skip_rule: str | None) -> str:
    parts = []
    for a in c.alerts:
        if a.rule == skip_rule:
            continue
        msg = str(a.raw.get("message", "")).split(". The limit")[0].split(". The account holder")[0]
        if msg:
            parts.append(f"{a.id} ({a.rule}): {msg}")
    return "; ".join(parts[:2])


def _nothing_text(c: _C, code: str, t) -> str:
    if code == "RFI_PURPOSE_DOCUMENTS":
        return ("The money stayed in the customer's own name and nothing suggests crime, but nothing on file explains "
                "the activity")
    if code == "RFI_COUNTERPARTY_DETAILS":
        return f"Nothing on file shows who {t.counterparty if t else 'the counterparty'} is, and nothing else is concerning"
    return "Nothing is suspicious yet, but no document on file shows where the money came from"


def _covered_text(c: _C) -> str:
    rules = sorted(c.rules)
    return f"This explains all the activity behind the alerts ({', '.join(rules)})"


# --------------------------------------------------------------------------

def decide(ctx, alerts):
    if not alerts:
        return None
    c = _C(ctx, alerts)
    return _listed(c) or _escalations(c) or _explain(c)
