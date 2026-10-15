# Search engines (SEO)

**Code:** `p3/sitepages.py` (pages, titles, descriptions, the FAQ, structured data, the server copy of a page),
`p3/app.py` (`RenderPage`, the page routes, `robots.txt`, `sitemap.xml`), `p3/admin.py` (`/api/admin/site-indexing`),
`web/app.js` (`PAGE_PATHS`, `FLOW_PATHS`, `pageLink`, `navigateTo`, `routePath`, `_onHistory`, `_openFlow`; the gallery
dialog's address: `galleryHref`, `galleryClick`, `openGallery`, `closeGallery`). **Tests:** `tests/test_seo.py`,
`tests/test_seo_sitemap.py`, `tests/test_gallery_routes.py`, `tests/test_sharing.py`, `tests/test_flow_routes.py`.

## Indexing is a switch, off by default

Admin → Settings → System → **Search engines**. Off (the default): every page says `<meta name="robots" content="noindex">`
and `robots.txt` disallows the site — proto and every test copy stay out of search engines. On: the public pages say
`index,follow`, `robots.txt` allows them and names the sitemap. A production configuration (`P3_ENV=production`) is
always indexable. `P3_ENV` is not involved otherwise. Every change is logged with the product settings (who, when).

Only the public production site (Atelier) turns it on.

## The public pages

| View (app) | URL under the base path | H1 |
|---|---|---|
| home | `/` | Custom Jewelry, Designed by You |
| inspiration | `/gallery` | What Will You Create? |
| faq | `/faq` | Frequently Asked Questions |
| materials | `/materials` | Real metal, one fixed price per … (follows the products) |
| technology | `/technology` | NanoParticle Jetting™ |
| designers | `/about` | The Inkjet Revolution |
| shipping-returns, terms, privacy, contact | `/shipping-returns`, `/terms`, `/privacy`, `/contact` | the page's name |
| a gallery design | `/gallery/<name>` (below) | the design's name |

Each has its own `<title>`, meta description (true, following the products on offer), canonical link (the public origin:
`P3_PUBLIC_BASE_URL`, else the request's), Open Graph / Twitter tags and a sitemap entry. The links in the header, the
About menu, the phone menu, the footer and the legal pages are real `<a href>` links; a plain click opens the page in
place (`pageLink`), Ctrl/⌘/middle click opens its URL. An old `#faq`-style link opens the page at its URL.

**Real markup, not only a template.** The page asked for is in the HTML as real markup: `ServerView` puts a copy of the
view's Alpine `<template>` in the page (filled with this site's sentences, its state-dependent parts `x-cloak`-ed until the
script starts), and the copy removes itself when the visitor moves to another view — the template takes over from then on
(`ssrView`). The FAQ is written by the server (`Faq`): the page and its FAQPage data have one source. The gallery page
and the home page list every gallery design as a plain link to its page, the materials page the metals (blocks for
readers that run no scripts, removed as the page starts). How It Works is served exactly as written.

**Never pages:** the Admin, the API, `/verify` (sign-in links), `/quote` (quote links), `/dev`, `/showcase`, the API
docs, My Account (`#account`) and the customer's journey — `/design`, `/my-designs`, `/favorites`, `/customize`, `/bag`,
`/checkout` (`SitePages.FlowPaths`: the app alone, with no canonical link, social tags, structured data, server-side copy
or anything of a customer). They carry `X-Robots-Tag: noindex` (and a `noindex` meta tag where they are documents) and
are never in the sitemap. Only the API is disallowed in `robots.txt`: a crawler
can read a page's `noindex` only if it may fetch the page, and a disallowed URL can still be listed bare from links.

**The API docs** (`/docs`, `/redoc`, `/openapi.json`) are a developer aid, not part of the public site: wherever the site
is open to search engines (production, or Search engines ON) they answer 404; on proto and development copies they stay,
always `noindex`.

## Gallery design pages: `/gallery/<name>` (since 2026-10-15)

Every design in the public Inspiration Gallery has its own address, `/gallery/<name>` (`designs.share_slug`: set at
publication, never changed by a rename). The page is the Gallery with that design's dialog open
(`p3/app.py` `GalleryDesignPage`, `_DesignPage`):

- **Head:** its own `<title>` (`<name> · Custom Ring Design · XJet Atelier`, or Charm), meta description, one canonical
  link to itself, Open Graph / Twitter tags with the design's picture, `CreativeWork` and `BreadcrumbList` (Home →
  Inspiration Gallery → the design), and a preload of the dialog's picture.
