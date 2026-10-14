"""Overview, transactions and category rules."""
from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlmodel import func, select

from app import cash, fire, insights, planning
from app.categorize import correct
from app.config import CATEGORIES, NON_SPEND, load_config
from app.db import session
from app.models import Account, AgentRun, PlaidItem, Rule, Txn
from app.web.common import STATUS, back, render

router = APIRouter()


# ---------------- dashboard ----------------

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, month: str = ""):
    months = insights.months()
    if not months:
        return back("/connect")
    month = month if month in months else months[0]
    summary = insights.month_summary(month)
    ins = insights.all_insights()
    # 12-month spend trend
    with session() as s:
        rows = s.exec(select(Txn.date, Txn.amount, Txn.category)).all()
    trend = defaultdict(float)
    for d, a, c in rows:
        if c not in NON_SPEND and a > 0:
            trend[d.strftime("%Y-%m")] += a
    trend_items = sorted(trend.items())[-12:]
    with session() as s:
        uncategorized = s.exec(select(func.count()).select_from(Txn).where(Txn.category == "")).one()
        recent = s.exec(select(AgentRun).order_by(AgentRun.id.desc()).limit(5)).all()
    try:
        goals_ov = planning.overview()
        register = fire.goal_register(goals_ov, fire.plan())
    except Exception:
        goals_ov, register = None, []
    with session() as s:
        attention = s.exec(select(PlaidItem).where(PlaidItem.status != "ok")).all()
    return render(request, "dashboard.html", month=month, months=months, s=summary, ins=ins, attention=attention, cash=cash.summary(),
                  trend=trend_items, uncategorized=uncategorized, recent=recent, gov=goals_ov,
                  register=register, nw=fire.net_worth())


@router.get("/status", response_class=HTMLResponse)
def status(request: Request):
    if not STATUS["busy"]:
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    return render(request, "_status.html")


# ---------------- transactions ----------------

@router.get("/transactions", response_class=HTMLResponse)
def transactions(request: Request, month: str = "", category: str = "", q: str = "", source: str = ""):
    with session() as s:
        stmt = select(Txn, Account.name).join(Account, Account.id == Txn.account_id, isouter=True)
        if month:
            stmt = stmt.where(func.strftime("%Y-%m", Txn.date) == month)
        if category:
            stmt = stmt.where(Txn.category == ("" if category == "Uncategorized" else category))
        if q:
            stmt = stmt.where(Txn.raw_name.ilike(f"%{q}%") | Txn.merchant.ilike(f"%{q}%"))
        if source:
            stmt = stmt.where(Txn.category_source == source)
        rows = s.exec(stmt.order_by(Txn.date.desc()).limit(500)).all()
    total = round(sum(t.amount for t, _ in rows if t.category not in NON_SPEND), 2)
    return render(request, "transactions.html", rows=rows, categories=CATEGORIES, months=insights.months(),
                  f={"month": month, "category": category, "q": q, "source": source}, total=total)


@router.post("/txn/{txn_id}/category", response_class=HTMLResponse)
def txn_category(txn_id: str, category: str = Form(...)):
    n = correct(txn_id, category)
    return HTMLResponse(f'<span class="text-xs text-emerald-700">saved · {n} similar updated · rule learned</span>')


# ---------------- rules ----------------

@router.get("/rules", response_class=HTMLResponse)
def rules(request: Request):
    with session() as s:
        rs = s.exec(select(Rule).order_by(Rule.id.desc())).all()
        by_source = dict(s.exec(select(Txn.category_source, func.count()).group_by(Txn.category_source)).all())
    return render(request, "rules.html", rules=rs, by_source=by_source, budgets=load_config().get("budgets") or {})


@router.post("/rules/{rid}/delete")
def rule_delete(rid: int):
    with session() as s:
        if r := s.get(Rule, rid):
            s.delete(r)
            s.commit()
    return back("/rules")
