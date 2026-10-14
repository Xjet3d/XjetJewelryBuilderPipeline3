# Deploying Pipeline 3

> **Production** (`atelier.xjet3d.com` + `admin.atelier.xjet3d.com`, no `/JewelryB2C3/` prefix): see
> `deploy/production/README.md`, `.env.production.example` and `docs/PRODUCTION-READINESS-HANDOFF.md`. Nothing below
> changes for proto, which stays the staging copy; `scripts/deploy-proto.sh` is its deploy procedure (in-flight
> check, backup, fast-forward to pushed `main`, restart, health, `scripts/verify-production.sh`).
>
> **Branches (since 2026-10-08):** `main` is development and goes to **proto** only. `atelier-production` is what
> **atelier** runs, and it moves only with the owner's explicit approval of a version reviewed on proto. A push to
> `main`, by anyone, never reaches atelier.

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

atelier (`xjetatelier.xjet3d.com`, behind Cloudflare: the public site is HTTPS only) runs Pipeline 3 under
`/JewelryB2C3/` as the user systemd service `xjet-jewelry-b2c3` in `~/git/XjetJewelryBuilderPipeline3` (127.0.0.1:8340,
data in `var/`), next to Pipeline 2's live shop. Staff reach the server itself with `ssh atelier` (a host alias in
`~/.ssh/config` whose HostName is the server's own address: `xjetatelier.xjet3d.com` is Cloudflare, which carries no
SSH, so an alias pointing there times out).

**The Admin on atelier is private (since 2026-10-14).** On the public domain nginx answers 404 for
`/JewelryB2C3/admin`, `/JewelryB2C3/api/admin/…` and the developer tools, by design. Keep it that way: never expose the
Admin publicly again and never work around it. Administrators open the Admin through an SSH tunnel, as
`docs/AUTH-OPERATIONS-AJW-39.md` §3a describes. Everything else that changes the site goes through the server's own
shell (`ssh atelier`), never the public URL: the gallery push below, and the operator commands of `python -m p3.cli`
(§6 there). The Admin key stays on the server as the break-glass secret: typed at the CLI's prompt, never on a command
line, in shell history or over the web.

### Update procedure

1. **Code: only with the owner's approval.** atelier follows the GitHub branch `atelier-production`, never `main`.
   Development goes to `main` and to proto; the owner reviews it on proto. When the owner approves a version for
   atelier, and only then, that reviewed commit is put on the production branch:

   ```bash
   git push origin <approved commit>:refs/heads/atelier-production
   ```

   This is a fast-forward of what atelier runs: never force it. To undo a version, approve a revert commit instead of
   moving the branch back; the auto-deploy does not go backwards. `xjet-jewelry-b2c3-autodeploy.timer`
   (`deploy/atelier/`) runs `scripts/auto-deploy-atelier.sh` every 2 minutes. When `atelier-production` moved, it backs
   up, fast-forwards, restarts, waits for the health answer and rolls back if there is none; a push to `main` changes
   nothing there. Backups (`scripts/backup.sh`) hard-link every unchanged asset file to the previous backup, so a backup
   at every deploy costs only what changed.
2. **Gallery content: an explicit step, only with the owner's approval, whenever proto's approved Inspiration Gallery
   changed.** From a machine with ssh to tron and to atelier:

   ```bash
   P3_TARGET_SSH=atelier bash scripts/gallery-sync.sh push https://xjetatelier.xjet3d.com/JewelryB2C3
   ```

   It exports proto's approved gallery on tron, checks it, copies it over SSH into a temporary directory on atelier's
   server and runs `scripts/gallery-sync.sh import` there, in atelier's checkout (`P3_TARGET_CHECKOUT`, default
   `~/git/XjetJewelryBuilderPipeline3`): the plan, which stops on any row atelier made itself; a backup into
   `~/p3-backups`, next to the auto-deploy's; the import; a check of the site on its own address. Then it checks the
   public pages at the address given and removes the temporary directory. No Admin key and no Admin API are involved, so
   it works whether or not administrator sign-in is enforced. The running service shows the new gallery at once (it
   reads the gallery from the database on every request; the sitemap follows within a minute): nothing is restarted.
   The whole bundle is copied each time, and only what changed is written: pushing unchanged content changes nothing.
   Without a working `ssh atelier` it stops before anything is exported. It connects to atelier twice (the check,
   then the copy with the import), so an SSH password is asked for twice; an SSH key needs none.

   A site whose Admin API does answer (not atelier) can take the bundle over HTTPS instead:
   `bash scripts/gallery-sync.sh push BASE_URL` sends it to that Admin API with the site's Admin key (asked for, or read
   from the file `P3_TARGET_ADMIN_KEY_FILE` names; the prompt may not work in Git Bash on Windows), in 8 MB pieces,
   smaller if a proxy refuses that size, and only the files the site does not have yet; the site keeps a snapshot of its
   database (`<data dir>/gallery-sync/backups`, the newest five) before it imports.

   The bundle never goes through GitHub: the repository is public, and a bundle holds what the site keeps private
   (prompts, refinement words, reference images).
3. **Check, from anywhere:**

   ```bash
   bash scripts/gallery-sync.sh verify https://xjetatelier.xjet3d.com/JewelryB2C3
   ```

   `/api/gallery` has the tiles, `/api/showcase` tells the homepage story, and every image, the movie, its poster and its
   clip answer. The latest import is in `/api/health` outside production, in the Admin's `/api/admin/health`
   (`gallery_sync`: source, status, time, bundle, items, or the error), and on the server in
   `.venv/bin/python -m p3.gallerysync status` (in the checkout).

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
  needs the site's Admin key (refused over the web once administrator sign-in is enforced) and can change gallery
  content only. The server's own `import` (the SSH push) runs next to the running service: SQLite serializes their
  writes (each waits up to 30 s for the other), and the service reads the imported rows at its next request; only the
  sitemap is kept in memory, for at most a minute.
- **By hand, with a shell on the target:** `bash scripts/gallery-sync.sh export DIR` on the source,
  `bash scripts/gallery-sync.sh import DIR` on the target (plan, backup, import, check; what the SSH push runs on
  atelier). `python -m p3.gallerysync import DIR` alone is a dry run.

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
