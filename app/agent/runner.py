"""The finance agent: a tool-use loop on the Messages API with a verification gate.

The model can only finish by calling `final_answer`. Before that answer is accepted, the number
checker confirms every figure in it appears in a SQL/calculator result from this run. If not, the
agent gets the list of unsupported numbers back as a tool error and has to fix its answer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import traceback
from dataclasses import dataclass, field

from app.agent import tools
from app.agent.checker import check
from app.config import load_config
from app.db import session
from app.llm import client, cost, model
from app.models import AgentRun, AgentStep
from app.privacy import record

log = logging.getLogger("agent")
TASKS: set[asyncio.Task] = set()
MAX_REJECTIONS = 2

SYSTEM = """You are a personal finance analyst with read-only access to the user's transactions.

How to work:
- Start with get_schema if you haven't. Answer from data, never from assumptions.
- Use run_sql for every figure (aggregate in SQL; don't eyeball rows). Use calculate for any arithmetic
  you'd otherwise do in your head: differences, percentages, averages, annualizing.
- Use make_chart when a trend or breakdown is easier to see than read.
- get_insights returns detected subscriptions, price increases, duplicate charges and unusual charges.
- get_goals returns the user's 401(k) status (IRS limit, year-to-date, projected year-end, % needed to max out this
  year and next, employer match), savings/retirement goals with required monthly amounts, 3-month cash flow,
  where spending has room to trim, a budget plan that frees up what the goals need, the Roth IRA tracker
  (limit after income phase-out, contributed, monthly needed) and retirement (FI number, Coast FIRE number,
  projection, age you reach each). Use it for any question
  about goals, retirement, 401(k), budgets or cutting back. Explain its numbers; don't recompute them differently.
- Finish by calling final_answer. Every number in it must come from a tool result in this conversation;
  a checker rejects answers with unsupported numbers.

Answer style: lead with the direct answer, then 2-4 short supporting points. Use $ with 2 decimals for exact
amounts or whole dollars for large totals. Say which date range you used. If the data can't answer the question,
say so plainly.

