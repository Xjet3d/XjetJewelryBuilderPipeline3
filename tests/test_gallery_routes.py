"""Gallery design addresses (2026-10-15): each public gallery design has its own page, /gallery/<slug> — the Gallery with
that design open, with its own title, description, canonical address and structured data — and its cards link there.
The address before, /design/<slug>, moves there in one permanent hop, and only for a design in the public gallery: no
blanket redirect, no chain, and nothing outside the public gallery becomes reachable by name."""

import re

from p3 import app as P3App
from p3 import sitepages as SitePages
from p3.gallery import ShareSlug
from p3.settings import WebDir
from tests.conftest import Harness

AdminKey = "gallery-routes-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Origin = "https://atelier.example.com"
Anon = {"X-Access-Token": "NOPE00"}


async def _Publish(H, Prompt, **Form):
    B = await H.NewDesign(Prompt, **Form)
    await H.Client.put(f"/api/designs/{B['design_id']}/selection", json={"candidate_id": B["candidates"][0]["id"]})
    R = await H.Client.post("/api/admin/gallery", json={"design_id": B["design_id"]}, headers=Admin)
    assert R.status_code == 200, R.text
    return B["design_id"], R.json(), (await H.Client.get(f"/api/gallery/{R.json()['id']}/share")).json()


async def _Set(H, Product, On):
    R = await H.Client.put("/api/admin/products/availability", json={"product": Product, "available": On}, headers=Admin)
    assert R.status_code == 200, R.text


async def test_a_gallery_design_has_its_own_page_in_the_gallery(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey, PublicBaseUrl=Origin, BasePath="/JewelryB2C3")
    try:
        _D1, Item1, S1 = await _Publish(H, "A slim band")
        _D2, _I2, S2 = await _Publish(H, "A twisted band")
        await H.Client.put("/api/admin/site-indexing", json={"on": True}, headers=Admin)
        Url = f"{Origin}/JewelryB2C3/gallery/{S1['slug']}"
        assert S1["url"] == Url                                                   # Share / Copy link
        Tiles = (await H.Client.get("/api/gallery", headers=Anon)).json()["items"]
        assert {T["slug"] for T in Tiles} == {S1["slug"], S2["slug"]}             # each card links to its page
        R = await H.Client.get(f"/gallery/{S1['slug']}", headers=Anon)             # no sign-in needed
        assert R.status_code == 200 and R.headers["cache-control"] == "no-cache"
        Page = R.text
        assert Page.count('<link rel="canonical"') == 1 and f'<link rel="canonical" href="{Url}">' in Page
        assert f'<meta property="og:url" content="{Url}">' in Page and Page.count('property="og:title"') == 1
        assert '<meta name="robots" content="index,follow">' in Page
        assert '"view": "inspiration"' in Page                                    # the app starts on the Gallery ...
        Open = re.search(r"window\.__p3Open = (\{.*?\});", Page).group(1)
        assert f'"gallery": "{Item1["id"]}"' in Open and f'"slug": "{S1["slug"]}"' in Open    # ... with the design open
        assert f'<a href="/JewelryB2C3/gallery/{S2["slug"]}">' in Page            # the other designs, as plain links
        assert f'<a href="/JewelryB2C3/gallery/{S1["slug"]}">' not in Page        # never a link to itself
        assert '<a href="/JewelryB2C3/gallery">See every design in the Inspiration Gallery</a>' in Page
        Titles = {re.search(r"<title>(.*?)</title>", (await H.Client.get(P)).text).group(1)
                  for P in (f"/gallery/{S1['slug']}", f"/gallery/{S2['slug']}", "/gallery")}
        assert len(Titles) == 3                                                   # each page its own title
        Map = (P3App.SitemapCache.clear(), (await H.Client.get("/sitemap.xml")).text)[1]
        Locs = re.findall(r"<loc>(.*?)</loc>", Map)
        assert Url in Locs and f"{Origin}/JewelryB2C3/gallery/{S2['slug']}" in Locs and not [L for L in Locs if "/design/" in L]
    finally:
        await H.Close()


