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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import delete, func, select

from app import insights
from app.agent import runner
from app.categorize import categorize_pending, correct
from app.config import CATEGORIES, NON_SPEND, load_config
from app.db import init_db, session
from app.ingest import sources
from app.models import Account, AgentRun, AgentStep, Budget, Goal, Outbound, PlaidItem, Plan401k, RetirementAccount, RetirementPlan, Rule, Txn
from app import retirement
from app import planning
import datetime as dt
from app import privacy, security
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


app = FastAPI(title="Northstar", lifespan=lifespan)
WEB = Path(__file__).parent / "web"
templates = Jinja2Templates(directory=str(WEB / "templates"))
app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")
PUBLIC = ("/static/", "/manifest.webmanifest", "/sw.js", "/login")


@app.middleware("http")
async def access_guard(request: Request, call_next):
    """Only this Mac and your Tailscale devices; non-local clients also need the passcode."""
    host = request.client.host if request.client else None
    kind = security.classify(host, request.headers)
    request.state.local = kind == "local"
    if kind == "denied":
        return PlainTextResponse("Forbidden: only this Mac and your Tailscale devices can open Northstar.", status_code=403)
    if security.bad_origin(request.method, request.headers):
        return PlainTextResponse("Forbidden: request from another site.", status_code=403)
    path = request.url.path
    if kind == "local" or path.startswith(PUBLIC):
        return await call_next(request)
    if not security.has_passcode():
        return HTMLResponse("<meta name=viewport content='width=device-width'><p style='font:16px system-ui;padding:24px'>"
                            "Set a passcode first: open Northstar on your Mac → Privacy → Phone access.</p>", status_code=403)
    if not security.valid_session(request.cookies.get(security.COOKIE)):
        if request.method != "GET" or path.startswith("/api/") or path.endswith("/stream"):
            return PlainTextResponse("Login required", status_code=401)
        return RedirectResponse(f"/login?next={quote(str(request.url.path))}", status_code=303)
    return await call_next(request)
templates.env.filters["money"] = lambda v: "" if v is None else (f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}")
templates.env.filters["money0"] = lambda v: "" if v is None else (f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}")


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
    try:
        goals_ov = planning.overview()
    except Exception:
        goals_ov = None
    return render(request, "dashboard.html", month=month, months=months, s=summary, ins=ins,
                  trend=trend_items, uncategorized=uncategorized, recent=recent, gov=goals_ov)


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


@app.post("/connect/{item_id}/disconnect")
async def connect_disconnect(item_id: str, keep_history: str = Form("")):
    try:
        res = await asyncio.to_thread(sources.disconnect_item, item_id, bool(keep_history))
    except Exception as e:
        return back(f"/connect?error={quote(str(e))}")
    STATUS["message"] = (f"Bank disconnected ({res['status']}). Plaid stops billing for it."
                         + (f" Deleted {res['deleted_transactions']} transactions." if res["deleted_transactions"] else " Your past transactions were kept."))
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


# ---------------- phone: login, PWA ----------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: str = ""):
    return render(request, "login.html", next=next if next.startswith("/") else "/", error=error)


@app.post("/login")
def login(request: Request, passcode: str = Form(...), next: str = Form("/")):
    client = request.client.host if request.client else "?"
    try:
        ok = security.check_passcode(passcode, client)
    except PermissionError as e:
        return back(f"/login?error={quote(str(e))}")
    if not ok:
        return back(f"/login?next={quote(next)}&error={quote('Wrong passcode')}")
    resp = back(next if next.startswith("/") and not next.startswith("//") else "/")
    resp.set_cookie(security.COOKIE, security.make_session(), max_age=security.SESSION_DAYS * 86400,
                    httponly=True, samesite="strict")
    return resp


@app.get("/logout")
def logout(request: Request):
    resp = render(request, "logout.html")
    resp.delete_cookie(security.COOKIE)
    return resp


