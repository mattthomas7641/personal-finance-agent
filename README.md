# Northstar (Personal Finance Agent)

A personal finance agent you can actually trust with numbers, built to keep you pointed at your goals. Connect your banks (Plaid) or import CSVs, then ask questions in plain English — *"Why was my spending higher last month?"*, *"What am I paying for subscriptions per year?"* — and get answers where **every number is verified against your data before you see it**.

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

Run on a deterministic synthetic ledger (12 months, 744 transactions) with planted facts: a Netflix price increase, a duplicate charge, a forgotten subscription, a big one-off purchase, a travel month and a raise. Ground truth comes from SQL, not hand-written answers.

```bash
python -m evals.run checker      # free
python -m evals.run categorize   # ~$0.02
python -m evals.run qa           # 20 questions, ~$1-2
```

| Suite | Result |
|---|---|
| Number checker | **7/7** planted hallucinated numbers caught, **0/5** false positives |
| Categorization (Claude Sonnet 5) | **96%** of 744 transactions (95% of 40 unique descriptors the LLM had to label) for **$0.015** total |
| Q&A agent (Claude Opus 5.5, 20 questions) | **20/20 correct**, **20/20 verified** by the number checker, 4.4 steps and **$0.033** per question |

Plus 150 tests (`pytest`) covering: Plaid update mode (re-login flagged per bank, others keep syncing), every page rendering, Roth phase-out and FI/Coast FIRE maths, SQL-sandbox escapes, calculator injection, number-checker edge cases, the privacy layer (no names, phone numbers or account digits reach the model; local-only mode never calls it; tokens live only in the Keychain), network/passcode/CSRF rules, 401(k) limit, paycheck and match maths, budget-plan invariants, CSV formats, insights on planted facts, the agent loop (hallucinated number → rejected → fixed) against a scripted model, and an offline end-to-end test in real Chromium.

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
- **Update mode:** when a bank needs you to log in again (password change, expiring consent), Northstar flags it on the Overview and Accounts pages and **Reconnect** reopens Plaid for the same connection, keeping your history. Other banks keep syncing.

## Goals, 401(k) and budgets

- **401(k) max-out tracker.** Enter a few pay-stub numbers (salary, contribution %, year-to-date, employer match); the pay schedule is detected from your deposits. It projects your year-end total against the IRS limit (including age 50+ / 60-63 catch-up), tells you the exact % to land on the limit with the paychecks left, or the % to set from January if that's unrealistic, and warns about missed match or maxing out early without a true-up.
- **Savings and retirement goals** with required monthly amounts; retirement projections use your own return assumption and count 401(k) contributions + match (without double counting a planned 401(k) increase).
- **Roth IRA tracker.** IRS limit with age 50+ catch-up and the MAGI phase-out (proportional reduction, rounded up to $10, $200 floor), contributions entered or auto-summed from matching transfers, the April 15 deadline, and the monthly amount to max out by Dec 31.
- **Retirement and Coast FIRE.** FI number = annual spending ÷ withdrawal rate (spending defaults to your real last 12 months). Projects all accounts plus 401(k), match, Roth and other contributions to your retirement age, gives the required monthly amount and the age you reach FI. Coast FIRE number = FI ÷ (1 + return)^years left, with your progress and the age you'll reach it.
- **Budget plan.** Works out how much more you need per month, takes part from your average surplus, then trims discretionary categories (never rent, utilities, health) with a reason for each: *"up 23% vs your 12-month average; over budget 1 of the last 3 months"*. One click turns it into spending limits.
- The agent can answer *"Am I on track to max out my 401(k)?"* or *"Where should I cut back?"* from the same numbers. It won't recommend investments or tax strategies.

## On your phone

Runs on your Mac; your iPhone reaches it privately over [Tailscale](https://tailscale.com) (free) and installs it to the home screen.

- **Private network only.** The server listens on localhost; `tailscale serve --bg 8766` exposes it over HTTPS to your own devices only. Requests from anything else are refused, and DNS-rebinding / cross-site requests are blocked.
- **Passcode** for every device except the Mac itself (PBKDF2 hash, signed 30-day sessions, lockout after repeated wrong guesses). It can only be changed from the Mac.
- **Works when the Mac is off.** A service worker keeps the pages you've opened (refreshing the main ones in the background) and shows them with a *"Mac offline · data from 3h ago"* banner; actions that need the Mac are disabled. Signing out deletes the saved copies. Tested end to end in headless Chromium.
- **Auto-start:** `scripts/install-autostart.sh` runs it at login via launchd.

## Roadmap

- Credit card rewards: track points per card, earn rate by category, and which card to use where.
- Plaid Investments: read 401(k) contributions directly instead of from your pay stub.

## Security & privacy policy

See [SECURITY.md](SECURITY.md) and the [privacy policy](https://mattthomas7641.github.io/northstar/privacy.html).

## Layout

```
app/
  ingest/   sources.py (Plaid, CSV, demo)  demo.py (synthetic ledger + planted facts)
  agent/    runner.py (loop + verification gate)  tools.py (sql, calculator, charts)  checker.py
  categorize.py  insights.py  main.py (FastAPI)  web/templates/
evals/run.py  tests/
```
