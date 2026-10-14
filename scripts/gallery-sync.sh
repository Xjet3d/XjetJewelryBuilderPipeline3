#!/usr/bin/env bash
# gallery-sync.sh — proto's approved Inspiration Gallery onto another Pipeline 3 site, and nothing else (p3/gallerysync.py:
# XJet's own published masters, their images, 360° movies and the thumbnails / posters / clips made of them, and only the
# rows the gallery and the homepage story need — never accounts, sessions, customers' designs, orders, quotes or usage).
#
# atelier's Admin is not public (since 2026-10-14 its public domain answers 404 for it, by design), and once
# administrator sign-in is enforced no key opens it over the web (docs/AUTH-OPERATIONS-AJW-39.md). So a push to atelier
# goes over SSH (P3_TARGET_SSH=atelier): the bundle is copied to the site's server and that server's own `import` puts
# it in — no Admin key, no Admin API, never the public URL. A site whose Admin API does answer can take the bundle there
# instead (its Admin key, in pieces small enough for Cloudflare and nginx, only the files it does not have yet). Never
# through GitHub: the repository is public, and a bundle holds what the site keeps private (prompts, refinement words,
# reference images).
#
#   P3_TARGET_SSH=atelier bash scripts/gallery-sync.sh push [PUBLIC_URL]
#                                                  from a machine with ssh to proto's server (tron) and to the site's
#                                                  server (P3_TARGET_SSH: its ssh host alias): export on proto, check,
#                                                  copy into a temporary directory on the site's server, then `import`
#                                                  there in the site's checkout (P3_TARGET_CHECKOUT): plan, back up
#                                                  (~/p3-backups), import, check the site; then its public pages at
#                                                  PUBLIC_URL when given. The temporary directory goes at the end.
#   bash scripts/gallery-sync.sh push BASE_URL     for a site whose Admin API answers at BASE_URL, from a machine with
#                                                  ssh to tron and HTTPS to the site: export, check, send, plan, import
#                                                  (after a database snapshot on the site), check the site. Asks for the
#                                                  site's Admin key, or reads it from the file P3_TARGET_ADMIN_KEY_FILE
#                                                  names.
#   bash scripts/gallery-sync.sh export DIR        on the source: write a bundle directory
#   bash scripts/gallery-sync.sh import DIR        on a target with a shell: plan, back up, import, check the local site
#   bash scripts/gallery-sync.sh verify BASE_URL [N]   from anywhere: the gallery (N tiles), the homepage story, its files
#
# Environment: P3_GALLERY_SOURCE (proto), P3_GALLERY_SOURCE_HOST (tron; "local" exports here), P3_GALLERY_SOURCE_CHECKOUT
# (/home/yakir/git/XjetJewelryBuilderPipeline3), P3_GALLERY_SOURCE_DATA_DIR (the source app's own setting),
# P3_TARGET_SSH, P3_TARGET_CHECKOUT (the site's checkout on its server: ~/git/XjetJewelryBuilderPipeline3),
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

# A push over SSH (P3_TARGET_SSH): the bundle goes into a temporary directory on the site's server (TargetDir), and
# the server's own `import` (above) puts it in, in the site's checkout. CleanTarget, in the EXIT trap, removes that
# directory however the script ends. TargetCd: the server shell's way into the checkout (quoted; ~/ is that shell's).
TargetDir=""
TargetCheckout="${P3_TARGET_CHECKOUT:-}"
[[ -n "$TargetCheckout" ]] || TargetCheckout='~/git/XjetJewelryBuilderPipeline3'
TargetCd() {
    if [[ "$TargetCheckout" == "~/"* ]]; then
        printf "cd ~/'%s'" "${TargetCheckout#\~/}"
    else
        printf "cd '%s'" "$TargetCheckout"
    fi
}

PrepareTarget() {
    local Make='mktemp -d "${TMPDIR:-/tmp}/p3-gallery-import.XXXXXX"'
    [[ "$P3_TARGET_SSH" =~ ^[A-Za-z0-9][A-Za-z0-9._@-]*$ && "$TargetCheckout" != *"'"* ]] || {
        echo "P3_TARGET_SSH is the site's ssh host alias (e.g. atelier), P3_TARGET_CHECKOUT a path without quotes" >&2
        exit 2
    }
    echo "0. $P3_TARGET_SSH: the site's checkout ($TargetCheckout), and a temporary directory there for the bundle"
    # The directory is mktemp's line, the last one (a login file on the server may print something before it)
    TargetDir="$(ssh "$P3_TARGET_SSH" "$(TargetCd) && test -f scripts/gallery-sync.sh && $Make" | tail -n 1)" \
        && [[ "$TargetDir" =~ ^/[A-Za-z0-9._/~-]+$ ]] || {
        TargetDir=""
        echo "no way to the site over SSH: check that ssh $P3_TARGET_SSH works and that $TargetCheckout is the" \
             "site's checkout there (P3_TARGET_CHECKOUT)" >&2
        exit 1
    }
}

