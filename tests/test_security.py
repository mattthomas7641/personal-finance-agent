import os
import tempfile

os.environ["LEDGER_DATA"] = os.environ.get("LEDGER_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import security  # noqa: E402
from app.main import app  # noqa: E402

PHONE = ("100.88.12.34", 50000)      # a Tailscale address
LAN = ("192.168.1.23", 50000)        # someone else on the same Wi-Fi
MAC = ("127.0.0.1", 50000)


@pytest.fixture(autouse=True)
def fresh_auth():
    security.AUTH_FILE.unlink(missing_ok=True)
    security._failures.clear()
    yield
    security.AUTH_FILE.unlink(missing_ok=True)


def client(addr):
    return TestClient(app, client=addr, follow_redirects=False)


@pytest.mark.parametrize("ip,ok", [("127.0.0.1", True), ("::1", True), ("100.64.0.1", True), ("100.127.255.254", True),
                                   ("fd7a:115c:a1e0::1", True), ("192.168.1.5", False), ("10.0.0.2", False),
                                   ("8.8.8.8", False), ("100.128.0.1", False), ("testclient", False)])
def test_network_allowlist(ip, ok):
    assert security.is_allowed_network(ip) is ok


def test_lan_and_internet_are_refused():
    with client(LAN) as c:
        r = c.get("/transactions")
        assert r.status_code == 403 and "Forbidden" in r.text
        assert c.get("/static/tailwind.js").status_code == 403


def test_mac_needs_no_passcode():
    with client(MAC) as c:
        assert c.get("/privacy").status_code == 200


def test_phone_blocked_until_passcode_is_set():
    with client(PHONE) as c:
        r = c.get("/transactions")
        assert r.status_code == 403 and "Set a passcode first" in r.text


def test_phone_login_flow():
    security.set_passcode("correct-horse")
    with client(PHONE) as c:
        r = c.get("/transactions")
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        assert c.post("/ask", data={"question": "hi"}).status_code == 401  # no side effects without a session
        r = c.post("/login", data={"passcode": "nope", "next": "/transactions"})
        assert "Wrong" in r.headers["location"]
        r = c.post("/login", data={"passcode": "correct-horse", "next": "/transactions"})
        assert r.status_code == 303 and r.headers["location"] == "/transactions"
        assert c.get("/transactions").status_code == 200


def test_open_redirect_blocked():
    security.set_passcode("correct-horse")
    with client(PHONE) as c:
        r = c.post("/login", data={"passcode": "correct-horse", "next": "//evil.example.com"})
        assert r.headers["location"] == "/"


def test_bruteforce_lockout(monkeypatch):
    monkeypatch.setattr(security.time, "sleep", lambda s: None)
    security.set_passcode("correct-horse")
    for _ in range(8):
        assert not security.check_passcode("wrong", "100.1.1.1")
    with pytest.raises(PermissionError):
        security.check_passcode("correct-horse", "100.1.1.1")


def test_sessions_are_signed_and_reset_by_new_passcode():
    security.set_passcode("correct-horse")
    tok = security.make_session()
    assert security.valid_session(tok)
    exp, sig = tok.split(".")
    assert not security.valid_session(f"{int(exp) + 999}.{sig}")  # tampered expiry
    security.set_passcode("another-one")
    assert not security.valid_session(tok)


def test_passcode_only_changeable_from_the_mac():
    security.set_passcode("correct-horse")
    with client(PHONE) as c:
        c.cookies.set(security.COOKIE, security.make_session())
        r = c.post("/security/passcode", data={"passcode": "hijacked!", "confirm": "hijacked!"})
        assert r.status_code == 403
    assert security.check_passcode("correct-horse", "x")


def test_pwa_assets_public():
    security.set_passcode("correct-horse")
    with client(PHONE) as c:
        assert c.get("/manifest.webmanifest").json()["display"] == "standalone"
        assert c.get("/sw.js").status_code == 200 and c.get("/static/icon-180.png").status_code == 200
