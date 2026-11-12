"""Mac notifications for things that need you: a cancelled subscription that charged again, a bank that needs
you to log in, a category on pace to go over budget, a new weekly digest. Each event notifies once.
Turn off with notifications: false in config.yaml. Uses osascript, so it only does anything on macOS."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import date

from sqlmodel import select

from app.config import load_config
from app.db import session
from app.models import AgentRun, Notified, PlaidItem

log = logging.getLogger("notify")


def enabled() -> bool:
    if os.environ.get("NORTHSTAR_NO_NOTIFY"):  # tests and demos
        return False
    return sys.platform == "darwin" and bool(load_config().get("notifications", True))


def show(title: str, message: str) -> None:
    # Text goes in as arguments, never spliced into the script, so a merchant name can't inject AppleScript.
    subprocess.run(["osascript", "-e", "on run argv", "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
                    "-e", "end run", message[:200], title[:80]], capture_output=True, timeout=5,
                   env={"PATH": "/usr/bin:/bin"})


def send_once(key: str, title: str, message: str) -> bool:
    with session() as s:
        if s.get(Notified, key):
            return False
        s.add(Notified(key=key))
        s.commit()
    if enabled():
        try:
            show(title, message)
        except Exception as e:  # never let a notification break a sync
            log.warning("notification failed: %s", e)
    return True


def events(today: date | None = None) -> list[tuple[str, str, str]]:
    """(key, title, message) for everything worth a notification right now."""
    from app import insights, subscriptions

    today = today or date.today()
    out = []
    for a in subscriptions.overview(today)["alerts"]:
        out.append((f"cancel:{a['merchant']}:{a['date']}", "Charged after you cancelled",
                    f"{a['merchant']} charged ${a['amount']:,.2f} on {a['date']}."))
    with session() as s:
        for it in s.exec(select(PlaidItem).where(PlaidItem.status.in_(["login_required", "expiring"]))).all():
            out.append((f"bank:{it.id}:{it.status}", "Bank needs attention",
                        f"{it.institution or 'A bank'} " + ("needs you to log in again." if it.status == "login_required"
                                                             else "connection expires soon.")))
        d = s.exec(select(AgentRun).where(AgentRun.kind == "digest", AgentRun.status == "done")
                   .order_by(AgentRun.id.desc())).first()
    if d:
        out.append((f"digest:{d.id}", "Your weekly digest is ready", "Open Northstar to read it."))
    month = insights.month_summary(today.strftime("%Y-%m"))
    if month.get("partial"):
        for c in month["categories"]:
            if c["budget"] and c["projected"] > c["budget"]:
                out.append((f"budget:{month['month']}:{c['category']}", f"{c['category']} on pace to go over",
                            f"${c['spent']:,.0f} so far, on pace for ${c['projected']:,.0f} vs a ${c['budget']:,.0f} budget."))
    return out


def run(today: date | None = None) -> int:
    """Send anything new. Returns how many were sent."""
    return sum(send_once(*e) for e in events(today))
