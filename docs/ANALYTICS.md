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
| Settings (on/off, host, each tool's ID and on/off), the tools (`Providers`) and the Content-Security-Policy additions | `p3/analytics.py` |
| Admin card: *Settings → System → Analytics & cookies* (changes logged) | `p3/admin.py`, `web/admin.html`, `web/admin.js` |
| The banner, Privacy preferences (Cookie Settings in the footers), the GA4 and Clarity loaders, the event allowlist, clean addresses | `web/consent.js` |
| The site's screens and funnel events | `web/app.js` (`measure`, `measureScreen`) |
| The quote page: consent, its events (viewed, approved + order, declined), never Clarity | `p3/quotes.py`, `web/quote.html` |

- A tool may load only when **all** of these hold: analytics is offered on this environment, the visitor accepted
  Analytics, the page is on the configured host, the tool is switched on, and its ID is valid. There is no fallback ID.
- A customer page gets `window.__p3.analytics = {version, host, ga4, clarity}` only while at least one tool may load;
  a tool that may not load is `null` there, never its ID. Otherwise the whole value is `null`, there is no banner, and
  the policy stays `script-src 'self'`, `connect-src 'self'`.
- Customer pages (never `/admin`, `/dev`, `/api/…`, `/static/…`) add the hosts of the tools that may load, and only
  theirs, to the Content-Security-Policy — Google's list for Analytics without Ads features, Microsoft's for Clarity. So
  with GA4 configured and Clarity empty, no Clarity script or request can even be attempted.
- The browser starts a tool only when `location.hostname` equals the configured host, so an ID cannot run elsewhere.

### Settings: entered in the Admin, never in the code

The IDs belong to each environment and live only in its database (`product_settings`, key `analytics`, every change in
`product_settings_log`):

```json
{"enabled": true, "host": "xjetatelier.xjet3d.com",
 "providers": {"ga4": {"enabled": true, "id": "G-…"}, "clarity": {"enabled": true, "id": "…"}}}
```

Nothing copies one environment's IDs to another, and no ID is written in the application
(`tests/test_analytics.py` checks the code). A configuration saved before 2026-10-14 in the flat shape
(`{"ga4": …, "clarity": …}`, a tool on whenever its ID was set) is read as exactly that until the Admin saves again.

**The Admin card** (*Settings → System → Analytics & cookies*) has these parts:

- **Offer Analytics to visitors** (the switch) and the **Allowed site host** (one click fills in this server's own host).
- One block per tool, listed from `Providers`. Each has its ID field (with its format, e.g. `G-XXXXXXXXXX`), *Use … after
  consent*, and its saved state: *Not configured*, *Configured · off*, *Test ID · nothing sent*, *Configured ·
  analytics off* or *Configured · on after consent*.
- **Save changes** sends the whole form, then reads the server back. It says *Saved* only when the server holds what was
  entered; otherwise it says *Not saved as entered* and shows what the server holds. A tool cannot be switched on without
  its ID, and analytics cannot be offered without the host.
- **Saved on this server** always shows what the database holds: the switch, the host, each tool's ID and on/off, and
  who changed it and when.

**A marketing tool later** (Google Ads, Meta Pixel …) needs no change to this model. It takes:

- one more entry in `Providers` with `category: "marketing"`, its ID format and its policy hosts (the Admin card lists it
  and its ID is entered there like the others);
- its loader in `web/consent.js`, behind a real *Marketing & Advertising* switch, with its own Consent Mode signals;
- a new `ConsentVersion`, so every visitor is asked again.

Until then there is no marketing tool: the Admin says so, and visitors see *Not currently used* with no switch.

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
buttons, 6 px corners). The primary action wears Atelier's gold, the site's `btn-gold`: a warm gold gradient with dark
text. The styles live in `consent.js` itself, so the quote page shows the same.

- **The banner** (a card in the bottom-left corner; a sheet along the bottom on phones): *Your privacy matters* — what
  the site uses and why, in one paragraph, and a Privacy Policy link. It names no tool, cookie or retention period.
  **Accept all** (primary), **Reject optional** (beside it, the same size, outlined), **Manage preferences** (a quiet
  text action).
- **Privacy preferences** (Manage preferences, or Cookie Settings at the foot of every page; a dialog, a bottom sheet on
  phones):
  - **Essential** — *Always active*: sign-in, designs, bag, checkout, security and the privacy choice itself.
  - **Analytics & Performance** — the one switch (On / Off). *Learn more* names only the tools that may load (Google
    Analytics 4, Microsoft Clarity). It says they start only after consent, are not used for advertising and receive
    no customer details or design prompts, and that Clarity is configured to mask sensitive information.
  - **Marketing & Advertising** — *Not currently used*: no tool and no switch. Adding Google Ads, Meta Pixel or another
    marketing platform later means a real switch here, a new consent version and its own Consent Mode signals.
  - Actions: **Reject optional**, **Save preferences**, **Accept all** (primary). Accept all allows Analytics &
    Performance only; `marketing` stays `false`.

The Privacy page (`web/index.html`, view `privacy`) is written for customers:

- the three categories, the tools by name, what Clarity masks, and how to withdraw consent;
- a summary of each technology with its purpose and how long it lasts, only with verified figures:
  - Google's default cookie lifetime, up to 2 years, and this property's 14-month data retention;
  - Microsoft's documented Clarity retention: recordings 30 days, or 9 months when kept; heatmaps and page statistics
    9 months;
  - the sign-in cookie's 30 days after the last visit, 180 days at most;
- the exact names, only behind *View technical cookie names*.

The page names no staff cookie and none of Microsoft's own cookies, whose use with this configuration is not verified.

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
page views with clean addresses), *Scrolls* (the site sends its own `scroll`, below; switched off when that release
goes live, so a screen is never counted twice), *Form interactions* and *Site search*. Data collection: Google signals
off. Data retention: 14 months. No Google Ads link. Internal-traffic filtering may be added later.

