"""The Plan: can you hit your FIRE target by your target age, and what has to change?

Deterministic maths on your data and assumptions. It sizes the gap, says how hard it is, and generates
alternatives (later age, smaller target, more income, more return). It never recommends investments.
"""
from __future__ import annotations

import math
from datetime import date

from sqlmodel import select

from app.db import session
from app.models import Account, CashAccount, Goal, Plan401k, RetirementAccount, RetirementPlan

# How far each category can realistically be cut when you're serious about it ("harsh" mode).
HARSH_CUT = {"Dining": 0.6, "Coffee": 0.8, "Shopping": 0.6, "Entertainment": 0.7, "Subscriptions": 0.7,
             "Travel": 0.7, "Personal Care": 0.4, "Transport": 0.3, "Groceries": 0.25, "Other": 0.4,
             "Health & Fitness": 0.3, "Utilities": 0.1, "Rent & Mortgage": 0.0, "Fees & Interest": 1.0}
REALISM = [(0.35, "Realistic", "Doable with discipline."),
           (0.50, "Aggressive", "Possible, but it will shape your life: high savings rate, few splurges."),
           (0.65, "Extreme", "Only works with very low spending for years. Most people can't sustain this.")]


def _fv(pv: float, monthly: float, r_annual: float, months: int) -> float:
    r = r_annual / 12
    if r == 0:
        return pv + monthly * months
    g = (1 + r) ** months
    return pv * g + monthly * (g - 1) / r


def _pmt(target: float, pv: float, r_annual: float, months: int) -> float:
    """Monthly amount needed to grow pv into target in `months`."""
    if months <= 0:
        return float("inf") if pv < target else 0.0
    r = r_annual / 12
    if r == 0:
        return max(0.0, (target - pv) / months)
    g = (1 + r) ** months
    return max(0.0, (target - pv * g) * r / (g - 1))


def _solve_rate(target: float, pv: float, monthly: float, months: int) -> float | None:
    """Annual return needed to reach target with the given monthly investing (bisection)."""
    if months <= 0:
        return None
    if _fv(pv, monthly, 0.0, months) >= target:
        return 0.0
    lo, hi = 0.0, 0.5
    if _fv(pv, monthly, hi, months) < target:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if _fv(pv, monthly, mid, months) >= target:
            hi = mid
        else:
            lo = mid
    return hi


def realism(rate: float) -> tuple[str, str]:
    for limit, label, why in REALISM:
        if rate <= limit:
            return label, why
    return "Not possible on current income", "Even cutting everything you realistically can doesn't get there."


# ---------------- net worth ----------------

def net_worth() -> dict:
    with session() as s:
        banks = s.exec(select(Account).where(Account.source == "plaid")).all()
        cash = s.exec(select(CashAccount)).all()
        invest = s.exec(select(RetirementAccount)).all()
    assets, debts = [], []
    for a in banks:
        if a.balance_current is None:
            continue
        row = {"name": a.name + (f" ••{a.mask}" if a.mask else ""), "group": "Bank", "balance": round(a.balance_current, 2),
               "updated": a.balance_updated.date().isoformat() if a.balance_updated else None}
        (debts if a.type in ("credit", "loan") else assets).append(row)
    for c in cash:
        assets.append({"name": c.name, "group": "Cash & savings", "balance": c.balance,
                       "updated": c.as_of.isoformat() if c.as_of else None})
    for r in invest:
        assets.append({"name": r.name, "group": "Investments", "balance": r.balance, "kind": r.kind,
                       "updated": r.as_of.isoformat() if r.as_of else None})
    total_assets = round(sum(a["balance"] for a in assets), 2)
    total_debts = round(sum(d["balance"] for d in debts), 2)
    invested = round(sum(a["balance"] for a in assets if a["group"] == "Investments"), 2)
    liquid = round(sum(a["balance"] for a in assets if a["group"] in ("Bank", "Cash & savings")), 2)
    return {"assets": assets, "debts": debts, "total_assets": total_assets, "total_debts": total_debts,
            "net_worth": round(total_assets - total_debts, 2), "invested": invested, "liquid": liquid}


# ---------------- subscriptions ----------------

STREAMING = ("netflix", "hulu", "disney", "max", "hbo", "peacock", "paramount", "apple tv", "youtube", "spotify",
             "pandora", "tidal", "crunchyroll", "starz", "sling", "fubo")


