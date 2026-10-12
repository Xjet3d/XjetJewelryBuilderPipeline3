# Search engines (SEO)

**Code:** `p3/sitepages.py` (pages, titles, descriptions, the FAQ, structured data, the server copy of a page),
`p3/app.py` (`RenderPage`, the page routes, `robots.txt`, `sitemap.xml`), `p3/admin.py` (`/api/admin/site-indexing`),
`web/app.js` (`PAGE_PATHS`, `pageLink`, `navigateTo`). **Tests:** `tests/test_seo.py`.

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
| a gallery design | `/design/<name>` | the design's name |

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
docs, My Account (`#account`), the Design screens and checkout. They carry `X-Robots-Tag: noindex` (and a `noindex`
meta tag where they are documents) and are never in the sitemap. Only the API is disallowed in `robots.txt`: a crawler
can read a page's `noindex` only if it may fetch the page, and a disallowed URL can still be listed bare from links.

**The API docs** (`/docs`, `/redoc`, `/openapi.json`) are a developer aid, not part of the public site: wherever the site
is open to search engines (production, or Search engines ON) they answer 404; on proto and development copies they stay,
always `noindex`.

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
- `/design/<name>`: `Product` with the design's name, image, description, category (Rings / Charms) and brand.

No reviews, ratings, prices or availability anywhere. (Google shows product rich results only with offers or reviews;
the Product data is valid without them, it is simply not eligible for that rich result.)

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

## Google Search Console — manual steps (someone with the Google account)

Search engines are ON on Atelier (2026-10-12). `xjet3d.com` carries two `google-site-verification` TXT records
(`Yx1ON8…`, `-QFs6h…`; `xjetatelier.xjet3d.com` has none): the DNS method of a Search Console **Domain property**, so XJet
very likely already has one — it covers `https://xjetatelier.xjet3d.com/…` without any change on our side. (The
domain's mail is Microsoft 365 — MX `xjet3d-com.mail.protection.outlook.com` — so the records are not a Google
Workspace sign-up.) Only the Google accounts that added those records can confirm it or add you.

1. Open [Search Console](https://search.google.com/search-console) with your XJet Google account and look for a property
   named `xjet3d.com` (Domain). Not there: ask marketing / IT who owns the `xjet3d.com` Domain property to add you as an
   **Owner** or **Full user** (*Settings → Users and permissions*). With access, go to step 3.
2. Only if no Domain property exists (or nobody can add you): *Add property → URL prefix* →
   `https://xjetatelier.xjet3d.com/JewelryB2C3/` → method **HTML tag** → paste the tag (or its `content`) into Atelier
   Admin → Settings → System → *Google Search Console verification* → Save → **Verify**. (A new Domain property would
   need a DNS TXT record in Cloudflare instead.) The Admin field is needed only for this route.
3. **Sitemaps** (in the property) → submit `https://xjetatelier.xjet3d.com/JewelryB2C3/sitemap.xml`.
4. **URL Inspection** → each of these → *Test live URL* → *Request indexing*:
   `https://xjetatelier.xjet3d.com/JewelryB2C3/`, `…/gallery`, `…/faq`, `…/materials`, `…/technology`, `…/about`
   and two designs from the sitemap (e.g. `…/design/serpent-scale`).
5. After a few days: **Pages** (indexed / not indexed and why), **Sitemaps** (read, URLs discovered), **Enhancements**
   (FAQ — Google shows FAQ rich results only for a few authoritative sites; Product snippets report missing offers by
   design), **Core Web Vitals** once there is field data.

Keep proto's switch OFF; never add proto to Search Console.

## Checking a site

```
curl -s https://xjetatelier.xjet3d.com/JewelryB2C3/robots.txt
curl -s https://xjetatelier.xjet3d.com/JewelryB2C3/sitemap.xml
curl -s https://xjetatelier.xjet3d.com/JewelryB2C3/faq | grep -E '<title>|name="robots"|rel="canonical"|<h1|ld\+json'
```

## Core Web Vitals

Lab measurements (no field data until Search Console has traffic): the local Chrome, headless, a throw-away profile,
cold cache; phone = 412x823 with Lighthouse's applied mobile throttling (Slow 4G, 4x CPU), desktop = 1366x800 without
throttling; at least 12 s after the load event (a large image can finish late).

A design page (`/design/<name>`) opens the homepage with the design in the Inspiration Gallery lightbox. Before
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
