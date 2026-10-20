"""Search engines and messaging apps: the home page carries Open Graph / Twitter tags and a canonical link (the share
page keeps its own, for the design); the Admin, the tools and the API are never indexed; design polling pauses while
the tab is hidden."""

import html
import json
import re

from p3 import sitepages as SitePages
from p3.settings import WebDir
from tests.conftest import Harness

AdminKey = "seo-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}


async def test_the_home_page_carries_social_tags_and_a_canonical_link(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey, PublicBaseUrl="https://atelier.example.com")
    try:
        Page = (await H.Client.get("/")).text
        assert '<meta property="og:title" content="XJet Atelier — Custom AI Jewelry, Designed by You">' in Page
        assert '<meta property="og:url" content="https://atelier.example.com/">' in Page
        assert '<link rel="canonical" href="https://atelier.example.com/">' in Page
        assert '<meta name="twitter:card" content="summary_large_image">' in Page
        assert 'property="og:image"' not in Page                                   # no gallery design yet: no preview image
        # A published design becomes the preview image
        B = await H.NewDesign("A slim band")
        await H.Client.put(f"/api/designs/{B['design_id']}/selection", json={"candidate_id": B["candidates"][0]["id"]})
        R = await H.Client.post("/api/admin/gallery", json={"design_id": B["design_id"]}, headers=Admin)
        assert R.status_code == 200, R.text
        Page = (await H.Client.get("/")).text
        assert 'property="og:image" content="https://atelier.example.com/thumb/' in Page and Page.count('property="og:title"') == 1
        # The design's page has its own tags, once
        Slug = (await H.Client.get(f"/api/gallery/{R.json()['id']}/share")).json()["slug"]
        Share = (await H.Client.get(f"/gallery/{Slug}")).text
        assert Share.count('property="og:title"') == 1 and f'content="https://atelier.example.com/gallery/{Slug}"' in Share
        assert "{{OG}}" not in Share and "{{OG}}" not in Page
    finally:
        await H.Close()


async def test_mock_mode_text_reaches_only_a_mock_site(tmp_path):
    """The mock-mode banner and badges are in the page only while the AI is in mock mode: a live site (and a search
    engine reading it) never gets "Mock mode … simulated placeholders"."""
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Page = (await H.Client.get("/")).text
        assert "data-mock-banner" in Page and "simulated placeholders" in Page and "<!--mock-->" not in Page
        H.App.state.Modes.Mode = "live"
        for Path_ in ("/", "/gallery/no-such-design", "/design/no-such-design"):
            Page = (await H.Client.get(Path_)).text
            assert "data-mock-banner" not in Page and "simulated placeholders" not in Page and ">Mock Mode<" not in Page, Path_
            assert "<!--mock-->" not in Page and "<!--/mock-->" not in Page and "x-data=\"p3App()\"" in Page, Path_
    finally:
        H.App.state.Modes.Mode = "mock"
        await H.Close()


def _Live(Page: str, View: str) -> str:
    """The page's own markup as served (its live copy, outside any <template>)."""
    I = Page.index(f'data-ssr-view="{View}"')
    Kept = Page.index(f" && ssrView !== '{View}'\">", I)              # the view's template follows its live copy
    return Page[I:Page.rindex("<template", I, Kept)]


def _JsonLd(Page: str) -> list[dict]:
    return [json.loads(M) for M in re.findall(r'<script type="application/ld\+json">(.*?)</script>', Page, re.S)]


async def _Publish(H, Prompt="A slim band"):
    B = await H.NewDesign(Prompt)
    await H.Client.put(f"/api/designs/{B['design_id']}/selection", json={"candidate_id": B["candidates"][0]["id"]})
    R = await H.Client.post("/api/admin/gallery", json={"design_id": B["design_id"]}, headers=Admin)
    assert R.status_code == 200, R.text
    return (await H.Client.get(f"/api/gallery/{R.json()['id']}/share")).json()


