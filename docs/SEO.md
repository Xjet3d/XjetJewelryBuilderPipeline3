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

**Never pages:** the Admin, the API, `/verify` (sign-in links), `/quote` (quote links), `/dev`, `/showcase`, My Account
(`#account`), the Design screens and checkout. They are disallowed in `robots.txt`, carry `X-Robots-Tag: noindex` where
they are documents, and are never in the sitemap.

## Structured data (JSON-LD) — only what is true

- Home: `Organization` (XJet Ltd., xjet3d.com, the XJet Atelier brand; the support address when one is configured) and
  `WebSite` (XJet Atelier).
- `/faq`: `FAQPage` with the ten questions and answers exactly as the page shows them.
- `/design/<name>`: `Product` with the design's name, image, description, category (Rings / Charms) and brand.

No reviews, ratings, prices or availability anywhere. (Google shows product rich results only with offers or reviews;
the Product data is valid without them, it is simply not eligible for that rich result.)

## robots.txt at the host's root (Atelier)

Search engines read `robots.txt` only at a host's root. Atelier's root (`https://xjetatelier.xjet3d.com/robots.txt`) is
served by nginx and answers **404** today — search engines then crawl everything, which is fine for the P3 pages (the
Admin, the API and the links are `noindex` anyway). P3's own file (`/JewelryB2C3/robots.txt`) has the right lines but
is not read there. If a root `robots.txt` is ever added (P2 or nginx — an infrastructure change that needs approval), it
must not disallow `/JewelryB2C3/` and should carry:

```
User-agent: *
Disallow: /JewelryB2C3/admin
Disallow: /JewelryB2C3/api/
Disallow: /JewelryB2C3/verify
Disallow: /JewelryB2C3/quote
Disallow: /JewelryB2C3/dev
Disallow: /JewelryB2C3/showcase
Allow: /JewelryB2C3/
Sitemap: https://xjetatelier.xjet3d.com/JewelryB2C3/sitemap.xml
```

The sitemap is submitted in Search Console directly, so nothing waits for that file.

## Google Search Console — manual steps (someone with the Google account)

1. On Atelier, after this release: Admin → Settings → System → **Search engines** → turn it **ON**. Check that
   `https://xjetatelier.xjet3d.com/JewelryB2C3/faq` shows `<meta name="robots" content="index,follow">` (View source).
2. First check whether your organisation already has a **Domain property for `xjet3d.com`** in Search Console:
   `xjet3d.com` already carries two `google-site-verification` TXT records (seen 2026-10-09), so someone may have
   verified the whole domain — it covers `xjetatelier.xjet3d.com` too; ask its owner to add you, then go to step 4.
   Otherwise, in Search Console (search.google.com/search-console), **Add property → URL prefix**:
   `https://xjetatelier.xjet3d.com/JewelryB2C3/`.
3. Verify ownership with **HTML tag**: copy the `content` of the `google-site-verification` meta tag Google shows (or the
   whole tag), paste it in the same Admin card (*Google Search Console verification*) and Save, then press **Verify** in
   Search Console. (A Domain property for `xjet3d.com` would need a DNS TXT record — an infrastructure change.)
4. **Sitemaps** → submit `https://xjetatelier.xjet3d.com/JewelryB2C3/sitemap.xml`.
5. **URL Inspection** → `https://xjetatelier.xjet3d.com/JewelryB2C3/` → *Test live URL* → *Request indexing*. Repeat for
   `/gallery`, `/faq`, `/materials` and one `/design/<name>` page.
6. After a few days: **Pages** (indexed / not indexed and why), **Sitemaps** (read, URLs discovered),
   **Enhancements** (FAQ, Product snippets — Product will report missing offers by design).

Keep proto's switch OFF; never add proto to Search Console.

## Checking a site

```
curl -s https://xjetatelier.xjet3d.com/JewelryB2C3/robots.txt
curl -s https://xjetatelier.xjet3d.com/JewelryB2C3/sitemap.xml
curl -s https://xjetatelier.xjet3d.com/JewelryB2C3/faq | grep -E '<title>|name="robots"|rel="canonical"|<h1|ld\+json'
```
