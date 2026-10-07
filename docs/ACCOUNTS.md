# Accounts, tokens and credits — current design and the shared-auth integration point

## Now (Phase 1)

P3 is operationally separate from P2. It has its own accounts, tokens, usage and database, and makes no connection to P2's `tokens.db` / `designs.db`.

```
            HTTP  X-Access-Token
                     │
                     ▼
p3/auth.py  RequirePrincipal ──► AccountProvider (p3/accounts/__init__.py)   ◄── the ONLY integration point
                                        │
                                        ├── LocalAccountProvider (p3/accounts/local.py)  ← today
                                        │      var/accounts.db: accounts · access_tokens (sha256) · usage_events
                                        │
                                        └── (future) SharedAccountProvider  → shared Users/Auth/Tokens
                     │
                     ▼  Principal(AccountId="p3local:acct_…", DisplayName, Issuer)
services (images, movies, customize, designs) — see only Principal / AccountId
                     │
                     ▼
var/pipeline3.db  (P3 application data only) — designs.owner_account_id, bag_lines.owner_account_id
```

### Rules the code follows

- **One boundary.** Routes resolve the caller with `Ctx.Accounts.Authenticate(token)` into a `Principal`. Services never see the token.
- **No direct identity-storage access outside `p3/accounts/`.** The one exception is the one-time migration in `p3/migrations.py`. `tests/test_accounts.py::test_only_the_accounts_package_touches_identity_storage` fails if any other module references token or account tables.
- **Structured, provider-issued IDs.** `AccountId` is `"<issuer>:<id>"`, for example `p3local:acct_9f…`. Application tables store only this string; there are no foreign keys into the identity domain.
- **Two separate stores.** Identity lives in `var/accounts.db`; application data lives in `var/pipeline3.db`. Neither file contains tables from the other domain (enforced by a test).
- **Credits behind the provider** (`p3/credits.py` decides the units; the provider holds the allowance). Every paid action calls `AuthorizeSpend(Who, Kind, Units)` *before* anything is created or submitted: it **reserves** the credits atomically (used + reserved + units must fit the allowance, so parallel requests cannot overspend). `Settle(AccountId, Kind, RefId, Charged)` then **charges** the reserved credits when the result is delivered (a ready image, a finished movie — once per `RefId`) or **releases** them when it fails. `RecordUsage(…)` records each provider submission with its estimated list-price cost (`cost_usd`, `cost_source`) and whether it is XJet's own work (`internal`). Reservations are rebuilt from the job tables at every start. The tariff (Admin → Settings → Products → Credits, default):

  - every account has an allowance (`max_generations`, default **10 credits**; `generations_used` = credits charged, `tokens_reserved` = credits held by work in flight);
  - **1 credit per generated image** (a design makes four), **1 per refinement image** (a refinement makes four), **1 per finished 360° movie**, **0 per 3D model** (Hi3D is XJet's production cost). Ten credits are, for example, four design images, two refinement images and four movies;
  - a failed, timed-out or duplicate option is not charged; "Generate another option" is a new credit; a reused movie is free; the Admin's "Make a new movie" and 3D requests are recorded as internal usage and never charged;
  - with too few credits for the work, the start is refused with HTTP 402 `quota_exhausted`: "You have used all the credits on this account. Contact us to extend your allowance." Nothing is submitted (tested). Before 2026-10-07 only finished movies were charged (one generation each); older usage keeps that meaning.
- **Background jobs** look up the owner from the design (`designs.owner_account_id`). Usage is therefore recorded correctly even for jobs resumed after a restart.
- **Tokens are stored hashed** (SHA-256). The plaintext token is shown once, by `python -m p3.cli create-token`.

### Sign-in, registration and the account area (P2 parity)

The customer-facing mechanism reproduces P2's: the same screens, messages, status text and rules.

- **Tokens** are 6 uppercase letters (P2's alphabet, with the same profanity filter) and are case-insensitive on entry. Older `p3_…` tokens issued before this change still authenticate.
- **Email registration** (`POST /api/register {Name, Email}`): an unknown or pending email gets a 24-hour verification link. An already-verified email gets its access token re-sent. Invalid email returns 400 "Please enter a valid email address."
- **Verify page** (`GET /verify?token=…`): states are *verified*, *already verified*, *link expired* and *invalid link*. On success the token is shown on the page and emailed, with a "Continue designing" link to `…/JewelryB2C3/#token=XXXXXX`.
- **Token sign-in** (`POST /api/register-token {Token}`): 404 "Token not found. Check the code and try again." / 403 "This token has been deactivated. Contact XJet." On success the browser goes **straight to the Design screen** with "You're signed in." (or "Your email has been verified successfully." after a `#token=` link).
- **Status** (`GET /api/token-status`): used / max / remaining / name / email. It feeds the top-right chip (name • coin • remaining), the "N of M generations remaining" line under the composer, and the account panel (identity, generations bar, access token with Copy, My Designs →, Sign out).
- **Invalid token while signed in**: protected routes return 401 "Access token not recognised or deactivated. Please re-register." The message is shown and, as in P2, the session is kept until the user signs out.
- **Session persistence**: `localStorage` keys `p3_session:<base>` and `p3_profile:<base>`. These are deliberately *not* P2's `xjet_session` / `xjet_profile`: both apps share the `proto` origin but have separate token stores, so neither app reads or overwrites the other's session.

**Token lifecycle (identical to P2 `token_store.py`).** Registering a new email mints the account's 6-letter token immediately, but it stays inactive: unusable and unrevealed until the link is clicked. Verifying activates it and shows it, and "Already verified" shows the same token again. Re-registering a verified email re-sends that same token by email. Authentication looks tokens up by SHA-256 hash. Self-registered accounts also keep their token on the account row (`accounts.delivery_token`), because P2's flow must re-send and re-show it; P2 itself stores tokens in plaintext. Operator-issued tokens (`p3.cli create-token`) are stored as hashes only.

**Mail.** `P3_MAIL_MODE=smtp` sends through the same relay and sender as P2: `SMTP_SERVER` (default `xjet3d-com.mail.protection.outlook.com`), `SMTP_PORT` 25, IPv4 preferred, no auth/TLS, From "XJet Atelier <no-reply@xjet3d.com>", Reply-To no-reply, `Auto-Submitted`. The relay whitelists the office's public IP, which tron and proto share. As in P2, email is sent in the background after the response, and a relay failure is logged rather than shown. Each message (and the relay's verdict) is also recorded in `var/outbox/`, readable only through the developer endpoint `GET /api/dev/outbox` (admin key). `P3_MAIL_MODE=outbox` (default for local development) records without sending. Links use `P3_PUBLIC_BASE_URL` (for example `http://proto`) when set, and the request's own origin otherwise. The deployed tron instance runs `P3_MAIL_MODE=smtp`.

**Operator CLI:** `python -m p3.cli create-token --name … --email … --max-generations 10`, `set-quota <token> --max-generations N [--reset-usage]`, `list-tokens`, `deactivate-token <token>`.

### Configuration

`P3_ACCOUNT_PROVIDER=local` (default; the only value implemented). The factory is `p3.accounts.BuildProvider`.

## Future: shared Users / Auth / Tokens for P2 and P3

Target:

```
Shared Users / Auth / Tokens / Credits   (one service or one shared database)
        ├── Pipeline 2  (P2-specific data stays in P2)
        └── Pipeline 3  (P3-specific data stays in pipeline3.db)
```

What has to be built, all of it outside P3's application code:

1. **Add `SharedAccountProvider`.** Implement the `AccountProvider` protocol against the shared system (an HTTP API or a shared database), then register it in `BuildProvider` and set `P3_ACCOUNT_PROVIDER=shared`. No route, service or table in P3 changes.
2. **Account identity.** P2 today keys everything by the plaintext token (`tokens.token` is the primary key; name, email and quota live on the same row). The shared system needs a stable account ID that is separate from the token value (for example `shared:<uuid>`), with tokens as credentials attached to it.
3. **Credits.** P2 counts `generations_used` / `max_generations`, with one generation charged per 360° video. P3 reports `image` and `movie` units. The shared provider owns the conversion policy (for example "a P3 four-image batch costs N credits"). That policy is an **open product decision**. P3 already calls `AuthorizeSpend` before work and `RecordUsage` after each submission, so it plugs straight in.
4. **Migrating P3's existing accounts.** For each `p3local:` account in `accounts.db`, create or link a shared account and rewrite `designs.owner_account_id` / `bag_lines.owner_account_id` with a one-time mapping, the same approach as `p3/migrations.py`. Because the IDs are namespaced, a mapping table (`p3local:acct_x → shared:uuid`) is enough. Alternatively, the shared provider can keep accepting `p3local:` IDs as aliases.
5. **Token hashing.** P2 stores tokens in plaintext and P3 stores SHA-256 hashes. A shared system should store hashes. Existing P2 tokens can be hashed on import, and users keep the same token string.
6. **What stays per pipeline.** P3 keeps `pipeline3.db` (designs, batches, candidates, customizations, movies, meshes, bag). Do **not** merge P3 application tables into P2's `designs.db`; the schemas and semantics differ (four candidates per batch, no measurement).

Not done now, deliberately: P3 does not read or write P2's SQLite files, and does not accept P2 tokens.

## Data migration already performed

Databases created before this change kept tokens and usage inside `pipeline3.db` and keyed designs by the raw token. On first start, `p3/migrations.py`:
- backs up the database to `pipeline3.pre-accounts-<UTC>.db`;
- imports each token into `accounts.db`, keeping the same token string;
- rebuilds `designs` and `bag_lines` with `owner_account_id`;
- moves usage across, then drops the old tables;
- verifies foreign keys before committing.

Re-running it does nothing.