async def test_every_public_page_has_its_own_url_title_description_canonical_and_h1(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey, PublicBaseUrl="https://atelier.example.com")
    try:
        Titles, Descriptions = set(), set()
        for View, Path_ in SitePages.Paths.items():
            R = await H.Client.get("/" + Path_)
            assert R.status_code == 200 and R.headers["cache-control"] == "no-cache", View
            Page = R.text
            Title = re.search(r"<title>(.*?)</title>", Page, re.S).group(1)
            Description = re.search(r'<meta name="description" content="([^"]*)">', Page).group(1)
            Titles.add(Title), Descriptions.add(Description)
            assert "XJet Atelier" in Title and 70 < len(html.unescape(Description)) <= 200, (View, Description)
            assert f'<link rel="canonical" href="https://atelier.example.com/{Path_}">' in Page, View
            assert f'<meta property="og:url" content="https://atelier.example.com/{Path_}">' in Page, View
            assert '<meta name="robots" content="noindex">' in Page, View                  # off by default (proto)
            assert f'"view": "{View}"' in Page and f'"ssr": "{View}"' in Page, View       # the app starts on the page
            Live = _Live(Page, View)
            assert Live.count("<h1") == 1 and "</h1>" in Live, View                       # one H1, in the real markup
            assert f"<template x-if=\"view === '{View}' && ssrView !== '{View}'\">" in Page or View in SitePages.Legal, View
        assert len(Titles) == len(Descriptions) == len(SitePages.Paths)                    # each one its own
        # The page links are real links to those URLs; an old #faq link opens the page at its URL
        Home = (await H.Client.get("/")).text
        for View, Path_ in SitePages.Paths.items():
            if Path_:
                assert f'href="/{Path_}"' in Home and f"pageLink($event, '{View}')" in Home, View
        App = (await H.Client.get("/static/app.js")).text
        assert "if (PAGE_VIEWS.includes(hashView)) this.navigateTo(hashView, { history: 'replace' });" in App
        # every screen at its own address — a page's, My Account's, a step of the journey's — then the tab's title
        Nav = App[App.index("    navigateTo(v, { history = 'push' } = {}) {"):App.index("    pageUrl(v)")]
        assert "this._route(history);" in Nav and Nav.index("this._screenShown();") > Nav.index("this._route(history);")
        assert "_screenShown() { document.title = this.screenTitle(); this.measureScreen(); }," in App
        Route = App[App.index("    routePath() {"):App.index("    _goBackOr(v) {")]
        assert "if (PAGE_VIEWS.includes(v)) return this.pageUrl(v);" in Route and "if (v === 'account') return BASE + '/#account';" in Route
        assert "if (!to || how === 'none' || to === here) return;" in Route                 # the same address adds no step
        assert (await H.Client.get("/faq/")).status_code in (200, 307)                     # a trailing slash still finds it
    finally:
        await H.Close()


async def test_crawlers_read_each_page_without_running_scripts(tmp_path):
    """The page asked for is real markup: the FAQ's answers, the gallery's designs (links to their pages), the metals, the
    legal texts and the computed sentences in this site's words. How It Works is served exactly as written."""
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Share = await _Publish(H)
        Faq = (await H.Client.get("/faq")).text
        Live = _Live(Faq, "faq")
        Items = SitePages.Faq(H.Ctx, ("ring",))
        for Q, A in Items:
            assert html.escape(Q, quote=True) in Live and html.escape(A, quote=True) in Live, Q
        assert "{{FAQ_ITEMS}}" not in Faq and 'x-for="(q, i) in [' not in Faq
        Gallery = _Live((await H.Client.get("/gallery")).text, "inspiration")
        assert f'<a href="/gallery/{Share["slug"]}">{html.escape(Share["title"])}</a>' in Gallery and 'x-init="$el.remove()"' in Gallery
        assert "Bands, signets, statement and stackable rings — real XJet designs." in Gallery
        Materials = _Live((await H.Client.get("/materials")).text, "materials")
        assert ">Real metal, one fixed price per ring</h1>" in Materials and "Silver — available to order" in Materials
        Terms = _Live((await H.Client.get("/terms")).text, "terms")
        assert "The current release supports rings only." in Terms and "Privacy</h1>" not in Terms     # this page's part only
        Tech = _Live((await H.Client.get("/technology")).text, "technology")
        assert "the same printers make every XJet Atelier ring." in Tech
        About = _Live((await H.Client.get("/about")).text, "designers")
        assert ">80+</div>" in About and "x-for=\"f in [" not in About                                # the facts as plain markup
        Home = (await H.Client.get("/")).text
        Source = (WebDir / "index.html").read_text(encoding="utf-8")
        How = Source[Source.index('<section id="how-it-works"'):Source.index("</section>", Source.index('<section id="how-it-works"'))]
        assert Home.count(How) == 2                                                     # live copy and template, unchanged
        assert f'<a href="/gallery/{Share["slug"]}">' in _Live(Home, "home")
        # A part shown by state waits for the script (never a wrong "empty gallery" flash)
        assert 'x-cloak x-show="galleryState === \'loaded\' && !gallery.length"' in _Live(Home, "home")
    finally:
        await H.Close()


