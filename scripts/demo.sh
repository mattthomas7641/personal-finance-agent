#!/usr/bin/env bash
# Run Northstar on a synthetic 12-month ledger in a throwaway folder: safe for screenshots and recordings.
set -euo pipefail
cd "$(dirname "$0")/.."
DEMO="$(mktemp -d -t northstar-demo)"
export LEDGER_DATA="$DEMO"
.venv/bin/python - <<'PY'
from app.db import init_db, session
from app.ingest.sources import load_demo
from app.models import RetirementPlan
init_db()
load_demo(with_truth_categories=True)
with session() as s:
    s.merge(RetirementPlan(id=1, birth_year=1996, retirement_age=40, fire_target_low=1_500_000, fire_target_high=2_000_000))
    s.commit()
PY
echo "Demo data in $DEMO. Open http://localhost:8767 (Ctrl-C to stop)."
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8767
