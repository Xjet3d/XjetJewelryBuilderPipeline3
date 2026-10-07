# XJet Atelier (Pipeline 3) — Production Readiness Handoff

Date: 2026-10-07. Branch `main`, deployed on proto (`http://proto/JewelryB2C3/`) at the commits listed in section K.
The review it implements: `P3_Production_Readiness_Review_2026-10-07` (areas A–Z, operations, route separation).

**Nothing in this document is a legal, security or accessibility certification.** Where a matter needs legal counsel,
a business decision, a credential or a change outside the repository, it is marked **Requires legal counsel / business
approval** or **Blocks production**, with who does what. No real secret appears in this document or in the repository.

---

## A. Executive status

| Area | Status |
|---|---|
| Production posture (configuration validation, locked live provider, no mock fallback, no dev tools / API docs / showcase in production, Admin on its own host) | **Implemented** (`p3/settings.py: ValidateProduction`, `p3/modes.py`, `p3/app.py: _Separation`) |
| Security headers, safe public health, log scrubbing, media cache fix, robots / sitemap / canonical / social tags | **Implemented** |
| Customer availability per product (Rings ON/OFF, Charms ON/OFF, future products), product-aware wording | **Implemented** (`p3/products.py`, Admin → Settings → Products) |
| Credits: 1 per design request, 1 per refinement request, 1 per extra option, 1 per movie, 0 per 3D; reserved before paid work, charged on delivery; no silent paid retries; internal usage never charged; cost per submission; daily AI spend cap; request limits | **Implemented** (`p3/credits.py`, `p3/ratelimit.py`) |
| Privacy and sharing (masters show images + name only; consent recorded at publication; share GET side-effect free; sign-out forgets the profile; alike registration answers; single-use verify links; EXIF) | **Implemented** |
| Commerce truths (email state on orders and quote requests, no "reply to no-reply", configurable support address, staff notifications, no duplicate bag line, 3D results before production, STL from the ordered option) | **Implemented** |
| Payment provider | **Not implemented by design — business decision pending** (section F) |
| Production host, DNS, TLS, mail relay, backups timer, monitoring | **Prepared as templates and scripts; not installed** (`deploy/production/`, `scripts/`) — Dov, section J |
| Legal texts (Terms, Privacy, consent wording), tax/shipping rules, support address | **Requires legal counsel / business approval** (section H) |
| Accessibility (WCAG 2.2 AA practical review) | **Reviewed, gaps listed; not certified** (section I) |
| Production data | **Decision needed: fresh database (recommended) vs cleaned migration** (section G) |

**P0 blockers that remain open** (all outside the repository): a production host with the configuration of section E
(secrets, SMTP relay, fal.ai key with a spending limit, daily cap), DNS + TLS for `atelier.xjet3d.com` and
`admin.atelier.xjet3d.com`, the approved pricing profile, the confirmed support address, the legal texts, and the
payment decision (reservation model is truthful today — see F).

---

## B. What was implemented (by batch, all on `main`, all deployed to proto)

### 1. Production posture — commit `59f3373`
- `P3_ENV=production` makes the service **refuse to start** with development defaults: fal provider with `FAL_KEY`,
  an https `P3_PUBLIC_BASE_URL`, `P3_ADMIN_HOST` different from the public host, empty `P3_BASE_PATH`, two different
  random secrets (`P3_ADMIN_KEY`, `P3_SIGNING_SECRET`, 24+ chars), `P3_ALLOW_UNAPPROVED_PRICING=false`,
  `P3_MAIL_MODE=smtp`, `P3_DAILY_AI_SPEND_CAP_USD`, `P3_SUPPORT_EMAIL`, `P3_DATA_DIR` outside the checkout.
- The AI mode is **locked to the configuration** in production (`P3_LOCK_MODE` implied): the developer switch answers
  409, `var/runtime.json` is ignored, there is no silent fallback to mock. Mock artefacts cannot be produced in live.
- **Route separation:** `/dev`, `/api/dev/*`, `/showcase`, `/api/showcase`, `/docs`, `/redoc`, `/openapi.json` exist only
  where developer tools are on (never in production); with `P3_ADMIN_HOST`, `/admin`, `/api/admin/*` answer only on
  that host (404 elsewhere). The `#developer` panel opens only where the server says tools exist (`window.__p3`).
- **Security headers** on every answer (nosniff, X-Frame-Options DENY, Referrer-Policy, Permissions-Policy, CSP,
  HSTS over https); `X-Robots-Tag: noindex` on the Admin, the tools and the API. Media errors are never cached.
