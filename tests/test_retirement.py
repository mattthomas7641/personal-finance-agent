import datetime as dt
import os
import tempfile
from datetime import date

os.environ["LEDGER_DATA"] = os.environ.get("LEDGER_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402

from app import retirement as R  # noqa: E402
from app.db import init_db, session  # noqa: E402
from app.ingest import sources  # noqa: E402
from app.models import Plan401k, RetirementAccount, RetirementPlan, Txn  # noqa: E402

init_db()
sources.load_demo(with_truth_categories=True)
TODAY = date(2026, 10, 2)


# ---------------- Roth IRA ----------------

@pytest.mark.parametrize("age,magi,status,expected", [
    (30, 90_000, "single", 7500), (55, 90_000, "single", 8600), (30, None, "single", 7500),
    (30, 168_000, "single", 0), (30, 160_500, "single", 3750),   # halfway through 153k-168k
    (30, 167_900, "single", 200),                                  # IRS $200 floor
    (30, 247_000, "mfj", 3750), (30, 5_000, "mfs", 3750),
])
def test_roth_limit_and_phaseout(age, magi, status, expected):
    allowed, full, note, official = R.roth_allowed(2026, age, magi, status)
    assert allowed == expected and official


def test_roth_phaseout_rounds_up_to_ten():
    allowed, *_ = R.roth_allowed(2026, 30, 154_000, "single")  # 7500 * (1 - 1000/15000) = 7000.0
    assert allowed == 7000
    allowed, *_ = R.roth_allowed(2026, 30, 154_321, "single")  # 6839.5 -> 6840
    assert allowed == 6840


def test_unpublished_year_uses_latest_and_says_so():
    allowed, full, note, official = R.roth_allowed(2027, 30, None, "single")
    assert allowed == 7500 and not official and "2026 figures" in note


def test_roth_status_manual_and_monthly_needed():
    plan = RetirementPlan(birth_year=1995, magi=90_000, roth_contributed=3000)
    s = R.roth_status(TODAY, plan)
    assert s.allowed == 7500 and s.remaining == 4500 and s.months_to_dec == 3
    assert s.monthly_to_max_by_dec == 1500 and s.deadline == "2027-04-15" and not s.maxed


def test_roth_status_detects_transfers():
    with session() as s:
        for i, m in enumerate((1, 4, 7)):
            s.merge(Txn(id=f"roth{i}", account_id="demo_checking", date=date(2026, m, 5), amount=500.0,
                        raw_name="FIDELITY ROTH IRA CONTRIB", category="Transfers"))
        s.merge(Txn(id="roth-old", account_id="demo_checking", date=date(2025, 12, 5), amount=500.0,
                    raw_name="FIDELITY ROTH IRA CONTRIB", category="Transfers"))
        s.commit()
    s_ = R.roth_status(TODAY, RetirementPlan(birth_year=1995, magi=90_000, roth_detect_pattern="fidelity roth"))
    assert s_.contributed == 1500 and "3 transfers" in s_.contributions_source


def test_roth_over_contribution_warning_and_high_income_note():
    over = R.roth_status(TODAY, RetirementPlan(birth_year=1995, magi=90_000, roth_contributed=8000))
    assert any("more than you're allowed" in n for n in over.notes)
    high = R.roth_status(TODAY, RetirementPlan(birth_year=1995, magi=200_000))
    assert high.allowed == 0 and any("can't contribute" in n for n in high.notes)


def test_magi_estimated_from_401k_salary():
    with session() as s:
        s.merge(Plan401k(id=1, annual_salary=100_000, contribution_pct=10, birth_year=1995))
        s.commit()
    st = R.roth_status(TODAY, RetirementPlan())
    assert st.magi_used == 90_000 and st.magi_estimated
    with session() as s:
        s.delete(s.get(Plan401k, 1))
        s.commit()


# ---------------- FI and Coast FIRE ----------------

def test_fv_matches_closed_form():
    r, n = 0.05, 240
    g = (1 + r / 12) ** n
    assert R.fv(10_000, 500, r, n) == pytest.approx(10_000 * g + 500 * (g - 1) / (r / 12))
    assert R.fv(10_000, 500, 0, 12) == 16_000


def _setup(balance, monthly=0.0, **plan):
    with session() as s:
        for a in s.exec(R.select(RetirementAccount)).all():
            s.delete(a)
        s.add(RetirementAccount(name="Brokerage", kind="brokerage", balance=balance, monthly_contribution=monthly))
        s.commit()
    return RetirementPlan(birth_year=1996, retirement_age=60, roth_target=False, **plan)


def test_fi_and_coast_numbers():
    plan = _setup(100_000, annual_spending=40_000, withdrawal_rate_pct=4, expected_return_pct=5)
    st = R.retirement_status(TODAY, plan)
    assert st.fi_number == 1_000_000
    _, months = R.timeline(TODAY, 1996, 60)  # age 30.3 -> 60, the same timeline Plan uses
    assert st.coast_number == pytest.approx(1_000_000 / 1.05 ** (months / 12), abs=0.01)
    assert not st.coast_reached and st.invested == 100_000


def test_coast_reached_when_growth_alone_gets_there():
    plan = _setup(300_000, annual_spending=40_000, withdrawal_rate_pct=4, expected_return_pct=5)
    st = R.retirement_status(TODAY, plan)
    assert st.coast_reached and st.coast_progress_pct == 100
    assert st.projected_at_retirement >= st.fi_number and st.on_track


def test_contributions_bring_coast_age_and_fi_age():
    plan = _setup(50_000, monthly=1500, annual_spending=40_000, withdrawal_rate_pct=4, expected_return_pct=5)
    st = R.retirement_status(TODAY, plan)
    assert st.coast_age is not None and 30 < st.coast_age < 60
    assert st.fi_age is not None and st.fi_age > st.coast_age
    # required monthly is exactly what reaches FI by 60
    proj = R.fv(50_000, st.required_monthly, 0.05, R.timeline(TODAY, 1996, 60)[1])
    assert proj == pytest.approx(1_000_000, rel=1e-3)


def test_spending_defaults_to_last_12_months():
    plan = _setup(10_000, expected_return_pct=5)
    st = R.retirement_status(TODAY, plan)
    assert st.annual_spending == R.trailing_annual_spending(TODAY) > 0 and "last 12 months" in st.spending_source


def test_401k_and_roth_count_toward_monthly():
    with session() as s:
        s.merge(Plan401k(id=1, annual_salary=90_000, contribution_pct=6, match_rate_pct=100, match_up_to_pct=4, birth_year=1996))
        s.commit()
    plan = _setup(10_000, expected_return_pct=5)
    plan.roth_target, plan.magi = True, 80_000
    st = R.retirement_status(TODAY, plan)
    assert st.contribution_breakdown["401(k) contributions"] == 450
    assert st.contribution_breakdown["employer match"] == 300
    assert st.contribution_breakdown["Roth IRA (to max it)"] == 625
    with session() as s:
        s.delete(s.get(Plan401k, 1))
        s.commit()


def test_no_data_returns_none():
    with session() as s:
        for a in s.exec(R.select(RetirementAccount)).all():
            s.delete(a)
        s.commit()
    assert R.retirement_status(TODAY, RetirementPlan()) is None
