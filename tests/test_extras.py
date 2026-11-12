"""Backups and export, the 30-day forecast, the paycheck plan, savings-rate trend, card rewards,
notifications and the facts-based weekly digest."""
import asyncio
import datetime as dt
import io
import json
import os
import tempfile
import zipfile
from types import SimpleNamespace as N

os.environ["LEDGER_DATA"] = os.environ.get("LEDGER_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

from app import backup, digest, fire, forecast, main, notify, rewards  # noqa: E402
from app.db import init_db, session  # noqa: E402
from app.ingest import sources  # noqa: E402
from app.models import Account, AgentRun, CardReward, Notified, PlaidItem, RetirementPlan  # noqa: E402

init_db()
sources.load_demo(with_truth_categories=True)
MAC = ("127.0.0.1", 50000)
TODAY = dt.date(2026, 10, 1)


@pytest.fixture(autouse=True)
def clean():
    yield
    with session() as s:
        for m in (CardReward, Notified, RetirementPlan):
            for r in s.exec(select(m)).all():
                s.delete(r)
        s.commit()


def test_backup_once_a_day_keeps_last_14(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "backups")
    first = backup.backup_now(dt.date(2026, 1, 1))
    assert first and first.stat().st_size > 0 and oct(first.stat().st_mode)[-3:] == "600"
    assert backup.backup_now(dt.date(2026, 1, 1)) is None  # already done today
    for i in range(2, 20):
        backup.backup_now(dt.date(2026, 1, i))
    names = [b.name for b in backup.backups()]
    assert len(names) == 14 and names[-1] == "ledger-2026-01-19.db"


def test_export_has_data_but_never_tokens():
    with session() as s:
        s.merge(PlaidItem(id="exp-item", access_token="keychain:exp-item", institution="Test Bank"))
        s.commit()
    with TestClient(main.app, client=MAC) as c:
        r = c.get("/export")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert "transactions.csv" in z.namelist() and "attachment" in r.headers["content-disposition"]
    assert z.read("transactions.csv").count(b"\n") > 100
    everything = b"".join(z.read(n) for n in z.namelist())
    assert b"keychain:" not in everything and b"access_token" not in everything
    with session() as s:
        s.delete(s.get(PlaidItem, "exp-item"))
        s.commit()


def test_forecast_walks_balance_with_bills_paychecks_and_everyday_spending():
    with session() as s:
        s.merge(Account(id="demo_checking", name="Everyday Checking", type="depository", mask="0042", source="demo",
                        balance_current=3000.0))
        s.commit()
    fc = forecast.forecast(TODAY)
    assert fc["start"] == 3000.0 and len(fc["series"]) == 31
    assert any(e["kind"] == "income" for e in fc["events"]) and any(e["kind"] == "subscription" for e in fc["events"])
    net = sum(e["amount"] for e in fc["events"])
    assert fc["end"] == pytest.approx(3000 + net - fc["everyday_per_day"] * 30, abs=0.5)
    assert fc["lowest"] <= min(p["balance"] for p in fc["series"]) + 0.01


def test_paycheck_plan_and_savings_trend():
    with session() as s:
        s.merge(RetirementPlan(id=1, birth_year=1996, retirement_age=45, fire_target_low=1_000_000, roth_target=False))
        s.commit()
    pp = fire.paycheck_plan(TODAY)
    assert pp and pp["rows"][-1]["what"] == "Brokerage" and pp["from_take_home"] >= 0
    trend = fire.savings_rate_trend(TODAY)
    assert len(trend) == 12 and all(-200 < m["rate"] < 100 for m in trend)


def test_card_rewards_pick_the_best_card_per_category():
    with session() as s:
        s.merge(Account(id="demo_credit", name="Rewards Card", type="credit", mask="7781", source="demo"))
        s.merge(Account(id="dining_card", name="Dining Card", type="credit", mask="1111", source="demo"))
        s.commit()
    rewards.save("demo_credit", 1.0, {}, 1.0)
    rewards.save("dining_card", 1.0, {"Dining": 4}, 1.0)
    a = rewards.analysis(dt.date(2026, 10, 1))
    dining = next(r for r in a["rows"] if r["category"] == "Dining")
    assert dining["best_card"].startswith("Dining Card") and dining["missed"] == pytest.approx(dining["spend"] * 0.03, abs=0.05)
    assert a["optimal"] > a["earned"] > 0


def test_notifications_fire_once_per_event(monkeypatch):
    shown = []
    monkeypatch.setattr(notify, "enabled", lambda: True)
    monkeypatch.setattr(notify, "show", lambda t, m: shown.append(t))
    with session() as s:
        s.merge(PlaidItem(id="nt-item", access_token="keychain:nt", institution="Test Bank", status="login_required"))
        s.commit()
    first = notify.run()
    assert first >= 1 and "Bank needs attention" in shown
    assert notify.run() == 0  # same events don't notify again
    with session() as s:
        s.delete(s.get(PlaidItem, "nt-item"))
        s.commit()


def test_notification_text_is_passed_as_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.subprocess, "run", lambda args, **kw: calls.append(args))
    notify.show('Title "x"', 'Netflix" & do shell script "rm -rf ~')
    script, argv = calls[0][:-2], calls[0][-2:]
    assert all("Netflix" not in part for part in script) and argv[0].startswith("Netflix")


class Fake:
    def __init__(self, texts):
        self.texts, self.calls, self.messages = list(texts), [], self

    async def create(self, **kw):
        self.calls.append(kw)
        return N(content=[N(type="text", text=self.texts.pop(0))],
                 usage=N(input_tokens=1200, output_tokens=150, cache_read_input_tokens=0, cache_creation_input_tokens=0))


def test_digest_uses_only_numbers_from_the_facts(monkeypatch):
    facts = digest.facts(TODAY)
    week = facts["spending"]["this_week"]
    fake = Fake([f"Spending: you spent $9,999.99 this week.",                      # invented -> rejected
                 f"Spending: you spent ${week:,.2f} this week. This week: skip takeout."])
    monkeypatch.setattr(digest, "client", lambda: fake)
    text = asyncio.run(digest.write(TODAY))
    assert f"{week:,.2f}" in text and len(fake.calls) == 2 and "tools" not in fake.calls[0]
    with session() as s:
        run = s.exec(select(AgentRun).where(AgentRun.kind == "digest").order_by(AgentRun.id.desc())).first()
    assert run.verified and run.turns == 2 and run.status == "done"
