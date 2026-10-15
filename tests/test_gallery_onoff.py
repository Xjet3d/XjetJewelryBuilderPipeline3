"""Gallery ON/OFF (the owner's policy of 2026-10-15). A product switched OFF after its launch is paused, not hidden: its
gallery designs stay viewable — their page and old link, Share, Favorites, the sitemap — and read-only. Every
production action on a design of it is refused with 409 design_unavailable, whatever the way in: Make it yours, an
option choice, Customize, a customization change, a movie, another option, the bag, a quote request; its bag lines
cannot be ordered. A product never yet on offer stays hidden. Switched back ON, everything works again."""

import re

from p3 import app as P3App
from p3.settings import WebDir
from tests.conftest import Harness
from tests.test_charm_foundation import CharmDesign
from tests.test_orders import Address, Customer

AdminKey = "gallery-onoff-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Unavailable = "Currently unavailable for customization or ordering."
Old = "2020-01-01T00:00:00.000+00:00"


async def _Set(H, Product, On):
    R = await H.Client.put("/api/admin/products/availability", json={"product": Product, "available": On}, headers=Admin)
    assert R.status_code == 200, R.text
    return R.json()


async def _Publish(H, Prompt, **Form):
    B = await H.NewDesign(Prompt, **Form)
    await H.Client.put(f"/api/designs/{B['design_id']}/selection", json={"candidate_id": B["candidates"][0]["id"]})
    R = await H.Client.post("/api/admin/gallery", json={"design_id": B["design_id"]}, headers=Admin)
    assert R.status_code == 200, R.text
    return B, R.json(), (await H.Client.get(f"/api/gallery/{R.json()['id']}/share")).json()


def _Refused(R, What=""):
    assert R.status_code == 409 and R.json()["error"]["code"] == "design_unavailable", (What, R.status_code, R.text[:200])
    assert R.json()["error"]["message"] == "This design is currently unavailable for customization or ordering.", What


