# Cookie consent and analytics

How XJet Atelier measures its site: cookie consent first, then Google Analytics 4 (GA4) and Microsoft Clarity, and a
small set of funnel events. The owner's decisions of 2026-10-13 govern: Basic Consent Mode, `design_session_started`,
consent on the quote page too, privacy contact atelier@xjet3d.com, Proto and Atelier measured apart, nothing on Atelier
before the owner approves the tested commit.

## Principles

- **Nothing optional before consent.** No Google tag, no Clarity script, no analytics cookie and no request to either
  until the visitor allows Analytics & Performance. Reject optional leaves the site fully working.
- **Basic Consent Mode.** The Google tag is not loaded at all before consent (no cookieless pings). Once Analytics is
  accepted: `ad_storage`, `ad_user_data`, `ad_personalization` and `analytics_storage` start `denied`, then only
  `analytics_storage` is updated to `granted`. Atelier runs no advertising; Google signals and ad personalization are off.
- **Clarity follows the same Analytics choice** (its consent API v2: `analytics_Storage: granted`, `ad_Storage: denied`).
  It never runs on the quote page, the Admin or the developer tools, and never starts on an address that carries a secret.
- **No personal data in analytics.** Only listed events with listed fields, values from fixed lists, numbers or design
  references. Page addresses lose their query string and fragment (UTM tags excepted) — quote links and sign-in links
  carry secret tokens.
- **Each environment measures itself.** Off by default everywhere; the tools run only on the host configured with them.

## How it works

| Part | Where |
|---|---|
| Settings (on/off, host, GA4 ID, Clarity ID) and the Content-Security-Policy additions | `p3/analytics.py` |
| Admin card: *Settings → System → Analytics & cookies* (changes logged) | `p3/admin.py`, `web/admin.html`, `web/admin.js` |
| The banner, Privacy preferences (Cookie Settings in the footers), the GA4 and Clarity loaders, the event allowlist, clean addresses | `web/consent.js` |
| The site's screens and funnel events | `web/app.js` (`measure`, `measureScreen`) |
| The quote page: consent, its events (viewed, approved + order, declined), never Clarity | `p3/quotes.py`, `web/quote.html` |

- A customer page gets `window.__p3.analytics = {version, host, ga4, clarity}` only while analytics is on, a host is set
  and at least one ID exists; otherwise `null`, no banner, and the policy stays `script-src 'self'`, `connect-src 'self'`.
- With a tool on, customer pages (never `/admin`, `/dev`, `/api/…`, `/static/…`) add that tool's hosts to the
  Content-Security-Policy — Google's list for Analytics without Ads features, Microsoft's for Clarity.
- The browser starts a tool only when `location.hostname` equals the configured host, so an ID cannot run elsewhere.

### The consent record

`localStorage["atelier_consent"] = {"v": 1, "analytics": true|false, "marketing": false, "vendors": ["ga4", "clarity"],
"at": "<ISO time>"}` — nothing about the person. The banner comes back when the consent version changes or when a tool
is switched on that the decision did not cover. Withdrawing consent (Cookie Settings → Reject optional, or Analytics &
Performance off → Save preferences) tells GA4 (`ga-disable-<ID>`, consent update denied) and Clarity
(`clarity('consent', false)`), deletes their first-party cookies (`_ga`, `_ga_<id>`, `_clck`, `_clsk`) and reloads the
page. Changing the wording or the look of the banner keeps the version: visitors are asked again only for a new version
or a new tool.

### What the visitor sees (the owner's wording, 2026-10-14)

Two layers, in Atelier's own look (warm ivory surface, beige hairline, Cinzel title, Inter text, spaced capitals on
buttons, 6 px corners; the primary action a warm charcoal, never pure black). The styles live in `consent.js` itself, so
the quote page shows the same.

