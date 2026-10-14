from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("LEDGER_DATA", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
try:
    DATA_DIR.chmod(0o700)  # bank data: readable by this macOS user only
except OSError:
    pass
os.umask(0o077)  # files created from here on (database, logs, auth.json) are private too

env = ROOT / ".env"
if env.exists():
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def load_config() -> dict:
    p = ROOT / "config.yaml"
    return yaml.safe_load((p if p.exists() else ROOT / "config.example.yaml").read_text()) or {}


# Fixed taxonomy. Small and opinionated so categorization is checkable.
CATEGORIES = [
    "Income", "Rent & Mortgage", "Utilities", "Groceries", "Dining", "Coffee", "Transport", "Travel",
    "Shopping", "Subscriptions", "Entertainment", "Health & Fitness", "Personal Care", "Fees & Interest",
    "Transfers", "Other", "Excluded",
]
# Not counted as spending. "Excluded" is for charges you mark yourself: reimbursed work expenses, one-offs.
NON_SPEND = {"Income", "Transfers", "Excluded"}
# The AI categorizer never picks "Excluded"; only you do.
AI_CATEGORIES = [c for c in CATEGORIES if c != "Excluded"]
