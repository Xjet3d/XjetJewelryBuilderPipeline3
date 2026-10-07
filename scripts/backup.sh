#!/usr/bin/env bash
# backup.sh — a consistent copy of XJet Atelier's two SQLite databases (the SQLite online-backup API, safe while the
# service runs) and of the customer assets, into a timestamped directory; older backups are pruned.
#
#   P3_DATA_DIR=/srv/atelier/data P3_BACKUP_DIR=/srv/atelier/backups bash scripts/backup.sh
#
# Environment: P3_DATA_DIR (default ./var), P3_BACKUP_DIR (default <data dir>/../backups), P3_BACKUP_KEEP_DAYS
# (default 30), P3_PYTHON (default .venv/bin/python next to this script's repository). Restore: scripts/restore.sh.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${P3_DATA_DIR:-$HERE/var}"
DEST="${P3_BACKUP_DIR:-$(dirname "$DATA")/backups}"
KEEP="${P3_BACKUP_KEEP_DAYS:-30}"
PY="${P3_PYTHON:-$HERE/.venv/bin/python}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$DEST/$TS"

[[ -d "$DATA" ]] || { echo "No data directory: $DATA" >&2; exit 1; }
mkdir -p "$OUT"

for DB in pipeline3.db accounts.db; do
    if [[ -f "$DATA/$DB" ]]; then
        "$PY" - "$DATA/$DB" "$OUT/$DB" <<'PY'
import sqlite3, sys
Src, Dst = sys.argv[1:3]
with sqlite3.connect(f"file:{Src}?mode=ro", uri=True) as S, sqlite3.connect(Dst) as D:
    S.backup(D)                      # page-by-page, consistent even while the service writes
print(f"  {Src} -> {Dst}")
PY
    fi
done

if [[ -d "$DATA/assets" ]]; then
    if command -v rsync >/dev/null 2>&1; then
        rsync -a "$DATA/assets/" "$OUT/assets/"
    else
        cp -a "$DATA/assets" "$OUT/assets"
    fi
    echo "  assets -> $OUT/assets"
fi
[[ -f "$DATA/runtime.json" ]] && cp -a "$DATA/runtime.json" "$OUT/"

# What was backed up, for the record
{
    echo "backup=$TS"
    echo "data_dir=$DATA"
    echo "git=$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    du -sh "$OUT" 2>/dev/null | awk '{print "size=" $1}'
} > "$OUT/MANIFEST"

# Prune: directories older than KEEP days (never the one just written)
find "$DEST" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP" ! -name "$TS" -exec rm -rf {} +
echo "Backup written: $OUT"
