#!/usr/bin/env bash
# gallery-sync.sh — proto's approved Inspiration Gallery onto another Pipeline 3 site, and nothing else (p3/gallerysync.py:
# XJet's own published masters, their images, 360° movies and the thumbnails / posters / clips made of them, and only the
# rows the gallery and the homepage story need — never accounts, sessions, customers' designs, orders, quotes or usage).
#
# atelier is reached over HTTPS only, so the bundle travels the way atelier's code does: through the GitHub repository,
# as the tree of a commit at refs/gallery/proto — a ref that clones and normal fetches never download — and atelier's
# auto-deploy (scripts/auto-deploy-atelier.sh) imports it at its next tick, after the code.
#
#   bash scripts/gallery-sync.sh publish           on a developer machine with ssh to proto's server and push access to
#                                                  GitHub: export on proto, check, commit to refs/gallery/proto, push
#   bash scripts/gallery-sync.sh pull              on the target (atelier's auto-deploy runs it every tick): when the ref
#                                                  moved since the last import: plan, back up, import, check the local site
#   bash scripts/gallery-sync.sh export DIR        on the source: write a bundle directory
#   bash scripts/gallery-sync.sh import DIR        on a target with a shell: plan, back up, import, check the local site
#   bash scripts/gallery-sync.sh verify BASE_URL [N]   from anywhere: the gallery (N tiles), the homepage story, its files
#
# Environment: P3_GALLERY_SOURCE (proto), P3_GALLERY_REF (refs/gallery/<source>), P3_GALLERY_SOURCE_HOST (tron),
# P3_GALLERY_SOURCE_CHECKOUT (/home/yakir/git/XjetJewelryBuilderPipeline3), P3_GALLERY_SOURCE_DATA_DIR (the source app's own
# setting), P3_LOCAL_URL (http://127.0.0.1:8340/JewelryB2C3),
# P3_DATA_DIR (the site's data directory; default: the app's own setting), P3_PYTHON.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
SOURCE="${P3_GALLERY_SOURCE:-proto}"
REF="${P3_GALLERY_REF:-refs/gallery/$SOURCE}"
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

# Plan (stops on any row this site made itself), back up, import, look at the local site. Every step must succeed.
Import() {
    local Bundle="$1" Ref="${2:-}" Data
    Data="$(DataDir)" || return 1
    echo "plan for $Data:"
    "$PY" -m p3.gallerysync import "$Bundle" --data-dir "$Data" || return 1
    P3_DATA_DIR="$Data" bash scripts/backup.sh | sed 's/^/  backup: /' || return 1
    "$PY" -m p3.gallerysync import "$Bundle" --data-dir "$Data" --apply ${Ref:+--ref "$Ref"} || return 1
    "$PY" -m p3.gallerysync verify "$LOCAL" || echo "(the local site's checks above failed; the import stays — see p3/gallerysync.py)"
    return 0
}

