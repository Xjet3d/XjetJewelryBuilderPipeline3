# Deploying Pipeline 3

> **Production** (`atelier.xjet3d.com` + `admin.atelier.xjet3d.com`, no `/JewelryB2C3/` prefix): see
> `deploy/production/README.md`, `.env.production.example` and `docs/PRODUCTION-READINESS-HANDOFF.md`. Nothing below
> changes for proto, which stays the staging copy; `scripts/deploy-proto.sh` is its deploy procedure (in-flight
> check, backup, fast-forward to pushed `main`, restart, health, `scripts/verify-production.sh`).

```
browser ── http://proto/JewelryB2C3/ ──► proto nginx ──► http://tron/JewelryB2C3/ ──► tron nginx ──► 127.0.0.1:8340 (systemd)
```

P3 is operationally separate from Pipeline 2: its own host (tron), process, port, data and tokens. proto only forwards one path to it.

## tron

| Item | Value |
|---|---|
| Checkout | `/home/yakir/git/XjetJewelryBuilderPipeline3` (branch `main`) |
| Python | `.venv` created with `python3 -m venv .venv` (Python 3.12) |
| Config | `.env` in the checkout (never committed): `P3_PROVIDER=mock`, `P3_BASE_PATH=/JewelryB2C3`, `P3_ADMIN_KEY=<random>` |
| Data | `var/` in the checkout (`pipeline3.db`, `accounts.db`, `assets/`, `dev/`, `runtime.json`) |
| Service | `xjet-jewelry-b2c3.service` (`deploy/tron/xjet-jewelry-b2c3.service`), 127.0.0.1:**8340** |
| nginx | `/etc/nginx/snippets/jewelryb2c3.conf` (`deploy/tron/jewelryb2c3.conf`), included from `sites-enabled/dov-hello` (:80) and `conf.d/packtical-ssl.conf` (:443) |
| Health | `http://tron/JewelryB2C3/api/health` |
| Tools Hub | "XJet Atelier Pipeline 3" · Service · `/JewelryB2C3/` · port 8340 |

### Update to the latest `main`

```bash
ssh tron 'bash -s' < scripts/deploy-proto.sh        # or, on tron: bash scripts/deploy-proto.sh
```

It refuses while generation jobs are in flight, backs up `var/` (`~/p3-backups/<timestamp>/`, `scripts/backup.sh`),
fast-forwards to `origin/main` (never a local commit), restarts the service, waits for the health answer and runs
the read-only checks. By hand, the same steps are `git pull --ff-only`, `uv pip install --python .venv/bin/python -r
requirements.txt`, `sudo systemctl restart xjet-jewelry-b2c3.service`, `curl -s http://127.0.0.1:8340/JewelryB2C3/api/health`.

### Access tokens

```bash
cd ~/git/XjetJewelryBuilderPipeline3 && .venv/bin/python -m p3.cli create-token --label "Name"
```

### Mock / live

The service starts in mock mode, which makes no paid calls. Switch modes from the Home footer: click **Developer** and enter the server's `P3_ADMIN_KEY`. Live mode also needs `FAL_KEY` in `.env` and a typed cost confirmation.

## proto

`deploy/proto/setup-proto.sh` (run with sudo on proto) makes exactly two insertions:
- **nginx:** installs `deploy/proto/jewelryb2c3-proxy.conf` as `/etc/nginx/snippets/jewelryb2c3-proxy.conf`, and adds one `include` line before the default `location /` in `/etc/nginx/sites-available/jewelry-b2c`.
- **portal:** adds the JewelryB2C3 tile (`deploy/proto/portal-tile.html`) after JewelryB2C2 in `/var/www/html/index.html`.

It backs up all three files to `/var/backups/p3-proto-<timestamp>/` and refuses to edit unless each anchor occurs exactly once. It reloads nginx only if `nginx -t` passes; otherwise it restores the backups automatically. It is idempotent, and `--rollback <backup dir>` undoes it.

**Do not run Pipeline 2's `nginx-install.sh` on proto.** proto's live `jewelry-b2c` site has hand-added routes the script does not know about, such as `/pendant/` (Pendant Maker on :8011) and the `/amulette` redirects, and the script rewrites the whole file.

## Rollback

- **proto:**
  ```bash
  sudo bash ~/p3-proto/setup-proto.sh --rollback /var/backups/p3-proto-<timestamp>
  ```
- **tron:**
  ```bash
  sudo systemctl disable --now xjet-jewelry-b2c3.service
  ```
  Then remove the two `include` lines and the snippet, run `nginx -t`, and reload.
- Pipeline 2 is unaffected either way.
