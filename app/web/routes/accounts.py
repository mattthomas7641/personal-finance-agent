"""Getting data in: Plaid Link (incl. update mode), manual accounts, CSV import, demo data, categorization."""
from __future__ import annotations

import asyncio
import datetime as dt
import os
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from sqlmodel import delete, func, select

from app import cash
from app.categorize import categorize_pending
from app.db import session
from app.ingest import sources
from app.models import Account, CashAccount, Goal, PlaidItem, Txn
from app.web.common import STATUS, _num, back, background, render

router = APIRouter()


# ---------------- connect / import ----------------

@router.get("/connect", response_class=HTMLResponse)
def connect(request: Request, error: str = ""):
    with session() as s:
        items = s.exec(select(PlaidItem)).all()
        accounts = s.exec(select(Account)).all()
        counts = dict(s.exec(select(Txn.account_id, func.count()).group_by(Txn.account_id)).all())
    return render(request, "connect.html", items=items, accounts=accounts, counts=counts, error=error, cash=cash.summary(),
                  plaid_ready=bool(os.environ.get("PLAID_CLIENT_ID") and os.environ.get("PLAID_SECRET")),
                  plaid_env=sources.plaid_env(), has_anthropic=bool(os.environ.get("ANTHROPIC_API_KEY")))


@router.post("/api/link_token")
def link_token(item_id: str = ""):
    try:
        if item_id:  # update mode: log in again to an existing bank
            return {"link_token": sources.create_update_link_token(item_id)}
        return {"link_token": sources.create_link_token()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/api/exchange")
async def exchange(request: Request):
    body = await request.json()
    try:
        sources.exchange_public_token(body["public_token"], body.get("institution", ""))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    asyncio.create_task(background("Plaid sync", sources.sync_plaid))
    return {"ok": True}


@router.post("/api/reconnected/{item_id}")
async def reconnected(item_id: str):
    sources.mark_reconnected(item_id)
    asyncio.create_task(background("Plaid sync", sources.sync_plaid))
    return {"ok": True}


@router.post("/connect/sandbox")
async def connect_sandbox():
    try:
        await asyncio.to_thread(sources.connect_sandbox_bank)
    except Exception as e:
        return back(f"/connect?error={quote(str(e))}")
    asyncio.create_task(background("Plaid sync", sources.sync_plaid))
    return back("/connect")


@router.post("/connect/{item_id}/disconnect")
async def connect_disconnect(item_id: str, keep_history: str = Form("")):
    try:
        res = await asyncio.to_thread(sources.disconnect_item, item_id, bool(keep_history))
    except Exception as e:
        return back(f"/connect?error={quote(str(e))}")
    STATUS["message"] = (f"Bank disconnected ({res['status']}). Plaid stops billing for it."
                         + (f" Deleted {res['deleted_transactions']} transactions." if res["deleted_transactions"] else " Your past transactions were kept."))
    return back("/connect")


@router.post("/accounts/manual")
def manual_add(name: str = Form(...), kind: str = Form("savings"), balance: str = Form("0"), apy_pct: str = Form("0"),
               detect_pattern: str = Form("")):
    with session() as s:
        s.add(CashAccount(name=name.strip(), kind=kind, balance=_num(balance), apy_pct=_num(apy_pct),
                          as_of=dt.date.today(), detect_pattern=detect_pattern.strip()))
        s.commit()
    return back("/connect#manual")


@router.post("/accounts/manual/{aid}")
def manual_update(aid: int, balance: str = Form(...), apy_pct: str = Form("0"), detect_pattern: str = Form(""),
                  delete: str = Form("")):
    with session() as s:
        a = s.get(CashAccount, aid)
        if a and delete:
            for g in s.exec(select(Goal).where(Goal.account_id == aid)).all():
                g.account_id = None
                s.add(g)
            s.delete(a)
        elif a:
            a.balance, a.apy_pct, a.detect_pattern, a.as_of = _num(balance), _num(apy_pct), detect_pattern.strip(), dt.date.today()
            s.add(a)
        s.commit()
    return back("/connect#manual")


@router.post("/connect/sync")
async def connect_sync():
    asyncio.create_task(background("Plaid sync", sources.sync_plaid))
    return back("/connect")


@router.post("/connect/csv")
async def connect_csv(file: UploadFile = File(...), account: str = Form(...), sign: str = Form("auto")):
    raw = await file.read()
    if len(raw) > 20_000_000:
        return back(f"/connect?error={quote('That file is too large (20 MB max).')}")
    text = raw.decode("utf-8", errors="replace").replace("\x00", "")
    spend_neg = {"auto": None, "negative": True, "positive": False}[sign]
    try:
        sources.parse_csv(text, spend_neg)  # validate before starting
    except Exception as e:
        return back(f"/connect?error={quote(str(e))}")
    asyncio.create_task(background(f"CSV import ({file.filename})", sources.import_csv, text, account, spend_neg))
    return back("/connect")


@router.post("/connect/demo")
async def connect_demo():
    asyncio.create_task(background("Demo data", sources.load_demo))
    return back("/connect")


@router.post("/connect/remove_demo")
def remove_demo():
    with session() as s:
        s.exec(delete(Txn).where(Txn.source == "demo"))
        s.exec(delete(Account).where(Account.source == "demo"))
        s.commit()
    return back("/connect")


@router.post("/connect/categorize")
async def recategorize(ai: int = Form(0)):
    async def run():
        STATUS.update(busy=True, message="Categorizing…")
        try:
            st = await categorize_pending(use_llm=True if ai else None)
            STATUS["message"] = f"Categorized {st['rule'] + st['memory'] + st['plaid'] + st['llm']} ({st['llm']} by AI)" + \
                (f" · {st['left']} left for you to categorize" if st["left"] else "")
        except Exception as e:
            STATUS["message"] = f"Categorize failed: {e}"
        finally:
            STATUS["busy"] = False
    asyncio.create_task(run())
    return back("/privacy" if ai else "/connect")