- **For readers that run no scripts:** the design's name as the H1, its description and picture, a link back to the
  Gallery, and every *other* gallery design as a link (never a link to itself). On this page the Gallery's own heading
  is an H2; once the scripts run, the dialog's design name is the H1.
- **The cards** (home, Gallery, "See all designs") are real `<a href="…/gallery/<name>">` links (`galleryHref`). A
  plain click opens the dialog in place and adds a history step (`galleryClick` → `openGallery`); Ctrl/⌘/Shift/middle
  click and "Open link in new tab" open the design's page as links do. The address and the tab's title follow the dialog.
- **History:** Back closes the dialog (the page behind it, at its own address); Forward opens it again
  (`_onGalleryHistory`). The dialog's own controls (Escape, the backdrop, Back to gallery) step back through the step
  the click added, so Back / Forward stay in order; when the app closes it (Make it yours, a sign-in first), or after a
  reload, the page's own address takes the step's place. A reload or a copied address opens the same design again.
- **Share / Copy link** (customer and Admin) give `/gallery/<name>`.
- **Another spelling** (capitals, the design's current name after a rename): **301** to the canonical address, one hop.
- **Not found** (never published, taken out of the gallery, or of a product never yet on offer): **404**, `noindex` — the
  Gallery, saying "This design is no longer in the Inspiration Gallery". A design that is not in the public gallery (a
  customer's own, a fork, a hidden product's) is never reachable by name.
- **The address before, `/design/<name>`** (links already shared, bookmarks, the sitemap until 2026-10-15): **301** to
  `/gallery/<name>` (the query string kept: campaign tags), and only when the name belongs to a public gallery design;
  any other `/design/…` is a 404 `noindex` as before. No blanket redirect, no chain. Redirects answer
  `Cache-Control: no-cache`, so browsers ask again each time (a rollback is not stuck in their caches); search engines
  treat a 301 as permanent and move the old URL's standing to the new one.
- **Sitemap:** `/gallery/<name>` only, `<lastmod>` as below. The `/design/` URLs left the sitemap with this change; search
  engines follow their 301 when they next crawl them.
- **Analytics:** unchanged. Opening a design in place sends `gallery_design_opened`, not a page view; a design's page
  loaded directly is a page view of `/gallery/<name>` (as `/design/<name>` was). The app sends its own page views
  (`send_page_view: false`): GA4's *Enhanced measurement → Page changes based on browser history events* should be off,
  or GA4 may also count the address changes.

### When a product is switched OFF — the owner's policy (2026-10-15)

The product switch (Admin → Settings → Products) controls making, not existence. Switching a launched product OFF
pauses it: its gallery designs stay viewable and in search, read-only.

| State of the design's product | `/gallery/<name>` | `/design/<name>` | Sitemap | Gallery listing | ♥ Favorites | Production actions |
|---|---|---|---|---|---|---|
| ON (on offer) | 200, indexable | 301 | listed | listed | listed | all |
| OFF after its launch (paused) | 200, indexable, read-only: "Currently unavailable for customization or ordering" (its description says so too) | 301 | listed (its date moves when the switch does) | not listed | kept, marked | refused: 409 `design_unavailable` |
| Never on offer yet (pre-launch, Admin preview only) | 404 `noindex` | 404 | not listed | not listed | not listed | refused |
| Removed from the gallery (permanent removal) | 404 `noindex` | 404 | removed | not listed | dropped | — |

- **Production actions:** Make it yours, choosing an option, Customize, a customization change, a 360° movie,
  another option or a retry, the bag (a paused line stays, marked "Currently unavailable for customization or
  ordering", and cannot be ordered), checkout, an order, a quote request. The server refuses them
  (`products.RequireOffered`, 409 `design_unavailable`). The dialog shows the notice in place of its Make it yours
  call (`isOff`).
- **Every way in gets the same state:** the Gallery, ♥ Favorites, a link, history, an old shared link. The dialog
  rereads the state as it opens (the share answer's `available`), so a page opened before the switch catches up.
- **Launched:** a product has been on offer if it is ON now, ON by default (rings), or was switched ON at some time in
  the settings log (`ProductSettings.Launched`). Before that, its designs stay private, so an Admin can prepare a
  product's gallery in the preview without customers or search engines seeing it.
- **Admin preview:** a browser signed in to the Admin sees every product as ON.
- **Not listed while OFF:** the Gallery's listing, filters, badges and wording describe the products on offer, so a
  paused design is not listed there (the policy allows it to stay visible or not). It keeps its page, its links from
  outside, Favorites and the sitemap.

## The sitemap's `<lastmod>`

Every URL carries the date its own content last changed (`page_lastmod`): a fingerprint of the page's title,
description, canonical link, structured data and what a reader gets from the page itself — its text, the links'
targets and the pictures (address, alt), not its markup, so restyling a page is not a change — and, for a gallery
design, its name, image, product and address (`p3/sitepages.py` `PageFingerprint` / `DesignFingerprint`). A date moves
only when that page's content changes; nothing is dated by the request or by the address the sitemap is read at. A new
design starts at its publication date in the gallery; the site's own pages started at the first sitemap build after
2026-10-12 10:55 UTC (no earlier record of their content exists). Fingerprints carry a version: a record made another
way is renewed without moving its date. The sitemap is rebuilt at most once a minute.

## Structured data (JSON-LD) — only what is true

- Home: `Organization` (XJet Ltd., xjet3d.com, the XJet Atelier brand; the support address when one is configured) and
  `WebSite` (XJet Atelier).
- `/faq`: `FAQPage` with the ten questions and answers exactly as the page shows them.
- `/gallery/<name>`: `CreativeWork` (the design's name, picture, description and kind — Rings / Charms — made by XJet
  Ltd.) and `BreadcrumbList` (Home → Inspiration Gallery → the design).

No reviews, ratings, prices or availability anywhere, and no `Product`: the pages show no price (2026-10-13), Google's
product snippets need an offer, reviews or a rating, and a price is never invented to satisfy them.

## Atelier host and Cloudflare — changes outside the P3 repository

Four approved items (2026-10-12) live outside this repository: the root `robots.txt`, a permanent root redirect, plain
HTTP → HTTPS, and `noindex` on Pipeline 2. Where they are controlled (found 2026-10-12):

- **Cloudflare**, zone `xjet3d.com` (name servers `earl` / `rosemary.ns.cloudflare.com`; the account uses single
  sign-on). `xjetatelier.xjet3d.com` is proxied: its address is Cloudflare's, so the origin is reachable only through
  Cloudflare. **All four can be done here**, as rules scoped to this host — the only place that can fix plain HTTP
  (Cloudflare 523: it cannot reach the origin on port 80, so the request never gets to nginx). Needs a Cloudflare login
  that may edit the zone's Rules (account Administrator, or Domain Administrator for `xjet3d.com`).
- **nginx on the Atelier origin server** (Ubuntu, nginx 1.24, Let's Encrypt): the site file
  `/etc/nginx/sites-available/jewelry-b2c` (root-owned; `sudo`, `nginx -t`, `systemctl reload nginx`). It was written
  by Pipeline 2's `nginx-install.sh` and `fix-nginx-hardening.sh` (repository `Xjet3d/XjetJewelryBuilder`, branch
  `Deploy`; Pipeline 2 runs from `/home/dov/git/XjetJewelryBuilder` on port 5001), then edited by hand: the live root
  redirect goes to `/JewelryB2C3/`, the Pipeline 3 location proxies to `127.0.0.1:8340`, and `/JewelryB2C2/` has no
  basic auth — none of which is in Git. Needs SSH to the origin's real address (the `atelier` alias in `~/.ssh/config`
  points at `xjetatelier.xjet3d.com`, which is Cloudflare: port 22 unreachable) and `sudo` on that server. nginx can do
  items 1, 2 and 4, not 3.

**Recommended: four Cloudflare rules** (*Rules* in the `xjet3d.com` zone; each scoped to the host, so no other
`xjet3d.com` site changes). Single Redirects run in order and the first match wins, so the root rule comes first.

| # | Kind | When (expression) | Then |
|---|---|---|---|
| 1 | URL Rewrite (*Transform Rules → Rewrite URL*) | `(http.host eq "xjetatelier.xjet3d.com" and http.request.uri.path eq "/robots.txt")` | Path → static `/JewelryB2C3/robots.txt`; query unchanged |
| 2 | Single Redirect | `(http.host eq "xjetatelier.xjet3d.com" and http.request.uri.path eq "/")` | static `https://xjetatelier.xjet3d.com/JewelryB2C3/`, **301**, preserve query string |
| 3 | Single Redirect (after 2) | `(http.host eq "xjetatelier.xjet3d.com" and not ssl)` | dynamic `concat("https://", http.host, http.request.uri.path)`, **301**, preserve query string |
| 4 | Response Header Transform (*Modify Response Header*) | `(http.host eq "xjetatelier.xjet3d.com" and starts_with(http.request.uri.path, "/JewelryB2C2"))` | set static `X-Robots-Tag` = `noindex` |

Rule 1 makes the visitor's `/robots.txt` P3's own file (it follows the Search engines switch: with it ON, `Disallow:
/JewelryB2C3/api/`, `Allow: /JewelryB2C3/` and the sitemap). If `starts_with()` is not offered, `http.request.uri.path
wildcard "/JewelryB2C2*"` is the same. Pipeline 2 stays available: no redirect, no deletion.

**The nginx alternatives** (items 1, 2 and 4 only), in the `server { listen 443 … }` block of `jewelry-b2c`:

**1. Root `robots.txt`** — one source of truth, P3's own file (it follows the Search engines switch):

```nginx
location = /robots.txt {
    proxy_pass http://127.0.0.1:8340/JewelryB2C3/robots.txt;   # the P3 service on the Atelier host
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
}
```

With Search engines ON it answers `User-agent: *` / `Disallow: /JewelryB2C3/api/` / `Allow: /JewelryB2C3/` /
`Sitemap: https://xjetatelier.xjet3d.com/JewelryB2C3/sitemap.xml`. Nothing else is disallowed (Pipeline 2 included:
its `noindex` must stay readable).

**2. Root redirect permanent** — the root `/` answers nginx's own `302 Found` today; make it `301`:

```nginx
location = / { return 301 /JewelryB2C3/$is_args$args; }      # replaces the current 302 to /JewelryB2C3/
```

`/JewelryB2C3/` answers 200, so there is no loop; a query string is kept. If the current rule is a `rewrite …
redirect`, use `… permanent`.

**3. Plain HTTP → HTTPS** — Cloudflare only (rule 3 above): nginx never sees these requests. (The zone-wide *Always Use
HTTPS* switch does the same for every proxied `xjet3d.com` host — use it only if that is intended for all of them.)

**4. Pipeline 2 not indexable** — every answer under `/JewelryB2C2/` gets `X-Robots-Tag: noindex` (rule 4 above), or in
nginx inside `location /JewelryB2C2/ { … }`:

```nginx
add_header X-Robots-Tag "noindex" always;
```

nginx caveat: a location with its own `add_header` stops inheriting the server-level `add_header` lines — repeat the
server's security headers in that location, or prefer the Cloudflare rule. Pipeline 2 is neither redirected nor deleted.

**Checks after each change** (expected results in brackets):

```
curl -sI https://xjetatelier.xjet3d.com/robots.txt                       [200, text/plain]
curl -s  https://xjetatelier.xjet3d.com/robots.txt                       [the four lines above]
curl -sI https://xjetatelier.xjet3d.com/                                 [301, location: /JewelryB2C3/]
curl -sI http://xjetatelier.xjet3d.com/JewelryB2C3/faq                   [301 to https://…/JewelryB2C3/faq]
curl -sI http://xjetatelier.xjet3d.com/                                  [301 to https://xjetatelier.xjet3d.com/]
curl -sI https://xjetatelier.xjet3d.com/JewelryB2C2/ | grep -i robots    [x-robots-tag: noindex]
curl -sI https://xjetatelier.xjet3d.com/JewelryB2C3/ | grep -i robots    [nothing: P3 stays indexable]
```

## Google Search Console — the Atelier property (set up 2026-10-12)

- **Property:** URL prefix `https://xjetatelier.xjet3d.com/JewelryB2C3/`, owned by XJet's "XJET NPJ" Google
  account (the verified Owner of the URL-prefix property `https://xjet3d.com/`). That account has no
  access to a Domain property `xjet3d.com` (Search Console: "you don't have access to this property"), and
  `https://xjet3d.com/` does not cover the `xjetatelier` host — hence a property of Atelier's own.
- **Verification:** HTML tag. Its `content` is saved in Atelier Admin → Settings → System → *Google Search Console
  verification* (Admin through the SSH tunnel: `docs/AUTH-OPERATIONS-AJW-39.md` §3a), and the home page
  `https://xjetatelier.xjet3d.com/JewelryB2C3/` carries
  `<meta name="google-site-verification" content="…">`. **Keep it** — clearing the field (or a new Atelier database)
  makes the property unverified. A second method can be added in Search Console → Settings → Ownership verification.
- **Sitemap:** `https://xjetatelier.xjet3d.com/JewelryB2C3/sitemap.xml` submitted on 2026-10-12 (status "Couldn't
  fetch" right after submission, while Googlebot's live tests of the pages passed — re-check after a day or two).
- **Request indexing:** done 2026-10-12 for `…/JewelryB2C3/`, `/gallery`, `/faq`, `/materials`, `/technology`, `/about`
  and `/design/serpent-scale`; `/design/aurora-line` hit "Something went wrong — try again in a few hours" twice (it is
  in the sitemap anyway). Re-requesting a URL does not move it up the queue. Since 2026-10-15 the design pages are
  `/gallery/<name>` (the `/design/…` URLs answer 301): inspect one and resubmit the sitemap after the release.
- **Next:** after a few days, **Pages** (indexed / not indexed and why), **Sitemaps** (read, URLs discovered),
  **Enhancements** (FAQ — Google shows FAQ rich results only for a few authoritative sites; Product snippets report
  missing offers by design), **Core Web Vitals** once there is field data.
- `xjet3d.com` carries two `google-site-verification` TXT records (`Yx1ON8…`, `-QFs6h…`) — another account's
  verification (mail is Microsoft 365, so not a Workspace sign-up). If that Domain property is ever found, its owner
  can add the XJET NPJ account; nothing depends on it.

Keep proto's switch OFF; never add proto to Search Console.

## Checking a site

```
curl -s https://xjetatelier.xjet3d.com/JewelryB2C3/robots.txt
curl -s https://xjetatelier.xjet3d.com/JewelryB2C3/sitemap.xml
curl -s https://xjetatelier.xjet3d.com/JewelryB2C3/faq | grep -E '<title>|name="robots"|rel="canonical"|<h1|ld\+json'
curl -sI https://xjetatelier.xjet3d.com/JewelryB2C3/design/serpent-scale       [301, location: /JewelryB2C3/gallery/serpent-scale]
curl -s https://xjetatelier.xjet3d.com/JewelryB2C3/gallery/serpent-scale | grep -E '<title>|rel="canonical"|<h1'
```

## Core Web Vitals

Lab measurements (no field data until Search Console has traffic): the local Chrome, headless, a throw-away profile,
cold cache; phone = 412x823 with Lighthouse's applied mobile throttling (Slow 4G, 4x CPU), desktop = 1366x800 without
throttling; at least 12 s after the load event (a large image can finish late).

A design page (`/design/<name>` until 2026-10-15) opened the homepage with the design in the Inspiration Gallery
lightbox; since then `/gallery/<name>` opens the Gallery under it (to be measured again after the release). Before
2026-10-12 a phone saw the dialog grow by about 350 px when the picture arrived (CLS 0.42) and the picture was the
950 KB original PNG (LCP about 12.7 s). Since then (53d3aa6, 6ff4c97, ae44f06):

- the lightbox picture holds its square from the first paint (`.gallery-shot`, the size it had once loaded);
- it is a thumbnail (`srcset` 320/800/1024 — 45 KB at 800 px, like the gallery cards), fetched with high priority and
  preloaded from the design page's head with the same `srcset` and `sizes` (`p3/sitepages.py` `LightboxPreload`);
- the homepage story's stage keeps its room from the first paint (`sc-stage` in the markup);
- the crawler-only copy of the picture is `loading="lazy"` (hidden, so never fetched by a browser);
- the homepage story waits while the lightbox covers it (as the hero rotation does) and resumes when it closes, unless
  the visitor prefers reduced motion.

| Design page | Phone LCP | Phone CLS | Desktop LCP | Desktop CLS |
|---|---|---|---|---|
| Before (Atelier) | 12.7–13.0 s | 0.42 | 0.66–0.74 s | 0.02 |
| After (proto) | 3.7–3.8 s | 0.04 | about 1.0 s | 0.02 |

The look is unchanged (same sizes and positions). Proto paints about a second later than Atelier (no CDN), so Atelier's
figures are lower.

**The Gallery, and every design page over it (2026-10-15).** Before the scripts ran, the Gallery's grid was empty: the
cards pushed "Start your design" about 750 px down when they arrived, and the All / Rings / Charms filter appeared later
still, once the catalog said which products are on offer. Now the page carries the grid's loading squares — as many as
the visitor's gallery has designs (`p3/sitepages.py` `GalleryPlaceholders`; the app's own loading squares are as many,
`galleryPlaceholders`) — and its config names the products on offer, which the app starts from until the catalog arrives
(`SERVER_PRODUCTS` in `web/app.js`). The finished page is pixel-identical to before.

| Phone, lab (local copies, same data; the mock banner hidden, as on a live site) | LCP | CLS |
|---|---|---|
| `/gallery` before | 5.5 s | 0.87 |
| `/gallery` after | 5.7 s | 0.08 |
| a design page before (`/design/<name>`: the homepage under the dialog) | 6.8 s | 0.04 |
| a design page after (`/gallery/<name>`: the Gallery under the dialog) | 5.3 s | 0.08 |

Medians of three runs; LCP varies about ±0.5 s from run to run. What is left (0.08) is the header's Design-screen steps
before the scripts start (below); the dialog's fade-in keeps its picture out of LCP, which is a card behind it.

## Future recommendations — recorded, not implemented (2026-10-12)

Decided later, each with its own approval:

- Link XJet Atelier prominently from `xjet3d.com` (navigation or footer) and from its jewelry pages — the article
  *XJet's New Economic Logic for Jewelry Innovation*, the T-GOLD 2026 page, the consumer-applications page, which rank on
  Google for "XJet custom / AI / personalized jewelry".
- New SEO articles and guides (custom rings, AI jewelry design, personalized charms, metals and care).
- Rings and Charms collection landing pages.
- A press / backlink campaign (trade press that already covered XJet jewelry; partners such as XOLUTIONS).
- An external marketing and social launch.
- Reviews and review structured data, only from real, attributable reviews.
- Multilingual SEO with `hreflang`, when a target market justifies it.
- A Google Business Profile, only if there is a customer-facing location.

Technical follow-ups, found while measuring (pre-existing, not changed):

- Phones: before the scripts start, the header shows the Design screen's steps (Design · Customize · Bag · Checkout),
  so it is 100 px tall until Alpine hides them (73 px) and the page moves up 27 px. Measured on proto as CLS 0.04 on
  every page; on Atelier it fell outside what the browser counted. Cloaking those steps until the scripts start would
  remove it.
- The homepage story reveals larger pictures as it plays; a lab run with no interaction can then report the homepage's
  LCP late (about 14 s on proto). Real visitors' first tap or scroll ends LCP; worth a look if field data shows it.