- **Public `/api/health`** says only `ok` (plus the AI mode outside production); the full picture is
  `GET /api/admin/health` (admin key). Access-log lines have `token=`, `sig=`, `key=` values redacted.
- `/robots.txt` (staging disallowed, production allowed with the sitemap), `/sitemap.xml`, `/favicon.ico`, the robots
  meta (index only in production), the meta description (names the products on offer).

### 2. Customer availability per product — commit `2c988cb`
- Independent switches **Rings available to customers** (ON) and **Charms available to customers** (OFF) in Admin →
  Settings → Products; a future product joins `products.All` and gets its own `<plural>_available` key. The stored
  `charms_available` value was preserved (proto: both ON).
- OFF = not on the Design screen, not in the gallery or its filters, not in the sitemap, not in the wording; a start
  request is refused (400 `unknown_product`). The Admin configures everything regardless and previews hidden products.
- The site's wording (hero, how-it-works, FAQ, Materials & Pricing, Terms, shipping/returns, Design Studio, checkout,
  order page, meta description) follows the products on offer ("ring" / "charm" / "piece"); gallery filters are
  generated. API: `PUT /api/admin/products/availability {"product", "available"}`.

### 3. Credits — commit `1a65c58`
- **Model** (section D): a credit is a customer-triggered action — 1 per design request (its four images), 1 per
  refinement request, 1 per explicitly requested additional option, 1 per 360° movie, 0 per 3D model. Reserved
  atomically against the allowance when the action is created (parallel requests cannot overspend), charged once it
  delivers, released when every image of it fails; rebuilt from the job tables at every start. (Corrected on
  2026-10-07 after the Maison Dusk / C-1016 case, which had been charged per image; the counters were recounted.)
- **No silent paid retries:** a duplicate image is a failed, retryable option (never re-requested by the app;
  `max_duplicate_retries_per_slot = 0`); "Generate another option" is the customer's explicit new credit; a failed or
  timed-out slot is shown, never resubmitted. The fal.ai client library's own HTTP retries remain for idempotent
  reads (status/result); submissions are made once by the app (a timed-out submission keeps its request id and is
  never resubmitted).
- **Internal usage:** the Admin's "Make a new movie" and every Hi3D request are recorded as `internal` usage and never
  charged to a customer; a second Hi3D request for an image joins the one in flight.
- **Cost:** every submission is recorded with its estimated list-price cost (`cost_usd`, `cost_source`; Admin →
  Pricing → AI prices). **Daily cap:** `P3_DAILY_AI_SPEND_CAP_USD` stops paid submissions for the day
  (`spend_cap_reached`; the customer reads "AI generation is paused for today"). Admin → Settings → Products → Credits
  shows the tariff (editable) and the day's spend.
- **Request limits** (`P3_RATE_LIMITS`): paid starts 40/h per account, 80/h per IP; sign-in attempts 30/15 min per IP;
  registrations 10/15 min per IP, 5/h per email; 429 with Retry-After. Client IP = first `X-Forwarded-For` (nginx).

### 4. Privacy and sharing — commit `b214d96`
- A shared master design answers with its images and name only — never its prompt, reference image or refinement
  words; a design forked from someone else's master keeps the master's prompt private.
- Gallery publication records **whose design it is** (`xjet` / `customer`) and, for a customer's, a **consent note**
  (who agreed, when, how) — required (`consent_required`). The wording of the consent request to customers is a
  pending legal decision (section H).
- The share link name is assigned at publication; `GET /api/gallery/{id}/share` no longer writes.
- Registration answers the same for a new, pending or registered email; the log keeps a masked address; a used
  verification link reveals nothing; sign-out forgets the profile kept in the browser; uploaded photos get their EXIF
  orientation applied (and dropped).

### 5. Commerce truths — commit `ec44490`
- Orders carry `confirmation_email` = `sent` (left through SMTP) / `pending` / `failed` / `not_sent` (outbox mode) /
  `not_configured`; quote requests carry `email_status`. The confirmation page, the toast and the quote dialog say
  exactly that. **Never "a confirmation has been sent" unless it was.**
- Emails never say "reply to this email" at a no-reply address: they name `MAIL_REPLY_TO` / `P3_SUPPORT_EMAIL` or
  "use the Contact page"; `Reply-To` is `MAIL_REPLY_TO` when configured.
- `P3_STAFF_NOTIFY_EMAILS`: staff are emailed about new orders and quote requests (events `staff_notified` /
  `staff_notify_failed`); nothing is sent or invented when empty.
- The support address on the site comes from `P3_SUPPORT_EMAIL` (required in production; the Pipeline 2 address
  `atelier@xjet3d.com` remains the development fallback only — **confirm it**, section J).
