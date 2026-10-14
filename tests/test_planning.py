import os
import tempfile
from datetime import date

os.environ["LEDGER_DATA"] = os.environ.get("LEDGER_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402

from app import planning as P  # noqa: E402
from app.db import init_db, session  # noqa: E402
from app.ingest import sources  # noqa: E402
from app.models import Budget, Goal, Plan401k  # noqa: E402

init_db()
sources.load_demo(with_truth_categories=True)
TODAY = date(2026, 10, 2)


def plan(**kw):
    base = dict(annual_salary=90000, contribution_pct=6, ytd_contributed=3800, ytd_as_of=date(2026, 9, 25),
                match_rate_pct=100, match_up_to_pct=4, birth_year=1995)
    return Plan401k(**{**base, **kw})


@pytest.mark.parametrize("year,birth,limit", [(2026, 1995, 24500), (2026, 1972, 32500), (2026, 1964, 35750),
                                               (2026, 1962, 32500), (2026, None, 24500), (2025, 1995, 23500)])
def test_irs_limits_with_catch_up(year, birth, limit):
    assert P.limit_for(year, birth)[0] == limit


def test_unpublished_year_falls_back_and_says_so():
    lim, note, official = P.limit_for(2027, 1995)
    assert lim == 24500 and not official and "2026" in note


def test_detects_biweekly_paychecks_from_deposits():
    s = P.detect_paychecks(TODAY)
    assert s.frequency == "biweekly" and s.detected and s.last_pay_date == date(2026, 9, 18)


def test_remaining_paychecks_counts_from_stub_date():
    # biweekly from 2026-09-18: after 09-25 -> 10/2, 10/16, 10/30, 11/13, 11/27, 12/11, 12/25
    assert P.remaining_paychecks("biweekly", date(2026, 9, 18), date(2026, 9, 25)) == 7
    assert P.remaining_paychecks("monthly", None, date(2026, 10, 2)) == 3
    assert P.remaining_paychecks("semimonthly", None, date(2026, 12, 20)) == 1


def test_required_pct_lands_exactly_on_limit():
    k = P.k401_status(TODAY, plan(contribution_pct=20, ytd_contributed=19000))
    gross = 90000 / 26
    assert k.required_pct == pytest.approx((24500 - 19000) / (gross * 7) * 100, abs=0.01)
    assert k.realistic_this_year and "Raise your contribution" in k.recommendation


def test_unrealistic_catch_up_recommends_next_year_pct():
    k = P.k401_status(TODAY, plan())
    assert k.required_pct > 50 and not k.realistic_this_year
    assert k.next_year_pct_to_max == pytest.approx(24500 / 90000 * 100, abs=0.01)
    assert "first 2027 paycheck" in k.recommendation


def test_on_track_when_projection_reaches_limit():
    k = P.k401_status(TODAY, plan(contribution_pct=30, ytd_contributed=19000, has_true_up=True))
    assert k.on_track_to_max and k.shortfall == 0 and k.projected_year_end == 24500


def test_missed_match_and_early_max_warnings():
    low = P.k401_status(TODAY, plan(contribution_pct=2))
    assert not low.getting_full_match and any("left on the table" in n for n in low.notes)
    early = P.k401_status(TODAY, plan(contribution_pct=60, ytd_contributed=18000, has_true_up=False))
    assert early.max_hit_early_paycheck is not None and early.losing_match_from_early_max > 0
    assert any("match stops" in n for n in early.notes)
    trued = P.k401_status(TODAY, plan(contribution_pct=60, ytd_contributed=18000, has_true_up=True))
    assert trued.losing_match_from_early_max == 0


def test_stale_ytd_note_and_new_year_reset():
    old = P.k401_status(TODAY, plan(ytd_as_of=date(2026, 7, 1)))
    assert any("over 6 weeks old" in n for n in old.notes)
    jan = P.k401_status(date(2027, 1, 5), plan(ytd_as_of=date(2026, 12, 20), ytd_contributed=24500))
    assert jan.ytd == 0


def test_cash_flow_uses_complete_months():
    cf = P.cash_flow(TODAY)
    assert cf["months"] == ["2026-07", "2026-08", "2026-09"] and cf["avg_income"] > cf["avg_spending"] > 0


def test_cut_plan_never_touches_essentials_and_adds_up():
    p = P.cut_plan(2500, TODAY)
    cats = {c["category"] for c in p["cuts"]}
    assert not cats & P.ESSENTIAL
    assert p["from_surplus"] + p["from_cuts"] + p["short_by"] == pytest.approx(2500, abs=0.05)
    for c in p["cuts"]:
        assert c["cut"] <= c["realistic_cut"] + 0.01 and c["new_budget"] < c["avg_last_3_months"]


def test_small_target_covered_by_surplus_alone():
    p = P.cut_plan(100, TODAY)
    assert p["covered"] and p["cuts"] == [] and p["from_surplus"] == 100


def test_savings_goal_status():
    g = Goal(name="Emergency fund", target_amount=15000, target_date=date(2027, 6, 30), current_amount=6000, monthly_contribution=500)
    s = P.goal_status(g, TODAY)
    assert s["months_left"] == 8 and s["required_monthly"] == 1125 and s["monthly_gap"] == 625 and not s["on_track"]


def test_retirement_goal_counts_401k_and_compounds():
    k = P.k401_status(TODAY, plan())
    g = Goal(name="Retire", kind="retirement", target_amount=1_000_000, target_date=date(2056, 10, 1),
             current_amount=50_000, expected_return_pct=5)
    s = P.goal_status(g, TODAY, k)
    monthly = (0.06 * 90000 + k.employer_match_year) / 12
    r, n = 0.05 / 12, 360
    expected = 50_000 * (1 + r) ** n + monthly * (((1 + r) ** n - 1) / r)
    assert s["monthly_now"] == pytest.approx(monthly, abs=0.01) and s["projected"] == pytest.approx(expected, rel=1e-6)


def test_apply_plan_writes_budgets():
    P.apply_budgets([("Dining", 250.4)])
    with session() as s:
        assert s.get(Budget, "Dining").monthly_limit == 250 and s.get(Budget, "Dining").source == "plan"


def test_overview_does_not_double_count_401k_and_retirement():
    with session() as s:
        s.merge(Plan401k(id=1, annual_salary=90000, contribution_pct=6, ytd_contributed=4150, ytd_as_of=date(2026, 10, 2),
                         match_rate_pct=100, match_up_to_pct=4, birth_year=1995))
        s.add(Goal(name="Retire", kind="retirement", target_amount=1_500_000, target_date=date(2055, 1, 1),
                   current_amount=40_000, expected_return_pct=5))
        s.commit()
    ov = P.overview(TODAY)
    [r] = [g for g in ov["goals"] if g["kind"] == "retirement"]
    extra = (ov["k401"]["next_year_pct_to_max"] - 6) / 100 * 90000 / 12
    assert r["monthly_gap_after_401k_change"] == pytest.approx(max(0, r["monthly_gap"] - extra), abs=0.01)
    assert ov["monthly_needed_for_goals"] == pytest.approx(r["monthly_gap_after_401k_change"] + ov["monthly_needed_for_401k"], abs=0.02)
    with session() as s:
        for g in s.exec(__import__("sqlmodel").select(Goal)).all():
            s.delete(g)
        s.delete(s.get(Plan401k, 1))
        s.commit()
