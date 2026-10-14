"""Getting transactions in: Plaid (sandbox or production), CSV exports, or the demo ledger."""
from __future__ import annotations

import csv
import hashlib
import io
import os
import re
from datetime import date, datetime

from sqlmodel import select

from app.categorize import from_plaid, normalize, display_name
from app.db import session
from app.models import Account, PlaidItem, Txn
from app.privacy import load_secret, store_secret

# ---------------- Plaid ----------------

SANDBOX_INSTITUTION = "ins_109508"  # "First Platypus Bank", Plaid's sandbox test institution


def plaid_client():
    import plaid
    from plaid.api import plaid_api

    cid, secret = os.environ.get("PLAID_CLIENT_ID"), os.environ.get("PLAID_SECRET")
    if not cid or not secret:
        raise RuntimeError("Set PLAID_CLIENT_ID and PLAID_SECRET in .env (dashboard.plaid.com → Developers → Keys).")
    host = plaid.Environment.Production if plaid_env() == "production" else plaid.Environment.Sandbox
    cfg = plaid.Configuration(host=host, api_key={"clientId": cid, "secret": secret})
    return plaid_api.PlaidApi(plaid.ApiClient(cfg))


def plaid_env() -> str:
    return os.environ.get("PLAID_ENV", "sandbox").lower()


def create_link_token() -> str:
    from plaid.model.country_code import CountryCode
    from plaid.model.link_token_create_request import LinkTokenCreateRequest
    from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
    from plaid.model.products import Products

    req = LinkTokenCreateRequest(
        client_name="Northstar", language="en", country_codes=[CountryCode("US")],
        user=LinkTokenCreateRequestUser(client_user_id="local-user"), products=[Products("transactions")],
    )
    return plaid_client().link_token_create(req)["link_token"]


def exchange_public_token(public_token: str, institution: str = "") -> str:
    from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest

    res = plaid_client().item_public_token_exchange(ItemPublicTokenExchangeRequest(public_token=public_token))
    with session() as s:
        s.merge(PlaidItem(id=res["item_id"], access_token=store_secret(res["item_id"], res["access_token"]),
                          institution=institution, env=plaid_env()))
        s.commit()
    return res["item_id"]


def connect_sandbox_bank() -> str:
    """Create a sandbox Item server-side (no Link UI). Plaid's 'user_transactions_dynamic'
    sandbox persona returns realistic, recurring transactions."""
    from plaid.model.products import Products
    from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
    from plaid.model.sandbox_public_token_create_request_options import SandboxPublicTokenCreateRequestOptions

    if plaid_env() != "sandbox":
        raise RuntimeError("Sandbox banks only work with PLAID_ENV=sandbox.")
    req = SandboxPublicTokenCreateRequest(
        institution_id=SANDBOX_INSTITUTION, initial_products=[Products("transactions")],
        options=SandboxPublicTokenCreateRequestOptions(override_username="user_transactions_dynamic"),
    )
    public_token = plaid_client().sandbox_public_token_create(req)["public_token"]
    return exchange_public_token(public_token, "First Platypus Bank (sandbox)")


def sync_plaid() -> dict:
    """Pull new/changed/removed transactions for every connected Item using /transactions/sync cursors."""
    from plaid.model.accounts_get_request import AccountsGetRequest
    from plaid.model.transactions_sync_request import TransactionsSyncRequest

    client = plaid_client()
    stats = {"added": 0, "modified": 0, "removed": 0, "items": 0}
    with session() as s:
        items = s.exec(select(PlaidItem)).all()
    for item in items:
        stats["items"] += 1
        for a in client.accounts_get(AccountsGetRequest(access_token=load_secret(item.access_token)))["accounts"]:
            with session() as s:
                s.merge(Account(id=a["account_id"], name=a["name"], type=str(a["type"]), mask=a.get("mask") or "",
                                source="plaid", item_id=item.id))
                s.commit()
        cursor, added, modified, removed = item.cursor, [], [], []
        while True:
            kw = {"access_token": load_secret(item.access_token)}
            if cursor:
                kw["cursor"] = cursor
            res = client.transactions_sync(TransactionsSyncRequest(**kw))
            added += res["added"]
            modified += res["modified"]
            removed += res["removed"]
            cursor = res["next_cursor"]
            if not res["has_more"]:
                break
        with session() as s:
            for t in added + modified:
                pfc = t.get("personal_finance_category")
                existing = s.get(Txn, t["transaction_id"])
                row = existing or Txn(id=t["transaction_id"], account_id=t["account_id"], date=t["date"],
                                      amount=float(t["amount"]), raw_name=t.get("original_description") or t["name"])
                row.date, row.amount, row.pending = t["date"], float(t["amount"]), bool(t["pending"])
                row.merchant = row.merchant or (t.get("merchant_name") or "")
                row.plaid_category = from_plaid(pfc.to_dict() if pfc else None) or ""
                row.source = "plaid"
                s.add(row)
            for r in removed:
                if old := s.get(Txn, r["transaction_id"]):
                    s.delete(old)
            it = s.get(PlaidItem, item.id)
            it.cursor = cursor
            s.add(it)
            s.commit()
        stats["added"] += len(added)
        stats["modified"] += len(modified)
        stats["removed"] += len(removed)
    return stats


