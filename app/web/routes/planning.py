"""Goals, budgets, the retirement-by-age plan and retirement accounts."""
from __future__ import annotations

import datetime as dt
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlmodel import select

from app import cash, fire, planning, retirement
from app.config import CATEGORIES
from app.db import session
from app.models import Budget, Goal, Plan401k, RetirementAccount, RetirementPlan
from app.web.common import _num, back, render

router = APIRouter()


# ---------------- goals & budgets ----------------


@router.get("/goals", response_class=HTMLResponse)
def goals_page(request: Request, saved: str = ""):
    with session() as s:
        plan = s.get(Plan401k, 1) or Plan401k()
        goals = s.exec(select(Goal).order_by(Goal.target_date)).all()
    ov = planning.overview()
    try:
        fp = fire.plan()
    except Exception:
        fp = None
    return render(request, "goals.html", ov=ov, plan=plan, goals=goals, register=fire.goal_register(ov, fp), fp=fp, saved=saved, categories=CATEGORIES, cash_accounts=cash.accounts(),
                  sched=planning.detect_paychecks(), today=dt.date.today())


@router.post("/goals/401k")
def save_401k(annual_salary: str = Form(...), contribution_pct: str = Form(...), ytd_contributed: str = Form("0"),
              ytd_as_of: str = Form(""), match_rate_pct: str = Form("0"), match_up_to_pct: str = Form("0"),
              has_true_up: str = Form(""), birth_year: str = Form(""), pay_frequency: str = Form("auto"),
              marginal_tax_rate_pct: str = Form("22")):
    with session() as s:
        s.merge(Plan401k(
            id=1, annual_salary=_num(annual_salary), contribution_pct=_num(contribution_pct),
            ytd_contributed=_num(ytd_contributed), ytd_as_of=dt.date.fromisoformat(ytd_as_of) if ytd_as_of else None,
            match_rate_pct=_num(match_rate_pct), match_up_to_pct=_num(match_up_to_pct), has_true_up=bool(has_true_up),
            birth_year=int(birth_year) if birth_year.strip() else None, pay_frequency=pay_frequency,
            marginal_tax_rate_pct=_num(marginal_tax_rate_pct, 22)))
        s.commit()
    return back("/goals?saved=401k#k401")


@router.post("/goals/new")
def goal_new(name: str = Form(...), kind: str = Form("savings"), target_amount: str = Form(...), target_date: str = Form(...),
             current_amount: str = Form("0"), monthly_contribution: str = Form("0"), expected_return_pct: str = Form("5"),
             account_id: str = Form("")):
    with session() as s:
        s.add(Goal(name=name.strip(), kind=kind if kind in ("savings", "retirement") else "savings",
                   account_id=int(account_id) if account_id.strip() else None,
                   target_amount=_num(target_amount), target_date=dt.date.fromisoformat(target_date),
                   current_amount=_num(current_amount), monthly_contribution=_num(monthly_contribution),
                   expected_return_pct=_num(expected_return_pct, 5)))
        s.commit()
    return back("/goals#goals")


@router.post("/goals/{gid}/update")
def goal_update(gid: int, current_amount: str = Form("0"), monthly_contribution: str = Form("0"), account_id: str = Form("")):
    with session() as s:
        g = s.get(Goal, gid)
        g.account_id = int(account_id) if account_id.strip() else None
        if not g.account_id:  # a linked goal takes "saved so far" from the account, so keep the stored value
            g.current_amount = _num(current_amount)
        g.monthly_contribution = _num(monthly_contribution)
        s.add(g)
        s.commit()
    return back("/goals#goals")


@router.post("/goals/{gid}/delete")
def goal_delete(gid: int):
    with session() as s:
        if g := s.get(Goal, gid):
            s.delete(g)
            s.commit()
    return back("/goals#goals")


@router.post("/budgets/apply_plan")
def budgets_apply_plan():
    ov = planning.overview()
    if ov["plan"]:
        planning.apply_budgets([(c["category"], c["new_budget"]) for c in ov["plan"]["cuts"]])
    return back("/goals?saved=budgets#plan")


@router.post("/budgets")
async def budgets_save(request: Request):
    form = await request.form()
    with session() as s:
        for cat in CATEGORIES:
            v = form.get(f"b_{cat}", "")
            row = s.get(Budget, cat)
            if str(v).strip() == "":
                if row:
                    s.delete(row)
            else:
                s.merge(Budget(category=cat, monthly_limit=_num(v), source="manual"))
        s.commit()
    return back("/goals?saved=budgets#budgets")


# ---------------- plan (FIRE) ----------------

@router.get("/plan", response_class=HTMLResponse)
def plan_page(request: Request, saved: str = ""):
    with session() as s:
        rp = s.get(RetirementPlan, 1) or RetirementPlan()
        k = s.get(Plan401k, 1)
    return render(request, "plan.html", p=fire.plan(), rp=rp, birth=rp.birth_year or (k.birth_year if k else None), saved=saved)


