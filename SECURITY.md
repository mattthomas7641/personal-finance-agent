# Security policy: Northstar (Personal Finance Agent)

Owner and security contact: Matthew Thomas (developer, sole user). Report issues via GitHub issues or the email on the Plaid application.
Last reviewed: 2026-10-14 · Review schedule: every 6 months, and whenever a new data source or vendor is added.

## Scope

Northstar is a single-user application that runs on its owner's Mac. It reads the owner's own bank and card
transactions (Plaid Transactions, or CSV exports) to categorize spending, track budgets and goals, and answer
questions. It never initiates payments or moves money. It is not offered to other people.

## Data handled

| Data | Where it lives | Who can access it |
|---|---|---|
| Transactions, accounts, budgets, goals | `data/ledger.db` (SQLite) on the owner's Mac | The owner |
| Plaid access tokens | macOS Keychain. The database holds only a `keychain:` reference. A token from an older database is moved into the Keychain at startup, and a failure to do so is logged as an error. Tokens never appear in logs, pages or prompts. | The app, as the owner's macOS user |
| API keys (Anthropic, Plaid) | `.env` on the Mac, excluded from git | The owner |
| Text sent to the Claude API | Redacted transaction descriptors and query results. Goal and plan questions also send numbers: salary, balances, net worth and goal targets (no names or account numbers). Everything sent is logged locally on the Privacy page. | Anthropic (processor), per its API terms |

## Controls

**Access control**
- The server listens on localhost only. Remote access is only possible from the owner's own devices over Tailscale (WireGuard + HTTPS via `tailscale serve`); all other clients get HTTP 403.
- Every proxied or non-local client must enter a passcode (10+ characters, PBKDF2-SHA256, 300k iterations, salted). A request counts as proxied if it carries Tailscale identity headers, `X-Forwarded-*` headers or a `*.ts.net` Host. Sessions are HMAC-signed cookies (HttpOnly, SameSite=Strict, Secure over HTTPS, 30 days). Repeated wrong guesses lock login for 15 minutes. The passcode can only be changed from the Mac.
- Known limitation: a browser on the Mac itself needs no passcode, so anything running as the owner's macOS user is trusted.
- Cross-site writes are rejected: a foreign or `null` Origin, or a browser `Sec-Fetch-Site` other than same-origin, gets HTTP 403. DNS rebinding is blocked by a Host allow-list.
- Every page carries a Content-Security-Policy: scripts only from the app itself, Plaid Link, or inline blocks with that response's random nonce; no inline event handlers, plugins, framing (`frame-ancestors 'none'`), `<base>` rewrites or foreign form targets. Plus `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` and a Permissions-Policy that turns off camera, microphone, location and payment APIs. Tests check the headers and that no template uses inline handlers.
- Pages and JSON are sent with `Cache-Control: no-store`. The phone's offline copies expire after 7 days and are deleted on sign-out or when the session expires.
- Least privilege for the AI agent: its database connection is read-only and an SQLite authorizer only allows reading the `transactions` view, with no access to tokens or internal IDs. Each query is stopped after 3 seconds and values are capped at 1 MB, so a bad or malicious query can't stall the app.
- Prompt injection: transaction descriptions are untrusted (anyone can name a charge). The agent is told that tool results are data, not instructions. Its tools are read-only and its only output is the answer you read. An eval plants instruction-like descriptions and checks that answers are unaffected.
- Accounts used to operate the app (Plaid dashboard, GitHub) use two-factor authentication.

**Encryption**
- At rest: the Mac uses FileVault full-disk encryption; Plaid tokens are additionally stored in the macOS Keychain.
- In transit: Plaid and Anthropic APIs are HTTPS (TLS 1.2+). Phone access is HTTPS over Tailscale's encrypted network. Local browser traffic never leaves the machine.

**Privacy and minimization**
- Before any text is sent to the Claude API it is redacted: names in P2P payment lines, emails, phone numbers, card/account/reference digits, and owner-defined terms. Amounts and dates are kept, and so are the financial figures in goal and plan answers (see the table above).
- "Local only" mode sends nothing for categorization.
- No consumer data is sold or shared beyond the redacted API calls above.

**Vulnerability and change management**
- macOS automatic security updates are enabled.
- Exact dependency versions are pinned in `requirements.lock`. Dependabot proposes updates weekly, and CI runs `pip-audit` (known CVEs) and `bandit` (static analysis) on every push and weekly.
- 245+ automated tests run in CI. They cover SQL sandbox escapes and query limits, redaction, passcode/session/CSRF rules, the network allow-list, token storage, and failure isolation (Plaid outages, Claude API errors, scheduler failures, malformed files).
- A third-party-style review of security, resilience and cost was done on 2026-10-14. Its findings are fixed and covered by `tests/test_hardening.py`.
- No secrets are committed to git (`.env`, `data/` and configuration are git-ignored; history was scanned before publishing).

## Preventing leaks into the public repository

The repo is public, so anything committed is published. In October 2026 a saved browser copy of the Goals page
(real salary and goal figures) was committed by mistake; the history was rewritten and the repository deleted
and recreated to purge it. Four layers now stop that class of mistake:

| Layer | Runs | Catches |
|---|---|---|
| `scripts/leak_guard.py` pre-commit hook | Every commit on the owner's Mac (`scripts/install-hooks.sh`) | Blocked file types (`.env`, databases, logs, CSV/OFX/PDF exports, saved `.html` pages, `*_files/`, `.DS_Store`, keys, images outside `docs/screenshots`), API keys and tokens for the services used here, SSNs, card numbers (Luhn-checked), real email addresses, and a private denylist of identifying terms in `.leakguard.local` (git-ignored) |
| Same guard as a pre-push hook | Every push, checking **each commit** being pushed, so something added and then deleted is still caught | Same as above |
| CI `leaks` job | Every push and pull request | gitleaks over the full history, plus the leak guard over the tree |
| GitHub secret scanning with push protection | On GitHub | Known credential formats, rejected before they're accepted |

Screenshots must come from `scripts/demo.sh` (synthetic data). A false positive can be allowed with
`leakguard: allow` on that line; bypassing the hooks with `--no-verify` is not acceptable for this repo.

## Backups

`data/ledger.db` is copied once a day with SQLite's online backup API into `data/backups/` (directory 0700, files
0600); the newest 14 are kept. The export (Privacy & security → Download my data) is plain CSV/JSON and never
includes bank access tokens or the passcode hash. To restore, stop Northstar and copy a backup over `data/ledger.db`.

## Data retention and deletion

- Transaction data is kept locally for as long as the owner uses the app, to support year-over-year budgeting.
- Disconnecting a bank (Accounts → Disconnect) calls Plaid `/item/remove`, which revokes access, and deletes the access token from the Keychain. The owner can choose to delete that bank's transaction history at the same time.
- Demo data can be removed with one click; the full dataset is deleted by removing `data/ledger.db`.
- The local log of text sent to the Claude API can be cleared from the Privacy page.
- Anthropic's API retention applies to redacted text sent to it; it is not used for model training under the API terms.

## Incident response

If a device or key may be compromised, or something was committed by mistake: disconnect all banks (revokes Plaid access), rotate the Plaid secret and
Anthropic API key, change the app passcode (invalidates all sessions), and remove the device from Tailscale. For an accidental commit, rewrite git history (`git filter-repo`), force-push, and ask GitHub Support to purge cached views.
