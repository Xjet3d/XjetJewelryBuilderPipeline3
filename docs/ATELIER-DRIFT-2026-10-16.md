# Atelier server drift of 2026-10-16: username sign-in, reconciled into Git

## What happened

On 2026-10-16 at 08:24:11 UTC Dov changed five files directly in Atelier's checkout
(`/home/dov/git/XjetJewelryBuilderPipeline3`, then at 208181d) and restarted the service. The change adds an
operator-made sign-in by **username and password** with no email address (`python -m p3.cli set-username-login`).
It was never committed, so it existed in no GitHub branch:

- a release touching these files would have made `scripts/auto-deploy-atelier.sh` fail at every tick (git refuses to
  fast-forward over local edits);
- a release not touching them would have left them outside Git;
- a release that failed its health check would have erased them (`git reset --hard`).

## What is in Git now

The commit "Accounts: operator-made username + password sign-in (reconciled from the Atelier server)" (author Dov) is
that change, unchanged. Each file is byte-identical to the server's working tree (sha256 of the LF content):

| File | sha256 (server = commit) | Change |
|---|---|---|
| p3/accounts/identity.py | 8f9cb31ab0f00cd463491370c6fd390df9b54c5c256dab23fb3ddab8b0ca7c88 | `UsernamePattern`, `IdentityStore.SetUsernamePassword` |
| p3/authapi.py | e624578380ef79420abbc8b7fa72c7ee17d6499c709c37fca36097201b1948a5 | refused sign-in says "email, username or password" |
| p3/cli.py | 9427a28e186711856d4b21b965d59d06be18d7bc6ead2a1d5333f549f5c02eba | `set-username-login <username> [--name]` |
| tests/test_auth.py | 521c4c6cae356f9459ac27187e38df966e22a7f254cb93daf48044b0e3e27bc1 | 2 tests |
| web/index.html | 24d4674b400396753091fc055575fc2c61ed3c2a8fcfd790d6f2273484583bab | sign-in field "Email or username" (type text) |

The server's own patch (`git diff` on the server, 155 lines) has sha256
`8cdaf8c97e0434b1f9900756607891448aafc0ccb9b74694515aae4e955ad2e2`.

Two safeguards came with it:

- `.gitignore` now ignores `.env.*` (except the `*.example` files), `*.bak`, `*.secret` and `backups/`: the server
  checkout's untracked `.env.bak-before-auth` and `backups/` can no longer be committed by a `git add -A`.
- `scripts/auto-deploy-atelier.sh` refuses to deploy while tracked files have local edits. It first keeps a copy of
  them (`~/p3-backups/local-changes-<head>-<hash>.patch`), and its failure shows in `systemctl --user status
  xjet-jewelry-b2c3-autodeploy`. Nothing is overwritten or reset.

## At the release that includes this commit (owner-approved; nothing is done before)

The release is the first one whose `atelier-production` contains the reconciliation commit. The server still runs
the old auto-deploy script, whose fast-forward refuses over the local edits; nothing breaks and nothing is lost, but the
release waits for these steps (Dov, or with his and the owner's go-ahead):

```bash
ssh atelier
cd /home/dov/git/XjetJewelryBuilderPipeline3
systemctl --user stop xjet-jewelry-b2c3-autodeploy.timer          # pause the auto-deploy
git fetch origin atelier-production
R=$(git log --format=%H -1 --grep='reconciled from the Atelier server' origin/atelier-production)
# the local edits must be exactly the reconciled commit's change (no output = identical):
diff <(git diff HEAD) <(git diff "$R~1" "$R") && echo IDENTICAL
git stash push -m "atelier drift 2026-10-16, reconciled in ${R:0:7}" -- \
    p3/accounts/identity.py p3/authapi.py p3/cli.py tests/test_auth.py web/index.html   # set aside, not discarded
bash scripts/auto-deploy-atelier.sh                                # fast-forward, restart, health, checks
# sign in with a username at /JewelryB2C3/ (the field reads "Email or username"), then:
systemctl --user start xjet-jewelry-b2c3-autodeploy.timer
mv .env.bak-before-auth backups ~/p3-backups/ 2>/dev/null || true  # optional: keep these outside the checkout
```

**Rollback** to exactly the state before the release: `git reset --hard 208181d && git stash pop`, then
`systemctl --user restart xjet-jewelry-b2c3`. The stash entry is dropped only after the release is confirmed
(`git stash drop`). The data backup of the deploy is in `~/p3-backups/<timestamp>/`.