@router.post("/plan/settings")
def plan_settings(birth_year: str = Form(""), retirement_age: str = Form("40"), fire_target_low: str = Form(""),
                  fire_target_high: str = Form(""), expected_return_pct: str = Form("5"), harsh: str = Form("")):
    with session() as s:
        rp = s.get(RetirementPlan, 1) or RetirementPlan(id=1)
        if birth_year.strip():
            rp.birth_year = int(_num(birth_year))
        rp.retirement_age = int(_num(retirement_age, 40))
        rp.fire_target_low = _num(fire_target_low) if fire_target_low.strip() else None
        rp.fire_target_high = _num(fire_target_high) if fire_target_high.strip() else None
        rp.expected_return_pct = _num(expected_return_pct, 5)
        rp.harsh = bool(harsh)
        s.add(rp)
        s.commit()
    return back("/plan?saved=1")


@router.post("/plan/apply_cuts")
def plan_apply_cuts():
    p = fire.plan()
    if p and p.get("harsh_cuts"):
        planning.apply_budgets([(c["category"], c["target"]) for c in p["harsh_cuts"]])
    return back("/plan?saved=1")


# ---------------- retirement ----------------

@router.get("/retirement", response_class=HTMLResponse)
def retirement_page(request: Request, saved: str = "", imported: str = "", error: str = ""):
    with session() as s:
        plan = s.get(RetirementPlan, 1) or RetirementPlan()
        k = s.get(Plan401k, 1)
        accounts = s.exec(select(RetirementAccount).order_by(RetirementAccount.id)).all()
    return render(request, "retirement.html", ov=retirement.overview(), plan=plan, k401=k, accounts=accounts,
                  saved=saved, imported=imported, error=error, today=dt.date.today(),
                  trailing=retirement.trailing_annual_spending())


@router.post("/retirement/import-fidelity")
async def retirement_import_fidelity(file: UploadFile = File(...)):
    from app.ingest.brokerage import import_fidelity_positions
    raw = await file.read()
    if len(raw) > 5_000_000:
        return back(f"/retirement?error={quote('That file is too large for a Fidelity positions export.')}#accounts")
    text = raw.decode("utf-8", errors="replace").replace("\x00", "")
    try:
        res = import_fidelity_positions(text)
    except ValueError as e:
        return back(f"/retirement?error={quote(str(e))}#accounts")
    except Exception:  # csv.Error and anything else a malformed file can trigger
        return back(f"/retirement?error={quote('Could not read that file. Download Positions from Fidelity again and retry.')}#accounts")
    msg = (f"Imported {len(res['accounts'])} Fidelity accounts (${res['total']:,.0f} total, as of {res['as_of']})"
           + (f": added {', '.join(res['created'])}" if res["created"] else "")
           + (f"; updated {', '.join(res['updated'])}" if res["updated"] else ""))
    return back(f"/retirement?imported={quote(msg)}#accounts")


@router.post("/retirement/plan")
def retirement_plan_save(birth_year: str = Form(""), retirement_age: str = Form("60"), annual_spending: str = Form(""),
                         withdrawal_rate_pct: str = Form("4"), expected_return_pct: str = Form("5"),
                         filing_status: str = Form("single"), magi: str = Form(""), roth_contributed: str = Form("0"),
                         roth_detect_pattern: str = Form(""), roth_target: str = Form(""), include_gap_in_plan: str = Form("")):
    with session() as s:
        # Update in place: this form doesn't include the Plan page's fields (target, blunt mode), which must survive.
        rp = s.get(RetirementPlan, 1) or RetirementPlan(id=1)
        rp.birth_year = int(birth_year) if birth_year.strip() else None
        rp.retirement_age = int(_num(retirement_age, rp.retirement_age))
        rp.annual_spending = _num(annual_spending) if annual_spending.strip() else None
        rp.withdrawal_rate_pct = _num(withdrawal_rate_pct, 4) or 4
        rp.expected_return_pct = _num(expected_return_pct, 5)
        rp.filing_status = filing_status if filing_status in ("single", "mfj", "mfs") else "single"
        rp.magi = _num(magi) if magi.strip() else None
        rp.roth_contributed = _num(roth_contributed)
        rp.roth_detect_pattern = roth_detect_pattern.strip()
        rp.roth_target = bool(roth_target)
        rp.include_gap_in_plan = bool(include_gap_in_plan)
        s.add(rp)
        s.commit()
    return back("/retirement?saved=1")


@router.post("/retirement/accounts")
def retirement_account_add(name: str = Form(...), kind: str = Form("other"), balance: str = Form("0"),
                           monthly_contribution: str = Form("0")):
    with session() as s:
        s.add(RetirementAccount(name=name.strip(), kind=kind, balance=_num(balance),
                                monthly_contribution=_num(monthly_contribution), as_of=dt.date.today()))
        s.commit()
    return back("/retirement#accounts")


@router.post("/retirement/accounts/{aid}")
def retirement_account_update(aid: int, balance: str = Form(...), monthly_contribution: str = Form("0"), delete: str = Form("")):
    with session() as s:
        a = s.get(RetirementAccount, aid)
        if a and delete:
            s.delete(a)
        elif a:
            a.balance, a.monthly_contribution, a.as_of = _num(balance), _num(monthly_contribution), dt.date.today()
            s.add(a)
        s.commit()
    return back("/retirement#accounts")
