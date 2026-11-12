"""Backups and export.

Plaid can re-send transactions, but goals, subscription decisions, tags, manual accounts, balance history and
rules exist only in data/ledger.db. A copy is made once a day (SQLite's online backup API, safe while the app
is running) into data/backups/, and the newest KEEP copies are kept. The export is a zip of plain CSV/JSON you
can open anywhere; it never includes bank access tokens or the passcode.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import zipfile
from datetime import date, datetime
from pathlib import Path

from app.config import DATA_DIR
from app.db import DB_PATH

BACKUP_DIR = DATA_DIR / "backups"
KEEP = 14


def backups() -> list[Path]:
    return sorted(BACKUP_DIR.glob("ledger-*.db")) if BACKUP_DIR.exists() else []


def backup_now(today: date | None = None, force: bool = False) -> Path | None:
    """Copy the database to data/backups/ledger-YYYY-MM-DD.db (once a day unless forced); prune old copies."""
    today = today or date.today()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.chmod(0o700)
    dest = BACKUP_DIR / f"ledger-{today.isoformat()}.db"
    if dest.exists() and not force:
        return None
    tmp = dest.with_suffix(".tmp")
    src, out = sqlite3.connect(DB_PATH), sqlite3.connect(tmp)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    tmp.replace(dest)
    dest.chmod(0o600)
    for old in backups()[:-KEEP]:
        old.unlink(missing_ok=True)
    return dest


# What goes into the export: (file name, SQL). Never the plaiditem table (tokens) or auth.json.
EXPORT = [
    ("transactions.csv", "select t.date, t.amount, t.merchant, t.raw_name as description, t.category, t.tag, t.pending, "
                         "a.name as account from txn t left join account a on a.id = t.account_id order by t.date"),
    ("accounts.csv", "select name, type, mask, source, balance_current, balance_updated from account"),
    ("manual_accounts.csv", "select * from cashaccount"),
    ("investment_accounts.csv", "select name, kind, balance, monthly_contribution, as_of from retirementaccount"),
    ("goals.csv", "select * from goal"),
    ("budgets.csv", "select * from budget"),
    ("rules.csv", "select pattern, category, merchant from rule"),
    ("subscription_decisions.csv", "select * from subscriptiondecision"),
    ("balance_history.csv", "select * from balancesnapshot order by day"),
    ("card_rewards.csv", "select * from cardreward"),
]
SETTINGS = [("retirement_plan.json", "select * from retirementplan"), ("401k.json", "select * from plan401k")]


def export_zip() -> bytes:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, sql in EXPORT:
            try:
                rows = con.execute(sql).fetchall()
            except sqlite3.OperationalError:
                continue  # table not created yet
            out = io.StringIO()
            w = csv.writer(out)
            if rows:
                w.writerow(rows[0].keys())
                w.writerows(tuple(r) for r in rows)
            z.writestr(name, out.getvalue())
        for name, sql in SETTINGS:
            try:
                z.writestr(name, json.dumps([dict(r) for r in con.execute(sql).fetchall()], indent=2, default=str))
            except sqlite3.OperationalError:
                pass
        z.writestr("README.txt", f"Northstar export, {datetime.now():%Y-%m-%d %H:%M}.\n"
                                 "Plain CSV and JSON. Bank access tokens and your passcode are never included.\n")
    con.close()
    return buf.getvalue()
