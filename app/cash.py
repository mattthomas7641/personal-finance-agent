"""Manually tracked cash accounts (HYSA, CDs, banks Plaid can't reach): balances, interest, detected deposits."""
from __future__ import annotations

from datetime import date, timedelta

from sqlmodel import select

from app.db import session
from app.models import CashAccount, Txn


def accounts() -> list[CashAccount]:
    with session() as s:
        return s.exec(select(CashAccount).order_by(CashAccount.id)).all()


def _last_full_months(today: date, n: int = 3) -> list[str]:
    out, d = [], today.replace(day=1)
    for _ in range(n):
        d = (d - timedelta(days=1)).replace(day=1)
        out.append(d.strftime("%Y-%m"))
    return out


def monthly_deposits(pattern: str, today: date | None = None) -> tuple[float, int]:
    """Average monthly money moved into the account over the last 3 complete months, from your connected
    accounts' transactions whose description contains `pattern` (outflows from checking, net of withdrawals back)."""
    if not pattern.strip():
        return 0.0, 0
    today = today or date.today()
    months = set(_last_full_months(today))
    pat = pattern.strip().lower()
    with session() as s:
        rows = [t for t in s.exec(select(Txn)).all()
                if t.date.strftime("%Y-%m") in months and pat in (t.raw_name or "").lower()]
    net = sum(t.amount for t in rows)  # +out of checking into savings, -back from savings
    return round(net / 3, 2), len(rows)


def summary(today: date | None = None) -> dict:
    today = today or date.today()
    rows = []
    for a in accounts():
        dep, n = monthly_deposits(a.detect_pattern, today)
        rows.append({
            "id": a.id, "name": a.name, "kind": a.kind, "balance": a.balance, "apy_pct": a.apy_pct,
            "interest_month": round(a.balance * a.apy_pct / 100 / 12, 2), "interest_year": round(a.balance * a.apy_pct / 100, 2),
            "as_of": a.as_of.isoformat() if a.as_of else None,
            "stale": bool(a.as_of and (today - a.as_of).days > 45),
            "detect_pattern": a.detect_pattern, "monthly_deposits": dep, "deposit_transactions": n,
        })
    return {"accounts": rows, "total": round(sum(r["balance"] for r in rows), 2),
            "interest_year": round(sum(r["interest_year"] for r in rows), 2)}
