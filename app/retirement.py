"""Roth IRA, retirement and Coast FIRE maths. Deterministic arithmetic on your data and assumptions.

Not tax or investment advice: it never suggests what to invest in or which account type to choose.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date

from sqlmodel import select

from app.db import session
from app.models import Plan401k, RetirementAccount, RetirementPlan, Txn

# IRA contribution limits and Roth MAGI phase-out ranges (IRS annual COLA notices).
IRA_LIMITS = {
    2025: {"limit": 7000, "catch_up_50": 1000,
           "phaseout": {"single": (150000, 165000), "mfj": (236000, 246000), "mfs": (0, 10000)}},
    2026: {"limit": 7500, "catch_up_50": 1100,
           "phaseout": {"single": (153000, 168000), "mfj": (242000, 252000), "mfs": (0, 10000)}},
}


def _plan() -> RetirementPlan:
    with session() as s:
        return s.get(RetirementPlan, 1) or RetirementPlan()


def _k401() -> Plan401k | None:
    with session() as s:
        return s.get(Plan401k, 1)


def _accounts() -> list[RetirementAccount]:
    with session() as s:
        return s.exec(select(RetirementAccount).order_by(RetirementAccount.id)).all()


def timeline(today: date, birth: int | None, retirement_age: int) -> tuple[float | None, int]:
    """(age, months to retirement), shared by every page so they all count the same months.
    Age uses a mid-year birthday; without a birth year, assume 30 years to go."""
    if not birth:
        return None, 360
    age = today.year - birth + (today.month - 6) / 12
    return age, max(0, int(round((retirement_age - age) * 12)))


def months_until(a: date, b: date) -> int:
    return max(0, (b.year - a.year) * 12 + (b.month - a.month))


# ---------------- Roth IRA ----------------

def roth_allowed(year: int, age: int | None, magi: float | None, status: str) -> tuple[float, float, str, bool]:
    """(allowed, full_limit, explanation, official). IRS rule: reduce proportionally across the phase-out
    range, round up to the nearest $10, and allow at least $200 if not fully phased out."""
    known = max((k for k in IRA_LIMITS if k <= year), default=min(IRA_LIMITS))
    lim = IRA_LIMITS[known]
    full = lim["limit"] + (lim["catch_up_50"] if age is not None and age >= 50 else 0)
    note = f"${lim['limit']:,} limit" + (f" + ${lim['catch_up_50']:,} age 50+ catch-up" if full > lim["limit"] else "")
    if known != year:
        note += f" ({known} figures; {year} not published here yet)"
    lo, hi = lim["phaseout"].get(status, lim["phaseout"]["single"])
    if magi is None or magi <= lo:
        return float(full), float(full), note, known == year
    if magi >= hi:
        return 0.0, float(full), note + f"; income above ${hi:,} means no direct contributions", known == year
    reduced = full * (1 - (magi - lo) / (hi - lo))
    reduced = max(200.0, math.ceil(reduced / 10) * 10)
    return float(reduced), float(full), note + f"; reduced by income in the ${lo:,}-${hi:,} range", known == year


def detected_contributions(pattern: str, year: int) -> tuple[float, int]:
    """Sum of outgoing transactions in calendar `year` whose description contains `pattern`."""
    if not pattern.strip():
        return 0.0, 0
    pat = pattern.strip().lower()
    with session() as s:
        rows = s.exec(select(Txn).where(Txn.amount > 0)).all()
    hits = [t for t in rows if t.date.year == year and pat in (t.raw_name or "").lower()]
    return round(sum(t.amount for t in hits), 2), len(hits)


@dataclass
class RothStatus:
    tax_year: int
    allowed: float
    full_limit: float
    limit_note: str
    official: bool
    contributed: float
    contributions_source: str
    remaining: float
    deadline: str
    months_to_dec: int
    months_to_deadline: int
    monthly_to_max_by_dec: float
    monthly_to_max_by_deadline: float
    maxed: bool
    magi_used: float | None
    magi_estimated: bool
    notes: list[str] = field(default_factory=list)


def roth_status(today: date | None = None, plan: RetirementPlan | None = None) -> RothStatus:
    today = today or date.today()
    plan = plan or _plan()
    k = _k401()
    birth = plan.birth_year or (k.birth_year if k else None)
    age = today.year - birth if birth else None
    magi, estimated = plan.magi, False
    if magi is None and k and k.annual_salary:
        magi = k.annual_salary * (1 - k.contribution_pct / 100)  # rough: salary minus pre-tax deferrals
        estimated = True
    # Jan 1 - Apr 15: last year's Roth can still be funded; track that year until the deadline passes,
    # unless it's already maxed.
    year = today.year
    allowed, full, note, official = roth_allowed(year, age, magi, plan.filing_status)
    detected, n = detected_contributions(plan.roth_detect_pattern, year)
    contributed = detected if plan.roth_detect_pattern.strip() else plan.roth_contributed
    source = (f"{n} transfers matching '{plan.roth_detect_pattern}' this year" if plan.roth_detect_pattern.strip()
              else "entered manually")
    remaining = max(0.0, allowed - contributed)
    deadline = date(year + 1, 4, 15)
    m_dec = max(1, 12 - today.month + 1)
    m_deadline = max(1, months_until(today, deadline))
    notes = []
    if estimated:
        notes.append("Income for the Roth limit is estimated from your 401(k) salary; enter your MAGI for accuracy.")
    if allowed == 0:
        notes.append("At this income you can't contribute to a Roth IRA directly. A tax professional can explain your options.")
    if contributed > allowed:
        notes.append(f"You've put in ${contributed - allowed:,.0f} more than you're allowed; ask your brokerage how to fix excess contributions before the deadline.")
    if today.month <= 4 and today <= date(year, 4, 15):
        notes.append(f"You can still contribute for {year - 1} until April 15, {year}.")
    return RothStatus(year, allowed, full, note, official, round(contributed, 2), source, round(remaining, 2),
                      deadline.isoformat(), m_dec, m_deadline, round(remaining / m_dec, 2),
                      round(remaining / m_deadline, 2), remaining < 1 and allowed > 0, magi, estimated, notes)


# ---------------- retirement & Coast FIRE ----------------

def trailing_annual_spending(today: date | None = None) -> float:
    from app.planning import monthly_by_category
    spend, _ = monthly_by_category(today)
    months = {m for v in spend.values() for m in v}
    total = sum(sum(v.values()) for v in spend.values())
    return round(total / max(1, len(months)) * 12, 2) if months else 0.0


def fv(pv: float, monthly: float, r_annual: float, months: int) -> float:
    r = r_annual / 12
    if r == 0:
        return pv + monthly * months
    g = (1 + r) ** months
    return pv * g + monthly * (g - 1) / r


def months_to_reach(target: float, pv: float, monthly: float, r_annual: float, cap: int = 12 * 70) -> int | None:
    if pv >= target:
        return 0
    for m in range(1, cap + 1):
        if fv(pv, monthly, r_annual, m) >= target:
            return m
    return None


@dataclass
class RetirementStatus:
    age: float | None
    retirement_age: int
    years_to_retirement: float
    annual_spending: float
    spending_source: str
    withdrawal_rate_pct: float
    expected_return_pct: float
    fi_number: float          # annual spending ÷ withdrawal rate
    target: float             # what every projection aims at: your Plan target, else the FI number
    target_source: str
    invested: float
    monthly_contributions: float
    contribution_breakdown: dict
    projected_at_retirement: float
    on_track: bool
    required_monthly: float
    monthly_gap: float
    fi_age: float | None
    coast_number: float
    coast_reached: bool
    coast_progress_pct: float
    coast_age: float | None
    coast_on_time: bool       # reach Coast FIRE before the retirement age
    progress_pct: float
    accounts: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def retirement_status(today: date | None = None, plan: RetirementPlan | None = None) -> RetirementStatus | None:
    today = today or date.today()
    plan = plan or _plan()
    k = _k401()
    birth = plan.birth_year or (k.birth_year if k else None)
    accounts = _accounts()
    if not birth and not accounts:
        return None
    age, months = timeline(today, birth, plan.retirement_age)
    years = months / 12
    spending = plan.annual_spending if plan.annual_spending else trailing_annual_spending(today)
    source = "you entered" if plan.annual_spending else "your last 12 months of spending"
    r = (plan.expected_return_pct or 0) / 100
    fi = spending * 100 / plan.withdrawal_rate_pct if plan.withdrawal_rate_pct else 0.0
    # Everything below aims at ONE number: the target you set on Plan, or the FI number from spending if you haven't.
    target = plan.fire_target_low or fi
    target_source = "your target" if plan.fire_target_low else "FI number from your spending"

    breakdown = {}
    if k and k.annual_salary:
        from app.planning import k401_status
        ks = k401_status(today, k)
        breakdown["401(k) contributions"] = round(k.contribution_pct / 100 * k.annual_salary / 12, 2)
        if ks:
            breakdown["employer match"] = round(ks.employer_match_year / 12, 2)
    if plan.roth_target:
        rs = roth_status(today, plan)
        breakdown["Roth IRA (to max it)"] = round(rs.allowed / 12, 2)
    for a in accounts:
        if a.kind not in ("401k", "roth_ira") and a.monthly_contribution:
            breakdown[a.name] = round(a.monthly_contribution, 2)
    monthly = round(sum(breakdown.values()), 2)
    invested = round(sum(a.balance for a in accounts), 2)

    from app.fire import _pmt
    projected = fv(invested, monthly, r, months)
    required = _pmt(target, invested, r, months) if months else max(0.0, target - invested)
    m_fi = months_to_reach(target, invested, monthly, r)
    coast = target / ((1 + r) ** years) if r else target
    # Coast number shrinks as retirement gets closer, so solve month by month against the moving target.
    m_coast = None
    for m in range(0, 12 * 70 + 1):
        remaining_years = max(0.0, years - m / 12)
        moving = target / ((1 + r) ** remaining_years) if r else target
        if fv(invested, monthly, r, m) >= moving:
            m_coast = m
            break
    notes = []
    if not accounts:
        notes.append("Add your account balances (401(k), Roth IRA, brokerage...) to see real progress.")
    if age is None:
        notes.append("Add your birth year so projections use your real timeline (assuming 30 years for now).")
    if not spending:
        notes.append("No spending history yet; enter your expected annual spending in retirement.")
    return RetirementStatus(
        age=age, retirement_age=plan.retirement_age, years_to_retirement=years, annual_spending=spending,
        spending_source=source, withdrawal_rate_pct=plan.withdrawal_rate_pct, expected_return_pct=plan.expected_return_pct,
        fi_number=round(fi, 2), target=round(target, 2), target_source=target_source, invested=invested, monthly_contributions=monthly, contribution_breakdown=breakdown,
        projected_at_retirement=round(projected, 2), on_track=projected >= target - 1, required_monthly=round(required, 2),
        monthly_gap=round(max(0.0, required - monthly), 2),
        fi_age=round(age + m_fi / 12, 1) if (m_fi is not None and age is not None) else None,
        coast_on_time=m_coast is not None and age is not None and age + m_coast / 12 <= plan.retirement_age,
        coast_number=round(coast, 2), coast_reached=invested >= coast, coast_progress_pct=round(min(100, invested / coast * 100), 1) if coast else 0,
        coast_age=round(age + m_coast / 12, 1) if (m_coast is not None and age is not None) else None,
        progress_pct=round(min(100, invested / target * 100), 1) if target else 0,
        accounts=[{"name": a.name, "kind": a.kind, "balance": a.balance} for a in accounts], notes=notes,
    )


def overview(today: date | None = None) -> dict:
    today = today or date.today()
    plan = _plan()
    rs = retirement_status(today, plan)
    roth = roth_status(today, plan)
    return {"retirement": asdict(rs) if rs else None, "roth": asdict(roth), "plan": {
        "roth_target": plan.roth_target, "include_gap_in_plan": plan.include_gap_in_plan},
        "disclaimer": "Projections use your assumptions (return, spending, withdrawal rate). Not tax or investment advice."}
