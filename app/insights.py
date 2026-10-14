"""Deterministic insights. No LLM: these are facts the agent and dashboard can rely on."""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date

from sqlmodel import select

from app.config import NON_SPEND, load_config
from app.categorize import normalize
from app.db import session
from app.models import Txn


def _txns(since: date | None = None) -> list[Txn]:
    with session() as s:
        q = select(Txn)
        if since:
            q = q.where(Txn.date >= since)
        return s.exec(q.order_by(Txn.date)).all()


def recurring(txns: list[Txn] | None = None) -> list[dict]:
    """Charges from the same merchant roughly every month (26-35 days apart), 3+ times.
    Reports current price, previous price (if it changed), and when it last charged."""
    txns = txns if txns is not None else _txns()
    by_key: dict[str, list[Txn]] = defaultdict(list)
    for t in txns:
        if t.amount > 0 and t.category not in NON_SPEND and t.category not in ("Groceries", "Dining", "Coffee", "Transport"):
            by_key[normalize(t.raw_name)].append(t)
    out = []
    latest = max((t.date for t in txns), default=date.today())
    for key, ts in by_key.items():
        ts.sort(key=lambda t: t.date)
        if len(ts) < 3:
            continue
        gaps = [(b.date - a.date).days for a, b in zip(ts, ts[1:])]
        if not all(24 <= g <= 37 for g in gaps[-3:]):
            continue
        amounts = [round(t.amount, 2) for t in ts]
        cur = amounts[-1]
        # A price change = a stable old price (2+ charges) followed by a stable new price (2+ charges).
        # Variable bills (utilities) never qualify, so they aren't reported as "price increases".
        prev, changed_on = None, None
        for i in range(2, len(amounts) - 1):
            if (amounts[i] == amounts[i + 1] == cur and amounts[i - 1] == amounts[i - 2] != cur
                    and all(a == cur for a in amounts[i:])):
                prev, changed_on = amounts[i - 1], ts[i].date
                break
        out.append({
            "merchant": ts[-1].merchant or key.title(), "category": ts[-1].category, "amount": round(cur, 2),
            "previous_amount": round(prev, 2) if prev is not None else None,
            "price_changed_on": changed_on.isoformat() if changed_on else None,
            "annual_cost": round(cur * 12, 2), "charges": len(ts), "last_charged": ts[-1].date.isoformat(),
            "active": (latest - ts[-1].date).days <= 40,
        })
    return sorted(out, key=lambda r: -r["annual_cost"])


def duplicates(txns: list[Txn] | None = None, window_days: int = 2) -> list[dict]:
    """Same normalized merchant + same amount within `window_days`, excluding recurring bills."""
    txns = txns if txns is not None else _txns()
    seen: dict[tuple, Txn] = {}
    out = []
    for t in sorted(txns, key=lambda t: t.date):
        if t.amount <= 0 or t.category in NON_SPEND or t.pending:
            continue
        k = (normalize(t.raw_name), round(t.amount, 2))
        if k in seen and (t.date - seen[k].date).days <= window_days:
            out.append({"merchant": t.merchant or k[0].title(), "amount": t.amount, "dates": [seen[k].date.isoformat(), t.date.isoformat()],
                        "ids": [seen[k].id, t.id]})
        seen[k] = t
    return out


def unusual(txns: list[Txn] | None = None, z: float = 3.5, min_amount: float = 150) -> list[dict]:
    """Single charges far above that category's typical charge (robust z-score on median/MAD)."""
    txns = txns if txns is not None else _txns()
    by_cat: dict[str, list[float]] = defaultdict(list)
    for t in txns:
        if t.amount > 0 and t.category not in NON_SPEND:
            by_cat[t.category].append(t.amount)
    stats = {}
    for c, xs in by_cat.items():
        if len(xs) >= 8:
            med = statistics.median(xs)
            mad = statistics.median(abs(x - med) for x in xs) or 1.0
            stats[c] = (med, mad)
    out = []
    for t in txns:
        if t.category in stats and t.amount >= min_amount:
            med, mad = stats[t.category]
            score = 0.6745 * (t.amount - med) / mad
            if score >= z:
                out.append({"merchant": t.merchant, "category": t.category, "amount": t.amount,
                            "date": t.date.isoformat(), "typical": round(med, 2)})
    return sorted(out, key=lambda r: -r["amount"])


def month_summary(month: str | None = None) -> dict:
    """Spend by category for a month (YYYY-MM), income, net, and budget pacing."""
    txns = _txns()
    if not txns:
        return {"month": month, "categories": [], "spend": 0, "income": 0, "net": 0}
    month = month or max(t.date for t in txns).strftime("%Y-%m")
    cur = [t for t in txns if t.date.strftime("%Y-%m") == month]
    spend: dict[str, float] = defaultdict(float)
    income = 0.0
    for t in cur:
        if t.category == "Income":
            income += -t.amount
        elif t.category not in NON_SPEND:
            spend[t.category or "Uncategorized"] += t.amount
    budgets = load_config().get("budgets") or {}
    y, m = map(int, month.split("-"))
    days_in = ((date(y + (m == 12), m % 12 + 1, 1)) - date(y, m, 1)).days
    in_progress = month == date.today().strftime("%Y-%m")
    last_day = date.today().day if in_progress else days_in
    cats = []
    for c, v in sorted(spend.items(), key=lambda kv: -kv[1]):
        b = budgets.get(c)
        cats.append({"category": c, "spent": round(v, 2), "budget": b,
                     "projected": round(v / last_day * days_in, 2) if b and last_day < days_in else round(v, 2),
                     "over": bool(b and v > b)})
    total = sum(spend.values())
    return {"month": month, "categories": cats, "spend": round(total, 2), "income": round(income, 2),
            "net": round(income - total, 2), "partial": in_progress}


def all_insights() -> dict:
    txns = _txns()
    subs = recurring(txns)
    return {
        "subscriptions": subs,
        "price_increases": [s for s in subs if s["previous_amount"] is not None and s["amount"] > s["previous_amount"]],
        "duplicates": duplicates(txns),
        "unusual": unusual(txns),
    }


def months() -> list[str]:
    return sorted({t.date.strftime("%Y-%m") for t in _txns()}, reverse=True)