@app.post("/security/passcode")
def set_passcode(request: Request, passcode: str = Form(...), confirm: str = Form(...)):
    if not request.state.local:
        return PlainTextResponse("Change the passcode from the Mac itself.", status_code=403)
    if passcode != confirm:
        return back(f"/privacy?error={quote('Passcodes did not match')}#phone")
    try:
        security.set_passcode(passcode)
    except ValueError as e:
        return back(f"/privacy?error={quote(str(e))}#phone")
    return back("/privacy?saved=passcode#phone")


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse({
        "name": "Northstar", "short_name": "Northstar", "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#fafafa", "theme_color": "#18181b",
        "icons": [{"src": "/static/icon-180.png", "sizes": "180x180", "type": "image/png"},
                  {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}],
    }, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return FileResponse(WEB / "static" / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


# ---------------- privacy ----------------

@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request, error: str = "", saved: str = ""):
    cfg = load_config().get("privacy") or {}
    with session() as s:
        log = s.exec(select(Outbound).order_by(Outbound.id.desc()).limit(60)).all()
        sent_chars = s.exec(select(func.coalesce(func.sum(Outbound.chars), 0))).one()
        samples = s.exec(select(Txn.raw_name).where(Txn.source != "demo").distinct().limit(400)).all() or \
            s.exec(select(Txn.raw_name).distinct().limit(400)).all()
        uncategorized = s.exec(select(func.count()).select_from(Txn).where(Txn.category == "")).one()
    changed = [(r, privacy.redact(r)) for r in samples if privacy.redact(r) != " ".join(r.split())][:25]
    return render(request, "privacy.html", mode=cfg.get("mode", "standard"), terms=cfg.get("redact_terms") or [],
                  log=log, sent_chars=sent_chars, changed=changed, uncategorized=uncategorized, error=error, saved=saved,
                  has_passcode=security.has_passcode(), is_local=request.state.local, tailscale_url=tailscale_url())


def tailscale_url() -> str:
    """https://<this-mac>.<tailnet>.ts.net if Tailscale is installed and logged in."""
    import shutil
    import subprocess
    exe = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    try:
        st = json.loads(subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=3).stdout)
        name = (st.get("Self") or {}).get("DNSName", "").rstrip(".")
        return f"https://{name}" if name else ""
    except Exception:
        return ""


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


# ---------------- goals & budgets ----------------

def _num(v: str, default: float = 0.0) -> float:
    try:
        return float(str(v).replace("$", "").replace(",", "").replace("%", "").strip() or default)
    except ValueError:
        return default


@app.get("/goals", response_class=HTMLResponse)
def goals_page(request: Request, saved: str = ""):
    with session() as s:
        plan = s.get(Plan401k, 1) or Plan401k()
        goals = s.exec(select(Goal).order_by(Goal.target_date)).all()
    ov = planning.overview()
    return render(request, "goals.html", ov=ov, plan=plan, goals=goals, saved=saved, categories=CATEGORIES,
                  sched=planning.detect_paychecks(), today=dt.date.today())


@app.post("/goals/401k")
def save_401k(annual_salary: str = Form(...), contribution_pct: str = Form(...), ytd_contributed: str = Form("0"),
              ytd_as_of: str = Form(""), match_rate_pct: str = Form("0"), match_up_to_pct: str = Form("0"),
              has_true_up: str = Form(""), birth_year: str = Form(""), pay_frequency: str = Form("auto"),
              marginal_tax_rate_pct: str = Form("22")):
    with session() as s:
        s.merge(Plan401k(
            id=1, annual_salary=_num(annual_salary), contribution_pct=_num(contribution_pct),
            ytd_contributed=_num(ytd_contributed), ytd_as_of=dt.date.fromisoformat(ytd_as_of) if ytd_as_of else None,
            match_rate_pct=_num(match_rate_pct), match_up_to_pct=_num(match_up_to_pct), has_true_up=bool(has_true_up),
            birth_year=int(birth_year) if birth_year.strip() else None, pay_frequency=pay_frequency,
            marginal_tax_rate_pct=_num(marginal_tax_rate_pct, 22)))
        s.commit()
    return back("/goals?saved=401k#k401")


@app.post("/goals/new")
def goal_new(name: str = Form(...), kind: str = Form("savings"), target_amount: str = Form(...), target_date: str = Form(...),
             current_amount: str = Form("0"), monthly_contribution: str = Form("0"), expected_return_pct: str = Form("5")):
    with session() as s:
        s.add(Goal(name=name.strip(), kind=kind if kind in ("savings", "retirement") else "savings",
                   target_amount=_num(target_amount), target_date=dt.date.fromisoformat(target_date),
                   current_amount=_num(current_amount), monthly_contribution=_num(monthly_contribution),
                   expected_return_pct=_num(expected_return_pct, 5)))
        s.commit()
    return back("/goals#goals")


