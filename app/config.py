from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("LEDGER_DATA", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

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
    "Transfers", "Other",
]
# Excluded from "spending" totals.
NON_SPEND = {"Income", "Transfers"}
