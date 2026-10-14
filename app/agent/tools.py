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
    except sqlite3.Error as e:
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
  id TEXT, date TEXT 'YYYY-MM-DD', month TEXT 'YYYY-MM', amount REAL, merchant TEXT, description TEXT (raw bank text),
  category TEXT, pending INTEGER, account TEXT, account_type TEXT
Sign convention: amount > 0 = money out (spending), amount < 0 = money in (income, refunds).
Categories: {", ".join(CATEGORIES)}. Exclude {sorted(NON_SPEND)} when computing "spending".
Data covers {lo} to {hi} ({n} transactions). Accounts: {", ".join(redact(a) for a in accts if a)}.
Text fields are privacy-redacted: [person], [phone], [acct], [email] and # replace personal details.
The latest month may be partial."""


def get_goals() -> str:
    from app.planning import overview

    return json.dumps(redact_obj(overview()), default=str)


def get_insights() -> str:
    ins = insights.all_insights()
    for d in ins["duplicates"]:
        d.pop("ids", None)
    return json.dumps(redact_obj(ins), default=str)
