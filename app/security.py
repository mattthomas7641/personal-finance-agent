"""Who may reach the app.

1. Network: only this Mac (loopback) and your Tailscale devices (100.64.0.0/10, fd7a:115c:a1e0::/48).
   Everyone else, including other devices on the same Wi-Fi, gets a 403 before any page renders.
2. Passcode: required for every non-loopback client. Stored as a salted PBKDF2 hash in data/auth.json.
   Sessions are HMAC-signed cookies (30 days). Repeated wrong guesses lock login for a while.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import time

from app.config import DATA_DIR

AUTH_FILE = DATA_DIR / "auth.json"
COOKIE = "ledger_session"
SESSION_DAYS = 30
TAILNET = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")]
_failures: dict[str, list[float]] = {}


def is_local(host: str | None) -> bool:
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def is_allowed_network(host: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return ip.is_loopback or any(ip in n for n in TAILNET)


def _load() -> dict:
    return json.loads(AUTH_FILE.read_text()) if AUTH_FILE.exists() else {}


def has_passcode() -> bool:
    return "hash" in _load()


def _hash(passcode: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", passcode.encode(), bytes.fromhex(salt), 300_000).hex()


def set_passcode(passcode: str) -> None:
    if len(passcode) < 6:
        raise ValueError("Use at least 6 characters.")
    salt = secrets.token_hex(16)
    data = {"salt": salt, "hash": _hash(passcode, salt), "secret": secrets.token_hex(32)}  # new secret logs out old sessions
    AUTH_FILE.write_text(json.dumps(data))
    AUTH_FILE.chmod(0o600)


def check_passcode(passcode: str, client: str) -> bool:
    now = time.time()
    recent = [t for t in _failures.get(client, []) if now - t < 900]
    if len(recent) >= 8:
        raise PermissionError("Too many attempts. Try again in 15 minutes.")
    d = _load()
    ok = bool(d) and hmac.compare_digest(_hash(passcode, d["salt"]), d["hash"])
    if not ok:
        _failures[client] = recent + [now]
        time.sleep(0.5)
    else:
        _failures.pop(client, None)
    return ok


def make_session() -> str:
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    sig = hmac.new(bytes.fromhex(_load()["secret"]), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_session(token: str | None) -> bool:
    if not token or "." not in token or not has_passcode():
        return False
    exp, sig = token.split(".", 1)
    good = hmac.new(bytes.fromhex(_load()["secret"]), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()
