# Sign in with Apple — requirements, state and handoff (AJW-41)

Sign in with Apple was built with the rest of customer sign-in in AJW-39 (`p3/oidc.py` `AppleProvider`, `p3/authapi.py`,
`p3/accounts/identity.py`, the "Continue with Apple" button). It works exactly like Google's: press the button, Apple's page,
back signed in. What has never happened is a run against the real Apple, because XJet has no Sign in with Apple
configuration yet. AJW-41 therefore is: prove the code against a stand-in Apple (done, below), and hand Yakir the exact
list of what only the Apple Developer account holder can create.

## 1. What Apple requires (Apple Developer documentation, "Configuring your environment for Sign in with Apple")

| Need | Detail |
|---|---|
| **Apple Developer Program membership** | Paid, in XJet's name (an organisation). A personal Apple ID is not enough. |
| **Who can do the setup** | The **Account Holder** or an **Admin**. App Manager and below cannot create identifiers or keys. |
| **App ID** | A *primary App ID* with the *Sign in with Apple* capability enabled. The web Services ID is attached to it. The App ID does not have to be a published app in practice, but Apple's article words it as "an existing app"; if the console refuses, ask Apple Developer Support. |
| **Services ID** | The web "client id" (e.g. `com.xjet.atelier.web`; the name is XJet's choice). Sign in with Apple is enabled on it and configured with the **domains** and **return URLs** below. |
| **Domains** | Every domain that shows the button must be registered **and verified**. Apple gives a verification file; it must be served over HTTPS at `https://<domain>/.well-known/apple-developer-domain-association.txt`, then *Verify* is pressed in the console. A domain can belong to one Services ID at a time. |
| **Return URLs** | Absolute (scheme + host + path), HTTPS, exact match, no `localhost`, no IP address. Up to 100 per Services ID for an organisation. |
| **Key** | A *Key* with *Sign in with Apple* enabled, tied to the primary App ID. A `.p8` private key is **downloadable once**; it yields the **Key ID** (10 characters). Max two keys per App ID. |
| **Team ID** | 10 characters, top right of the developer account's Membership page. |
| **Client secret** | Not stored. The server builds it for each exchange: an ES256 JWT (`iss`=Team ID, `sub`=Services ID, `aud`=`https://appleid.apple.com`, `kid`=Key ID) signed with the `.p8`. |
| **Private email relay** | Customers may hide their address (`…@privaterelay.appleid.com`). Mail only reaches them if XJet's **outbound email domain or address is registered as an email source** (Certificates, Identifiers & Profiles → More → *Configure Sign in with Apple for Email Communication*) and passes **SPF and/or DKIM** (both recommended). |
| **Server-to-server notifications** (optional) | One HTTPS endpoint per App ID, told when a person changes email forwarding, deletes their app account, or deletes their Apple Account. **Not built** (see §5). |

## 2. XJet's values — what is fixed and what is NOT yet decided

The code builds the return URL from the request's own origin plus `P3_BASE_PATH`, so Apple must be told the URL the
customer actually reaches. **The repository names three different places**; this needs an owner decision before anything
is typed into Apple:

| Where | Customer URL | Return URL for Apple |
|---|---|---|
| **atelier today** (`P3_BASE_PATH=/JewelryB2C3`, `P3_PUBLIC_BASE_URL=https://xjetatelier.xjet3d.com`, behind Cloudflare) | `https://xjetatelier.xjet3d.com/JewelryB2C3/` | `https://xjetatelier.xjet3d.com/JewelryB2C3/api/auth/apple/callback` |
| **Planned production** (`deploy/production/`, `docs/PRODUCTION-READINESS-HANDOFF.md`) | `https://atelier.xjet3d.com/` (Admin: `https://admin.atelier.xjet3d.com/`), no prefix | `https://atelier.xjet3d.com/api/auth/apple/callback` |
| `.env.production.example` and AUTH-OPERATIONS §4 | `https://xjetatelier.xjet3d.com/` with no prefix | `https://xjetatelier.xjet3d.com/api/auth/apple/callback` ← **does not match atelier today** (it omits `/JewelryB2C3`) |

Apple takes many return URLs on one Services ID, so the safe course is to register every environment that will really
serve customers (the first two rows) and leave out any that never will. Do **not** register a URL that does not exist.

**The Admin.** Apple rejects `localhost`, and atelier's Admin is reached through an SSH tunnel (`P3_ADMIN_BASE_URL=http://localhost:8340`),
so Admin sign-in with Apple cannot work there; administrators use Google. Register an Admin return URL
(`https://admin.atelier.xjet3d.com/api/admin/auth/apple/callback`) only if production's Admin host goes live.

## 3. Handoff for Yakir (the eight points the ticket asks for)

1. **Missing:** a Services ID, a Sign in with Apple key (`.p8`), the Team ID, and verified domains. None exists; nothing was invented.
2. **Where to log in:** <https://developer.apple.com/account> with the Apple ID that is the Account Holder (or an Admin) of XJet's Apple Developer Program membership. If XJet has no membership, enrolling is the first step.
3. **Role needed:** Account Holder or Admin.
4. **Create / configure** (Certificates, Identifiers & Profiles):
   1. *Identifiers → App IDs*: a primary App ID with **Sign in with Apple** enabled (bundle ID of XJet's choice, e.g. `com.xjet.atelier`).
   2. *Identifiers → Services IDs*: new Services ID (e.g. `com.xjet.atelier.web`), enable **Sign in with Apple → Configure**, pick the primary App ID, add the domains and return URLs of §2, download the domain-association file and **Verify** each domain once the file is served (see §6).
   3. *Keys*: new key, enable **Sign in with Apple**, configure it for the App ID, **download the `.p8` (only once)**, note the **Key ID**.
   4. *More → Sign in with Apple for Email Communication*: register the sender domain or address the shop's mail comes from (the SMTP relay's From address) and make sure SPF/DKIM pass for it.
5. **Names, domains, return URLs:** §2, as decided by the owner.
6. **Exact values/files needed back from Yakir:** the **Services ID** → `P3_APPLE_CLIENT_ID`; the **Team ID** → `P3_APPLE_TEAM_ID`; the **Key ID** → `P3_APPLE_KEY_ID`; the **`.p8` file** → `P3_APPLE_PRIVATE_KEY_PATH`; and the **domain-association file** (a public text file, not secret). Team ID, Key ID and Services ID are identifiers, not secrets. The `.p8` is the only secret.
7. **Secure transfer:** the `.p8` goes straight to the server operator by an encrypted channel (a password manager's share, or `scp` from Yakir's machine) and is installed as `/etc/xjet-atelier/apple-signin.p8`, mode `0640`, owner `root:atelier`. Never in Jira, Git (`*.p8` is now in `.gitignore`), chat, email, screenshots or logs.
8. **Blocked until then:** the real end-to-end sign-in, domain verification, the private-relay mail check, and every item of §8 marked "needs Apple". Everything else is built and tested.

## 4. What is built and how it was tested without Apple

| Behaviour | Where | Test |
|---|---|---|
| Authorize redirect (`response_mode=form_post`, `scope=name email`, state, nonce) | `AppleProvider.AuthorizeUrl` | `tests/test_apple_provider.py` |
| Client-secret JWT (ES256, `kid`, `iss`/`sub`/`aud`, short life) | `AppleProvider.ClientSecret` | same |
| Code exchange, ID-token checks (signature against Apple's keys, RS256 only, issuer, audience, expiry, nonce, subject) | `AppleProvider.Complete`, `_Verify` | same: wrong nonce/aud/iss, expired, unknown kid, forged algorithm, wrong key, refused code |
| `email_verified` as the string `"true"`, relay address, missing email, name from first-authorization `user` field, malformed `user` | `Complete`, `ParseUserHint` | same |
| Cross-site POST callback, state cookie `SameSite=None; Secure`, single-use server-side state | `authapi.py` | `tests/test_auth.py` (with a fake Apple) |
| New sign-up, returning login, link by verified email, relay address = separate account, held until confirmed by email when not vouched for, duplicate prevention | `identity.py` | `tests/test_auth.py` |
| Cancel / denied (`error`), invalid, replayed, expired state → `#auth=cancelled/failed` | `Finish` | `tests/test_auth.py` |
| `sign_up` / `login` with `method=apple`, no personal data | `web/app.js`, `web/consent.js`, `docs/ANALYTICS.md` | `tests/test_analytics.py` |
| A half-configured Apple, or an unreadable key file, now logs which setting is missing (names only) instead of the button silently vanishing | `BuildProviders`, `settings._ApplePrivateKey` | `tests/test_apple_provider.py` |

Relay customers are not stuck: an account made with a relay address has no email of its own, and checkout asks for a
contact email anyway (`Orders.ValidateCustomer`).

## 5. Not built (by decision, not omission)

* **Server-to-server notifications** (consent revoked, account deleted). Without them a customer who revokes XJet in their
  Apple settings simply cannot sign in with Apple any more (Apple refuses the code); the XJet account, designs, credits and
  orders are untouched and the customer can still use email/password or Google. A notification endpoint can be added later
  (verify Apple's signed JWT, mark the identity revoked); it is optional for "press the button, sign in".
* **Admin sign-in with Apple on atelier** (needs a public HTTPS Admin host; see §2).

## 6. Serving the domain-association file

The file is public. It must answer at `https://<each registered domain>/.well-known/apple-developer-domain-association.txt`
**at the domain root**, outside `/JewelryB2C3/` on atelier, so nginx (or a Cloudflare rule), not the app, serves it:

```nginx
location = /.well-known/apple-developer-domain-association.txt {
    alias /etc/xjet-atelier/apple-developer-domain-association.txt;   # the file downloaded from Apple, mode 0644
}
```

Cloudflare must not rewrite, redirect or challenge that path (the `/` → `/JewelryB2C3/` redirect rule must not catch it).

## 7. Operation and troubleshooting

* The button appears only when all four of `P3_APPLE_CLIENT_ID`, `P3_APPLE_TEAM_ID`, `P3_APPLE_KEY_ID` and the key
  (`P3_APPLE_PRIVATE_KEY` or `P3_APPLE_PRIVATE_KEY_PATH`) are set. If it is missing, the service log says which: `Sign in with Apple is not offered: missing …`.
* `#auth=failed` after Apple's page: look for `apple sign-in refused: …` in the log. `token exchange failed: HTTPStatusError` = wrong Services ID / Team ID / Key ID, a `.p8` that is not that key's, or a return URL that does not exactly match one registered. `ID token rejected` / `nonce mismatch` = a replayed or foreign token.
* `invalid_client` at Apple → the three identifiers and the key do not belong together. `invalid_grant` → the code was used twice or expired (codes live minutes).
* Rotating the key: create the second key, deploy its `.p8` and Key ID, restart, test, then revoke the old one in Apple.
* The `.p8` is a secret and `*.p8` is git-ignored. Keep it out of the checkout anyway (`/etc/xjet-atelier/`).

## 8. Production validation plan

Needs Apple = cannot run until the §3 values exist.

| Check | Needs Apple |
|---|---|
| New signup (a fresh Apple ID): account created once, welcome credits once, `sign_up` with `method=apple` | yes |
| Returning login: same account, no new credits, `login` with `method=apple`, no second event on refresh or back button | yes |
| "Hide My Email": account created with the relay address, ordering asks for a contact email, XJet's mail to the relay address arrives (SPF/DKIM registered) | yes |
| Existing email account + Apple with the same verified address → linked, no duplicate, credits/designs/orders/history unchanged | yes |
| Cancel on Apple's page, and "Stop using Apple ID" → friendly message, no session, no event | yes |
| Invalid, expired and replayed callbacks (reuse the callback URL after success; wait out the 10-minute state; alter `state`) | no (done in tests), re-check live |
| Google and email/password still sign in; sessions, logout, "Sign-in & security" connect/disconnect | partly |
| Monitoring: `/api/admin/auth-stats` shows apple sign-ins; log lines `apple sign-in refused`; alert on a rising count | yes |
| **Rollback:** unset `P3_APPLE_CLIENT_ID` (or remove the key file) and restart: the button disappears, existing Apple identities stay in the database, those customers sign in with Google or a password if they have connected one. A customer who only ever used Apple, with a hidden address, has no other way in until the button returns, so announce any rollback and keep it short. Nothing else changes. | no |

Do not set the Apple variables on atelier until the values exist and the owner has approved the deploy.
