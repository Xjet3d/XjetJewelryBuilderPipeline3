#!/usr/bin/env bash
# auto-deploy-atelier.sh — keep atelier's Pipeline 3 (a user systemd service, staging under /JewelryB2C3/) on GitHub main.
# Run every few minutes by xjet-jewelry-b2c3-autodeploy.timer (deploy/atelier/). Nothing happens unless origin/main moved:
#   1. fetch; stop if the checkout already is origin/main (the usual case: no output, no restart);
#   2. skip (retry at the next tick) while paid generation jobs are in flight, so a restart never strands them;
#   3. back up the databases and assets (scripts/backup.sh), fast-forward to origin/main (never a local commit),
#      install requirements when they changed, restart the user service;
#   4. wait for the health answer; if there is none, go back to the previous commit and restart it (the failure is
#      logged and the same broken commit is not retried until main moves again);
#   5. run the read-only checks of scripts/verify-production.sh (their result is logged, never a reason to roll back).
# Gallery content is not part of this automatic update. After an update, when proto's approved Inspiration Gallery
# changed, push it explicitly (only gallery content moves; deploy/README.md, atelier):
#   bash scripts/gallery-sync.sh push https://xjetatelier.xjet3d.com/JewelryB2C3
# Manual run: bash scripts/auto-deploy-atelier.sh   ·   Pause: systemctl --user stop xjet-jewelry-b2c3-autodeploy.timer
# Environment: P3_CHECKOUT (default ~/git/XjetJewelryBuilderPipeline3), P3_SERVICE (xjet-jewelry-b2c3),
# P3_LOCAL_URL (http://127.0.0.1:8340/JewelryB2C3), P3_BACKUP_DIR (~/p3-backups).
set -euo pipefail

CHECKOUT="${P3_CHECKOUT:-$HOME/git/XjetJewelryBuilderPipeline3}"
SERVICE="${P3_SERVICE:-xjet-jewelry-b2c3}"
LOCAL="${P3_LOCAL_URL:-http://127.0.0.1:8340/JewelryB2C3}"
export P3_BACKUP_DIR="${P3_BACKUP_DIR:-$HOME/p3-backups}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
cd "$CHECKOUT"
exec 9>"$CHECKOUT/.auto-deploy.lock"
flock -n 9 || { echo "another deploy is running"; exit 0; }

git fetch -q origin main
Old="$(git rev-parse HEAD)"; New="$(git rev-parse origin/main)"
[[ "$Old" == "$New" ]] && exit 0
git merge-base --is-ancestor "$Old" "$New" || { echo "checkout has commits that are not on origin/main ($Old): not deploying" >&2; exit 1; }
[[ -f .auto-deploy.bad && "$(cat .auto-deploy.bad)" == "$New" ]] && { echo "$New failed to start before; waiting for a newer main"; exit 0; }

echo "deploying ${Old:0:7} -> ${New:0:7}: $(git log -1 --format=%s "$New")"
Busy="$(.venv/bin/python - <<'PY'
import sqlite3
try:
    C = sqlite3.connect("file:var/pipeline3.db?mode=ro", uri=True)
    print(sum(C.execute(f"SELECT COUNT(*) FROM {T} WHERE status IN ('queued','running','pending','generating')").fetchone()[0]
              for T in ("candidates", "movies", "meshes")))
except sqlite3.Error:
    print(0)
PY
)"
[[ "$Busy" == "0" ]] || { echo "$Busy generation job(s) in flight: will retry at the next tick"; exit 0; }

P3_DATA_DIR="$CHECKOUT/var" bash scripts/backup.sh | sed 's/^/  backup: /'
git merge --ff-only -q "$New"
if [[ "$(git diff --name-only "$Old" "$New" -- requirements.txt)" ]]; then
    echo "requirements changed: installing"
    .venv/bin/pip install -q -r requirements.txt
fi

Health() {
    local R=""
    for _ in $(seq 1 30); do
        R="$(curl -s --max-time 3 "$LOCAL/api/health" || true)"
        [[ -n "$R" ]] && { echo "$R"; return 0; }
        sleep 1
    done
    return 1
}

systemctl --user restart "$SERVICE"
if R="$(Health)"; then
    echo "deployed ${New:0:7}; health: ${R:0:120}"
    rm -f .auto-deploy.bad
else
    echo "NO health answer after 30 s: rolling back to ${Old:0:7}" >&2
    echo "$New" > .auto-deploy.bad
    git reset -q --hard "$Old"
    systemctl --user restart "$SERVICE"
    Health >/dev/null && echo "rolled back; service is up on ${Old:0:7}" >&2 || echo "ROLLBACK ALSO HAS NO HEALTH ANSWER: check journalctl --user -u $SERVICE" >&2
    exit 1
fi
bash scripts/verify-production.sh "$LOCAL" 2>&1 | sed 's/^/  verify: /' || echo "  verify: some checks failed (see above); the deploy stays"
