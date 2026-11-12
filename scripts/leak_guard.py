#!/usr/bin/env python3
"""Leak guard: stops secrets and personal financial data from reaching git.

Runs as a pre-commit hook (staged changes), a pre-push hook (every commit being pushed) and in CI (the whole
tree). It complements gitleaks, which knows hundreds of API-key formats but nothing about *personal* data:
this repo's real leak was a saved browser page with someone's own salary and goal figures.

Three kinds of check:
  1. Paths: files that should never be committed here (env files, databases, logs, bank/CSV exports,
     saved web pages, .DS_Store, keys, images outside the demo-screenshot folder).
  2. Secrets: the credential formats this project uses (Anthropic, Plaid, GitHub, AWS, private keys).
  3. Personal data: SSNs, card numbers (Luhn-checked), real email addresses, and a private denylist of
     terms that identify you (name of your employer, account references...) kept in .leakguard.local,
     which is git-ignored so the list itself is never published.

A known false positive can be allowed by putting "leakguard: allow" on the same line.
Usage: leak_guard.py --staged | --commits <rev-list range> | --all
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip() or ".")
DENYLIST = ROOT / ".leakguard.local"
ALLOW_MARK = "leakguard: allow"

BLOCKED_PATHS = [
    (".env", "environment file with API keys"), (".env.*", "environment file"), ("*.env", "environment file"),
    ("data/*", "your local database and logs"), ("*.db", "database"), ("*.sqlite*", "database"), ("*.db-*", "database"),
    ("*.log", "log file (can contain merchant names and errors)"), ("config.yaml", "your personal config"),
    ("*.csv", "bank or brokerage export"), ("*.ofx", "bank export"), ("*.qfx", "bank export"), ("*.qbo", "bank export"),
    ("*.xls", "spreadsheet export"), ("*.xlsx", "spreadsheet export"), ("*.pdf", "statement or document"),
    ("*.har", "browser network capture (contains cookies)"), ("*_files/*", "saved web page assets"),
    ("*.DS_Store", "macOS folder metadata"), ("*.pem", "private key"), ("*.key", "private key"), ("*.p12", "certificate"),
    ("auth.json", "passcode hash"), ("*.bundle", "git backup"),
]
ALLOWED_HTML = ("app/web/templates/*",)
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".tiff")
ALLOWED_IMAGES = ("docs/screenshots/*", "app/web/static/*")

SECRETS = [
    (r"sk-ant-[A-Za-z0-9_\-]{20,}", "Anthropic API key"),
    (r"access-(?:sandbox|development|production)-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "Plaid access token"),
    (r"(?i)plaid_secret\s*[=:]\s*['\"]?[0-9a-f]{20,}", "Plaid secret"),
    (r"(?i)plaid_client_id\s*[=:]\s*['\"]?[0-9a-f]{20,}", "Plaid client ID"),
    (r"gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,}", "GitHub token"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key"),
    (r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", "private key"),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", "Slack token"),
]
SSN = re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")
CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
SAFE_EMAIL_DOMAINS = ("users.noreply.github.com", "example.com", "example.org", "example.net", "anthropic.com", "test")


def luhn(digits: str) -> bool:
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        total, alt = total + n, not alt
    return total % 10 == 0


def denylist() -> list[str]:
    if not DENYLIST.exists():
        return []
    return [t.strip() for t in DENYLIST.read_text().splitlines() if t.strip() and not t.startswith("#")]


def path_problem(path: str) -> str | None:
    name = path.split("/")[-1]
    for pat, why in BLOCKED_PATHS:
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(name, pat):
            if path == ".env.example" or path.endswith(".example"):
                return None
            return why
    if name.lower().endswith((".html", ".htm")) and not any(fnmatch.fnmatch(path, p) for p in ALLOWED_HTML):
        return "saved web page (may contain a snapshot of your real data)"
    if name.lower().endswith(IMAGE_EXT) and not any(fnmatch.fnmatch(path, p) for p in ALLOWED_IMAGES):
        return "image outside docs/screenshots (screenshots must come from scripts/demo.sh)"
    return None


def line_problems(line: str, terms: list[str]) -> list[str]:
    if ALLOW_MARK in line:
        return []
    out = [why for pat, why in SECRETS if re.search(pat, line)]
    if SSN.search(line):
        out.append("looks like a Social Security number")
    for m in CARD.finditer(line):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and luhn(digits) and len(set(digits)) > 2:
            out.append("looks like a card number")
            break
    for m in EMAIL.finditer(line):
        domain = m.group().split("@", 1)[1].lower()
        if not any(domain == d or domain.endswith("." + d) for d in SAFE_EMAIL_DOMAINS):
            out.append(f"real email address ({m.group()})")
    low = line.lower()
    for t in terms:
        if t.lower() in low:
            out.append(f'term from your private denylist ("{t[:3]}…")')
    return out


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=ROOT).stdout


def changes(diff_args: list[str]) -> list[tuple[str, list[tuple[int, str]]]]:
    """(path, [(line number, added line)]) for added/modified files in a diff."""
    files = [f for f in git("diff", "--name-only", "--diff-filter=ACMR", *diff_args).splitlines() if f]
    out = []
    for f in files:
        added, n = [], 0
        for raw in git("diff", "-U0", "--no-color", *diff_args, "--", f).splitlines():
            if raw.startswith("@@"):
                m = re.search(r"\+(\d+)", raw)
                n = int(m.group(1)) if m else 0
            elif raw.startswith("+") and not raw.startswith("+++"):
                added.append((n, raw[1:]))
                n += 1
        out.append((f, added))
    return out


def all_files() -> list[tuple[str, list[tuple[int, str]]]]:
    out = []
    for f in git("ls-files").splitlines():
        p = ROOT / f
        try:
            text = p.read_text(errors="strict")
        except (UnicodeDecodeError, OSError):
            out.append((f, []))
            continue
        out.append((f, list(enumerate(text.splitlines(), 1))))
    return out


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def check(items) -> list[str]:
    terms = denylist()
    problems = []
    for label, lines in items:
        path = label.split(":", 1)[1] if re.match(r"^[0-9a-f]{7}:", label) else label
        if why := path_problem(path):
            problems.append(f"{label}: {why}")
            continue
        if path == "scripts/leak_guard.py" or path.startswith("tests/test_leak_guard"):
            continue  # the guard's own patterns and test fixtures
        for n, line in lines:
            for why in line_problems(line, terms):
                problems.append(f"{label}:{n}: {why}")
    return problems


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("--staged", "--commits", "--all"):
        print(__doc__)
        return 2
    if argv[0] == "--staged":
        items = changes(["--cached"])
    elif argv[0] == "--commits":
        # Every commit on its own: something added in one commit and deleted in the next is still public history.
        items = []
        for c in git("rev-list", "--reverse", *argv[1:]).split():
            parent = c + "^" if git("rev-list", "--parents", "-n", "1", c).split()[1:] else EMPTY_TREE
            items += [(f"{c[:7]}:{f}", lines) for f, lines in changes([parent, c])]
    else:
        items = all_files()
    problems = check(items)
    if not problems:
        return 0
    print("\nLeak guard blocked this because it may expose secrets or personal data:\n", file=sys.stderr)
    for p in problems[:50]:
        print(f"  • {p}", file=sys.stderr)
    print("\nRemove it (or add it to .gitignore). If it's a genuine false positive, add 'leakguard: allow' to that line.\n"
          "Don't bypass with --no-verify: CI runs the same checks, and a push to GitHub is public.\n", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
