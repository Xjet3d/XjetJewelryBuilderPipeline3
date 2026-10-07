#!/usr/bin/env bash
# restore.sh — put a backup made by scripts/backup.sh back: the two databases (and, with --assets, the assets).
# The service must be stopped first; the current files are kept aside as <name>.before-restore-<timestamp>.
#
#   sudo systemctl stop xjet-atelier
#   P3_DATA_DIR=/srv/atelier/data bash scripts/restore.sh /srv/atelier/backups/20261007T033000Z [--assets]
#   sudo systemctl start xjet-atelier
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${P3_DATA_DIR:-$HERE/var}"
SRC="${1:?backup directory (made by scripts/backup.sh)}"
WITH_ASSETS="${2:-}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"

[[ -f "$SRC/MANIFEST" ]] || { echo "Not a backup directory (no MANIFEST): $SRC" >&2; exit 1; }
if pgrep -f "uvicorn p3.app:App" >/dev/null 2>&1; then
    echo "The service is running — stop it first (sudo systemctl stop xjet-atelier)." >&2
    exit 1
fi
mkdir -p "$DATA"
for DB in pipeline3.db accounts.db; do
    [[ -f "$SRC/$DB" ]] || continue
    if [[ -f "$DATA/$DB" ]]; then
        mv "$DATA/$DB" "$DATA/$DB.before-restore-$TS"
        rm -f "$DATA/$DB-wal" "$DATA/$DB-shm"
    fi
    cp -a "$SRC/$DB" "$DATA/$DB"
    echo "  restored $DB (the previous file is $DATA/$DB.before-restore-$TS)"
done
if [[ "$WITH_ASSETS" == "--assets" && -d "$SRC/assets" ]]; then
    [[ -d "$DATA/assets" ]] && mv "$DATA/assets" "$DATA/assets.before-restore-$TS"
    cp -a "$SRC/assets" "$DATA/assets"
    echo "  restored assets (the previous directory is $DATA/assets.before-restore-$TS)"
fi
echo "Restored from $SRC. Start the service and run scripts/verify-production.sh."
