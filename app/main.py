"""Northstar web app: startup, the scheduled Plaid sync, and the access guard in front of every request.
Routes live in app/web/routes/."""
from __future__ import annotations

import asyncio
import datetime as dt
import os
from contextlib import asynccontextmanager
import logging
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import select

from app import security
from app.db import init_db, session
from app.ingest import sources
from app.models import PlaidItem
from app.web.common import STATUS, WEB, background, templates  # noqa: F401  (templates re-exported for tests)
from app.web.routes import access, accounts, agent, overview, planning

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")
PUBLIC = ("/static/", "/manifest.webmanifest", "/sw.js", "/login")


async def daily_sync(first_delay: float = 60, every: float = 6 * 3600):
    """Sync shortly after startup, then every 6 hours. Nothing in one round can stop the loop."""
    await asyncio.sleep(first_delay)
    while True:
        try:
            with session() as s:
                has_items = s.exec(select(PlaidItem)).first() is not None
            if has_items and not STATUS["busy"]:
                await background("Scheduled Plaid sync", sources.sync_plaid)
            maybe_weekly_digest()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduled sync round failed; will try again")
        await asyncio.sleep(every)


def maybe_weekly_digest(now: dt.datetime | None = None) -> int | None:
    """Start the weekly digest if the last one is 7+ days old. Skipped in local-only privacy mode, without an
    API key, without data, or when today's question budget is spent."""
    from app.agent import runner
    from app.config import load_config
    from app.models import AgentRun, Txn
    from app.privacy import local_only
    from app.web.routes.agent import spent_today

    now = now or dt.datetime.now(dt.timezone.utc)
    if local_only() or not os.environ.get("ANTHROPIC_API_KEY") or not (load_config().get("agent") or {}).get("weekly_digest", True):
        return None
    with session() as s:
        if s.exec(select(Txn)).first() is None:
            return None
        last = s.exec(select(AgentRun).where(AgentRun.kind == "digest").order_by(AgentRun.id.desc())).first()
    if last and now - last.created_at.replace(tzinfo=last.created_at.tzinfo or dt.timezone.utc) < dt.timedelta(days=7):
        return None
    if spent_today() >= float((load_config().get("agent") or {}).get("daily_cost_usd", 2.0)):
        return None
    return runner.launch(runner.DIGEST.format(day=now.date().isoformat()), kind="digest")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(daily_sync())
    yield
    task.cancel()


app = FastAPI(title="Northstar", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")
for routes in (overview, agent, accounts, access, planning):
    app.include_router(routes.router)


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
        return no_store(await call_next(request))
    if not security.has_passcode():
        return HTMLResponse("<meta name=viewport content='width=device-width'><p style='font:16px system-ui;padding:24px'>"
                            "Set a passcode first: open Northstar on your Mac → Privacy → Phone access.</p>", status_code=403)
    if not security.valid_session(request.cookies.get(security.COOKIE)):
        if request.method != "GET" or path.startswith("/api/") or path.endswith("/stream"):
            return PlainTextResponse("Login required", status_code=401)
        return RedirectResponse(f"/login?next={quote(str(request.url.path))}", status_code=303)
    return no_store(await call_next(request))


def no_store(resp):
    """Pages carry your financial data: keep them out of the browser's HTTP cache. (The phone's offline
    copies are managed separately by the service worker, which expires them after a week.)"""
    if resp.headers.get("content-type", "").startswith(("text/html", "application/json", "text/event-stream")):
        resp.headers["Cache-Control"] = "no-store"
    return resp
