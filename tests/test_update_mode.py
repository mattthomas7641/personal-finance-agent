"""Update mode: banks that need re-login are flagged, others keep syncing, and Reconnect reuses the same Item."""
import datetime as dt
import os
import sqlite3
import tempfile

os.environ["LEDGER_DATA"] = os.environ.get("LEDGER_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from plaid.exceptions import ApiException  # noqa: E402
from sqlmodel import select  # noqa: E402

from app import privacy  # noqa: E402
from app.db import init_db, session  # noqa: E402
from app.ingest import sources  # noqa: E402
from app.models import PlaidItem, Txn  # noqa: E402

init_db()


def api_error(code, status=400):
    e = ApiException(status=status, reason="err")
    e.body = '{"error_code": "%s", "error_message": "msg for %s", "display_message": null}' % (code, code)
    return e


class FakePlaid:
    """Per-token behaviour: 'ok', 'login' (item_get reports ITEM_LOGIN_REQUIRED), 'sync_fails', 'expiring', 'mutation'."""

    def __init__(self, modes):
        self.modes, self.sync_calls, self.link_requests = modes, {}, []

    def _mode(self, req):
        return self.modes[req.access_token]

    def item_get(self, req):
        mode = self._mode(req)
        err = {"error_code": "ITEM_LOGIN_REQUIRED", "error_message": "login again", "display_message": None} if mode == "login" else None
        exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=5 if mode == "expiring" else 300)
        return {"item": {"error": err, "consent_expiration_time": exp}}

    def accounts_get(self, req):
        return {"accounts": [{"account_id": f"acc-{req.access_token}", "name": "Checking", "type": "depository", "mask": "0001"}]}

    def transactions_sync(self, req):
        tok = req.access_token
        n = self.sync_calls[tok] = self.sync_calls.get(tok, 0) + 1
        mode = self._mode(req)
        if mode == "sync_fails":
            raise api_error("ITEM_LOGIN_REQUIRED")
        if mode == "mutation" and n == 1:
            raise api_error("TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION")
        return {"added": [{"transaction_id": f"t-{tok}", "account_id": f"acc-{tok}", "date": dt.date(2026, 10, 1),
                           "amount": 12.5, "name": "CAFE", "original_description": "CAFE 123", "pending": False,
                           "merchant_name": "Cafe", "personal_finance_category": None}],
                "modified": [], "removed": [], "next_cursor": f"cur-{tok}", "has_more": False}

    def link_token_create(self, req):
        self.link_requests.append(req)
        return {"link_token": "link-production-xyz"}


def make_items(**modes):
    with session() as s:
        for it in s.exec(select(PlaidItem)).all():
            s.delete(it)
        for name in modes:
            s.merge(PlaidItem(id=name, access_token=privacy.store_secret(name, f"tok-{name}"), institution=name.title()))
        s.commit()
    return FakePlaid({f"tok-{n}": m for n, m in modes.items()})


def status(item_id):
    with session() as s:
        return s.get(PlaidItem, item_id)


def test_one_bank_needing_login_does_not_stop_others(monkeypatch):
    fake = make_items(good="ok", stale="login")
    monkeypatch.setattr(sources, "plaid_client", lambda: fake)
    res = sources.sync_plaid()
    assert res["added"] == 1 and res["needs_attention"] == ["Stale"]
    assert status("good").status == "ok" and status("good").last_synced is not None and status("good").cursor == "cur-tok-good"
    st = status("stale")
    assert st.status == "login_required" and st.error_code == "ITEM_LOGIN_REQUIRED" and st.cursor == ""
    with session() as s:
        assert s.get(Txn, "t-tok-good") and not s.get(Txn, "t-tok-stale")


def test_login_error_during_sync_is_caught(monkeypatch):
    fake = make_items(flaky="sync_fails")
    monkeypatch.setattr(sources, "plaid_client", lambda: fake)
    assert sources.sync_plaid()["needs_attention"] == ["Flaky"]
    assert status("flaky").status == "login_required"


def test_expiring_consent_is_flagged_but_still_syncs(monkeypatch):
    fake = make_items(oauth="expiring")
    monkeypatch.setattr(sources, "plaid_client", lambda: fake)
    res = sources.sync_plaid()
    assert res["added"] == 1 and status("oauth").status == "expiring" and status("oauth").consent_expires is not None


def test_pagination_mutation_restarts(monkeypatch):
    fake = make_items(busy="mutation")
    monkeypatch.setattr(sources, "plaid_client", lambda: fake)
    assert sources.sync_plaid()["added"] == 1 and fake.sync_calls["tok-busy"] == 2


def test_update_mode_link_token_uses_existing_item(monkeypatch):
    fake = make_items(stale="login")
    monkeypatch.setattr(sources, "plaid_client", lambda: fake)
    assert sources.create_update_link_token("stale") == "link-production-xyz"
    req = fake.link_requests[-1]
    assert req.access_token == "tok-stale" and "products" not in req.to_dict()  # update mode: no products


def test_reconnect_clears_status_and_resyncs(monkeypatch):
    fake = make_items(stale="login")
    monkeypatch.setattr(sources, "plaid_client", lambda: fake)
    sources.sync_plaid()
    assert status("stale").status == "login_required"
    sources.mark_reconnected("stale")
    fake.modes["tok-stale"] = "ok"
    sources.sync_plaid()
    assert status("stale").status == "ok" and status("stale").error_code == ""


def test_old_database_gets_new_columns(tmp_path, monkeypatch):
    """A database created before update mode existed must keep working."""
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE plaiditem (id VARCHAR PRIMARY KEY, access_token VARCHAR, institution VARCHAR, cursor VARCHAR, env VARCHAR, created_at DATETIME)")
    con.execute("INSERT INTO plaiditem VALUES ('old', 'keychain:old', 'Old Bank', '', 'production', '2026-10-01')")
    con.commit()
    con.close()
    from sqlmodel import create_engine

    from app import db as dbmod
    monkeypatch.setattr(dbmod, "engine", create_engine(f"sqlite:///{db}"))
    dbmod.add_missing_columns()
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(plaiditem)")}
    assert {"status", "error_code", "consent_expires", "last_synced"} <= cols
    assert sqlite3.connect(db).execute("select status from plaiditem").fetchone()[0] == "ok"
