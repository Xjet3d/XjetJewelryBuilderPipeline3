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
        # The share page has the design's own tags, once
        Slug = (await H.Client.get(f"/api/gallery/{R.json()['id']}/share")).json()["slug"]
        Share = (await H.Client.get(f"/design/{Slug}")).text
        assert Share.count('property="og:title"') == 1 and f'content="https://atelier.example.com/design/{Slug}"' in Share
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
        for Path_ in ("/", "/design/no-such-design"):
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
        assert "if (PAGE_VIEWS.includes(hashView)) this.navigateTo(hashView);" in App
        assert "history.replaceState(null, '', this.pageUrl(v));" in App and "P3_PAGE === 'home'" in App
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
        assert f'<a href="/design/{Share["slug"]}">{html.escape(Share["title"])}</a>' in Gallery and 'x-init="$el.remove()"' in Gallery
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
        assert f'<a href="/design/{Share["slug"]}">' in _Live(Home, "home")
        # A part shown by state waits for the script (never a wrong "empty gallery" flash)
        assert 'x-cloak x-show="galleryState === \'loaded\' && !gallery.length"' in _Live(Home, "home")
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
        for Path_ in ("/", "/faq", "/gallery", f"/design/{Share['slug']}"):
            assert '<meta name="robots" content="index,follow">' in (await H.Client.get(Path_)).text, Path_
        Robots = (await H.Client.get("/robots.txt")).text.splitlines()
        assert Robots[0] == "User-agent: *" and "Allow: /JewelryB2C3/" in Robots
        for P in ("/admin", "/api/", "/verify", "/quote", "/dev", "/showcase"):
            assert f"Disallow: /JewelryB2C3{P}" in Robots, P
        assert "Sitemap: https://atelier.example.com/JewelryB2C3/sitemap.xml" in Robots
        Map = (await H.Client.get("/sitemap.xml")).text
        Locs = re.findall(r"<loc>(.*?)</loc>", Map)
        assert Locs[:len(SitePages.Paths)] == [f"https://atelier.example.com/JewelryB2C3/{P}" for P in SitePages.Paths.values()]
        assert f"https://atelier.example.com/JewelryB2C3/design/{Share['slug']}" in Locs and len(Locs) == len(SitePages.Paths) + 1
        assert not [L for L in Locs if re.search(r"/(admin|api|verify|quote|dev|showcase)\b|#", L)]
        # Never indexed, whatever the switch says: the Admin, the API, sign-in links, quote links, a missing design
        for P in ("/admin/", "/api/catalog", "/verify?token=x", "/quote?token=x"):
            assert "noindex" in (await H.Client.get(P)).headers.get("x-robots-tag", ""), P
        Missing = await H.Client.get("/design/no-such-design")
        assert Missing.status_code == 404 and '<meta name="robots" content="noindex">' in Missing.text
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
        Design = (await H.Client.get(f"/design/{Share['slug']}")).text
        [Product] = _JsonLd(Design)
        assert Product["@type"] == "Product" and Product["name"] == Share["title"] and Product["category"] == "Rings"
        assert Product["url"] == f"https://atelier.example.com/design/{Share['slug']}" and Product["image"][0].endswith("?w=800&f=jpg")
        for Page in (json.dumps(Home), json.dumps(Data), json.dumps(Product)):
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
        Page = (await H.Client.get(f"/design/{Share['slug']}")).text
        Live = _Live(Page, "home")
        Title = html.escape(Share["title"])
        assert f"<h1>{Title}</h1>" in Live and "data-ssr-fallback" in Live          # for readers that run no scripts
        assert '<h2 class="hero-title text-5xl' in Page and '<h1 class="hero-title' not in Page   # the tagline steps down
        assert '<h1 class="brand-font text-2xl text-zinc-900" x-text="galleryItem.title"></h1>' in Page        # the dialog's name is the H1
        assert f"<title>{Title} · XJet Atelier</title>" in Page and Page.count('rel="canonical"') == 1
        Home = (await H.Client.get("/")).text
        assert '<h1 class="hero-title text-5xl' in Home and "<h1>" not in Home        # the home page keeps its own H1
    finally:
        await H.Close()


def test_every_sentence_the_server_writes_is_still_on_the_page():
    """The server writes a page's computed sentences (p3/sitepages.py Fallbacks) into the bindings that show them: each
    binding must still exist in web/index.html (a renamed getter would otherwise leave the server's text unused)."""
    Source = (WebDir / "index.html").read_text(encoding="utf-8")
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