Boundaries: you can't move money or change anything. You may explain spending patterns, suggest budget changes,
and say what contribution % reaches the IRS 401(k) limit or a goal's target (that's arithmetic on their numbers).
Don't recommend specific investments, funds, Roth vs pre-tax choices, or tax strategies; say a licensed advisor
can help with those. Mention that projections depend on the assumptions entered."""

TOOL_SPECS = [
    {"name": "get_schema", "description": "Describe the transactions table, sign convention, categories and date range.",
     "strict": True, "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    {"name": "run_sql", "description": "Run one read-only SQLite SELECT against `transactions`. Returns up to 200 rows.",
     "strict": True, "input_schema": {"type": "object", "properties": {"query": {"type": "string"}},
                                      "required": ["query"], "additionalProperties": False}},
    {"name": "calculate", "description": "Evaluate arithmetic, e.g. '(512.40-430.10)/430.10*100'. Supports round/abs/min/max/sum.",
     "strict": True, "input_schema": {"type": "object", "properties": {"expression": {"type": "string"}},
                                      "required": ["expression"], "additionalProperties": False}},
    {"name": "get_insights", "description": "Detected subscriptions (with price changes), duplicate charges and unusual charges.",
     "strict": True, "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    {"name": "get_goals", "description": "401(k) and Roth IRA status, retirement/FI and Coast FIRE projections, savings goals, cash flow, where to cut, and a budget plan.",
     "strict": True, "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    {"name": "make_chart", "description": "Chart a 2-column query result (label, value). kind: bar or line.",
     "strict": True, "input_schema": {"type": "object", "properties": {
         "title": {"type": "string"}, "kind": {"type": "string", "enum": ["bar", "line"]}, "query": {"type": "string"}},
         "required": ["title", "kind", "query"], "additionalProperties": False}},
    {"name": "final_answer", "description": "Submit the answer to the user. Rejected if it contains numbers not found in tool results.",
     "strict": True, "input_schema": {"type": "object", "properties": {"answer": {"type": "string"}},
                                      "required": ["answer"], "additionalProperties": False}},
]


@dataclass
class FinanceAgent:
    question: str
    history: list[dict] = field(default_factory=list)  # prior Q/A turns for follow-ups
    run_id: int = 0
    _idx: int = 0
    evidence: list[str] = field(default_factory=list)
    charts: list[dict] = field(default_factory=list)

    def step(self, kind: str, content, name: str = "") -> None:
        if not isinstance(content, str):
            content = json.dumps(content, default=str)
        with session() as s:
            s.add(AgentStep(run_id=self.run_id, idx=self._idx, kind=kind, name=name, content=content[:20000]))
            s.commit()
        self._idx += 1

    def update(self, **kw) -> None:
        with session() as s:
            run = s.get(AgentRun, self.run_id)
            for k, v in kw.items():
                setattr(run, k, v)
            s.add(run)
            s.commit()

    def create(self, parent_id: int | None = None) -> int:
        with session() as s:
            run = AgentRun(question=self.question, parent_id=parent_id)
            s.add(run)
            s.commit()
            s.refresh(run)
        self.run_id = run.id
        return run.id

    def call_tool(self, name: str, args: dict) -> tuple[str, bool]:
        if name == "get_schema":
            return tools.schema(), False
        if name == "run_sql":
            out = tools.run_sql(args["query"])
            ok = not out.startswith(("Error", "SQL error"))
            if ok:
                self.evidence.append(out)
            return out, not ok
        if name == "calculate":
            out = tools.calculate(args["expression"])
            ok = not out.startswith("Error")
            if ok:
                self.evidence.append(out.split("=")[-1])  # only the result counts as evidence
            return out, not ok
        if name == "get_insights":
            out = tools.get_insights()
            self.evidence.append(out)
            return out, False
        if name == "get_goals":
            out = tools.get_goals()
            self.evidence.append(out)
            return out, False
        if name == "make_chart":
            try:
                cols, rows = tools.query_rows(args["query"])
            except Exception as e:
                return f"Chart query failed: {e}", True
            if len(cols) < 2 or not rows:
                return "Chart query must return 2 columns (label, value) and at least one row.", True
            chart = {"title": args["title"], "kind": args["kind"], "labels": [str(r[0]) for r in rows],
                     "values": [round(float(r[1] or 0), 2) for r in rows]}
            self.charts.append(chart)
            self.evidence.append(json.dumps(chart["values"]))
            return f"Chart {len(self.charts)} created with {len(rows)} points.", False
        return f"Unknown tool {name}", True

    async def run(self) -> str:
        cfg = load_config().get("agent", {})
        max_turns, max_cost = cfg.get("max_turns", 16), cfg.get("max_cost_usd", 0.5)
        m = model("agent")
        messages: list[dict] = []
        for q, a in self.history[-4:]:
            messages += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
        messages.append({"role": "user", "content": self.question})
        spent, rejections = 0.0, 0
        self.step("question", self.question)
        record("question", self.question)
        try:
            for turn in range(1, max_turns + 1):
                resp = await client().messages.create(
                    model=m, max_tokens=16000,
                    system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                    tools=TOOL_SPECS, thinking={"type": "adaptive", "display": "summarized"},
                    output_config={"effort": "medium"}, messages=messages,
                )
                spent += cost(m, resp.usage)
                self.update(turns=turn, cost_usd=round(spent, 4))
                for b in resp.content:
                    if b.type == "thinking" and getattr(b, "thinking", ""):
                        self.step("thinking", b.thinking)
                    elif b.type == "text" and b.text.strip():
                        self.step("text", b.text)
                messages.append({"role": "assistant", "content": resp.content})
                if resp.stop_reason == "refusal":
                    raise RuntimeError("The model declined this request.")
                calls = [b for b in resp.content if b.type == "tool_use"]
                if not calls:
                    messages.append({"role": "user", "content": "Please submit your answer with final_answer."})
                    continue
                results, final = [], None
                for c in calls:
                    self.step("tool_call", c.input, name=c.name)
                    if c.name == "final_answer":
                        bad = check(c.input["answer"], self.evidence, self.question)
                        if bad and rejections < MAX_REJECTIONS:
                            rejections += 1
                            msg = (f"Rejected: these numbers don't appear in any tool result: {', '.join(bad)}. "
                                   "Compute them with run_sql/calculate, or remove them, then call final_answer again.")
                            self.step("check", msg, name="checker")
                            results.append({"type": "tool_result", "tool_use_id": c.id, "content": msg, "is_error": True})
                        else:
                            final = (c.input["answer"], not bad)
                            self.step("check", "verified ✓" if not bad else f"unverified after {MAX_REJECTIONS} retries: {', '.join(bad)}", name="checker")
                            results.append({"type": "tool_result", "tool_use_id": c.id, "content": "accepted"})
                        continue
                    out, is_err = self.call_tool(c.name, c.input)
                    self.step("tool_result", out, name=c.name)
                    record("tool_result", f"[{c.name}] {out}")
                    results.append({"type": "tool_result", "tool_use_id": c.id, "content": out, "is_error": is_err})
                messages.append({"role": "user", "content": results})
                if final:
                    answer, verified = final
                    self.step("final", answer)
                    self.update(status="done", answer=answer, verified=verified, charts=json.dumps(self.charts))
                    return answer
                if spent >= max_cost:
                    break
            msg = "I couldn't finish within the step/budget limit. Try a narrower question."
            self.update(status="stopped", answer=msg, charts=json.dumps(self.charts))
            self.step("final", msg)
            return msg
        except Exception as e:
            log.exception("run %s failed", self.run_id)
            self.step("error", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}")
            self.update(status="failed", answer=f"Failed: {e}")
            return f"Failed: {e}"


def launch(question: str, history: list[tuple[str, str]] | None = None, parent_id: int | None = None) -> int:
    agent = FinanceAgent(question, history or [])
    run_id = agent.create(parent_id)
    task = asyncio.create_task(agent.run())
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    return run_id
