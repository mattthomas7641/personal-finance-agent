"""Every template must compile, and every page must render, so a template typo can't reach the running app."""
import os
import tempfile
from pathlib import Path

os.environ["LEDGER_DATA"] = os.environ.get("LEDGER_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, templates  # noqa: E402

TEMPLATES = sorted(p.name for p in (Path(__file__).parent.parent / "app/web/templates").glob("*.html"))


@pytest.mark.parametrize("name", TEMPLATES)
def test_template_compiles(name):
    templates.env.get_template(name)


@pytest.mark.parametrize("path", ["/connect", "/transactions", "/goals", "/plan", "/subscriptions", "/runs", "/rules", "/privacy"])
def test_page_renders(path):
    with TestClient(app, client=("127.0.0.1", 5000)) as c:
        assert c.get(path).status_code == 200, path
