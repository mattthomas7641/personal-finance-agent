from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.config import DATA_DIR

DB_PATH = os.environ.get("LEDGER_DB", str(DATA_DIR / "ledger.db"))
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False, "timeout": 10})


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    # WAL lets the agent's read-only queries run while a sync is writing; busy_timeout waits instead of failing.
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=10000")
    cur.close()

# What the agent sees. Spending is positive, income negative (Plaid convention).
AGENT_VIEW = """
CREATE VIEW transactions AS
SELECT t.date, strftime('%Y-%m', t.date) AS month, t.amount, t.merchant, t.raw_name AS description,
       t.category, t.tag, t.pending, a.name AS account, a.type AS account_type
FROM txn t LEFT JOIN account a ON a.id = t.account_id
"""


def init_db() -> None:
    from app import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    add_missing_columns()
    with engine.begin() as c:
        c.exec_driver_sql("DROP VIEW IF EXISTS transactions")
        c.exec_driver_sql(AGENT_VIEW)
    migrate_tokens_to_keychain()
    seed_budgets_from_config()


def add_missing_columns() -> None:
    """create_all() doesn't alter existing tables; add any new model columns so older databases keep working."""
    from sqlalchemy import inspect

    insp = inspect(engine)
    with engine.begin() as c:
        for table in SQLModel.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {col["name"] for col in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    ddl = col.type.compile(engine.dialect)
                    default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                    clause = f" DEFAULT {default!r}" if isinstance(default, (str, int, float)) and not isinstance(default, bool) else (
                        f" DEFAULT {int(default)}" if isinstance(default, bool) else "")
                    c.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}{clause}')


def seed_budgets_from_config() -> None:
    from sqlmodel import select

    from app.config import load_config
    from app.models import Budget

    with session() as s:
        if s.exec(select(Budget)).first() is None:
            for cat, lim in (load_config().get("budgets") or {}).items():
                s.add(Budget(category=cat, monthly_limit=float(lim), source="config"))
            s.commit()


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
                except Exception as e:  # no keychain (e.g. headless Linux): keep working, but say so loudly
                    import logging
                    logging.getLogger(__name__).error("Could not move the Plaid token for %s into the Keychain: %s. "
                                                      "It stays in the database until this succeeds.", item.institution or item.id, e)
        s.commit()


@contextmanager
def session():
    with Session(engine, expire_on_commit=False) as s:
        yield s


QUERY_SECONDS = 3.0           # agent queries are aggregates over a few thousand rows; anything slower is a mistake
MAX_VALUE_BYTES = 1_000_000   # caps zeroblob()/printf()/group_concat() blow-ups


def readonly_conn(timeout: float = QUERY_SECONDS) -> sqlite3.Connection:
    """Connection for agent SQL: opened read-only, and an authorizer allows only reads of the
    `transactions` view (no access to plaid tokens, no writes, no ATTACH, no PRAGMA).
    A progress handler interrupts any query that runs longer than `timeout` seconds."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False, timeout=2)
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
    deadline = time.monotonic() + timeout
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
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