def disconnect_item(item_id: str, keep_history: bool = True) -> dict:
    """Stop Plaid access (and billing) for one bank login via /item/remove, then forget the token.
    If Plaid can't be reached the local record is kept, so billing never silently continues."""
    import json as _json

    from plaid.exceptions import ApiException
    from plaid.model.item_remove_request import ItemRemoveRequest

    from app.privacy import delete_secret

    with session() as s:
        item = s.get(PlaidItem, item_id)
        if not item:
            raise ValueError("That bank isn't connected.")
        ref = item.access_token
    try:
        plaid_client().item_remove(ItemRemoveRequest(access_token=load_secret(ref)))
        status = "removed at Plaid"
    except ApiException as e:
        code = ""
        try:
            code = _json.loads(e.body or "{}").get("error_code", "")
        except Exception:
            pass
        if code not in ("ITEM_NOT_FOUND", "INVALID_ACCESS_TOKEN"):
            raise RuntimeError(f"Plaid didn't confirm the disconnect ({code or e.status}); nothing was deleted. Try again.") from e
        status = "already gone at Plaid"
    except RuntimeError as e:
        if "keychain" not in str(e).lower():
            raise
        status = "token was already missing"

    deleted_txns = 0
    with session() as s:
        accounts = s.exec(select(Account).where(Account.item_id == item_id)).all()
        if not keep_history:
            for a in accounts:
                for t in s.exec(select(Txn).where(Txn.account_id == a.id)).all():
                    s.delete(t)
                    deleted_txns += 1
                s.delete(a)
        s.delete(s.get(PlaidItem, item_id))
        s.commit()
    delete_secret(ref)
    return {"status": status, "deleted_transactions": deleted_txns}


# ---------------- CSV ----------------

DATE_COLS = ["date", "transaction date", "posted date", "posting date", "trans. date"]
DESC_COLS = ["description", "name", "payee", "merchant", "details", "memo"]
AMT_COLS = ["amount", "transaction amount"]
DEBIT_COLS, CREDIT_COLS = ["debit", "withdrawal", "withdrawals"], ["credit", "deposit", "deposits"]


def _find(headers: list[str], names: list[str]) -> str | None:
    low = {h.lower().strip(): h for h in headers}
    return next((low[n] for n in names if n in low), None)


def _money(v: str) -> float | None:
    v = (v or "").strip().replace("$", "").replace(",", "")
    if not v:
        return None
    neg = v.startswith("(") and v.endswith(")")
    return -float(v.strip("()")) if neg else float(v)


def _date(v: str) -> date:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(v.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"unrecognized date '{v}'")


def parse_csv(text: str, spend_is_negative: bool | None = None) -> list[dict]:
    """Handles the common bank export shapes: a signed Amount column, or Debit/Credit columns.
    Most banks export spending as negative; we flip to Plaid's convention (spend positive)."""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        return []
    h = list(rows[0].keys())
    dcol, ncol = _find(h, DATE_COLS), _find(h, DESC_COLS)
    acol, debit, credit = _find(h, AMT_COLS), _find(h, DEBIT_COLS), _find(h, CREDIT_COLS)
    if not dcol or not ncol or not (acol or debit or credit):
        raise ValueError(f"Couldn't find date/description/amount columns in: {h}")
    out = []
    for r in rows:
        if acol:
            amt = _money(r[acol])
            if amt is None:
                continue
        else:
            d, c = _money(r.get(debit, "")) or 0.0, _money(r.get(credit, "")) or 0.0
            amt = abs(d) - abs(c)  # already in spend-positive form
            out.append({"date": _date(r[dcol]), "amount": round(amt, 2), "raw_name": r[ncol].strip()})
            continue
        out.append({"date": _date(r[dcol]), "amount": amt, "raw_name": r[ncol].strip()})
    if acol:
        if spend_is_negative is None:  # infer: most rows of a card/checking export are purchases
            spend_is_negative = sum(1 for o in out if o["amount"] < 0) > len(out) / 2
        if spend_is_negative:
            for o in out:
                o["amount"] = -o["amount"]
    return out


def import_csv(text: str, account_name: str, spend_is_negative: bool | None = None) -> int:
    rows = parse_csv(text, spend_is_negative)
    acc_id = "csv_" + re.sub(r"\W+", "_", account_name.lower()).strip("_")
    n = 0
    with session() as s:
        s.merge(Account(id=acc_id, name=account_name, type="csv", source="csv"))
        for i, r in enumerate(rows):
            tid = "csv_" + hashlib.sha1(f"{acc_id}|{r['date']}|{r['amount']}|{r['raw_name']}|{i}".encode()).hexdigest()[:16]
            if not s.get(Txn, tid):
                s.add(Txn(id=tid, account_id=acc_id, date=r["date"], amount=r["amount"], raw_name=r["raw_name"], source="csv"))
                n += 1
        s.commit()
    return n


# ---------------- demo ----------------

def load_demo(with_truth_categories: bool = False) -> int:
    from app.ingest.demo import generate

    txns = generate()
    with session() as s:
        s.merge(Account(id="demo_checking", name="Everyday Checking", type="depository", mask="0042", source="demo"))
        s.merge(Account(id="demo_credit", name="Rewards Card", type="credit", mask="7781", source="demo"))
        for t in txns:
            row = Txn(id=t.id, account_id=f"demo_{t.account}", date=t.date, amount=t.amount, raw_name=t.raw_name, source="demo")
            if with_truth_categories:
                row.category, row.category_source = t.truth, "truth"
                row.merchant = display_name(normalize(t.raw_name))
            s.merge(row)
        s.commit()
    return len(txns)
