"""Subscriptions: every recurring charge with its real yearly cost, next expected charge, price history,
and what you decided about it (keep / review / cancelled / not a subscription).

Detection finds most of them (see insights.recurring). For the rest, like a cloud bill with one charge so
far or a merchant whose descriptions change, you add them yourself by the text they show up as. Cancelling
one does two things: its old charges stop counting in the averages your plan uses, and any later charge
is flagged.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlmodel import select

from app.db import session
from app.models import SubscriptionDecision, Txn

BILLS = ("Rent & Mortgage", "Utilities", "Fees & Interest")  # recurring, but not subscriptions
STATUSES = ("keep", "review", "cancelled", "ignore")
CYCLES = {"weekly": (7, 52), "biweekly": (14, 26), "monthly": (30, 12), "quarterly": (91, 4), "yearly": (365, 1),
          "usage": (30, 12)}
CYCLE_LABELS = {"weekly": "weekly", "biweekly": "every 2 weeks", "monthly": "monthly", "quarterly": "quarterly",
                "yearly": "yearly", "usage": "usage-based"}


def decisions() -> dict[str, SubscriptionDecision]:
    with session() as s:
        return {d.key: d for d in s.exec(select(SubscriptionDecision)).all()}


def decide(key: str, status: str, today: date | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(status)
    with session() as s:
        d = s.get(SubscriptionDecision, key) or SubscriptionDecision(key=key)
        if d.manual and status == "ignore":
            s.delete(d)  # "not a subscription" on one you added = remove it
        else:
            d.status, d.decided_on = status, today or date.today()
            s.add(d)
        s.commit()


def add_manual(name: str, match: str, cadence: str, amount: float | None = None) -> str:
    match = match.strip().upper()
    if not match or cadence not in CYCLES:
        raise ValueError("Give the text the charge shows up as, and how often it bills.")
    key = f"manual:{match}"
    with session() as s:
        d = s.get(SubscriptionDecision, key) or SubscriptionDecision(key=key, status="keep")
        d.manual, d.name, d.match, d.cadence, d.amount = True, (name.strip() or match.title())[:80], match[:80], cadence, amount
        s.add(d)
        s.commit()
    return key


def _matches(txn: Txn, match: str) -> bool:
    return match in (txn.raw_name or "").upper() or match in (txn.merchant or "").upper()


def _charges(match: str) -> list[Txn]:
    with session() as s:
        rows = s.exec(select(Txn).where(Txn.amount > 0, Txn.pending == False)).all()  # noqa: E712
    return sorted((t for t in rows if _matches(t, match)), key=lambda t: t.date)


def belongs_to_cancelled(txns: list[Txn]) -> set[str]:
    """IDs of charges from subscriptions you've cancelled, dated on or before the cancellation.
    Planning averages leave these out: that money won't be spent again."""
    from app.categorize import normalize

    dec = [d for d in decisions().values() if d.status == "cancelled"]
    if not dec:
        return set()
    by_key = {d.key: d for d in dec if not d.manual}
    manual = [d for d in dec if d.manual]
    out = set()
    for t in txns:
        if t.amount <= 0:
            continue
        d = by_key.get(normalize(t.raw_name))
        if d and t.date <= d.decided_on:
            out.add(t.id)
            continue
        for m in manual:
            if t.date <= m.decided_on and _matches(t, m.match):
                out.add(t.id)
                break
    return out


def off_ledger_monthly() -> float:
    """Monthly cost of subscriptions you added by hand that bill somewhere Northstar can't see (a card that
    isn't connected): no matching charges at all. Plans add this to your spending so they aren't too rosy."""
    total = 0.0
    for d in decisions().values():
        if d.manual and d.status != "cancelled" and d.amount and not _charges(d.match):
            total += d.amount * CYCLES[d.cadence][1] / 12
    return round(total, 2)


def _charged_after(key: str, day: date, match: str = "") -> list[Txn]:
    from app.categorize import normalize

    with session() as s:
        rows = s.exec(select(Txn).where(Txn.date > day, Txn.amount > 0)).all()
    return [t for t in rows if (_matches(t, match) if match else normalize(t.raw_name) == key)]


