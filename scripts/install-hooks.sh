#!/bin/sh
# Turn on the leak guard for this clone: every commit and push runs scripts/leak_guard.py first.
cd "$(git rev-parse --show-toplevel)" || exit 1
chmod +x .githooks/*
git config core.hooksPath .githooks
[ -f .leakguard.local ] || printf '# One term per line that must never appear in this repo (git-ignored, stays on this Mac).\n# e.g. your personal email, employer name, account or reference numbers.\n' > .leakguard.local
echo "Leak guard hooks installed (core.hooksPath=.githooks)."
