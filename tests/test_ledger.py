import asyncio
import os
import tempfile
from types import SimpleNamespace as N

os.environ["LEDGER_DATA"] = tempfile.mkdtemp()

import pytest  # noqa: E402

from app.agent import runner  # noqa: E402
from app.agent.checker import check  # noqa: E402
from app.agent.tools import calculate, run_sql  # noqa: E402
from app.categorize import builtin_category, categorize_pending, correct, normalize  # noqa: E402
from app.db import init_db, readonly_conn, session  # noqa: E402
from app.ingest import sources  # noqa: E402
from app.ingest.demo import FACTS  # noqa: E402
from app import insights  # noqa: E402
from app.models import AgentRun, PlaidItem, Txn  # noqa: E402

init_db()
sources.load_demo(with_truth_categories=True)
with session() as s:
    s.merge(PlaidItem(id="item1", access_token="access-sandbox-SECRET"))
    s.commit()


# ---------------- SQL sandbox ----------------

def test_sql_reads_view():
    out = run_sql("select count(*) as n from transactions")
    assert out.startswith("n\n") and int(out.split("\n")[1]) > 500


@pytest.mark.parametrize("q", [
    "select access_token from plaiditem", "select * from txn", "delete from txn", "update txn set amount = 0",
    "select 1; delete from txn", "attach database 'x.db' as y", "pragma table_info(txn)", "drop view transactions",
    "with t as (select * from plaiditem) select * from t",
])
def test_sql_sandbox_blocks(q):
    out = run_sql(q)
    assert out.startswith(("Error", "SQL error")), out
    assert "SECRET" not in out


def test_readonly_connection_cannot_write_even_directly():
    c = readonly_conn()
    with pytest.raises(Exception):
        c.execute("insert into txn(id) values ('x')")


# ---------------- calculator ----------------

def test_calculate():
    assert calculate("(512.40 - 430.10) / 430.10 * 100").endswith("= 19.135085")
    assert calculate("round(1,234.5 * 12, 2)").endswith("= 14814.0")
    assert calculate("__import__('os')").startswith("Error")
    assert calculate("open('x')").startswith("Error")


# ---------------- number checker ----------------

def test_checker_accepts_rounded_and_scaled_values():
    ev = ["month | total\n2026-05 | 412.37\n2026-06 | 1980.80", "= 380.3442"]
    assert check("June: $1,980.80, up 380.3% from $412 in May (2026-05). Paid Jun 14.", ev) == []
    assert check("About $2k in June.", ev) == []


def test_checker_rejects_invented_numbers():
    ev = ["total\n412.37"]
    assert check("You spent $450 in May, 12% more than usual, across 37 purchases.", ev) == ["$450", "12%", "37"]


def test_checker_allows_numbers_from_question():
    assert check("Yes, you stayed under $500.", ["total\n431.20"], question="Did I stay under $500 on dining?") == []


# ---------------- categorization ----------------

@pytest.mark.parametrize("raw,key", [
    ("SQ *BLUE BOTTLE COFFEE 0412", "BLUE BOTTLE COFFEE"), ("TST* JOE'S PIZZA", "JOE'S PIZZA"),
    ("AMZN Mktp US*2K4L19", "AMZN MKTP US 2K4L19"), ("STARBUCKS STORE 09912", "STARBUCKS"),
])
def test_normalize(raw, key):
    assert normalize(raw) == key or normalize(raw).startswith(key.split()[0])


@pytest.mark.parametrize("raw,cat", [
    ("CHASE CREDIT CRD AUTOPAY", "Transfers"), ("PAYMENT THANK YOU-MOBILE", "Transfers"), ("Zelle to Sam", "Transfers"),
    ("ACME CORP PAYROLL PPD", "Income"), ("INTEREST CHARGE ON PURCHASES", "Fees & Interest"), ("BLUE BOTTLE", None),
])
def test_builtin_rules(raw, cat):
    assert builtin_category(raw) == cat


def test_correction_creates_rule_and_relabels_similar():
    with session() as s:
        t = s.exec(__import__("sqlmodel").select(Txn).where(Txn.raw_name == "NETFLIX.COM")).first()
    n = correct(t.id, "Entertainment")
    assert n >= 10
    with session() as s:
        cats = {x.category for x in s.exec(__import__("sqlmodel").select(Txn).where(Txn.raw_name == "NETFLIX.COM")).all()}
    assert cats == {"Entertainment"}
    correct(t.id, "Subscriptions")


