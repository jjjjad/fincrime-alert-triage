"""Scenario logic for the emoney institution (see scenarios/__init__.py).

The e-money policy (institutions/emoney/POLICY.md) drives every branch:
  * escalate on signs of crime first (scam reports / mule chat, structuring
    questions, listed party in control, dissolved-company "loans", shared
    devices with pass-through, unexplained higher-risk corridor flows);
  * close only when a document on file matches the alerted activity on amount,
    party and date (takings book, sale receipt, settlement/insurer letter,
    hospital invoice, venue refund email, booking confirmation, deposit letters,
    own-account statements, internal-list disproof);
  * own-account transfers at 5x the declared income or more -> RFI for an
    updated profile;
  * when the same activity appears with nothing on file -> RFI naming the
    exact document to ask for.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from ..data import fmt_gbp, norm_name

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

_GBP = re.compile(r"£\s?(\d[\d,]*(?:\.\d{1,2})?)")
_LONG_DATE = re.compile(r"\b(\d{1,2} (?:January|February|March|April|May|June|July|August|September|"
                        r"October|November|December) \d{4})\b")


def _amts(text: str) -> list[float]:
    return [float(m.group(1).replace(",", "")) for m in _GBP.finditer(text or "")]


def _has_amt(text: str, amt: float, tol: float = 0.01) -> bool:
    return any(abs(a - amt) <= tol for a in _amts(text))


def _pdate(s: str):
    try:
        return datetime.strptime(s.strip(), "%d %B %Y")
    except (ValueError, AttributeError):
        return None


def _d(dt) -> str:
    return f"{dt.day} {dt.strftime('%B %Y')}" if dt else "an unknown date"


def _body(doc) -> str:
    """Document text without the title/ID/date header."""
    t = doc.text or ""
    if "**Dated:**" in t:
        t = t.split("**Dated:**", 1)[1].split("\n", 1)[-1]
    return t.strip()


def _same(a: str, b: str) -> bool:
    x, y = norm_name(a), norm_name(b)
    return bool(x and y) and x == y


def _in_text(name: str, text: str) -> bool:
    n = norm_name(name)
    return bool(n) and n in norm_name(text)


def _ch(t) -> str:
    return str(t.raw.get("channel") or t.channel or "").lower()


def _who(t) -> str:
    return t.counterparty or "an unnamed counterparty"


def _desc(t) -> str:
    if _ch(t).startswith("cash"):
        return f"{fmt_gbp(t.amount)} cash {'top-up' if t.direction == 'in' else 'withdrawal'} on {_d(t.ts)} ({t.id})"
    verb = "from" if t.direction == "in" else "to"
    return f"{fmt_gbp(t.amount)} {verb} {_who(t)} on {_d(t.ts)} ({t.id})"


def _docs(ctx, *types):
    return [d for d in ctx.docs if d.type in types]


def _alert_txns(ctx, alerts):
    out, seen = [], set()
    for a in alerts:
        for i in a.txn_ids:
            t = ctx.folder.txns.get(i)
            if t is not None and i not in seen:
                seen.add(i)
                out.append(t)
    return sorted(out, key=lambda t: (t.ts or datetime.min, t.id))


def _rules(alerts) -> set:
    return {a.rule for a in alerts}


def _alert(alerts, rule):
    for a in alerts:
        if a.rule == rule:
            return a
    return None


def _is_self(ctx, t) -> bool:
    return _same(t.counterparty, ctx.customer.name)


def _routine_topup(ctx, t) -> bool:
    """Small top-up from the customer's own linked card/bank with no external account shown."""
    return t.direction == "in" and _is_self(ctx, t) and not t.cp_account and t.amount < 1000


def _income(ctx) -> float | None:
    return ctx.customer.expected_monthly_in


def _multiple(ctx, alerts, amount: float) -> float:
    inc = _income(ctx) or 0
    m = amount / inc if inc else 0.0
    a = _alert(alerts, "TM01")
    if a is not None:
        try:
            m = max(m, float(a.measured.get("multiple") or 0))
        except (TypeError, ValueError):
            pass
    return m


def _fmt_x(x: float) -> str:
    return f"{x:.1f}"


def _out(verdict, code, sentences, evidence):
    reason = " ".join(s.strip() for s in sentences if s and s.strip())
    ev = []
    for e in evidence:
        if e and e not in ev:
            ev.append(e)
    return {"verdict": verdict, "reason_code": code, "reason": reason, "evidence": ev}


def _list_entry(ctx, eid):
    for e in ctx.folder.list_entries:
        if str(e.get("entry_id")) == str(eid):
            return e
    return None


def _high_risk(ctx, country: str) -> bool:
    return bool(country) and country.strip().lower() in ctx.high_risk


def _moved_on(ctx, inflows, hours=48):
    outs = []
    for o in ctx.txns:
        if o.direction != "out" or o.ts is None or _ch(o) == "card":
            continue
        if any(i.ts and timedelta(0) <= o.ts - i.ts <= timedelta(hours=hours) for i in inflows):
            outs.append(o)
    return outs


def _third_party_in(ctx, txns):
    return [t for t in txns if t.direction == "in" and not _is_self(ctx, t) and not _ch(t).startswith("cash")]


def _names(txns, n=3):
    seen = []
    for t in txns:
        if t.counterparty and t.counterparty not in seen:
            seen.append(t.counterparty)
    s = ", ".join(seen[:n])
    k = len(seen) - n
    return s + (f" and {k} other{'s' if k > 1 else ''}" if k > 0 else "")


# --------------------------------------------------------------------------
# escalation handlers (signs of crime first)
# --------------------------------------------------------------------------

_MULE_CHAT = re.compile(r"(send it on|pay(?:s)? into my account and i send|payments assistant|keep \d+% as my|"
                        r"money transfer agent|forward the money)", re.I)


