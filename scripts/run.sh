#!/bin/zsh
# Start ledger-agent. Listens on all interfaces so your Tailscale devices can reach it; the app itself
# refuses anything that isn't this Mac or a Tailscale address (see app/security.py).
cd "$(dirname "$0")/.."
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8766