- **The banner** (a card in the bottom-left corner; a sheet along the bottom on phones): *Your privacy matters* — what
  the site uses and why, in one paragraph, and a Privacy Policy link. It names no tool, cookie or retention period.
  **Accept all** (primary), **Reject optional** (beside it, the same size, outlined), **Manage preferences** (a quiet
  text action).
- **Privacy preferences** (Manage preferences, or Cookie Settings at the foot of every page; a dialog, a bottom sheet on
  phones):
  - **Essential** — *Always active*: sign-in, designs, bag, checkout, security and the privacy choice itself.
  - **Analytics & Performance** — the one switch (On / Off). *Learn more* names the tools switched on (Google
    Analytics 4, Microsoft Clarity), says they start only after consent, are never used for advertising, receive no
    customer details or design prompts, and that Clarity masks sensitive information.
  - **Marketing & Advertising** — *Not currently used*: no tool and no switch. Adding Google Ads, Meta Pixel or another
    marketing platform later means a real switch here, a new consent version and its own Consent Mode signals.
  - Actions: **Reject optional**, **Save preferences**, **Accept all** (primary). Accept all allows Analytics &
    Performance only; `marketing` stays `false`.

The Privacy page (`web/index.html`, view `privacy`) carries the details: the three categories, the tools by name, what
Clarity masks, retention, the withdrawal, and a *Cookie details* box with the names below.

### Essential storage (no consent needed)

| Name | Kind | Purpose |
|---|---|---|
| `atelier_session` | Cookie (HttpOnly, SameSite=Lax, Secure) | Customer sign-in (AJW-39; 30 days idle, 180 days at most) |
| `atelier_oauth` | Cookie (HttpOnly, 10 minutes) | A Google or Apple sign-in on its way back |
| `p3_session:<base>` | localStorage | The customer's sign-in (access code) and credit count |
| `p3_profile:<base>` | localStorage | The signed-in customer's name and email, shown in the page; removed at sign-out |
| `p3_state:<base>` | localStorage | The current design journey |
| `p3_gallery_pending:<base>`, `p3_fav_pending:<base>` | localStorage | A gallery design chosen or ♥-ed before sign-in |
| `p3_pending_panel` | sessionStorage | What the visitor was doing before a Google or Apple sign-in |
| `atelier_consent` | localStorage | The cookie choice itself |
| `atelier_admin_session`, `atelier_admin_oauth` | Cookies (HttpOnly) | Admin sign-in (staff only) |
| `p3_dev_key`, `p3_admin_filters` | sessionStorage | Developer tools and Admin filters (staff only) |

## Environments

| | GA4 | Clarity | Host |
|---|---|---|---|
| Atelier (production) | property "XJet Atelier — Production", stream `https://xjetatelier.xjet3d.com` | project "XJet Atelier — Production" | `xjetatelier.xjet3d.com` |
| Proto (functional tests) | placeholder `G-TEST0000` — nothing loaded or sent | placeholder `test0000` — nothing loaded or sent | `proto` |
| Development copies | off | off | — |

