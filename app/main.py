from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import delete, func, select

from app import insights
from app.agent import runner
from app.categorize import categorize_pending, correct
from app.config import CATEGORIES, NON_SPEND, load_config
from app.db import init_db, session
from app.ingest import sources
from app.models import Account, AgentRun, AgentStep, Outbound, PlaidItem, Rule, Txn
from app import privacy
from app.config import ROOT
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")
STATUS = {"message": "", "busy": False}


async def background(label: str, fn, *args):
    """Run an import/sync, then categorize whatever is new."""
    STATUS.update(busy=True, message=f"{label}…")
    try:
        res = await asyncio.to_thread(fn, *args) if not asyncio.iscoroutinefunction(fn) else await fn(*args)
        STATUS["message"] = f"{label}: {res}. Categorizing…"
        cat = await categorize_pending()
        STATUS["message"] = (f"{label} done · categorized {cat['rule'] + cat['memory'] + cat['plaid'] + cat['llm']} "
                             f"({cat['llm']} by AI, ${cat['cost_usd']})" + (f" · {cat['left']} left" if cat["left"] else ""))
    except Exception as e:
        log.exception(label)
        STATUS["message"] = f"{label} failed: {e}"
    finally:
        STATUS["busy"] = False


async def daily_sync():
    while True:
        await asyncio.sleep(6 * 3600)
        with session() as s:
            has_items = s.exec(select(PlaidItem)).first() is not None
        if has_items and not STATUS["busy"]:
            await background("Scheduled Plaid sync", sources.sync_plaid)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(daily_sync())
    yield
    task.cancel()


app = FastAPI(title="ledger-agent", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))
templates.env.filters["money"] = lambda v: "" if v is None else (f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}")


def render(request, name, **ctx):
    ctx.setdefault("status", STATUS)
    return templates.TemplateResponse(request, name, ctx)


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ---------------- dashboard ----------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, month: str = ""):
    months = insights.months()
    if not months:
        return back("/connect")
    month = month if month in months else months[0]
    summary = insights.month_summary(month)
    ins = insights.all_insights()
    # 12-month spend trend
    with session() as s:
        rows = s.exec(select(Txn.date, Txn.amount, Txn.category)).all()
    trend = defaultdict(float)
    for d, a, c in rows:
        if c not in NON_SPEND and a > 0:
            trend[d.strftime("%Y-%m")] += a
    trend_items = sorted(trend.items())[-12:]
    with session() as s:
        uncategorized = s.exec(select(func.count()).select_from(Txn).where(Txn.category == "")).one()
        recent = s.exec(select(AgentRun).order_by(AgentRun.id.desc()).limit(5)).all()
    return render(request, "dashboard.html", month=month, months=months, s=summary, ins=ins,
                  trend=trend_items, uncategorized=uncategorized, recent=recent)


@app.get("/status", response_class=HTMLResponse)
def status(request: Request):
    if not STATUS["busy"]:
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    return render(request, "_status.html")


# ---------------- transactions ----------------

@app.get("/transactions", response_class=HTMLResponse)
def transactions(request: Request, month: str = "", category: str = "", q: str = "", source: str = ""):
    with session() as s:
        stmt = select(Txn, Account.name).join(Account, Account.id == Txn.account_id, isouter=True)
        if month:
            stmt = stmt.where(func.strftime("%Y-%m", Txn.date) == month)
        if category:
            stmt = stmt.where(Txn.category == ("" if category == "Uncategorized" else category))
        if q:
            stmt = stmt.where(Txn.raw_name.ilike(f"%{q}%") | Txn.merchant.ilike(f"%{q}%"))
        if source:
            stmt = stmt.where(Txn.category_source == source)
        rows = s.exec(stmt.order_by(Txn.date.desc()).limit(500)).all()
    total = round(sum(t.amount for t, _ in rows if t.category not in NON_SPEND), 2)
    return render(request, "transactions.html", rows=rows, categories=CATEGORIES, months=insights.months(),
                  f={"month": month, "category": category, "q": q, "source": source}, total=total)


@app.post("/txn/{txn_id}/category", response_class=HTMLResponse)
def txn_category(txn_id: str, category: str = Form(...)):
    n = correct(txn_id, category)
    return HTMLResponse(f'<span class="text-xs text-emerald-700">saved · {n} similar updated · rule learned</span>')


# ---------------- ask ----------------

@app.post("/ask")
async def ask(question: str = Form(...), parent: int = Form(0)):
    history = []
    with session() as s:
        rid = parent
        while rid and len(history) < 4:
            r = s.get(AgentRun, rid)
            if not r:
                break
            history.insert(0, (r.question, r.answer))
            rid = r.parent_id
    return back(f"/runs/{runner.launch(question.strip(), history, parent or None)}")


@app.get("/runs", response_class=HTMLResponse)
def runs(request: Request):
    with session() as s:
        rs = s.exec(select(AgentRun).order_by(AgentRun.id.desc()).limit(100)).all()
    return render(request, "runs.html", runs=rs)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: int):
    with session() as s:
        run = s.get(AgentRun, run_id)
    return render(request, "run.html", run=run, charts=json.loads(run.charts or "[]"))


