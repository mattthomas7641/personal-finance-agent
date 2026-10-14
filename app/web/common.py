"""Shared web plumbing: templates and filters, the background-job status line, redirects, form parsing."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.categorize import categorize_pending

log = logging.getLogger("app")
WEB = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB / "templates"))
templates.env.filters["money"] = lambda v: "" if v is None else (f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}")
templates.env.filters["money0"] = lambda v: "" if v is None else (f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}")

STATUS = {"message": "", "busy": False}


async def background(label: str, fn, *args):
    """Run an import/sync, then categorize whatever is new."""
    STATUS.update(busy=True, message=f"{label}…")
    try:
        res = await asyncio.to_thread(fn, *args) if not asyncio.iscoroutinefunction(fn) else await fn(*args)
        attention = res.get("needs_attention") if isinstance(res, dict) else None
        if isinstance(res, dict) and "added" in res:
            res = f"{res['added']} new, {res['modified']} updated, {res['removed']} removed"
        STATUS["message"] = f"{label}: {res}. Categorizing…"
        cat = await categorize_pending()
        STATUS["message"] = (f"{label} done · categorized {cat['rule'] + cat['memory'] + cat['plaid'] + cat['llm']} "
                             f"({cat['llm']} by AI, ${cat['cost_usd']})" + (f" · {cat['left']} left" if cat["left"] else "")
                             + (f" · {cat['error']}" if cat.get("error") else "")
                             + (f" · ⚠ {', '.join(attention)} needs attention (see Accounts)" if attention else ""))
    except Exception as e:
        log.exception(label)
        STATUS["message"] = f"{label} failed: {e}"
    finally:
        STATUS["busy"] = False


def render(request, name, **ctx):
    ctx.setdefault("status", STATUS)
    return templates.TemplateResponse(request, name, ctx)


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _num(v: str, default: float = 0.0) -> float:
    try:
        return float(str(v).replace("$", "").replace(",", "").replace("%", "").strip() or default)
    except ValueError:
        return default
