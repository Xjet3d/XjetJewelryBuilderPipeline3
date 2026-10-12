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


async def _Map(H) -> dict:
    P3App.SitemapCache.clear()                                    # the sitemap is rebuilt at most once a minute
    R = await H.Client.get("/sitemap.xml")
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
        Design1, Design2 = f"{Base}/design/{S1['slug']}", f"{Base}/design/{S2['slug']}"
        # a design starts at the date it was published here
        Pub = H.Ctx.Db.One("SELECT created_at FROM gallery_items WHERE design_id = ?", (D1,))["created_at"]
        assert Map[Design1] == Pub[:19] + "+00:00"
        # nothing changed: every date stays, request after request (never the time of the request)
        H.Ctx.Db.Execute("UPDATE page_lastmod SET changed_at = ?", (Old,))
        assert set((await _Map(H)).values()) == {"2020-01-01T00:00:00+00:00"}
        assert set((await _Map(H)).values()) == {"2020-01-01T00:00:00+00:00"}
        # one design renamed: its page moves, and the pages that list the designs (home, gallery); the others stay
        R = await H.Client.patch(f"/api/admin/designs/{D1}", json={"title": "Aurora Lattice"}, headers=Admin)
        assert R.status_code == 200, R.text
        Map = await _Map(H)
        Moved = {U for U, V in Map.items() if V != "2020-01-01T00:00:00+00:00"}
        assert Moved == {Design1, f"{Base}/", f"{Base}/gallery"}, Moved
        assert Design2 not in Moved and f"{Base}/faq" not in Moved and f"{Base}/terms" not in Moved
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
        Pages = {S["slug"]: (await H.Client.get(f"/design/{S['slug']}")).text for S in (S1, S2)}
        Descs = {re.search(r'<meta name="description" content="([^"]*)">', P).group(1) for P in Pages.values()}
        assert len(Descs) == 2                                         # one description per design, naming it
        for S in (S1, S2):
            P = Pages[S["slug"]]
            T = html.escape(S["title"])
            assert f"<title>{T} · Custom Ring Design · XJet Atelier</title>" in P
            assert f'content="{T} — a ring designed with XJet Atelier, from the Inspiration Gallery.' in P
            Other = S2 if S is S1 else S1
            assert f'/design/{Other["slug"]}">' in P                   # the crawler block links the other designs
    finally:
        await H.Close()


async def test_the_gallery_description_reads_well():
    from p3 import sitepages as SitePages
    assert SitePages.Words(("ring", "charm"))["stext"] == "ring and charm"
    assert SitePages.Words(("charm",))["stext"] == "charm" and SitePages.Words(())["stext"] == "jewelry"


def test_public_images_carry_descriptive_alt_text():
    Page = (WebDir / "index.html").read_text(encoding="utf-8")
    Ink = re.search(r'<img src="\{\{BASE\}\}/static/images/Ink\.JPG" alt="([^"]*)"', Page).group(1)
    assert Ink == "Bottles of XJet NanoParticle Jetting metal and support materials"
    Js = (WebDir / "showcase.js").read_text(encoding="utf-8")
    assert "img.alt = 'Design option ' + (+el.dataset.k + 1);" in Js and "X.orig.alt = S.title ?" in Js
