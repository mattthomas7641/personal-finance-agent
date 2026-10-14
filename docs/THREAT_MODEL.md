# Threat model

**Scope:** Northstar running on the owner's Mac, reachable from the owner's phone over Tailscale.
**Last reviewed:** 2026-10-14.

## Assets

| Asset | Where |
|---|---|
| Transactions, balances, salary, goals | `data/ledger.db` (directory mode 0700, FileVault) |
| Plaid access tokens | macOS Keychain; the database holds only a reference |
| Anthropic and Plaid API keys | `.env` (mode 0600, git-ignored) |
| Passcode hash and session secret | `data/auth.json` (0600) |
| Phone session | HttpOnly, SameSite=Strict, Secure cookie (30 days) |
| The public repository | GitHub; must never contain personal data |

## Trust boundaries

1. **Browser on the Mac**: trusted, no passcode. Anything running as the owner's macOS user is trusted.
2. **Owner's devices over Tailscale**: HTTPS via `tailscale serve`, then the passcode.
3. **Everything else on the network**: HTTP 403.
4. **Data from banks, merchants and files**: untrusted input that reaches the model.
5. **Anthropic API**: receives redacted descriptors, query results and planning figures. Every byte sent is logged locally.
6. **GitHub**: receives only what is committed.

## Attacker stories and controls

| # | Attacker / scenario | Impact | Controls | Where |
|---|---|---|---|---|
| 1 | A web page open on the Mac submits forms to `localhost` (CSRF) | Spend API money, disconnect a bank, change privacy settings, inject transactions | Writes need a same-origin `Origin` (a `null` Origin is refused) and a same-origin `Sec-Fetch-Site`; Host allow-list stops DNS rebinding | `app/security.py` `bad_origin`, `tests/test_hardening.py` |
| 2 | Someone on the same Wi-Fi or the internet | Read financial data | Server listens on localhost; non-tailnet clients get 403 | `app/security.py` `classify` |
| 3 | A tailnet device that isn't the owner (shared or tagged node), or a request arriving through the proxy without identity headers | Read data without the passcode | Any proxy marker (`X-Forwarded-*`, Tailscale headers, `*.ts.net` Host) makes a request remote, so it needs the passcode | `via_tailscale_serve` |
| 4 | Lost or stolen phone | Read cached pages | Session expires in 30 days; offline copies expire in 7 days and are deleted on sign-out or session expiry; changing the passcode revokes all sessions | `sw.js`, `security.py` |
| 5 | Stolen `auth.json` | Offline passcode guessing | 10+ character passcode, PBKDF2-SHA256 with 300k iterations, salted | `set_passcode` |
| 6 | A merchant or CSV row with instructions in its description (prompt injection) | Misleading answer, omitted charges | Tool results are labeled data; tools are read-only; the only output is the answer you read; the number checker still applies; injection eval | `runner.py` SYSTEM, `evals/run.py injection` |
| 7 | Built-in category rules gamed by a descriptor ("SALARY", "EPAY") | Charges hidden from spending | Whole-word matching; Income requires money in | `categorize.py` `builtin_category` |
| 8 | Model writes a destructive or expensive query | Data change, read tokens, stall the server | Read-only connection, authorizer limited to one view, 3 s deadline, 1 MB value cap, worker thread | `db.py` `readonly_conn` |
| 9 | Runaway agent loop | API cost | Per-question turn and cost caps; daily budget | `runner.py`, `/ask` |
| 10 | Personal data in prompts | Data held by a third party | Redaction of names, emails, phones, account digits and owner terms; local-only mode; outbound log | `privacy.py` |
| 11 | Accidental commit of personal data or secrets | Public disclosure | `.gitignore` covers `data/`, `.env`, `config.yaml`, saved pages, `.DS_Store`; history scanned; incident steps in SECURITY.md | `.gitignore` |
| 12 | Vulnerable dependency | Varies | `requirements.lock`, Dependabot, `pip-audit` and `bandit` in CI (weekly too) | `.github/` |

## Known gaps (accepted)

- Local processes running as the owner are trusted. Requiring the passcode on the Mac too is a one-line change if that ever matters.
- Login lockout state is in memory and resets on restart.
- There's no per-device session revocation; changing the passcode logs every device out.
- Planning answers send salary, balance and goal figures to the API (no names or account numbers). Use local-only mode and skip planning questions to avoid this.

## Incident response

See [SECURITY.md](../SECURITY.md#incident-response).
