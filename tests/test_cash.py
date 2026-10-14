import datetime as dt
import os
import tempfile
from datetime import date

os.environ["LEDGER_DATA"] = os.environ.get("LEDGER_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

from app import cash, planning  # noqa: E402
from app.db import init_db, session  # noqa: E402
from app.models import CashAccount, Goal, Txn  # noqa: E402

init_db()
TODAY = date(2026, 10, 14)


def _wipe():
    with session() as s:
        for m in (CashAccount, Goal):
            for r in s.exec(select(m)).all():
                s.delete(r)
        for t in s.exec(select(Txn).where(Txn.id.startswith("hysa"))).all():
            s.delete(t)
        s.commit()


@pytest.fixture(autouse=True)
def clean():
    _wipe()
    yield
    _wipe()  # other test modules share this database


def add_account(**kw):
    with session() as s:
        a = CashAccount(**{"name": "Marcus HYSA", "balance": 12000, "apy_pct": 4.0, "as_of": TODAY, **kw})
        s.add(a)
        s.commit()
        s.refresh(a)
        return a


def add_transfer(i, d, amount, name="MARCUS SAVINGS TRANSFER"):
    with session() as s:
        s.add(Txn(id=f"hysa{i}", account_id="chk", date=d, amount=amount, raw_name=name, category="Transfers"))
        s.commit()


def test_interest_and_totals():
    add_account()
    add_account(name="Old checking", balance=500, apy_pct=0, kind="checking")
    sm = cash.summary(TODAY)
    marcus = sm["accounts"][0]
    assert marcus["interest_year"] == 480 and marcus["interest_month"] == 40
    assert sm["total"] == 12500 and sm["interest_year"] == 480


def test_monthly_deposits_from_transfers():
    add_transfer(1, date(2026, 7, 15), 500)
    add_transfer(2, date(2026, 8, 15), 500)
    add_transfer(3, date(2026, 9, 15), 800)
    add_transfer(4, date(2026, 9, 20), -200)        # moved some back out
    add_transfer(5, date(2026, 10, 2), 999)          # current month: not complete, ignored
    add_transfer(6, date(2026, 9, 3), 50, "COFFEE")  # unrelated
    dep, n = cash.monthly_deposits("marcus", TODAY)
    assert dep == 533.33 and n == 4


def test_stale_flag():
    add_account(as_of=TODAY - dt.timedelta(days=60))
    assert cash.summary(TODAY)["accounts"][0]["stale"]


def test_goal_follows_account_balance_and_deposits():
    a = add_account(detect_pattern="marcus", apy_pct=0)
    add_transfer(1, date(2026, 7, 15), 500)
    add_transfer(2, date(2026, 8, 15), 500)
    add_transfer(3, date(2026, 9, 15), 500)
    g = Goal(name="Emergency fund", target_amount=15000, target_date=date(2027, 4, 30), current_amount=0, account_id=a.id)
    st = planning.goal_status(g, TODAY)
    assert st["current"] == 12000 and st["monthly_now"] == 500 and st["months_left"] == 6
    assert st["projected"] == 15000 and st["on_track"] and st["linked_account"]["name"] == "Marcus HYSA"


def test_linked_goal_counts_interest():
    a = add_account(balance=10000, apy_pct=4.8)
    g = Goal(name="House", target_amount=20000, target_date=date(2027, 10, 1), account_id=a.id, monthly_contribution=500)
    st = planning.goal_status(g, TODAY)
    r, n = 0.048 / 12, 12
    expected = 10000 * (1 + r) ** n + 500 * ((1 + r) ** n - 1) / r
    assert st["projected"] == pytest.approx(expected, abs=0.01) and st["projected"] > 10000 + 500 * 12


def test_routes_add_update_delete_unlinks_goal():
    from app.main import app
    with TestClient(app, client=("127.0.0.1", 5000), follow_redirects=False) as c:
        assert c.post("/accounts/manual", data={"name": "Ally HYSA", "kind": "savings", "balance": "$5,000", "apy_pct": "4.2"}).status_code == 303
        with session() as s:
            a = s.exec(select(CashAccount)).one()
        assert a.balance == 5000 and a.apy_pct == 4.2 and a.as_of == date.today()
        c.post("/goals/new", data={"name": "Trip", "target_amount": "3000", "target_date": "2027-06-01", "account_id": str(a.id)})
        c.post(f"/accounts/manual/{a.id}", data={"balance": "5200", "apy_pct": "4.2", "detect_pattern": "ALLY"})
        with session() as s:
            assert s.get(CashAccount, a.id).balance == 5200
            g = s.exec(select(Goal)).one()
            assert g.account_id == a.id
        assert c.get("/connect").status_code == 200 and c.get("/goals").status_code == 200
        c.post(f"/accounts/manual/{a.id}", data={"balance": "0", "delete": "1"})
        with session() as s:
            assert s.exec(select(CashAccount)).first() is None and s.exec(select(Goal)).one().account_id is None