Cmd="${1:-}"
case "$Cmd" in
  export)
    Out="${2:?usage: gallery-sync.sh export DIR}"
    "$PY" -m p3.gallerysync export "$Out" --source "$SOURCE" --commit "$(git rev-parse --short HEAD 2>/dev/null || echo unknown)" \
        ${P3_DATA_DIR:+--data-dir "$P3_DATA_DIR"}
    ;;

  import)
    Import "${2:?usage: gallery-sync.sh import DIR}"
    ;;

  verify)
    "$PY" -m p3.gallerysync verify "${2:?usage: gallery-sync.sh verify BASE_URL [N]}" ${3:+--expect "$3"}
    ;;

  pull)
    # Quiet unless there is something new: no bundle published, GitHub unreachable or the same commit → nothing to do
    timeout 900 git fetch -q origin "+$REF:$REF" 2>/dev/null || exit 0
    New="$(git rev-parse "$REF^{commit}")"
    [[ "$(cat .gallery-sync.last 2>/dev/null || true)" == "$New" ]] && exit 0
    [[ "$(cat .gallery-sync.bad 2>/dev/null || true)" == "$New" ]] && exit 0
    echo "importing $SOURCE's gallery ${New:0:7}: $(git log -1 --format=%s "$New")"
    Data="$(DataDir)"
    Tmp="$(mktemp -d "$Data/.gallery-import.XXXXXX")"
    trap 'rm -rf "$Tmp"' EXIT
    git archive --format=tar "$New" | tar -x -C "$Tmp"
    if Import "$Tmp" "$New"; then
        echo "$New" > .gallery-sync.last
        rm -f .gallery-sync.bad
    else
        echo "$New" > .gallery-sync.bad
        echo "the import of ${New:0:7} did not complete: the site keeps what it had; it is tried again when $REF moves" >&2
        exit 1
    fi
    ;;

  publish)
    HostS="${P3_GALLERY_SOURCE_HOST:-tron}"
    Checkout="${P3_GALLERY_SOURCE_CHECKOUT:-/home/yakir/git/XjetJewelryBuilderPipeline3}"
    Remote="/tmp/p3-gallery-export-$$"
    Local="$(mktemp -d)"
    trap 'rm -rf "$Local"; ssh "$HostS" "rm -rf $Remote" >/dev/null 2>&1 || true' EXIT
    SrcData="${P3_GALLERY_SOURCE_DATA_DIR:+--data-dir '$P3_GALLERY_SOURCE_DATA_DIR'}"
    echo "1. export on $SOURCE ($HostS:$Checkout)"
    ssh "$HostS" "cd '$Checkout' && .venv/bin/python -m p3.gallerysync export '$Remote' --source '$SOURCE' $SrcData --commit \"\$(git rev-parse --short HEAD 2>/dev/null || echo unknown)\""
    echo "2. copy"
    ssh "$HostS" "tar -C '$Remote' -cf - ." | tar -C "$Local" -xf -
    echo "3. check"
    "$PY" -m p3.gallerysync check "$Local"
    GitDir="$(git rev-parse --absolute-git-dir)"
    git fetch -q origin "+$REF:$REF" 2>/dev/null || true
    Parent="$(git rev-parse -q --verify "$REF^{commit}" 2>/dev/null || true)"
    ContentId() { "$PY" -c 'import json,sys; print(json.load(sys.stdin)["content_id"])'; }
    NewId="$(ContentId < "$Local/manifest.json")"
    if [[ -n "$Parent" && "$(git show "$Parent:manifest.json" | ContentId)" == "$NewId" ]]; then
        echo "unchanged since ${Parent:0:7}: nothing to publish"
        exit 0
    fi
    echo "4. commit $REF"
    Index="$GitDir/gallery-sync.index"
    rm -f "$Index"
    ( cd "$Local" && GIT_INDEX_FILE="$Index" git --git-dir="$GitDir" --work-tree=. -c core.autocrlf=false -c core.safecrlf=false add -A -f . )
    Tree="$(GIT_INDEX_FILE="$Index" git --git-dir="$GitDir" write-tree)"
    rm -f "$Index"
    Want="$(find "$Local" -type f | wc -l)"
    Got="$(git ls-tree -r --name-only "$Tree" | wc -l)"
    [[ "$Want" -eq "$Got" ]] || { echo "the commit has $Got files, the bundle $Want: not published" >&2; exit 1; }
    Items="$("$PY" -c 'import json,sys; M = json.load(sys.stdin); print(len(M["items"]), "gallery items,", M["counts"]["files"], "files")' < "$Local/manifest.json")"
    Commit="$(git commit-tree "$Tree" ${Parent:+-p "$Parent"} -m "Gallery from $SOURCE: $Items (content ${NewId:0:12})")"
    git update-ref "$REF" "$Commit"
    echo "5. push $REF (${Commit:0:7})"
    git push -q origin "$Commit:$REF"
    echo "Published. atelier imports it at its next auto-deploy tick (within a few minutes); then:"
    echo "  bash scripts/gallery-sync.sh verify https://xjetatelier.xjet3d.com/JewelryB2C3"
    ;;

  *)
    sed -n '2,21p' "$0"
    exit 2
    ;;
esac
