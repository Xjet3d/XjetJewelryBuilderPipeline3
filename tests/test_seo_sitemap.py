"""The sitemap's <lastmod> (each page's own content, never the request time), the API docs on a site open to search
engines, the design pages' own titles, descriptions and links, and descriptive alt text."""

import html
import re
import xml.etree.ElementTree as ET

from p3 import app as P3App
from p3.settings import WebDir
from tests.conftest import Harness

AdminKey = "seo-sitemap-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
Old = "2020-01-01T00:00:00.000+00:00"


async def _Publish(H, Prompt):
    B = await H.NewDesign(Prompt)
    await H.Client.put(f"/api/designs/{B['design_id']}/selection", json={"candidate_id": B["candidates"][0]["id"]})
    R = await H.Client.post("/api/admin/gallery", json={"design_id": B["design_id"]}, headers=Admin)
    assert R.status_code == 200, R.text
    return B["design_id"], (await H.Client.get(f"/api/gallery/{R.json()['id']}/share")).json()


async def _Map(H, Headers: dict | None = None) -> dict:
    P3App.SitemapCache.clear()                                    # the sitemap is rebuilt at most once a minute
    R = await H.Client.get("/sitemap.xml", headers=Headers)
    assert R.status_code == 200 and R.headers["content-type"].startswith("application/xml")
    Root_ = ET.fromstring(R.text)                                 # valid XML, the sitemap namespace
    return {U.find("s:loc", Ns).text: U.find("s:lastmod", Ns).text for U in Root_.findall("s:url", Ns)}


async def test_lastmod_is_the_date_each_pages_own_content_last_changed(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey, PublicBaseUrl="https://atelier.example.com", BasePath="/JewelryB2C3")
    try:
        D1, S1 = await _Publish(H, "A slim band")
        _D2, S2 = await _Publish(H, "A twisted band")
        await H.Client.put("/api/admin/site-indexing", json={"on": True}, headers=Admin)
        Map = await _Map(H)
        assert len(Map) == 12 and all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", V) for V in Map.values())
        Base = "https://atelier.example.com/JewelryB2C3"
        Design1, Design2 = f"{Base}/gallery/{S1['slug']}", f"{Base}/gallery/{S2['slug']}"
        # a design starts at the date it was published here
        Pub = H.Ctx.Db.One("SELECT created_at FROM gallery_items WHERE design_id = ?", (D1,))["created_at"]
        assert Map[Design1] == Pub[:19] + "+00:00"
        # nothing changed: every date stays, request after request (never the time of the request)
        H.Ctx.Db.Execute("UPDATE page_lastmod SET changed_at = ?", (Old,))
        assert set((await _Map(H)).values()) == {"2020-01-01T00:00:00+00:00"}
        assert set((await _Map(H)).values()) == {"2020-01-01T00:00:00+00:00"}
        # one design renamed: its page moves, and the pages that list the designs (home, gallery, and About, Materials,
        # Technology and FAQ, which end with four of them); the others stay
        R = await H.Client.patch(f"/api/admin/designs/{D1}", json={"title": "Aurora Lattice"}, headers=Admin)
        assert R.status_code == 200, R.text
        Map = await _Map(H)
        Moved = {U for U, V in Map.items() if V != "2020-01-01T00:00:00+00:00"}
        assert Moved == {Design1, f"{Base}/", f"{Base}/gallery", f"{Base}/about", f"{Base}/materials", f"{Base}/technology",
                         f"{Base}/faq"}, Moved
        assert Design2 not in Moved and f"{Base}/terms" not in Moved and f"{Base}/contact" not in Moved
    finally:
        await H.Close()


