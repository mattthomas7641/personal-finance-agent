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
def _month_label(ym: str, fmt: str) -> str:
    import datetime as dt
    try:
        return dt.date.fromisoformat(f"{ym}-01").strftime(fmt)
    except (TypeError, ValueError):
        return str(ym)


def _answer_html(text: str):
    """Agent answers are plain text with light markdown: render bullet lists, numbered lists and **bold**.
    Everything is escaped first, so model output can never inject HTML."""
    import re

    from markupsafe import Markup, escape

    out, items, kind = [], [], None

    def flush():
        nonlocal items, kind
        if items:
            out.append(f"<{kind} class='{'list-disc' if kind == 'ul' else 'list-decimal'} pl-5 space-y-1 my-2'>" + "".join(f"<li>{i}</li>" for i in items) + f"</{kind}>")
        items, kind = [], None

    for line in str(text or "").splitlines():
        esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", str(escape(line)))
        m = re.match(r"^\s*(?:[-*•]|(\d+)[.)])\s+(.*)", esc)
        if m:
            k = "ol" if m.group(1) else "ul"
            if kind and kind != k:
                flush()
            kind = k
            items.append(m.group(2))
        else:
            flush()
            out.append(f"<p class='my-2'>{esc}</p>" if esc.strip() else "")
    flush()
    return Markup("".join(out))  # nosec B704 - each line is escaped above before tags are added


templates = Jinja2Templates(directory=str(WEB / "templates"))
templates.env.filters["money"] = lambda v: "" if v is None else (f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}")
templates.env.filters["mon"] = lambda ym: _month_label(ym, "%b")            # "2026-09" -> "Sep"
templates.env.filters["mon_year"] = lambda ym: _month_label(ym, "%B %Y")    # "2026-09" -> "September 2026"
templates.env.filters["duration"] = lambda months: (f"{months // 12} yr" + (f" {months % 12} mo" if months % 12 else "")) if months >= 12 else f"{months} mo"
templates.env.filters["answer"] = lambda text: _answer_html(text)
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
        from app import fire
        fire.snapshot()  # balances may have changed
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
