import asyncio
import datetime as dt
import os
import tempfile

os.environ["LEDGER_DATA"] = os.environ.get("LEDGER_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from sqlmodel import select  # noqa: E402

from app import categorize, privacy  # noqa: E402
from app.agent.tools import get_insights, run_sql  # noqa: E402
from app.db import init_db, session  # noqa: E402
from app.models import Outbound, PlaidItem, Txn  # noqa: E402

init_db()


@pytest.mark.parametrize("raw,must_not_contain,must_contain", [
    ("ZELLE TO SAM RIVERA 5551234567", ["SAM", "RIVERA", "555"], ["ZELLE", "[person]"]),
    ("Zelle payment from Jane Doe Conf# 8817", ["Jane", "Doe", "8817"], ["Zelle", "[person]"]),
    ("VENMO *Alex Kim", ["Alex", "Kim"], ["VENMO", "[person]"]),
    ("CASH APP*JOHN SMITH*SENT", ["JOHN", "SMITH"], ["CASH APP"]),
    ("CHASE CREDIT CRD AUTOPAY XXXXX1234", ["1234"], ["AUTOPAY"]),
    ("ACME CORP PAYROLL PPD ID: 1234567890", ["1234567890"], ["PAYROLL"]),
    ("Payment to bob.smith@gmail.com", ["bob", "gmail"], ["[email]"]),
    ("CALL (415) 555-0199 FOR SVC", ["555", "0199"], ["[phone]"]),
    ("SQ *BLUE BOTTLE COFFEE 0412", ["0412"], ["BLUE BOTTLE COFFEE"]),
])
def test_redact(raw, must_not_contain, must_contain):
    out = privacy.redact(raw)
    for s in must_not_contain:
        assert s not in out, (raw, out)
    for s in must_contain:
        assert s in out, (raw, out)


def test_redact_keeps_merchants_and_dates():
    for s in ["UBER *TRIP", "STARBUCKS", "PAYPAL *STEAMGAMES", "TRADER JOE S", "2026-06-14", "2026-06"]:
        assert privacy.redact(s).replace(" ", "") == s.replace(" ", "")


def test_custom_redact_terms(monkeypatch):
    monkeypatch.setattr(privacy, "load_config", lambda: {"privacy": {"redact_terms": ["Matthew Thomas", "Bedford Ave"]}})
    assert privacy.redact("ACH CREDIT MATTHEW THOMAS 22 BEDFORD AVE") == "ACH CREDIT [redacted] 22 [redacted]"


@pytest.fixture
def p2p_txn():
    with session() as s:
        s.add(Txn(id="p2p1", account_id="acct", date=dt.date(2026, 9, 1), amount=75.0,
                  raw_name="ZELLE TO SAM RIVERA 5551234567", merchant="Zelle Sam Rivera", category="Transfers"))
        s.commit()
    yield
    with session() as s:
        s.delete(s.get(Txn, "p2p1"))
        s.commit()


def test_agent_sql_results_are_redacted_but_numbers_intact(p2p_txn):
    out = run_sql("select date, description, merchant, amount from transactions where amount = 75")
    assert "SAM" not in out and "Rivera" not in out and "555" not in out
    assert "2026-09-01" in out and "75.00" in out and "[person]" in out


def test_agent_cannot_read_internal_ids(p2p_txn):
    assert run_sql("select id from transactions").startswith("SQL error")


def test_insights_have_no_internal_ids():
    assert '"ids"' not in get_insights()


def test_categorization_sends_only_redacted_numbered_lines(monkeypatch, p2p_txn):
    sent = {}

    async def fake_structured(*, user, output, **kw):
        sent["user"] = user
        return output(items=[categorize.Labeled(id=0, merchant="Zelle", category="Transfers")]), 0.0

    monkeypatch.setattr("app.llm.structured", fake_structured)
    labels, _ = asyncio.run(categorize.llm_label([("ZELLE TO SAM RIVERA 5551234567", 75.0)]))
    assert "SAM" not in sent["user"] and "555" not in sent["user"] and sent["user"].strip().endswith("(+75)")
    assert labels["ZELLE TO SAM RIVERA 5551234567"].category == "Transfers"  # mapped back by id
    with session() as s:
        logged = s.exec(select(Outbound).where(Outbound.kind == "categorize")).all()[-1].text
    assert logged == sent["user"]


def test_local_only_mode_never_calls_llm(monkeypatch):
    with session() as s:
        s.add(Txn(id="lo1", account_id="acct", date=dt.date(2026, 9, 2), amount=12.0, raw_name="SOME NEW CAFE 7731"))
        s.commit()

    async def boom(**kw):
        raise AssertionError("LLM called in local-only mode")

    monkeypatch.setattr("app.llm.structured", boom)
    monkeypatch.setattr(privacy, "load_config", lambda: {"privacy": {"mode": "local_only"}})
    stats = asyncio.run(categorize.categorize_pending())
    assert stats["llm"] == 0 and stats["left"] >= 1
    with session() as s:
        s.delete(s.get(Txn, "lo1"))
        s.commit()


def test_plaid_token_stored_in_keychain_not_db():
    ref = privacy.store_secret("item-x", "access-production-REALTOKEN")
    with session() as s:
        s.merge(PlaidItem(id="item-x", access_token=ref))
        s.commit()
        assert "REALTOKEN" not in s.get(PlaidItem, "item-x").access_token
    assert privacy.load_secret(ref) == "access-production-REALTOKEN"


def test_plaintext_tokens_migrated_on_startup():
    with session() as s:
        s.merge(PlaidItem(id="item-legacy", access_token="access-sandbox-PLAINTEXT"))
        s.commit()
    init_db()
    with session() as s:
        ref = s.get(PlaidItem, "item-legacy").access_token
    assert ref == "keychain:item-legacy" and privacy.load_secret(ref) == "access-sandbox-PLAINTEXT"