def test_a_date_follows_what_a_reader_gets_not_the_markup():
    from p3 import sitepages as SitePages
    Page = ('<title>T</title><div class="ssr-view" data-ssr-view="faq"><p class="a">Hello <b>world</b></p>'
            '<a href="/faq#x">More</a><img src="/i.png" alt="A ring" :alt="g.title"></div>')
    Print = SitePages.PageFingerprint(Page, "faq")
    assert Print.startswith(SitePages.FingerprintVersion + ":")
    Restyled = Page.replace('class="a"', 'class="b text-sm"').replace("<b>world</b>", '<strong class="x">world</strong>')
    assert SitePages.PageFingerprint(Restyled.replace(':alt="g.title"', ':alt="g.name"'), "faq") == Print   # styling only
    for Changed in (Page.replace("Hello", "Hi"), Page.replace("/faq#x", "/gallery"), Page.replace("A ring", "A charm"),
                    Page.replace("/i.png", "/j.png"), Page.replace("<title>T</title>", "<title>U</title>")):
        assert SitePages.PageFingerprint(Changed, "faq") != Print, Changed


async def test_a_fingerprint_made_another_way_is_renewed_without_moving_the_date(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _Publish(H, "A slim band")
        await _Map(H)
        H.Ctx.Db.Execute("UPDATE page_lastmod SET changed_at = ?, fingerprint = 'made-the-old-way'", (Old,))
        Stale = {"2020-01-01T00:00:00+00:00"}
        assert set((await _Map(H)).values()) == Stale                       # the pages did not change: no date moves
        Rows = H.Ctx.Db.All("SELECT fingerprint FROM page_lastmod")
        assert Rows and all(R["fingerprint"].startswith("v2:") for R in Rows)  # the records are renewed
        assert set((await _Map(H)).values()) == Stale
    finally:
        await H.Close()


async def test_a_date_does_not_move_with_the_address_the_pages_are_read_at(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)                      # no public base URL: a page names the address it is read at
    try:
        await _Publish(H, "A slim band")
        await _Map(H)
        H.Ctx.Db.Execute("UPDATE page_lastmod SET changed_at = ?", (Old,))
        Stale = {"2020-01-01T00:00:00+00:00"}
        Other = await _Map(H, {"host": "proto.example"})          # the same pages at another address (http://proto, https://tron)
        assert set(Other.values()) == Stale and all(U.startswith("http://proto.example/") for U in Other)
        assert set((await _Map(H)).values()) == Stale
    finally:
        await H.Close()


async def test_the_api_docs_are_hidden_where_search_engines_are_invited_and_never_indexed(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        for P in ("/docs", "/redoc", "/openapi.json"):                 # proto / development: there, but never indexed
            R = await H.Client.get(P)
            assert R.status_code == 200 and R.headers.get("x-robots-tag") == "noindex, nofollow", P
        await H.Client.put("/api/admin/site-indexing", json={"on": True}, headers=Admin)
        for P in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):   # a site open to search engines: gone
            R = await H.Client.get(P)
            assert R.status_code == 404 and R.headers.get("x-robots-tag") == "noindex, nofollow", P
        assert (await H.Client.get("/faq")).status_code == 200 and (await H.Client.get("/api/catalog")).status_code == 200
    finally:
        await H.Close()


async def test_design_pages_have_their_own_title_description_and_links(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        _D1, S1 = await _Publish(H, "A slim band")
        _D2, S2 = await _Publish(H, "A twisted band")
        Pages = {S["slug"]: (await H.Client.get(f"/gallery/{S['slug']}")).text for S in (S1, S2)}
        Descs = {re.search(r'<meta name="description" content="([^"]*)">', P).group(1) for P in Pages.values()}
        assert len(Descs) == 2                                         # one description per design, naming it
        for S in (S1, S2):
            P = Pages[S["slug"]]
            T = html.escape(S["title"])
            assert f"<title>{T} · Custom Ring Design · XJet Atelier</title>" in P
            assert f'content="{T} — a ring designed with XJet Atelier, from the Inspiration Gallery.' in P
            Other = S2 if S is S1 else S1
            assert f'<a href="/gallery/{Other["slug"]}">' in P         # the crawler block links the other designs ...
            assert f'<a href="/gallery/{S["slug"]}">' not in P            # ... never the design itself
    finally:
        await H.Close()


async def test_the_gallery_description_reads_well():
    from p3 import sitepages as SitePages
    assert SitePages.Words(("ring", "charm"))["stext"] == "ring and charm"
    assert SitePages.Words(("charm",))["stext"] == "charm" and SitePages.Words(())["stext"] == "jewelry"


def test_the_lightbox_picture_and_the_story_keep_their_room_from_the_first_paint():
    """Design pages on a phone (CLS): nothing below moves when the story starts or the design's picture arrives."""
    from p3 import sitepages as SitePages
    Page = (WebDir / "index.html").read_text(encoding="utf-8")
    Img = re.search(r'<img :srcset="srcsetFor\(galleryItem\.image_url\)"[^>]*>', Page).group(0)
    assert f'sizes="{SitePages.LightboxSizes}"' in Img and 'width="1024" height="1024"' in Img and 'fetchpriority="high"' in Img
    assert 'class="gallery-shot ' in Img and ":src=\"thumb(galleryItem.image_url, 800)\"" in Img   # a thumbnail, not the original
    assert ".gallery-shot { width: 100%; max-width: 42vh; height: auto; aspect-ratio: 1 / 1; }" in Page
    assert "@media (min-width: 768px) { .gallery-shot { max-width: 72vh; } }" in Page
    assert re.search(r'<div class="sc-stage" role="img"[^>]*\n\s*data-sc-design="[^"]+" x-init="\$nextTick\(\(\) => window\.P3Showcase', Page)


def test_the_story_waits_while_the_lightbox_covers_it():
    """Design pages on a phone (LCP): the homepage story under the dialog does not play on to its larger pictures."""
    Js = (WebDir / "app.js").read_text(encoding="utf-8")
    Open = Js[Js.index("    openGallery(g, { address = 'push' } = {}) {"):Js.index("    metalLabel(id)")]
    Close = Js[Js.index("    closeGallery(how = '') {"):Js.index("    _pendingGallery()")]
    assert "this._pauseStory();" in Open and "this._resumeStory();" in Close
    assert "if (sc && sc.state().playing) { sc.pause(); PAUSED_STORY = sc; }" in Js             # only a playing story
    assert "if (sc && sc === this._story()) sc.play();" in Js                                   # only the one it paused
    assert "\nlet PAUSED_STORY = null;" in Js            # outside Alpine: a reactive proxy would break that identity check
    Engine = (WebDir / "showcase.js").read_text(encoding="utf-8")
    assert "play() { playing = true; last = null; }," in Engine and "pause() { playing = false; if (D) render(T); }," in Engine


async def test_a_design_page_preloads_its_lightbox_picture_and_its_crawler_picture_waits(tmp_path):
    from p3 import sitepages as SitePages
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        _D, S1 = await _Publish(H, "A slim band")
        Page = (await H.Client.get(f"/gallery/{S1['slug']}")).text
        Item = next(T for T in (await H.Client.get("/api/gallery")).json()["items"] if T["title"] == S1["title"])
        Thumb = Item["image_url"].replace("/assets/", "/thumb/", 1)              # app.js thumb() / srcsetFor()
        Set = f"{Thumb}?w=320 320w, {Thumb}?w=800 800w, {Thumb}?w=1024 1024w"
        assert (f'<link rel="preload" as="image" href="{Thumb}?w=800" imagesrcset="{Set}" '
                f'imagesizes="{SitePages.LightboxSizes}" fetchpriority="high">') in Page
        Crawler = re.search(r"<div data-ssr-fallback[^>]*>.*?</div>", Page, re.S).group(0)
        assert re.search(r'<img src="[^"]+" alt="[^"]+" loading="lazy">', Crawler)        # hidden: never fetched by a browser
        assert "loading=\"lazy\"" not in Page.split("<body", 1)[0]                           # the preload itself is not lazy
    finally:
        await H.Close()


def test_public_images_carry_descriptive_alt_text():
    Page = (WebDir / "index.html").read_text(encoding="utf-8")
    Ink = re.search(r'<img src="\{\{BASE\}\}/static/images/Ink\.JPG" alt="([^"]*)"', Page).group(1)
    assert Ink == "Bottles of XJet NanoParticle Jetting metal and support materials"
    Js = (WebDir / "showcase.js").read_text(encoding="utf-8")
    assert "img.alt = 'Design option ' + (+el.dataset.k + 1);" in Js and "X.orig.alt = S.title ?" in Js
