"""Subscriptions: every recurring charge with its real yearly cost, next expected charge, price history,
and what you decided about it (keep / review / cancelled). A charge after you marked something cancelled is
flagged, because that's the one you most need to know about."""
from __future__ import annotations

from datetime import date

from sqlmodel import select

from app.db import session
from app.models import SubscriptionDecision, Txn

BILLS = ("Rent & Mortgage", "Utilities", "Fees & Interest")  # recurring, but not subscriptions
STATUSES = ("keep", "review", "cancelled")


def decisions() -> dict[str, SubscriptionDecision]:
    with session() as s:
        return {d.key: d for d in s.exec(select(SubscriptionDecision)).all()}


def decide(key: str, status: str, today: date | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(status)
    with session() as s:
        d = s.get(SubscriptionDecision, key) or SubscriptionDecision(key=key)
        d.status, d.decided_on = status, today or date.today()
        s.add(d)
        s.commit()


def _charged_after(key: str, day: date) -> list[Txn]:
    from app.categorize import normalize

    with session() as s:
        rows = s.exec(select(Txn).where(Txn.date > day, Txn.amount > 0)).all()
    return [t for t in rows if normalize(t.raw_name) == key]


def overview(today: date | None = None) -> dict:
    from app.fire import STREAMING
    from app.insights import recurring

    today = today or date.today()
    dec = decisions()
    items, alerts = [], []
    for r in recurring():
        if r["category"] in BILLS:
            continue
        d = dec.get(r["key"])
        status = d.status if d else "review"
        # Monthly billing drifts 28-31 days; treat it as exactly 12 a year so $17.99/mo shows as $215.88.
        per_year = 12 if 26 <= r["cadence_days"] <= 35 else 365 / max(r["cadence_days"], 1)
        monthly = round(r["amount"] * per_year / 12, 2)
        flags = []
        if (today - date.fromisoformat(r["first_charged"])).days <= 90:
            flags.append("New")
        if r["previous_amount"] is not None and r["amount"] > r["previous_amount"]:
            flags.append("Price up")
        streaming = any(k in r["merchant"].lower() for k in STREAMING)
        if not r["active"] and status != "cancelled":
            continue  # stopped on its own; nothing to decide
        items.append({**r, "status": status, "monthly": monthly, "annual": round(r["amount"] * per_year, 2), "flags": flags,
                      "streaming": streaming})
    # Checked straight from the transactions: an extra charge after cancelling breaks the monthly pattern,
    # so the recurring detector alone could miss exactly the charge that matters.
    seen = {i["key"]: i for i in items}
    for key, d in dec.items():
        if d.status != "cancelled":
            continue
        late = _charged_after(key, d.decided_on)
        if late:
            t = late[-1]
            alerts.append({"merchant": t.merchant or key.title(), "amount": t.amount, "date": t.date.isoformat(),
                           "cancelled_on": d.decided_on.isoformat()})
            if key in seen and "Charged after cancel" not in seen[key]["flags"]:
                seen[key]["flags"].append("Charged after cancel")
    live = [i for i in items if i["status"] != "cancelled"]
    streaming = [i for i in live if i["streaming"]]
    notes = []
    if len(streaming) >= 2:
        notes.append(f"{len(streaming)} streaming or music services cost ${sum(i['annual'] for i in streaming):,.0f} a year. "
                     "Keep one and rotate the others month to month.")
    order = {"review": 0, "keep": 1, "cancelled": 2}
    items.sort(key=lambda i: (order[i["status"]], -i["annual"]))
    return {
        "items": items, "alerts": alerts, "notes": notes,
        "monthly": round(sum(i["monthly"] for i in live), 2), "annual": round(sum(i["annual"] for i in live), 2),
        "to_review": sum(i["status"] == "review" for i in items),
        "review_annual": round(sum(i["annual"] for i in items if i["status"] == "review"), 2),
        "cancelled_annual": round(sum(i["annual"] for i in items if i["status"] == "cancelled" and "Charged after cancel" not in i["flags"]), 2),
        "upcoming": sorted((i for i in live if i["next_expected"] >= today.isoformat()), key=lambda i: i["next_expected"])[:5],
    }