async def test_no_page_carries_an_email_address_for_cloudflare_to_rewrite(tmp_path):
    """Atelier's Cloudflare rewrites an email address in a page's markup into a /cdn-cgi/l/email-protection link, which
    answers 404 to every crawler (19 Oct; browsers decode it). The address reaches a page only in its settings (a
    script) and every link is built from it: the privacy sentence too, so it follows the address set in the Admin.
    Cloudflare rewrites addresses in the text and in mailto: links only (a field's placeholder is left as it is)."""
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Share = await _Publish(H)
        Address = re.compile(r"[\w.+-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}", re.I)
        for Path_ in [*SitePages.Paths.values(), f"gallery/{Share['slug']}"]:
            Page = (await H.Client.get("/" + Path_)).text
            Markup = re.sub(r"<(script|style)\b.*?</\1>", "", Page, flags=re.S)
            Text = [T for T in re.findall(r">([^<]+)<", Markup) if Address.search(T)]
            assert not Text and 'href="mailto:' not in Markup, (Path_, Text[:2])
        Privacy = _Live((await H.Client.get("/privacy")).text, "privacy")
        assert ("""write to <a :href="mailto('Privacy question')" class="underline underline-offset-2 hover:text-black" """
                """x-text="SUPPORT_EMAIL || 'us through the Contact page'">us through the Contact page</a>.""") in Privacy
    finally:
        await H.Close()


def test_pictures_of_a_known_shape_carry_their_size():
    """PageSpeed Insights and Screaming Frog flagged pictures without width and height (19 Oct). Every design picture
    (square), the static pictures and the boxes a picture fills carry theirs, so the browser knows their shape before
    they load. Only the pictures shown at their own size, which varies, do not: the zoom, the full Customize picture and
    the enlarged view."""
    Tag = re.compile(r"""<img\b(?:[^>"']|"[^"]*"|'[^']*')*>""")                   # a ">" inside a quoted value is no end
    Imgs = Tag.findall((WebDir / "index.html").read_text(encoding="utf-8"))
    Missing = [I for I in Imgs if not re.search(r"\swidth=", I)]
    assert len(Missing) == 3 and all(any(K in I for K in (':src="zoom?.src"', ':src="cust?.image_url"', ':src="previewSrc"'))
                                     for I in Missing), Missing
    for Name, (W, H) in {"Ink.JPG": (658, 508), "ISO.png": (242, 114), "Angel.JPG": (430, 681),
                         "atelier-loading-poster.jpg": (1152, 720)}.items():
        assert any(Name in I and f'width="{W}" height="{H}"' in I for I in Imgs), Name
    assert all(re.search(r'width="(\d+)" height="\1"', I) for I in Imgs if "thumb(" in I)        # design pictures: square


async def test_links_to_xjet_go_straight_to_its_site(tmp_path):
    """www.xjet3d.com answers 301 to https://xjet3d.com/ (the 19 Oct crawl): the page and its structured data link there
    directly."""
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Share = await _Publish(H)
        for Path_ in ("", f"gallery/{Share['slug']}"):
            Page = (await H.Client.get("/" + Path_)).text
            assert "www.xjet3d.com" not in Page and 'href="https://xjet3d.com/"' in Page, Path_
            Orgs = [D.get("creator") or D for D in _JsonLd(Page) if D.get("@type") in ("Organization", "CreativeWork")]
            assert Orgs and all(O["url"] == "https://xjet3d.com/" for O in Orgs), Path_
    finally:
        await H.Close()


