"""Privacy layer: everything that leaves this machine for the Claude API passes through here.

- `redact()` scrubs personal details from free text (bank descriptors, merchant names):
  people's names in P2P payments, emails, phone numbers, card/account/reference digits, and any
  terms you list in config (your own name, street, employer...). Amounts and dates are untouched
  because they live in numeric/date cells, not in the text being scrubbed.
- `record()` keeps a local log of exactly what was sent, shown on the Privacy page.
- Plaid access tokens live in the OS keychain (macOS Keychain), not in the database.
"""
from __future__ import annotations


import re
from datetime import datetime, timezone

from app.config import load_config

SERVICE = "ledger-agent"  # Keychain service id; kept from the original name so saved tokens stay readable

P2P = re.compile(r"\b(ZELLE|VENMO|CASH ?APP|APPLE CASH)\b", re.I)  # PayPal omitted: mostly merchants
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
MASKED = re.compile(r"(?:X{2,}|\*{2,}|\.{2,}|ENDING(?: IN)?\s*|ACCT\s*|CARD\s*)\s*\d{2,}", re.I)
LONG_DIGITS = re.compile(r"\d{4,}")
ISO_DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
P2P_WORDS = {"TO", "FROM", "FOR", "PAYMENT", "TRANSFER", "PMT", "XFER", "SENT", "RECEIVED", "REQUEST", "INST", "ON", "WEB", "ID", "CONF"}


def _redact_p2p_tail(tail: str) -> str:
    """After 'ZELLE'/'VENMO'/...: keep verbs like PAYMENT/TO, replace the run of name words with [person]."""
    parts = re.split(r"([\s*:]+)", tail)
    out, in_name, done = [], False, False
    for p in parts:
        if not p or re.fullmatch(r"[\s*:]+", p):
            if not in_name:
                out.append(p)
            continue
        is_name = not done and p.upper() not in P2P_WORDS and re.fullmatch(r"[A-Za-z][A-Za-z'.-]*", p) is not None
        if is_name:
            if not in_name:
                out.append("[person]")
                in_name = True
            continue
        if in_name:
            out.append(" ")
            in_name, done = False, True
        out.append(p)
    return "".join(out)


def _custom_terms() -> list[str]:
    return [t for t in (load_config().get("privacy") or {}).get("redact_terms", []) if t and len(t) > 1]


def redact(text: str) -> str:
    if not text or ISO_DATE.match(text.strip()):
        return text
    s = text
    for term in _custom_terms():
        s = re.sub(re.escape(term), "[redacted]", s, flags=re.I)
    s = EMAIL.sub("[email]", s)
    s = PHONE.sub("[phone]", s)

    m = P2P.search(s)
    if m:
        s = s[:m.end()] + _redact_p2p_tail(s[m.end():])
    s = MASKED.sub("[acct]", s)
    s = LONG_DIGITS.sub("#", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def redact_obj(obj):
    """Recursively redact string values (leaves numbers and ISO dates alone)."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: (v if k in ("ids", "date", "dates", "last_charged", "price_changed_on") else redact_obj(v)) for k, v in obj.items()}
    return obj


def local_only() -> bool:
    return (load_config().get("privacy") or {}).get("mode") == "local_only"


# ---------------- outbound log ----------------

def record(kind: str, text: str) -> None:
    from app.db import session
    from app.models import Outbound

    with session() as s:
        s.add(Outbound(kind=kind, text=text[:20000], chars=len(text), created_at=datetime.now(timezone.utc)))
        s.commit()


# ---------------- keychain ----------------

def store_secret(name: str, value: str) -> str:
    """Save to the OS keychain; returns the reference to keep in the DB."""
    import keyring

    keyring.set_password(SERVICE, name, value)
    return f"keychain:{name}"


def load_secret(ref: str) -> str:
    if not ref.startswith("keychain:"):
        return ref  # legacy plaintext value
    import keyring

    v = keyring.get_password(SERVICE, ref.split(":", 1)[1])
    if v is None:
        raise RuntimeError("Plaid token missing from the keychain; reconnect this bank.")
    return v


def delete_secret(ref: str) -> None:
    if ref.startswith("keychain:"):
        import keyring
        try:
            keyring.delete_password(SERVICE, ref.split(":", 1)[1])
        except Exception:
            pass