### Clarity project settings

Masking: **Balanced** (numbers and emails masked; inputs and drop-downs are always masked); the pages add
`data-clarity-mask="true"` to the account menu and chip, sign-in, the bag, checkout, the confirmation, My Account, the
quote request and the prompt echo. Cookie consent: **required** (the site passes the visitor's choice). No custom
identifiers are sent.

## Events

Sent only after consent, through `AtelierAnalytics.track()`; anything not listed is dropped.

**Page views (since 2026-10-17): one per screen, at its real address, under its own title.** A screen is its address
and its title (`web/app.js` `measureScreen`, `web/consent.js` `sendPage`):

| Screen | Page location | Page title |
|---|---|---|
| The site's pages | `/`, `/gallery`, `/materials`, `/technology`, `/about`, `/faq`, `/contact`, the legal pages | the page's own (`p3/sitepages.py` `Meta`) |
| A gallery design (its page, or its dialog opened from a card) | `/gallery/<slug>` | `<Design> · Custom Ring Design · XJet Atelier` |
| Design · My Designs · Favorites | `/design` · `/my-designs` · `/favorites` | Design Studio · My Designs · Favorites |
| Customize · Bag · Checkout | `/customize` · `/bag` · `/checkout` | Customize · Your Bag · Checkout |
| An order's confirmation | where it was reached (`/checkout`) | Your Order |
| My Account | `/` (its `#account` is not sent) | My Account |

- The page served at a step's address carries that step's title (`ScreenTitles`), so a reload or a link sends the same
  screen the app then shows: one page view, not two. The same screen again sends nothing.
- A burst of screens counts once, for the screen it ends on: a reload's restore passes through the Design screen on its
  way to My Designs, Customize or Checkout.
- Opening a gallery design sends its page view; closing it sends the screen behind (Back and Forward too). An old
  `#faq` link counts as the FAQ page.
- A sign-in's return (`#auth=…` from Google, Apple or an email link, `#token=…`) is no screen of its own: its page view
  is the screen the sign-in opens, or the page it came back to when it did not complete. Google's and Apple's sign-in
  pages are never sent as a referrer.
- Every event carries the screen it happens on (`page_location`, `page_title`), after that screen's page view: the
  address moves with each screen (`gtag('set')`, never fixed in `config`), for GA4's own events too.
  `gallery_design_opened`, `customize_opened` and `begin_checkout` come right after the page view of the screen they open.

| Event | When | Fields |
|---|---|---|
| `sign_up` | a new account: made with Google or Apple, or its email confirmed by its link (the six-letter code's registration: when sent) | `method` (email · google · apple) |
| `login` | signed in: a password (an email or a username), Google, Apple, the six-letter code or its link | `method` (password · google · apple · code · link) |
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
| `order_created` | an order placed (checkout or approved quote) — not a payment | `source`, `currency`, `value`, `item_count` |
| `scroll` | the visitor scrolled a screen to 90 % (once per screen; only a page taller than the window, only their own scrolling — the window or the screen's `<main>`) | `percent_scrolled` (90) |

Items are anonymous: `item_id` `custom-ring` / `custom-charm`, `item_variant` = the metal, `price`, `quantity`.

The method of a sign-in that returns from Google, Apple or an emailed link is the one the server's session records
(`/api/auth/session` `method`); a confirmed email is a new account (`#auth=created`).

**`purchase` is kept for a payment received, and is not sent today** (since 2026-10-17; it used to duplicate
`order_created`). No payment is taken online (`P3_PAYMENT_PROVIDER=none`): staff mark an order paid in the Admin. A
`purchase` at that moment would be sent by the server (GA4 Measurement Protocol): it needs a Measurement Protocol API
secret, the visitor's GA4 client id kept with the order (only with Analytics consent) and a line on the Privacy page —
the owner's decision. A payment provider, once connected, confirms payments the same way.

### Event hierarchy (which events are key events)

Not every event is a key event: a few business outcomes are, the funnel is read in an exploration, and the rest is
for diagnosis.

| Level | Events | GA4 |
|---|---|---|
| Primary business conversions | `order_created` (an order placed, with its value), `quote_requested` (a gold quote: a sales lead); later `purchase` (a payment received) | key events |
| Secondary conversion signals | `sign_up` (a new account), `design_generation_completed` (a design made: four options ready) | key events, reported apart from the primary ones |
| Funnel steps | page views of `/design`, `/customize`, `/bag`, `/checkout`; `gallery_design_opened`, `design_session_started`, `prompt_submitted`, `design_selected`, `customize_opened`, `add_to_cart`, `begin_checkout` | one funnel exploration; never key events |
| Diagnostics | `login`, `design_generation_started` / `_failed`, `movie_generation_*`, `material_selected`, `size_selected`, `quote_viewed` / `_approved` / `_declined`, `scroll`; GA4's `first_visit`, `session_start`, `user_engagement` | never key events |
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