async def test_the_admin_switch_lets_search_engines_index_the_public_pages(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey, PublicBaseUrl="https://atelier.example.com", BasePath="/JewelryB2C3")
    try:
        Share = await _Publish(H)
        assert (await H.Client.get("/robots.txt")).text == "User-agent: *\nDisallow: /JewelryB2C3/\n"
        St = (await H.Client.get("/api/admin/site-indexing", headers=Admin)).json()
        assert St["on"] is False and St["indexable"] is False and St["sitemap_url"] == "https://atelier.example.com/JewelryB2C3/sitemap.xml"
        assert (await H.Client.get("/api/admin/site-indexing")).status_code in (401, 403)
        assert (await H.Client.put("/api/admin/site-indexing", json={"on": "yes"}, headers=Admin)).status_code == 400
        St = (await H.Client.put("/api/admin/site-indexing", json={"on": True}, headers=Admin)).json()
        assert St["on"] and St["indexable"] and St["changed_by"]
        for Path_ in ("/", "/faq", "/gallery", f"/gallery/{Share['slug']}"):
            assert '<meta name="robots" content="index,follow">' in (await H.Client.get(Path_)).text, Path_
        Robots = (await H.Client.get("/robots.txt")).text.splitlines()
        assert Robots[0] == "User-agent: *" and "Allow: /JewelryB2C3/" in Robots
        assert "Disallow: /JewelryB2C3/api/" in Robots                       # only the API is kept out ...
        for P in ("/admin", "/verify", "/quote", "/dev", "/showcase"):            # ... the rest says noindex, readable
            assert not [L for L in Robots if L.startswith("Disallow:") and P in L], P
        assert "Sitemap: https://atelier.example.com/JewelryB2C3/sitemap.xml" in Robots
        Map = (await H.Client.get("/sitemap.xml")).text
        Locs = re.findall(r"<loc>(.*?)</loc>", Map)
        assert Locs[:len(SitePages.Paths)] == [f"https://atelier.example.com/JewelryB2C3/{P}" for P in SitePages.Paths.values()]
        assert f"https://atelier.example.com/JewelryB2C3/gallery/{Share['slug']}" in Locs and len(Locs) == len(SitePages.Paths) + 1
        assert not [L for L in Locs if "/design/" in L]                      # the address before is never listed
        assert not [L for L in Locs if re.search(r"/(admin|api|verify|quote|dev|showcase)\b|#", L)]
        # Never indexed, whatever the switch says: the Admin, the API, sign-in links, quote links, a missing design
        for P in ("/admin/", "/api/catalog", "/verify?token=x", "/quote?token=x"):
            assert "noindex" in (await H.Client.get(P)).headers.get("x-robots-tag", ""), P
        for Path_ in ("/gallery/no-such-design", "/design/no-such-design"):
            Missing = await H.Client.get(Path_)
            assert Missing.status_code == 404 and '<meta name="robots" content="noindex">' in Missing.text, Path_
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM product_settings_log WHERE key = 'site_indexing'")["n"] == 1   # logged
        St = (await H.Client.put("/api/admin/site-indexing", json={"on": False}, headers=Admin)).json()
        assert not St["indexable"] and '<meta name="robots" content="noindex">' in (await H.Client.get("/faq")).text
    finally:
        await H.Close()


