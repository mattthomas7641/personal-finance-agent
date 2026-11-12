"""The leak guard must block the things that have leaked (or nearly leaked) from projects like this one."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("leak_guard", Path(__file__).parents[1] / "scripts" / "leak_guard.py")
lg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lg)


@pytest.mark.parametrize("path", ["Northstar.html", "Northstar_files/tailwind.js", ".DS_Store", "app/.DS_Store", ".env",
                                  "data/ledger.db", "ledger.db-wal", "server.log", "config.yaml", "Downloads/Portfolio_Positions.csv",
                                  "statement.pdf", "export.ofx", "capture.har", "screenshot.png", "docs/me.jpg", "backup.bundle"])
def test_blocked_paths(path):
    assert lg.path_problem(path)


@pytest.mark.parametrize("path", ["app/web/templates/plan.html", "docs/screenshots/overview.png", "app/web/static/icon-180.png",
                                  ".env.example", "config.example.yaml", "app/main.py", "README.md"])
def test_allowed_paths(path):
    assert lg.path_problem(path) is None


@pytest.mark.parametrize("line,expect", [
    ("ANTHROPIC_API_KEY=sk-ant-api03-" + "a" * 40, "Anthropic API key"),
    ("token = 'access-production-" + "1234abcd-12ab-34cd-56ef-1234567890ab'", "Plaid access token"),
    ("PLAID_SECRET=" + "0123456789abcdef0123456789", "Plaid secret"),
    ("ghp_" + "A" * 36, "GitHub token"),
    ("-----BEGIN RSA PRIVATE KEY-----", "private key"),
    ("ssn: 123-45-6789", "Social Security"),
    ("card 3782 822463 10005", "card number"),
    ("contact me at someone@gmail.com", "real email"),
])
def test_blocked_content(line, expect):
    assert any(expect in p for p in lg.line_problems(line, []))


@pytest.mark.parametrize("line", ["user.email=323404204+mattthomas7641@users.noreply.github.com", "see https://example.com",
                                  "amount 1234567890123", "date 2026-10-14", "access-sandbox-SECRET",
                                  "someone@gmail.com  # leakguard: allow"])
def test_allowed_content(line):
    assert lg.line_problems(line, []) == []


def test_private_denylist_terms_are_blocked_without_printing_them():
    problems = lg.line_problems("# plan sponsor: Acme Widgets Inc", ["acme widgets"])
    assert problems and "acme widgets" not in problems[0].lower()