def h_scam_reports(ctx, alerts):
    reps = [d for d in ctx.docs if d.type == "sending_bank_fraud_report" or
            re.search(r"push payment scam|request to freeze funds", d.text or "", re.I)]
    chats = [d for d in _docs(ctx, "support_chat") if _MULE_CHAT.search(d.text or "")]
    if not reps and not chats:
        return None
    ins = _third_party_in(ctx, ctx.win)
    outs = _moved_on(ctx, ins) if ins else []
    s, ev = [], []
    if reps:
        parts = []
        for d in reps:
            m = re.search(r"Our customer ([A-Z][\w'-]+(?: [A-Z][\w'-]+)+) sent (£[\d,\.]+) on (\d{1,2} \w+ \d{4})", d.text)
            bank = d.fields.get("from", "").replace("Fraud operations, ", "")
            if m:
                parts.append(f"{d.id} from {bank or 'the sending bank'} reports that {m.group(1)} paid "
                             f"{m.group(2)} on {m.group(3)} as the victim of a scam")
            else:
                parts.append(f"{d.id} is a scam report from {bank or 'the sending bank'}")
        s.append("; ".join(parts) + ".")
        ev += [d.id for d in reps]
    if ins:
        s.append(f"In the review window {len({t.counterparty for t in ins})} unrelated people "
                 f"({_names(ins)}) paid in {fmt_gbp(sum(t.amount for t in ins))}, and "
                 f"{fmt_gbp(sum(o.amount for o in outs))} went straight out again within 48 hours"
                 + (f", mostly to {max(outs, key=lambda o: o.amount).counterparty or 'cash'}" if outs else "") + ".")
    for c in chats:
        m = re.search(r"payments assistant for ([A-Z][\w ]+?(?:Ltd|Limited|LLP|plc))", c.text)
        s.append(f"In {c.id} the customer says they receive money for "
                 f"{m.group(1) if m else 'an online employer'} and forward it to a crypto account for a cut, "
                 "which is the pattern of a recruited money mule.")
        ev.append(c.id)
    dev = _docs(ctx, "device_export")
    for d in dev:
        cs = re.findall(r"\|\s*([A-Z][a-z]+(?: [A-Z][a-z]+)*(?:, [A-Z][a-z]+(?: [A-Z][a-z]+)*)+)\s*\|\s*$",
                        d.text, re.M)
        if cs:
            s.append(f"Device export {d.id} shows logins from {cs[0]}.")
        ev.append(d.id)
    ev += [t.id for t in ins[:8]] + [o.id for o in outs[:6]]
    return _out("ESCALATE", "ESC_MULE_ACTIVITY", s[:4], ev)


_STRUCT_NOTE = re.compile(r"(aggregat|avoid (?:being )?(?:prompted|a request|triggering)|exempt them from|"
                          r"bypass|circumvent|keep(?:ing)? (?:each |individual )?transactions? (?:below|under))", re.I)


def h_structuring(ctx, alerts):
    notes = [d for d in ctx.docs if d.type in ("staff_note", "contact_note", "support_chat")
             and _STRUCT_NOTE.search(d.text or "") and re.search(r"identif|\bID\b|verification", d.text or "")]
    cash = [t for t in ctx.win if t.direction == "in" and _ch(t).startswith("cash") and 800 <= t.amount < 1000]
    if not notes or (len(cash) < 3 and "TM03" not in _rules(alerts)):
        return None
    a3 = _alert(alerts, "TM03")
    locs = a3.measured.get("locations") if a3 else None
    outs = _moved_on(ctx, cash, 48)
    s = [f"{len(cash)} cash top-ups each just under £1,000 ({', '.join(fmt_gbp(t.amount) for t in cash[:4])}"
         f"{'...' if len(cash) > 4 else ''}) totalling {fmt_gbp(sum(t.amount for t in cash))} were made between "
         f"{_d(cash[0].ts)} and {_d(cash[-1].ts)}" + (f" at {locs} different locations" if locs else "") + "."]
    n = notes[0]
    s.append(f"Staff note {n.id} records the customer asking whether top-ups at different locations are "
             "aggregated and whether keeping each one under the limit avoids ID checks, which shows the "
             "amounts were split deliberately to stay under the £1,000 threshold.")
    if outs:
        big = max(outs, key=lambda o: o.amount)
        s.append(f"The cash was then moved on: {_desc(big)}.")
    return _out("ESCALATE", "ESC_STRUCTURING", s,
                [n.id] + [t.id for t in cash] + [o.id for o in outs[:3]])


_MY = re.compile(r"^(?:January|February|March|April|May|June|July|August|September|October|November|December) \d{4}$")


def _psc_rows(doc):
    """Person rows of a register/PSC table: [name, born, *other cells] (role column dropped)."""
    rows = []
    for line in (doc.text or "").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or not re.match(r"^[A-Z][a-z'-]+(?: [A-Z][a-z'-]+)+$", cells[0]):
            continue
        born_i = next((i for i, c in enumerate(cells[1:], 1) if _MY.match(c)), None)
        if born_i is None:
            continue
        rows.append([cells[0], cells[born_i]] + cells[born_i + 1:])
    return rows


def _dob_my(le) -> str:
    try:
        return datetime.strptime(str(le.get("date_of_birth")), "%Y-%m-%d").strftime("%B %Y")
    except (ValueError, TypeError):
        return ""


