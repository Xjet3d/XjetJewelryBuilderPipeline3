# AJW-39 — Customer authentication and account management: architecture and migration proposal

Status: **approved 2026-10-13 and implemented** — what was built, how to run it and where it differs: [AUTH-OPERATIONS-AJW-39.md](AUTH-OPERATIONS-AJW-39.md). Scope: Pipeline 3 only (`JewelryB2C3`). Public origin: `https://xjetatelier.xjet3d.com`.

## 1. Current state (audit)

- Customer auth: header `X-Access-Token` (6 letters, kept in `localStorage` `p3_session:<base>`) → `p3/auth.py:RequirePrincipal` → `Ctx.Accounts.Authenticate` → `Principal(AccountId="p3local:acct_…")`.
- Identity store `var/accounts.db`: `accounts`, `access_tokens` (SHA-256), `usage_events`, `account_events`; credits live on `accounts` (`max_generations`, reservations).
- Application data `var/pipeline3.db`: designs, bag lines, quotes, orders reference the customer only by the string `owner_account_id` (no cross-DB foreign keys). A test forbids any module outside `p3/accounts/` from touching identity tables.
- AJW-1 email registration mails the access token — forbidden by this epic; it is replaced.
- Admin: no identity. `P3_ADMIN_KEY` shared secret (bearer, or exchanged for the `atelier_admin_session` cookie, 180-day sliding). `RequireAdmin` in `p3/admin.py` is the single seam; `/api/dev/*` uses `RequireDeveloper`.
- No customer cookies, CSRF protection, session revocation or recent-authentication concept exist.

## 2. Target identity model

Existing `p3local:acct_…` IDs are kept. Designs, credits, quotes, orders and history are untouched; only credentials are added.

New tables in `accounts.db` (identity domain only):

| Table | Purpose |
|---|---|
| `login_identities` | `(provider, subject)` unique → `account_id`; provider ∈ google, apple, password; stored email, `email_verified`, created/last-used |
| `password_credentials` | `account_id`, argon2id hash (parameters stored with hash, rehash on login), `updated_at` |
| `auth_tokens` | purpose (verify_email, reset_password, claim_legacy, confirm_link), `sha256(token)`, `account_id`, expiry, `used_at`; single use |
| `customer_sessions` | `sha256(session id)`, `account_id`, created/last_seen/expires, `auth_at` (recent-auth), user agent, `revoked_at`, CSRF secret |
| `oauth_states` | state, nonce, PKCE verifier, redirect target, short TTL, single use |
| `admin_identities` | `(provider, subject)` allowlist, name, enabled, `added_by`, `added_at` — see §6 |

Welcome credits are granted only by the code path that **creates** an account. Linking, migration and claiming never call it (tested).

## 3. Sessions

- Opaque 256-bit random id in cookie `atelier_session`: HttpOnly, Secure, SameSite=Lax, path-scoped to the app, no JS access. Server stores only the hash.
- New id on every login (no fixation); id rotated and other sessions revoked on password change/reset and on connect/disconnect of a login method.
- Idle 30 days sliding / absolute 180 days; logout revokes server-side; "sign out everywhere".
- CSRF: double-submit token header (`X-CSRF`) required on all mutating `/api` requests, plus Origin check. OAuth `state` + `nonce` + PKCE against replay.
- Recent authentication (≤10 min) required for: set/change/remove password, connect/disconnect a method, change email, delete account.
- `X-Access-Token` header stops authenticating after cutover (see §5).

## 4. Authentication flows

- **Email/password:** register → verification email (single-use, 24 h, hashed, rate-limited per IP/email) → login. Login requires a verified email. Responses identical for known/unknown emails (no enumeration), constant-time-ish work. Password ≥10 chars, checked against a small breached-password list; argon2id; autocomplete attributes set for password managers.
- **Forgot/reset:** always "if the address exists, we sent a link"; token 1 h, single use, hashed; reset revokes all sessions.
- **Google / Apple:** official authorization-code + PKCE flow; server-side validation of ID token (signature via provider JWKS, `iss`, `aud`, `exp`, `nonce`, `email_verified`). Apple client secret is a signed JWT from the team key; Apple's first-login-only name is stored when received. Callbacks on `https://xjetatelier.xjet3d.com/JewelryB2C3/api/auth/<provider>/callback`.
- **Linking rules:** match on `(provider, subject)` first. If new and the provider asserts a **verified** email equal to an existing account's verified email → link to that account (Google; Apple only for a real, non-relay address). Otherwise (unverified, or Apple private relay matching nothing) → create a separate account or, if the email matches, require confirmation via an emailed single-use link. Never two accounts for one verified email.
- **Disconnect** is allowed only if another login method or a verified recovery email remains.
- Public Home stays open; Design and all private routes/APIs require a session.