def test_cascade_uses_rules_and_memory_without_llm():
    with session() as s:
        s.add(Txn(id="new1", account_id="demo_credit", date=__import__("datetime").date(2026, 9, 30), amount=6.25,
                  raw_name="SQ *BLUE BOTTLE COFFEE 9999"))
        s.add(Txn(id="new2", account_id="demo_checking", date=__import__("datetime").date(2026, 9, 30), amount=900,
                  raw_name="CHASE CREDIT CRD AUTOPAY"))
        s.commit()
    stats = asyncio.run(categorize_pending(use_llm=False))
    with session() as s:
        assert s.get(Txn, "new1").category == "Coffee" and s.get(Txn, "new1").category_source == "memory"
        assert s.get(Txn, "new2").category == "Transfers"
        s.delete(s.get(Txn, "new1"))
        s.delete(s.get(Txn, "new2"))
        s.commit()
    assert stats["left"] == 0


# ---------------- CSV ----------------

def test_csv_signed_amounts_flipped_to_spend_positive():
    rows = sources.parse_csv("Transaction Date,Description,Amount\n09/01/2026,STARBUCKS,-5.25\n09/02/2026,UBER *TRIP,-14.10\n09/03/2026,REFUND,3.00\n")
    assert [r["amount"] for r in rows] == [5.25, 14.10, -3.00]


def test_csv_debit_credit_columns():
    rows = sources.parse_csv("Date,Description,Debit,Credit\n2026-09-01,RENT,2350.00,\n2026-09-02,PAYROLL,,3125.00\n")
    assert [r["amount"] for r in rows] == [2350.0, -3125.0]


def test_csv_bad_headers():
    with pytest.raises(ValueError):
        sources.parse_csv("foo,bar\n1,2\n")


# ---------------- insights find the planted facts ----------------

def test_insights_find_planted_facts():
    ins = insights.all_insights()
    [inc] = ins["price_increases"]
    assert "netflix" in inc["merchant"].lower() and inc["previous_amount"] == FACTS["netflix_old"] and inc["amount"] == FACTS["netflix_new"]
    assert [(d["amount"], d["dates"][0]) for d in ins["duplicates"]] == [(89.99, "2026-08-03")]
    assert ins["unusual"][0]["amount"] == 1899.0
    assert any("headspace" in s["merchant"].lower() for s in ins["subscriptions"])


def test_month_summary_excludes_transfers_and_income():
    s = insights.month_summary("2026-06")
    cats = {c["category"] for c in s["categories"]}
    assert "Transfers" not in cats and "Income" not in cats and s["income"] > 0


# ---------------- agent loop with a scripted model ----------------

def tool_use(id, name, inp):
    return N(type="tool_use", id=id, name=name, input=inp)


class Fake:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.messages = self

    async def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        content = self.script.pop(0)
        return N(content=content, stop_reason="tool_use", usage=N(input_tokens=500, output_tokens=100,
                                                                    cache_read_input_tokens=0, cache_creation_input_tokens=0))


def run_agent(script, monkeypatch, q="How much did I spend on travel in June 2026?"):
    fake = Fake(script)
    monkeypatch.setattr(runner, "client", lambda: fake)
    agent = runner.FinanceAgent(q)
    agent.create()
    answer = asyncio.run(agent.run())
    with session() as s:
        return answer, s.get(AgentRun, agent.run_id), fake


def test_agent_verified_answer(monkeypatch):
    sql = "select round(sum(amount),2) as total from transactions where category='Travel' and month='2026-06'"
    total = run_sql(sql).split("\n")[1]
    answer, run, _ = run_agent([
        [tool_use("a", "run_sql", {"query": sql})],
        [tool_use("b", "final_answer", {"answer": f"You spent ${float(total):,.2f} on travel in June 2026."})],
    ], monkeypatch)
    assert run.status == "done" and run.verified and total in answer.replace(",", "")


def test_agent_hallucinated_number_is_rejected_then_fixed(monkeypatch):
    sql = "select round(sum(amount),2) as total from transactions where category='Travel' and month='2026-06'"
    answer, run, fake = run_agent([
        [tool_use("a", "run_sql", {"query": sql})],
        [tool_use("b", "final_answer", {"answer": "You spent $2,500.00 on travel, mostly flights."})],
        [tool_use("c", "final_answer", {"answer": "You spent $1,822.30 on travel in June 2026."})],
    ], monkeypatch)
    rejection = fake.calls[2]["messages"][-1]["content"][0]
    assert rejection["is_error"] and "$2,500.00" in rejection["content"]
    assert run.verified and "1,822.30" in answer


def test_agent_marks_unverified_after_retries(monkeypatch):
    bad = [tool_use(str(i), "final_answer", {"answer": "You spent $9,999 on travel."}) for i in range(3)]
    answer, run, _ = run_agent([[b] for b in bad], monkeypatch)
    assert run.status == "done" and not run.verified
