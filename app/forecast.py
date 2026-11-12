"""30-day cash forecast and bills calendar.

Starts from the cash in your connected checking accounts, adds expected paychecks (from the detected pay
schedule), subtracts expected bills and subscriptions on their next charge dates, and spreads your usual
everyday spending evenly across the days. It's arithmetic on your own history, so it's a guide, not a promise.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlmodel import select

from app.db import session
from app.models import Account, CashAccount

STEP = {"weekly": 7, "biweekly": 14, "semimonthly": 15, "monthly": 30}


def starting_cash() -> tuple[float, list[dict]]:
    """Checking-type balances: Plaid depository accounts that aren't savings, plus manual checking/cash."""
    rows = []
    with session() as s:
        for a in s.exec(select(Account).where(Account.balance_current != None)).all():  # noqa: E711
            if "depository" in (a.type or "") and "saving" not in a.name.lower():
                rows.append({"name": a.name, "balance": round(a.balance_current, 2)})
        for c in s.exec(select(CashAccount).where(CashAccount.kind.in_(["checking", "cash"]))).all():
            rows.append({"name": c.name, "balance": round(c.balance, 2)})
    return round(sum(r["balance"] for r in rows), 2), rows


def _occurrences(first: date, step: int, start: date, end: date) -> list[date]:
    if step <= 0:
        return []
    d = first
    while d < start:
        d += timedelta(days=step)
    out = []
    while d <= end:
        out.append(d)
        d += timedelta(days=step)
    return out


def forecast(today: date | None = None, days: int = 30) -> dict:
    from app import insights, subscriptions
    from app.planning import cash_flow, detect_paychecks

    today = today or date.today()
    end = today + timedelta(days=days)
    cash, accounts = starting_cash()
    events = []

    pay = detect_paychecks(today)
    if pay.detected and pay.last_pay_date:
        for d in _occurrences(pay.last_pay_date, STEP.get(pay.frequency, 14), today + timedelta(days=1), end):
            events.append({"date": d, "name": "Paycheck", "amount": pay.avg_net_pay, "kind": "income"})

    # Bills (rent, utilities) straight from the recurring detector; subscriptions honour your decisions.
    subs = subscriptions.overview(today)
    sub_keys = {i["key"] for i in subs["items"]}
    recurring_monthly = 0.0
    for r in insights.recurring():
        if r["key"] in sub_keys or not r["active"] or r["category"] not in subscriptions.BILLS:
            continue
        recurring_monthly += r["amount"] * r["per_year"] / 12
        for d in _occurrences(date.fromisoformat(r["next_expected"]), r["cadence_days"], today + timedelta(days=1), end):
            events.append({"date": d, "name": r["merchant"], "amount": -r["amount"], "kind": "bill"})
    for i in subs["items"]:
        if i["status"] == "cancelled" or i.get("off_ledger"):
            continue
        recurring_monthly += i["monthly"]
        if i["next_expected"]:
            for d in _occurrences(date.fromisoformat(i["next_expected"]), i["cadence_days"], today + timedelta(days=1), end):
                events.append({"date": d, "name": i["merchant"], "amount": -i["amount"], "kind": "subscription"})

    cf = cash_flow(today)
    everyday = max(0.0, cf["avg_spending"] - recurring_monthly) * 12 / 365  # per day, outside bills and subscriptions
    events.sort(key=lambda e: e["date"])
    balance, lowest, lowest_day, series = cash, cash, today, [{"date": today.isoformat(), "balance": round(cash, 2)}]
    by_day: dict[date, float] = {}
    for e in events:
        by_day[e["date"]] = by_day.get(e["date"], 0) + e["amount"]
    for n in range(1, days + 1):
        d = today + timedelta(days=n)
        balance += by_day.get(d, 0) - everyday
        series.append({"date": d.isoformat(), "balance": round(balance, 2)})
        if balance < lowest:
            lowest, lowest_day = balance, d
    return {
        "start": cash, "accounts": accounts, "end": round(balance, 2), "lowest": round(lowest, 2),
        "lowest_on": lowest_day.isoformat(), "everyday_per_day": round(everyday, 2),
        "income": round(sum(e["amount"] for e in events if e["amount"] > 0), 2),
        "bills": round(-sum(e["amount"] for e in events if e["amount"] < 0), 2),
        "events": [{**e, "date": e["date"].isoformat(), "amount": round(e["amount"], 2)} for e in events],
        "series": series, "pay_detected": pay.detected, "pay_frequency": pay.frequency, "days": days,
    }
