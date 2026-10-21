# Customer authentication and administrator access — as built, and how to run it (AJW-39)

Implements `docs/AUTH-PROPOSAL-AJW-39.md` (approved 2026-10-13). Scope: Pipeline 3 only. Pipeline 2, fulfillment authentication,
AI providers, pricing, payment, credit, order and fulfillment logic are untouched.

## 1. What a customer can do now

- Sign up and sign in with **Google**, **Apple**, or **email and password**; confirm their email; reset a forgotten password.
- Stay signed in (a cookie session), sign out, sign out of other browsers, see where they are signed in.
- Connect or disconnect Google / Apple / password on the **Your account** page (`#account`, section *Sign-in & security*).
  A method can be disconnected only while another one remains. Changing how you sign in needs a recent sign-in (10 minutes).
- A customer of the old system moves to a real login **once**, with their six-letter code (or without it, by email — "Forgot
  password" finds the account by its address). Credits, designs, assets, quotes, orders and history stay on the same account.

## 2. Architecture

| Piece | Where |
|---|---|
| Identity store (logins, argon2id passwords, single-use tokens, sessions, OAuth state) | `p3/accounts/identity.py`, tables in `var/accounts.db` |
| HTTP: session middleware, CSRF, `/api/auth/*`, `/auth/verify|reset|confirm` pages | `p3/authapi.py`, `web/authpage.html` |
| Google / Apple OpenID Connect (code flow, server-side ID-token validation) | `p3/oidc.py` |
| The one customer seam (`RequirePrincipal`) | `p3/auth.py` |
| Administrator identity (allowlist, sessions, audit) | `p3/adminauth.py`, `p3/admin.py`, tables in `var/pipeline3.db` |
| Operator commands | `p3/cli.py` |
| Customer UI | `web/index.html`, `web/app.js`; Admin sign-in `web/admin.html`, `web/admin.js` |

- **Same account ids.** `p3local:acct_…` is unchanged; application tables are not touched. Welcome credits are granted by exactly
  one code path (`IdentityStore.CreateAccount`); linking, claiming and migrating never call it (tests assert the credits).
- **Sessions.** A random 256-bit id in the `atelier_session` cookie (HttpOnly, SameSite=Lax, Secure over https, scoped to the
  site's path); only its SHA-256 is stored. A new id is issued at every sign-in; password change/reset and connect/disconnect
  rotate it and revoke the others. 30 days idle (sliding), 180 days absolute; server-side revocation; replaying a logged-out cookie fails.
- **CSRF.** Every unsafe `/api` request carries `X-CSRF-Token` (from `GET /api/auth/session`) when it has a session, and every unsafe
  `/api` request has its `Origin`/`Referer` checked. The pre-session endpoints (login, register, forgot, reset, verify, claim)
  are exempt from the token but not from the Origin check, and each needs a secret the attacker lacks.
- **Existing routes unchanged.** The middleware turns a valid cookie into an internal `X-Access-Token: s1.<id>`; all ~60 customer routes
  keep calling `RequirePrincipal`. A client-sent `X-Access-Token` is discarded when it is a session marker, and in `modern` mode always.
- **Tokens (verify email, reset, confirm link, legacy claim)**: `secrets.token_urlsafe(32)`, stored as SHA-256 only, single use
  (atomic), expiring (24 h / 1 h / 1 h / 30 min), only the newest of a kind works, rate-limited. They travel in the URL **fragment**
  and are consumed by a POST — a mail scanner opening the link cannot use it up.
- **No enumeration.** Register and forgot answer identically for known and unknown addresses; the owner of a known address
  is told by email. Failed logins answer identically for unknown address and wrong password, at the same cost (a dummy argon2 verify).
- **Pre-hijacking.** Someone registering a victim's address with a password of their choosing gets nothing: the password is not
  usable until the address is confirmed, and it is discarded if the real owner later signs in with a verified Google/Apple identity.
- **Linking.** `(provider, subject)` first. A new identity whose provider vouches for the email (Google; Apple only when
  verified and not a `@privaterelay.appleid.com` address) links to the account with that address. An identity the provider does
  not vouch for must be confirmed by a link emailed to the address. Never two accounts for one verified address.
- **Logs.** No password, token, session id, authorization code or full email address is logged (the access log redacts
  `code=`, `state=`, `token=`; emails are masked). Tests assert it.

## 3. Modes and rollout

`P3_AUTH_MODE`:

| Mode | Behaviour |
|---|---|
| `legacy` | Exactly the system before AJW-39 (six-letter codes; `/api/register` mails a code). The existing test-suite runs in this mode. |
| `dual` | Cookie sessions **and** the six-letter code work. The old `/api/register` and `/verify` are closed (no email may carry a code). Sign-in screen = new dialog; a code holder is offered "Secure my account". |
| `modern` | Sessions only. `X-Access-Token` is ignored; `/api/register-token` is closed. The code is only exchanged once at `/api/auth/claim`. |

Production refuses `legacy`, and refuses `modern` unless `P3_ADMIN_IDENTITY_ENFORCED=true`.

**Rollout (each step needs its own approval; none has been done):**
1. Backup (`scripts/backup.sh`; includes `accounts.db` and `pipeline3.db`). Deploy the build with `P3_AUTH_MODE=dual`, providers configured, `P3_ADMIN_IDENTITY_ENFORCED=false`. The tables are created at start; nothing else changes.
2. `python -m p3.cli legacy-report` — how many accounts are unclaimed, how many have no email, which addresses are shared by two accounts (review those by hand; they are never merged automatically).
3. Smoke test on the live host: register, confirm, sign in/out, Google, Apple, forgot password, a legacy code → claim.
4. Enroll the administrators (§6), have both sign in at the Admin once with their provider, then set `P3_ADMIN_IDENTITY_ENFORCED=true` and restart. Verify the key no longer opens the Admin on the web.
5. Announce; watch §7 for a transition window (suggested 60–90 days). Then `P3_AUTH_MODE=modern`.
6. After the window: `python -m p3.cli retire-legacy-codes` (dry run) then `--yes`. Owners recover with "Forgot password" by email.

**Rollback.** Nothing legacy is deleted until step 6: a code is deactivated only when its account gets a *verified* login, or in step 6.
- `P3_AUTH_MODE=dual` (or `legacy`) and restart: codes that were never claimed work as before. Claimed accounts keep their new login (their old code is retired; recover with Forgot password, or an operator `create-token`/`AdminSetActive` issues a fresh code).
- `P3_ADMIN_IDENTITY_ENFORCED=false` and restart: the Admin key opens the Admin again (operator action, on the server).
- Data: the new tables are additive. Restore from the step-1 backup only if the database itself is damaged — it would discard sign-ups made since.

## 3a. Atelier as it runs today: Admin only through an SSH tunnel (decided 2026-10-14)

`atelier` (xjetatelier.xjet3d.com, already has its certificate) runs Pipeline 3 as `uvicorn` on `127.0.0.1:8340` with `P3_BASE_PATH=/JewelryB2C3`, behind nginx (`/etc/nginx/snippets/jewelryb2c3.conf`). There is no separate admin host. The Admin is kept off the internet instead:
- nginx answers **404** for `/JewelryB2C3/admin`, `/JewelryB2C3/api/admin/` and `/JewelryB2C3/api/dev/` on the public name (snippet in the rollout guide below).
- Administrators open `ssh -L 8340:127.0.0.1:8340 atelier`, then `http://localhost:8340/JewelryB2C3/admin/`. The tunnel reaches the app directly, not nginx.
- `P3_ADMIN_BASE_URL=http://localhost:8340` makes Admin sign-in return to that address (Google accepts `http://localhost`; Apple does not, so administrators sign in with Google).
- Google redirect URIs: customers `https://xjetatelier.xjet3d.com/JewelryB2C3/api/auth/google/callback`; Admin `http://localhost:8340/JewelryB2C3/api/admin/auth/google/callback`.
- This layout is `P3_ENV=development`; the production guard (`ValidateProduction`, separate hosts, empty base path) does not apply to it.

## 4. Google and Apple setup (not yet done: needs the XJet accounts)

> **Apple:** the return URLs below depend on the host and `P3_BASE_PATH` (atelier today serves under `/JewelryB2C3/`). Use [APPLE-SIGNIN-AJW-41.md](APPLE-SIGNIN-AJW-41.md) §2–3 for the Apple values and the handoff.

**Google Cloud Console → APIs & Services → Credentials → OAuth client ID → Web application.**
Authorized redirect URIs (exactly):
- `https://xjetatelier.xjet3d.com/api/auth/google/callback`
- `https://<P3_ADMIN_HOST>/api/admin/auth/google/callback` (the Admin host; each host that signs people in needs its own)
Consent screen: the app's name, support email, scopes `openid email profile`. Set `P3_GOOGLE_CLIENT_ID` / `P3_GOOGLE_CLIENT_SECRET`.

**Apple Developer → Certificates, Identifiers & Profiles.** Create a *Services ID* with *Sign in with Apple* enabled; Domains `xjetatelier.xjet3d.com` (and the Admin host);
Return URLs `https://xjetatelier.xjet3d.com/api/auth/apple/callback` and `https://<P3_ADMIN_HOST>/api/admin/auth/apple/callback`. Create a *Key* with Sign in with Apple, download the `.p8` once.
Set `P3_APPLE_CLIENT_ID` (the Services ID), `P3_APPLE_TEAM_ID`, `P3_APPLE_KEY_ID`, `P3_APPLE_PRIVATE_KEY_PATH`. If the sender domain for Apple's private relay is wanted, register `xjet3d.com` mail with Apple.

Public HTTPS production domains only; no internal/proto host is an OAuth origin. A provider without all of its settings does not appear on the sign-in page.

## 5. Administrator access model

- Customer sessions and administrator sessions are **separate** (different cookie, table, host). A customer session, a social login, or an email match opens no admin route (tested).
- Administrators are rows in `admin_identities` keyed by **provider + the provider's subject id** (not the email), enabled/disabled, with the enrolled email re-checked at each sign-in (it must be verified by the provider and equal). Only Google and Apple identities can be enrolled.
- A sign-in lasts 12 hours (no sliding), only while the identity is enabled. Every unsafe admin request is written to `admin_audit`; so is each operator change.
- With `P3_ADMIN_IDENTITY_ENFORCED=true` the shared key opens **nothing** on the web (`/api/admin/*`, `/api/dev/*`, `/api/admin/login`).

## 6. Enrolling Yakir and Dov, and recovery

Identities to enroll (confirmed by Dov 2026-10-13; **Yakir's address needs confirming**):
- Dov — `dov.grobgeld@xjetdigital.com`
- Yakir — `yakir.tubul@xjetdigital.com`

Subject ids cannot be guessed; they come from the provider. Procedure, on the server:
1. Each person opens `https://<P3_ADMIN_HOST>/admin/` and presses *Continue with Google* (or Apple). They are refused ("not an administrator") — and the attempt is recorded.
2. `python -m p3.cli admin-candidates` lists the refused identities: provider, **subject**, email, verified or not.
3. Confirm with each person, out of band, that the listed email is theirs. Then
   `python -m p3.cli admin-enroll --name Dov --provider google --subject <subject> --email dov.grobgeld@xjetdigital.com` (asks for the break-glass key: `P3_ADMIN_KEY`).
4. Each person signs in again — now admitted. Then set `P3_ADMIN_IDENTITY_ENFORCED=true` and restart.
5. If an address was wrong: `admin-disable <id>`, enroll the right one.

**Break-glass (decided 2026-10-13):** `P3_ADMIN_KEY` stays on the server and works only through `p3.cli` (enroll, disable/enable, revoke all admin sessions, list, candidates). It can never open the Admin through the web while enforcement is on. Lost access (a person's account is locked, a provider is down): run `admin-enroll` for another identity (or `admin-enable`), or turn enforcement off (`P3_ADMIN_IDENTITY_ENFORCED=false`, restart) as an operator. Anyone who can read the server's environment can do this; that is the trust boundary.

## 7. Monitoring

- `GET /api/admin/auth-stats` (administrator only): sign-ins in the last 24 h by method, live sessions, emailed links by kind, the migration picture, administrators, mode.
- Log lines (no secrets): `Password sign-in refused for d***@…`, `Security email via … to d***@…`, `<provider> sign-in refused: <reason>`, `Admin sign-in refused (<reason>)`, `p3.ratelimit` 429s.
- Watch during rollout: failures per hour, `link_*` errors (expired/used links = mail delays), `blocked` outcomes, unclaimed legacy count falling, admin `denied` outcomes.
- Rate limits (per IP and per email): login 30/15 min and 8/15 min; security emails 12/h and 4/h; claim 12/15 min; link use 40/15 min.

## 8. Requirement check

| Requirement | Status |
|---|---|
| Google / Apple / email-password sign-up and login | Built, tested against fake providers; **not yet run against real Google/Apple** (needs the §4 credentials) |
| Email verification, forgot/reset, persistent sessions, logout | Built, tested |
| Home public; Design and every private route/API need a session | Tested (`modern` mode: 401 without a session, even with the old code) |
| Credits, designs, assets, quotes, orders preserved; no duplicate accounts or welcome credits | Tested (claim by password, by Google, by email recovery, link by verified email) |
| Server-side token validation; argon2id; hashed single-use time-limited rate-limited tokens; no enumeration | Built, tested |
| Session fixation/theft/CSRF/replay; secure cookies; rotate/revoke after sensitive changes | Built, tested |
| Disconnect only if another method remains; recent authentication | Built, tested |
| Legacy six-letter code: not a permanent login, never emailed, no temp passwords | Built, tested; retirement by CLI |
| Only Yakir and Dov reach Admin; customer auth never grants it | Built, tested; **enrollment pending real identities** |
| Responsive, accessible UI | Checked in Chrome at 1280 px and 390 px; labelled fields, `autocomplete`, live regions, ≥44 px targets. No automated WCAG audit tool was run. |
| Pipeline 2 and unrelated systems unchanged | Yes |
| No live fal.ai traffic in tests | Yes (mock provider throughout) |

## 9. Limitations and remaining risks

- Real Google/Apple credentials, the Apple key and the Admin-host redirect URIs are not configured; the provider code is exercised through fakes and by reading the providers' documented flows. First live test is part of rollout step 3.
- Yakir's address is unconfirmed; the Admin host's name is not in this repository's example (it must be registered with each provider).
- A password chosen at registration becomes usable when the mailbox owner clicks the confirmation link. Someone who registers another person's *new* address with their own password can therefore take that new account if the owner clicks an unsolicited link (existing and mid-claim accounts are not affected: a repeat registration never replaces a stored password, and a verified Google/Apple sign-in discards an unconfirmed password). Asking for the password on the confirmation page would close this; not done.
- Rate limits are in memory, per process (the service runs one worker); a restart resets them.
- The password check is length plus a short list of common passwords, not a breach-corpus lookup.
- No email-change, account-deletion or "login notification" features (the latter is Phase 2, with passkeys, WebAuthn and Facebook).
- A claim needs the six-letter code or access to the account's email; an account with neither (no email, lost code) needs an operator.
- `P3_AUTH_MODE=dual` still accepts `X-Access-Token` for codes not yet exchanged — by design, for the transition.
- The OAuth state cookie is `SameSite=None; Secure` over https (Apple posts back cross-site); it is HttpOnly, 10 minutes, single use, bound to a server-side state row.

## 10. Where this differs from the proposal

- Administrator sessions are separate from customer sessions (the proposal said an admin "needs a customer session and an enrolled identity"; separate is stricter and fits the separate Admin host).
- Enrollment reads the refused sign-in attempts and an operator confirms; there is no web one-time enrollment link.
- Welcome-credit and linking behaviour, token lifetimes and the migration paths are as proposed. The unclaimed-code retirement is a CLI command (`retire-legacy-codes`), not a timer, so nobody loses a working code by accident.

## 11. Tests

`tests/test_auth.py` (customer flows, 33), `tests/test_admin_identity.py` (administrator, 10), `tests/test_auth_ops.py` (migration tooling, monitoring, production guard, log redaction, 7);
the architecture guard in `tests/test_accounts.py` now also keeps the new identity tables inside `p3/accounts/`. The whole suite runs with `python -m pytest`
(the existing tests run in `legacy` mode via the harness default).
