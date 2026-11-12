"""Weekly digest: code computes every number, the model only writes them up.

The first version ran the full question-answering agent (5-7 tool calls), and its dates tripped the number
checker often enough that most digests escalated to a bigger model. Here the facts are computed
deterministically, sent once (redacted), and the answer is checked against exactly those facts. One call,
usually on the cheapest model, and a digest can never contain a number that isn't in the facts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, timedelta

from sqlmodel import select

from app.agent.checker import check
from app.agent.runner import TASKS, FinanceAgent
from app.config import NON_SPEND, load_config
from app.db import session
from app.llm import client, cost, friendly_api_error, model
from app.models import Txn
from app.privacy import record, redact_obj

log = logging.getLogger("digest")

INSTRUCTIONS = """You write a short weekly money digest for one person from the JSON facts you're given.

Sections, in this order, as short bullets under plain headings: Spending, Anything new, Retirement plan, This week.
- Use only numbers that appear in the facts. Don't calculate new numbers (no differences, percentages or totals
  that aren't already there). Write money as $1,234.56 and dates as they appear in the facts.
- Skip a section's bullets if there's nothing to say, but keep "This week": one blunt, specific action.
- If data_through is earlier than the week asked about, say the latest data is from that date.
- No investment, tax or legal advice. Under 180 words."""


def facts(today: date | None = None) -> dict:
    """Everything the digest may say, computed in code."""
    from app import fire, insights, subscriptions

    today = today or date.today()
    with session() as s:
        txns = s.exec(select(Txn)).all()
    latest = max((t.date for t in txns), default=today)
    end = min(today, latest)
    week, prior = (end - timedelta(days=6), end), (end - timedelta(days=13), end - timedelta(days=7))

    def spent(lo, hi):
        rows = [t for t in txns if lo <= t.date <= hi and t.amount > 0 and t.category not in NON_SPEND]
        return round(sum(t.amount for t in rows), 2), rows

    this_total, this_rows = spent(*week)
    prior_total, _ = spent(*prior)
    by_cat: dict[str, float] = {}
    for t in this_rows:
        by_cat[t.category or "Uncategorized"] = by_cat.get(t.category or "Uncategorized", 0) + t.amount
    month = insights.month_summary(end.strftime("%Y-%m"))
    pace = [{"category": c["category"], "spent_so_far": c["spent"], "on_pace_for": c["projected"], "budget": c["budget"]}
            for c in month["categories"] if c["budget"] and c["projected"] > c["budget"]]
    subs = subscriptions.overview(today)
    new = [{"merchant": i["merchant"], "amount": i["amount"], "flags": i["flags"]}
           for i in subs["items"] if {"New", "Price up"} & set(i["flags"])]
    alerts = [{"kind": a["kind"], "merchant": a["merchant"], "detail": a["text"]} for a in insights.alerts()]
    out = {
        "week": {"from": week[0].isoformat(), "to": week[1].isoformat()}, "data_through": latest.isoformat(),
        "spending": {"this_week": this_total, "previous_week": prior_total,
                     "change_vs_previous_week": round(this_total - prior_total, 2),
                     "top_categories_this_week": [{"category": k, "amount": round(v, 2)}
                                                  for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])[:3]],
                     "month_to_date": month["spend"], "categories_on_pace_to_go_over_budget": pace},
        "new_or_changed_subscriptions": new,
        "charged_after_cancelling": subs["alerts"],
        "needs_a_look": alerts,
    }
    p = fire.plan(today)
    if p and not p.get("needs") and p.get("targets"):
        t0 = p["targets"][0]
        ms = t0["milestones"][0] if t0["milestones"] else None
        out["retirement_plan"] = {
            "target": t0["target"], "retire_at_age": p["target_age"], "invested_now": p["invested"],
            "should_have_by_age": ms["age"] if ms else None, "should_have_amount": ms["need"] if ms else None,
            "behind_by": round(max(0, ms["need"] - p["invested"]), 2) if ms else None,
            "needs_invested_per_month": t0["required_monthly"], "investing_now_per_month": t0["investing_now"],
            "verdict": t0["rating"],
            "biggest_cut": ({"category": p["harsh_cuts"][0]["category"], "from": p["harsh_cuts"][0]["avg_month"],
                             "to": p["harsh_cuts"][0]["target"]} if p["harsh_cuts"] else None),
        }
    return out


async def write(today: date | None = None, run_id: int | None = None) -> str:
    """Compute facts, have the model write them up, check every number; one retry, then the backup model."""
    today = today or date.today()
    agent = FinanceAgent(f"Weekly digest, week ending {today.isoformat()}")
    if run_id:
        agent.run_id = run_id
    else:
        agent.create(kind="digest")
    started, spent, used = time.monotonic(), 0.0, []
    try:
        data = redact_obj(facts(today))
        payload = json.dumps(data, default=str)
        agent.step("tool_result", payload, name="facts")
        record("digest", payload)
        evidence = [payload]
        messages = [{"role": "user", "content": "Facts:\n" + payload}]
        tiers = [model("agent"), model("agent"), model("agent_escalate") or model("agent")]
        for attempt, m in enumerate(tiers):
            used.append(m)
            resp = await client().messages.create(
                model=m, max_tokens=4000, system=INSTRUCTIONS, output_config={"effort": "low"}, messages=messages)
            spent += cost(m, resp.usage)
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            bad = check(text, evidence)
            agent.step("text", text, name=m)
            agent.update(cost_usd=round(spent, 4), turns=attempt + 1, model=",".join(dict.fromkeys(used)),
                         seconds=round(time.monotonic() - started, 1))
            if not bad:
                agent.step("check", "verified ✓", name="checker")
                agent.step("final", text)
                agent.update(status="done", answer=text, verified=True)
                return text
            msg = f"These numbers aren't in the facts: {', '.join(bad)}. Rewrite using only numbers from the facts."
            agent.step("check", "Rejected: " + msg, name="checker")
            messages += [{"role": "assistant", "content": resp.content}, {"role": "user", "content": msg}]
        agent.step("final", text)
        agent.update(status="done", answer=text, verified=False)
        return text
    except Exception as e:
        import anthropic
        log.exception("digest failed")
        msg = friendly_api_error(e) if isinstance(e, anthropic.APIError) else f"Something went wrong: {e}"
        agent.update(status="failed", answer=msg, cost_usd=round(spent, 4))
        return msg


def launch(today: date | None = None) -> int:
    """Start a digest in the background; returns the run id to link to."""
    agent = FinanceAgent(f"Weekly digest, week ending {(today or date.today()).isoformat()}")
    run_id = agent.create(kind="digest")
    task = asyncio.create_task(write(today, run_id))
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    return run_id


def enabled() -> bool:
    return bool((load_config().get("agent") or {}).get("weekly_digest", True))