def subscriptions(today: date | None = None) -> dict:
    """Every recurring charge you should know about: cost per year, new ones, price increases, overlaps."""
    from app.insights import recurring

    today = today or date.today()
    subs = [r for r in recurring() if r["active"] and r["category"] not in ("Rent & Mortgage", "Utilities", "Fees & Interest")]
    items = []
    for r in subs:
        flags = []
        if r.get("first_charged") and (today - date.fromisoformat(r["first_charged"])).days <= 90:
            flags.append("new in the last 3 months")
        if r["previous_amount"] is not None and r["amount"] > r["previous_amount"]:
            flags.append(f"price went up from ${r['previous_amount']:.2f}")
        if any(k in r["merchant"].lower() for k in STREAMING):
            flags.append("streaming")
        items.append({**r, "flags": flags})
    streaming = [i for i in items if "streaming" in i["flags"]]
    notes = []
    if len(streaming) >= 2:
        notes.append(f"You pay for {len(streaming)} streaming/music services "
                     f"(${sum(i['annual_cost'] for i in streaming):,.0f}/yr). Keep one, rotate the rest month to month.")
    return {"items": items, "monthly": round(sum(i["amount"] for i in items), 2),
            "annual": round(sum(i["annual_cost"] for i in items), 2), "notes": notes}


# ---------------- the plan ----------------

def _harsh_cuts(today: date) -> list[dict]:
    from app.planning import _full_months, monthly_by_category

    months = _full_months(today)
    spend, _ = monthly_by_category(today)
    out = []
    for cat, by_month in spend.items():
        avg3 = sum(by_month.get(m, 0) for m in months[:3]) / 3
        avg12 = sum(by_month.get(m, 0) for m in months) / max(1, sum(1 for m in months if m in by_month))
        cap = HARSH_CUT.get(cat, 0.4)
        if avg3 < 10 or cap == 0:
            continue
        cut = round(avg3 * cap, 2)
        out.append({"category": cat, "avg_month": round(avg3, 2), "avg_year": round(avg3 * 12, 2),
                    "target": round(avg3 - cut, 2), "cut": cut, "trend_pct": round((avg3 / avg12 - 1) * 100) if avg12 else 0})
    return sorted(out, key=lambda r: -r["cut"])


def _blunt(c: dict, need: float) -> str:
    share = f", {c['cut'] / need * 100:.0f}% of the gap" if need > 0 else ""
    trend = f" It's up {c['trend_pct']}% vs your 12-month average." if c["trend_pct"] >= 15 else ""
    return (f"{c['category']}: ${c['avg_month']:,.0f}/mo is ${c['avg_year']:,.0f} a year.{trend} "
            f"Cut it to ${c['target']:,.0f}/mo. That frees ${c['cut']:,.0f}/mo{share}.")