def h_list(ctx, alerts):
    a = _alert(alerts, "TM22")
    if a is None:
        return None
    eid = a.measured.get("list_entry") or (re.search(r"[A-Z]{2}-L\d+", a.raw.get("message", "")) or [None])[0]
    le = _list_entry(ctx, eid) or {}
    lname = le.get("name", "")
    t = ctx.folder.txns.get(a.txn_ids[0]) if a.txn_ids else None
    matched_on = str(a.measured.get("matched_on", ""))
    matched_party = str(a.measured.get("matched_party", "")) or (t.counterparty if t else "")
    pay = _desc(t) if t else "the payment"
    tdt = t.ts if t else None
    psc = _docs(ctx, "significant_control_register")
    regs = _docs(ctx, "company_register_extract")
    invs = [d for d in ctx.docs if d.type in ("invoice", "receipt") and t and _has_amt(d.text, t.amount)]
    lmy = _dob_my(le)
    co = t.counterparty if t else matched_party

    # 1. control register: does the listed person still control the payee on the payment date?
    for d in psc + regs:
        for r in _psc_rows(d):
            if not _same(r[0], lname):
                continue
            if lmy and r[1] != lmy:
                continue
            ceased = _pdate(r[4]) if d.type == "significant_control_register" and len(r) >= 5 and r[4] else None
            if d.type == "significant_control_register" and (ceased is None or (tdt and ceased.date() >= tdt.date())):
                reg_note = ""
                for g in regs:
                    for rr in _psc_rows(g):
                        if _same(rr[0], lname) and len(rr) >= 4 and _pdate(rr[3]):
                            reg_note = (f" Register extract {g.id} shows {lname} resigned as director on {rr[3]}, "
                                        "only just before the payment, but that did not end their control, and the invoice does not change who owns the payee.")
                s = [f"{pay} matched internal list entry {eid} ({lname}, born {lmy}, listed as: {le.get('reason', '').split('.')[0].strip().lower()}).",
                     f"The significant control register {d.id} shows {lname}, born {r[1]}, as a person with "
                     f"significant control of {co} ({r[2][:1].lower() + r[2][1:]}) until {r[4] if len(r) >= 5 and r[4] else 'now'}, so the listed "
                     f"person still controlled the payee when it was paid on {_d(tdt)}." + reg_note]
                return _out("ESCALATE", "ESC_LISTED_PARTY", s,
                            [d.id] + [g.id for g in regs] + [i.id for i in invs] + ([t.id] if t else []) + [eid])
            if d.type == "significant_control_register" and ceased and tdt and ceased.date() < tdt.date():
                return _out("RFI", "RFI_COUNTERPARTY_DETAILS",
                            [f"{pay} matched internal list entry {eid} ({lname}).",
                             f"{d.id} shows {lname} ceased to control {co} on {r[4]}, before the payment, "
                             "but nothing on file shows who now runs it or what the payment was for.",
                             f"Ask the customer what the payment to {co} was for and for the invoice or "
                             "contract behind it, with the name of the person they dealt with."],
                            [d.id] + [i.id for i in invs] + ([t.id] if t else []) + [eid])

    # 2. address / officer match disproved by the company register
    if regs and le:
        g = regs[0]
        office = g.fields.get("registered office", "")
        officers = _psc_rows(g)
        listed_officer = [r for r in officers if _same(r[0], lname) and (not lmy or r[1] == lmy)]
        if listed_officer:
            return _out("ESCALATE", "ESC_LISTED_PARTY",
                        [f"{pay} matched internal list entry {eid} ({lname}).",
                         f"Register extract {g.id} names {lname}, born {listed_officer[0][1]}, as an officer of "
                         f"{co}, matching the listed person's name and date of birth."],
                        [g.id, eid] + ([t.id] if t else []))
        laddr = str(le.get("address", ""))
        unit_l = laddr.split(",")[0].strip().lower()
        unit_o = office.split(",")[0].strip().lower()
        if office and unit_l and unit_l != unit_o:
            uniq = []
            for r in officers:
                if r[0] not in [u[0] for u in uniq]:
                    uniq.append(r)
            offs = " and ".join(f"{r[0]} (born {r[1]})" for r in uniq[:2]) or "other people"
            rc = [i for i in invs if i.type == "receipt" or _in_text(matched_party, i.text)]
            s = [f"{pay} matched internal list entry {eid} only on {matched_on}.",
                 f"Register extract {g.id} shows {co} is registered at {office}, not the listed "
                 f"{laddr}, and is run and owned by {offs}, not the listed {lname} (born {lmy}).",
                 (f"Receipt {rc[0].id} confirms the {fmt_gbp(t.amount)} was for a "
                  f"{(t.reference or 'course').lower()} at {co}." if rc and t else "")]
            return _out("CLOSE", "LIST_MATCH_DIFFERENT_PARTY", s,
                        [g.id] + [i.id for i in rc] + ([t.id] if t else []) + [eid])

    # 3. nothing on file settles it either way
    s = [f"{pay}, reference '{t.reference if t else ''}', matched internal list entry {eid} ({lname}) on "
         f"{matched_on} alone, which does not prove it is the same party.",
         "Nothing on file confirms or rules out the match and nothing else on the account is concerning.",
         f"Ask the customer who {matched_party} is (full name, address and how they found them) and for the "
         f"invoice or quote for the {fmt_gbp(t.amount) if t else ''} {(t.reference or '').lower() if t else ''} work."]
    return _out("RFI", "RFI_COUNTERPARTY_DETAILS", s, ([t.id] if t else []) + [eid])


def h_dissolved_payer(ctx, alerts):
    regs = [d for d in _docs(ctx, "company_register_extract")
            if re.search(r"dissolved", d.fields.get("status", ""), re.I)]
    if not regs:
        return None
    for g in regs:
        co = g.fields.get("company", "")
        ins = [t for t in ctx.win if t.direction == "in" and co and _same(t.counterparty, co)]
        if not ins:
            continue
        outs = _moved_on(ctx, ins)
        chat = _docs(ctx, "support_chat")
        s = [f"The account received {len(ins)} payments totalling {fmt_gbp(sum(t.amount for t in ins))} from {co} "
             f"between {_d(ins[0].ts)} and {_d(ins[-1].ts)}, referenced '{ins[0].reference}'."]
        if chat:
            s.append(f"In {chat[0].id} the customer calls this a loan from a friend, but the payer is a company "
                     f"and register extract {g.id} shows {co} was {g.fields.get('status', 'dissolved')[:1].lower() + g.fields.get('status', 'dissolved')[1:]}, "
                     "so the explanation is not credible.")
        else:
            s.append(f"Register extract {g.id} shows {co} was {g.fields.get('status', 'dissolved').lower()}.")
        if outs:
            s.append(f"{fmt_gbp(sum(o.amount for o in outs))} was paid straight on within 48 hours to "
                     f"{_names(outs)}.")
        return _out("ESCALATE", "ESC_UNEXPLAINED_FUNDS", s,
                    [g.id] + [c.id for c in chat] + [t.id for t in ins] + [o.id for o in outs[:6]])
    return None