def _manual_item(d: SubscriptionDecision, today: date) -> dict:
    step, per_year = CYCLES[d.cadence]
    charges = _charges(d.match)
    last = charges[-1] if charges else None
    if d.amount:
        amount = d.amount
    elif d.cadence == "usage" and charges:  # average monthly spend over the last 3 months of charges
        since = today - timedelta(days=92)
        recent = [t.amount for t in charges if t.date >= since] or [charges[-1].amount]
        amount = round(sum(recent) / 3 if len(recent) > 1 else recent[0], 2)
    else:
        amount = round(last.amount, 2) if last else 0.0
    history, prev = [], None
    for t in charges[-6:]:
        if round(t.amount, 2) != prev:
            history.append({"amount": round(t.amount, 2), "since": t.date.isoformat()})
            prev = round(t.amount, 2)
    return {"key": d.key, "merchant": d.name or d.match.title(), "category": last.category if last else "",
            "amount": amount, "cadence": CYCLE_LABELS[d.cadence], "cadence_days": step, "per_year": per_year,
            "variable": d.cadence == "usage", "history": history, "charges": len(charges),
            "first_charged": charges[0].date.isoformat() if charges else today.isoformat(),
            "last_charged": last.date.isoformat() if last else None,
            "next_expected": (last.date + timedelta(days=step)).isoformat() if last else "",
            "previous_amount": None, "price_changed_on": None, "active": True, "manual": True, "match": d.match,
            "off_ledger": not charges}


def overview(today: date | None = None) -> dict:
    from app.fire import STREAMING
    from app.insights import recurring

    today = today or date.today()
    dec = decisions()
    manual = [d for d in dec.values() if d.manual]
    items, alerts = [], []
    for r in recurring():
        if r["category"] in BILLS:
            continue
        if any(m.match in r["key"] or m.match in r["merchant"].upper() for m in manual):
            continue  # you added this one yourself; your version wins
        d = dec.get(r["key"])
        status = d.status if d else "review"
        if status == "ignore" or (not r["active"] and status != "cancelled"):
            continue
        items.append({**r, "status": status, "manual": False})
    for d in manual:
        if d.status != "ignore":
            items.append({**_manual_item(d, today), "status": d.status})

    for i in items:
        i["annual"] = round(i["amount"] * i["per_year"], 2)
        i["monthly"] = round(i["annual"] / 12, 2)
        i["streaming"] = any(k in i["merchant"].lower() for k in STREAMING)
        i["flags"] = []
        if i.get("first_charged") and (today - date.fromisoformat(i["first_charged"])).days <= 90 and i.get("charges", 0) > 1:
            i["flags"].append("New")
        if i["previous_amount"] is not None and i["amount"] > i["previous_amount"]:
            i["flags"].append("Price up")
        if i["variable"]:
            i["flags"].append("Varies")

    # Charges after a cancellation are checked straight from the transactions: an extra charge breaks the
    # billing pattern, so the recurring detector alone could miss exactly the charge that matters.
    seen = {i["key"]: i for i in items}
    for key, d in dec.items():
        if d.status != "cancelled":
            continue
        late = _charged_after(key, d.decided_on, d.match if d.manual else "")
        if late:
            t = late[-1]
            alerts.append({"merchant": d.name or t.merchant or key.title(), "amount": t.amount, "date": t.date.isoformat(),
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
    items.sort(key=lambda i: (order.get(i["status"], 3), -i["annual"]))
    return {
        "items": items, "alerts": alerts, "notes": notes,
        "monthly": round(sum(i["monthly"] for i in live), 2), "annual": round(sum(i["annual"] for i in live), 2),
        "to_review": sum(i["status"] == "review" for i in items),
        "review_annual": round(sum(i["annual"] for i in items if i["status"] == "review"), 2),
        "cancelled_annual": round(sum(i["annual"] for i in items if i["status"] == "cancelled" and "Charged after cancel" not in i["flags"]), 2),
        "upcoming": sorted((i for i in live if i["next_expected"] and i["next_expected"] >= today.isoformat()),
                           key=lambda i: i["next_expected"])[:5],
    }
