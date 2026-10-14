from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

from sqlmodel import Session, SQLModel, create_engine

from app.config import DATA_DIR

DB_PATH = os.environ.get("LEDGER_DB", str(DATA_DIR / "ledger.db"))
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})

# What the agent sees. Spending is positive, income negative (Plaid convention).
AGENT_VIEW = """
CREATE VIEW transactions AS
SELECT t.date, strftime('%Y-%m', t.date) AS month, t.amount, t.merchant, t.raw_name AS description,
       t.category, t.pending, a.name AS account, a.type AS account_type
FROM txn t LEFT JOIN account a ON a.id = t.account_id
"""


def init_db() -> None:
    from app import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    with engine.begin() as c:
        c.exec_driver_sql("DROP VIEW IF EXISTS transactions")
        c.exec_driver_sql(AGENT_VIEW)
    migrate_tokens_to_keychain()


def migrate_tokens_to_keychain() -> None:
    from sqlmodel import select

    from app.models import PlaidItem
    from app.privacy import store_secret

    with session() as s:
        for item in s.exec(select(PlaidItem)).all():
            if not item.access_token.startswith("keychain:"):
                try:
                    item.access_token = store_secret(item.id, item.access_token)
                    s.add(item)
                except Exception:
                    pass  # no keychain available (e.g. headless Linux): leave as-is
        s.commit()


@contextmanager
def session():
    with Session(engine, expire_on_commit=False) as s:
        yield s


def readonly_conn() -> sqlite3.Connection:
    """Connection for agent SQL: opened read-only, and an authorizer allows only reads of the
    `transactions` view (no access to plaid tokens, no writes, no ATTACH, no PRAGMA)."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    allowed_tables = {"transactions", "txn", "account"}  # the view reads txn/account internally

    def auth(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_SELECT or action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            if arg1 in allowed_tables and (arg1 == "transactions" or source == "transactions"):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_RECURSIVE:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    conn.set_authorizer(auth)
    return conn