## 5. Legacy six-letter-token migration

1. Cutover: existing accounts keep ID, credits and data; they have no credential yet.
2. A legacy customer visits the site → "Sign in" → either (a) enters their old code once ("claim"): it is verified server-side, then the user must attach a login (Google, Apple or email+password, with email verification); the token is then deactivated; or (b) uses "I lost my code": enters their email → emailed single-use confirm link → attach a login. Neither path emails or reveals the token or a password.
3. Social or email login by someone whose verified email matches an unclaimed legacy account links to it (§4), so no duplicate and no second welcome credit.
4. A legacy account with no email can only be claimed with its code (or by an admin).
5. Tokens that are never claimed are deactivated at the end of a window (proposal: 90 days) — a product decision, default off until you set the date.
6. Migration script is idempotent, backs up both DBs first, and has a dry-run that reports counts (accounts, with/without email, duplicate emails) — duplicates are flagged for manual review, never auto-merged.

## 6. Admin authorization

- Separate from customer auth: a customer session never carries admin rights, and no code path creates admin rights from a social login or an email match.
- `admin_identities` allowlist holds exactly two rows (Yakir, Dov) keyed by **provider + subject ID**, not email. Admin routes require a customer session **and** an enabled admin identity **and** a recent (≤12 h) login with that identity; admin and `/api/dev/*` actions are audit-logged.
- Enrollment: a CLI command (`python -m p3.cli admin-enroll`) run on the server by an operator prints a one-time link; Yakir/Dov log in with their real provider account and the subject is recorded after both confirm the shown name/email. Enforcement is switched on only after both are enrolled.
- Recovery (break-glass): `P3_ADMIN_KEY` keeps working **only** via a server-side CLI/localhost path that can enroll or re-enroll an identity and revoke admin sessions; it no longer opens the Admin portal through the web. Disabled when the variable is unset.

## 7. UX

Responsive sign-in/registration page (tabs: Sign in / Create account), "Continue with Google/Apple" buttons following each provider's brand rules, email+password form with correct `autocomplete` (`username`, `current-password`, `new-password`), inline accessible errors (`aria-live`, labels, focus management, ≥4.5:1 contrast, 24px targets, keyboard-only operable), verify/forgot/reset pages. Account management lives on a **new Account page** reached from the existing account chip (the current panel is too small for connected methods, password and sessions); the chip panel keeps credits and Sign out. Styling uses the current XJet Atelier tokens.

## 8. Rollout and rollback

- Feature flag `P3_AUTH_MODE`: `legacy` (today) → `dual` (new sessions accepted, token still accepted, legacy login shows claim prompt) → `modern` (token no longer authenticates). Admin enforcement is a separate flag `P3_ADMIN_IDENTITY_ENFORCED`.
- Staged: staging with mocks → production `dual` → monitor → `modern`.
- Rollback: set `P3_AUTH_MODE=legacy`; tokens were never deleted before the window ends (only marked claimed), so legacy login resumes. New tables are additive and unused by legacy mode. Pre-migration backups of both DBs. Admin enforcement can be reverted to key access by flag (operator only).
- Monitoring: counters for logins/failures/rate-limit hits/verification and reset sends, claimed-vs-unclaimed legacy accounts; no secrets, tokens or passwords in logs (masked emails only).

## 9. Batches

1 audit/this document (done) · 2 data model + sessions · 3 email/password + verification · 4 reset · 5 Google · 6 Apple · 7 linking + Account page · 8 legacy migration · 9 admin hardening · 10 tests/docs/rollout. All batches implemented on `main` in sequence with tests (mock mail, mock OIDC providers; no live fal.ai). Production deployment needs separate approval.

## 10. Open items / risks

- Google OAuth client and Apple Services ID/key must be created for `xjetatelier.xjet3d.com`; until then providers are built and tested against mocks.
- Yakir's and Dov's real provider accounts must be confirmed before admin enforcement.
- Legacy deactivation date (§5.5).
- New dependencies: `argon2-cffi`, `pyjwt[crypto]` (JWKS validation), `httpx` if not already present.
- Pipeline 2, fulfillment auth, pricing/credit/order logic and AI providers are not modified.
