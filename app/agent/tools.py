"""Tools the finance agent can call. All read-only."""
from __future__ import annotations

import ast
import json
import operator
import re
import sqlite3

from app import insights
from app.config import CATEGORIES, NON_SPEND
from app.db import readonly_conn
from app.privacy import redact, redact_obj

MAX_ROWS = 200


def run_sql(query: str) -> str:
    q = query.strip().rstrip(";")
    if not q.lower().startswith(("select", "with")):
        return "Error: only SELECT queries are allowed."
    if ";" in q:
        return "Error: one statement at a time."
    conn = readonly_conn()
    try:
        cur = conn.execute(q)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(MAX_ROWS + 1)
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            return "SQL error: query took too long and was stopped. Aggregate with GROUP BY instead of joining tables to themselves."
        return f"SQL error: {e}"
    except (sqlite3.Error, OverflowError, MemoryError) as e:
        return f"SQL error: {e}"
    finally:
        conn.close()
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    # Numbers pass through untouched; text cells (descriptions, merchants) are redacted before
    # they can reach the model.
    fmt = lambda v: f"{v:.2f}" if isinstance(v, float) else ("" if v is None else (str(v) if isinstance(v, int) else redact(str(v))))
    lines = [" | ".join(cols)] + [" | ".join(fmt(v) for v in r) for r in rows]
    if truncated:
        lines.append(f"... truncated to {MAX_ROWS} rows; aggregate in SQL instead.")
    return "\n".join(lines) if rows else " | ".join(cols) + "\n(no rows)"


def result_rows(out: str) -> str:
    """The data part of a run_sql result. The header holds the model's own column expressions
    (e.g. "sum(amount)/12.0"), and numbers the model typed itself are not evidence."""
    return out.split("\n", 1)[1] if "\n" in out else ""


def query_rows(query: str) -> tuple[list[str], list[tuple]]:
    conn = readonly_conn()
    try:
        cur = conn.execute(query.strip().rstrip(";"))
        return [d[0] for d in cur.description], [tuple(redact(v) if isinstance(v, str) else v for v in r) for r in cur.fetchmany(MAX_ROWS)]
    finally:
        conn.close()


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Mod: operator.mod}
_FUNCS = {"round": round, "abs": abs, "min": min, "max": max, "sum": lambda *a: sum(a)}


def calculate(expression: str) -> str:
    """Safe arithmetic: numbers, + - * / ** %, parentheses, round/abs/min/max/sum. No names, no calls beyond those."""
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            left, right = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(right) > 64:
                raise ValueError("exponent too large")
            return _OPS[type(n.op)](left, right)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ValueError("unsupported expression")
    try:
        v = ev(ast.parse(re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", expression), mode="eval"))  # 1,234.5 -> 1234.5
    except Exception as e:
        return f"Error: {e}"
    return f"{expression} = {round(v, 6)}"


def schema() -> str:
    conn = readonly_conn()
    try:
        lo, hi, n = conn.execute("select min(date), max(date), count(*) from transactions").fetchone()
        accts = [r[0] for r in conn.execute("select distinct account from transactions")]
    finally:
        conn.close()
    return f"""Table `transactions` (read-only):
  date TEXT 'YYYY-MM-DD', month TEXT 'YYYY-MM', amount REAL, merchant TEXT, description TEXT (raw bank text),
  category TEXT, tag TEXT ('' | 'one_off' | 'bad_investment', set by the user), pending INTEGER, account TEXT, account_type TEXT
Sign convention: amount > 0 = money out (spending), amount < 0 = money in (income, refunds).
Tagged one-offs and bad investments are real spending, but leave them out of "typical" or average monthly spending.
Categories: {", ".join(CATEGORIES)}. Exclude {sorted(NON_SPEND)} when computing "spending".
Data covers {lo} to {hi} ({n} transactions). Accounts: {", ".join(redact(a) for a in accts if a)}.
Text fields are privacy-redacted: [person], [phone], [acct], [email] and # replace personal details.
The latest month may be partial."""


def _compact_plan(p: dict) -> dict:
    """The page needs year-by-year milestones and every category; the model doesn't. Smaller results are
    cheaper on every later turn, because each turn re-sends the whole conversation."""
    p = dict(p)
    for k in ("all_cuts", "cash_flow"):
        p.pop(k, None)
    p["targets"] = [{**t, "milestones": [m for m in t.get("milestones", []) if m is t["milestones"][-1] or m["age"] % 5 == 0]}
                    for t in p.get("targets", [])]
    if "subscriptions" in p:
        p["subscriptions"] = {**p["subscriptions"], "items": [{k: i[k] for k in ("merchant", "amount", "annual_cost", "flags") if k in i}
                                                             for i in p["subscriptions"].get("items", [])]}
    return p


def _compact_goals(ov: dict) -> dict:
    ov = dict(ov)
    # "plan" is the older goals-based cut list; spending cuts now come only from get_plan, so the agent
    # can't quote two different limits for the same category.
    for k in ("cash_accounts", "budgets", "disclaimer", "today", "plan"):
        ov.pop(k, None)
    if ov.get("k401"):
        ov["k401"] = {k: v for k, v in ov["k401"].items() if k not in ("pay_schedule", "notes")}
    if ov.get("retirement"):
        ov["retirement"] = {k: v for k, v in ov["retirement"].items() if k != "accounts"}
    keep = ("category", "avg_last_3_months", "reasons")
    ov["where_to_cut"] = [{k: c[k] for k in keep if k in c} for c in ov.get("where_to_cut", [])]
    return ov


def get_plan() -> str:
    from app.fire import plan

    return json.dumps(redact_obj(_compact_plan(plan() or {})), default=str)


def get_goals() -> str:
    from app.planning import overview

    return json.dumps(redact_obj(_compact_goals(overview())), default=str)


def get_insights() -> str:
    ins = insights.all_insights()
    live = {a["key"] for a in insights.alerts(ins)}  # skip what the user already dismissed
    ins["duplicates"] = [d for d in ins["duplicates"] if f"dup:{':'.join(d['ids'])}" in live]
    ins["unusual"] = [u for u in ins["unusual"] if f"big:{u['id']}" in live]
    for d in ins["duplicates"]:
        d.pop("ids", None)
    for u in ins["unusual"]:
        u.pop("id", None)
    for r in ins["subscriptions"]:
        for k in ("key", "history"):
            r.pop(k, None)
    return json.dumps(redact_obj(ins), default=str)
