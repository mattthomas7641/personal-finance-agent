import os
import tempfile
from datetime import date

os.environ["LEDGER_DATA"] = os.environ.get("LEDGER_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from sqlmodel import select  # noqa: E402

from app import fire  # noqa: E402
from app.db import init_db, session  # noqa: E402
from app.ingest import sources  # noqa: E402
from app.models import Account, CashAccount, Goal, Plan401k, RetirementAccount, RetirementPlan  # noqa: E402

init_db()
sources.load_demo(with_truth_categories=True)
TODAY = date(2026, 10, 14)


@pytest.fixture(autouse=True)
def setup():
    def wipe():
        with session() as s:
            for m in (RetirementAccount, CashAccount, Goal, RetirementPlan, Plan401k):
                for r in s.exec(select(m)).all():
                    s.delete(r)
            s.commit()
    wipe()
    yield
    wipe()


def configure(low=1_500_000, high=2_000_000, invested=58_500, age_target=40, birth=1996, ret=5.0):
    with session() as s:
        s.add(RetirementPlan(id=1, birth_year=birth, retirement_age=age_target, fire_target_low=low,
                             fire_target_high=high, expected_return_pct=ret, roth_target=False))
        s.add(RetirementAccount(name="Brokerage", kind="brokerage", balance=invested, monthly_contribution=1500))
        s.commit()


def test_pmt_and_fv_are_inverse():
    need = fire._pmt(1_500_000, 58_500, 0.05, 116)
    assert fire._fv(58_500, need, 0.05, 116) == pytest.approx(1_500_000, rel=1e-9)
    assert fire._pmt(100, 1_000, 0.05, 12) == 0  # already there


def test_solve_rate():
    r = fire._solve_rate(1_000_000, 50_000, 3_000, 120)
    assert fire._fv(50_000, 3_000, r, 120) == pytest.approx(1_000_000, rel=1e-6)
    assert fire._solve_rate(100, 1_000, 0, 12) == 0.0


@pytest.mark.parametrize("rate,label", [(0.2, "Realistic"), (0.4, "Aggressive"), (0.6, "Extreme"), (0.9, "Not possible on current income")])
def test_realism_bands(rate, label):
    assert fire.realism(rate)[0] == label


def test_plan_out_of_reach_gives_alternatives():
    configure()
    p = fire.plan(TODAY)
    lo, hi = p["targets"]
    assert lo["target"] == 1_500_000 and hi["target"] == 2_000_000
    assert not lo["feasible"] and lo["rating"].startswith("Not possible")
    a = lo["alternatives"]
    assert a["retire_at_age"] > 40 and a["reachable_by_target_age"] < 1_500_000 and a["extra_income_monthly"] > 0
    # the all-in path reaches the target exactly at the alternative age
    months = round((a["retire_at_age"] - p["age"]) * 12)
    assert fire._fv(p["invested"], p["feasible_monthly"], 0.05, months) >= 1_500_000 * 0.99
    assert lo["milestones"][-1]["age"] == 40 and lo["milestones"][-1]["need"] == pytest.approx(1_500_000, rel=1e-3)
    assert any("doesn't fit" in r for r in p["recommendations"])


def test_plan_within_reach_is_realistic():
    configure(low=400_000, high=400_000, invested=150_000)
    p = fire.plan(TODAY)
    [t] = p["targets"]
    assert t["feasible"] and t["rating"] == "Realistic" and t["spending_cut"] == 0


def test_spending_cut_accounts_for_surplus():
    configure()
    p = fire.plan(TODAY)
    t = p["targets"][0]
    assert t["spending_cut"] == pytest.approx(max(0, t["extra_monthly"] - p["surplus"]), abs=0.01)


def test_harsh_cuts_never_touch_rent_and_are_blunt():
    configure()
    p = fire.plan(TODAY)
    cats = [c["category"] for c in p["all_cuts"]]
    assert "Rent & Mortgage" not in cats
    msg = p["harsh_cuts"][0]["message"]
    assert "/mo is $" in msg and "Cut it to" in msg


def test_subscriptions_flags():
    s = fire.subscriptions(TODAY)
    netflix = next(i for i in s["items"] if "netflix" in i["merchant"].lower())
    assert any("price went up" in f for f in netflix["flags"]) and "streaming" in netflix["flags"]
    assert any("streaming" in n for n in s["notes"]) and s["annual"] == pytest.approx(sum(i["annual_cost"] for i in s["items"]))


def test_net_worth_assets_minus_debts():
    with session() as s:
        s.merge(Account(id="nw-chk", name="Checking", type="depository", source="plaid", balance_current=3000))
        s.merge(Account(id="nw-cc", name="Card", type="credit", source="plaid", balance_current=800))
        s.add(CashAccount(name="HYSA", balance=10_000))
        s.add(RetirementAccount(name="401k", kind="401k", balance=40_000))
        s.commit()
    nw = fire.net_worth()
    assert nw["total_assets"] == 53_000 and nw["total_debts"] == 800 and nw["net_worth"] == 52_200
    assert nw["invested"] == 40_000 and nw["liquid"] == 13_000
    with session() as s:
        s.delete(s.get(Account, "nw-chk"))
        s.delete(s.get(Account, "nw-cc"))
        s.commit()


def test_needs_birth_year():
    assert fire.plan(TODAY)["needs"] == "birth_year"
