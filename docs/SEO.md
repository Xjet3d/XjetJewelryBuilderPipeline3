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
description, canonical link, structured data and its own markup — for a gallery design, its name, image, product and
address (`p3/sitepages.py` `PageFingerprint` / `DesignFingerprint`). A date moves only when that page changes — a
release that leaves the page alone leaves its date alone, and nothing is dated by the request. A new design starts at
its publication date in the gallery. The sitemap is rebuilt at most once a minute.

## Structured data (JSON-LD) — only what is true

- Home: `Organization` (XJet Ltd., xjet3d.com, the XJet Atelier brand; the support address when one is configured) and
  `WebSite` (XJet Atelier).
- `/faq`: `FAQPage` with the ten questions and answers exactly as the page shows them.
- `/design/<name>`: `Product` with the design's name, image, description, category (Rings / Charms) and brand.

No reviews, ratings, prices or availability anywhere. (Google shows product rich results only with offers or reviews;
the Product data is valid without them, it is simply not eligible for that rich result.)

## Atelier host and Cloudflare — changes outside the P3 repository

These four need the Atelier host's nginx (root via SSH on the host — not reachable from the P3 tools) or the Cloudflare
account of `xjet3d.com` (DNS is on Cloudflare: `earl` / `rosemary.ns.cloudflare.com`). Approved 2026-10-12; the owner of
that access applies them. The `server` block is the one that serves `xjetatelier.xjet3d.com` on port 443 (it holds
`location /JewelryB2C2/` and the root redirect); test with `nginx -t` before `systemctl reload nginx`.

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
location = / { return 301 /JewelryB2C3/; }      # replaces the current 302 to /JewelryB2C3/
```

`/JewelryB2C3/` answers 200, so there is no loop. If the current rule is a `rewrite … redirect`, use `… permanent`.

**3. Plain HTTP → HTTPS** — `http://…` answers Cloudflare **523** (Cloudflare cannot reach the origin on port 80).
Safest fix, at Cloudflare and for this host only: *Rules → Redirect Rules → Create rule* — when *Hostname equals
xjetatelier.xjet3d.com* **and** *SSL/HTTPS is off* → *Dynamic redirect* to
`concat("https://", http.host, http.request.uri.path)`, status **301**, *Preserve query string* on. (The zone-wide
*Always Use HTTPS* switch does the same for every proxied `xjet3d.com` host — use it only if that is intended for all
of them.)

**4. Pipeline 2 not indexable** — every answer under `/JewelryB2C2/` gets `X-Robots-Tag: noindex`. Either at Cloudflare
(*Rules → Transform Rules → Modify Response Header*: when *URI Path starts with /JewelryB2C2/* → *Set static*
`X-Robots-Tag` = `noindex, nofollow`), or in nginx inside `location /JewelryB2C2/ { … }`:

```nginx
add_header X-Robots-Tag "noindex, nofollow" always;
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
curl -sI https://xjetatelier.xjet3d.com/JewelryB2C2/ | grep -i robots    [x-robots-tag: noindex, nofollow]
curl -sI https://xjetatelier.xjet3d.com/JewelryB2C3/ | grep -i robots    [nothing: P3 stays indexable]
```

## Google Search Console — manual steps (someone with the Google account)

Search engines are ON on Atelier (2026-10-12). `xjet3d.com` carries two `google-site-verification` TXT records
(`Yx1ON8…`, `-QFs6h…`; `xjetatelier.xjet3d.com` has none): the DNS method of a Search Console **Domain property**, so XJet
very likely already has one — it covers `https://xjetatelier.xjet3d.com/…` without any change on our side. Only the
Google accounts that added those records can confirm it or add you.

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