GA4 and Clarity measure **Atelier production only** (the owner's decision of 2026-10-13): Proto has no property or
project of its own. Proto tests the consent and analytics code with the placeholder IDs `G-TEST0000` and `test0000`:
the banner, the choice and every event run in the page, but neither tool is loaded, nothing is sent, and the page's
policy stays closed to both (`window.dataLayer` and `window.clarity.q` show what would have been sent). Atelier's IDs
are entered in Atelier's Admin only (Admin through the SSH tunnel: `docs/AUTH-OPERATIONS-AJW-39.md` §3a), after the
owner approved the release. Production reports can also filter on hostname = `xjetatelier.xjet3d.com` as a second
guard.

### GA4 property settings

Web stream → Enhanced measurement: **off** for *Page changes based on browser history events* (the site sends its own
page views with clean addresses), *Form interactions* and *Site search*. Data collection: Google signals off. Data
retention: 14 months. No Google Ads link. Internal-traffic filtering may be added later.

### Clarity project settings

Masking: **Balanced** (numbers and emails masked; inputs and drop-downs are always masked); the pages add
`data-clarity-mask="true"` to the account menu and chip, sign-in, the bag, checkout, the confirmation, My Account, the
quote request and the prompt echo. Cookie consent: **required** (the site passes the visitor's choice). No custom
identifiers are sent.

## Events

Sent only after consent, through `AtelierAnalytics.track()`; anything not listed is dropped. Page views: every screen,
sites pages at their own address, design-journey steps as `<base>/app/<step>` (`ai-studio`, `review`, `checkout`,
`order`, `confirmation`, `account`).

| Event | When | Fields |
|---|---|---|
| `sign_up` / `login` | registration sent / signed in with the code or a link | `method` (email · code · link) |
| `design_session_started` | a new design journey (a first prompt, or a gallery design) | `product_type`, `entry` (home · gallery) |
| `prompt_submitted` | a description or a change is sent | `product_type`, `kind` (new · refine), `has_reference_image`, `prompt_length` (short · medium · long) — never the text |
| `design_generation_started` / `_completed` / `_failed` | generation accepted / finished while watched / refused or failed | `product_type`, `kind` (new · refine · retry), `options_ready`, `options_failed`, `duration_s`, `reason` (content_policy · prompt_refused · no_credits · provider · error) |
| `design_selected` | an option chosen | `product_type`, `option` (1–4) |
| `gallery_design_opened` | a gallery design opened | `product_type`, `design` (the gallery design's public name) |
| `customize_opened` | Customize | `product_type`, `source` (results · gallery) |
| `movie_generation_started` / `_completed` / `_failed` | the 360° movie | `product_type`, `retry` |
| `material_selected` / `size_selected` | metal / size chosen | `product_type`, `material_id`, `material_group` / `size` |
| `add_to_cart` / `begin_checkout` | added to the bag / checkout opened | `currency`, `value`, `items` |
| `quote_requested` / `quote_viewed` / `quote_approved` / `quote_declined` | gold quote steps | `product_type`, (`material_group`), (`currency`, `value`) |
| `order_created` (+ GA4 `purchase`) | an order placed (checkout or approved quote) | `source`, `currency`, `value`, `item_count`; `purchase`: `transaction_id` (ORD-…), `items` |

Items are anonymous: `item_id` `custom-ring` / `custom-charm`, `item_variant` = the metal, `price`, `quantity`.
Not GA4 events: 3D production failures (back office, in the Admin) and abandoned quotes (from Atelier's own quote data
in the reporting layer).

## Testing (Proto)

1. The site works before any choice; Reject optional breaks nothing.
2. Rejected: no request to Google or Clarity, no `_ga`/`_cl*` cookie (browser network and cookies).
3. Accepted: `gtag/js`, then `g/collect` with `gcs=G101`; Clarity's tag; GA4 DebugView shows the events.
4. Changed later: Cookie Settings → Reject optional reloads without either tool and deletes their cookies.
5. Sign-in, generation, Customize, quotes, orders and the Admin work as before.
6. No personal data in any hit (checked per event; the quote page's events carry no name, email, address or token).
7. Admin and developer tools load neither tool; the quote page never loads Clarity.
8. Proto uses the placeholder IDs: nothing is loaded or sent anywhere (Atelier production is the only property and
   project).

## Reporting (Looker Studio, once data flows)

One dashboard, *XJet Atelier — Growth & UX Dashboard*: Search Console (impressions, clicks, CTR, position, queries,
landing pages, indexed pages), the GA4 funnel (visitor → design session → prompt → generated → selected → customize →
quote/bag → order, with step conversion and the largest drop), device/browser/country and engagement, and performance
(Core Web Vitals — LCP, CLS, INP — from the Chrome UX Report once Atelier has enough traffic to appear there, or from
web-vitals events added later). Clarity stays the drill-down for recordings and heatmaps. A short periodic summary
("Top Atelier Insights") comes after the data is shown to be reliable.
