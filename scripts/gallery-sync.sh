#!/usr/bin/env bash
# gallery-sync.sh — proto's approved Inspiration Gallery onto another Pipeline 3 site, and nothing else (p3/gallerysync.py:
# XJet's own published masters, their images, 360° movies and the thumbnails / posters / clips made of them, and only the
# rows the gallery and the homepage story need — never accounts, sessions, customers' designs, orders, quotes or usage).
#
# atelier is reached over HTTPS only: the bundle goes to atelier's own Admin API (its Admin key), in pieces small enough
# for Cloudflare and nginx, and only the files atelier does not have yet are sent. Never through GitHub: the repository is
# public, and a bundle holds what the site keeps private (prompts, refinement words, reference images).
#
#   bash scripts/gallery-sync.sh push BASE_URL     from a machine with ssh to proto's server (tron) and HTTPS to the site:
#                                                  export on proto, check, send, plan, import (after a database snapshot
#                                                  on the site), then check the site. Asks for the site's Admin key, or
#                                                  reads it from the file P3_TARGET_ADMIN_KEY_FILE names.
#   bash scripts/gallery-sync.sh export DIR        on the source: write a bundle directory
#   bash scripts/gallery-sync.sh import DIR        on a target with a shell: plan, back up, import, check the local site
#   bash scripts/gallery-sync.sh verify BASE_URL [N]   from anywhere: the gallery (N tiles), the homepage story, its files
#
# Environment: P3_GALLERY_SOURCE (proto), P3_GALLERY_SOURCE_HOST (tron; "local" exports here), P3_GALLERY_SOURCE_CHECKOUT
# (/home/yakir/git/XjetJewelryBuilderPipeline3), P3_GALLERY_SOURCE_DATA_DIR (the source app's own setting),
# P3_TARGET_ADMIN_KEY_FILE, P3_LOCAL_URL (http://127.0.0.1:8340/JewelryB2C3), P3_DATA_DIR (the site's data directory;
# default: the app's own setting), P3_PYTHON.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
SOURCE="${P3_GALLERY_SOURCE:-proto}"
LOCAL="${P3_LOCAL_URL:-http://127.0.0.1:8340/JewelryB2C3}"
PY="${P3_PYTHON:-}"
if [[ -z "$PY" ]]; then
    for C in .venv/bin/python .venv/Scripts/python.exe; do
        if [[ -x "$C" ]]; then PY="$C"; break; fi
    done
fi
[[ -n "$PY" ]] || { echo "No Python found in .venv: set P3_PYTHON" >&2; exit 1; }

DataDir() {
    if [[ -n "${P3_DATA_DIR:-}" ]]; then echo "$P3_DATA_DIR"; return 0; fi
    "$PY" -c 'from p3.settings import LoadSettings; print(LoadSettings().DataDir)'
}

Cmd="${1:-}"
case "$Cmd" in
  export)
    Out="${2:?usage: gallery-sync.sh export DIR}"
    "$PY" -m p3.gallerysync export "$Out" --source "$SOURCE" --commit "$(git rev-parse --short HEAD 2>/dev/null || echo unknown)" \
        ${P3_DATA_DIR:+--data-dir "$P3_DATA_DIR"}
    ;;

  import)
    Bundle="${2:?usage: gallery-sync.sh import DIR}"
    Data="$(DataDir)"
    echo "plan for $Data:"
    "$PY" -m p3.gallerysync import "$Bundle" --data-dir "$Data"           # stops on any row this site made itself
    P3_DATA_DIR="$Data" bash scripts/backup.sh | sed 's/^/  backup: /'
    "$PY" -m p3.gallerysync import "$Bundle" --data-dir "$Data" --apply
    "$PY" -m p3.gallerysync verify "$LOCAL"
    ;;

  push)
    Target="${2:?usage: gallery-sync.sh push BASE_URL   (e.g. https://xjetatelier.xjet3d.com/JewelryB2C3)}"
    HostS="${P3_GALLERY_SOURCE_HOST:-tron}"
    Checkout="${P3_GALLERY_SOURCE_CHECKOUT:-/home/yakir/git/XjetJewelryBuilderPipeline3}"
    Local="$(mktemp -d)"
    Remote="/tmp/p3-gallery-export-$$"
    if [[ "$HostS" == "local" ]]; then
        trap 'rm -rf "$Local"' EXIT
        echo "1. export on $SOURCE (here)"
        "$PY" -m p3.gallerysync export "$Local/bundle" --source "$SOURCE" --commit "$(git rev-parse --short HEAD 2>/dev/null || echo unknown)" \
            ${P3_GALLERY_SOURCE_DATA_DIR:+--data-dir "$P3_GALLERY_SOURCE_DATA_DIR"}
    else
        trap 'rm -rf "$Local"; ssh "$HostS" "rm -rf $Remote" >/dev/null 2>&1 || true' EXIT
        SrcData="${P3_GALLERY_SOURCE_DATA_DIR:+--data-dir '$P3_GALLERY_SOURCE_DATA_DIR'}"
        echo "1. export on $SOURCE ($HostS:$Checkout)"
        ssh "$HostS" "cd '$Checkout' && .venv/bin/python -m p3.gallerysync export '$Remote' --source '$SOURCE' $SrcData --commit \"\$(git rev-parse --short HEAD 2>/dev/null || echo unknown)\""
        echo "2. copy"
        mkdir -p "$Local/bundle"
        ssh "$HostS" "tar -C '$Remote' -cf - ." | tar -C "$Local/bundle" -xf -
    fi
    echo "3. send to $Target, plan, import, check"
    "$PY" -m p3.gallerysync push "$Local/bundle" "$Target"
    ;;

  verify)
    "$PY" -m p3.gallerysync verify "${2:?usage: gallery-sync.sh verify BASE_URL [N]}" ${3:+--expect "$3"}
    ;;

  *)
    sed -n '2,21p' "$0"
    exit 2
    ;;
esac
