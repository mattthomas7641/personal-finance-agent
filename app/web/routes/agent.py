"""Asking questions: launching agent runs, the run page, live trace stream and JSON trace export."""
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


@router.get("/runs", response_class=HTMLResponse)
def runs(request: Request):
    with session() as s:
        rs = s.exec(select(AgentRun).order_by(AgentRun.id.desc()).limit(100)).all()
    return render(request, "runs.html", runs=rs)


@router.get("/runs/{run_id:int}", response_class=HTMLResponse)
def run_page(request: Request, run_id: int):
    with session() as s:
        run = s.get(AgentRun, run_id)
    if not run:
        return HTMLResponse("Question not found.", status_code=404)
    return render(request, "run.html", run=run, charts=json.loads(run.charts or "[]"))


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
