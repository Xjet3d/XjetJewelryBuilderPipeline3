#!/usr/bin/env bash
# verify-production.sh — what a deploy must show before anyone calls it done. Read-only HTTP checks, no sign-in.
#
#   bash scripts/verify-production.sh https://atelier.xjet3d.com https://admin.atelier.xjet3d.com
#   bash scripts/verify-production.sh http://proto/JewelryB2C3            (a staging copy: the Admin is on the same host)
set -uo pipefail

PUBLIC="${1:?public origin, e.g. https://atelier.xjet3d.com}"
ADMIN="${2:-}"
Fail=0

check() {          # check <label> <condition result 0/1>
    if [[ "$2" -eq 0 ]]; then echo "  ok   $1"; else echo "  FAIL $1"; Fail=1; fi
}
code() { curl -s -o /dev/null -w "%{http_code}" "$@"; }
head_() { curl -s -D - -o /dev/null "$@"; }

echo "Public site: $PUBLIC"
H="$(head_ "$PUBLIC/")"
check "home page answers 200"                 "$([[ "$(code "$PUBLIC/")" == 200 ]]; echo $?)"
check "security headers present"              "$(grep -qi "x-content-type-options: nosniff" <<<"$H" && grep -qi "content-security-policy:" <<<"$H" && grep -qi "x-frame-options: DENY" <<<"$H"; echo $?)"
if [[ "$PUBLIC" == https://* ]]; then
    check "HSTS present (https)"              "$(grep -qi "strict-transport-security:" <<<"$H"; echo $?)"
fi
Health="$(curl -s "$PUBLIC/api/health")"
check "health says ok"                        "$(grep -q '"ok": *true' <<<"$Health"; echo $?)"
if [[ "$PUBLIC" == https://* ]]; then
    check "health is minimal in production"   "$(! grep -q '"mode"' <<<"$Health"; echo $?)"
    check "robots.txt allows the site"        "$(curl -s "$PUBLIC/robots.txt" | grep -q "^Allow: /"; echo $?)"
    check "sitemap answers 200"               "$([[ "$(code "$PUBLIC/sitemap.xml")" == 200 ]]; echo $?)"
    for P in /admin/ /api/admin/session /dev /api/dev/mode /docs /openapi.json /redoc /showcase /api/showcase; do
        check "public host has no $P (404)"   "$([[ "$(code "$PUBLIC$P")" == 404 ]]; echo $?)"
    done
else
    check "robots.txt disallows a staging copy" "$(curl -s "$PUBLIC/robots.txt" | grep -q "^Disallow: /$"; echo $?)"
fi
check "catalog answers 200"                   "$([[ "$(code "$PUBLIC/api/catalog")" == 200 ]]; echo $?)"
check "gallery answers 200"                   "$([[ "$(code "$PUBLIC/api/gallery")" == 200 ]]; echo $?)"
check "a missing thumbnail is not cached"     "$(head_ "$PUBLIC/thumb/designs/nope/candidates/nope.png?w=320" | grep -qi "cache-control: no-store"; echo $?)"

if [[ -n "$ADMIN" ]]; then
    echo "Admin host: $ADMIN"
    check "Admin page answers 200"            "$([[ "$(code "$ADMIN/admin/")" == 200 ]]; echo $?)"
    check "Admin API wants a sign-in (403)"   "$([[ "$(code "$ADMIN/api/admin/session")" == 403 ]]; echo $?)"
    check "Admin host is not indexed"         "$(head_ "$ADMIN/admin/" | grep -qi "x-robots-tag: noindex"; echo $?)"
fi

if [[ "$Fail" -eq 0 ]]; then echo "All checks passed."; else echo "Some checks FAILED." >&2; fi
exit "$Fail"
