"""Load one institution folder into normalised, read-only records.

The exact field names in the hackathon files are not known in advance, so
every field is read through `pick()` with a list of likely aliases. When the
real data arrives, run `python3 tools/explore.py institutions/bank` and add
any missing aliases here - nothing else in the engine reads raw field names.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def read_jsonl(path: str) -> list[dict]:
    out: list[dict] = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def pick(d: Any, *keys: str, default: Any = None) -> Any:
    if not isinstance(d, dict):
        return default
    for k in keys:
        if "." in k:  # nested lookup, e.g. "profile.declared_income"
            cur: Any = d
            for part in k.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            if cur not in (None, "", []):
                return cur
            continue
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return default


_MONEY_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def to_amount(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        return to_amount(pick(v, "amount", "value", "gbp", "amount_gbp"))
    m = _MONEY_RE.search(str(v))
    return float(m.group(0).replace(",", "")) if m else None


def to_dt(v: Any) -> datetime | None:
    if not v:
        return None
    s = str(v).strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:26] if "." in s else s[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except ValueError:
        return None


def as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str) and "," in v:
        return [x.strip() for x in v.split(",") if x.strip()]
    return [v]


_CORP = re.compile(r"\b(ltd|limited|plc|llp|llc|inc|gmbh|sa|bv|co)\b\.?", re.I)


def norm_name(s: Any) -> str:
    if not s:
        return ""
    s = _CORP.sub(" ", str(s).lower())
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(s.split())


def fmt_gbp(x: float | None) -> str:
    if x is None:
        return "an unknown amount"
    return f"£{x:,.2f}" if x != int(x) else f"£{int(x):,}"


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------

@dataclass
class Txn:
    id: str
    customer_id: str
    ts: datetime | None
    amount: float
    direction: str            # "in" | "out"
    channel: str              # cash | card | transfer | fund_buy | fund_sell
    counterparty: str
    cp_country: str
    cp_account_name: str
    cp_account: str
    reference: str
    raw: dict

    @property
    def day(self) -> str:
        return self.ts.strftime("%Y-%m-%d") if self.ts else "unknown date"


@dataclass
class Doc:
    id: str
    customer_id: str
    type: str
    title: str
    text: str
    raw: dict
    dated: datetime | None = None
    fields: dict = field(default_factory=dict)   # "**Key:** value" lines, lower-cased keys

    @property
    def blob(self) -> str:
        return f"{self.id} {self.type} {self.title} {self.text}".lower()


@dataclass
class Alert:
    id: str
    rule: str
    txn_ids: list[str]
    measured: dict
    raw: dict


@dataclass
class Customer:
    id: str
    name: str
    kind: str                       # individual | business
    expected_monthly_in: float | None
    countries: list[str]
    occupation: str
    raw: dict


@dataclass
class Case:
    id: str
    customer_id: str
    alert_ids: list[str]
    start: datetime | None
    end: datetime | None
    held_txn_id: str | None
    raw: dict


@dataclass
class Folder:
    path: str
    policy_text: str
    cases: list[Case]
    alerts: dict[str, Alert]
    customers: dict[str, Customer]
    txns_by_customer: dict[str, list[Txn]]
    txns: dict[str, Txn]
    docs_by_customer: dict[str, list[Doc]]
    docs: dict[str, Doc]
    reviews_by_customer: dict[str, list[dict]]
    list_entries: list[dict]


# --------------------------------------------------------------------------
# normalisers
# --------------------------------------------------------------------------

_IN_WORDS = ("in", "credit", "cr", "inbound", "incoming", "deposit", "top_up", "topup",
             "top-up", "received", "receipt", "funding", "subscription_in")


def _direction(r: dict, amount: float | None) -> str:
    d = str(pick(r, "direction", "dr_cr", "credit_debit", "flow", default="")).lower()
    if d:
        return "in" if any(d == w or d.startswith(w) for w in _IN_WORDS) else "out"
    t = str(pick(r, "type", "transaction_type", "txn_type", default="")).lower()
    if any(w in t for w in ("deposit", "top", "credit", "incoming", "received")):
        return "in"
    if any(w in t for w in ("withdraw", "payment_out", "debit", "outgoing", "sent")):
        return "out"
    return "out" if (amount or 0) < 0 else "in"


def _channel(r: dict) -> str:
    c = str(r.get("channel") or "").lower()
    if c in ("fund_subscription",):
        return "fund_buy"
    if c in ("fund_redemption",):
        return "fund_sell"
    t = " ".join(str(pick(r, k, default="")) for k in
                 ("channel", "method", "type", "transaction_type", "txn_type", "rail")).lower()
    if "atm" in t or "cash" in t:
        return "cash"
    if "card" in t or "pos" in t:
        return "card"
    if "redeem" in t or "redemption" in t or "sell" in t or "sale_of_units" in t:
        return "fund_sell"
    if "buy" in t or "purchase" in t or "subscription" in t:
        return "fund_buy"
    return "transfer"


def norm_txn(r: dict) -> Txn:
    amt = to_amount(pick(r, "amount_gbp", "amount", "gbp_amount", "value", "amount_in_gbp"))
    cp = pick(r, "counterparty_name", "counterparty", "payee", "payer", "beneficiary",
              "beneficiary_name", "sender", "recipient", "sender_name", "recipient_name",
              "merchant", "description", default="")
    if isinstance(cp, dict):
        cp = pick(cp, "name", "account_name", default="")
    return Txn(
        id=str(pick(r, "transaction_id", "txn_id", "id")),
        customer_id=str(pick(r, "customer_id", "client_id", "customer")),
        ts=to_dt(pick(r, "booked_at", "timestamp", "date", "datetime", "created_at", "value_date")),
        amount=abs(amt or 0.0),
        direction=_direction(r, amt),
        channel=_channel(r),
        counterparty=str(cp or ""),
        cp_country=str(pick(r, "counterparty_country", "country", "beneficiary_country",
                            "sender_country", "recipient_country", "cp_country",
                            "counterparty.country", default="")),
        cp_account_name=str(pick(r, "counterparty_account_name", "account_holder_name",
                                 "account_name", "remitter_account_name", "cp_account_name",
                                 "counterparty.account_name", default="")),
        cp_account=str(pick(r, "counterparty_account", "counterparty_iban", "iban",
                            "account_number", "cp_account", "counterparty.account", default="")),
        reference=str(pick(r, "reference", "narrative", "memo", "payment_reference", default="")),
        raw=r,
    )


def norm_alert(r: dict) -> Alert:
    rule = str(pick(r, "rule_id", "rule", "rule_code", "scenario", default="")).upper()
    m = re.search(r"TM\s*0*(\d+)", rule)
    rule = f"TM{int(m.group(1)):02d}" if m else rule
    txns = as_list(pick(r, "transaction_ids", "txn_ids", "transactions", "triggering_transactions",
                        "fired_on"))
    txns = [str(t.get("id") if isinstance(t, dict) else t) for t in txns]
    measured = pick(r, "measured", "metrics", "details", "values", default=None)
    if not isinstance(measured, dict):
        measured = {k: v for k, v in r.items() if k not in ("transaction_ids", "txn_ids")}
    return Alert(id=str(pick(r, "alert_id", "id")), rule=rule, txn_ids=txns,
                 measured=measured, raw=r)


def norm_customer(r: dict) -> Customer:
    monthly = to_amount(pick(r, "declared.monthly_income_gbp", "expected_monthly_inflow", "expected_monthly_turnover",
                             "declared_monthly_income", "monthly_income", "expected_monthly_volume",
                             "declared_monthly_turnover", "profile.expected_monthly_inflow",
                             "declared.monthly_turnover", "declared.monthly_income"))
    if monthly is None:
        annual = to_amount(pick(r, "declared.annual_turnover_gbp", "declared_annual_income", "annual_income", "annual_turnover",
                                "expected_annual_turnover", "declared_income", "declared_turnover",
                                "expected_annual_volume", "declared.annual_income",
                                "declared.annual_turnover"))
        monthly = annual / 12 if annual else None
    countries = as_list(pick(r, "declared.expected_countries", "expected_countries", "declared_countries", "countries",
                             "trading_countries", "declared.countries", default=[]))
    kind = str(pick(r, "kind", "customer_type", "type", "segment", "entity_type", default="")).lower()
    return Customer(
        id=str(pick(r, "customer_id", "client_id", "id")),
        name=str(pick(r, "name", "full_name", "legal_name", "company_name", "customer_name",
                      default="")),
        kind="business" if any(w in kind for w in ("bus", "company", "corp", "sme", "ltd")) else
        "individual",
        expected_monthly_in=monthly,
        countries=[str(c) for c in countries],
        occupation=str(pick(r, "business_activity", "occupation", "industry", "nature_of_business",
                            "employment", default="")),
        raw=r,
    )


def norm_case(r: dict) -> Case:
    held = pick(r, "held_payment_id", "held_transaction_id", "held_payment", "held_txn_id")
    if isinstance(held, dict):
        held = pick(held, "txn_id", "transaction_id", "id")
    window = pick(r, "review_window", "window", default={})
    return Case(
        id=str(pick(r, "case_id", "id")),
        customer_id=str(pick(r, "customer_id", "client_id", "customer")),
        alert_ids=[str(a) for a in as_list(pick(r, "alert_ids", "alerts", default=[]))],
        start=to_dt(pick(r, "window_start", "review_start", "start", "from", default=None)
                    or pick(window, "start", "from")),
        end=to_dt(pick(r, "window_end", "review_end", "end", "to", default=None)
                  or pick(window, "end", "to")),
        held_txn_id=str(held) if held else None,
        raw=r,
    )


def load(folder: str) -> Folder:
    j = lambda name: read_jsonl(os.path.join(folder, name))
    policy = ""
    pp = os.path.join(folder, "POLICY.md")
    if os.path.exists(pp):
        with open(pp, encoding="utf-8") as f:
            policy = f.read()

    alerts = {a.id: a for a in map(norm_alert, j("alerts.jsonl"))}
    customers = {c.id: c for c in map(norm_customer, j("customers.jsonl"))}

    txns: dict[str, Txn] = {}
    by_cust: dict[str, list[Txn]] = {}
    for t in map(norm_txn, j("transactions.jsonl")):
        txns[t.id] = t
        by_cust.setdefault(t.customer_id, []).append(t)
    for lst in by_cust.values():
        lst.sort(key=lambda t: (t.ts or datetime.min, t.id))

    docs: dict[str, Doc] = {}
    docs_by: dict[str, list[Doc]] = {}
    ev_dir = os.path.join(folder, "evidence")
    for r in j("evidence.jsonl"):
        did = str(pick(r, "doc_id", "evidence_id", "document_id", "id"))
        text = ""
        fname = pick(r, "path", "file", "filename", default=f"{did}.md")
        p = os.path.join(folder, fname) if os.sep in str(fname) else os.path.join(ev_dir, str(fname))
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                text = f.read()
        d = Doc(id=did, customer_id=str(pick(r, "customer_id", "client_id", "customer")),
                type=str(pick(r, "type", "doc_type", "document_type", "category", default="")),
                title=str(pick(r, "title", "name", "summary", "description", default="")),
                text=text, raw=r, dated=to_dt(pick(r, "dated", "date")), fields=md_fields(text))
        docs[did] = d
        docs_by.setdefault(d.customer_id, []).append(d)
    for lst in docs_by.values():
        lst.sort(key=lambda d: d.id)

    reviews: dict[str, list[dict]] = {}
    for r in j("previous_reviews.jsonl"):
        reviews.setdefault(str(pick(r, "customer_id", "client_id", "customer")), []).append(r)

    cases = [norm_case(r) for r in j("cases.jsonl")]
    return Folder(path=folder, policy_text=policy, cases=cases, alerts=alerts, customers=customers,
                  txns_by_customer=by_cust, txns=txns, docs_by_customer=docs_by, docs=docs,
                  reviews_by_customer=reviews, list_entries=j("internal_list.jsonl"))


_FIELD_RE = re.compile(r"^\*\*(.+?):\*\*\s*(.*)$", re.M)


def md_fields(text: str) -> dict:
    return {m.group(1).strip().lower(): m.group(2).strip() for m in _FIELD_RE.finditer(text or "")}


def in_window(t: Txn, case: Case, pad_days: int = 0) -> bool:
    if t.ts is None:
        return True
    if case.start and t.ts < case.start - timedelta(days=pad_days):
        return False
    if case.end and t.ts > case.end + timedelta(days=pad_days, hours=23, minutes=59):
        return False
    return True