async def test_the_address_before_moves_to_the_design_page_in_one_hop(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey, PublicBaseUrl=Origin, BasePath="/JewelryB2C3")
    try:
        D1, _Item, S1 = await _Publish(H, "A slim band")
        Page = f"/JewelryB2C3/gallery/{S1['slug']}"
        R = await H.Client.get(f"/design/{S1['slug']}", headers=Anon)
        assert R.status_code == 301 and R.headers["location"] == Page              # permanent, to the canonical address
        assert R.headers["cache-control"] == "no-cache"                            # browsers ask again (a rollback stays possible)
        R = await H.Client.get(R.headers["location"].removeprefix("/JewelryB2C3"), headers=Anon)
        assert R.status_code == 200                                                # one hop: the page itself, no chain
        R = await H.Client.get(f"/design/{S1['slug']}?utm_source=whatsapp&utm_medium=social")
        assert R.status_code == 301 and R.headers["location"] == Page + "?utm_source=whatsapp&utm_medium=social"   # tags kept
        # another spelling, or the name the design has now (its link name never changes): still one hop to the canonical
        await H.Client.patch(f"/api/admin/designs/{D1}", json={"title": "Aurora Lattice"}, headers=Admin)
        for Old in (f"/design/{S1['slug'].upper()}", "/design/aurora-lattice", "/gallery/aurora-lattice", f"/gallery/{S1['slug'].upper()}"):
            R = await H.Client.get(Old)
            assert R.status_code == 301 and R.headers["location"] == Page, Old
        # nothing else under /design/ or /gallery/ goes anywhere: an unknown name is a page not found, never indexed
        await H.Client.put("/api/admin/site-indexing", json={"on": True}, headers=Admin)
        for Missing in ("/design/no-such-design", "/gallery/no-such-design"):
            R = await H.Client.get(Missing)
            assert R.status_code == 404 and "location" not in R.headers, Missing
            assert '<meta name="robots" content="noindex">' in R.text and 'window.__p3Open = {"gallery": null};' in R.text, Missing
        assert (await H.Client.get("/gallery")).status_code == 200                 # the Gallery itself is unchanged
    finally:
        await H.Close()


