"""Evals on the synthetic demo ledger (isolated temp DB; your real data is never touched).

  python -m evals.run checker      # free: number checker vs. planted hallucinations
  python -m evals.run categorize   # LLM categorization accuracy vs. ground truth (~$0.02)
  python -m evals.run qa           # agent Q&A accuracy vs. SQL-computed answers (~$1-2)
  python -m evals.run all
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

os.environ["LEDGER_DATA"] = tempfile.mkdtemp(prefix="ledger-eval-")

from app.db import init_db, session  # noqa: E402
from app.ingest import sources  # noqa: E402
from app.agent.tools import run_sql  # noqa: E402

EVALS = Path(__file__).parent

# question, ground-truth SQL (single value) or literal, kind
QA = [
    ("How much did I spend in total in June 2026, excluding transfers?",
     "select round(sum(amount),2) from transactions where month='2026-06' and category not in ('Income','Transfers')", "money"),
    ("How much did I spend on dining in March 2026?",
     "select round(sum(amount),2) from transactions where month='2026-03' and category='Dining'", "money"),
    ("What was my total income from October 2025 through September 2026?",
     "select round(-sum(amount),2) from transactions where category='Income'", "money"),
    ("How much did I spend on coffee over the whole year?",
     "select round(sum(amount),2) from transactions where category='Coffee'", "money"),
    ("Which category did I spend the most on in June 2026, other than rent?",
     "select category from transactions where month='2026-06' and category not in ('Income','Transfers','Rent & Mortgage') group by category order by sum(amount) desc limit 1", "text"),
    ("How many times did I buy coffee in August 2026?",
     "select count(*) from transactions where month='2026-08' and category='Coffee'", "count"),
    ("What is my average monthly grocery spend?",
     "select round(sum(amount)/12.0,2) from transactions where category='Groceries'", "money"),
    ("How much did Netflix cost per month before and after the price change?", "15.49|17.99", "multi"),
    ("Did I get charged twice for anything? Give the merchant and amount.", "89.99", "money"),
    ("What was my largest single shopping purchase, and how much was it?", "1899", "money"),
    ("How much did my paycheck increase after my raise, per paycheck?", "312.50", "money"),
    ("How much do I spend per year on Headspace?", "155.88", "money"),
    ("In which month was my total spending (excluding rent and transfers) highest?",
     "select month from transactions where category not in ('Income','Transfers','Rent & Mortgage') group by month order by sum(amount) desc limit 1", "text"),
    ("How much did I spend on transport in Q1 2026 (January through March)?",
     "select round(sum(amount),2) from transactions where month between '2026-01' and '2026-03' and category='Transport'", "money"),
    ("By what percent did my dining spend change from May 2026 to June 2026?",
     "select round((sum(case when month='2026-06' then amount end)-sum(case when month='2026-05' then amount end))*100.0/sum(case when month='2026-05' then amount end),1) from transactions where category='Dining'", "pct"),
    ("What did I pay in bank fees and interest over the year?",
     "select round(sum(amount),2) from transactions where category='Fees & Interest'", "money"),
    ("How much did I move to savings in total?",
     "select round(sum(amount),2) from transactions where description like 'TRANSFER TO SAVINGS%'", "money"),
    ("What's my net (income minus spending, excluding transfers) for the full year?",
     "select round(-sum(case when category='Income' then amount else 0 end) - sum(case when category not in ('Income','Transfers') then amount else 0 end),2) from transactions", "money"),
    ("How much did I spend at Trader Joe's?",
     "select round(sum(amount),2) from transactions where description like 'TRADER JOE%'", "money"),
    ("How much did my June 2026 trip cost in flights and lodging?",
     "select round(sum(amount),2) from transactions where month='2026-06' and category='Travel'", "money"),
]


def truth(spec: str) -> str:
    if spec.lower().startswith("select"):
        return run_sql(spec).split("\n")[1].strip()
    return spec


def nums(text: str) -> list[float]:
    return [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*\.?\d*", text)]


def correct(answer: str, expected: str, kind: str) -> bool:
    if kind == "text":
        return expected.lower() in answer.lower() or expected.replace("2026-", "").lower() in answer.lower() or \
            _month_name(expected).lower() in answer.lower()
    got = nums(answer)
    targets = [float(x) for x in expected.split("|")]
    tol = lambda t: 0.15 if kind == "pct" else max(0.01, abs(t) * 0.002)
    return all(any(abs(g - t) <= tol(t) or abs(g - round(t)) < 0.5 and kind == "money" and abs(t) > 1000 for g in got)
               for t in targets)


def _month_name(ym: str) -> str:
    import calendar
    try:
        y, m = ym.split("-")
        return f"{calendar.month_name[int(m)]} {y}"
    except Exception:
        return ym


def table(rows, headers):
    w = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    line = lambda r: "  ".join(str(x)[:60].ljust(n) for x, n in zip(r, w))
    return "\n".join([line(headers), line(["-" * n for n in w])] + [line(r) for r in rows])


# ---------------- checker (free) ----------------

def eval_checker() -> dict:
    """Plant numbers into otherwise-correct answers; the checker must catch each, and pass the clean ones."""
    from app.agent.checker import check

    ev = [run_sql(QA[i][1]) for i in (0, 1, 3, 6)]
    vals = [truth(QA[i][1]) for i in (0, 1, 3, 6)]
    clean = [f"You spent ${float(v):,.2f}." for v in vals] + [f"Roughly ${float(vals[0]) / 1000:.1f}k in June."]
    planted = [f"You spent ${float(v) * 1.1:,.2f}." for v in vals] + [
        f"You spent ${float(vals[0]):,.2f}, about 23% more than usual.",
        f"Across 41 purchases you spent ${float(vals[1]):,.2f}.",
        "Your dining spend averaged $512.75 per month.",
    ]
    fp = sum(bool(check(a, ev)) for a in clean)
    caught = sum(bool(check(a, ev)) for a in planted)
    res = {"caught": f"{caught}/{len(planted)}", "false_positives": f"{fp}/{len(clean)}"}
    print(f"Checker: caught {caught}/{len(planted)} planted numbers, {fp}/{len(clean)} false positives.")
    return res


# ---------------- categorization ----------------

async def eval_categorize() -> dict:
    from app.categorize import builtin_category, llm_label, normalize
    from app.ingest.demo import generate

    txns = generate()
    keys: dict[str, str] = {}
    for t in txns:
        keys.setdefault(normalize(t.raw_name), t.truth)
    llm_keys = [k for k in keys if not builtin_category(k)]
    t0 = time.time()
    labels, cost = await llm_label([k for k in llm_keys])
    wrong = []
    for k in llm_keys:
        got = labels.get(k).category if labels.get(k) else None
        if got != keys[k]:
            wrong.append((k, keys[k], got))
    builtin_ok = sum(builtin_category(k) == keys[k] for k in keys if builtin_category(k))
    by_txn = sum(1 for t in txns if (builtin_category(normalize(t.raw_name)) or
                                     (labels.get(normalize(t.raw_name)).category if labels.get(normalize(t.raw_name)) else None)) == t.truth)
    if wrong:
        print(table(wrong, ("descriptor", "truth", "llm")))
    res = {"merchant_accuracy": round(1 - len(wrong) / len(llm_keys), 3), "unique_descriptors": len(keys),
           "llm_descriptors": len(llm_keys), "builtin_correct": f"{builtin_ok}", "transaction_accuracy": round(by_txn / len(txns), 3),
           "cost_usd": round(cost, 4), "cost_per_1k_txns": round(cost / len(txns) * 1000, 4), "seconds": round(time.time() - t0, 1)}
    print(f"Categorization: {res['merchant_accuracy']:.0%} of {len(llm_keys)} LLM-labeled descriptors, "
          f"{res['transaction_accuracy']:.0%} of {len(txns)} transactions, ${res['cost_usd']} total.")
    return res


# ---------------- Q&A ----------------

async def eval_qa() -> dict:
    from app.agent.runner import FinanceAgent
    from app.models import AgentRun

    sem = asyncio.Semaphore(4)

    async def one(q, spec, kind):
        expected = truth(spec)
        async with sem:
            agent = FinanceAgent(q)
            agent.create()
            ans = await agent.run()
        with session() as s:
            run = s.get(AgentRun, agent.run_id)
        return q, expected, ans, correct(ans, expected, kind), run

    results = await asyncio.gather(*(one(*x) for x in QA))
    rows, ok, verified, cost, turns = [], 0, 0, 0.0, 0
    for q, exp, ans, good, run in results:
        ok += good
        verified += run.verified
        cost += run.cost_usd
        turns += run.turns
        rows.append((q[:48], exp[:14], "✓" if good else "✗", "✓" if run.verified else "—", run.turns, f"${run.cost_usd:.3f}"))
    print(table(rows, ("question", "expected", "ok", "verified", "steps", "cost")))
    n = len(results)
    res = {"accuracy": round(ok / n, 3), "verified_rate": round(verified / n, 3), "avg_steps": round(turns / n, 1),
           "avg_cost_usd": round(cost / n, 4), "n": n}
    print(f"\nQ&A: {ok}/{n} correct, {verified}/{n} verified by the checker, {res['avg_steps']} steps and ${res['avg_cost_usd']} per question.")
    return res


async def main(which: str):
    init_db()
    sources.load_demo(with_truth_categories=True)
    out = {}
    if which in ("checker", "all"):
        out["checker"] = eval_checker()
    if which in ("categorize", "all"):
        out["categorize"] = await eval_categorize()
    if which in ("qa", "all"):
        out["qa"] = await eval_qa()
    path = EVALS / "results.json"
    prev = json.loads(path.read_text()) if path.exists() else {}
    prev.update({k: {**v, "at": time.strftime("%Y-%m-%d %H:%M")} for k, v in out.items()})
    path.write_text(json.dumps(prev, indent=2))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "checker"))