def h_shared_device(ctx, alerts):
    devs = _docs(ctx, "device_export")
    shared = None
    for d in devs:
        ids = set(re.findall(r"[A-Z]{2}-C\d+", d.text or ""))
        others = ids - {ctx.customer.id}
        if others:
            shared = (d, sorted(others))
            break
    if not shared:
        return None
    d, others = shared
    ins = _third_party_in(ctx, ctx.win)
    outs = _moved_on(ctx, ins) if ins else []
    tin = sum(t.amount for t in ins)
    tout = sum(o.amount for o in outs)
    if len({t.counterparty for t in ins}) < 2 or tout < 0.6 * tin:
        return None
    dest = {}
    for o in outs:
        dest[o.counterparty or "cash"] = dest.get(o.counterparty or "cash", 0) + o.amount
    top = max(dest, key=dest.get)
    countries = re.findall(r"\|\s*([A-Z][a-z]+(?: [A-Z][a-z]+)*(?:, [A-Z][a-z]+(?: [A-Z][a-z]+)*)+)\s*\|\s*$",
                           d.text, re.M)
    s = [f"{len(ins)} unrelated people ({_names(ins)}) paid {fmt_gbp(tin)} into the account in the window and "
         f"{fmt_gbp(tout)} was sent on within hours, mostly to {top} ({fmt_gbp(dest[top])}).",
         f"Device export {d.id} shows the same device was used to log in to {len(others)} other customers' "
         f"accounts ({', '.join(others[:3])})" + (f" from {countries[0]}" if countries else "") +
         ", so the account appears to be controlled alongside other accounts as part of a mule network.",
         "None of the payers is explained and the customer's declared salary profile does not cover this flow."]
    return _out("ESCALATE", "ESC_MULE_ACTIVITY", s, [d.id] + [t.id for t in ins[:8]] + [o.id for o in outs[:6]])


# --------------------------------------------------------------------------
# refunds, corridors and invoices
# --------------------------------------------------------------------------

def h_refund(ctx, alerts):
    refs = [t for t in ctx.win if t.direction == "in" and re.search(r"\brefund", t.reference or "", re.I)]
    if not refs:
        return None
    r = max(refs, key=lambda t: t.amount)
    key = re.sub(r"(?i)^refund\s*", "", r.reference).strip()
    orig = [o for o in ctx.txns if o.direction == "out" and o.ts and r.ts and o.ts < r.ts and
            (_same(o.counterparty, r.counterparty) or (key and key in (o.reference or "")))]
    if not orig:
        return None
    o = max(orig, key=lambda x: x.ts)
    docs = [d for d in ctx.docs if _has_amt(d.text, r.amount) and (key in d.text or _in_text(r.counterparty, d.text))]
    if r.amount > o.amount + 0.01:
        return _out("ESCALATE", "ESC_UNEXPLAINED_FUNDS",
                    [f"{_desc(r)} is described as a refund of {_desc(o)}, but it is larger than the original payment.",
                     "Nothing on file explains the extra money."], [r.id, o.id] + [d.id for d in docs])
    if not docs:
        return _out("RFI", "RFI_PURPOSE_DOCUMENTS",
                    [f"{_desc(r)} is referenced as a refund of {_desc(o)}.",
                     f"Ask the customer for the cancellation or refund confirmation from {r.counterparty} for "
                     f"booking {key or 'reference'}."], [r.id, o.id])
    rev = [v for v in ctx.reviews if "CLOSE" in str(v.get("verdict", "")).upper()
           and _in_text(o.counterparty, str(v.get("note", "")))]
    # the refund must account for the bulk of what was received
    other_in = [t for t in ctx.win if t.direction == "in" and t.id != r.id and not _routine_topup(ctx, t)]
    if sum(t.amount for t in other_in) > 0.25 * r.amount:
        return None
    s = [f"{_desc(r)} returns the {fmt_gbp(o.amount)} paid to {o.counterparty} on {_d(o.ts)} ({o.id}), "
         f"reference {o.reference}.",
         f"{docs[0].id} from {r.counterparty} confirms the cancellation and a full refund of {fmt_gbp(r.amount)} "
         f"for {key}."]
    if rev:
        s.append(f"Earlier review {rev[0].get('review_id')} already checked the original deposit against the "
                 "booking contract, so the money coming back from the same venue is explained.")
    return _out("CLOSE", "REFUND_OF_EARLIER_PAYMENT", s,
                [docs[0].id, r.id, o.id] + [str(v.get("review_id")) for v in rev])