@app.get("/runs/{run_id}/stream")
async def run_stream(request: Request, run_id: int):
    tpl = templates.get_template("_step.html")

    async def gen():
        last, idle = -1, 0
        while not await request.is_disconnected():
            with session() as s:
                steps = s.exec(select(AgentStep).where(AgentStep.run_id == run_id, AgentStep.idx > last).order_by(AgentStep.idx)).all()
                run = s.get(AgentRun, run_id)
            for st in steps:
                last = st.idx
                yield f"event: step\ndata: {tpl.render(st=st).replace(chr(10), ' ')}\n\n"
            if run.status != "running":
                idle += 1
                if idle > 1:
                    yield "event: end\ndata: end\n\n"
                    return
            await asyncio.sleep(0.6)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------- connect / import ----------------

@app.get("/connect", response_class=HTMLResponse)
def connect(request: Request, error: str = ""):
    with session() as s:
        items = s.exec(select(PlaidItem)).all()
        accounts = s.exec(select(Account)).all()
        counts = dict(s.exec(select(Txn.account_id, func.count()).group_by(Txn.account_id)).all())
    return render(request, "connect.html", items=items, accounts=accounts, counts=counts, error=error,
                  plaid_ready=bool(os.environ.get("PLAID_CLIENT_ID") and os.environ.get("PLAID_SECRET")),
                  plaid_env=sources.plaid_env(), has_anthropic=bool(os.environ.get("ANTHROPIC_API_KEY")))


@app.post("/api/link_token")
def link_token():
    try:
        return {"link_token": sources.create_link_token()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/exchange")
async def exchange(request: Request):
    body = await request.json()
    try:
        sources.exchange_public_token(body["public_token"], body.get("institution", ""))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    asyncio.create_task(background("Plaid sync", sources.sync_plaid))
    return {"ok": True}


@app.post("/connect/sandbox")
async def connect_sandbox():
    try:
        await asyncio.to_thread(sources.connect_sandbox_bank)
    except Exception as e:
        return back(f"/connect?error={quote(str(e))}")
    asyncio.create_task(background("Plaid sync", sources.sync_plaid))
    return back("/connect")


@app.post("/connect/sync")
async def connect_sync():
    asyncio.create_task(background("Plaid sync", sources.sync_plaid))
    return back("/connect")


@app.post("/connect/csv")
async def connect_csv(file: UploadFile = File(...), account: str = Form(...), sign: str = Form("auto")):
    text = (await file.read()).decode("utf-8", errors="replace")
    spend_neg = {"auto": None, "negative": True, "positive": False}[sign]
    try:
        sources.parse_csv(text, spend_neg)  # validate before starting
    except Exception as e:
        return back(f"/connect?error={quote(str(e))}")
    asyncio.create_task(background(f"CSV import ({file.filename})", sources.import_csv, text, account, spend_neg))
    return back("/connect")


@app.post("/connect/demo")
async def connect_demo():
    asyncio.create_task(background("Demo data", sources.load_demo))
    return back("/connect")


@app.post("/connect/remove_demo")
def remove_demo():
    with session() as s:
        s.exec(delete(Txn).where(Txn.source == "demo"))
        s.exec(delete(Account).where(Account.source == "demo"))
        s.commit()
    return back("/connect")


@app.post("/connect/categorize")
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


# ---------------- privacy ----------------

@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request):
    cfg = load_config().get("privacy") or {}
    with session() as s:
        log = s.exec(select(Outbound).order_by(Outbound.id.desc()).limit(60)).all()
        sent_chars = s.exec(select(func.coalesce(func.sum(Outbound.chars), 0))).one()
        samples = s.exec(select(Txn.raw_name).where(Txn.source != "demo").distinct().limit(400)).all() or \
            s.exec(select(Txn.raw_name).distinct().limit(400)).all()
        uncategorized = s.exec(select(func.count()).select_from(Txn).where(Txn.category == "")).one()
    changed = [(r, privacy.redact(r)) for r in samples if privacy.redact(r) != " ".join(r.split())][:25]
    return render(request, "privacy.html", mode=cfg.get("mode", "standard"), terms=cfg.get("redact_terms") or [],
                  log=log, sent_chars=sent_chars, changed=changed, uncategorized=uncategorized)


@app.post("/privacy")
def privacy_save(mode: str = Form(...), terms: str = Form("")):
    path = ROOT / "config.yaml"
    cfg = yaml.safe_load(path.read_text()) if path.exists() else load_config()
    cfg["privacy"] = {"mode": "local_only" if mode == "local_only" else "standard",
                      "redact_terms": [t.strip() for t in terms.splitlines() if t.strip()]}
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return back("/privacy")


@app.post("/privacy/clear_log")
def privacy_clear():
    with session() as s:
        s.exec(delete(Outbound))
        s.commit()
    return back("/privacy")


# ---------------- rules ----------------

@app.get("/rules", response_class=HTMLResponse)
def rules(request: Request):
    with session() as s:
        rs = s.exec(select(Rule).order_by(Rule.id.desc())).all()
        by_source = dict(s.exec(select(Txn.category_source, func.count()).group_by(Txn.category_source)).all())
    return render(request, "rules.html", rules=rs, by_source=by_source, budgets=load_config().get("budgets") or {})


@app.post("/rules/{rid}/delete")
def rule_delete(rid: int):
    with session() as s:
        if r := s.get(Rule, rid):
            s.delete(r)
            s.commit()
    return back("/rules")
