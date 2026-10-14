"""Phone access (login, passcode, PWA manifest and service worker) and the privacy page."""
from __future__ import annotations

import json
import os
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from sqlmodel import delete, func, select
import yaml

from app import privacy, security
from app.config import ROOT, load_config
from app.db import session
from app.models import Outbound, Txn
from app.web.common import WEB, back, render

router = APIRouter()


# ---------------- phone: login, PWA ----------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: str = ""):
    return render(request, "login.html", next=next if next.startswith("/") else "/", error=error)


@router.post("/login")
def login(request: Request, passcode: str = Form(...), next: str = Form("/")):
    client = request.client.host if request.client else "?"
    try:
        ok = security.check_passcode(passcode, client)
    except PermissionError as e:
        return back(f"/login?error={quote(str(e))}")
    if not ok:
        return back(f"/login?next={quote(next)}&error={quote('Wrong passcode')}")
    resp = back(next if next.startswith("/") and not next.startswith("//") else "/")
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(security.COOKIE, security.make_session(), max_age=security.SESSION_DAYS * 86400,
                    httponly=True, samesite="strict", secure=https)
    return resp


@router.get("/logout")
def logout(request: Request):
    resp = render(request, "logout.html")
    resp.delete_cookie(security.COOKIE)
    return resp


@router.post("/security/passcode")
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


@router.get("/manifest.webmanifest")
def manifest():
    return JSONResponse({
        "name": "Northstar", "short_name": "Northstar", "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#fafafa", "theme_color": "#18181b",
        "icons": [{"src": "/static/icon-180.png", "sizes": "180x180", "type": "image/png"},
                  {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}],
    }, media_type="application/manifest+json")


@router.get("/sw.js")
def service_worker():
    return FileResponse(WEB / "static" / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


# ---------------- privacy ----------------

@router.get("/privacy", response_class=HTMLResponse)
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
        st = json.loads(subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=3,
                                       env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}).stdout)
        name = (st.get("Self") or {}).get("DNSName", "").rstrip(".")
        return f"https://{name}" if name else ""
    except Exception:
        return ""


@router.post("/privacy")
def privacy_save(mode: str = Form(...), terms: str = Form("")):
    path = ROOT / "config.yaml"
    cfg = yaml.safe_load(path.read_text()) if path.exists() else load_config()
    cfg["privacy"] = {"mode": "local_only" if mode == "local_only" else "standard",
                      "redact_terms": [t.strip() for t in terms.splitlines() if t.strip()]}
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return back("/privacy")


@router.post("/privacy/clear_log")
def privacy_clear():
    with session() as s:
        s.exec(delete(Outbound))
        s.commit()
    return back("/privacy")
