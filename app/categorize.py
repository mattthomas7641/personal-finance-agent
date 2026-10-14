"""Categorization cascade, cheapest first:

1. rules          — your corrections (exact on normalized descriptor), then built-ins for card
                     payments/transfers/payroll/fees so nothing is double counted
2. merchant memory — the same normalized descriptor was already categorized before
3. Plaid category  — Plaid's personal_finance_category, mapped to our taxonomy (free, when present)
4. LLM batch       — only the remaining *unique* descriptors, ~80 per structured-output call

So a year of transactions usually costs one or two LLM calls, and corrections stick forever.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel
from sqlmodel import select

from app.config import CATEGORIES
from app.db import session
from app.models import Rule, Txn

_STRIP_PREFIX = re.compile(r"^(SQ|TST|PP|PAYPAL|SP|DD|IC|PY|BT)\s*\*\s*", re.I)
_NOISE = re.compile(r"(#?\d[\d\-]{2,}|\b(PPD|WEB PAY|PAYMNT|PENDING|ONLINE|ID:?\S*|STORE)\b|[*#])", re.I)


def normalize(raw: str) -> str:
    """'SQ *BLUE BOTTLE COFFEE 0412' -> 'BLUE BOTTLE COFFEE'. Used as the rule/memory key."""
    s = _STRIP_PREFIX.sub("", raw.strip().upper())
    s = _NOISE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


# High-precision patterns every real bank export needs. Without the Transfers rules, paying your
# credit card would be counted twice: once as the card purchases, once as the checking payment.
BUILTIN = [
    (re.compile(r"PAYMENT\s*(-\s*)?THANK YOU|AUTOPAY|AUTO PAY|CRCARDPMT|CREDIT CARD PAYMENT|CARD PAYMENT|EPAY|PAYMENT RECEIVED", re.I), "Transfers"),
    (re.compile(r"TRANSFER (TO|FROM)|ONLINE TRANSFER|ZELLE|VENMO|CASH APP|ROBINHOOD|VANGUARD|FIDELITY|SCHWAB|WEALTHFRONT|BETTERMENT", re.I), "Transfers"),
    (re.compile(r"PAYROLL|DIRECT DEP|DIR DEP|SALARY", re.I), "Income"),
    (re.compile(r"INTEREST CHARGE|LATE FEE|ANNUAL FEE|SERVICE FEE|OVERDRAFT|FOREIGN TRANSACTION FEE|ATM FEE", re.I), "Fees & Interest"),
]


def builtin_category(raw: str) -> str | None:
    return next((cat for pat, cat in BUILTIN if pat.search(raw)), None)


def display_name(key: str) -> str:
    return " ".join(w.capitalize() if len(w) > 3 else w for w in key.split()).replace("Amzn Mktp Us", "Amazon")


# Plaid personal_finance_category (primary / detailed) -> our taxonomy
PLAID_MAP = {
    "INCOME": "Income", "TRANSFER_IN": "Transfers", "TRANSFER_OUT": "Transfers", "LOAN_PAYMENTS": "Transfers",
    "BANK_FEES": "Fees & Interest", "ENTERTAINMENT": "Entertainment", "FOOD_AND_DRINK": "Dining",
    "FOOD_AND_DRINK_GROCERIES": "Groceries", "FOOD_AND_DRINK_COFFEE": "Coffee", "GENERAL_MERCHANDISE": "Shopping",
    "HOME_IMPROVEMENT": "Shopping", "MEDICAL": "Health & Fitness", "PERSONAL_CARE": "Personal Care",
    "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS": "Health & Fitness", "GENERAL_SERVICES": "Other",
    "GOVERNMENT_AND_NON_PROFIT": "Other", "TRANSPORTATION": "Transport", "TRAVEL": "Travel",
    "RENT_AND_UTILITIES": "Utilities", "RENT_AND_UTILITIES_RENT": "Rent & Mortgage",
    "ENTERTAINMENT_TV_AND_MOVIES": "Subscriptions", "ENTERTAINMENT_MUSIC_AND_AUDIO": "Subscriptions",
}


def from_plaid(pfc: dict | None) -> str | None:
    if not pfc:
        return None
    return PLAID_MAP.get(pfc.get("detailed", "")) or PLAID_MAP.get(pfc.get("primary", ""))


class Labeled(BaseModel):
    key: str
    merchant: str
    category: Literal[tuple(CATEGORIES)]  # type: ignore[valid-type]


class Batch(BaseModel):
    items: list[Labeled]


INSTRUCTIONS = f"""You categorize bank transaction descriptors for a personal budget.
Categories: {", ".join(CATEGORIES)}.
- "Subscriptions" = recurring digital services (streaming, apps, cloud storage). Gyms go to "Health & Fitness".
- Payroll/direct deposit = "Income". Moves between own accounts, Venmo/Zelle to people = "Transfers".
- Ride-hail, transit, bikeshare, taxis = "Transport". Food delivery and restaurants = "Dining".
- Return every key exactly as given, plus a short clean merchant name (e.g. "Blue Bottle Coffee")."""


async def llm_label(keys: list[str], examples: str = "") -> tuple[dict[str, Labeled], float]:
    from app.llm import structured

    out: dict[str, Labeled] = {}
    total = 0.0
    for i in range(0, len(keys), 80):
        chunk = keys[i:i + 80]
        res, cost = await structured(
            role="categorize",
            instructions=INSTRUCTIONS,
            cached_context="Examples of the user's own corrections:\n" + (examples or "(none)"),
            user="Descriptors (amount sign: + spend, - money in):\n" + "\n".join(chunk),
            output=Batch, effort="low", max_tokens=8000,
        )
        total += cost
        for it in res.items:
            out[it.key.split("  (")[0].strip()] = it
    return out, total


async def categorize_pending(use_llm: bool = True) -> dict:
    """Categorize every uncategorized transaction through the cascade."""
    with session() as s:
        rules = {r.pattern: r for r in s.exec(select(Rule)).all()}
        memory = {}
        for t in s.exec(select(Txn).where(Txn.category != "")).all():
            memory.setdefault(normalize(t.raw_name), (t.category, t.merchant))
        todo = s.exec(select(Txn).where(Txn.category == "")).all()

    stats = {"rule": 0, "memory": 0, "plaid": 0, "llm": 0, "left": 0, "cost_usd": 0.0}
    need: dict[str, list[Txn]] = {}
    for t in todo:
        key = normalize(t.raw_name)
        if key in rules:
            t.category, t.merchant, t.category_source = rules[key].category, rules[key].merchant or display_name(key), "rule"
        elif cat := builtin_category(t.raw_name):
            t.category, t.category_source = cat, "rule"
            t.merchant = t.merchant or display_name(key)
        elif key in memory:
            t.category, t.merchant = memory[key]
            t.category_source = "memory"
        elif t.plaid_category:
            t.category, t.category_source = t.plaid_category, "plaid"
            t.merchant = t.merchant or display_name(key)
            memory[key] = (t.category, t.merchant)
        else:
            need.setdefault(key, []).append(t)
            continue
        stats[t.category_source] += 1

    if need and use_llm:
        examples = "\n".join(f"{k} -> {r.category}" for k, r in list(rules.items())[:40])
        keys = [f"{k}  ({'+' if v[0].amount > 0 else '-'}{abs(v[0].amount):.0f})" for k, v in need.items()]
        labels, cost = await llm_label(keys, examples)
        stats["cost_usd"] = round(cost, 4)
        for key, txns in need.items():
            lab = labels.get(key)
            for t in txns:
                if lab:
                    t.category, t.merchant, t.category_source = lab.category, lab.merchant, "llm"
                    stats["llm"] += 1
                else:
                    stats["left"] += 1
    else:
        stats["left"] = sum(len(v) for v in need.values())

    with session() as s:
        for t in todo:
            if t.category:
                s.merge(t)
        s.commit()
    return stats


def correct(txn_id: str, category: str, apply_to_similar: bool = True) -> int:
    """User fixes a category: save a rule and re-label every transaction with the same descriptor."""
    with session() as s:
        t = s.get(Txn, txn_id)
        key = normalize(t.raw_name)
        rule = s.exec(select(Rule).where(Rule.pattern == key)).first() or Rule(pattern=key, category=category)
        rule.category, rule.merchant = category, t.merchant
        s.add(rule)
        targets = [x for x in s.exec(select(Txn)).all() if normalize(x.raw_name) == key] if apply_to_similar else [t]
        for x in targets:
            x.category, x.category_source = category, "user"
            s.add(x)
        s.commit()
        return len(targets)
