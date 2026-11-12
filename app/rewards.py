"""Credit card rewards: which card to use where, and what that's worth.

You enter each card's earn rates (points or % back per $1, by category) and what a point is worth to you.
Northstar adds up the last 12 months of spending by category and shows what you earned with the cards you
actually used, the best card for each category, and the rewards you'd have earned using it.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from sqlmodel import select

from app.config import NON_SPEND
from app.db import session
from app.models import Account, CardReward, Txn

SKIP = {"Rent & Mortgage"}  # usually can't go on a card without a fee


def cards() -> list[dict]:
    with session() as s:
        accts = {a.id: a for a in s.exec(select(Account)).all()}
        rewards = {r.account_id: r for r in s.exec(select(CardReward)).all()}
    out = []
    for a in accts.values():
        if "credit" not in (a.type or "") and a.id not in rewards:
            continue
        r = rewards.get(a.id)
        out.append({"account_id": a.id, "name": a.name + (f" ••{a.mask}" if a.mask else ""), "set_up": bool(r),
                    "base_rate": r.base_rate if r else 1.0, "rates": json.loads(r.rates) if r else {},
                    "point_value_cents": r.point_value_cents if r else 1.0})
    return out


def save(account_id: str, base_rate: float, rates: dict[str, float], point_value_cents: float) -> None:
    with session() as s:
        r = s.exec(select(CardReward).where(CardReward.account_id == account_id)).first() or CardReward(account_id=account_id)
        r.base_rate, r.point_value_cents = base_rate, point_value_cents
        r.rates = json.dumps({k: v for k, v in rates.items() if v})
        s.add(r)
        s.commit()


def value_per_dollar(card: dict, category: str) -> float:
    return card["rates"].get(category, card["base_rate"]) * card["point_value_cents"] / 100


def analysis(today: date | None = None) -> dict:
    today = today or date.today()
    set_up = [c for c in cards() if c["set_up"]]
    by_id = {c["account_id"]: c for c in set_up}
    with session() as s:
        txns = s.exec(select(Txn).where(Txn.date >= today - timedelta(days=365), Txn.amount > 0)).all()
    cats: dict[str, dict] = {}
    earned = 0.0
    for t in txns:
        if t.category in NON_SPEND or t.category in SKIP or not t.category or t.tag:
            continue
        c = cats.setdefault(t.category, {"category": t.category, "spend": 0.0, "earned": 0.0})
        c["spend"] += t.amount
        if t.account_id in by_id:
            v = t.amount * value_per_dollar(by_id[t.account_id], t.category)
            c["earned"] += v
            earned += v
    rows = []
    for c in cats.values():
        best = max(set_up, key=lambda k: value_per_dollar(k, c["category"]), default=None)
        best_value = c["spend"] * value_per_dollar(best, c["category"]) if best else 0.0
        rows.append({**c, "spend": round(c["spend"], 2), "earned": round(c["earned"], 2),
                     "best_card": best["name"] if best else None,
                     "best_rate": best["rates"].get(c["category"], best["base_rate"]) if best else None,
                     "best_value": round(best_value, 2), "missed": round(max(0.0, best_value - c["earned"]), 2)})
    rows.sort(key=lambda r: -r["missed"])
    optimal = round(sum(r["best_value"] for r in rows), 2)
    return {"cards": cards(), "rows": rows, "earned": round(earned, 2), "optimal": optimal,
            "missed": round(max(0.0, optimal - earned), 2), "set_up": bool(set_up)}
