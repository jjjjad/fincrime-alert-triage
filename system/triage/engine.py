"""Deterministic triage: one verdict per case.

Each alert in a case is analysed by the handler for its rule and produces a
Finding (verdict, reason code, facts, evidence IDs). The case verdict is the
most severe finding: one unexplained alert is enough to stop a CLOSE.
Case-level checks (scam reports, held payments without an invoice) run first
and can override the per-alert view.

Cost asymmetry from the brief drives the defaults: a wrongful CLOSE costs 10,
anything else at most 3. So CLOSE requires positive evidence for every alert,
and when unsure between RFI and ESCALATE we prefer ESCALATE only where there is
a sign of crime (RFI after suspicion risks tipping off).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import evidence as ev
from .data import (Alert, Case, Customer, Doc, Folder, Txn, fmt_gbp, in_window, norm_name, pick,
                   to_amount, to_dt)
from .policy import (INSTITUTIONS, OWN_ACCOUNT_MULTIPLE, SEVERITY, detect_institution,
                     parse_high_risk)

RULE_PRIORITY = ["TM22", "TM04", "TM03", "TM02", "TM06", "TM08", "TM10", "TM01", "TM05", "TM07"]
CASH_BUSINESS = ("shop", "cafe", "café", "restaurant", "bakery", "market", "taxi", "salon", "barber",
                 "bar", "pub", "takeaway", "convenience", "newsagent", "car wash", "launderette",
                 "retail", "hairdress", "food", "trader", "builder")


@dataclass
class Finding:
    verdict: str
    code: str
    alert: Alert | None
    facts: list[str]
    evidence: list[str] = field(default_factory=list)
    ask: str = ""   # RFI only: exactly what to ask the customer for


@dataclass
class Ctx:
    folder: Folder
    inst: str
    cfg: dict
    case: Case
    customer: Customer
    txns: list[Txn]
    win: list[Txn]
    docs: list[Doc]
    doc_kinds: dict[str, set[str]]
    reviews: list[dict]
    high_risk: set[str]

    def docs_of(self, kind: str) -> list[Doc]:
        return [d for d in self.docs if kind in self.doc_kinds[d.id]]


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------

def _is_customer(ctx: Ctx, name: str) -> bool:
    a, b = norm_name(name), norm_name(ctx.customer.name)
    return bool(a and b) and (a == b or a in b or b in a)


def _is_high_risk(ctx: Ctx, country: str) -> bool:
    return bool(country) and country.strip().lower() in ctx.high_risk


def _declared_country(ctx: Ctx, country: str) -> bool:
    c = country.strip().lower()
    return any(c == x.strip().lower() for x in ctx.customer.countries)


def _alert_txns(ctx: Ctx, alert: Alert) -> list[Txn]:
    out = [ctx.folder.txns[i] for i in alert.txn_ids if i in ctx.folder.txns]
    return sorted(out, key=lambda t: (t.ts or datetime.min, t.id))


def _who(t: Txn) -> str:
    return t.counterparty or t.cp_account_name or "an unnamed counterparty"


def _describe(t: Txn) -> str:
    verb = "received from" if t.direction == "in" else "paid to"
    if t.channel == "cash":
        return f"{fmt_gbp(t.amount)} cash {'deposited' if t.direction == 'in' else 'withdrawn'} on {t.day} ({t.id})"
    return f"{fmt_gbp(t.amount)} {verb} {_who(t)} on {t.day} ({t.id})"


def _review_covers(ctx: Ctx, t: Txn) -> dict | None:
    """An earlier review covers only what it describes - never new or larger activity."""
    for r in ctx.reviews:
        outcome = str(pick(r, "outcome", "verdict", "decision", default="")).upper()
        if outcome and "CLOSE" not in outcome:
            continue
        text = " ".join(str(v) for v in r.values())
        if not t.counterparty or norm_name(t.counterparty) not in norm_name(text):
            continue
        cap = None
        for k in ("max_amount", "amount", "reviewed_amount", "amount_gbp"):
            if r.get(k) is not None:
                cap = to_amount(r.get(k))
                break
        if cap is not None and t.amount <= cap * 1.0001:
            return r
    return None


def explain_txn(ctx: Ctx, t: Txn) -> tuple[str, list[str], str] | None:
    """Find a document that explains a single transaction."""
    name = _who(t)
    # refund of an earlier payment: same counterparty, same amount, opposite direction, earlier
    for o in ctx.txns:
        if (o.id != t.id and o.direction != t.direction and o.counterparty and
                norm_name(o.counterparty) == norm_name(t.counterparty) and
                abs(o.amount - t.amount) <= 0.01 and o.ts and t.ts and o.ts <= t.ts and
                ("refund" in t.reference.lower() or (t.ts - o.ts).days <= 90)):
            return ("REFUND_OF_EARLIER_PAYMENT", [o.id, t.id],
                    f"{_describe(t)} refunds the earlier payment {o.id} of the same amount")
    for d in ctx.docs_of("refund"):
        if ev.mentions_name(d, name) and ev.mentions_amount(d, t.amount):
            return ("REFUND_OF_EARLIER_PAYMENT", [d.id, t.id], f"{d.id} documents {_describe(t)} as a refund")
    for d in ctx.docs_of("invoice"):
        if ev.mentions_amount(d, t.amount) and (ev.mentions_name(d, name) or not t.counterparty) \
                and ev.near_date(d, t.ts):
            return ("INVOICE_MATCHES", [d.id, t.id],
                    f"{d.id} matches {_describe(t)} on amount, payee and date")
    for kind in ("event", "loan"):
        for d in ctx.docs_of(kind):
            if ev.mentions_amount(d, t.amount, tol=0.02) and ev.near_date(d, t.ts, days=120):
                return ("DOCUMENTED_EVENT", [d.id, t.id], f"{d.id} documents the event behind {_describe(t)}")
    for d in ctx.docs_of("group"):
        if ev.mentions_name(d, name):
            return ("GROUP_COMPANY", [d.id, t.id],
                    f"{d.id} shows {name} is a verified company in the customer's group")
    for d in ctx.docs_of("own_account"):
        if ev.mentions_name(d, ctx.customer.name) and (
                ev.mentions_name(d, name) or (t.cp_account and t.cp_account in d.text) or
                _is_customer(ctx, t.cp_account_name or t.counterparty)):
            return ("OWN_ACCOUNT", [d.id, t.id],
                    f"{d.id} shows the customer holds the account behind {_describe(t)}")
    for d in ctx.docs_of("relationship"):
        if ev.mentions_name(d, name):
            return ("THIRD_PARTY_DOCUMENTED", [d.id, t.id],
                    f"{d.id} documents the customer's relationship with {name}")
    for d in ctx.docs_of("collection"):
        if ev.mentions_name(d, name):
            return ("COLLECTION_DOCUMENTED", [d.id, t.id],
                    f"{d.id} lists {name} among the people the customer collects for")
    r = _review_covers(ctx, t)
    if r is not None:
        rid = str(pick(r, "review_id", "id"))
        return ("WITHIN_PROFILE", [rid, t.id],
                f"earlier review {rid} already covered payments with {name} of this size")
    return None


def _business_records(ctx: Ctx, txns: list[Txn]) -> Doc | None:
    total = sum(t.amount for t in txns)
    for d in ctx.docs_of("business_records"):
        if any(abs(a - total) <= max(1.0, total * 0.05) for a in ev.amounts(d)):
            return d
        if txns and all(ev.mentions_amount(d, t.amount) for t in txns):
            return d
    return None


def _moved_on(ctx: Ctx, inflows: list[Txn], hours: int) -> list[Txn]:
    """Outgoing transactions within `hours` after any of the inflows."""
    outs = []
    for o in ctx.txns:
        if o.direction != "out" or o.ts is None:
            continue
        if any(i.ts and timedelta(0) <= o.ts - i.ts <= timedelta(hours=hours) for i in inflows):
            outs.append(o)
    return outs


def _dominant(codes: list[str]) -> str:
    return Counter(codes).most_common(1)[0][0]


def _close_from(alert: Alert, explained: list[tuple[str, list[str], str]], lead: str) -> Finding:
    code = _dominant([e[0] for e in explained])
    evid = [i for e in explained for i in e[1]]
    joined = "; ".join(e[2] for e in explained[:3])
    facts = [lead, joined[:1].upper() + joined[1:]]
    return Finding("CLOSE", code, alert, facts, evid)


# --------------------------------------------------------------------------
# rule handlers
# --------------------------------------------------------------------------

def tm01(ctx: Ctx, a: Alert) -> Finding:
    inflows = [t for t in ctx.win if t.direction == "in" and t.channel != "fund_sell"]
    if ctx.inst == "payments_fx" and not inflows:  # volume = payments the client sends
        inflows = [t for t in ctx.win if t.direction == "out"]
    total = sum(t.amount for t in inflows)
    exp = ctx.customer.expected_monthly_in
    ratio = total / exp if exp else None
    what = "of payment volume" if ctx.inst == "payments_fx" else "received"
    lead = (f"Alert {a.id} (TM01) flagged {fmt_gbp(total)} {what} in the review window against "
            f"declared expected volume of {fmt_gbp(exp)} a month" +
            (f" ({ratio:.1f}x)." if ratio else "."))
    subject = _alert_txns(ctx, a) or sorted(inflows, key=lambda t: -t.amount)[:5]
    subject = [t for t in subject if t.direction == "in"] or subject
    explained, unexplained = [], []
    for t in subject:
        e = explain_txn(ctx, t)
        (explained if e else unexplained).append(e or t)

    if ctx.inst == "payments_fx" and unexplained:
        outs = [t for t in ctx.win if t.direction == "out"]
        if outs and all(explain_txn(ctx, t) and explain_txn(ctx, t)[0] == "INVOICE_MATCHES" for t in outs):
            ex = [explain_txn(ctx, t) for t in outs]
            return _close_from(a, ex, lead)
        return Finding("RFI", "RFI_UPDATED_PROFILE", a,
                       [lead, "Funding came from the client but payments exceed the expected volume on file."],
                       [t.id for t in unexplained],
                       ask="an updated expected monthly payment volume with the business reason for the increase")

    if not unexplained:
        own = sum(1 for e in explained if e[0] == "OWN_ACCOUNT")
        if own and ratio is not None and ratio >= OWN_ACCOUNT_MULTIPLE:
            evid = [i for e in explained for i in e[1]]
            return Finding("RFI", "RFI_UPDATED_PROFILE", a,
                           [lead, f"The inflows are transfers from the customer's own accounts, but they take "
                                  f"activity to {ratio:.1f} times the declared level, at or above the "
                                  f"{OWN_ACCOUNT_MULTIPLE:.0f}x policy limit."],
                           evid, ask="an updated expected income or account volume")
        return _close_from(a, explained, lead)

    big = max(unexplained, key=lambda t: t.amount)
    onward = _moved_on(ctx, unexplained, 48)
    onward_hr = [o for o in onward if _is_high_risk(ctx, o.cp_country)]
    if onward_hr or (onward and sum(o.amount for o in onward) >= 0.8 * sum(t.amount for t in unexplained)):
        return Finding("ESCALATE", "ESC_UNEXPLAINED_FUNDS", a,
                       [lead, f"No document explains {_describe(big)}, and {fmt_gbp(sum(o.amount for o in onward))} "
                              f"was moved on within 48 hours ({', '.join(o.id for o in onward[:3])})."],
                       [t.id for t in unexplained] + [o.id for o in onward])
    if ctx.customer.kind == "business" and len(unexplained) >= 5 and big.amount < 0.5 * total:
        return Finding("RFI", "RFI_BUSINESS_RECORDS", a,
                       [lead, f"The uplift is spread across {len(unexplained)} receipts with no takings or "
                              f"sales records on file."],
                       [t.id for t in unexplained[:5]],
                       ask=f"business records (sales ledger or takings reports) for the review window "
                           f"reconciling the {fmt_gbp(sum(t.amount for t in unexplained))} received")
    return Finding("RFI", "RFI_SOURCE_OF_FUNDS", a,
                   [lead, f"Nothing on file explains {_describe(big)} and nothing about it looks criminal."],
                   [t.id for t in unexplained],
                   ask=f"proof of where the {fmt_gbp(big.amount)} from {_who(big)} came from and what it "
                       f"is for (for example a loan agreement, sale contract or payslip)")


def tm02(ctx: Ctx, a: Alert) -> Finding:
    hours = ctx.cfg.get("rapid_hours", 48)
    sub = _alert_txns(ctx, a)
    ins = [t for t in sub if t.direction == "in"] or \
          [t for t in ctx.win if t.direction == "in" and t.channel != "fund_sell"]
    outs = [t for t in sub if t.direction == "out"] or _moved_on(ctx, ins, hours)
    tin, tout = sum(t.amount for t in ins), sum(t.amount for t in outs)
    lead = (f"Alert {a.id} (TM02) flagged {fmt_gbp(tin)} in and {fmt_gbp(tout)} out again within "
            f"{hours // 24 if hours >= 72 else hours} {'days' if hours >= 72 else 'hours'}.")
    senders = {norm_name(t.counterparty) for t in ins if t.counterparty and not _is_customer(ctx, t.counterparty)}
    hr_out = [t for t in outs if _is_high_risk(ctx, t.cp_country) and not _declared_country(ctx, t.cp_country)]

    ex_in = [(t, explain_txn(ctx, t)) for t in ins]
    ex_out = [(t, explain_txn(ctx, t)) for t in outs]
    un_in = [t for t, e in ex_in if not e]
    un_out = [t for t, e in ex_out if not e]

    many = ctx.cfg.get("many_parties") or 6
    if len(senders) >= min(many, 6) and un_in:
        return Finding("ESCALATE", "ESC_MULE_ACTIVITY", a,
                       [lead, f"{len(senders)} different people paid in and the money left again "
                              f"with nothing on file explaining it."],
                       [t.id for t in un_in[:6]] + [t.id for t in outs[:3]])
    if hr_out and any(t in un_out for t in hr_out):
        t = hr_out[0]
        return Finding("ESCALATE", "ESC_HIGH_RISK_CORRIDOR", a,
                       [lead, f"{_describe(t)} went to {t.cp_country}, a higher-risk country the customer "
                              f"did not declare, with no document explaining it."],
                       [x.id for x in hr_out] + [x.id for x in un_in[:3]])
    if not un_in and not un_out:
        return _close_from(a, [e for _, e in ex_in + ex_out if e], lead)

    if ctx.inst == "investment":
        third = [t for t in un_in if t.counterparty and not _is_customer(ctx, t.counterparty)]
        if third:
            return Finding("ESCALATE", "ESC_THIRD_PARTY_FUNDING", a,
                           [lead, f"{_describe(third[0])} came from someone other than the customer and was "
                                  f"withdrawn with little invested."],
                           [t.id for t in third] + [t.id for t in outs])
        return Finding("RFI", "RFI_PURPOSE_DOCUMENTS", a,
                       [lead, "The deposit came from the customer and was withdrawn with little invested; "
                              "nothing on file explains why."],
                       [t.id for t in ins + outs],
                       ask="the reason the deposit was withdrawn without being invested, with any supporting document")

    linked = {norm_name(t.counterparty) for t in un_out} & senders
    if linked:
        return Finding("ESCALATE", "ESC_PASS_THROUGH", a,
                       [lead, "Money came in and went straight back out to the same or linked parties with no "
                              "credible purpose on file."],
                       [t.id for t in un_in + un_out])
    exp = ctx.customer.expected_monthly_in or 0
    if un_in and exp and sum(t.amount for t in un_in) > 2 * exp:
        big = max(un_in, key=lambda t: t.amount)
        return Finding("ESCALATE", "ESC_UNEXPLAINED_FUNDS", a,
                       [lead, f"No document explains {_describe(big)}, it is well above the declared profile, "
                              f"and it was moved on at once."],
                       [t.id for t in un_in + un_out])
    target = (un_out or un_in)[0]
    return Finding("RFI", "RFI_PURPOSE_DOCUMENTS", a,
                   [lead, f"Nothing on file shows what {_describe(target)} was for; nothing else in the case "
                          f"suggests crime."],
                   [t.id for t in un_in + un_out],
                   ask=f"documents showing what the {fmt_gbp(target.amount)} "
                       f"{'from' if target.direction == 'in' else 'to'} {_who(target)} was for "
                       f"(invoice, contract or agreement)")


def tm03(ctx: Ctx, a: Alert) -> Finding:
    lim = ctx.cfg.get("structuring_limit") or 10_000
    band = ctx.cfg.get("structuring_band", 0.10)
    sub = _alert_txns(ctx, a) or [t for t in ctx.win if lim * (1 - band) <= t.amount < lim]
    total = sum(t.amount for t in sub)
    lead = (f"Alert {a.id} (TM03) flagged {len(sub)} transactions just under the {fmt_gbp(lim)} "
            f"threshold totalling {fmt_gbp(total)}.")
    if ctx.inst == "payments_fx":
        ex = [(t, explain_txn(ctx, t)) for t in sub]
        if sub and all(e and e[0] == "INVOICE_MATCHES" for _, e in ex):
            return _close_from(a, [e for _, e in ex], lead)
        for t, e in ex:
            if not e:
                for d in ctx.docs_of("invoice"):
                    if ev.mentions_name(d, _who(t)):
                        return Finding("ESCALATE", "ESC_INVOICE_MISMATCH", a,
                                       [lead, f"{d.id} for {_who(t)} does not match {_describe(t)}; the "
                                              f"payments look split to avoid the invoice requirement."],
                                       [d.id] + [x.id for x in sub])
    d = _business_records(ctx, sub)
    if d:
        return Finding("CLOSE", "BUSINESS_RECORDS_RECONCILE", a,
                       [lead, f"{d.id} reconciles to these amounts, so they reflect the customer's normal "
                              f"business rather than splitting."],
                       [d.id] + [t.id for t in sub])
    return Finding("ESCALATE", "ESC_STRUCTURING", a,
                   [lead, f"The amounts ({', '.join(fmt_gbp(t.amount) for t in sub[:4])}) sit just below the "
                          f"limit and no business records on file explain the pattern."],
                   [t.id for t in sub])


def tm04(ctx: Ctx, a: Alert) -> Finding:
    sub = _alert_txns(ctx, a) or ctx.win
    ins = [t for t in sub if t.direction == "in"]
    outs = [t for t in sub if t.direction == "out"]
    group, direction = (ins, "payers") if len(ins) >= len(outs) else (outs, "payees")
    names = sorted({_who(t) for t in group})
    lead = (f"Alert {a.id} (TM04) flagged {len(names)} different {direction} totalling "
            f"{fmt_gbp(sum(t.amount for t in group))} in 30 days.")
    for d in ctx.docs_of("collection"):
        hit = [n for n in names if ev.mentions_name(d, n)]
        if names and len(hit) >= 0.8 * len(names):
            return Finding("CLOSE", "COLLECTION_DOCUMENTED", a,
                           [lead, f"{d.id} documents a collection on behalf of named people and lists "
                                  f"{len(hit)} of the {len(names)} {direction}."],
                           [d.id] + [t.id for t in group[:5]])
    d = _business_records(ctx, group)
    if d:
        return Finding("CLOSE", "BUSINESS_RECORDS_RECONCILE", a,
                       [lead, f"{d.id} reconciles to the {direction}."], [d.id] + [t.id for t in group[:5]])
    if direction == "payees":
        ex = [explain_txn(ctx, t) for t in group]
        if group and all(ex):
            return _close_from(a, ex, lead)
        for d in ctx.docs_of("invoice"):  # payment file listing the beneficiaries
            hit = [n for n in names if ev.mentions_name(d, n)]
            if names and len(hit) >= 0.8 * len(names):
                return Finding("CLOSE", "INVOICE_MATCHES", a,
                               [lead, f"Payment file {d.id} lists {len(hit)} of the {len(names)} beneficiaries "
                                      f"with matching amounts."], [d.id] + [t.id for t in group[:5]])
        return Finding("RFI", "RFI_PURPOSE_DOCUMENTS", a,
                       [lead, "Nothing on file shows what the payments to these beneficiaries were for."],
                       [t.id for t in group[:6]],
                       ask=f"the payment file or invoices for the payments to {', '.join(names[:4])}"
                           f"{' and others' if len(names) > 4 else ''}")
    onward = _moved_on(ctx, ins, 48)
    if onward and sum(o.amount for o in onward) >= 0.7 * sum(t.amount for t in ins):
        return Finding("ESCALATE", "ESC_MULE_ACTIVITY", a,
                       [lead, f"{fmt_gbp(sum(o.amount for o in onward))} left again within 48 hours "
                              f"({', '.join(o.id for o in onward[:3])}), the pattern of an account passing on "
                              f"other people's money."],
                       [t.id for t in ins[:6]] + [o.id for o in onward[:3]])
    return Finding("RFI", "RFI_PURPOSE_DOCUMENTS", a,
                   [lead, "Nothing on file says what these payments are, and the money stayed in the account."],
                   [t.id for t in ins[:6]],
                   ask=f"what the payments from {', '.join(names[:4])}{' and others' if len(names) > 4 else ''} "
                       f"were for, with supporting documents")


def tm05(ctx: Ctx, a: Alert) -> Finding:
    lim = ctx.cfg.get("cash_limit") or 10_000
    sub = _alert_txns(ctx, a) or [t for t in ctx.win if t.channel == "cash" and t.amount >= lim]
    if not sub:
        sub = [t for t in ctx.win if t.channel == "cash"][:1]
    t = max(sub, key=lambda x: x.amount) if sub else None
    lead = f"Alert {a.id} (TM05) flagged {_describe(t) if t else 'a large cash transaction'}."
    ex = [explain_txn(ctx, x) for x in sub]
    if sub and all(ex):
        return _close_from(a, ex, lead)
    d = _business_records(ctx, sub)
    if d:
        return Finding("CLOSE", "BUSINESS_RECORDS_RECONCILE", a,
                       [lead, f"{d.id} reconciles the cash to the customer's takings."], [d.id] + [x.id for x in sub])
    cashy = ctx.customer.kind == "business" and any(w in ctx.customer.occupation.lower() for w in CASH_BUSINESS)
    if cashy:
        return Finding("RFI", "RFI_BUSINESS_RECORDS", a,
                       [lead, f"The customer is a cash business ({ctx.customer.occupation}) but no takings "
                              f"records on file reconcile to this deposit."],
                       [x.id for x in sub],
                       ask=f"till or takings records covering the {fmt_gbp(t.amount if t else None)} cash "
                           f"deposited on {t.day if t else 'the alert date'}")
    return Finding("RFI", "RFI_SOURCE_OF_FUNDS", a,
                   [lead, "Nothing on file explains where the cash came from and nothing else suggests crime."],
                   [x.id for x in sub],
                   ask=f"proof of where the {fmt_gbp(t.amount if t else None)} cash came from "
                       f"(for example a sale receipt or withdrawal record)")


def tm06(ctx: Ctx, a: Alert) -> Finding:
    sub = _alert_txns(ctx, a) or [t for t in ctx.win if _is_high_risk(ctx, t.cp_country)]
    countries = sorted({t.cp_country for t in sub if t.cp_country})
    lead = (f"Alert {a.id} (TM06) flagged {len(sub)} payment(s) totalling {fmt_gbp(sum(t.amount for t in sub))} "
            f"to or from {', '.join(countries) or 'a higher-risk country'}.")
    if countries and all(_declared_country(ctx, c) for c in countries):
        return Finding("CLOSE", "WITHIN_PROFILE", a,
                       [lead, f"The customer declared {', '.join(countries)} when they joined, so the activity "
                              f"fits their profile."], [t.id for t in sub])
    ex = [explain_txn(ctx, t) for t in sub]
    if sub and all(ex):
        return _close_from(a, ex, lead)
    un = [t for t, e in zip(sub, ex) if not e]
    return Finding("ESCALATE", "ESC_HIGH_RISK_CORRIDOR", a,
                   [lead, f"The country was not declared and nothing on file explains {_describe(un[0]) if un else 'the payments'}."],
                   [t.id for t in un or sub])


def tm07(ctx: Ctx, a: Alert) -> Finding:
    sub = _alert_txns(ctx, a) or ctx.win[:3]
    lead = f"Alert {a.id} (TM07) flagged activity on an account dormant for 180 days or more" + \
           (f", starting with {_describe(sub[0])}." if sub else ".")
    for d in ctx.docs:
        if any(p in d.blob for p in ("new device", "different person", "not the customer", "someone else",
                                     "third party access", "account takeover", "sold the account")):
            return Finding("ESCALATE", "ESC_ACCOUNT_MISUSE", a,
                           [lead, f"{d.id} indicates someone other than the customer may be using the account."],
                           [d.id] + [t.id for t in sub])
    ex = [explain_txn(ctx, t) for t in sub]
    kyc = ctx.docs_of("kyc")
    if kyc:
        d = kyc[-1]
        return Finding("CLOSE", "RETURNING_CUSTOMER", a,
                       [lead, f"{d.id} confirms the verified customer is the one resuming use, and the activity "
                              f"is in line with their profile."],
                       [d.id] + [t.id for t in sub] + [i for e in ex if e for i in e[1]])
    if sub and all(ex):
        return _close_from(a, ex, lead)
    return Finding("RFI", "RFI_UPDATED_PROFILE", a,
                   [lead, "There is no recent verification on file to confirm the returning customer's "
                          "current circumstances."],
                   [t.id for t in sub],
                   ask="confirmation of current occupation and expected account use (updated expected income or volume)")


def tm08(ctx: Ctx, a: Alert) -> Finding:
    sub = _alert_txns(ctx, a) or [t for t in ctx.win if t.counterparty and not _is_customer(ctx, t.counterparty)]
    names = sorted({_who(t) for t in sub})
    lead = f"Alert {a.id} (TM08) flagged money to or from {', '.join(names[:3]) or 'a third party'}, not the customer."
    ex = [(t, explain_txn(ctx, t)) for t in sub]
    if sub and all(e for _, e in ex):
        return _close_from(a, [e for _, e in ex], lead)
    un = [t for t, e in ex if not e]
    un_names = sorted({_who(t) for t in un})
    if len(un_names) >= 2:
        return Finding("ESCALATE", "ESC_THIRD_PARTY_FUNDING", a,
                       [lead, f"{len(un_names)} unrelated parties ({', '.join(un_names[:3])}) fund or receive "
                              f"money from the account with no documented relationship."],
                       [t.id for t in un])
    t = un[0]
    return Finding("RFI", "RFI_COUNTERPARTY_DETAILS", a,
                   [lead, f"Nothing on file explains who {_who(t)} is or their relationship to the customer."],
                   [t.id for t in un],
                   ask=f"who {_who(t)} is, their relationship to the customer, and what the "
                       f"{fmt_gbp(t.amount)} on {t.day} was for")


def tm10(ctx: Ctx, a: Alert) -> Finding:
    sub = _alert_txns(ctx, a)
    sells = [t for t in sub if t.channel == "fund_sell"] or [t for t in ctx.win if t.channel == "fund_sell"]
    buys = [t for t in ctx.txns if t.channel == "fund_buy"]
    lead = (f"Alert {a.id} (TM10) flagged {len(sells)} fund redemption(s) totalling "
            f"{fmt_gbp(sum(t.amount for t in sells))} within 90 days of purchase.")
    for t in sells:
        for d in ctx.docs_of("event"):
            if ev.near_date(d, t.ts, 120):
                return Finding("CLOSE", "DOCUMENTED_EVENT", a,
                               [lead, f"{d.id} documents the event that needed the money."], [d.id, t.id])
    payout_third = [t for t in ctx.win if t.direction == "out" and t.channel == "transfer" and t.counterparty
                    and not _is_customer(ctx, t.counterparty)]
    if len(sells) >= 2 or (len(buys) >= 2 and sells) or payout_third:
        return Finding("ESCALATE", "ESC_EARLY_REDEMPTION", a,
                       [lead, "Units were bought and quickly sold back" +
                        (f", with proceeds paid to {_who(payout_third[0])}" if payout_third else
                         " repeatedly") + ", which looks like using the fund to move money."],
                       [t.id for t in sells] + [t.id for t in buys[:2]] + [t.id for t in payout_third[:2]])
    return Finding("RFI", "RFI_PURPOSE_DOCUMENTS", a,
                   [lead, "This is a single early redemption back to the customer with no reason on file."],
                   [t.id for t in sells + buys[:1]],
                   ask="the reason for selling the units so soon after buying them, with any supporting document")


def _list_entry_for(ctx: Ctx, a: Alert) -> dict | None:
    ref = pick(a.raw, "list_entry_id", "matched_entry_id", "internal_list_id", "list_id", "matched_list_entry",
               "measured.list_entry_id", "measured.matched_entry_id")
    for e in ctx.folder.list_entries:
        eid = str(pick(e, "entry_id", "list_id", "id"))
        if ref and eid == str(ref):
            return e
    names = [_who(t) for t in _alert_txns(ctx, a)] + [str(pick(a.raw, "matched_name", "measured.matched_name",
                                                               default=""))]
    for e in ctx.folder.list_entries:
        en = norm_name(pick(e, "name", "full_name", "entity_name"))
        if en and any(norm_name(n) == en for n in names if n):
            return e
    return None


def tm22(ctx: Ctx, a: Alert) -> Finding:
    entry = _list_entry_for(ctx, a)
    sub = _alert_txns(ctx, a)
    if entry is None:
        return Finding("RFI", "RFI_COUNTERPARTY_DETAILS", a,
                       [f"Alert {a.id} (TM22) flagged a possible internal list match.",
                        "The matched list entry could not be identified from the case files."],
                       [t.id for t in sub], ask="the counterparty's full name, date of birth and address")
    eid = str(pick(entry, "entry_id", "list_id", "id"))
    lname = str(pick(entry, "name", "full_name", "entity_name", default="the listed party"))
    lead = f"Alert {a.id} (TM22) matched {lname} on the internal list ({eid})."

    # gather what we know about the real counterparty
    texts, sources = [], []
    for t in sub:
        texts.append(" ".join(str(v) for v in t.raw.values()))
        sources.append(t.id)
    for d in ctx.docs:
        if ev.mentions_name(d, lname):
            texts.append(d.text)
            sources.append(d.id)
    blob = "\n".join(texts)
    same, diff = [], []

    ldob = pick(entry, "dob", "date_of_birth", "birth_date")
    if ldob:
        ld = to_dt(ldob)
        seen = ev.dob_values(blob)
        if ld and seen:
            (same if any(s.date() == ld.date() for s in seen) else diff).append("date of birth")
    lid = pick(entry, "id_number", "identifier", "passport_number", "company_number", "registration_number",
               "national_id")
    if lid:
        lid = str(lid)
        if lid in blob:
            same.append("identifier")
        elif any(k in blob.lower() for k in ("passport", "company number", "registration number", "id number")):
            diff.append("identifier")
    laddr = str(pick(entry, "address", "postcode", default=""))
    if laddr:
        lp, seen_pc = ev.postcodes(laddr), ev.postcodes(blob)
        if lp and seen_pc:
            (same if lp & seen_pc else diff).append("address")

    docs_used = [s for s in sources if s in ctx.folder.docs]
    if same and (len(same) >= 2 or "date of birth" in same or "identifier" in same) and not diff:
        return Finding("ESCALATE", "ESC_LISTED_PARTY", a,
                       [lead, f"The counterparty matches the entry on {', '.join(same)}, so it is the listed "
                              f"party or controlled by it."],
                       [eid] + docs_used + [t.id for t in sub])
    if diff and not same:
        return Finding("CLOSE", "LIST_MATCH_DIFFERENT_PARTY", a,
                       [lead, f"The {', '.join(diff)} on file for the counterparty "
                              f"({', '.join(docs_used) or 'transaction details'}) differ from the list entry, so it "
                              f"is a different party."],
                       [eid] + docs_used + [t.id for t in sub])
    return Finding("RFI", "RFI_COUNTERPARTY_DETAILS", a,
                   [lead, "The match rests on the name alone; nothing on file confirms or rules out that it is "
                          "the same party."],
                   [eid] + docs_used + [t.id for t in sub],
                   ask=f"the counterparty's full name, date of birth (or company number) and address so the match "
                       f"to {eid} can be ruled in or out")


HANDLERS = {"TM01": tm01, "TM02": tm02, "TM03": tm03, "TM04": tm04, "TM05": tm05, "TM06": tm06,
            "TM07": tm07, "TM08": tm08, "TM10": tm10, "TM22": tm22}


# --------------------------------------------------------------------------
# case-level checks and combination
# --------------------------------------------------------------------------

def case_checks(ctx: Ctx, findings: list[Finding]) -> list[Finding]:
    out = []
    # 1. signs of crime in the documents -> escalate, never ask
    for d in ctx.docs:
        flags = ev.red_flags(d)
        if flags:
            ins = [t for t in ctx.win if t.direction == "in" and t.counterparty and not _is_customer(ctx, t.counterparty)]
            many = len({norm_name(t.counterparty) for t in ins}) >= 3
            onward = _moved_on(ctx, ins, 72)
            code = "ESC_MULE_ACTIVITY" if many and onward else "ESC_UNEXPLAINED_FUNDS"
            out.append(Finding("ESCALATE", code, None,
                               [f"{d.id} records '{flags[0]}', a sign of crime, so the customer must not be asked "
                                f"questions that could tip them off."]
                               + ([f"{len(ins)} payments from different people came in and "
                                   f"{fmt_gbp(sum(o.amount for o in onward))} left again soon after."] if many and onward else []),
                               [d.id] + [t.id for t in ins[:4]] + [o.id for o in onward[:2]]))
            break
    # 2. payments & FX: a held payment of >= £10k to anyone but the client needs an invoice on file
    req = ctx.cfg.get("invoice_required")
    if req and ctx.case.held_txn_id and ctx.case.held_txn_id in ctx.folder.txns:
        t = ctx.folder.txns[ctx.case.held_txn_id]
        if t.amount >= req and not _is_customer(ctx, t.counterparty):
            e = explain_txn(ctx, t)
            if not e or e[0] not in ("INVOICE_MATCHES", "GROUP_COMPANY", "OWN_ACCOUNT"):
                payee_docs = [d for d in ctx.docs_of("invoice") if ev.mentions_name(d, _who(t))]
                if payee_docs:
                    out.append(Finding("ESCALATE", "ESC_INVOICE_MISMATCH", None,
                                       [f"Held payment {_describe(t)} is not supported by {payee_docs[0].id}, which "
                                        f"names the same payee but a different amount or date."],
                                       [payee_docs[0].id, t.id]))
                else:
                    out.append(Finding("RFI", "RFI_PURPOSE_DOCUMENTS", None,
                                       [f"Held payment {_describe(t)} is {fmt_gbp(req)} or more to someone other "
                                        f"than the client, and policy requires an invoice or contract on file "
                                        f"before release."],
                                       [t.id],
                                       ask=f"the invoice or contract for the {fmt_gbp(t.amount)} payment to {_who(t)}"))
    return out


def _sentences(xs: list[str]) -> list[str]:
    return [x.strip().rstrip(".") + "." for x in xs if x and x.strip()]


def _reason(primary: Finding, others: list[Finding], verdict: str) -> str:
    s = _sentences(primary.facts)[:2]
    if verdict == "RFI" and primary.ask:
        s.append(f"Ask the customer for {primary.ask}.")
    rest = [f for f in others if f is not primary and f.alert is not None]
    if rest and len(s) < 4:
        if verdict == "CLOSE":
            parts = [f"{f.alert.id} ({f.alert.rule}, {f.code.lower().replace('_', ' ')})" for f in rest[:3]]
            s.append(f"The other alerts are also explained: {', '.join(parts)}.")
        else:
            parts = [f"{f.alert.id} ({f.alert.rule}) -> {f.verdict}" for f in rest[:3]]
            s.append(f"Other alerts in the case: {', '.join(parts)}.")
    if len(s) < 2:
        s.append("One verdict covers every alert in the case.")
    return " ".join(s[:4])


def decide(folder: Folder, case: Case, inst: str, high_risk: set[str]) -> dict:
    cfg = INSTITUTIONS[inst]
    cust = folder.customers.get(case.customer_id) or Customer(case.customer_id, "", "individual", None, [], "", {})
    txns = folder.txns_by_customer.get(case.customer_id, [])
    win = [t for t in txns if in_window(t, case)]
    docs = folder.docs_by_customer.get(case.customer_id, [])
    ctx = Ctx(folder, inst, cfg, case, cust, txns, win, docs, {d.id: ev.kinds(d) for d in docs},
              folder.reviews_by_customer.get(case.customer_id, []), high_risk)

    alert_ids = list(case.alert_ids) or [a.id for a in folder.alerts.values()
                                         if pick(a.raw, "case_id") == case.id]
    alerts = [folder.alerts[i] for i in alert_ids if i in folder.alerts]

    # 1. institution-specific scenario logic (system/triage/scenarios/<inst>.py)
    from .scenarios import scenario_decide
    try:
        out = scenario_decide(ctx, alerts)
    except Exception:  # fall back to the generic handlers rather than lose the case
        out = None
    if out is not None:
        return _finish(folder, case, alert_ids, out)

    # 2. generic per-rule handlers
    findings: list[Finding] = []
    for aid in alert_ids:
        a = folder.alerts.get(aid)
        if a is None or a.rule not in HANDLERS:
            findings.append(Finding("ESCALATE", "ESC_UNEXPLAINED_FUNDS", a,
                                    [f"Alert {aid} could not be read or uses an unknown rule, so it cannot be "
                                     f"safely closed.", "It is passed to the MLRO for manual review."], []))
            continue
        try:
            findings.append(HANDLERS[a.rule](ctx, a))
        except Exception as exc:  # never drop a case: a missing case counts as an escalation anyway
            findings.append(Finding("ESCALATE", "ESC_UNEXPLAINED_FUNDS", a,
                                    [f"Alert {aid} ({a.rule}) could not be analysed automatically ({type(exc).__name__}).",
                                     "It is passed to the MLRO for manual review."], [t for t in a.txn_ids]))
    findings += case_checks(ctx, findings)
    if not findings:
        findings.append(Finding("ESCALATE", "ESC_UNEXPLAINED_FUNDS", None,
                                ["The case has no readable alerts.", "It is passed to the MLRO for manual review."]))

    def rank(f: Finding):
        rp = RULE_PRIORITY.index(f.alert.rule) if f.alert and f.alert.rule in RULE_PRIORITY else -1
        return (-SEVERITY[f.verdict], rp, f.alert.id if f.alert else "")

    findings.sort(key=rank)
    primary = findings[0]
    verdict = primary.verdict

    valid = set(folder.docs) | set(folder.txns) | {str(pick(e, "entry_id", "list_id", "id")) for e in folder.list_entries} \
        | {str(pick(r, "review_id", "id")) for rs in folder.reviews_by_customer.values() for r in rs}
    evidence: list[str] = []
    for f in [primary] + findings[1:]:
        for i in f.evidence:
            if i in valid and i not in evidence:
                evidence.append(i)
    return {
        "case_id": case.id,
        "alert_ids": alert_ids,
        "verdict": verdict,
        "reason_code": primary.code,
        "reason": _reason(primary, findings, verdict),
        "evidence": evidence[:25],
    }


def _finish(folder: Folder, case: Case, alert_ids: list[str], out: dict) -> dict:
    """Normalise a scenario module's answer: valid code for the verdict, evidence IDs that exist."""
    from .policy import CODES
    verdict, code = out["verdict"], out["reason_code"]
    assert verdict in CODES and code in CODES[verdict], (verdict, code)
    valid = set(folder.docs) | set(folder.txns) | {str(pick(e, "entry_id", "id")) for e in folder.list_entries} \
        | {str(pick(r, "review_id", "id")) for rs in folder.reviews_by_customer.values() for r in rs}
    ev_ids: list[str] = []
    for i in out.get("evidence", []):
        if i in valid and i not in ev_ids:
            ev_ids.append(i)
    return {"case_id": case.id, "alert_ids": alert_ids, "verdict": verdict, "reason_code": code,
            "reason": out["reason"], "evidence": ev_ids[:25]}


def run_folder(path: str) -> list[dict]:
    from .data import load
    folder = load(path)
    inst = detect_institution(path, folder.policy_text)
    hr = parse_high_risk(folder.policy_text)
    return [decide(folder, c, inst, hr) for c in folder.cases]
