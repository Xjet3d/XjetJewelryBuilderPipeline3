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

## atelier

atelier (`xjetatelier.xjet3d.com`, behind Cloudflare: HTTPS only, no SSH from outside) runs Pipeline 3 under
`/JewelryB2C3/` as a user systemd service in `~/git/XjetJewelryBuilderPipeline3`, next to Pipeline 2's live shop.

### Update procedure

1. **Code: automatic.** `xjet-jewelry-b2c3-autodeploy.timer` (`deploy/atelier/`) runs `scripts/auto-deploy-atelier.sh`
   every 2 minutes. When GitHub `main` moved, it backs up, fast-forwards, restarts, waits for the health answer and rolls
   back if there is none. Whatever is pushed to `main` is live on atelier within minutes. Backups (`scripts/backup.sh`)
   hard-link every unchanged asset file to the previous backup, so a backup at every deploy costs only what changed.
2. **Gallery content: an explicit step after an update, whenever proto's approved Inspiration Gallery changed.** From a
   machine with ssh to tron and HTTPS to atelier:

   ```bash
   bash scripts/gallery-sync.sh push https://xjetatelier.xjet3d.com/JewelryB2C3
   ```

   It exports proto's approved gallery on tron, checks it, and sends it to atelier's own Admin API with atelier's Admin
   key, which it asks for (or reads from the file `P3_TARGET_ADMIN_KEY_FILE` names). The files go in 8 MB pieces, smaller
   if a proxy refuses that size, and only the files atelier does not have yet are sent. atelier plans the import, stops on
   any row it made itself, keeps a snapshot of its database (`<data dir>/gallery-sync/backups`, the newest five), imports,
   and the script then checks the site. Pushing unchanged content sends nothing and changes nothing. In Git Bash on
   Windows the key prompt may not work: put the key in a file and set `P3_TARGET_ADMIN_KEY_FILE`, or run it on tron
   (`ssh -t tron`, then `P3_GALLERY_SOURCE_HOST=local bash scripts/gallery-sync.sh push …` in the checkout).

   The bundle never goes through GitHub: the repository is public, and a bundle holds what the site keeps private
   (prompts, refinement words, reference images).
3. **Check, from anywhere:**

   ```bash
   bash scripts/gallery-sync.sh verify https://xjetatelier.xjet3d.com/JewelryB2C3
   ```

   `/api/gallery` has the tiles, `/api/showcase` tells the homepage story, and every image, the movie, its poster and its
   clip answer. The latest import is in `/api/health` outside production and in `/api/admin/health` (`gallery_sync`:
   source, status, time, bundle, items, or the error).

### What the Gallery sync moves (`p3/gallerysync.py`)

- **Moves:** XJet's own published gallery masters (`owner_kind` xjet, made with the real AI provider), the XJet design a
  variation came from (the homepage story starts there), their batches, their images (ready and failed options, so the
  option letters of every Ring ID stay the same), their ready 360° movies, the files behind them, and the thumbnails,
  posters and clips already made of those files. Only the columns the gallery and `/api/showcase` read.
- **Never moves:** accounts or sign-in tokens, sessions and session events, gallery uses and favourites, customizations,
  bag, orders, quote requests, usage and credits, 3D models, a customer's design (also one published with consent),
  anything made in mock mode or still in progress, who asked for a movie, provider upload URLs.
- **Idempotent.** Rows are matched by id and written only when they differ, files only when their content differs. Every
  row it wrote is recorded on atelier (`gallery_sync_items`; each import in `gallery_sync_runs`). A later import brings
  what changed on proto and takes off atelier's gallery the synced tiles proto no longer publishes; their designs stay,
  because customers may be using them. A tile atelier added itself stays, after the synced ones, which keep proto's order.
  A row on atelier that did not come from a sync stops the import, and nothing is written.
- **IDs and links.** A design keeps proto's Ring ID and share link name when they are free on atelier, and keeps what it
  was given there on every later import.
- **Safety.** The bundle is checked before anything is written: allow-listed tables and columns, safe ids and paths, the
  SHA-256 of every file and of the rows. Files are written atomically with proto's modification time, so a thumbnail or
  poster made on proto counts as current; the rows in one transaction. An import needs twice its size plus 1 GB free. A
  failed import is recorded (`gallery_sync`) and changes nothing. The upload API (`/api/admin/gallery-sync/uploads`)
  needs the site's Admin key and can change gallery content only.
- **By hand, with a shell on the target:** `bash scripts/gallery-sync.sh export DIR` on the source,
  `bash scripts/gallery-sync.sh import DIR` on the target (plan, backup, import, check). `python -m p3.gallerysync import
  DIR` alone is a dry run.

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