- A double click adds one bag line. Production / QC / shipped / completed need every line's 3D result for the ordered
  size and material to be complete (measured and unflagged, or accepted by the Admin); an exception needs a note and is
  recorded as one. The STL is prepared from the **ordered option** only (`model_of_other_option` otherwise).
- "Review & place order" instead of "Review & pay".

### 6. SEO / performance — commit `ec44490` (with batch 5)
- Open Graph / Twitter tags and a canonical link on the home page (the share page keeps the design's own tags);
  design polling pauses while the tab is hidden.

### 7. Operations — the commit that adds this document
- `deploy/production/`: nginx (two hosts, TLS, defence-in-depth refusals, login throttle), systemd unit with
  `EnvironmentFile` and hardening, nightly backup timer, README with the exact installation and deploy steps.
- `.env.production.example`: every variable with purpose, placeholder, required/optional, secret, provider.
- `scripts/backup.sh` (consistent SQLite copies + assets, pruning), `scripts/restore.sh`, `scripts/verify-production.sh`
  (the read-only checks every deploy must pass), `scripts/deploy-proto.sh` (the proto procedure: in-flight check,
  backup, fast-forward to pushed main, restart, health, checks).

---

## C. Environment variables (complete list)

| Name | Purpose | Required (prod) | Placeholder / default | Secret | Provider |
|---|---|---|---|---|---|
| `P3_ENV` | `development` \| `production` (validation, lock, no dev tools) | **Required** | `production` | no | — |
| `P3_PUBLIC_BASE_URL` | Public https origin (emails, share links, sitemap, OG) | **Required** | `https://atelier.xjet3d.com` | no | — |
| `P3_ADMIN_HOST` | Host that alone answers the Admin and its API | **Required** | `admin.atelier.xjet3d.com` | no | — |
| `P3_BASE_PATH` | URL prefix; **empty** in production | Required empty | `` | no | — |
| `P3_DATA_DIR` | Data directory outside the checkout | **Required** | `/srv/atelier/data` | no | — |
| `P3_ADMIN_KEY` | Admin sign-in key | **Required** | random 24+ chars | **yes** | — |
| `P3_SIGNING_SECRET` | Signs download links (≠ admin key) | **Required** | random 24+ chars | **yes** | — |
| `P3_PROVIDER` | `fal` in production | **Required** | `fal` | no | fal.ai |
| `FAL_KEY` | fal.ai API key (use a key with a spending limit) | **Required** | `<key>` | **yes** | fal.ai |
| `P3_DAILY_AI_SPEND_CAP_USD` | Paid submissions stop for the day at this estimate | **Required** | e.g. `50` | no | — |
| `P3_ALLOW_UNAPPROVED_PRICING` | Must be false | Required false | `false` | no | — |
| `P3_PRICING_PROFILE` | The approved price profile | Optional | `config/pricing_profile.json` | no | — |
| `P3_PAYMENT_PROVIDER` | Only `none` exists | Optional | `none` | no | — (decision, F) |
| `P3_MAIL_MODE` | `smtp` in production (`outbox` sends nothing) | **Required** | `smtp` | no | — |
| `SMTP_SERVER` / `SMTP_PORT` / `SMTP_PREFER_IPV4` | The relay | **Required** (server) | `<relay>` / `25` / `true` | no | Microsoft 365 relay (as P2) |
| `MAIL_FROM` / `MAIL_FROM_NAME` | Sender | **Required** | `no-reply@xjet3d.com` / `XJet Atelier` | no | — |
| `MAIL_REPLY_TO` | Where replies go (a read mailbox) | Recommended | `atelier@…` | no | — |
| `P3_SUPPORT_EMAIL` | The support address shown on the site and in emails | **Required** | `<confirmed address>` | no | — |
| `P3_STAFF_NOTIFY_EMAILS` | Recipients of new-order / quote-request notes | Optional | `orders@…,sales@…` | no | — |
| `P3_RATE_LIMITS` | Per-IP / per-account request limits | Optional | `true` | no | — |
| `P3_LOCK_MODE` / `P3_NO_DEV_TOOLS` | Implied by production | Optional | `true` | no | — |
| `P3_ACCOUNT_PROVIDER` | `local` (only one exists) | Optional | `local` | no | — |
| `P3_POLL_INTERVAL_S` / `P3_MAX_TRANSIENT_POLL_ERRORS` / `P3_MOCK_LATENCY_S` | Tuning | Optional | defaults | no | — |

---

## D. Credits model

- Allowance per account (`max_generations`, default **10 credits**; Admin → Users "Credits allowance").
- A credit is a **customer-triggered action, not an output file**: **1 per design request** (its four images),
  **1 per refinement request** (its four images), **1 per explicitly requested additional option** (one click,
  however many slots it retries), **1 per finished 360° movie**, **0 per 3D model** (XJet's production cost).
  Selecting an option, changing the material or the size and reusing an existing movie cost nothing. Example:
  10 credits = 4 design requests + 2 refinements + 4 movies. Tariff editable in Admin → Settings → Products →
  Credits (`p3/credits.py`).
- Reserved before the action is created (atomic; parallel requests cannot overspend), charged once it delivers (its
  first ready image; a finished movie), released when every image of it fails / times out / duplicates. The
  Admin's movies and all Hi3D requests are internal, never charged. Reservations are rebuilt at startup.
- Historic usage before 2026-10-07 was charged per finished movie only. The counters of every account on proto were
  recounted on 2026-10-07 under this rule (charged movies + requests that delivered since the credits deploy) after
  the Maison Dusk / C-1016 case, which had been charged per image.
- Every submission records its estimated list-price cost; the daily spend cap is enforced before each paid submission.
- **Open decision (business):** the default allowance for self-registered customers (10 today), whether credits can
  be bought, and the message shown at zero ("Contact us to extend your allowance").

## E. Production safety summary

Startup validation (section B.1), locked live provider, no mock fallback, approved pricing required, explicit public
URL, secrets through `EnvironmentFile`, security headers, safe health, rate limits, sanitized customer errors (the
provider's words never reach a customer), scrubbed logs, backups/restore scripts, data outside the checkout, daily AI
spend cap, no silent paid retries, credits reserved before paid work.

## F. Payment — explicit blocker / decision

No payment provider is connected (`P3_PAYMENT_PROVIDER=none`): an order is a **reservation**; the site, the
confirmation page and the emails say "payment is arranged after you place the order" and never fake a payment. The
Admin records payments by hand (with a note). **Requires business approval:** choose and contract a provider
(hosted/tokenised form so card details never reach XJet), decide tax and shipping rules, then implement the adapter in
`p3/payments.py` (`Begin` / `Confirm` + a webhook route). **Blocks production** only if online payment is required at
launch; the reservation model is truthful and can launch as is if the business accepts it.

## G. Production data decision

**Recommended: a fresh database** (`/srv/atelier/data`, empty; the Admin re-creates the gallery from XJet's own designs
by uploading/generating them anew — paid generations). Alternative: a **cleaned migration** of proto's data, which
would require removing: every customer account and token (`accounts.db` — all three proto accounts are test
accounts), every customer design, bag line, order and quote request, session events, the outbox, the usage events,
all `var/dev` meshes/STLs of test rings, and keeping only the gallery master designs made by XJet staff (with their
candidates, movies and meshes) and the settings tables (products, prices, models). The cleaning script is not written;
if the migration is chosen, it must be written and tested against a copy first. **Do not move proto data into
production without this decision.**

## H. Pricing, legal and content checklist — Requires legal counsel / business approval

- Pricing: approve `config/pricing_profile.json` (Admin → Pricing & Materials; `pricing_profile_approved` in
  `/api/admin/health`), the material prices and the charm fixed prices (set on proto 2026-10-07: silver 69/109/159,
  steel 49/69/99, vermeil 59/99/139 for 10/14/18 mm — confirm), gold "quoted individually", shipping ($0 standard /
  $45 express, ETAs), import duties / VAT statement, promo policy.
- Legal texts in `web/index.html` are marked "Draft pending legal review": Terms of Service (reservation model,
  made-to-order no-return clause, non-exclusive design, liability cap), Privacy (data collected, retention, AI
  providers as processors — fal.ai receives prompts and images), Shipping & Returns, the registration consent, the
  consent wording for publishing a customer's design in the gallery, cookie/localStorage notice (no third-party
  cookies or analytics today), company identity and address, the "AI renderings" disclaimer.
- Support address, staff recipients, reply-to mailbox (section C).
- Retention policy for removed designs' media (soft-removed designs keep their files; a deletion schedule is a decision).

## I. Accessibility (WCAG 2.2 AA) — practical review, not a certification

Done over the session: icon navigation with visible hover/focus labels and `aria-label`s, dialogs with focus trap and
Escape, `role="switch"` / `radiogroup` on the Admin controls, form fields with labels, `inputmode` on email/phone,
`aria-live` waiting states, reduced-motion respected for the rotating status line. **Gaps to work through before
claiming conformance:** colour contrast of the gold accent (#8A6420 on light backgrounds in small text) and of muted
grey text (#6F6F6F) against #F8F8F8; keyboard operation of the 360° movie controls and the gallery lightbox
(arrow/escape handling exists, needs a full pass); alt texts of generated images (currently the design title); focus
order on the Design screen after a generation completes; announcing credit errors (402) to screen readers; the size
guide table headers; a skip-to-content link. An external audit is advised.

## J. Instructions for Dov (20 steps, in order)

1. Choose the production host (a Linux VM with Python 3.12+, nginx; ≥ 50 GB disk; proto's `var/dev` alone is 4.5 GB).
2. Create the DNS records `atelier.xjet3d.com` and `admin.atelier.xjet3d.com` → the host. *(No DNS change was made.)*
3. Issue TLS certificates for both names (certbot or the company CA); install nginx from `deploy/production/nginx-atelier.conf`.
4. Create the service user and directories (`deploy/production/README.md`, "First installation").
5. Clone the repository at the release tag; create `.venv`; install `requirements.txt`.
6. Copy `.env.production.example` to `/etc/xjet-atelier/env` (0640 root:atelier) and fill every REQUIRED value.
7. Generate `P3_ADMIN_KEY` and `P3_SIGNING_SECRET` (two different random strings, 24+ chars). Keep them in the company's password manager.
8. Create a **dedicated fal.ai key with a spending limit** for production; set `FAL_KEY` and `P3_DAILY_AI_SPEND_CAP_USD`.
9. Configure the mail relay (`SMTP_*`, `MAIL_FROM`, `MAIL_REPLY_TO`), ask IT to allow the host's IP on the relay; set `P3_MAIL_MODE=smtp`.
10. Confirm the support address with the business; set `P3_SUPPORT_EMAIL`; set `P3_STAFF_NOTIFY_EMAILS`.
11. Decide the production data (section G); for a fresh start leave `/srv/atelier/data` empty.
12. Install `xjet-atelier.service`; start it; read the journal — it lists anything still missing and refuses to start until fixed.
13. Sign in to the Admin on the admin host; in Settings → Pricing & Materials approve the price profile; check the charm prices; in Settings → Products set Rings/Charms availability and the credits tariff; in AI models & prompts check the active versions.
14. Add the gallery master designs (XJet designs; the publication dialog records whose design it is).
15. Run `scripts/verify-production.sh https://atelier.xjet3d.com https://admin.atelier.xjet3d.com` — all checks must pass.
16. Enable the nightly backup timer; run `scripts/backup.sh` once; test `scripts/restore.sh` on a copy.
17. Set up monitoring: an external uptime check on `https://atelier.xjet3d.com/api/health` every minute, disk space alert (< 10 GB), journal error alerts; `journalctl` size limits are set in the unit.
18. Get the legal texts approved (section H) and update `web/index.html` (Terms/Privacy/Shipping) — then redeploy.
19. Decide the payment model (section F) and the credits default (section D).
20. Do one real end-to-end order on production with a test customer (a small paid generation), check the emails, the Admin order page, the 3D preparation and the STL; then open the site.

## K. Commits, tests and proto verification

Commits on `main` (this session): `59f3373` (posture), `2c988cb` (products), `1a65c58` (credits), `b214d96` (privacy and
sharing), `ec44490` (commerce truths and SEO) and the operations commit that adds this document (`git log --oneline`
from `59f3373`). Every batch was tested
with the full suite (`pytest`, ~300 tests, mock provider only — no paid AI call was made for this work) and deployed to
proto with `scripts/deploy-proto.sh`'s procedure (in-flight check, database backup under `~/p3-backups/`, fast-forward
to pushed main, restart, health). Proto keeps `P3_ENV` unset (development posture): the developer tools stay available
there, robots say `Disallow: /`, and both products are available to customers.

## L. Launch checklist

- [ ] Host, DNS, TLS (J.1–3)  - [ ] `/etc/xjet-atelier/env` complete; the service starts (J.6–12)
- [ ] fal.ai production key with a spending limit; daily cap set  - [ ] SMTP relay allows the host; a test email arrives
- [ ] Support address confirmed; staff recipients set  - [ ] Price profile approved; charm prices confirmed
- [ ] Products availability and credits tariff set  - [ ] Gallery masters published (consent where needed)
- [ ] `scripts/verify-production.sh` passes on both hosts  - [ ] Backups run nightly; restore tested
- [ ] Monitoring and alerts in place  - [ ] Legal texts approved and deployed
- [ ] Payment model decided (reservation vs provider)  - [ ] One real end-to-end order verified
- [ ] Production data decision executed (fresh or cleaned)
