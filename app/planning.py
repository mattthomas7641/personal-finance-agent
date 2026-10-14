"""Goals and planning maths. Deterministic: the agent explains these numbers, it doesn't invent them.

Everything here is arithmetic on your data and the assumptions you enter. It is not tax, legal or
investment advice, and it never suggests what to invest in.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from sqlmodel import select

from app.config import CATEGORIES, NON_SPEND
from app.db import session
from app.models import Budget, Goal, Plan401k, Txn

# IRS limits on employee deferrals (pre-tax + Roth combined). Source: irs.gov annual COLA notice.
# Add next year's row when the IRS publishes it (usually Oct-Nov).
LIMITS = {
    2025: {"deferral": 23500, "catch_up_50": 7500, "catch_up_60_63": 11250},
    2026: {"deferral": 24500, "catch_up_50": 8000, "catch_up_60_63": 11250},
}
PERIODS = {"weekly": 52, "biweekly": 26, "semimonthly": 24, "monthly": 12}
DISCLAIMER = "Estimates from your data and the numbers you entered. Not tax or investment advice."


def limit_for(year: int, birth_year: int | None) -> tuple[float, str, bool]:
    """(limit, explanation, is_official). Falls back to the latest known year if not yet published."""
    known = max(k for k in LIMITS if k <= year) if any(k <= year for k in LIMITS) else min(LIMITS)
    lim = LIMITS[known]
    age = year - birth_year if birth_year else None
    total, why = lim["deferral"], f"${lim['deferral']:,} employee limit"
    if age is not None and 60 <= age <= 63:
        total += lim["catch_up_60_63"]
        why += f" + ${lim['catch_up_60_63']:,} age 60-63 catch-up"
    elif age is not None and age >= 50:
        total += lim["catch_up_50"]
        why += f" + ${lim['catch_up_50']:,} age 50+ catch-up"
    return total, why + (f" ({known})" if known != year else ""), known == year


# ---------------- paychecks ----------------

@dataclass
class PaySchedule:
    frequency: str
    per_year: int
    last_pay_date: date | None
    avg_net_pay: float
    detected: bool


def detect_paychecks(today: date | None = None) -> PaySchedule:
    """Find recurring payroll deposits and infer the schedule from the gaps between them."""
    today = today or date.today()
    with session() as s:
        pays = s.exec(select(Txn).where(Txn.category == "Income", Txn.amount < -200).order_by(Txn.date)).all()
    pays = [p for p in pays if p.date <= today and p.date >= today - timedelta(days=200)]
    if len(pays) < 3:
        return PaySchedule("biweekly", 26, None, 0.0, False)
    gaps = [(b.date - a.date).days for a, b in zip(pays, pays[1:]) if (b.date - a.date).days > 3]
    g = statistics.median(gaps) if gaps else 14
    freq = "weekly" if g <= 8 else "biweekly" if g <= 15 else "semimonthly" if g <= 17 else "monthly"
    recent = pays[-6:]
    return PaySchedule(freq, PERIODS[freq], pays[-1].date, round(statistics.mean(-p.amount for p in recent), 2), True)


def remaining_paychecks(freq: str, last_pay: date | None, today: date, year: int | None = None) -> int:
    """Paychecks dated after `today` and within calendar `year` (default: today's year)."""
    year = year or today.year
    end = date(year, 12, 31)
    if freq in ("semimonthly", "monthly"):
        days = (15, 28) if freq == "semimonthly" else (28,)
        return sum(1 for m in range(1, 13) for d in days if today < date(year, m, d) <= end)
    step = 7 if freq == "weekly" else 14
    d = last_pay or today
    while d > today:  # last deposit may be after `today` (e.g. counting from an older pay stub)
        d -= timedelta(days=step)
    while d <= today:
        d += timedelta(days=step)
    n = 0
    while d <= end:
        n += 1
        d += timedelta(days=step)
    return n


# ---------------- 401(k) ----------------

@dataclass
class K401Status:
    year: int
    limit: float
    limit_note: str
    limit_official: bool
    ytd: float
    projected_year_end: float
    on_track_to_max: bool
    shortfall: float
    remaining_paychecks: int
    gross_per_paycheck: float
    current_pct: float
    required_pct: float | None  # % for the rest of the year to land exactly on the limit
    extra_per_paycheck: float
    take_home_change_per_month: float  # approx, after tax savings; negative = less take-home
    match_threshold_pct: float
    getting_full_match: bool
    employer_match_year: float
    max_hit_early_paycheck: int | None  # paycheck # (from now) at which you'd hit the limit
    losing_match_from_early_max: float
    next_year_pct_to_max: float = 0  # % of every paycheck from January that lands exactly on next year's limit
    next_year_limit_note: str = ""
    realistic_this_year: bool = True
    recommendation: str = ""
    notes: list[str] = field(default_factory=list)
    pay_schedule: dict = field(default_factory=dict)


def k401_status(today: date | None = None, plan: Plan401k | None = None) -> K401Status | None:
    today = today or date.today()
    if plan is None:
        with session() as s:
            plan = s.get(Plan401k, 1)
    if not plan or plan.annual_salary <= 0:
        return None
    sched = detect_paychecks(today)
    freq = sched.frequency if plan.pay_frequency == "auto" else plan.pay_frequency
    per_year = PERIODS[freq]
    gross = plan.annual_salary / per_year
    limit, note, official = limit_for(today.year, plan.birth_year)
    ytd = plan.ytd_contributed
    # Paychecks after the stub date aren't in ytd yet: count them from the stub date, not today.
    as_of = plan.ytd_as_of or today
    if as_of.year != today.year:
        ytd, as_of = 0.0, date(today.year, 1, 1) - timedelta(days=1)
    left = remaining_paychecks(freq, sched.last_pay_date if sched.detected else None, as_of, today.year)
    per_check = gross * plan.contribution_pct / 100
    projected = min(limit, ytd + per_check * left)
    shortfall = max(0.0, limit - (ytd + per_check * left))
    required = None
    if left > 0:
        required = round(min(100.0, max(0.0, (limit - ytd) / (gross * left) * 100)), 2)
    extra = max(0.0, (required or 0) - plan.contribution_pct) / 100 * gross
    take_home = -extra * (1 - plan.marginal_tax_rate_pct / 100) * per_year / 12

    # Employer match: on contributions up to match_up_to_pct of pay, per paycheck.
    def match_for(pct: float, checks: int) -> float:
        return gross * min(pct, plan.match_up_to_pct) / 100 * plan.match_rate_pct / 100 * checks

    match_year = match_for(plan.contribution_pct, per_year)
    hit_at, lost = None, 0.0
    if per_check > 0 and not plan.has_true_up and ytd + per_check * left > limit:
        hit_at = int((limit - ytd) // per_check) + 1
        lost = round(match_for(plan.contribution_pct, max(0, left - hit_at)), 2)

    ny_limit, ny_note, ny_official = limit_for(today.year + 1, plan.birth_year)
    next_pct = round(min(100.0, ny_limit / plan.annual_salary * 100), 2)
    realistic = required is not None and required <= 50
    if required is None:
        rec = f"No paychecks left in {today.year}. Set {next_pct:g}% from your first {today.year + 1} paycheck to max out next year."
    elif shortfall < 1:
        rec = f"On track: {plan.contribution_pct:g}% gets you to the ${limit:,.0f} limit by year end."
    elif realistic:
        rec = (f"Raise your contribution from {plan.contribution_pct:g}% to {required:g}% for the last {left} paychecks "
               f"(+${extra:,.0f} per paycheck, about ${-take_home:,.0f}/month less take-home after tax savings).")
    else:
        rec = (f"Maxing out {today.year} would take {required:g}% of each remaining paycheck. Instead, set "
               f"{next_pct:g}% starting with your first {today.year + 1} paycheck to max out next year"
               + ("" if ny_official else " (based on the current limit; the IRS publishes next year's in the fall)") + ".")

    notes = []
    if plan.match_up_to_pct and plan.contribution_pct < plan.match_up_to_pct:
        missed = match_for(plan.match_up_to_pct, per_year) - match_year
        notes.append(f"You're contributing {plan.contribution_pct:g}% but your employer matches up to "
                     f"{plan.match_up_to_pct:g}%: about ${missed:,.0f}/year of match left on the table.")
    if lost > 0:
        notes.append(f"At {plan.contribution_pct:g}% you'd hit the limit around paycheck {hit_at} of {left} remaining. "
                     f"Without a year-end true-up, the match stops then: about ${lost:,.0f} lost. "
                     f"Spreading contributions at {required:g}% avoids that.")
    if not official:
        notes.append(f"The IRS hasn't published {today.year} limits in this app yet; using the latest known limit.")
    if plan.ytd_as_of and (today - plan.ytd_as_of).days > 45:
        notes.append("Your year-to-date amount is over 6 weeks old; update it from your latest pay stub.")

    return K401Status(
        year=today.year, limit=limit, limit_note=note, limit_official=official, ytd=round(ytd, 2),
        projected_year_end=round(projected, 2), on_track_to_max=shortfall < 1, shortfall=round(shortfall, 2),
        remaining_paychecks=left, gross_per_paycheck=round(gross, 2), current_pct=plan.contribution_pct,
        required_pct=required, extra_per_paycheck=round(extra, 2), take_home_change_per_month=round(take_home, 2),
        match_threshold_pct=plan.match_up_to_pct, getting_full_match=plan.contribution_pct >= plan.match_up_to_pct,
        employer_match_year=round(match_year, 2), max_hit_early_paycheck=hit_at, losing_match_from_early_max=lost,
        next_year_pct_to_max=next_pct, next_year_limit_note=ny_note, realistic_this_year=realistic or shortfall < 1,
        recommendation=rec, notes=notes, pay_schedule={"frequency": freq, "per_year": per_year, "detected_from_deposits": sched.detected,
                                   "last_pay_date": sched.last_pay_date.isoformat() if sched.last_pay_date else None,
                                   "avg_net_pay": sched.avg_net_pay},
    )


# ---------------- cash flow & cut plan ----------------

ESSENTIAL = {"Rent & Mortgage", "Utilities", "Health & Fitness", "Fees & Interest"}
# How much of a category's typical spend a realistic plan would trim, at most.
MAX_CUT = {"Dining": 0.35, "Coffee": 0.5, "Shopping": 0.35, "Entertainment": 0.4, "Subscriptions": 0.5,
           "Travel": 0.3, "Personal Care": 0.2, "Transport": 0.15, "Groceries": 0.1, "Other": 0.2}


def _full_months(today: date) -> list[str]:
    first_this = today.replace(day=1)
    out, d = [], first_this
    for _ in range(12):
        d = (d - timedelta(days=1)).replace(day=1)
        out.append(d.strftime("%Y-%m"))
    return out  # most recent first


def left_out(txns: list[Txn]) -> dict[str, str]:
    """Charges planning averages ignore, with the reason: you tagged them one-off / bad investment, or they
    belong to a subscription you've cancelled. Monthly totals still include them; forecasts don't."""
    from app.subscriptions import belongs_to_cancelled

    out = {t.id: t.tag for t in txns if t.tag and t.amount > 0}
    for tid in belongs_to_cancelled(txns):
        out.setdefault(tid, "cancelled")
    return out


def monthly_by_category(today: date | None = None) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    """({category: {month: spend}}, {month: income}) over the last 12 complete months, leaving out
    one-offs, bad investments and charges from cancelled subscriptions (see left_out)."""
    today = today or date.today()
    months = set(_full_months(today))
    spend: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    income: dict[str, float] = defaultdict(float)
    with session() as s:
        txns = s.exec(select(Txn)).all()
    skip = left_out([t for t in txns if t.date.strftime("%Y-%m") in months])
    for t in txns:
        m = t.date.strftime("%Y-%m")
        if m not in months or t.id in skip:
            continue
        if t.category == "Income":
            income[m] += -t.amount
        elif t.category not in NON_SPEND and t.category:
            spend[t.category][m] += t.amount
    return spend, income


def cash_flow(today: date | None = None) -> dict:
    """Average monthly income, spending and surplus over the last 3 complete months."""
    today = today or date.today()
    recent = _full_months(today)[:3]
    spend, income = monthly_by_category(today)
    from app.subscriptions import off_ledger_monthly

    inc = sum(income.get(m, 0) for m in recent) / 3
    off = off_ledger_monthly()  # subscriptions on a card that isn't connected
    sp = sum(v.get(m, 0) for v in spend.values() for m in recent) / 3 + off
    with session() as s:
        window = [t for t in s.exec(select(Txn).where(Txn.date >= date.fromisoformat(min(recent) + "-01"),
                                                     Txn.date < today.replace(day=1))).all()]
    skip = left_out(window)
    totals: dict[str, float] = defaultdict(float)
    for t in window:
        if t.id in skip:
            totals[skip[t.id]] += t.amount / 3
    return {"months": sorted(recent), "avg_income": round(inc, 2), "avg_spending": round(sp, 2), "avg_surplus": round(inc - sp, 2),
            "left_out": {k: round(v, 2) for k, v in totals.items()}, "off_ledger": off}


def where_to_cut(today: date | None = None) -> list[dict]:
    """Discretionary categories ranked by how much room there is to trim, with the reason."""
    today = today or date.today()
    months = _full_months(today)
    recent, year = months[:3], months
    spend, _ = monthly_by_category(today)
    with session() as s:
        budgets = {b.category: b.monthly_limit for b in s.exec(select(Budget)).all()}
    out = []
    for cat, by_month in spend.items():
        if cat in ESSENTIAL:
            continue
        avg3 = sum(by_month.get(m, 0) for m in recent) / 3
        avg12 = sum(by_month.get(m, 0) for m in year) / max(1, sum(1 for m in year if m in by_month))
        if avg3 < 15:
            continue
        reasons = []
        if avg12 and avg3 > avg12 * 1.15:
            reasons.append(f"up {round((avg3 / avg12 - 1) * 100)}% vs your 12-month average")
        if cat in budgets:
            over = sum(1 for m in recent if by_month.get(m, 0) > budgets[cat])
            if over:
                reasons.append(f"over budget {over} of the last 3 months")
        cut = round(avg3 * MAX_CUT.get(cat, 0.15), 2)
        out.append({"category": cat, "avg_last_3_months": round(avg3, 2), "avg_last_12_months": round(avg12, 2),
                    "realistic_cut": cut, "reasons": reasons or ["discretionary"],
                    "score": cut * (1.5 if len(reasons) > 1 or (reasons and "discretionary" not in reasons[0]) else 1)})
    return sorted(out, key=lambda r: -r["score"])


def cut_plan(target_monthly: float, today: date | None = None) -> dict:
    """Find `target_monthly` of extra room: first from current surplus, then from category trims.
    Returns proposed budgets per category that, if kept, free up the target."""
    today = today or date.today()
    cf = cash_flow(today)
    from_surplus = max(0.0, min(target_monthly, cf["avg_surplus"] * 0.5))  # keep half the surplus as a buffer
    need = round(target_monthly - from_surplus, 2)
    plan, found = [], 0.0
    for c in where_to_cut(today):
        if found >= need:
            break
        take = round(min(c["realistic_cut"], need - found), 2)
        if take < 5:
            continue
        found += take
        plan.append({**c, "cut": take, "new_budget": round(c["avg_last_3_months"] - take, 0)})
    # Subscriptions are the easiest cuts: list them so the user can pick ones they don't use.
    from app.insights import recurring
    subs = [r for r in recurring() if r["active"] and r["category"] == "Subscriptions"]
    return {"target_monthly": round(target_monthly, 2), "from_surplus": round(from_surplus, 2),
            "from_cuts": round(found, 2), "covered": found + from_surplus >= target_monthly - 1,
            "short_by": round(max(0.0, target_monthly - from_surplus - found), 2), "cash_flow": cf,
            "cuts": plan, "subscriptions_to_review": subs[:8], "disclaimer": DISCLAIMER}


def apply_budgets(items: list[tuple[str, float]], source: str = "plan") -> None:
    with session() as s:
        for cat, lim in items:
            s.merge(Budget(category=cat, monthly_limit=round(lim, 0), source=source))
        s.commit()


def budgets() -> dict[str, float]:
    with session() as s:
        return {b.category: b.monthly_limit for b in s.exec(select(Budget)).all()}


# ---------------- savings & retirement goals ----------------

def months_between(a: date, b: date) -> int:
    return max(0, (b.year - a.year) * 12 + (b.month - a.month))


def goal_status(g: Goal, today: date | None = None, k401: K401Status | None = None) -> dict:
    today = today or date.today()
    n = months_between(today, g.target_date)
    linked = None
    if g.account_id:  # follow a manual account: its balance is "saved so far", detected transfers the monthly amount
        from app.cash import monthly_deposits
        from app.models import CashAccount
        with session() as s:
            acct = s.get(CashAccount, g.account_id)
        if acct:
            dep, _ = monthly_deposits(acct.detect_pattern, today)
            linked = {"name": acct.name, "balance": acct.balance, "detected_monthly": dep,
                      "apy_pct": acct.apy_pct, "pattern": acct.detect_pattern}
            g = g.model_copy(update={"current_amount": acct.balance,
                                     "monthly_contribution": g.monthly_contribution or max(0.0, dep)})
    if g.kind == "retirement":
        r = (g.expected_return_pct or 0) / 100 / 12
        monthly = g.monthly_contribution
        if k401:  # count your 401(k) deferrals + employer match toward the goal
            monthly += (k401.current_pct / 100 * k401.gross_per_paycheck * k401.pay_schedule["per_year"] + k401.employer_match_year) / 12
        grow = (1 + r) ** n
        projected = g.current_amount * grow + (monthly * ((grow - 1) / r) if r else monthly * n)
        needed = g.target_amount - g.current_amount * grow
        required_monthly = max(0.0, needed * r / (grow - 1) if r and grow > 1 else needed / max(n, 1))
    else:
        monthly = g.monthly_contribution
        r = (linked["apy_pct"] / 100 / 12) if linked and linked["apy_pct"] else 0.0  # a HYSA earns interest meanwhile
        grow = (1 + r) ** n
        projected = g.current_amount * grow + (monthly * (grow - 1) / r if r else monthly * n)
        needed = g.target_amount - g.current_amount * grow
        required_monthly = max(0.0, needed * r / (grow - 1) if r and grow > 1 else needed / max(n, 1))
    gap = round(required_monthly - monthly, 2)
    return {"id": g.id, "name": g.name, "kind": g.kind, "target": g.target_amount, "target_date": g.target_date.isoformat(),
            "months_left": n, "current": g.current_amount, "monthly_now": round(monthly, 2),
            "required_monthly": round(required_monthly, 2), "projected": round(projected, 2),
            "on_track": projected >= g.target_amount - 1, "monthly_gap": max(0.0, gap),
            "progress_pct": round(min(100, g.current_amount / g.target_amount * 100), 1) if g.target_amount else 0,
            "assumed_return_pct": g.expected_return_pct if g.kind == "retirement" else None, "linked_account": linked,
            "account_id": g.account_id}


def overview(today: date | None = None) -> dict:
    """Everything the Goals page and the agent need, in one call."""
    today = today or date.today()
    k = k401_status(today)
    with session() as s:
        goals = s.exec(select(Goal).order_by(Goal.target_date)).all()
    statuses = [goal_status(g, today, k) for g in goals]
    k401_need, extra_401k_monthly, extra_401k_monthly_total = 0.0, 0.0, 0.0
    if k and not k.on_track_to_max:
        plan = _plan()
        target_pct = k.required_pct if k.realistic_this_year else k.next_year_pct_to_max
        extra_401k_monthly = max(0.0, (target_pct or 0) - k.current_pct) / 100 * plan.annual_salary / 12
        k401_need = extra_401k_monthly * (1 - plan.marginal_tax_rate_pct / 100)  # take-home drop after tax savings
        extra_401k_monthly_total = extra_401k_monthly
    # A bigger 401(k) contribution also funds retirement goals: don't count that money twice.
    for x in statuses:
        if x["kind"] == "retirement" and extra_401k_monthly:
            covered = min(x["monthly_gap"], extra_401k_monthly)
            x["monthly_gap_after_401k_change"] = round(x["monthly_gap"] - covered, 2)
            extra_401k_monthly -= covered
    need = sum(x.get("monthly_gap_after_401k_change", x["monthly_gap"]) for x in statuses) + k401_need

    # Roth IRA and the retirement shortfall (Retirement page), if the user opted them into the plan.
    from app import retirement as R
    rplan = R._plan()
    roth = R.roth_status(today, rplan)
    roth_need = roth.monthly_to_max_by_dec if (rplan.roth_target and not roth.maxed and roth.allowed > 0) else 0.0
    ret = R.retirement_status(today, rplan)
    ret_need = 0.0
    if ret and rplan.include_gap_in_plan and not ret.on_track:
        ret_need = max(0.0, ret.monthly_gap - extra_401k_monthly_total)  # a planned 401(k) raise already closes part of it
    need += roth_need + ret_need
    return {"today": today.isoformat(), "k401": asdict(k) if k else None, "goals": statuses,
            "cash_flow": cash_flow(today), "where_to_cut": where_to_cut(today)[:6], "cash_accounts": _cash_summary(today),
            "monthly_needed_for_goals": round(need, 2), "monthly_needed_for_401k": round(k401_need, 2),
            "monthly_needed_for_roth": round(roth_need, 2), "monthly_needed_for_retirement": round(ret_need, 2),
            "roth": asdict(roth), "retirement": asdict(ret) if ret else None,
            "plan": cut_plan(need, today) if need > 0 else None, "budgets": budgets(), "disclaimer": DISCLAIMER}


def _cash_summary(today: date) -> dict:
    from app.cash import summary
    return summary(today)


def _plan() -> Plan401k | None:
    with session() as s:
        return s.get(Plan401k, 1)


def budget_table(today: date | None = None) -> list[dict]:
    """Every spending category with the context you need to set a limit: 3- and 12-month averages,
    this month so far, the pace for the full month, and the current limit."""
    import calendar

    today = today or date.today()
    spend, _ = monthly_by_category(today)
    full = _full_months(today)
    this_month = today.strftime("%Y-%m")
    with session() as s:
        so_far: dict[str, float] = defaultdict(float)
        for t in s.exec(select(Txn).where(Txn.date >= today.replace(day=1))).all():
            if t.category and t.category not in NON_SPEND and t.amount > 0 and not t.tag and t.date.strftime("%Y-%m") == this_month:
                so_far[t.category] += t.amount
    limits = budgets()
    days_in = calendar.monthrange(today.year, today.month)[1]
    rows = []
    for c in CATEGORIES:
        if c in NON_SPEND:
            continue
        by_m = spend.get(c, {})
        avg3 = sum(by_m.get(m, 0) for m in full[:3]) / 3
        avg12 = sum(by_m.get(m, 0) for m in full) / 12
        now = so_far.get(c, 0.0)
        pace = now / today.day * days_in if today.day else now
        lim = limits.get(c)
        rows.append({"category": c, "avg3": round(avg3, 2), "avg12": round(avg12, 2), "this_month": round(now, 2),
                     "pace": round(pace, 2), "limit": lim, "over_pace": bool(lim and pace > lim)})
    return sorted(rows, key=lambda r: -max(r["avg3"], r["this_month"]))