def h_corridor(ctx, alerts):
    if "TM06" not in _rules(alerts):
        return None
    hr = [t for t in ctx.win if _high_risk(ctx, t.cp_country)]
    if not hr:
        hr = [t for t in _alert_txns(ctx, alerts) if _high_risk(ctx, t.cp_country)]
    if not hr:
        return None
    country = hr[0].cp_country
    # invoice-backed payments (e.g. hospital treatment)
    invs = _docs(ctx, "hospital_invoice", "invoice")
    matched, unmatched = [], []
    for t in hr:
        hit = None
        for d in invs:
            payto = d.fields.get("pay to", "")
            if _has_amt(d.text, t.amount) and payto and _in_text(payto.split(",")[0], t.counterparty) \
                    and (not d.dated or not t.ts or d.dated <= t.ts + timedelta(days=1)):
                hit = d
                break
        (matched if hit else unmatched).append((t, hit))
    if matched and not unmatched:
        t, d = matched[0]
        pat = d.fields.get("patient", "")
        chat = _docs(ctx, "support_chat")
        s = [f"{_desc(t)} in {country} matches invoice {d.id} from {d.fields.get('pay to', t.counterparty)}: "
             f"{fmt_gbp(t.amount)} for {d.fields.get('treatment', 'treatment').lower()}"
             + (f" for patient {pat}" if pat else "") + f", dated {_d(d.dated)}."]
        if chat and pat and _in_text(pat, chat[0].text):
            s.append(f"In {chat[0].id} the customer explains it is for their parent {pat}, who shares the "
                     f"customer's surname, and the payment reference '{t.reference}' names the same patient.")
        return _out("CLOSE", "INVOICE_MATCHES", s, [d.id] + [c.id for c in chat] + [t.id])
    if matched and unmatched:
        return None
    outs = [t for t in hr if t.direction == "out"]
    ins3 = _third_party_in(ctx, ctx.win)
    ins3 = [t for t in ins3 if not _high_risk(ctx, t.cp_country)]
    recips = {t.counterparty for t in outs}
    chat = _docs(ctx, "support_chat")
    if len(recips) >= 2 or (ins3 and outs):
        sn = ctx.customer.name.split()[-1].lower()
        s = [f"{fmt_gbp(sum(t.amount for t in hr))} went to {len(recips)} different people in {country}, a "
             f"higher-risk country the customer did not declare ({_names(outs)}), referenced "
             f"'{outs[0].reference}'." if outs else
             f"{fmt_gbp(sum(t.amount for t in hr))} moved to or from {country}, a higher-risk country the customer "
             "did not declare."]
        if ins3:
            s.append(f"The money came in days earlier from {len({t.counterparty for t in ins3})} unrelated UK "
                     f"payers ({_names(ins3)}) totalling {fmt_gbp(sum(t.amount for t in ins3))}, far above the "
                     f"declared income of {fmt_gbp(_income(ctx))} a month.")
        if chat:
            fam = "none of the recipients shares the customer's surname" if not any(
                (t.counterparty or "").split()[-1:] == [ctx.customer.name.split()[-1]] for t in outs) else ""
            s.append(f"The customer's explanation in {chat[0].id} that it is family support is unsupported"
                     + (f": {fam} and nothing on file explains the payers." if fam else "."))
        return _out("ESCALATE", "ESC_HIGH_RISK_CORRIDOR", s,
                    [c.id for c in chat] + [t.id for t in outs[:6]] + [t.id for t in ins3[:6]])
    t = max(hr, key=lambda x: x.amount)
    return _out("RFI", "RFI_PURPOSE_DOCUMENTS",
                [f"{_desc(t)} involves {country}, a higher-risk country the customer did not declare, and nothing "
                 "on file shows what it was for.",
                 f"Ask the customer for the invoice or contract behind the payment with {t.counterparty}, and their "
                 "relationship to the recipient."],
                [c.id for c in chat] + [t.id])


# --------------------------------------------------------------------------
# documented one-off events and business records
# --------------------------------------------------------------------------

def _big_unexplained(ctx, explained_ids, alerts):
    """Inflows in the window that are neither routine top-ups nor explained."""
    return [t for t in ctx.win if t.direction == "in" and t.id not in explained_ids
            and not _routine_topup(ctx, t) and t.amount >= 500]


def h_letter_payout(ctx, alerts):
    """Settlement letter / insurer letter explaining one large inflow (TM01, TM07)."""
    letters = _docs(ctx, "settlement_letter", "insurer_letter")
    ins = [t for t in ctx.win if t.direction == "in" and not _is_self(ctx, t) and t.amount >= 1000]
    if not ins:
        return None
    t = max(ins, key=lambda x: x.amount)
    kind = None
    if re.search(r"settlement", t.reference or "", re.I):
        kind = "settlement"
    elif re.search(r"\bclaim\b", t.reference or "", re.I) or re.search(r"assurance|insurance", t.counterparty, re.I):
        kind = "insurer"
    if not letters and not kind:
        return None
    doc = None
    for d in letters:
        if _has_amt(d.text, t.amount) and _in_text(t.counterparty, d.text):
            doc = d
            break
    if doc is None:
        if letters:
            d = letters[0]
            return _out("RFI", "RFI_SOURCE_OF_FUNDS",
                        [f"{_desc(t)} is above the declared income of {fmt_gbp(_income(ctx))} a month.",
                         f"{d.id} on file does not match this payment on amount or payer.",
                         f"Ask the customer for the signed agreement or letter from {t.counterparty} confirming the "
                         f"{fmt_gbp(t.amount)} payment."], [d.id, t.id])
        what = ("the settlement agreement or the employer's letter confirming the compensation payment"
                if kind == "settlement" else "the insurer's claim acceptance letter")
        mult = _multiple(ctx, alerts, sum(x.amount for x in ctx.win if x.direction == "in"))
        return _out("RFI", "RFI_SOURCE_OF_FUNDS",
                    [f"{_desc(t)}, reference '{t.reference}', took receipts to {_fmt_x(mult)} times the declared "
                     f"monthly income of {fmt_gbp(_income(ctx))}.",
                     "Nothing on file shows what the payment is, but nothing about it suggests crime.",
                     f"Ask the customer for {what} from {t.counterparty} showing the {fmt_gbp(t.amount)} paid on "
                     f"{_d(t.ts)}."], [t.id])
    if doc.dated and t.ts and doc.dated > t.ts + timedelta(days=1):
        return _out("RFI", "RFI_SOURCE_OF_FUNDS",
                    [f"{_desc(t)} arrived before {doc.id} dated {_d(doc.dated)} was issued.",
                     f"Ask the customer for the original agreement with {t.counterparty} covering the payment."],
                    [doc.id, t.id])
    if _big_unexplained(ctx, {t.id}, alerts):
        return None
    if doc.type == "settlement_letter":
        s = [f"{doc.id} dated {_d(doc.dated)} confirms {t.counterparty} will pay {fmt_gbp(t.amount)} under a "
             "settlement agreement as compensation for loss of office.",
             f"The {_desc(t)}, reference '{t.reference}', matches it on payer, amount and timing, and the other "
             "receipts are the customer's usual small top-ups."]
    else:
        ref = re.search(r"CLAIM \d+", doc.text)
        s = [f"{doc.id} dated {_d(doc.dated)} from {t.counterparty} confirms claim {ref.group(0) if ref else ''} "
             f"under the customer's policy was accepted and {fmt_gbp(t.amount)} will be paid by bank transfer.",
             f"{_desc(t)}, reference '{t.reference}', matches the letter on payer, amount and reference."]
        if "TM07" in _rules(alerts):
            s.append("The one-off claim payout explains why the dormant account was used again.")
    return _out("CLOSE", "DOCUMENTED_EVENT", s, [doc.id, t.id])


