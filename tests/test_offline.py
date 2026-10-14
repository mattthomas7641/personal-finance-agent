"""End-to-end: the phone keeps showing last-synced pages when the Mac is off (real Chromium + service worker)."""
import asyncio
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.async_api")
ROOT = Path(__file__).parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_offline_shows_cached_pages_and_disables_actions():
    data = tempfile.mkdtemp()
    env = {**os.environ, "LEDGER_DATA": data}
    subprocess.run([sys.executable, "-c", "from app.db import init_db; init_db(); from app.ingest.sources import load_demo; "
                    "load_demo(with_truth_categories=True)"], env=env, cwd=ROOT, check=True)
    port = free_port()
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                           env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://localhost:{port}"
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)

        async def run():
            async with pw.async_playwright() as p:
                try:
                    browser = await p.chromium.launch()
                except Exception as e:
                    pytest.skip(f"Chromium not installed: {e}")
                page = await (await browser.new_context()).new_page()
                await page.goto(base + "/")
                await page.evaluate("navigator.serviceWorker.ready")
                await page.reload()
                await page.wait_for_timeout(2500)
                srv.send_signal(signal.SIGTERM)
                srv.wait()
                await page.goto(base + "/transactions")
                text = await page.inner_text("body")
                disabled = await page.evaluate("[...document.querySelectorAll('form button')].every(b => b.disabled)")
                await browser.close()
                return text, disabled

        text, disabled = asyncio.run(run())
        assert "Mac offline" in text and "Transactions" in text and disabled
    finally:
        if srv.poll() is None:
            srv.kill()
