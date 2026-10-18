# Atelier server drift of 2026-10-16: username sign-in (removed, AJW-40)

## What happened

On 2026-10-16 an operator-made sign-in by username and password (no email) was added by editing five files directly in
Atelier's checkout (`/home/dov/git/XjetJewelryBuilderPipeline3`, then at 208181d) and restarting the service. It was
never committed. It was then reconciled into Git (commit b77941d) so a release would not fail or erase it.

## Decision

The owner does not want a username login (AJW-40). It is removed from Git, Proto and Atelier, and is **not** carried
into production: `SetUsernamePassword`, the `set-username-login` command, the "Email or username" field and its tests
are gone. Customers sign in with email and password, Google, Apple, or the six-letter code, as AJW-39 built them.

## What stays (the lessons of the drift)

- `.gitignore` ignores `.env.*` (except the `*.example` files), `*.bak`, `*.secret` and `backups/`: a server checkout's
  untracked copies of secrets or backups can no longer be committed by a `git add -A`.
- `scripts/auto-deploy-atelier.sh` refuses to deploy while tracked files have local edits. It first keeps a copy of
  them (`~/p3-backups/local-changes-<head>-<hash>.patch`) and its failure shows in `systemctl --user status
  xjet-jewelry-b2c3-autodeploy`. Nothing is overwritten or reset.
- Code is never edited in atelier's checkout: it is changed in Git, approved on proto, and released to
  `atelier-production` (deploy/README.md).
