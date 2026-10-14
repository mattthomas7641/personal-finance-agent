"""Overview, transactions, subscriptions and category rules."""
from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlmodel import func, select

from app import cash, fire, insights, planning, subscriptions
from app.categorize import correct
from app.config import CATEGORIES, NON_SPEND
from app.db import session
from app.models import Account, AgentRun, Dismissal, PlaidItem, Rule, Txn
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
    prev_month = months[months.index(month) + 1] if months.index(month) + 1 < len(months) else None
    prev = insights.month_summary(prev_month) if prev_month else None
    ins = insights.all_insights()
    with session() as s:
        rows = s.exec(select(Txn.date, Txn.amount, Txn.category)).all()
    trend = defaultdict(float)
    for d, a, c in rows:
        if c not in NON_SPEND and a > 0:
            trend[d.strftime("%Y-%m")] += a
    trend_items = sorted(trend.items())[-12:]
    with session() as s:
        uncategorized = s.exec(select(func.count()).select_from(Txn).where(Txn.category == "")).one()
        digest = s.exec(select(AgentRun).where(AgentRun.kind == "digest").order_by(AgentRun.id.desc())).first()
        attention = s.exec(select(PlaidItem).where(PlaidItem.status != "ok")).all()
    try:
        goals_ov = planning.overview()
        fp = fire.plan()
        register = fire.goal_register(goals_ov, fp)
    except Exception:
        goals_ov, fp, register = None, None, []
    nw = fire.snapshot()
    budget_total = sum(c["budget"] for c in summary["categories"] if c["budget"])
    budget_spent = sum(c["spent"] for c in summary["categories"] if c["budget"])
    return render(request, "dashboard.html", month=month, months=months, s=summary, prev=prev, ins=ins, attention=attention,
                  cash=cash.summary(), trend=trend_items, uncategorized=uncategorized, gov=goals_ov, fp=fp,
                  register=register, nw=nw, history=fire.history(), alerts=insights.alerts(ins), digest=digest,
                  subs=subscriptions.overview(), budget_total=budget_total, budget_spent=budget_spent)


@router.post("/alerts/dismiss")
def alert_dismiss(key: str = Form(...)):
    with session() as s:
        s.merge(Dismissal(key=key[:200]))
        s.commit()
    return HTMLResponse("")


@router.get("/status", response_class=HTMLResponse)
def status(request: Request):
    if not STATUS["busy"]:
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    return render(request, "_status.html")


# ---------------- transactions ----------------

@router.get("/transactions", response_class=HTMLResponse)
def transactions(request: Request, month: str = "", category: str = "", q: str = "", source: str = "", page: int = 1):
    per_page = 200
    page = max(1, page)
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
        everything = s.exec(stmt.order_by(Txn.date.desc())).all()
    count = len(everything)
    rows = everything[(page - 1) * per_page: page * per_page]
    spend = [t for t, _ in everything if t.category not in NON_SPEND and t.amount > 0]
    total = round(sum(t.amount for t in spend), 2)
    summary = None
    if q and spend:  # a merchant drill-down: how often, how much, per month
        first, last = min(t.date for t in spend), max(t.date for t in spend)
        n_months = max(1, (last.year - first.year) * 12 + last.month - first.month + 1)
        summary = {"count": len(spend), "total": total, "avg": round(total / len(spend), 2),
                   "per_month": round(total / n_months, 2), "since": first.isoformat()}
    return render(request, "transactions.html", rows=rows, categories=CATEGORIES, months=insights.months(),
                  f={"month": month, "category": category, "q": q, "source": source}, total=total, count=count,
                  page=page, pages=max(1, -(-count // per_page)), summary=summary)


@router.post("/txn/{txn_id}/category", response_class=HTMLResponse)
def txn_category(txn_id: str, category: str = Form(...)):
    n = correct(txn_id, category)
    return HTMLResponse(f'<span class="badge plain t-ok">Saved · {n} similar updated · rule learned</span>')


# ---------------- rules ----------------

@router.get("/rules", response_class=HTMLResponse)
def rules(request: Request):
    with session() as s:
        rs = s.exec(select(Rule).order_by(Rule.id.desc())).all()
        by_source = dict(s.exec(select(Txn.category_source, func.count()).group_by(Txn.category_source)).all())
    return render(request, "rules.html", rules=rs, by_source=by_source)


@router.post("/rules/{rid}/delete")
def rule_delete(rid: int):
    with session() as s:
        if r := s.get(Rule, rid):
            s.delete(r)
            s.commit()
    return back("/rules")


# ---------------- subscriptions ----------------

@router.get("/subscriptions", response_class=HTMLResponse)
def subscriptions_page(request: Request, saved: str = ""):
    return render(request, "subscriptions.html", subs=subscriptions.overview(), saved=saved)


@router.post("/subscriptions/status")
def subscription_status(key: str = Form(...), status: str = Form(...)):
    if status in subscriptions.STATUSES:
        subscriptions.decide(key, status)
    return back("/subscriptions?saved=1")