def h_cash_sale(ctx, alerts):
    if "TM05" not in _rules(alerts):
        return None
    a = _alert(alerts, "TM05")
    cash = [ctx.folder.txns[i] for i in a.txn_ids if i in ctx.folder.txns]
    if not cash:
        return None
    t = cash[0]
    sales = _docs(ctx, "sale_receipt")
    adverts = _docs(ctx, "advert")
    for d in sales:
        if _has_amt(_body(d), t.amount) and _in_text(ctx.customer.name, d.text) and \
                (not d.dated or not t.ts or d.dated <= t.ts + timedelta(days=1)):
            buyer = None
            m = re.search(r"\bto ([A-Z][a-z]+ [A-Z][a-z]+)\b", d.text) or re.search(r"Buyer:\s*([A-Z][a-z]+ [A-Z][a-z]+)", d.text)
            if m:
                buyer = m.group(1)
            adv = [x for x in adverts if _in_text(ctx.customer.name, x.text)]
            item = None
            if adv:
                item = _body(adv[0]).split(",")[0].split(".")[0]
            s = [f"Sale receipt {d.id} dated {_d(d.dated)} records the customer selling "
                 f"{('a ' + item.lower()) if item else 'an item'}" + (f" to {buyer}" if buyer else "") +
                 f" for {fmt_gbp(t.amount)} paid in cash.",
                 f"The {_desc(t)} matches the receipt on amount and was paid in the day after the sale."]
            if adv:
                s.append(f"Advert {adv[0].id} dated {_d(adv[0].dated)} shows the customer listing it for sale "
                         "beforehand, cash on collection.")
            return _out("CLOSE", "DOCUMENTED_EVENT", s, [d.id] + [x.id for x in adv] + [t.id])
    if sales or adverts:
        return _out("RFI", "RFI_SOURCE_OF_FUNDS",
                    [f"The {_desc(t)} is not matched by the sale documents on file.",
                     "Ask the customer for a signed receipt showing the sale and the cash amount received."],
                    [x.id for x in sales + adverts] + [t.id])
    if ctx.customer.kind == "business":
        return _out("RFI", "RFI_BUSINESS_RECORDS",
                    [f"The {_desc(t)} is above the business cash limit and nothing on file shows where it came from.",
                     "Ask the customer for till or takings records covering this cash."], [t.id])
    return _out("RFI", "RFI_SOURCE_OF_FUNDS",
                [f"The {_desc(t)} is at or above the £1,000 limit for an individual and nothing on file shows "
                 "where the cash came from.",
                 "Ask the customer for proof of the source of this cash, such as a sale receipt."], [t.id])


def _takings_rows(doc):
    rows = []
    for m in re.finditer(r"\|\s*(\d{1,2} \w+ \d{4})\s*\|\s*([^|]+?)\s*\|\s*£([\d,\.]+)\s*\|\s*£([\d,\.]+)\s*\|", doc.text):
        dt = _pdate(m.group(1))
        if dt:
            rows.append((dt.date(), m.group(2), float(m.group(3).replace(",", "")), float(m.group(4).replace(",", ""))))
    return rows


def h_takings(ctx, alerts):
    books = _docs(ctx, "takings_book")
    a = _alert(alerts, "TM03")
    if not books and a is None:
        return None
    cash = [t for t in ctx.win if t.direction == "in" and _ch(t).startswith("cash")]
    if a is not None:
        cash = [ctx.folder.txns[i] for i in a.txn_ids if i in ctx.folder.txns] or cash
    if not cash:
        return None
    if books:
        b = books[0]
        rows = _takings_rows(b)
        ok, bad = [], []
        for t in cash:
            hit = [r for r in rows if t.ts and r[0] == t.ts.date() and abs(r[3] - t.amount) < 0.01 and r[2] >= t.amount]
            (ok if hit else bad).append(t)
        if not bad:
            markets = sorted({r[1] for r in rows})
            s = [f"Takings book {b.id} records each of the {len(cash)} cash top-ups "
                 f"({', '.join(fmt_gbp(t.amount) for t in cash[:6])}) on the same day as a market day's takings at "
                 f"{' and '.join(markets[:2])}, with takings of {fmt_gbp(min(r[2] for r in rows))} to "
                 f"{fmt_gbp(max(r[2] for r in rows))} each day.",
                 f"The deposits of {fmt_gbp(sum(t.amount for t in cash))} reconcile to the takings and fit the "
                 f"customer's declared work as a {ctx.customer.occupation.lower() or 'trader'}."]
            return _out("CLOSE", "BUSINESS_RECORDS_RECONCILE", s, [b.id] + [t.id for t in cash])
        return _out("RFI", "RFI_BUSINESS_RECORDS",
                    [f"Takings book {b.id} does not account for {len(bad)} of the cash top-ups, including "
                     f"{_desc(bad[0])}.",
                     "Ask the customer for the full takings records for the days the cash was paid in."],
                    [b.id] + [t.id for t in bad])
    return _out("RFI", "RFI_BUSINESS_RECORDS",
                [f"{len(cash)} cash top-ups between £800 and £999.99 totalling {fmt_gbp(sum(t.amount for t in cash))} "
                 f"were made between {_d(cash[0].ts)} and {_d(cash[-1].ts)}, and no takings records are on file.",
                 "Ask the customer for their daily takings records showing where the cash came from."],
                [t.id for t in cash])


