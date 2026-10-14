"""Import account balances from a Fidelity "Positions" CSV download (Accounts → Positions → Download).

Everything stays on this machine: the file is parsed locally and nothing is sent to any API.
Only the last 4 digits of each account number are kept, as a stable key for re-imports.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime

from sqlmodel import select

from app.db import session
from app.models import RetirementAccount


def _money(v: str | None) -> float | None:
    v = (v or "").strip()
    if not v or v in ("--", "n/a", "N/A"):
        return None
    neg = v.startswith("-") or (v.startswith("(") and v.endswith(")"))
    v = re.sub(r"[^0-9.]", "", v)
    if not v:
        return None
    return -float(v) if neg else float(v)


def guess_kind(name: str) -> str:
    n = name.upper()
    if re.search(r"401\s*\(?K\)?|403\s*\(?B\)?|457|THRIFT|SAVINGS PLAN|RETIREMENT PLAN", n):
        return "401k"
    if "ROTH" in n:
        return "roth_ira"
    if "HSA" in n or "HEALTH SAVINGS" in n:
        return "hsa"
    if re.search(r"\bIRA\b|ROLLOVER|TRADITIONAL", n):
        return "traditional_ira"
    if re.search(r"INDIVIDUAL|JOINT|BROKERAGE|TOD\b", n):
        return "brokerage"
    return "other"


def _download_date(text: str) -> date | None:
    m = re.search(r"Date downloaded\s+([A-Za-z]{3})-(\d{1,2})-(\d{4})", text)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y").date()
        except ValueError:
            return None
    return None


def parse_fidelity_positions(text: str) -> list[dict]:
    """Returns one entry per account: number_last4, name, kind, value, holdings."""
    text = text.lstrip("﻿")
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if "current value" in l.lower() and "account" in l.lower()), None)
    if start is None:
        raise ValueError("This doesn't look like a Fidelity positions download (no 'Current Value' column found).")
    # Data rows end at the first blank line; the disclaimer follows.
    body = []
    for l in lines[start:]:
        if not l.strip():
            break
        body.append(l)
    rows = list(csv.DictReader(io.StringIO("\n".join(body))))
    accounts: dict[str, dict] = {}
    for r in rows:
        # Fidelity has changed header capitalization over time ("Current Value" vs "Current value"): match case-insensitively.
        # Trailing commas add a None key, which is skipped.
        r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if isinstance(k, str) and isinstance(v, (str, type(None)))}
        number = r.get("account number", "")
        name = r.get("account name", "")
        if not number and r.get("account name/number"):  # older export: "INDIVIDUAL - X12345678"
            combined = r["account name/number"]
            name, _, number = combined.rpartition(" ")
            name = name.rstrip(" -")
        value = _money(r.get("current value"))
        if not number or value is None:
            continue
        key = re.sub(r"\W", "", number)[-4:]
        a = accounts.setdefault(key, {"number_last4": key, "name": name or f"Account {key}", "value": 0.0, "holdings": 0,
                                      "workplace": number.strip().isdigit()})
        a["value"] += value
        if r.get("symbol") and "pending" not in r.get("symbol", "").lower():
            a["holdings"] += 1
    if not accounts:
        raise ValueError("No account balances found in this file.")
    out = []
    for a in accounts.values():
        a["value"] = round(a["value"], 2)
        a["kind"] = guess_kind(a["name"])
        # Fidelity workplace plans (NetBenefits) are often named after the employer ("PROTIVITI") and use
        # all-digit plan numbers; brokerage/IRA account numbers start with a letter (Z..., X...).
        if a["kind"] == "other" and a.pop("workplace"):
            a["kind"] = "401k"
        a.pop("workplace", None)
        out.append(a)
    return out


def import_fidelity_positions(text: str) -> dict:
    accounts = parse_fidelity_positions(text)
    as_of = _download_date(text) or date.today()
    created, updated = [], []
    with session() as s:
        for a in accounts:
            ext = f"fidelity:{a['number_last4']}"
            row = s.exec(select(RetirementAccount).where(RetirementAccount.external_id == ext)).first()
            if row:
                row.balance, row.as_of, row.holdings = a["value"], as_of, a["holdings"]
                updated.append(row.name)
            else:
                row = RetirementAccount(name=f"Fidelity {a['name'].title()} ••{a['number_last4']}", kind=a["kind"],
                                        balance=a["value"], as_of=as_of, external_id=ext, holdings=a["holdings"])
                created.append(row.name)
            s.add(row)
        s.commit()
    return {"created": created, "updated": updated, "total": round(sum(a["value"] for a in accounts), 2),
            "as_of": as_of.isoformat(), "accounts": accounts}