async def test_a_design_outside_the_public_gallery_is_never_reachable_by_name(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Mine = await H.NewDesign("An engraved signet with a family crest")        # a customer's own design, never published
        Title = (await H.Design(Mine["design_id"]))["title"]
        Slug = ShareSlug(Title)
        _D, Item, S = await _Publish(H, "A slim band")
        assert Slug and Slug != S["slug"]
        for Path_ in (f"/gallery/{Slug}", f"/design/{Slug}"):
            R = await H.Client.get(Path_, headers=Anon)
            assert R.status_code == 404 and "location" not in R.headers and Mine["design_id"] not in R.text, Path_
        # taken out of the gallery: its page is not found and the address before no longer moves anywhere
        assert (await H.Client.delete(f"/api/admin/gallery/{Item['id']}", headers=Admin)).status_code == 200
        for Path_ in (f"/gallery/{S['slug']}", f"/design/{S['slug']}"):
            R = await H.Client.get(Path_, headers=Anon)
            assert R.status_code == 404 and '<meta name="robots" content="noindex">' in R.text, Path_
        P3App.SitemapCache.clear()
        assert S["slug"] not in (await H.Client.get("/sitemap.xml")).text
    finally:
        await H.Close()


async def test_each_product_mode_shows_the_pages_of_the_products_on_offer_only(tmp_path):
    """Both products, rings only, charms only, neither: a design's page, its hop from the address before and its
    sitemap entry exist exactly while its product is on offer (what happens to a page whose product is switched off
    for a while is the owner's decision — docs/SEO.md)."""
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _Set(H, "charm", True)
        _R, _RI, Ring = await _Publish(H, "A slim band", product="ring")
        _C, _CI, Charm = await _Publish(H, "A heart charm", product="charm")
        for Rings, Charms in ((True, True), (True, False), (False, True), (False, False)):
            await _Set(H, "ring", Rings)
            await _Set(H, "charm", Charms)
            P3App.SitemapCache.clear()
            Map = (await H.Client.get("/sitemap.xml")).text
            for S, On in ((Ring, Rings), (Charm, Charms)):
                Mode = (Rings, Charms, S["slug"])
                assert (await H.Client.get(f"/gallery/{S['slug']}")).status_code == (200 if On else 404), Mode
                assert (await H.Client.get(f"/design/{S['slug']}")).status_code == (301 if On else 404), Mode
                assert (f"/gallery/{S['slug']}</loc>" in Map) is On, Mode
            assert (await H.Client.get("/gallery")).status_code == 200
    finally:
        await H.Close()


def _LiveGallery(Page: str) -> str:
    """The Gallery's own markup as served (its live copy, outside any <template>)."""
    I = Page.index('data-ssr-view="inspiration"')
    return Page[I:Page.rindex("<template", I, Page.index(" && ssrView !== 'inspiration'\">", I))]


async def test_the_gallery_keeps_its_room_from_the_first_paint(tmp_path):
    """Phones (CLS): the Gallery — and a design's page over it — carries its grid's loading squares, as many as the
    visitor's designs, and the products on offer; the app starts from both, so neither the cards nor the filter move
    the page when they arrive."""
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _Set(H, "charm", True)
        await _Publish(H, "A slim band", product="ring")
        _C, _CI, Charm = await _Publish(H, "A heart charm", product="charm")
        for Path_ in ("/gallery", f"/gallery/{Charm['slug']}"):
            Page = (await H.Client.get(Path_)).text
            assert _LiveGallery(Page).count(SitePages.GallerySquare) == 2, Path_            # one per design
            assert '"products": ["ring", "charm"]' in Page and '"gallery_count": 2' in Page, Path_
        await _Set(H, "ring", False)
        Page = (await H.Client.get("/gallery")).text
        assert _LiveGallery(Page).count(SitePages.GallerySquare) == 1 and '"products": ["charm"]' in Page and '"gallery_count": 1' in Page
        Home = (await H.Client.get("/")).text
        assert "data-ssr-placeholder" not in Home and '"gallery_count"' not in Home and '"products": ["charm"]' in Home
    finally:
        await H.Close()
    # the page's squares are the app's own loading squares (the same look), and the app counts them the same way
    Source = (WebDir / "index.html").read_text(encoding="utf-8")
    Square = SitePages.GallerySquare.replace(' data-ssr-placeholder x-init="$el.remove()"', "")
    assert f'{SitePages.GalleryLoading}:key="i">{Square}</template></template>' in Source
    Js = (WebDir / "app.js").read_text(encoding="utf-8")
    assert "get galleryPlaceholders() { const n = window.__p3?.gallery_count; return Number.isInteger(n) && n >= 0 ? n : 8; }," in Js
    assert "get productsList() { return this.catalog?.products?.available || SERVER_PRODUCTS || ['ring']; }," in Js


def test_the_cards_are_links_and_the_dialog_keeps_the_design_address():
    """The client side (web/app.js, web/index.html); its behaviour in a browser is in docs/SEO.md's QA list."""
    Page = (WebDir / "index.html").read_text(encoding="utf-8")
    Js = (WebDir / "app.js").read_text(encoding="utf-8")
    # home, the Gallery, "See all designs": every card is a real link to the design's page — a plain click opens the dialog
    assert Page.count('<a :href="galleryHref(g)" @click="galleryClick($event, g)" data-gallery-tile') == 3
    assert '<button type="button" @click="openGallery(g)"' not in Page
    assert "galleryHref(g) { return g && g.slug ? BASE + '/gallery/' + encodeURIComponent(g.slug) : BASE + '/gallery'; }," in Js
    Click = Js[Js.index("    galleryClick(e, g) {"):Js.index("    openGallery(g, {")]
    assert "if (e && (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button > 0)) return;" in Click   # the browser's own
    Open = Js[Js.index("    openGallery(g, { address = 'push' } = {}) {"):Js.index("    metalLabel(id)")]
    assert "history.pushState({ p3Gallery: g.id, p3Pushed: PAGE_LOAD }, '', href)" in Open          # Back closes it
    assert "document.title = this._galleryTitle(g);" in Open
    Close = Js[Js.index("    closeGallery(how = '') {"):Js.index("    _onGalleryHistory() {")]
    assert "history.state.p3Pushed === PAGE_LOAD) history.back();" in Close     # only through this page load's own step
    assert "window.addEventListener('popstate', () => this._onGalleryHistory());" in Js
    Shared = Js[Js.index("    _openSharedGallery() {"):Js.index("    async startFromGallery(id) {")]
    assert "(BASE || '') + '/'" not in Shared and "this.openGallery(g, { address: 'replace' });" in Shared   # the address stays
    # the dialog's own controls are the visitor's close (Back and Forward stay in order)
    assert '@click.self="closeGallery(\'user\')" @keydown.escape.window="closeGallery(\'user\')"' in Page
    assert '<button @click="closeGallery(\'user\')" class="btn-outline flex-1 py-3 text-xs uppercase tracking-[0.2em]">Back to gallery</button>' in Page