@app.post("/goals/{gid}/update")
def goal_update(gid: int, current_amount: str = Form(...), monthly_contribution: str = Form(...)):
    with session() as s:
        g = s.get(Goal, gid)
        g.current_amount, g.monthly_contribution = _num(current_amount), _num(monthly_contribution)
        s.add(g)
        s.commit()
    return back("/goals#goals")


@app.post("/goals/{gid}/delete")
def goal_delete(gid: int):
    with session() as s:
        if g := s.get(Goal, gid):
            s.delete(g)
            s.commit()
    return back("/goals#goals")


@app.post("/budgets/apply_plan")
def budgets_apply_plan():
    ov = planning.overview()
    if ov["plan"]:
        planning.apply_budgets([(c["category"], c["new_budget"]) for c in ov["plan"]["cuts"]])
    return back("/goals?saved=budgets#plan")


@app.post("/budgets")
async def budgets_save(request: Request):
    form = await request.form()
    with session() as s:
        for cat in CATEGORIES:
            v = form.get(f"b_{cat}", "")
            row = s.get(Budget, cat)
            if str(v).strip() == "":
                if row:
                    s.delete(row)
            else:
                s.merge(Budget(category=cat, monthly_limit=_num(v), source="manual"))
        s.commit()
    return back("/goals?saved=budgets#budgets")


# ---------------- retirement ----------------

@app.get("/retirement", response_class=HTMLResponse)
def retirement_page(request: Request, saved: str = ""):
    with session() as s:
        plan = s.get(RetirementPlan, 1) or RetirementPlan()
        k = s.get(Plan401k, 1)
        accounts = s.exec(select(RetirementAccount).order_by(RetirementAccount.id)).all()
    return render(request, "retirement.html", ov=retirement.overview(), plan=plan, k401=k, accounts=accounts,
                  saved=saved, today=dt.date.today(), trailing=retirement.trailing_annual_spending())


@app.post("/retirement/plan")
def retirement_plan_save(birth_year: str = Form(""), retirement_age: str = Form("60"), annual_spending: str = Form(""),
                         withdrawal_rate_pct: str = Form("4"), expected_return_pct: str = Form("5"),
                         filing_status: str = Form("single"), magi: str = Form(""), roth_contributed: str = Form("0"),
                         roth_detect_pattern: str = Form(""), roth_target: str = Form(""), include_gap_in_plan: str = Form("")):
    with session() as s:
        s.merge(RetirementPlan(
            id=1, birth_year=int(birth_year) if birth_year.strip() else None,
            retirement_age=int(_num(retirement_age, 60)), annual_spending=_num(annual_spending) if annual_spending.strip() else None,
            withdrawal_rate_pct=_num(withdrawal_rate_pct, 4) or 4, expected_return_pct=_num(expected_return_pct, 5),
            filing_status=filing_status if filing_status in ("single", "mfj", "mfs") else "single",
            magi=_num(magi) if magi.strip() else None, roth_contributed=_num(roth_contributed),
            roth_detect_pattern=roth_detect_pattern.strip(), roth_target=bool(roth_target),
            include_gap_in_plan=bool(include_gap_in_plan)))
        s.commit()
    return back("/retirement?saved=1")


@app.post("/retirement/accounts")
def retirement_account_add(name: str = Form(...), kind: str = Form("other"), balance: str = Form("0"),
                           monthly_contribution: str = Form("0")):
    with session() as s:
        s.add(RetirementAccount(name=name.strip(), kind=kind, balance=_num(balance),
                                monthly_contribution=_num(monthly_contribution), as_of=dt.date.today()))
        s.commit()
    return back("/retirement#accounts")


@app.post("/retirement/accounts/{aid}")
def retirement_account_update(aid: int, balance: str = Form(...), monthly_contribution: str = Form("0"), delete: str = Form("")):
    with session() as s:
        a = s.get(RetirementAccount, aid)
        if a and delete:
            s.delete(a)
        elif a:
            a.balance, a.monthly_contribution, a.as_of = _num(balance), _num(monthly_contribution), dt.date.today()
            s.add(a)
        s.commit()
    return back("/retirement#accounts")


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
