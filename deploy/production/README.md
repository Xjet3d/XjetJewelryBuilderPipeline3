# Production deployment — XJet Atelier

Target architecture (approved direction; **no DNS, certificate or server change is made by the repository** — these
files are templates for the person who sets the production host up, see `docs/PRODUCTION-READINESS-HANDOFF.md`):

```
customers ── https://atelier.xjet3d.com/        ──► nginx ──► 127.0.0.1:8340  (xjet-atelier.service, one worker)
XJet staff ── https://admin.atelier.xjet3d.com/  ──► nginx ──► 127.0.0.1:8340  (the same process; the app answers
                                                                 the Admin only on P3_ADMIN_HOST)
```

- The customer site is served at the **root** of its own host (`P3_BASE_PATH` empty) — no `/JewelryB2C3/` prefix.
- The Admin, its API, the developer tools and the API docs do not exist on the public host: the app answers them only
  when the request's host is `P3_ADMIN_HOST`, and `P3_ENV=production` removes the developer tools, the API docs and the
  showcase prototype altogether. nginx adds the same refusal in front (defence in depth).
- Data (`P3_DATA_DIR`) lives **outside the code checkout**, e.g. `/srv/atelier/data`; the checkout is replaced on
  deploy, the data is backed up (`scripts/backup.sh`).
- Secrets live in `/etc/xjet-atelier/env` (mode 0600, owner root, readable by the service through `EnvironmentFile`),
  never in the checkout and never in this repository.

## Files

| File | Install as |
|---|---|
| `xjet-atelier.service` | `/etc/systemd/system/xjet-atelier.service` |
| `nginx-atelier.conf` | `/etc/nginx/sites-available/atelier` → symlink in `sites-enabled/` |
| `../../.env.production.example` | the template for `/etc/xjet-atelier/env` (fill in, never commit) |
| `../../scripts/backup.sh`, `restore.sh`, `verify-production.sh` | `/srv/atelier/app/scripts/` (part of the checkout) |

## First installation (one time)

```bash
# 1. a service user, the directories
sudo useradd --system --home /srv/atelier --shell /usr/sbin/nologin atelier
sudo mkdir -p /srv/atelier/app /srv/atelier/data /srv/atelier/backups /etc/xjet-atelier
sudo chown -R atelier:atelier /srv/atelier

# 2. the code (a tagged release of main) and its environment
sudo -u atelier git clone https://github.com/Xjet3d/XjetJewelryBuilderPipeline3.git /srv/atelier/app
cd /srv/atelier/app && sudo -u atelier python3 -m venv .venv && sudo -u atelier .venv/bin/pip install -r requirements.txt

# 3. the configuration: copy the example, fill every REQUIRED value, keep it private
sudo cp /srv/atelier/app/.env.production.example /etc/xjet-atelier/env
sudo chmod 0600 /etc/xjet-atelier/env && sudo chown root:atelier /etc/xjet-atelier/env && sudo chmod 0640 /etc/xjet-atelier/env
sudoedit /etc/xjet-atelier/env

# 4. the service refuses to start with development defaults (P3_ENV=production validates the configuration):
sudo install -m 0644 deploy/production/xjet-atelier.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now xjet-atelier.service
sudo journalctl -u xjet-atelier -n 50 --no-pager          # "refuses to start with development defaults" lists what is missing

# 5. nginx + TLS (certbot or the company's certificates), then
sudo install -m 0644 deploy/production/nginx-atelier.conf /etc/nginx/sites-available/atelier
sudo ln -sf /etc/nginx/sites-available/atelier /etc/nginx/sites-enabled/atelier
sudo nginx -t && sudo systemctl reload nginx

# 6. backups every night, verification after every deploy
sudo install -m 0644 deploy/production/xjet-atelier-backup.timer deploy/production/xjet-atelier-backup.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now xjet-atelier-backup.timer
bash scripts/verify-production.sh https://atelier.xjet3d.com https://admin.atelier.xjet3d.com
```

## Every deploy

```bash
cd /srv/atelier/app
sudo -u atelier bash scripts/backup.sh                       # databases + assets, before anything changes
sudo -u atelier git fetch --tags && sudo -u atelier git checkout <release tag>     # a tag of main, tested on proto first
sudo -u atelier .venv/bin/pip install -r requirements.txt
sudo systemctl restart xjet-atelier.service
bash scripts/verify-production.sh https://atelier.xjet3d.com https://admin.atelier.xjet3d.com
```

Rollback: `git checkout <previous tag>` + restart; if a database migration must be undone, `scripts/restore.sh <backup dir>`.

## What the app enforces at start (P3_ENV=production)

`p3/settings.py: ValidateProduction` refuses to start unless: the provider is fal with `FAL_KEY`; `P3_PUBLIC_BASE_URL` is an
https origin; `P3_ADMIN_HOST` is set and differs from the public host; `P3_BASE_PATH` is empty; `P3_ADMIN_KEY` and
`P3_SIGNING_SECRET` are two different random secrets of 24+ characters; `P3_ALLOW_UNAPPROVED_PRICING` is off;
`P3_MAIL_MODE=smtp`; `P3_DAILY_AI_SPEND_CAP_USD` is set; `P3_SUPPORT_EMAIL` is set; `P3_DATA_DIR` is outside the checkout.
Production also locks the AI mode to the configuration (no developer switch, no mock fallback), hides the developer
tools, the API docs and the `/showcase` reference page (the homepage hero's data stays public), and indexes the site (`robots.txt`, the robots meta, the sitemap).
