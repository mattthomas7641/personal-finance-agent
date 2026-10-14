# ledger-agent

A personal finance agent you can actually trust with numbers. Connect your banks (Plaid) or import CSVs, then ask questions in plain English — *"Why was my spending higher last month?"*, *"What am I paying for subscriptions per year?"* — and get answers where **every number is verified against your data before you see it**.

LLMs are bad at arithmetic and happy to make up figures. This agent can't: it answers by running read-only SQL and a calculator, and a deterministic **number checker** rejects any answer containing a figure that didn't come from a tool result. Rejected answers go back to the agent to fix.

```
 Plaid (sandbox/prod) ─┐                       ┌──────────── agent loop (Claude) ────────────┐
 CSV exports ──────────┼─► transactions ──────►│ get_schema · run_sql (read-only sandbox)     │
 demo ledger ──────────┘      │                │ calculate (safe AST) · get_insights · chart  │
                              ▼                │ final_answer ──► NUMBER CHECKER ──► user     │
              categorization cascade           │                    │ unsupported numbers?    │
   your rules → built-ins → memory → Plaid → LLM│                    └──► rejected, retry      │
                              │                └──────────────────────────────────────────────┘
                              ▼
     deterministic insights: subscriptions & price hikes · duplicates · unusual charges · budgets
```

## What makes it trustworthy

| | |
|---|---|
| **Number checker** | Every figure in an answer must match (at its stated precision, incl. `$1.2k`, `%`) a SQL or calculator result from that run. Dates and years aren't treated as claims. Unverifiable answers are rejected and retried, and labeled if they still fail. |
| **SQL sandbox** | The agent's connection is opened read-only, and a SQLite authorizer only allows reading the `transactions` view: no writes, no `ATTACH`/`PRAGMA`, and no access to the table holding Plaid tokens — even via CTEs. |
| **Safe calculator** | AST-based arithmetic only; no names, imports or attribute access. |
| **No double counting** | Built-in rules treat credit-card payments, transfers and investment moves as transfers, so paying your card isn't counted as spending twice. |
| **Corrections stick** | Fix a category once and a rule re-labels every matching transaction and applies before the AI ever sees it again. |
| **Privacy layer** | Everything sent to the API is scrubbed first: names in Zelle/Venmo/Cash App lines, emails, phone numbers, card/account digits, and any terms you list (your name, street…). Descriptors go out as numbered lines and map back locally. Amounts and dates are untouched. A **Privacy page** shows a before/after preview and a log of every character sent. |
| **Local-only mode** | Categorize with your rules, built-ins and Plaid's categories only; nothing leaves the machine unless you ask a question. |
| **Secrets** | Plaid access tokens live in the macOS Keychain, not the database; the agent's SQL can't see them or internal IDs. Data lives in `data/ledger.db` on your machine. |
| **Boundaries** | Read-only. No money movement, no investment/tax advice. |

## Cost-aware design

Categorization runs a cascade — your rules, built-in patterns, merchant memory, Plaid's own category — and only sends the remaining **unique descriptors** to Claude Sonnet in batches of 80. A year of transactions is typically one or two calls. Questions use Claude Opus 5.5 with a hard per-question budget.

## Evals

Run on a deterministic synthetic ledger (12 months, 742 transactions) with planted facts: a Netflix price increase, a duplicate charge, a forgotten subscription, a big one-off purchase, a travel month and a raise. Ground truth comes from SQL, not hand-written answers.

```bash
python -m evals.run checker      # free
python -m evals.run categorize   # ~$0.02
python -m evals.run qa           # 20 questions, ~$1-2
```

| Suite | Result |
|---|---|
| Number checker | **7/7** planted hallucinated numbers caught, **0/5** false positives |
| Categorization | run `python -m evals.run categorize` |
| Q&A (20 questions) | run `python -m evals.run qa` — accuracy, verified rate, steps and cost per question |

Plus 75 tests (`pytest`), including network/passcode/CSRF tests and an offline end-to-end test,, including privacy tests (no names/phones/digits reach the model, local-only never calls the LLM, tokens only in the keychain),: SQL sandbox escapes, calculator injection, checker edge cases, CSV formats, categorization cascade, insights on planted facts, and the agent loop (including hallucination → rejection → fix) against a scripted model.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env    # add ANTHROPIC_API_KEY, PLAID_CLIENT_ID, PLAID_SECRET
.venv/bin/uvicorn app.main:app --port 8766
```

Open http://localhost:8766 → **Accounts**:
- **Sandbox** (`PLAID_ENV=sandbox`): *Add a sandbox test bank* for realistic fake data.
- **Your real accounts** (`PLAID_ENV=production`, needs Plaid Production or trial access): *Connect a real bank* opens Plaid Link; syncs every 6 hours while running.
- **Any bank, today**: download your activity as CSV and import it.

## On your phone

Runs on your Mac; your iPhone reaches it privately over [Tailscale](https://tailscale.com) (free) and installs it to the home screen.

- **Private network only.** The server listens on localhost; `tailscale serve --bg 8766` exposes it over HTTPS to your own devices only. Requests from anything else are refused, and DNS-rebinding / cross-site requests are blocked.
- **Passcode** for every device except the Mac itself (PBKDF2 hash, signed 30-day sessions, lockout after repeated wrong guesses). It can only be changed from the Mac.
- **Works when the Mac is off.** A service worker keeps the pages you've opened (refreshing the main ones in the background) and shows them with a *"Mac offline · data from 3h ago"* banner; actions that need the Mac are disabled. Signing out deletes the saved copies. Tested end to end in headless Chromium.
- **Auto-start:** `scripts/install-autostart.sh` runs it at login via launchd.

## Layout

```
app/
  ingest/   sources.py (Plaid, CSV, demo)  demo.py (synthetic ledger + planted facts)
  agent/    runner.py (loop + verification gate)  tools.py (sql, calculator, charts)  checker.py
  categorize.py  insights.py  main.py (FastAPI)  web/templates/
evals/run.py  tests/
```
