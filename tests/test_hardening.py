"""Regression tests for the security review: cross-site writes, proxy detection, query limits,
failure isolation (Plaid, Claude API, scheduler, tools), cost controls and the categorization rules."""
import asyncio
import datetime as dt
import os
import tempfile
import time
from types import SimpleNamespace as N

os.environ["LEDGER_DATA"] = os.environ.get("LEDGER_DATA") or tempfile.mkdtemp()

import anthropic  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from plaid.exceptions import ApiException  # noqa: E402
from sqlmodel import select  # noqa: E402

from app import categorize, llm, main, security  # noqa: E402
from app.web.routes import agent as agent_routes  # noqa: E402
from app.agent import runner, tools  # noqa: E402
from app.categorize import builtin_category  # noqa: E402
from app.db import init_db, session  # noqa: E402
from app.ingest import sources  # noqa: E402
from app.models import AgentRun, PlaidItem, Txn  # noqa: E402
from tests.test_update_mode import FakePlaid, make_items, status  # noqa: E402

init_db()
MAC = ("127.0.0.1", 50000)


def api_error(cls, code=401):
    return cls("boom", response=httpx.Response(code, request=httpx.Request("POST", "https://api.anthropic.com")), body=None)


@pytest.fixture(autouse=True)
def fresh_auth():
    security.AUTH_FILE.unlink(missing_ok=True)
    security._failures.clear()
    yield
    security.AUTH_FILE.unlink(missing_ok=True)


# ---------------- cross-site writes ----------------

@pytest.mark.parametrize("headers", [
    {"Origin": "null"},                                   # sandboxed iframe / redirect chain
    {"Origin": "https://evil.example"},
    {"Sec-Fetch-Site": "cross-site"},                     # browser says so even without Origin
    {"Sec-Fetch-Site": "same-site", "Origin": "http://localhost"},
])
def test_cross_site_writes_refused(headers):
    with TestClient(main.app, client=MAC, follow_redirects=False) as c:
        r = c.post("/privacy/clear_log", headers={"Host": "localhost:8766", **headers})
        assert r.status_code == 403


