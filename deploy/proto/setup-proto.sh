#!/usr/bin/env bash
# setup-proto.sh — add Pipeline 3 to proto: one nginx include + one portal tile, and the tron CA the proxy snippet
# verifies tron's certificate with (proto reaches tron over HTTPS since tron's HTTPS policy of 2026-10-11).
#
#   sudo bash setup-proto.sh            install (idempotent)
#   sudo bash setup-proto.sh --rollback /var/backups/p3-proto-<timestamp>
#
# Deliberately does NOT run Pipeline 2's nginx-install.sh: proto's live jewelry-b2c site has
# hand-added routes (e.g. /pendant/) that the P2 script does not know about and would remove.
# Instead this makes two targeted insertions, each at an anchor that must occur exactly once,
# backs up every file first, and only reloads nginx if `nginx -t` passes (else restores).

set -euo pipefail

SITE=/etc/nginx/sites-available/jewelry-b2c
PORTAL=/var/www/html/index.html
SNIPPET=/etc/nginx/snippets/jewelryb2c3-proxy.conf
CA=/etc/nginx/tls/xjet-tron-root-ca.crt
# XJet Tron Internal Root CA (published at http://tron/tron-ca.crt); pinned so a tampered download is refused.
# If tron's CA is ever replaced, update this fingerprint after checking it with the tron owner.
CA_SHA256="38:96:1F:AC:2B:E5:0A:7F:58:51:8B:BD:9B:9F:6D:FE:A5:67:7C:F5:85:FB:03:D1:9C:01:42:D9:D4:ED:4A:E6"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }

restore() {
    local B="$1"
    [[ -f "$B/jewelry-b2c" && -f "$B/index.html" ]] || { echo "Not a backup dir: $B" >&2; exit 1; }
    cp -a "$B/jewelry-b2c" "$SITE"
    cp -a "$B/index.html" "$PORTAL"
    if [[ -f "$B/snippet.absent" ]]; then rm -f "$SNIPPET"; elif [[ -f "$B/jewelryb2c3-proxy.conf" ]]; then cp -a "$B/jewelryb2c3-proxy.conf" "$SNIPPET"; fi
    if [[ -f "$B/ca.absent" ]]; then rm -f "$CA"; elif [[ -f "$B/xjet-tron-root-ca.crt" ]]; then cp -a "$B/xjet-tron-root-ca.crt" "$CA"; fi
    nginx -t && systemctl reload nginx
    echo "Restored from $B and reloaded nginx."
}

if [[ "${1:-}" == "--rollback" ]]; then restore "${2:?backup dir}"; exit 0; fi

TS=$(date +%Y%m%d-%H%M%S)
B=/var/backups/p3-proto-$TS
mkdir -p "$B"
cp -a "$SITE" "$B/jewelry-b2c"
cp -a "$PORTAL" "$B/index.html"
if [[ -f "$SNIPPET" ]]; then cp -a "$SNIPPET" "$B/"; else touch "$B/snippet.absent"; fi
if [[ -f "$CA" ]]; then cp -a "$CA" "$B/"; else touch "$B/ca.absent"; fi
echo "Backups: $B"

# The tron CA: downloaded from tron (the CA download is exempt from tron's HTTPS redirect), checked against the pin
Tmp=$(mktemp)
curl -fsS --max-time 20 -H "Host: tron" http://172.16.10.32/tron-ca.crt -o "$Tmp"
Got=$(openssl x509 -in "$Tmp" -noout -fingerprint -sha256 | sed 's/^.*=//' | tr 'a-f' 'A-F')
if [[ "$Got" != "$CA_SHA256" ]]; then
    rm -f "$Tmp"; echo "tron CA fingerprint mismatch ($Got) — nothing changed" >&2; exit 1
fi
install -d -m 0755 "$(dirname "$CA")"
install -m 0644 "$Tmp" "$CA"; rm -f "$Tmp"
echo "tron CA installed: $CA"

install -m 0644 "$HERE/jewelryb2c3-proxy.conf" "$SNIPPET"

python3 - "$SITE" "$PORTAL" "$HERE/portal-tile.html" <<'PY'
import sys
Site, Portal, TileFile = sys.argv[1:4]
Include = "    include /etc/nginx/snippets/jewelryb2c3-proxy.conf;\n"

S = open(Site, encoding="utf-8", newline="").read()       # keep the file's own line endings
if "snippets/jewelryb2c3-proxy.conf" in S:
    print("nginx: include already present")
else:
    Nl = "\r\n" if "\r\n" in S else "\n"
    Anchor = "    # Default - serve static files or return 404" + Nl
    if S.count("server {") != 1 or S.count(Anchor) != 1:
        sys.exit(f"nginx: expected one server block and one '{Anchor.strip()}' line; refusing to edit")
    S = S.replace(Anchor, "    # XJet Atelier Pipeline 3 -> tron (owned by the Pipeline 3 repo)" + Nl
                  + Include.rstrip("\n") + Nl + Nl + Anchor)
    open(Site, "w", encoding="utf-8", newline="").write(S)
    print("nginx: include added")

P = open(Portal, encoding="utf-8", newline="").read()
if 'href="/JewelryB2C3/"' in P:
    print("portal: tile already present")
else:
    Nl = "\r\n" if "\r\n" in P else "\n"
    Start = P.find('<a class="card" href="/JewelryB2C2/">')
    if Start < 0 or P.count('href="/JewelryB2C2/"') != 1:
        sys.exit("portal: JewelryB2C2 card not found exactly once; refusing to edit")
    End = P.index("</a>", Start) + len("</a>")
    Tile = open(TileFile, encoding="utf-8").read().strip("\n").replace("\n", Nl)
    P = P[:End] + Nl + Nl + Tile + P[End:]
    open(Portal, "w", encoding="utf-8", newline="").write(P)
    print("portal: tile added after JewelryB2C2")
PY

if nginx -t; then
    systemctl reload nginx
    sleep 1
    # Smoke test through proto's own nginx: Pipeline 3 answers on proto itself, over https and http, with no redirect
    Health=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 20 --resolve proto:443:127.0.0.1 https://proto/JewelryB2C3/api/health)
    Loc=$(curl -sk -o /dev/null -w "%{redirect_url}" --max-time 20 --resolve proto:443:127.0.0.1 https://proto/JewelryB2C3/)
    PlainLoc=$(curl -s -o /dev/null -w "%{redirect_url}" --max-time 20 -H "Host: proto" http://127.0.0.1/JewelryB2C3/)
    if [[ "$Health" != 200 || -n "$Loc" || -n "$PlainLoc" ]]; then
        echo "Smoke test FAILED (health $Health, https redirect '$Loc', http redirect '$PlainLoc') — restoring backups" >&2
        restore "$B"
        exit 1
    fi
    echo "Smoke test OK: https://proto/JewelryB2C3/ and http://proto/JewelryB2C3/ answer on proto (no redirect to tron)."
    echo "nginx reloaded. Rollback: sudo bash $0 --rollback $B"
else
    echo "nginx -t FAILED — restoring backups" >&2
    restore "$B"
    exit 1
fi
