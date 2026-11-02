"""Rings ON/OFF, safe and reversible (WP2, 1 Nov 2026). With Rings OFF after their launch every surface tells the truth and
the rest of the site works: charms carry the site and their tiles are never called rings (M1); a customer's own ring
designs stay in My Designs, open read-only and are refused plainly when changed (M2, L1, L2); ring bag lines stay,
marked and left out of the total, never block the charm lines, and their quantity is locked (M3, L3); a bag that changed
after Checkout showed it is reviewed again; ring quote requests are refused; the sign-in email names no product (L10);
the Admin is told the effects (L4); the Terms version never moves. Switched back ON, every customer answer and the data
are exactly as before. No product on offer: neutral wording and XJet's own picture (L5)."""

import re

from p3 import app as P3App
from p3.mail import TokenEmail
from p3.settings import WebDir
from tests.conftest import Harness
from tests.test_orders import Customer, _Order

AdminKey = "rings-onoff-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Unavailable = "This design is currently unavailable for customization or ordering."
Tables = ("designs", "batches", "candidates", "customizations", "bag_lines", "orders", "order_lines", "quote_requests",
          "quote_events", "gallery_items", "gallery_uses", "gallery_favorites")


async def _Set(H, Product, On):
    R = await H.Client.put("/api/admin/products/availability", json={"product": Product, "available": On}, headers=Admin)
    assert R.status_code == 200, R.text


async def _Shop(H):
    """Rings and charms on offer: the customer holds a ring design (customized, in the bag) and a charm in the bag; the
    Inspiration Gallery shows a ring and a charm."""
    await _Set(H, "charm", True)
    R = await H.Client.put("/api/admin/charm-prices", json={"materials": {"silver": {"fixed_prices": {"14": 109}}}}, headers=Admin)
    assert R.status_code == 200, R.text
    Ring = await H.NewDesign("A slim twisted band")
    RingCu = await H.Proceed(Ring["design_id"], Ring["candidates"][0]["id"])
    await H.Idle()
    assert (await H.Client.patch(f"/api/customizations/{RingCu['id']}", json={"ring_size": 7, "material_id": "silver"})).status_code == 200
    assert (await H.Client.post("/api/bag", json={"customization_id": RingCu["id"]})).status_code == 200
    Charm = await H.NewDesign("A crescent moon charm", product="charm")
    CharmCu = await H.Proceed(Charm["design_id"], Charm["candidates"][0]["id"])
    await H.Idle()
    assert (await H.Client.patch(f"/api/customizations/{CharmCu['id']}", json={"charm_size": 14})).status_code == 200
    assert (await H.Client.post("/api/bag", json={"customization_id": CharmCu["id"]})).status_code == 200
    Items = {}
    for Prompt, Product in (("A signet with a hexagon face", "ring"), ("A heart charm", "charm")):
        B = await H.NewDesign(Prompt, product=Product)
        await H.Client.put(f"/api/designs/{B['design_id']}/selection", json={"candidate_id": B["candidates"][0]["id"]})
        G = await H.Client.post("/api/admin/gallery", json={"design_id": B["design_id"]}, headers=Admin)
        assert G.status_code == 200, G.text
        Items[Product] = G.json()
    return Ring, RingCu, Charm, Items


async def _Snapshot(H, Ring, RingCu) -> dict:
    """Every customer answer this test reads, and the data, as they stand."""
    Out = {}
    for Path in ("/api/catalog", "/api/gallery", "/api/designs", f"/api/designs/{Ring['design_id']}", f"/api/customizations/{RingCu['id']}",
                 "/api/bag", "/api/checkout", "/api/quote-requests", "/api/orders", "/api/favorites", "/api/showcase", "/api/auth/terms",
                 "/", "/gallery", "/faq", "/materials", "/terms"):
        A = await H.Client.get(Path)
        Out[Path] = (A.status_code, A.text)
    P3App.SitemapCache.clear()                          # its one-minute cache would answer with the read before
    Map = await H.Client.get("/sitemap.xml")
    # The pages listed are the same; their <lastmod> dates move with any change of a page's content — the switch too,
    # when the sitemap is read meanwhile (a crawler then re-reads the pages that changed back)
    Out["/sitemap.xml"] = (Map.status_code, re.sub(r"<lastmod>[^<]*</lastmod>", "", Map.text))
    for T in Tables:
        Out[T] = H.Ctx.Db.All(f"SELECT * FROM {T} ORDER BY rowid")
    return Out


