#!/usr/bin/env bash
# deploy-proto.sh — update the staging copy (proto, served by tron) to the pushed GitHub main, safely:
#   1. refuses while paid generation jobs are in flight (a restart would strand them);
#   2. backs up the databases and the assets (scripts/backup.sh);
#   3. fast-forwards the checkout to origin/main (never a local, unpushed commit);
#   4. restarts the service and waits for the health answer;
#   5. runs the read-only checks of scripts/verify-production.sh against the local base path.
#
#   ssh tron 'bash -s' < scripts/deploy-proto.sh            (from a developer machine)
#   bash scripts/deploy-proto.sh                            (on tron)
#
# Environment: P3_CHECKOUT (default /home/yakir/git/XjetJewelryBuilderPipeline3), P3_SERVICE (default
# xjet-jewelry-b2c3), P3_LOCAL_URL (default http://localhost:8340/JewelryB2C3), P3_BACKUP_DIR (default ~/p3-backups).
set -euo pipefail

CHECKOUT="${P3_CHECKOUT:-/home/yakir/git/XjetJewelryBuilderPipeline3}"
SERVICE="${P3_SERVICE:-xjet-jewelry-b2c3}"
LOCAL="${P3_LOCAL_URL:-http://localhost:8340/JewelryB2C3}"
export P3_BACKUP_DIR="${P3_BACKUP_DIR:-$HOME/p3-backups}"
cd "$CHECKOUT"

echo "1. jobs in flight?"
.venv/bin/python - <<'PY'
import sqlite3, sys
C = sqlite3.connect("file:var/pipeline3.db?mode=ro", uri=True)
Busy = {T: C.execute(f"SELECT COUNT(*) FROM {T} WHERE status IN ('queued', 'running', 'pending', 'generating')").fetchone()[0]
        for T in ("candidates", "movies", "meshes")}
print("   in flight:", Busy)
if any(Busy.values()):
    sys.exit("   refusing to restart while generation jobs run — try again in a few minutes")
PY

echo "2. backup"
P3_DATA_DIR="$CHECKOUT/var" bash scripts/backup.sh | sed 's/^/   /'

echo "3. code"
git fetch -q origin
git merge --ff-only -q origin/main
git log --oneline -1 | sed 's/^/   /'
if ! .venv/bin/python -c "import fastapi, uvicorn, httpx, PIL" 2>/dev/null || [[ requirements.txt -nt .venv/pyvenv.cfg ]]; then
    echo "   requirements changed: installing (uv)"
    (command -v uv >/dev/null && uv pip install -q --python .venv/bin/python -r requirements.txt) || .venv/bin/pip install -q -r requirements.txt
fi

echo "4. restart"
sudo systemctl restart "$SERVICE"
R=""
for _ in $(seq 1 30); do
    R="$(curl -s --max-time 3 "$LOCAL/api/health" || true)"
    [[ -n "$R" ]] && break
    sleep 1
done
if [[ -z "$R" ]]; then
    echo "   NO health answer after 30 s — check: sudo journalctl -u $SERVICE -n 50" >&2
    exit 1
fi
echo "   health: ${R:0:120}"
systemctl is-active "$SERVICE" | sed 's/^/   service: /'

echo "5. checks"
bash scripts/verify-production.sh "$LOCAL" | sed 's/^/   /'
