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
| Plaid access tokens | macOS Keychain (never in the database, logs, or prompts) | The app, as the owner's macOS user |
| API keys (Anthropic, Plaid) | `.env` on the Mac, excluded from git | The owner |
| Text sent to the Claude API | Redacted descriptors and query results only; logged locally on the Privacy page | Anthropic (processor), per its API terms |

## Controls

**Access control**
- The server listens on localhost only. Remote access is only possible from the owner's own devices over Tailscale (WireGuard + HTTPS via `tailscale serve`); all other clients get HTTP 403.
- Every non-local client must enter a passcode (PBKDF2-SHA256, 300k iterations, salted). Sessions are HMAC-signed cookies (HttpOnly, SameSite=Strict, 30 days). Repeated wrong guesses lock login for 15 minutes. The passcode can only be changed from the Mac.
- Cross-site requests and DNS rebinding are rejected (Origin and Host checks).
- Least privilege for the AI agent: its database connection is read-only and an SQLite authorizer only allows reading the `transactions` view, with no access to tokens or internal IDs.
- Accounts used to operate the app (Plaid dashboard, GitHub) use two-factor authentication.

**Encryption**
- At rest: the Mac uses FileVault full-disk encryption; Plaid tokens are additionally stored in the macOS Keychain.
- In transit: Plaid and Anthropic APIs are HTTPS (TLS 1.2+). Phone access is HTTPS over Tailscale's encrypted network. Local browser traffic never leaves the machine.

**Privacy and minimization**
- Before any text is sent to the Claude API it is redacted: names in P2P payment lines, emails, phone numbers, card/account/reference digits, and owner-defined terms. Amounts and dates are kept.
- "Local only" mode sends nothing for categorization.
- No consumer data is sold or shared beyond the redacted API calls above.

**Vulnerability and change management**
- macOS automatic security updates are enabled.
- Dependencies are pinned in `pyproject.toml` and reviewed at each review cycle; security fixes are applied when announced.
- 100+ automated tests cover the security controls (SQL sandbox escapes, redaction, passcode/session/CSRF rules, network allow-list, token storage).
- No secrets are committed to git (`.env`, `data/` and configuration are git-ignored; history was scanned before publishing).

## Data retention and deletion

- Transaction data is kept locally for as long as the owner uses the app, to support year-over-year budgeting.
- Disconnecting a bank (Accounts → Disconnect) calls Plaid `/item/remove`, which revokes access, and deletes the access token from the Keychain. The owner can choose to delete that bank's transaction history at the same time.
- Demo data can be removed with one click; the full dataset is deleted by removing `data/ledger.db`.
- The local log of text sent to the Claude API can be cleared from the Privacy page.
- Anthropic's API retention applies to redacted text sent to it; it is not used for model training under the API terms.

## Incident response

If a device or key may be compromised: disconnect all banks (revokes Plaid access), rotate the Plaid secret and
Anthropic API key, change the app passcode (invalidates all sessions), and remove the device from Tailscale.
