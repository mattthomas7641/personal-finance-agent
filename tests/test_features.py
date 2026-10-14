"""Subscriptions decisions, dismissable alerts, agent proposals with approval, the weekly digest schedule,
the budget table, what-if and the merged Plan page."""
import asyncio
import datetime as dt
import json
import os
import tempfile
from types import SimpleNamespace as N

os.environ["LEDGER_DATA"] = os.environ.get("LEDGER_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

from app import insights, main, planning, subscriptions  # noqa: E402
from app.agent import runner  # noqa: E402
from app.categorize import normalize  # noqa: E402
from app.db import init_db, session  # noqa: E402
from app.ingest import sources  # noqa: E402
from app.models import (AgentRun, Budget, Dismissal, Proposal, RetirementPlan, SubscriptionDecision,  # noqa: E402
                        Txn)

init_db()
sources.load_demo(with_truth_categories=True)
MAC = ("127.0.0.1", 50000)


def client():
    return TestClient(main.app, client=MAC, follow_redirects=False)


@pytest.fixture(autouse=True)
def clean():
    yield
    with session() as s:
        for m in (SubscriptionDecision, Dismissal, Proposal):
            for r in s.exec(select(m)).all():
                s.delete(r)
        for t in s.exec(select(Txn).where(Txn.id.like("feat-%"))).all():
            s.delete(t)
        s.commit()


# ---------------- subscriptions ----------------

def test_subscriptions_have_next_charge_history_and_cost():
    subs = subscriptions.overview(dt.date(2026, 10, 1))
    netflix = next(i for i in subs["items"] if "netflix" in i["merchant"].lower())
    assert netflix["status"] == "review" and netflix["next_expected"] > netflix["last_charged"]
    assert [h["amount"] for h in netflix["history"]][-2:] == [15.49, 17.99]
    assert subs["annual"] == pytest.approx(sum(i["annual"] for i in subs["items"] if i["status"] != "cancelled"), abs=0.01)


def test_charge_after_cancel_is_flagged():
    subs = subscriptions.overview(dt.date(2026, 10, 1))
    item = subs["items"][0]
    subscriptions.decide(item["key"], "cancelled", today=dt.date(2026, 9, 1))
    with session() as s:
        last = s.exec(select(Txn).where(Txn.amount > 0).order_by(Txn.date.desc())).all()
        t = next(t for t in last if normalize(t.raw_name) == item["key"])
        s.add(Txn(id="feat-late", account_id=t.account_id, date=dt.date(2026, 9, 20), amount=t.amount,
                  raw_name=t.raw_name, merchant=t.merchant, category=t.category, source="demo"))
        s.commit()
    after = subscriptions.overview(dt.date(2026, 10, 1))
    assert any(a["merchant"] == item["merchant"] for a in after["alerts"])


def test_decision_route():
    key = subscriptions.overview()["items"][0]["key"]
    with client() as c:
        assert c.post("/subscriptions/status", data={"key": key, "status": "keep"}).status_code == 303
        assert c.get("/subscriptions").status_code == 200
    assert subscriptions.decisions()[key].status == "keep"


# ---------------- alerts ----------------

def test_dismissed_alert_stays_hidden_and_agent_skips_it():
    from app.agent import tools
    alerts = insights.alerts()
    dup = next(a for a in alerts if a["kind"] == "Possible duplicate")
    with client() as c:
        c.post("/alerts/dismiss", data={"key": dup["key"]})
    assert dup["key"] not in {a["key"] for a in insights.alerts()}
    assert json.loads(tools.get_insights())["duplicates"] == []


# ---------------- proposals ----------------

def tool_use(id, name, inp):
    return N(type="tool_use", id=id, name=name, input=inp)


class Fake:
    def __init__(self, script):
        self.script, self.messages = list(script), self

    async def create(self, **kw):
        return N(content=self.script.pop(0), stop_reason="tool_use",
                 usage=N(input_tokens=10, output_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=0))


def test_agent_proposes_and_user_approves(monkeypatch):
    fake = Fake([[tool_use("a", "propose_budget", {"category": "Dining", "monthly_limit": 275, "reason": "Dining is up 20%."})],
                 [tool_use("b", "propose_budget", {"category": "Income", "monthly_limit": 5, "reason": "x"})],
                 [tool_use("c", "final_answer", {"answer": "I suggest a $275 Dining limit."})]])
    monkeypatch.setattr(runner, "client", lambda: fake)
    agent = runner.FinanceAgent("Where should I cut?")
    agent.create()
    asyncio.run(agent.run())
    with session() as s:
        props = s.exec(select(Proposal).where(Proposal.run_id == agent.run_id)).all()
        run = s.get(AgentRun, agent.run_id)
        before = s.get(Budget, "Dining")
        before = before.monthly_limit if before else None
    assert len(props) == 1 and props[0].status == "pending" and run.verified  # Income proposal refused
    assert before != 275  # nothing applied yet
    with client() as c:
        assert c.get(f"/runs/{agent.run_id}").status_code == 200
        c.post(f"/proposals/{props[0].id}/approve")
    with session() as s:
        assert s.get(Budget, "Dining").monthly_limit == 275 and s.get(Proposal, props[0].id).status == "approved"
        s.delete(s.get(Budget, "Dining"))
        s.commit()


def test_retirement_target_proposal_applies_on_approval():
    with session() as s:
        s.merge(RetirementPlan(id=1, birth_year=1996, retirement_age=40, fire_target_low=1_500_000, fire_target_high=2_000_000))
        p = Proposal(run_id=0, kind="retirement_target", payload=json.dumps({"retirement_age": 45, "target": 1_500_000}))
        s.add(p)
        s.commit()
        s.refresh(p)
    with client() as c:
        c.post(f"/proposals/{p.id}/reject")
        c.post(f"/proposals/{p.id}/approve")  # already decided: ignored
    with session() as s:
        assert s.get(RetirementPlan, 1).retirement_age == 40
        s.delete(s.get(RetirementPlan, 1))
        s.commit()


# ---------------- weekly digest ----------------

def test_weekly_digest_schedule(monkeypatch):
    launched = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(runner, "launch", lambda q, **kw: launched.append(kw) or 1)
    monkeypatch.setattr("app.privacy.local_only", lambda: False)
    now = dt.datetime(2026, 10, 19, 9, tzinfo=dt.timezone.utc)
    with session() as s:
        for r in s.exec(select(AgentRun).where(AgentRun.kind == "digest")).all():
            s.delete(r)
        s.commit()
    assert main.maybe_weekly_digest(now) == 1 and launched[-1] == {"kind": "digest"}
    with session() as s:
        s.add(AgentRun(question="digest", kind="digest", created_at=now - dt.timedelta(days=3)))
        s.commit()
    assert main.maybe_weekly_digest(now) is None  # last one is only 3 days old
    monkeypatch.setattr("app.privacy.local_only", lambda: True)
    assert main.maybe_weekly_digest(now + dt.timedelta(days=30)) is None  # local-only mode never sends on its own
    with session() as s:
        for r in s.exec(select(AgentRun).where(AgentRun.kind == "digest")).all():
            s.delete(r)
        s.commit()


# ---------------- planning pages ----------------

def test_budget_table_has_averages_and_pace():
    rows = planning.budget_table(dt.date(2026, 10, 15))
    dining = next(r for r in rows if r["category"] == "Dining")
    assert dining["avg3"] > 0 and dining["avg12"] > 0 and "Income" not in {r["category"] for r in rows}


def test_plan_settings_keep_roth_fields_and_retirement_redirects():
    with session() as s:
        s.merge(RetirementPlan(id=1, birth_year=1996, retirement_age=40, roth_target=True, roth_contributed=2500, harsh=True))
        s.commit()
    with client() as c:
        c.post("/plan/settings", data={"birth_year": "1996", "retirement_age": "42", "fire_target_low": "1500000",
                                       "fire_target_high": "", "expected_return_pct": "5", "has_harsh": "1"})
        assert c.get("/retirement").headers["location"] == "/plan#accounts"
        assert c.get("/plan").status_code == 200
        r = c.get("/plan/whatif", params={"retire_age": "45", "monthly": "2000", "return_pct": "5", "target": "1500000"})
        assert r.status_code == 200 and "you'd have" in r.text
    with session() as s:
        rp = s.get(RetirementPlan, 1)
        assert (rp.retirement_age, rp.fire_target_low, rp.roth_target, rp.roth_contributed, rp.harsh) == (42, 1_500_000, True, 2500, False)
        s.delete(rp)
        s.commit()


def test_excluded_category_is_not_spending():
    from app.config import AI_CATEGORIES, NON_SPEND
    assert "Excluded" in NON_SPEND and "Excluded" not in AI_CATEGORIES


def test_answer_formatting_escapes_model_output():
    from app.web.common import _answer_html
    html = str(_answer_html('**Total** <script>alert(1)</script>\n- <img src=x onerror=alert(1)>\n1. ok'))
    assert "<script>" not in html and "<img" not in html and "&lt;script&gt;" in html
    assert "<b>Total</b>" in html and "<ul" in html and "<ol" in html
