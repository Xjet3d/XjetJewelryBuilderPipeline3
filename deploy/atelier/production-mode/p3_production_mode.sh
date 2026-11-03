#!/usr/bin/env bash
# Atelier: Pipeline 3 in production mode (P3_ENV=production) — for Dov, run as dov on the Atelier server (3 Nov 2026).
# It arrives with the release; run it from the checkout, in the order of README.txt beside it:
#
#   bash deploy/atelier/production-mode/p3_production_mode.sh check            read-only: the .env changes and a dry run
#   bash deploy/atelier/production-mode/p3_production_mode.sh enroll-owner     after the owner's Google sign-in attempt
#   bash deploy/atelier/production-mode/p3_production_mode.sh apply            backup, new .env, restart, checks; rolls back
#   bash deploy/atelier/production-mode/p3_production_mode.sh rollback <dir>   the .env from that backup, restart
set -eu
Here="$(cd "$(dirname "$0")" && pwd)"
Checkout="${P3_CHECKOUT:-/home/dov/git/XjetJewelryBuilderPipeline3}"
[ -f "$Here/p3_production_mode.py" ] || { echo "p3_production_mode.py must be next to this script"; exit 2; }
[ -x "$Checkout/.venv/bin/python" ] || { echo "no virtualenv at $Checkout/.venv"; exit 2; }
exec "$Checkout/.venv/bin/python" "$Here/p3_production_mode.py" "$Checkout" "$@"