async def test_rings_off_is_truthful_everywhere_and_back_on_everything_is_as_before(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Ring, RingCu, Charm, Items = await _Shop(H)
        Before = await _Snapshot(H, Ring, RingCu)
        await _Set(H, "ring", False)
        # Catalog and Design: charms carry the site; a new ring is refused plainly (L1)
        Cat = (await H.Client.get("/api/catalog")).json()["products"]
        assert Cat["available"] == ["charm"] and Cat["default"] == "charm" and Cat["previewed"] == [] and Cat["paused"] == []
        N = await H.Client.post("/api/designs", data={"prompt": "A slim band", "product": "ring"})
        assert N.status_code == 409 and N.json()["error"] == {"code": "product_unavailable", "message": "Rings are currently unavailable for new designs."}
        # Gallery: the charm tile carries its product (M1); the ring is paused, viewable read-only, in the sitemap
        G = (await H.Client.get("/api/gallery")).json()
        assert [(T["id"], T["product_type"]) for T in G["items"]] == [(Items["charm"]["id"], "charm")]
        assert [(T["id"], T["available"]) for T in G["paused"]] == [(Items["ring"]["id"], False)]
        Share = (await H.Client.get(f"/api/gallery/{Items['ring']['id']}/share")).json()
        assert Share["available"] is False and f"/gallery/{Share['slug']}<" in (await H.Client.get("/sitemap.xml")).text
        # My Designs: the ring design stays, read-only (M2); it opens; Customize says it can't go to the bag (L2)
        Mine = {D["id"]: D for D in (await H.Client.get("/api/designs")).json()["designs"]}
        assert Mine[Ring["design_id"]]["available"] is False and "available" not in Mine[Charm["design_id"]]
        D = (await H.Client.get(f"/api/designs/{Ring['design_id']}")).json()
        assert D["available"] is False and D["customization"]["add_to_bag_blocked_reason"] == "design_unavailable"
        Cu = (await H.Client.get(f"/api/customizations/{RingCu['id']}")).json()
        assert Cu["can_add_to_bag"] is False and Cu["add_to_bag_blocked_reason"] == "design_unavailable"
        # Changing it is refused plainly, whichever way in, before any credit or AI call
        Cand = Ring["candidates"][0]["id"]
        Credits = H.Ctx.Accounts.Profile(H.Who)["used"]
        for What, Resp in {
            "refine": await H.Client.post(f"/api/designs/{Ring['design_id']}/batches", json={"parent_candidate_id": Cand, "instruction": "Thinner"}),
            "selection": await H.Client.put(f"/api/designs/{Ring['design_id']}/selection", json={"candidate_id": Cand}),
            "customize": await H.Client.post(f"/api/designs/{Ring['design_id']}/customize", json={"candidate_id": Cand}),
            "customization": await H.Client.patch(f"/api/customizations/{RingCu['id']}", json={"ring_size": 8}),
            "movie": await H.Client.post(f"/api/candidates/{Cand}/movie"),
            "quote request": await H.Client.post("/api/quote-requests", json={"design_id": Ring["design_id"], "candidate_id": Cand,
                                                                               "material_id": "gold_14k_yellow", "ring_size": 7,
                                                                               "quantity": 1, "customer": Customer}),
        }.items():
            assert Resp.status_code == 409 and Resp.json()["error"]["message"] == Unavailable, (What, Resp.status_code, Resp.text[:200])
        assert H.Ctx.Accounts.Profile(H.Who)["used"] == Credits
        # The bag: the ring line stays, marked and left out of the total; the charm can be ordered (M3); quantity locked (L3)
        Bag = (await H.Client.get("/api/bag")).json()
        Lines = {L["product_type"]: L for L in Bag["lines"]}
        assert Lines["ring"]["unavailable"] is True and Lines["ring"]["orderable"] is False and Lines["charm"]["orderable"] is True
        assert Bag["totals"] == {"USD": Lines["charm"]["line_total"]} and Bag["checkout_available"] is True
        assert Bag["checkout_note"] == ("One line is currently unavailable for customization or ordering. It stays in your bag, "
                                        "left out of the total; the rest can be ordered.")
        Q = await H.Client.patch(f"/api/bag/{Lines['ring']['id']}", json={"quantity": 2})
        assert Q.status_code == 409 and Q.json()["error"]["code"] == "design_unavailable"
        Co = (await H.Client.post("/api/checkout/quote", json={})).json()
        assert Co["ok"] is True and Co["excluded"] == 1 and Co["subtotal"] == Lines["charm"]["line_total"]
        # The site speaks of charms; the Terms version never moves; the sign-in email names no product; the Admin is told
        assert 'content="Design custom charms with AI' in (await H.Client.get("/")).text
        assert (await H.Client.get("/api/auth/terms")).text == Before["/api/auth/terms"][1]
        _Subject, Mail = TokenEmail("Dana", "ABC123", "https://p3.test/#token=ABC123")
        assert "start designing &mdash;" in Mail and "your ring" not in Mail
        Js = (WebDir / "admin.js").read_text(encoding="utf-8")
        assert "in My Designs, read-only" in Js and "left out of the total" in Js and "get no new quote" in Js
        # Back ON: every customer answer and the data are exactly as before
        await _Set(H, "ring", True)
        After = await _Snapshot(H, Ring, RingCu)
        assert [K for K in Before if Before[K] != After[K]] == []
    finally:
        await H.Close()


async def test_with_rings_off_the_charm_is_ordered_and_the_ring_line_stays(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _Shop(H)
        await _Set(H, "ring", False)
        Co = (await H.Client.post("/api/checkout/quote", json={})).json()
        Shown = [L["bag_line_id"] for L in Co["lines"] if not L["problem"]]
        RingLine = next(L for L in Co["lines"] if L["problem"])
        # A bag that changed after Checkout showed it is reviewed again, never ordered unseen
        for N, Over in enumerate(({"expected_lines": Shown + [RingLine["bag_line_id"]], "expected_total": Co["total"]},
                                  {"expected_lines": Shown, "expected_total": Co["total"] + 1})):
            X = await H.Client.post("/api/orders", json=_Order(client_request_id=f"x-{N}", **Over))
            assert X.status_code == 409 and X.json()["error"]["code"] == "bag_changed", X.text
        O = await H.Client.post("/api/orders", json=_Order(client_request_id="x-ok", expected_lines=Shown, expected_total=Co["total"]))
        assert O.status_code == 200, O.text
        assert [L["product_type"] for L in O.json()["lines"]] == ["charm"] and O.json()["total"] == Co["total"]
        Bag = (await H.Client.get("/api/bag")).json()
        assert [(L["product_type"], L["unavailable"]) for L in Bag["lines"]] == [("ring", True)] and Bag["checkout_available"] is False
        await _Set(H, "ring", True)                                          # back ON: the ring line can be ordered again
        Bag = (await H.Client.get("/api/bag")).json()
        assert Bag["lines"][0]["orderable"] is True and Bag["checkout_available"] is True and Bag["checkout_note"] is None
    finally:
        await H.Close()


async def test_with_no_product_on_offer_the_site_names_no_ring(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Ring, _RingCu, Charm, _Items = await _Shop(H)
        await _Set(H, "ring", False)
        await _Set(H, "charm", False)
        Faq = (await H.Client.get("/faq")).text                              # the FAQ and Materials say "per piece", never "per ring"
        assert "Fashion jewelry pieces are priced per piece by material." in Faq and "Your ring size does not change the price." not in Faq
        Materials = (await H.Client.get("/materials")).text
        assert "Fashion metals have a fixed price per piece, set by material." in Materials and "fixed price per ring" not in Materials
        assert "Prices in US dollars, per piece, fixed for the material." in Materials and "Prices in US dollars, per ring" not in Materials
        Head = (await H.Client.get("/")).text.split("<body")[0]
        assert '<meta property="og:image" content="http://p3.test/static/images/printed-jewelry.jpg">' in Head
        assert (await H.Client.get("/api/showcase")).json()["still"]["image_url"] == "/static/images/printed-jewelry.jpg"
        N = await H.Client.post("/api/designs", data={"prompt": "A band"})
        assert N.status_code == 409 and N.json()["error"]["code"] == "product_unavailable"
        Mine = (await H.Client.get("/api/designs")).json()["designs"]
        assert {Ring["design_id"], Charm["design_id"]} <= {D["id"] for D in Mine} and all(D["available"] is False for D in Mine)
    finally:
        await H.Close()


async def test_a_ring_made_while_rings_switch_off_is_kept_and_shown(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _Set(H, "charm", True)
        H.Provider.LatencyS = 1.0                                            # its images are still being made …
        R = await H.Client.post("/api/designs", data={"prompt": "A slim band", "product": "ring"})
        assert R.status_code == 200, R.text
        await _Set(H, "ring", False)                                         # … when rings are switched OFF
        H.Provider.LatencyS = 0.0
        await H.Idle()
        Mine = {D["id"]: D for D in (await H.Client.get("/api/designs")).json()["designs"]}
        assert Mine[R.json()["design_id"]]["available"] is False                # charged and shown, never hidden
        B = (await H.Client.get(f"/api/batches/{R.json()['id']}")).json()
        assert [C["status"] for C in B["candidates"]] == ["ready"] * 4
    finally:
        await H.Close()
