Atelier — Pipeline 3 in production mode (3 Nov 2026)

Run every command below as dov from the checkout (/home/dov/git/XjetJewelryBuilderPipeline3): this folder arrives
with the release (deploy/atelier/production-mode/), so nothing needs copying.

Why: Atelier (P3 under /JewelryB2C3/) still runs P3_ENV=development. The developer tools are already off (B1, 2 Nov:
/dev, /showcase, /docs answer 404), but the site still says "development", the AI mode depends on the developer switch,
the fal key lives in the Admin's copy and the Admin opens with the shared key. Production mode fixes all of that.
The P3 code from 3 Nov (on Atelier with this release) accepts Atelier's layout in production: the base path on the
shared host, the data in the checkout's git-ignored var/, and the Admin reached only through the SSH tunnel.

What changes (only .env; data, accounts, designs, orders, settings and the customer site stay as they are):
  P3_ENV=production, P3_PROVIDER=fal (AI stays live, now from the configuration; the developer switch no longer applies)
  FAL_KEY = the key saved today in the Admin (production ignores the Admin's copy)
  P3_ADMIN_KEY: today 21 characters -> a new random key of 32 (production needs 24+). It becomes the break-glass key for
               `python -m p3.cli` on this server; the web no longer accepts any shared key.
  P3_SIGNING_SECRET: a new random secret if shorter than 24 or equal to the admin key (old signed download links expire).
  P3_ADMIN_HOST=localhost, P3_ADMIN_BASE_URL=http://localhost:8340: the Admin answers only through the tunnel (nginx
               already hides it from the public host; this adds the same rule inside P3).
  P3_ADMIN_IDENTITY_ENFORCED=true: the Admin opens only with an enrolled Google identity. Today only Dov's is enrolled,
               so the owner's must be enrolled first (steps 1–3), or the owner would lose Admin access.
Consequence: a gallery sync to Atelier can no longer use the shared key over the web; it runs on the server
(scripts/gallery-sync.sh import) — the last sync was 8 Oct.

Order (each step only after the previous one worked):
  0. The 3 Nov code release is live on Atelier (Yakir; the script's `check` refuses older code).
  1. Owner (Yakir): open the tunnel (ssh atelier), go to http://localhost:8340/JewelryB2C3/admin/ and choose
     "Sign in with Google" with yakir.tubul@xjet3d.com. It is refused (not enrolled yet) — that is expected: the attempt
     is recorded so it can be enrolled.
  2. Dov: bash deploy/atelier/production-mode/p3_production_mode.sh enroll-owner
     Enrols that verified Google identity (it uses today's P3_ADMIN_KEY from .env as the break-glass key).
  3. Owner: sign in with Google again — the Admin opens. This proves Google sign-in works through the tunnel.
  4. Dov: bash deploy/atelier/production-mode/p3_production_mode.sh check
     Read-only: lists the .env changes (no secret is printed), checks the owner's enrolled identity and its sign-in, and
     dry-runs the new settings in a fresh Python (the running service is not touched). Expected last line:
     "== problems: none — ready to apply".
  5. Dov: bash deploy/atelier/production-mode/p3_production_mode.sh apply
     Backs up .env and both databases (verified) into ~/p3-backups/production-mode-<UTC stamp> (mode 700), writes the
     new .env, restarts xjet-jewelry-b2c3 and checks: the page says production, the developer tools are off, the Admin
     API is 404 on the public host and asks for an administrator sign-in through the tunnel, the public site answers.
     Any failed check puts the old .env back and restarts by itself.
  6. Send Yakir the output of steps 4 and 5. Yakir then checks the Admin (health: env production, mode live, locked)
     and the site.

Rollback (only if something is wrong later): bash deploy/atelier/production-mode/p3_production_mode.sh rollback ~/p3-backups/production-mode-<stamp>

Tested locally on 3 Nov against a simulated Atelier checkout (Atelier-like .env, a fal key saved in the Admin, a 21-
character admin key): refused while the owner was not enrolled, and again while enrolled but never signed in; ready
once both held; the new .env was then started in production mode and passed the script's own checks.