async def test_a_paused_design_stays_viewable_everywhere_and_nothing_of_it_is_made(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _Set(H, "charm", True)                                                    # charms launched
        await H.Client.put("/api/admin/charm-prices", json={"materials": {"silver": {"fixed_prices": {"14": 109}}}}, headers=Admin)
        _RB, RingItem, _RS = await _Publish(H, "A slim band", product="ring")
        _CB, CharmItem, CharmShare = await _Publish(H, "A heart charm", product="charm")
        Own = await H.NewDesign("A star charm", product="charm")                       # a charm of the customer's own
        OwnCand, OwnBatch = Own["candidates"][0]["id"], Own["id"]
        Cust = {"X-Access-Token": H.Ctx.Accounts.IssueToken("customer")[0]}
        # A customer favours the gallery charm, makes it theirs and takes it to the bag — all while charms are ON
        assert (await H.Client.put(f"/api/favorites/{CharmItem['design_id']}", headers=Cust)).status_code == 200
        D = (await H.Client.post(f"/api/gallery/{CharmItem['id']}/start", json={}, headers=Cust)).json()
        Cand = D["batches"][0]["candidates"][0]["id"]
        assert (await H.Client.put(f"/api/designs/{D['id']}/selection", json={"candidate_id": Cand}, headers=Cust)).status_code == 200
        C = (await H.Client.post(f"/api/designs/{D['id']}/customize", json={"candidate_id": Cand}, headers=Cust)).json()
        await H.Idle()
        assert (await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": 14}, headers=Cust)).status_code == 200
        Bag = (await H.Client.post("/api/bag", json={"customization_id": C["id"]}, headers=Cust)).json()
        assert Bag["checkout_available"] is True and Bag["lines"][0]["orderable"] is True

        await _Set(H, "charm", False)                                                   # charms paused
        # Viewable: its page (not a 404), its old address, Share, the sitemap, the customer's Favorites
        Page = await H.Client.get(f"/gallery/{CharmShare['slug']}")
        Head = Page.text.split("<body")[0]
        assert Page.status_code == 200 and Unavailable in Head and "make it yours" not in Head
        assert '"available": false' in Head and f'<link rel="canonical" href="http://p3.test/gallery/{CharmShare["slug"]}">' in Head
        assert (await H.Client.get(f"/design/{CharmShare['slug']}")).status_code == 301
        S = (await H.Client.get(f"/api/gallery/{CharmItem['id']}/share")).json()
        assert S["available"] is False and S["url"] == CharmShare["url"]
        P3App.SitemapCache.clear()
        assert f"/gallery/{CharmShare['slug']}</loc>" in (await H.Client.get("/sitemap.xml")).text
        Fav = (await H.Client.get("/api/favorites", headers=Cust)).json()["items"]
        assert [(F["design_id"], F["available"]) for F in Fav] == [(CharmItem["design_id"], False)]
        Api = (await H.Client.get("/api/gallery")).json()
        assert [T["id"] for T in Api["items"]] == [RingItem["id"]]                     # not in the Gallery's listing
        assert [(T["id"], T["available"], T["product_type"]) for T in Api["paused"]] == [(CharmItem["id"], False, "charm")]
        assert (await H.Client.put(f"/api/favorites/{CharmItem['design_id']}", headers=Cust)).status_code == 200   # ♥ makes nothing
        # Read-only: every production action is refused, whichever way in — the gallery design the customer made theirs …
        _Refused(await H.Client.post(f"/api/gallery/{CharmItem['id']}/start", json={}, headers=Cust), "Make it yours")
        _Refused(await H.Client.put(f"/api/designs/{D['id']}/selection", json={"candidate_id": Cand}, headers=Cust), "selection")
        _Refused(await H.Client.post(f"/api/designs/{D['id']}/customize", json={"candidate_id": Cand}, headers=Cust), "customize")
        _Refused(await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": 18}, headers=Cust), "customization")
        _Refused(await H.Client.post("/api/bag", json={"customization_id": C["id"]}, headers=Cust), "bag")
        _Refused(await H.Client.post("/api/quote-requests", json={"design_id": D["id"], "candidate_id": Cand, "material_id": "gold_18k_yellow",
                                                                   "customer": Customer, "quantity": 1}, headers=Cust), "quote")
        # … and the customer's own charm design: choosing, Customize, a movie, another option, a retry
        _Refused(await H.Client.put(f"/api/designs/{Own['design_id']}/selection", json={"candidate_id": OwnCand}), "own selection")
        _Refused(await H.Client.post(f"/api/designs/{Own['design_id']}/customize", json={"candidate_id": OwnCand}), "own customize")
        _Refused(await H.Client.post(f"/api/candidates/{OwnCand}/movie"), "movie")
        _Refused(await H.Client.post(f"/api/candidates/{OwnCand}/retry"), "another option")
        _Refused(await H.Client.post(f"/api/batches/{OwnBatch}/retry-failed"), "retry")
        for Did, Who in ((D["id"], Cust), (Own["design_id"], None)):                   # refining: the design is not open to it
            R = await H.Client.post(f"/api/designs/{Did}/batches", json={"parent_candidate_id": Cand, "instruction": "Thinner"},
                                    **({"headers": Who} if Who else {}))
            assert R.status_code == 404, R.text
        # Its bag line stays, marked, and cannot be ordered
        Bag = (await H.Client.get("/api/bag", headers=Cust)).json()
        assert Bag["checkout_available"] is False and Bag["lines"][0]["orderable"] is False and Bag["lines"][0]["unavailable"] is True
        assert "currently unavailable for customization or ordering" in Bag["checkout_note"]
        Q = (await H.Client.post("/api/checkout/quote", json={}, headers=Cust)).json()
        assert Q["ok"] is False and Q["lines"][0]["problem"] == Unavailable
        R = await H.Client.post("/api/orders", json={"customer": Customer, "address": Address, "terms_accepted": True,
                                                     "client_request_id": "off-1"}, headers=Cust)
        assert R.status_code == 409 and R.json()["error"]["code"] == "bag_not_orderable" and R.json()["error"]["message"] == Unavailable
        # Rings (ON) are untouched; the Admin sees charms launched and OFF
        assert (await H.Client.post(f"/api/gallery/{RingItem['id']}/start", json={}, headers=Cust)).status_code == 200
        Products = (await H.Client.get("/api/admin/products", headers=Admin)).json()["products"]
        assert {P["id"]: (P["available"], P["launched"]) for P in Products} == {"ring": (True, True), "charm": (False, True)}

        await _Set(H, "charm", True)                                                    # back ON: everything works again
        assert (await H.Client.post(f"/api/designs/{D['id']}/customize", json={"candidate_id": Cand}, headers=Cust)).status_code == 200
        assert (await H.Client.get("/api/bag", headers=Cust)).json()["checkout_available"] is True
        Fav = (await H.Client.get("/api/favorites", headers=Cust)).json()["items"]
        assert [F["design_id"] for F in Fav] == [CharmItem["design_id"]] and "available" not in Fav[0]
        assert (await H.Client.get("/api/gallery")).json()["paused"] == []
        Head = (await H.Client.get(f"/gallery/{CharmShare['slug']}")).text.split("<body")[0]
        assert Unavailable not in Head and "make it yours" in Head
    finally:
        await H.Close()


async def test_a_product_never_on_offer_stays_hidden(tmp_path):
    """Charms are OFF by default: before their launch an Admin may prepare charm designs in the gallery (seen only in the
    Admin preview) — none of them reaches customers or search engines."""
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Ring = await H.NewDesign()
        Asset = Ring["candidates"][0]["image_url"].split("/assets/", 1)[1]
        Cid, _ = CharmDesign(H, Asset, "Moon Drop")
        Item = (await H.Client.post("/api/admin/gallery", json={"design_id": Cid}, headers=Admin)).json()
        Slug = H.Ctx.Db.One("SELECT share_slug FROM designs WHERE id = ?", (Cid,))["share_slug"]
        assert H.Ctx.Products.Launched("ring") and not H.Ctx.Products.Launched("charm")
        Api = (await H.Client.get("/api/gallery")).json()
        assert Api["items"] == [] and Api["paused"] == []
        for Path_ in (f"/gallery/{Slug}", f"/design/{Slug}"):
            assert (await H.Client.get(Path_)).status_code == 404, Path_
        assert (await H.Client.get(f"/api/gallery/{Item['id']}/share")).status_code == 404
        P3App.SitemapCache.clear()
        assert Slug not in (await H.Client.get("/sitemap.xml")).text
        Cust = {"X-Access-Token": H.Ctx.Accounts.IssueToken("customer")[0]}
        assert (await H.Client.put(f"/api/favorites/{Cid}", headers=Cust)).status_code == 404
        assert (await H.Client.post(f"/api/gallery/{Item['id']}/start", json={}, headers=Cust)).status_code == 404
        # Launched once, then switched OFF again: paused — the page stays up, read-only
        await _Set(H, "charm", True)
        await _Set(H, "charm", False)
        assert H.Ctx.Products.Launched("charm")
        R = await H.Client.get(f"/gallery/{Slug}")
        assert R.status_code == 200 and Unavailable in R.text.split("<body")[0]
    finally:
        await H.Close()


async def test_pausing_moves_the_design_pages_date_and_resuming_moves_it_again(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey, PublicBaseUrl="https://atelier.example.com")
    try:
        await _Set(H, "charm", True)
        _R, _RI, Ring = await _Publish(H, "A slim band", product="ring")
        _C, _CI, Charm = await _Publish(H, "A heart charm", product="charm")

        async def Dates():
            P3App.SitemapCache.clear()
            Map = (await H.Client.get("/sitemap.xml")).text
            return dict(re.findall(r"<loc>https://atelier\.example\.com(/[^<]*)</loc><lastmod>([^<]+)</lastmod>", Map))

        await Dates()
        H.Ctx.Db.Execute("UPDATE page_lastmod SET changed_at = ?", (Old,))
        await _Set(H, "charm", False)
        D = await Dates()
        assert D[f"/gallery/{Charm['slug']}"] != "2020-01-01T00:00:00+00:00"           # its page now says it is unavailable
        assert D[f"/gallery/{Ring['slug']}"] == "2020-01-01T00:00:00+00:00"             # the ring's did not change
        H.Ctx.Db.Execute("UPDATE page_lastmod SET changed_at = ?", (Old,))
        await _Set(H, "charm", True)
        assert (await Dates())[f"/gallery/{Charm['slug']}"] != "2020-01-01T00:00:00+00:00"
    finally:
        await H.Close()


def test_the_customer_page_shows_a_paused_design_read_only_from_every_entry_point():
    """web/app.js and web/index.html: the dialog's production CTAs give way to the notice; Favorites and the bag say it;
    the paused designs are found by their link, from history, Favorites and after sign-in; a late refusal turns the
    dialog read-only."""
    Js = (WebDir / "app.js").read_text(encoding="utf-8")
    Page = (WebDir / "index.html").read_text(encoding="utf-8")
    assert "isOff(g) { return !!g && g.available === false; }," in Js
    assert "galleryFind(match) { return this.gallery.find(match) || this.galleryPaused.find(match); }," in Js
    assert "this.gallery = r.items || []; this.galleryPaused = r.paused || [];" in Js
    for Lookup in ("const g = this.galleryFind(x => x.slug === slug);", "const g = id && this.galleryFind(x => x.id === id);",
                   "const g = this.galleryFind(x => x.id === fav);"):
        assert Lookup in Js, Lookup                                                    # history, a link, ♥ after sign-in
    assert "if (typeof s.available === 'boolean') g.available = s.available;" in Js       # fresh as the dialog opens
    assert "const g = this.galleryItem; if (!g || this.isOff(g)) return;" in Js
    assert "if (e.code === 'design_unavailable') {" in Js
    assert ('<p x-show="isOff(galleryItem)" class="text-sm text-zinc-500 leading-relaxed" role="status" data-unavailable>'
            'Currently unavailable for customization or ordering.</p>') in Page
    assert '<button x-show="!isOff(galleryItem)" @click="makeItYours()"' in Page
    assert '<p x-show="!isOff(galleryItem)" class="brand-font text-xl leading-snug text-zinc-800">Love this design? Make it yours.</p>' in Page
    assert "x-text=\"isOff(f) ? 'Currently unavailable for customization or ordering' : 'Inspiration Gallery'\"" in Page
    assert "x-text=\"l.unavailable ? 'Currently unavailable for customization or ordering' : 'Cannot be ordered yet'\"" in Page