def h_booking(ctx, alerts):
    books = _docs(ctx, "booking_confirmation")
    a4 = _alert(alerts, "TM04")
    ins = [t for t in ctx.win if t.direction == "in" and not _is_self(ctx, t) and not _ch(t).startswith("cash")]
    outs = [t for t in ctx.win if t.direction == "out" and re.search(r"\bbooking\b", t.reference or "", re.I)]
    if not books and not (a4 and outs):
        return None
    if books:
        b = books[0]
        trav = re.findall(r"^\s*\d+\.\s+(.+?)\s*$", b.text, re.M)
        pay = b.fields.get("payment", "")
        price = b.fields.get("price", "")
        pp = re.search(r"£([\d,\.]+) per person", price)
        tot = re.search(r"£([\d,\.]+) in total", price)
        pp = float(pp.group(1).replace(",", "")) if pp else None
        tot = float(tot.group(1).replace(",", "")) if tot else None
        acct = re.search(r"account ([\d-]+ \d+)", pay)
        payee = re.search(r"to (.+?), account", pay)
        bref = b.fields.get("booking reference", "")
        not_trav = [t for t in ins if not any(_same(t.counterparty, x) for x in trav)]
        wrong_amt = [t for t in ins if pp is not None and abs(t.amount - pp) > 0.01]
        paid = [o for o in ctx.txns if o.direction == "out" and tot is not None and abs(o.amount - tot) < 0.01 and
                ((acct and o.cp_account == acct.group(1)) or (payee and _same(o.counterparty, payee.group(1))))]
        if not not_trav and not wrong_amt and paid:
            o = paid[0]
            s = [f"Booking confirmation {b.id} ({bref}) names the customer as lead traveller on "
                 f"{b.fields.get('trip', 'a group trip')[:1].lower() + b.fields.get('trip', 'a group trip')[1:]} for {b.fields.get('travellers', len(trav))} people at "
                 f"{fmt_gbp(pp)} each, {fmt_gbp(tot)} in total.",
                 f"All {len(ins)} payers ({_names(ins)}) are on the traveller list and each paid {fmt_gbp(pp)}.",
                 f"The customer then paid {_desc(o)}, reference '{o.reference}', to the account given on the "
                 "booking, so the collection was passed to the travel company as documented."]
            return _out("CLOSE", "COLLECTION_DOCUMENTED", s, [b.id, o.id] + [t.id for t in ins[:10]])
        issue = (f"{len(not_trav)} payers, including {not_trav[0].counterparty}, are not on its traveller list"
                 if not_trav else f"{len(wrong_amt)} payments do not match the per-person price" if wrong_amt
                 else "the onward payment does not match the travel company's account and total")
        if not_trav:
            return _out("ESCALATE", "ESC_THIRD_PARTY_FUNDING",
                        [f"Booking confirmation {b.id} does not explain the collection: {issue}.",
                         "Unexplained third parties paid the account and the money was passed on."],
                        [b.id] + [t.id for t in not_trav[:6]])
        return _out("RFI", "RFI_PURPOSE_DOCUMENTS",
                    [f"Booking confirmation {b.id} is on file but {issue}.",
                     "Ask the customer for the final invoice from the travel company and a list of who paid for "
                     "which place."], [b.id] + [t.id for t in ins[:6]] + [o.id for o in outs])
    o = outs[0]
    per = sorted({t.amount for t in ins})
    s = [f"{len({t.counterparty for t in ins})} people ({_names(ins)}) each paid "
         f"{fmt_gbp(per[0]) if len(per) == 1 else 'similar amounts'} with trip references, and the customer then "
         f"paid {_desc(o)}, reference '{o.reference}'.",
         "This looks like a group trip collection, but no booking is on file to show who the travellers are.",
         f"Ask the customer for the booking confirmation from {o.counterparty} for booking "
         f"{o.reference.replace('BOOKING', '').strip()} listing the travellers, the price per person and the "
         "payment details."]
    return _out("RFI", "RFI_PURPOSE_DOCUMENTS", s, [o.id] + [t.id for t in ins[:10]])


def h_deposits(ctx, alerts):
    ret_docs = _docs(ctx, "deposit_return_letter")
    hold_docs = _docs(ctx, "holding_deposit_receipt")
    rets = [t for t in ctx.win if t.direction == "in" and re.search(r"deposit return", t.reference or "", re.I)]
    holds = [t for t in ctx.win if t.direction == "out" and re.search(r"holding deposit", t.reference or "", re.I)]
    if not (rets or holds or ret_docs or hold_docs):
        return None
    if not rets and not holds:
        return None
    rmatch = {t.id: next((d for d in ret_docs if _has_amt(d.text, t.amount) and _in_text(t.counterparty, d.text)), None)
              for t in rets}
    hmatch = {t.id: next((d for d in hold_docs if _has_amt(d.text, t.amount) and _in_text(t.counterparty, d.text)), None)
              for t in holds}
    other_in = [t for t in ctx.win if t.direction == "in" and t not in rets and not _is_self(ctx, t)
                and not _ch(t).startswith("cash")]
    if rets and holds and all(rmatch.values()) and all(hmatch.values()) and not other_in:
        r, h = rets[0], holds[0]
        top = [t for t in ctx.win if t.direction == "in" and _is_self(ctx, t) and t.ts and h.ts and
               timedelta(0) <= h.ts - t.ts <= timedelta(hours=48)]
        s = [f"Letter {rmatch[r.id].id} from {r.counterparty} confirms the deposit protection scheme released "
             f"{fmt_gbp(r.amount)} at the end of the customer's tenancy, matching {_desc(r)}.",
             f"Receipt {hmatch[h.id].id} from {h.counterparty} confirms a holding deposit of {fmt_gbp(h.amount)} "
             f"for the customer's next flat, matching {_desc(h)}"
             + (f", topped up with the customer's own {fmt_gbp(sum(t.amount for t in top))}." if top else ".")]
        s.append("The money moved quickly because the old deposit was reused for the new tenancy, a documented "
                 "one-off house move.")
        return _out("CLOSE", "DOCUMENTED_EVENT", s,
                    [rmatch[r.id].id, hmatch[h.id].id, r.id, h.id] + [t.id for t in top])
    r = rets[0] if rets else None
    h = holds[0] if holds else None
    ask = []
    if r and not rmatch.get(r.id):
        ask.append(f"the deposit return letter from {r.counterparty} for the {fmt_gbp(r.amount)}")
    if h and not hmatch.get(h.id):
        ask.append(f"the holding deposit receipt or tenancy offer from {h.counterparty} for the {fmt_gbp(h.amount)}")
    s = [(f"{_desc(r)}, reference '{r.reference}', " if r else "") +
         (f"was followed by {_desc(h)}, reference '{h.reference}'." if h else "arrived."),
         "This looks like a tenancy deposit being returned and reused for a new flat, but nothing on file "
         "documents it and nothing suggests crime.",
         f"Ask the customer for {' and '.join(ask) or 'the tenancy documents'}."]
    return _out("RFI", "RFI_PURPOSE_DOCUMENTS", s,
                [d.id for d in ret_docs + hold_docs] + ([r.id] if r else []) + ([h.id] if h else []))