def test_same_origin_writes_allowed():
    with TestClient(main.app, client=MAC, follow_redirects=False) as c:
        r = c.post("/privacy/clear_log", headers={"Host": "localhost:8766", "Origin": "http://localhost:8766",
                                                  "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 303


# ---------------- who counts as "this Mac" ----------------

@pytest.mark.parametrize("headers", [{"X-Forwarded-For": "100.88.1.2"}, {"X-Forwarded-Host": "mac.tail1.ts.net"},
                                     {"Tailscale-User-Login": "me@example.com"}, {"Host": "mac.tail1.ts.net"}])
def test_anything_proxied_needs_the_passcode(headers):
    security.set_passcode("correct-horse-battery")
    with TestClient(main.app, client=MAC, follow_redirects=False) as c:
        assert c.get("/transactions", headers=headers).status_code == 303  # to /login


def test_short_passcodes_rejected():
    with pytest.raises(ValueError):
        security.set_passcode("123456")


def test_session_cookie_is_secure_over_https():
    security.set_passcode("correct-horse-battery")
    with TestClient(main.app, client=("100.88.12.34", 5000), follow_redirects=False) as c:
        r = c.post("/login", data={"passcode": "correct-horse-battery", "next": "/"},
                   headers={"X-Forwarded-Proto": "https", "Host": "mac.tail1.ts.net"})
        cookie = r.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie


def test_unknown_question_is_404():
    with TestClient(main.app, client=MAC) as c:
        assert c.get("/runs/987654").status_code == 404


# ---------------- agent SQL limits ----------------

@pytest.mark.parametrize("q", [
    "select length(zeroblob(100000000))",
    "select count(*) from transactions a, transactions b, transactions c, transactions d",
])
def test_runaway_queries_are_stopped_quickly(q):
    sources.load_demo(with_truth_categories=True)
    t = time.monotonic()
    out = tools.run_sql(q)
    assert out.startswith("SQL error") and time.monotonic() - t < 5


def test_tool_crash_becomes_error_result(monkeypatch):
    monkeypatch.setattr(tools, "get_goals", lambda: 1 / 0)
    out, is_err = asyncio.run(runner.FinanceAgent("q").safe_call("get_goals", {}))
    assert is_err and "ZeroDivisionError" in out


# ---------------- Claude API failures ----------------

class Fake:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return N(content=item, stop_reason="tool_use", usage=N(input_tokens=500, output_tokens=100,
                                                                cache_read_input_tokens=0, cache_creation_input_tokens=0))


def tool_use(id, name, inp):
    return N(type="tool_use", id=id, name=name, input=inp)


def run_agent(fake, monkeypatch, **kw):
    monkeypatch.setattr(runner, "client", lambda: fake)
    agent = runner.FinanceAgent("How much did I spend on travel in June 2026?", **kw)
    agent.create()
    answer = asyncio.run(agent.run())
    with session() as s:
        return answer, s.get(AgentRun, agent.run_id)


@pytest.mark.parametrize("err,words", [
    (api_error(anthropic.AuthenticationError, 401), "API key"),
    (api_error(anthropic.RateLimitError, 429), "rate-limiting"),
    (api_error(anthropic.InternalServerError, 500), "temporarily unavailable"),
])
def test_api_errors_give_a_clear_message(monkeypatch, err, words):
    answer, run = run_agent(Fake([err]), monkeypatch)
    assert run.status == "failed" and words in answer and "boom" not in answer


def test_categorization_outage_keeps_rule_based_categories(monkeypatch):
    async def down(**kw):
        raise api_error(anthropic.AuthenticationError, 401)
    monkeypatch.setattr(llm, "structured", down)
    with session() as s:
        for t in s.exec(select(Txn).where(Txn.id.in_(["hz1", "hz2"]))).all():
            s.delete(t)
        s.add(Txn(id="hz1", account_id="demo_checking", date=dt.date(2026, 10, 1), amount=-2000, raw_name="ACME PAYROLL 1001", source="demo"))
        s.add(Txn(id="hz2", account_id="demo_checking", date=dt.date(2026, 10, 1), amount=9.99, raw_name="ZZQX UNKNOWN 77", source="demo"))
        s.commit()
    try:
        stats = asyncio.run(categorize.categorize_pending(use_llm=True))
        with session() as s:
            assert s.get(Txn, "hz1").category == "Income"     # saved even though the API was down
            assert s.get(Txn, "hz2").category == ""           # left for next time, not lost
        assert "API key" in stats["error"] and stats["left"] >= 1
    finally:
        with session() as s:
            for t in s.exec(select(Txn).where(Txn.id.in_(["hz1", "hz2"]))).all():
                s.delete(t)
            s.commit()


def test_one_failed_categorization_batch_keeps_the_others(monkeypatch):
    calls = {"n": 0}

    async def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise api_error(anthropic.InternalServerError, 500)
        return categorize.Batch(items=[categorize.Labeled(id=i, category="Shopping", merchant="M") for i in range(80)]), 0.01
    monkeypatch.setattr(llm, "structured", flaky)
    labels, _ = asyncio.run(categorize.llm_label([(f"SHOP {i}", 5.0) for i in range(160)]))
    assert len(labels) == 80


# ---------------- Plaid failures ----------------

@pytest.fixture
def plaid_cleanup():
    yield
    with session() as s:  # other test modules share this database
        for t in s.exec(select(Txn).where(Txn.id.like("t-tok-%"))).all():
            s.delete(t)
        for it in s.exec(select(PlaidItem)).all():
            s.delete(it)
        s.commit()


class Unreachable(FakePlaid):
    def item_get(self, req):
        if self._mode(req) == "down":
            e = ApiException(status=503, reason="down")
            e.body = '{"error_code": "INTERNAL_SERVER_ERROR", "error_message": "x"}'
            raise e
        if self._mode(req) == "network":
            raise ConnectionError("connection reset")
        return super().item_get(req)


def test_plaid_outage_is_not_a_login_problem(monkeypatch, plaid_cleanup):
    fake = make_items(good="ok", flaky="down")
    monkeypatch.setattr(sources, "plaid_client", lambda: Unreachable(fake.modes))
    res = sources.sync_plaid()
    assert status("flaky").status == "unavailable" and status("good").status == "ok" and res["added"] == 1


def test_network_or_keychain_error_on_one_bank_does_not_stop_the_rest(monkeypatch, plaid_cleanup):
    fake = make_items(good="ok", broken="network")
    monkeypatch.setattr(sources, "plaid_client", lambda: Unreachable(fake.modes))
    res = sources.sync_plaid()
    assert res["added"] == 1 and status("broken").status == "unavailable" and status("good").last_synced


def test_scheduler_survives_a_failed_round(monkeypatch):
    calls = {"n": 0}

    def broken_session():
        calls["n"] += 1
        raise RuntimeError("database is locked")
    monkeypatch.setattr(main, "session", broken_session)

    async def go():
        task = asyncio.create_task(main.daily_sync(first_delay=0, every=0.01))
        await asyncio.sleep(0.1)
        alive = not task.done()
        task.cancel()
        return alive
    assert asyncio.run(go()) and calls["n"] >= 2


# ---------------- cost controls ----------------

def test_conversation_cache_breakpoint_on_last_block_only():
    msgs = [{"role": "user", "content": "q"},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "x"},
                                         {"type": "tool_result", "tool_use_id": "b", "content": "y"}]}]
    out = runner.cache_tail(msgs)
    assert out[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in out[-1]["content"][0] and "cache_control" not in msgs[-1]["content"][-1]


def test_escalates_to_stronger_model_after_rejection(monkeypatch):
    sql = "select round(sum(amount),2) as total from transactions where category='Travel' and month='2026-06'"
    total = float(tools.run_sql(sql).split("\n")[1])
    fake = Fake([[tool_use("a", "run_sql", {"query": sql})],
                 [tool_use("b", "final_answer", {"answer": "You spent $9,999.00 on travel."})],
                 [tool_use("c", "final_answer", {"answer": f"You spent ${total:,.2f} on travel in June 2026."})]])
    _, run = run_agent(fake, monkeypatch, model="claude-haiku-5-5", escalate_to="claude-opus-5-5")
    assert [c["model"] for c in fake.calls] == ["claude-haiku-5-5", "claude-haiku-5-5", "claude-opus-5-5"]
    assert run.verified and run.model == "claude-haiku-5-5,claude-opus-5-5"


def test_prices_known_for_current_models():
    usage = N(input_tokens=1_000_000, output_tokens=0, cache_read_input_tokens=1_000_000, cache_creation_input_tokens=0)
    assert llm.cost("claude-haiku-5-5", usage) == pytest.approx(0.11)
    assert llm.cost("claude-opus-5-5", usage) == pytest.approx(4.2)   # cache reads at 5% on Opus 5.5


def test_daily_question_budget(monkeypatch):
    monkeypatch.setattr(agent_routes, "load_config", lambda: {"agent": {"daily_cost_usd": 0.01}})
    with session() as s:
        s.add(AgentRun(question="earlier", status="done", cost_usd=0.02))
        s.commit()
        before = len(s.exec(select(AgentRun)).all())
    with TestClient(main.app, client=MAC, follow_redirects=False) as c:
        r = c.post("/ask", data={"question": "anything"})
    with session() as s:
        assert r.headers["location"] == "/runs" and len(s.exec(select(AgentRun)).all()) == before
        for run in s.exec(select(AgentRun).where(AgentRun.question == "earlier")).all():
            s.delete(run)
        s.commit()
    assert "budget" in main.STATUS["message"]
    main.STATUS["message"] = ""


# ---------------- built-in category rules ----------------

@pytest.mark.parametrize("raw,amount,cat", [
    ("ACME CORP PAYROLL", -2500, "Income"),
    ("CELERY SALARYMAN NOODLES", 14.5, None),     # no whole-word match
    ("SALARY FINANCE CO", 49.0, None),            # a charge is never income
    ("HOMEDEPOTEPAY 1234", 80.0, None),
    ("CHASE AUTOPAY", 500.0, "Transfers"),
    ("FIDELITY ROTH CONTRIB", 500.0, "Transfers"),
])
def test_builtin_rules_need_whole_words_and_the_right_sign(raw, amount, cat):
    assert builtin_category(raw, amount) == cat


def test_run_trace_export(monkeypatch):
    sql = "select round(sum(amount),2) as total from transactions where category='Travel' and month='2026-06'"
    total = float(tools.run_sql(sql).split("\n")[1])
    _, run = run_agent(Fake([[tool_use("a", "run_sql", {"query": sql})],
                             [tool_use("b", "final_answer", {"answer": f"You spent ${total:,.2f} on travel in June 2026."})]]),
                       monkeypatch)
    with TestClient(main.app, client=MAC) as c:
        t = c.get(f"/runs/{run.id}.json").json()
        assert c.get(f"/runs/{run.id}").status_code == 200
    assert t["verified"] and t["input_tokens"] == 1000 and t["output_tokens"] == 200
    assert [s["kind"] for s in t["steps"]][:3] == ["question", "tool_call", "tool_result"]


@pytest.mark.parametrize("payload", [b"Account Number,Symbol\x00\x00,Current Value\n\x00\"unterminated,1\n", b"\xff\xfe\x00garbage", b""])
def test_malformed_fidelity_file_gives_a_message_not_a_crash(payload):
    with TestClient(main.app, client=MAC, follow_redirects=False) as c:
        r = c.post("/retirement/import-fidelity", files={"file": ("Portfolio_Positions.csv", payload, "text/csv")})
    assert r.status_code == 303 and "error=" in r.headers["location"]