CleanTarget() {
    if [[ -n "$TargetDir" ]]; then
        ssh "$P3_TARGET_SSH" "rm -rf '$TargetDir'" >/dev/null 2>&1 || true
    fi
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
    Public=""
    if [[ -n "${P3_TARGET_SSH:-}" ]]; then
        Public="${2:-}"                 # optional: the site's public address, only to check its public pages at the end
    else
        Target="${2:?usage: gallery-sync.sh push BASE_URL, or with P3_TARGET_SSH=<ssh host>: push [PUBLIC_URL]}"
    fi
    HostS="${P3_GALLERY_SOURCE_HOST:-tron}"
    Checkout="${P3_GALLERY_SOURCE_CHECKOUT:-/home/yakir/git/XjetJewelryBuilderPipeline3}"
    Local="$(mktemp -d)"
    Remote="/tmp/p3-gallery-export-$$"
    trap 'CleanTarget; rm -rf "$Local"' EXIT
    if [[ -n "${P3_TARGET_SSH:-}" ]]; then
        PrepareTarget                   # first: without a way to the site's server nothing is exported
    fi
    if [[ "$HostS" == "local" ]]; then
        echo "1. export on $SOURCE (here)"
        "$PY" -m p3.gallerysync export "$Local/bundle" --source "$SOURCE" --commit "$(git rev-parse --short HEAD 2>/dev/null || echo unknown)" \
            ${P3_GALLERY_SOURCE_DATA_DIR:+--data-dir "$P3_GALLERY_SOURCE_DATA_DIR"}
    else
        trap 'CleanTarget; rm -rf "$Local"; ssh "$HostS" "rm -rf $Remote" >/dev/null 2>&1 || true' EXIT
        SrcData="${P3_GALLERY_SOURCE_DATA_DIR:+--data-dir '$P3_GALLERY_SOURCE_DATA_DIR'}"
        echo "1. export on $SOURCE ($HostS:$Checkout)"
        ssh "$HostS" "cd '$Checkout' && .venv/bin/python -m p3.gallerysync export '$Remote' --source '$SOURCE' $SrcData --commit \"\$(git rev-parse --short HEAD 2>/dev/null || echo unknown)\""
        echo "2. copy"
        mkdir -p "$Local/bundle"
        ssh "$HostS" "tar -C '$Remote' -cf - ." | tar -C "$Local/bundle" -xf -
    fi
    if [[ -n "${P3_TARGET_SSH:-}" ]]; then
        echo "3. check the bundle"
        "$PY" -m p3.gallerysync check "$Local/bundle"
        echo "4. on $P3_TARGET_SSH: copy into $TargetDir, plan, back up, import, check the site there"
        # One connection copies, imports and removes the temporary directory (ssh asks for a password, if it uses one,
        # here and in step 0 only); CleanTarget tries again only when this connection did not finish
        Backups="P3_BACKUP_DIR=\"\${P3_BACKUP_DIR:-\$HOME/p3-backups}\""     # where the server's auto-deploy keeps them
        tar -C "$Local/bundle" -cf - . | ssh "$P3_TARGET_SSH" "tar -C '$TargetDir' -xf - && $(TargetCd) && $Backups bash scripts/gallery-sync.sh import '$TargetDir'; Rc=\$?; rm -rf '$TargetDir'; exit \$Rc"
        TargetDir=""
        if [[ -n "$Public" ]]; then
            echo "5. check the public pages at $Public"
            "$PY" -m p3.gallerysync verify "$Public"
        else
            echo "5. no PUBLIC_URL given: the public pages were not checked"
        fi
    else
        echo "3. send to $Target, plan, import, check"
        "$PY" -m p3.gallerysync push "$Local/bundle" "$Target"
    fi
    ;;

  verify)
    "$PY" -m p3.gallerysync verify "${2:?usage: gallery-sync.sh verify BASE_URL [N]}" ${3:+--expect "$3"}
    ;;

  *)
    sed -n '2,34p' "$0"
    exit 2
    ;;
esac
