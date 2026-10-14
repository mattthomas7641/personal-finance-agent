#!/bin/zsh
# Start Northstar on localhost only. Your phone reaches it through `tailscale serve --bg 8766`
# (HTTPS on your private tailnet), never through your Wi-Fi network.
cd "$(dirname "$0")/.."
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8766