def plan(today: date | None = None) -> dict | None:
    from app import retirement as R
    from app.planning import cash_flow

    today = today or date.today()
    with session() as s:
        rp = s.get(RetirementPlan, 1) or RetirementPlan()
        k = s.get(Plan401k, 1)
        goals = s.exec(select(Goal)).all()
    birth = rp.birth_year or (k.birth_year if k else None)
    nw = net_worth()
    cf = cash_flow(today)
    rs = R.retirement_status(today, rp)
    if not birth:
        return {"needs": "birth_year", "net_worth": nw, "subscriptions": subscriptions(today), "cash_flow": cf}

    age = today.year - birth + (today.month - 6) / 12  # mid-year approximation
    target_age = rp.retirement_age
    months = max(0, int(round((target_age - age) * 12)))
    r = (rp.expected_return_pct or 0) / 100
    invested = nw["invested"]
    investing_now = rs.monthly_contributions if rs else 0.0  # 401(k) + match + Roth + other, per month
    income, spending, surplus = cf["avg_income"], cf["avg_spending"], cf["avg_surplus"]
    cuts = _harsh_cuts(today)
    max_cut = round(sum(c["cut"] for c in cuts), 2)
    feasible_monthly = investing_now + max(0.0, surplus) + max_cut  # everything you could put in if you went all in
    gross_base = income + investing_now  # take-home + what's already invested from payroll

    lo = rp.fire_target_low or (rs.fi_number if rs else 0)
    hi = rp.fire_target_high or lo
    targets = []
    for label, T in (("low", lo), ("high", hi)) if hi != lo else (("target", lo),):
        need = _pmt(T, invested, r, months)
        extra = max(0.0, need - investing_now)
        from_surplus = min(extra, max(0.0, surplus))
        cut_needed = max(0.0, extra - max(0.0, surplus))
        rate = need / gross_base if gross_base > 0 else 1.0
        rating, why = realism(rate) if cut_needed <= max_cut else realism(9.9)
        # Auto-generated alternatives
        alt_age = None
        for m in range(months, 12 * 60):
            if _fv(invested, feasible_monthly, r, m) >= T:
                alt_age = round(age + m / 12, 1)
                break
        reachable_by_age = round(_fv(invested, feasible_monthly, r, months), 0)
        extra_income = max(0.0, cut_needed - max_cut)
        rate_needed = _solve_rate(T, invested, feasible_monthly, months)
        on_track_now = round(_fv(invested, investing_now, r, months), 0)
        milestones = []
        for yrs in range(1, math.ceil(target_age - age) + 1):  # through the target age itself
            m = min(months, yrs * 12)
            milestones.append({"age": min(target_age, int(age) + yrs), "need": round(_fv(invested, need, r, m), 0),
                               "current_pace": round(_fv(invested, investing_now, r, m), 0),
                               "all_in": round(_fv(invested, feasible_monthly, r, m), 0)})
        targets.append({
            "label": label, "target": T, "required_monthly": round(need, 2), "investing_now": round(investing_now, 2),
            "extra_monthly": round(extra, 2), "from_surplus": round(from_surplus, 2), "spending_cut": round(cut_needed, 2),
            "spending_cut_pct": round(cut_needed / spending * 100, 1) if spending else 0,
            "savings_rate_needed": round(rate * 100, 1), "rating": rating, "why": why,
            "projected_current_pace": on_track_now, "feasible": cut_needed <= max_cut,
            "alternatives": {
                "retire_at_age": alt_age, "reachable_by_target_age": reachable_by_age,
                "extra_income_monthly": round(extra_income, 2),
                "extra_income_yearly_pretax": round(extra_income * 12 / 0.7, 0),
                "return_needed_pct": round(rate_needed * 100, 1) if rate_needed is not None else None,
            },
            "milestones": milestones[:45],
        })

    primary = targets[0]
    need_cut = primary["spending_cut"]
    harsh_list, covered = [], 0.0
    for c in cuts:
        if need_cut and covered >= need_cut:
            break
        harsh_list.append({**c, "message": _blunt(c, need_cut)})
        covered += c["cut"]

    # Which goals to change: savings goals compete with FIRE money.
    from app.planning import goal_status
    goal_recs = []
    for g in goals:
        if g.kind != "savings":
            continue
        st = goal_status(g, today)
        if primary["spending_cut"] > 0 and st["required_monthly"] > 0:
            later = g.target_date.replace(year=g.target_date.year + 1)
            st_later = goal_status(g.model_copy(update={"target_date": later}), today)
            goal_recs.append({
                "goal": g.name, "required_monthly": st["required_monthly"],
                "advice": (f"Push '{g.name}' back a year to {later.isoformat()}: drops from "
                           f"${st['required_monthly']:,.0f} to ${st_later['required_monthly']:,.0f}/mo, "
                           f"freeing ${st['required_monthly'] - st_later['required_monthly']:,.0f}/mo for investing.")})

    recs = []
    a = primary["alternatives"]
    if primary["feasible"]:
        recs.append(f"Invest ${primary['extra_monthly']:,.0f}/mo more"
                    + (f" and cut spending by ${primary['spending_cut']:,.0f}/mo ({primary['spending_cut_pct']}%)" if primary["spending_cut"] else "")
                    + f" to reach ${primary['target']:,.0f} by {target_age}.")
    else:
        recs.append(f"${primary['target']:,.0f} by {target_age} doesn't fit your current income, even with every realistic cut.")
    if a["retire_at_age"] and a["retire_at_age"] > target_age:
        recs.append(f"Going all in (investing ${feasible_monthly:,.0f}/mo), you reach ${primary['target']:,.0f} at about age {a['retire_at_age']}.")
    if a["reachable_by_target_age"] < primary["target"]:
        recs.append(f"By {target_age}, going all in gets you about ${a['reachable_by_target_age']:,.0f}. Make that your first milestone.")
    if a["extra_income_monthly"] > 0:
        recs.append(f"Or raise income: about ${a['extra_income_yearly_pretax']:,.0f}/yr more before tax closes the remaining gap.")
    if a["return_needed_pct"] is not None and a["return_needed_pct"] > (rp.expected_return_pct or 0):
        recs.append(f"Hitting it on time would need about {a['return_needed_pct']}%/yr after inflation; "
                    f"you assume {rp.expected_return_pct}%. Don't plan on outperforming.")
    recs += [gr["advice"] for gr in goal_recs]

    return {
        "age": round(age, 1), "target_age": target_age, "months": months, "return_pct": rp.expected_return_pct,
        "invested": invested, "investing_now": round(investing_now, 2), "income": income, "spending": spending,
        "surplus": surplus, "max_realistic_cut": max_cut, "feasible_monthly": round(feasible_monthly, 2),
        "targets": targets, "harsh_cuts": harsh_list, "all_cuts": cuts, "recommendations": recs, "goal_changes": goal_recs,
        "net_worth": nw, "subscriptions": subscriptions(today), "cash_flow": cf, "harsh": rp.harsh,
        "emergency_fund_target": round(spending * 6, 0),
    }