# --------------------------------------------------------------------------
# own-account transfers and dormant accounts
# --------------------------------------------------------------------------

def h_own_account(ctx, alerts):
    own_named = [t for t in _alert_txns(ctx, alerts) if _is_self(ctx, t) and t.cp_account]
    if not own_named:
        own_named = [t for t in ctx.win if _is_self(ctx, t) and t.cp_account and t.amount >= 1000]
    if not own_named:
        return None
    stmts = _docs(ctx, "bank_statement")
    by_acct = {}
    for d in stmts:
        acc = d.fields.get("account", "")
        if acc:
            by_acct[acc] = d
    shown, unshown = [], []
    for t in own_named:
        d = by_acct.get(t.cp_account)
        if d is not None and _same(d.fields.get("account holder", ""), ctx.customer.name) and _has_amt(d.text, t.amount):
            shown.append((t, d))
        else:
            unshown.append(t)
    rules = _rules(alerts)
    # everything else alerted must be routine
    other = [t for t in _alert_txns(ctx, alerts) if t not in own_named and not _routine_topup(ctx, t)
             and not (t.direction == "out" and t.amount < 250)]
    if other:
        return None
    big = max(own_named, key=lambda t: t.amount)
    if unshown:
        t = max(unshown, key=lambda x: x.amount)
        s = [f"{_desc(t)}, reference '{t.reference}', came from an account in the customer's own name"
             f" ({t.cp_account})" + (f" after {_alert(alerts, 'TM07').measured.get('dormant_days')} days of dormancy"
                                     if "TM07" in rules else "") + ".",
             "Policy counts it as the customer's own account only when a document on file shows they hold it, and "
             "none does; nothing else about the activity is concerning.",
             f"Ask the customer for a statement of account {t.cp_account} in their name showing the "
             f"{fmt_gbp(t.amount)} transfer on {_d(t.ts)}."]
        return _out("RFI", "RFI_SOURCE_OF_FUNDS", s, [t.id] + [d.id for _, d in shown])
    own_in = sum(t.amount for t, _ in shown if t.direction == "in")
    mult = _multiple(ctx, alerts, own_in)
    docs_ids = [d.id for _, d in shown]
    descr = "; ".join(f"{d.id} ({d.fields.get('bank', 'bank')}, account {d.fields.get('account')}) shows "
                      f"{fmt_gbp(t.amount)} {'leaving' if t.direction == 'in' else 'arriving'} on {_d(t.ts)}"
                      for t, d in shown[:2])
    if mult >= 5:
        s = [f"{_desc(big)} came from the customer's own account: {descr}, in the customer's name.",
             f"The own-account transfers explain the activity, but it is {_fmt_x(mult)} times the declared monthly "
             f"income of {fmt_gbp(_income(ctx))}, at or above the 5 times policy limit for closing.",
             "Ask the customer for an updated expected monthly income and account usage, including how much they "
             "plan to move in from their other accounts."]
        return _out("RFI", "RFI_UPDATED_PROFILE", s, docs_ids + [t.id for t, _ in shown])
    rv = _docs(ctx, "reverification_record")
    if "TM07" in rules and rv:
        a7 = _alert(alerts, "TM07")
        s = [f"The account was dormant for {a7.measured.get('dormant_days')} days; {rv[0].id} shows the customer's "
             f"identity was re-verified on {_d(rv[0].dated)} with a live selfie on their registered device.",
             f"The {_desc(big)} came from their own account: {descr}, in the customer's name.",
             f"At {_fmt_x(mult)} times the declared income this is below the 5 times limit, so a verified customer "
             "has simply resumed using the account."]
        return _out("CLOSE", "RETURNING_CUSTOMER", s, [rv[0].id] + docs_ids + [t.id for t, _ in shown])
    if "TM07" in rules and not rv:
        t = big
        return _out("RFI", "RFI_UPDATED_PROFILE",
                    [f"The dormant account received {_desc(t)} from the customer's own account ({descr}).",
                     "The customer has not been re-verified since the account went dormant.",
                     "Ask the customer to confirm their current circumstances and expected use of the account."],
                    docs_ids + [t.id])
    s = [f"The alerted money moved between accounts the customer holds: {descr}, each in the customer's name.",
         f"{_desc(big)} matches the statements on amount and account number"
         + (", and the onward transfer went to the customer's own savings account." if any(
             t.direction == "out" for t, _ in shown) else "."),
         f"At {_fmt_x(mult)} times the declared monthly income of {fmt_gbp(_income(ctx))} this is below the 5 "
         "times policy limit, so it can be closed."]
    return _out("CLOSE", "OWN_ACCOUNT", s, docs_ids + [t.id for t, _ in shown])


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

HANDLERS = [
    h_scam_reports,
    h_structuring,
    h_list,
    h_dissolved_payer,
    h_shared_device,
    h_refund,
    h_corridor,
    h_letter_payout,
    h_cash_sale,
    h_takings,
    h_booking,
    h_deposits,
    h_own_account,
]


def decide(ctx, alerts):
    for h in HANDLERS:
        out = h(ctx, alerts)
        if out is not None:
            return out
    return None
