"""Who may reach the app.

1. Network: the server listens on localhost only. Your phone reaches it through `tailscale serve`, which
   terminates HTTPS on your tailnet (https://<mac>.<tailnet>.ts.net) and proxies to localhost. Those
   proxied requests are recognized by Tailscale's identity header / ts.net host and treated as REMOTE.
   Direct connections from Tailscale IPs are also accepted; anything else gets a 403.
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


def via_tailscale_serve(headers) -> bool:
    """Tailscale Serve strips and re-sets Tailscale-User-Login, so it can't be spoofed through the proxy."""
    return bool(headers.get("tailscale-user-login")) or headers.get("host", "").split(":")[0].endswith(".ts.net")


def classify(host: str | None, headers) -> str:
    """'local' (this Mac, no passcode), 'remote' (your devices, passcode) or 'denied'."""
    if is_local(host):
        return "remote" if via_tailscale_serve(headers) else "local"
    return "remote" if is_allowed_network(host) else "denied"


OK_HOSTS = ("localhost", "127.0.0.1", "[::1]", "testserver")


def bad_origin(method: str, headers) -> bool:
    """Blocks DNS-rebinding (unexpected Host) and cross-site form posts (foreign Origin)."""
    from urllib.parse import urlsplit

    host = headers.get("host", "").rsplit(":", 1)[0] if not headers.get("host", "").startswith("[") else headers.get("host", "").split("]")[0] + "]"
    if host not in OK_HOSTS and not host.endswith(".ts.net") and not _is_tailnet_ip(host):
        return True
    origin = headers.get("origin")
    if method not in ("GET", "HEAD", "OPTIONS") and origin and origin != "null":
        return urlsplit(origin).hostname != host.strip("[]")
    return False


def _is_tailnet_ip(h: str) -> bool:
    try:
        return any(ipaddress.ip_address(h.strip("[]")) in n for n in TAILNET)
    except ValueError:
        return False


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
