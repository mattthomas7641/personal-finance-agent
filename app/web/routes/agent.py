"""Asking questions: agent runs, the weekly digest, proposals awaiting approval, run stats and trace export."""
from __future__ import annotations

import asyncio
import datetime as dt
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlmodel import func, select

from app.agent import runner
from app.config import load_config
from app.db import session
from app.models import AgentRun, AgentStep
from app.web.common import STATUS, back, render, templates

router = APIRouter()


# ---------------- ask ----------------

def spent_today() -> float:
    start = dt.datetime.combine(dt.date.today(), dt.time.min).astimezone()  # local midnight, timezone-aware
    with session() as s:
        return float(s.exec(select(func.coalesce(func.sum(AgentRun.cost_usd), 0.0)).where(AgentRun.created_at >= start)).one())


@router.post("/ask")
async def ask(question: str = Form(...), parent: int = Form(0)):
    cap = float((load_config().get("agent") or {}).get("daily_cost_usd", 2.0))
    if spent_today() >= cap:
        STATUS["message"] = (f"Today's question budget (${cap:.2f}) is used up. It resets at midnight; "
                             "change agent.daily_cost_usd in config.yaml to raise it.")
        return back("/runs")
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


def run_stats(runs: list[AgentRun]) -> dict:
    done = [r for r in runs if r.status in ("done", "stopped", "failed")]
    by_day: dict[str, float] = {}
    for r in runs:
        d = r.created_at.astimezone().date().isoformat()
        by_day[d] = by_day.get(d, 0.0) + r.cost_usd
    models: dict[str, int] = {}
    for r in runs:
        if r.model:
            key = r.model.replace("claude-", "").replace(",", " → ")
            models[key] = models.get(key, 0) + 1
    tokens_in = sum(r.input_tokens + r.cache_read_tokens for r in runs)
    return {"n": len(runs), "verified_pct": round(sum(r.verified for r in done) / len(done) * 100) if done else None,
            "total_cost": round(sum(r.cost_usd for r in runs), 4),
            "avg_cost": round(sum(r.cost_usd for r in done) / len(done), 4) if done else None,
            "avg_seconds": round(sum(r.seconds for r in done) / len(done), 1) if done else None,
            "cached_pct": round(sum(r.cache_read_tokens for r in runs) / tokens_in * 100) if tokens_in else None,
            "by_day": sorted(by_day.items())[-30:], "models": models}


def eval_scoreboard() -> dict:
    from pathlib import Path

    p = Path(__file__).resolve().parents[3] / "evals" / "results.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


@router.get("/runs", response_class=HTMLResponse)
def runs(request: Request):
    with session() as s:
        rs = s.exec(select(AgentRun).order_by(AgentRun.id.desc()).limit(200)).all()
    return render(request, "runs.html", runs=rs, stats=run_stats(rs), evals=eval_scoreboard())


@router.post("/digest")
async def digest_now():
    if spent_today() >= float((load_config().get("agent") or {}).get("daily_cost_usd", 2.0)):
        STATUS["message"] = "Today's question budget is used up. The digest can run tomorrow."
        return back("/")
    return back(f"/runs/{runner.launch(runner.DIGEST.format(day=dt.date.today().isoformat()), kind='digest')}")


@router.post("/proposals/{pid}/{action}")
def proposal_decide(pid: int, action: str):
    """Apply (or reject) a change the agent proposed. This is the only way agent suggestions change anything."""
    from app import planning
    from app.models import Proposal, RetirementPlan

    with session() as s:
        p = s.get(Proposal, pid)
        if not p or p.status != "pending" or action not in ("approve", "reject"):
            return back("/runs")
        if action == "approve":
            data = json.loads(p.payload)
            if p.kind == "budget":
                planning.apply_budgets([(data["category"], float(data["monthly_limit"]))], source="agent")
            elif p.kind == "retirement_target":
                rp = s.get(RetirementPlan, 1) or RetirementPlan(id=1)
                rp.retirement_age, rp.fire_target_low = int(data["retirement_age"]), float(data["target"])
                if rp.fire_target_high and rp.fire_target_high < rp.fire_target_low:
                    rp.fire_target_high = None
                s.add(rp)
        p.status = "approved" if action == "approve" else "rejected"
        s.add(p)
        s.commit()
        run_id = p.run_id
    return back(f"/runs/{run_id}#proposals")


@router.get("/runs/{run_id:int}", response_class=HTMLResponse)
def run_page(request: Request, run_id: int):
    with session() as s:
        run = s.get(AgentRun, run_id)
    if not run:
        return HTMLResponse("Question not found.", status_code=404)
    from app.models import Proposal
    with session() as s:
        proposals = s.exec(select(Proposal).where(Proposal.run_id == run_id).order_by(Proposal.id)).all()
    return render(request, "run.html", run=run, charts=json.loads(run.charts or "[]"),
                  proposals=[{**p.model_dump(), "data": json.loads(p.payload)} for p in proposals])


@router.get("/runs/{run_id}.json")
def run_trace(run_id: int):
    """Full trace of one question: every model step, tool call and result, checker verdict, tokens, cost, time."""
    with session() as s:
        run = s.get(AgentRun, run_id)
        if not run:
            return JSONResponse({"error": "not found"}, status_code=404)
        steps = s.exec(select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.idx)).all()
    return {**run.model_dump(mode="json"), "steps": [{"idx": st.idx, "kind": st.kind, "name": st.name, "content": st.content}
                                                     for st in steps]}


@router.get("/runs/{run_id}/stream")
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
            if run is None or run.status != "running":
                idle += 1
                if idle > 1:
                    yield "event: end\ndata: end\n\n"
                    return
            await asyncio.sleep(0.6)

    return StreamingResponse(gen(), media_type="text/event-stream")