async def test_the_search_console_code_is_a_meta_tag_on_the_home_page_only(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Code = "AbCdEf0123456789_-xyzQRS"
        R = await H.Client.put("/api/admin/site-indexing", json={"google_verification": f'<meta name="google-site-verification" content="{Code}" />'},
                               headers=Admin)
        assert R.status_code == 200 and R.json()["google_verification"] == Code and R.json()["on"] is False   # indexing untouched
        assert f'<meta name="google-site-verification" content="{Code}">' in (await H.Client.get("/")).text
        assert "google-site-verification" not in (await H.Client.get("/faq")).text
        assert (await H.Client.put("/api/admin/site-indexing", json={"google_verification": 'x"><script>'}, headers=Admin)).status_code == 400
        assert (await H.Client.put("/api/admin/site-indexing", json={}, headers=Admin)).status_code == 400
        await H.Client.put("/api/admin/site-indexing", json={"google_verification": ""}, headers=Admin)
        assert "google-site-verification" not in (await H.Client.get("/")).text
    finally:
        await H.Close()


async def test_structured_data_says_only_what_is_true(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey, PublicBaseUrl="https://atelier.example.com", SupportEmail="help@example.com")
    try:
        Share = await _Publish(H)
        Home = _JsonLd((await H.Client.get("/")).text)
        assert [D["@type"] for D in Home] == ["Organization", "WebSite"]
        assert Home[0]["name"] == "XJet Ltd." and Home[0]["brand"]["name"] == "XJet Atelier"
        assert Home[0]["contactPoint"]["email"] == "help@example.com" and Home[1]["url"] == "https://atelier.example.com/"
        Faq = (await H.Client.get("/faq")).text
        [Data] = _JsonLd(Faq)
        assert Data["@type"] == "FAQPage" and len(Data["mainEntity"]) == 10
        Live = _Live(Faq, "faq")
        for Q in Data["mainEntity"]:                                    # exactly what the page shows
            assert html.escape(Q["name"], quote=True) in Live and html.escape(Q["acceptedAnswer"]["text"], quote=True) in Live
        Design = (await H.Client.get(f"/gallery/{Share['slug']}")).text
        [Work, Trail] = _JsonLd(Design)                                  # a creative work: no price is shown, so no Product
        assert Work["@type"] == "CreativeWork" and Work["name"] == Share["title"] and Work["genre"] == "Rings"
        assert Work["url"] == f"https://atelier.example.com/gallery/{Share['slug']}" and Work["image"][0].endswith("?w=800&f=jpg")
        assert Work["creator"]["name"] == "XJet Ltd." and '"@type": "Product"' not in Design
        assert Trail["@type"] == "BreadcrumbList" and [(I["position"], I["name"], I["item"]) for I in Trail["itemListElement"]] == [
            (1, "Home", "https://atelier.example.com/"), (2, "Inspiration Gallery", "https://atelier.example.com/gallery"),
            (3, Share["title"], Work["url"])]                            # where the page sits: its own address under the Gallery
        for Page in (json.dumps(Home), json.dumps(Data), json.dumps(Work), json.dumps(Trail)):
            for Invented in ("offers", "price", "availability", "review", "aggregateRating", "ratingValue"):
                assert f'"{Invented}"' not in Page, Invented
        for P in ("/gallery", "/materials", "/terms"):
            assert _JsonLd((await H.Client.get(P)).text) == [], P
    finally:
        await H.Close()


async def test_a_design_page_is_headed_by_the_design_name(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Share = await _Publish(H)
        Page = (await H.Client.get(f"/gallery/{Share['slug']}")).text
        assert '"view": "inspiration"' in Page                                       # the Gallery, with the design open
        Live = _Live(Page, "inspiration")
        Title = html.escape(Share["title"])
        assert f"<h1>{Title}</h1>" in Live and "data-ssr-fallback" in Live          # for readers that run no scripts
        assert Live.count("<h1") == 1                                               # one H1 in the page's own markup
        assert '<h2 class="text-4xl brand-font mb-4">Inspiration Gallery</h2>' in Live     # the Gallery's heading steps down
        assert '<h2 class="hero-title text-5xl' in Page and '<h1 class="hero-title' not in Page   # so does the tagline
        assert '<h1 class="brand-font text-2xl text-zinc-900" x-text="galleryItem.title"></h1>' in Page        # the dialog's name is the H1
        assert f"<title>{Title} · Custom Ring Design · XJet Atelier</title>" in Page and Page.count('rel="canonical"') == 1
        Gallery = (await H.Client.get("/gallery")).text
        assert '<h1 class="text-4xl brand-font mb-4">Inspiration Gallery</h1>' in _Live(Gallery, "inspiration")     # its own H1, named
        Home = (await H.Client.get("/")).text
        assert '<h1 class="hero-title text-5xl' in Home and "<h1>" not in Home        # the home page keeps its own H1
    finally:
        await H.Close()


def test_every_sentence_the_server_writes_is_still_on_the_page():
    """The server writes a page's computed sentences (p3/sitepages.py Fallbacks) into the bindings that show them: each
    binding must still exist in web/index.html (a renamed getter would otherwise leave the server's text unused)."""
    Source = (WebDir / "index.html").read_text(encoding="utf-8") + (WebDir / "partials" / "site-footer.html").read_text(encoding="utf-8")
    class Ctx:                                       # what Fallbacks reads: the catalog's metal names
        class Catalog:
            @staticmethod
            def ToJson():
                return {"groups": [{"id": "fashion", "materials": [{"label": "Silver"}]}, {"id": "luxury", "materials": [{"label": "18K Yellow Gold"}]}]}
    for View in SitePages.Paths:
        for Products_ in (("ring",), ("charm",), ("ring", "charm")):
            for Expr in SitePages.Fallbacks(View, Ctx, Products_, SitePages.HeroCollection(Products_)):
                assert f'x-text="{Expr}"' in Source, (View, Expr)


def test_the_copy_says_jewelry_and_xjet_atelier():
    """American "Jewelry" everywhere a customer reads (pages, scripts, emails, server-rendered pages) and "XJet Atelier"
    in text — the logo artwork is not text."""
    for Name in ("index.html", "app.js", "showcase.html"):
        Text = (WebDir / Name).read_text(encoding="utf-8")
        assert "ewellery" not in Text, Name
        assert "XJET Atelier" not in Text and "XJET ATELIER" not in Text, Name
    for Name in ("app.py", "mail.py", "quotes.py", "registration.py", "orders.py"):
        Text = (WebDir.parent / "p3" / Name).read_text(encoding="utf-8")
        assert "Jewellery" not in Text and "XJET Atelier" not in Text and "XJET ATELIER" not in Text, Name


async def test_the_admin_the_tools_and_the_api_are_never_indexed(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        for P in ("/admin/", "/api/catalog", "/dev", "/static/admin.js"):
            assert (await H.Client.get(P)).headers.get("x-robots-tag") == "noindex, nofollow", P
        assert "x-robots-tag" not in (await H.Client.get("/")).headers
        assert "x-robots-tag" not in (await H.Client.get("/static/app.js")).headers
    finally:
        await H.Close()


def test_polling_pauses_while_the_tab_is_hidden():
    App = (WebDir / "app.js").read_text(encoding="utf-8")
    Tick = App[App.index("    ensurePolling() {"):App.index("    stopPolling()")]
    assert "if (document.hidden) return;" in Tick
    Movie = App[App.index("    pollCustomization() {"):App.index("    async retryMovie()")]          # the 360° movie's poll too
    assert "if (document.hidden) return;" in Movie
    Admin = (WebDir / "admin.js").read_text(encoding="utf-8")
    assert "}, document.hidden ? 10000 : 2000);" in Admin[Admin.index("    poll3d() {"):Admin.index("    now() {")]


async def test_the_materials_page_keeps_its_cards_room_until_the_catalog_answers(tmp_path):
    """/materials CLS (2026-10-17): the metal grids are loops filled when /api/catalog answers; the page as served holds
    one placeholder card per metal before each loop, at the cards' size (index.html .material-ph, .gold-ph), shown until
    the catalog arrives — so nothing below them moves. Only the live copy has them; no other page does."""
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Page = (await H.Client.get("/materials")).text
        Live = _Live(Page, "materials")
        Groups = {G["id"]: len(G["materials"]) for G in H.Ctx.Catalog.ToJson()["groups"]}
        Fashion, Gold = Live.count('<div class="material-ph"></div>'), Live.count('gold-ph" aria-hidden="true" data-ssr-placeholder x-show="!catalog"')
        assert Fashion == Groups["fashion"] >= 1 and Gold == Groups["luxury"] >= 1
        for Group, Loop in SitePages.MaterialLoops.items():
            I = Live.index(Loop)
            assert Live.rindex("data-ssr-placeholder", 0, I) > Live.rindex('<div class="grid', 0, I), Group    # inside the grid, before the loop
        assert Page.count('<div class="material-ph"></div>') == Fashion                        # not in the template the app keeps
        assert 'x-cloak x-show="!catalog"' not in Live                                           # visible from the first paint
        Css = (WebDir / "index.html").read_text(encoding="utf-8")
        for Rule in (".material-ph { height: 445px; }", ".gold-ph { height: 240px; }",               # re-measured 2026-10-20
                     "@media (min-width: 1150px) { .material-ph { height: 464px; } }", "@media (min-width: 1280px) { .gold-ph { height: 232px; } }"):
            assert Rule in Css, Rule
        for Other in ("/", "/gallery", "/faq"):
            assert '<div class="material-ph"></div>' not in (await H.Client.get(Other)).text, Other
    finally:
        await H.Close()


async def test_four_gallery_designs_close_about_materials_technology_and_faq(tmp_path):
    """WP5 (2026-10-17): About, Materials, Technology and FAQ end with four gallery designs, each a link to its own page —
    a different four on each page, from a fixed place in the gallery's order, the same in the page as served and in the
    app; the gallery's pictures say what they are (alt text); the Gallery page's H1 is its name."""
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        for N in range(6):
            await _Publish(H, f"A slim band with motif number {N}")
        Order = [I["slug"] for I in (await H.Client.get("/api/gallery")).json()["items"]]
        assert len(Order) == 6
        for View, Path_ in (("materials", "/materials"), ("technology", "/technology"), ("designers", "/about"), ("faq", "/faq")):
            Live = _Live((await H.Client.get(Path_)).text, View)
            At = SitePages.PageDesignOffsets[View] % 6
            Want = (Order[At:] + Order[:At])[:4]
            Links = re.findall(r'<li><a href="/gallery/([^"]+)">', Live[Live.index(SitePages.PageDesignsAnchor) - 2000:Live.index(SitePages.PageDesignsAnchor)])
            assert Links == Want, (View, Links, Want)
            assert 'x-for="g in pageDesigns"' in Live and "See every design" in Live, View
        Page = (WebDir / "index.html").read_text(encoding="utf-8")
        Js = (WebDir / "app.js").read_text(encoding="utf-8")
        assert Page.count('<template x-for="g in pageDesigns"') == 4
        # room above the four (2026-10-18): FAQ and Technology end on content with no space of its own below it; About
        # too since 2026-10-20 (it ends on a tinted section)
        Strip = '<section x-show="pageDesigns.length" class="px-6 md:px-20 {}pb-16 max-w-6xl mx-auto w-full"'
        assert Page.count(Strip.format("pt-14 ")) == 3 and Page.count(Strip.format("")) == 1
        for View, Path_ in (("faq", "/faq"), ("technology", "/technology")):         # the page as served (x-cloak added)
            Live = _Live((await H.Client.get(Path_)).text, View)
            assert 'class="px-6 md:px-20 pt-14 pb-16 max-w-6xl mx-auto w-full" aria-label="From the Inspiration Gallery"' in Live, View
        Offsets = re.search(r"const PAGE_DESIGN_OFFSETS = \{([^}]*)\};", Js).group(1)
        assert dict((K.strip(), int(V)) for K, V in (X.split(":") for X in Offsets.split(","))) == SitePages.PageDesignOffsets
        for Other in ("/", "/gallery", "/contact"):
            assert SitePages.PageDesignsAnchor not in _Live((await H.Client.get(Other)).text, {"/": "home", "/gallery": "inspiration", "/contact": "contact"}[Other])
        # alt text: what the picture is, everywhere a gallery design is shown, and on its own page
        assert Page.count(':alt="galleryAlt(g, cardMetal[g.id])"') == 3 and ':alt="galleryAlt(galleryItem, galleryMetal)"' in Page
        assert "design from the XJet Atelier Inspiration Gallery`" in Js
        Design = (await H.Client.get(f"/gallery/{Order[0]}")).text
        assert re.search(r'alt="[^"]+ — custom ring design from the XJet Atelier Inspiration Gallery"', Design)
        assert '<h1 class="text-4xl brand-font mb-4">Inspiration Gallery</h1>' in _Live((await H.Client.get("/gallery")).text, "inspiration")
    finally:
        await H.Close()
