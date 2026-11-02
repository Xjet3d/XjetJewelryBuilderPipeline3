"""Material statuses (WP3, 2 Nov 2026): Available, Preview Only, Quote Only and Hidden, per product and metal (Admin → Pricing
& Materials → Customer status). The release maps today exactly — the fashion metals available, solid gold quote only — so
customers see what they saw; activation is one Admin change (the seven gold-family metals Preview Only for rings and
charms). A status gates actions only: a price, Add to Bag, an order or a quote only where it allows them, never relaxed by
an Admin session; every saved record still opens, a placed order is never checked; a bag keeps a Preview Only line, leaves
it out of the total and orders the rest. Customize opens on Stainless Steel (a new charm at 14 mm, $69) without touching a
saved choice. The Materials page, the FAQ, the description and the crawler list follow; no status makes a Terms version."""

import re

from p3 import materialstatus as MaterialStatus
from tests.conftest import Harness
from tests.test_orders import _InBag, _Order
from tests.test_quotes import Admin, AdminKey, Customer, _Offer, _Request

Gold = ("vermeil", "gold_10k_yellow", "gold_14k_yellow", "gold_18k_yellow", "gold_10k_rose", "gold_14k_rose", "gold_18k_rose")
Solid = Gold[1:]
PreviewText = "Preview only — not available to order. Order it in Sterling Silver or Stainless Steel."


async def _Charms(H):
    """Charms on offer, at Atelier's prices per size in the three fixed-price metals, with a cost per gram."""
    R = await H.Client.put("/api/admin/products/availability", json={"product": "charm", "available": True}, headers=Admin)
    assert R.status_code == 200, R.text
    Rows = {"stainless_steel": (49, 69, 99), "silver": (69, 109, 159), "vermeil": (59, 99, 139)}
    R = await H.Client.put("/api/admin/charm-prices", headers=Admin, json={"materials": {
        M: {"fixed_prices": dict(zip(("10", "14", "18"), P)), "price_per_g": 6, "cost_per_g": 2} for M, P in Rows.items()}})
    assert R.status_code == 200, R.text


async def _Statuses(H, Changes, Note="WP3 test", DryRun=False):
    return await H.Client.put("/api/admin/material-statuses", headers=Admin,
                              json={"changes": Changes, "note": Note, "dry_run": DryRun})


def _Activation():
    return [{"product": P, "material_id": M, "status": "preview"} for P in ("ring", "charm") for M in Gold]


def _Description(Page: str) -> str:
    return re.search(r'<meta name="description" content="([^"]*)"', Page).group(1)


async def test_the_release_maps_today_exactly(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _Charms(H)
        A = (await H.Client.get("/api/admin/material-statuses", headers=Admin)).json()
        Today = {"stainless_steel": "available", "silver": "available", "vermeil": "available", **{M: "quote" for M in Solid}}
        assert all({R["id"]: R["status"] for R in A["products"][P]} == Today for P in ("ring", "charm"))
        # The customer catalog's groups are the groups of before, byte for byte; nothing is hidden
        Cat = (await H.Client.get("/api/catalog")).json()
        assert Cat["groups"] == H.Ctx.Catalog.ToJson()["groups"] and "hidden_materials" not in Cat
        assert [(G["id"], G["purchasable"], [M["id"] for M in G["materials"]]) for G in Cat["groups"]] == \
            [("fashion", True, ["stainless_steel", "silver", "vermeil"]), ("luxury", False, list(Solid))]
        assert [(M["id"], M["group"], M["purchasable"]) for M in Cat["products"]["charm"]["materials"]] == \
            [(M, "fashion", True) for M in ("stainless_steel", "silver", "vermeil")] + [(M, "luxury", False) for M in Solid]
        assert Cat["default_material_id"] == "stainless_steel"                        # Customize opens on it (WP3)
        # Gold is quoted, the fashion metals ordered, exactly as before
        B = await H.NewDesign("A signet ring with a crescent", product="ring")
        Body = {"design_id": B["design_id"], "candidate_id": B["candidates"][0]["id"], "ring_size": 7, "quantity": 1, "customer": Customer}
        assert (await H.Client.post("/api/quote-requests", json={**Body, "material_id": "gold_14k_rose"})).status_code == 200
        assert (await H.Client.post("/api/quote-requests", json={**Body, "material_id": "vermeil"})).status_code == 409
        assert (await H.Client.get("/api/quote", params={"material_id": "vermeil"})).json()["unit_price"] == 200.0
        Gq = (await H.Client.get("/api/quote", params={"material_id": "gold_14k_rose"})).json()
        assert Gq["unit_price"] is None and Gq["unavailable_reason"] == "luxury_pricing_unavailable"
        # The Materials page and its crawler list as before; the FAQ now says what gold truly is: by request
        Page = (await H.Client.get("/materials")).text
        assert "Gold — available by request" in Page and "Gold — preview only" not in Page
        assert "<li>14K Gold over Stainless Steel — available to order</li><li>10K Yellow Gold — by request (quoted individually)</li>" in Page
        assert _Description(Page).endswith("at a fixed price per piece, and solid gold by quote: appearance, finish, wear and care.")
        Faq = (await H.Client.get("/faq")).text
        assert ("Fashion jewelry: Stainless Steel, Sterling Silver, 14K Gold over Stainless Steel. Luxury: solid gold in 10K Yellow, "
                "14K Yellow, 18K Yellow, 10K Rose, 14K Rose, 18K Rose — by request (quoted individually).") in Faq
    finally:
        await H.Close()


async def test_the_admin_sets_statuses_by_its_rules_without_a_price_version(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _Charms(H)
        Terms = H.Ctx.Accounts.Consent.Current()
        # Available only with a fixed price (for a charm: every size on offer), density and cost; gold charms have none
        R = await _Statuses(H, [{"product": "ring", "material_id": "gold_10k_yellow", "status": "available"}])
        assert R.status_code == 400 and "can be Available only with a fixed price and its cost per gram set" in R.json()["error"]["message"]
        R = await _Statuses(H, [{"product": "charm", "material_id": "gold_14k_rose", "status": "available"}])
        assert R.status_code == 400 and "gold charms are quoted individually" in R.json()["error"]["message"]
        await H.Client.put("/api/admin/charm-prices", headers=Admin, json={"materials": {
            "stainless_steel": {"fixed_prices": {"10": 49, "14": 69, "18": 99}, "cost_per_g": 2},
            "silver": {"fixed_prices": {"10": 69, "14": 109, "18": 159}, "cost_per_g": 2}, "vermeil": {"fixed_prices": {"14": 99}}}})
        R = await _Statuses(H, [{"product": "charm", "material_id": "vermeil", "status": "available"}])
        assert R.status_code == 400 and "a fixed price for 10 mm, 18 mm" in R.json()["error"]["message"]
        # Stainless Steel stays Available while Customize opens on it
        R = await _Statuses(H, [{"product": "charm", "material_id": "stainless_steel", "status": "preview"}])
        assert R.status_code == 400 and R.json()["error"]["message"] == "Stainless Steel is the metal Customize opens on: it stays Available."
        await _Charms(H)
        Prices = (H.Ctx.MaterialPrices.Current()["version"], H.Ctx.CharmPrices.Current()["version"])
        # A dry run says what would change and which customer records it touches — and saves nothing
        await _InBag(H, Material="vermeil")
        await _Request(H, Material="gold_14k_yellow")
        Plan = (await _Statuses(H, _Activation(), DryRun=True)).json()
        assert not Plan["saved"] and len(Plan["changes"]) == 14
        V = next(C for C in Plan["changes"] if (C["product"], C["material_id"]) == ("ring", "vermeil"))
        assert (V["from"], V["to"], V["affected"]["customizations"], V["affected"]["bag_lines"], V["affected"]["bags"]) == ("available", "preview", 1, 1, 1)
        G = next(C for C in Plan["changes"] if (C["product"], C["material_id"]) == ("ring", "gold_14k_yellow"))
        assert (G["from"], G["affected"]["open_quotes"]) == ("quote", [{"ref": "Q-5001", "status": "new", "offer": False}])
        assert MaterialStatus.Of(H.Ctx, "ring", "vermeil") == "available"
        # Activation: one change, both products, logged with its note; no new price version; the Terms version unchanged
        R = await _Statuses(H, _Activation(), Note="Activation: gold Preview Only")
        assert R.status_code == 200 and R.json()["saved"], R.text
        assert all(MaterialStatus.Of(H.Ctx, P, M) == "preview" for P in ("ring", "charm") for M in Gold)
        assert R.json()["log"][0]["note"] == "Activation: gold Preview Only"
        assert (H.Ctx.MaterialPrices.Current()["version"], H.Ctx.CharmPrices.Current()["version"]) == Prices
        assert H.Ctx.Accounts.Consent.Current() == Terms
        assert (await _Statuses(H, _Activation())).json()["changes"] == []                   # the same again: nothing changes
        assert (await H.Client.put("/api/admin/material-statuses", json={"changes": _Activation()})).status_code in (401, 403)
    finally:
        await H.Close()


async def test_preview_only_has_no_price_bag_order_or_quote_and_the_bag_orders_the_rest(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _Charms(H)
        await _InBag(H, Material="vermeil")                                     # bagged before activation
        await _InBag(H, Prompt="A plain band", Material="silver")
        _B, Q = await _Request(H, Material="gold_14k_yellow")                   # an open gold quote, its offer sent
        assert (await _Offer(H, Q)).status_code == 200
        assert (await _Statuses(H, _Activation())).status_code == 200
        # No price anywhere
        for Params in ({"material_id": "vermeil"}, {"material_id": "vermeil", "product": "charm", "charm_size": 14}):
            Qv = (await H.Client.get("/api/quote", params=Params)).json()
            assert (Qv["unit_price"], Qv["unavailable_reason"]) == (None, "material_preview_only"), Params
        Cat = (await H.Client.get("/api/catalog")).json()
        assert [(G["id"], [M["id"] for M in G["materials"]]) for G in Cat["groups"]] == [("fashion", ["stainless_steel", "silver"]), ("preview", list(Gold))]
        Charm = {M["id"]: M for M in Cat["products"]["charm"]["materials"]}
        assert (Charm["vermeil"]["group"], Charm["vermeil"]["purchasable"], [P["price"] for P in Charm["vermeil"]["prices"]]) == ("preview", False, [None] * 3)
        # Customize: seen, never priced or bagged
        B = await H.NewDesign("A twisted band", product="ring")
        Cus = await H.Proceed(B["design_id"], B["candidates"][0]["id"])
        V = (await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"material_id": "vermeil", "ring_size": 7})).json()
        assert (V["quote"]["unit_price"], V["can_add_to_bag"], V["add_to_bag_blocked_reason"]) == (None, False, "preview_only")
        R = await H.Client.post("/api/bag", json={"customization_id": Cus["id"]})
        assert R.status_code == 409 and R.json()["error"] == {"code": "preview_only", "message": PreviewText}
        # An Admin session never relaxes a status
        Login = await H.Client.post("/api/admin/login", json={"key": AdminKey})
        Cookie = {"Cookie": "; ".join(f"{K}={V}" for K, V in Login.cookies.items())}
        assert (await H.Client.post("/api/bag", json={"customization_id": Cus["id"]}, headers=Cookie)).status_code == 409
        # The very line bagged before activation, added again: refused too (never "already in the bag")
        Before = H.Ctx.Db.One("SELECT customization_id FROM bag_lines WHERE material_id = 'vermeil'")["customization_id"]
        R = await H.Client.post("/api/bag", json={"customization_id": Before})
        assert R.status_code == 409 and R.json()["error"]["code"] == "preview_only", R.text
        # No quote: no new request, no new offer on the open one, and its sent offer is not approved
        Body = {"design_id": B["design_id"], "candidate_id": B["candidates"][0]["id"], "ring_size": 7, "quantity": 1, "customer": Customer}
        R = await H.Client.post("/api/quote-requests", json={**Body, "material_id": "gold_14k_yellow"})
        assert R.status_code == 409 and R.json()["error"] == {"code": "not_quotable", "message": "14K Yellow Gold is preview only: it is not available to order or quote."}
        assert (await _Offer(H, Q, unit_price=2700)).status_code == 409
        D = (await H.Client.get(f"/api/admin/quote-requests/{Q['id']}", headers=Admin)).json()
        assert D["offer_problem"] == "14K Yellow Gold is preview only: it is not available to order or quote."
        # The bag keeps the Preview Only line, leaves it out of the total and orders the rest
        Bag = (await H.Client.get("/api/bag")).json()
        Line = next(L for L in Bag["lines"] if L["material_id"] == "vermeil")
        assert (Line["orderable"], Line["material_status"], Bag["checkout_available"], Bag["totals"]) == (False, "preview", True, {"USD": 200.0})
        Co = (await H.Client.post("/api/checkout/quote", json={})).json()
        assert [L["problem"] for L in Co["lines"] if L["material_id"] == "vermeil"] == [PreviewText] and Co["ok"]
        O = (await H.Client.post("/api/orders", json=_Order())).json()
        assert [L["material_id"] for L in O["lines"]] == ["silver"], O
        assert [L["material_id"] for L in (await H.Client.get("/api/bag")).json()["lines"]] == ["vermeil"]
    finally:
        await H.Close()


async def test_every_saved_record_opens_under_each_status_and_a_hidden_metal_still_renders(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _Charms(H)
        Did, _C, _Bag = await _InBag(H, Material="vermeil")
        O = (await H.Client.post("/api/orders", json=_Order())).json()          # an order placed in it: never checked
        Did2, _C2, _Bag2 = await _InBag(H, Prompt="A plain band", Material="vermeil")
        _B, Q = await _Request(H, Material="gold_14k_yellow")
        Cus = H.Ctx.Db.One("SELECT id FROM customizations WHERE design_id = ?", (Did2,))["id"]
        for S in ("available", "preview", "quote", "hidden"):
            R = await _Statuses(H, [{"product": "ring", "material_id": "vermeil", "status": S},
                                    {"product": "ring", "material_id": "gold_14k_yellow", "status": "quote" if S == "available" else S}])
            assert R.status_code == 200, (S, R.text)
            for Path in (f"/api/designs/{Did}", f"/api/designs/{Did2}", f"/api/customizations/{Cus}", "/api/bag", "/api/checkout",
                         "/api/quote-requests", "/api/orders", f"/api/orders/{O['id']}"):
                assert (await H.Client.get(Path)).status_code == 200, (S, Path)
            for Path in (f"/api/admin/orders/{O['id']}", f"/api/admin/quote-requests/{Q['id']}"):
                assert (await H.Client.get(Path, headers=Admin)).status_code == 200, (S, Path)
            Bag = (await H.Client.get("/api/bag")).json()
            assert Bag["lines"][0]["material_label"] == "14K Gold over Stainless Steel" and Bag["lines"][0]["orderable"] == (S == "available")
            assert (await H.Client.get(f"/api/orders/{O['id']}")).json()["lines"][0]["material_id"] == "vermeil"
        # Hidden: offered nowhere, yet what was made in it renders
        Cat = (await H.Client.get("/api/catalog")).json()
        assert "vermeil" not in [M["id"] for G in Cat["groups"] for M in G["materials"]] and [M["id"] for M in Cat["hidden_materials"]] == ["vermeil", "gold_14k_yellow"]
        R = await H.Client.patch(f"/api/customizations/{Cus}", json={"material_id": "vermeil"})
        assert R.status_code == 400 and R.json()["error"] == {"code": "material_not_offered", "message": "This metal is no longer offered."}
        assert (await H.Client.get(f"/api/customizations/{Cus}")).json()["add_to_bag_blocked_reason"] == "material_not_offered"
    finally:
        await H.Close()


async def test_customize_opens_on_stainless_steel_and_never_rewrites_a_saved_choice(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _Charms(H)
        # A new ring opens on Stainless Steel at its fixed price; a new charm at 14 mm, $69
        B = await H.NewDesign("A twisted band", product="ring")
        Cus = await H.Proceed(B["design_id"], B["candidates"][0]["id"])
        assert (Cus["material_id"], Cus["quote"]["unit_price"]) == ("stainless_steel", 90.0)
        C = await H.NewDesign("A heart charm", product="charm")
        Ch = await H.Proceed(C["design_id"], C["candidates"][0]["id"])
        assert (Ch["material_id"], Ch["charm_size"], Ch["quote"]["unit_price"], Ch["can_add_to_bag"]) == ("stainless_steel", 14.0, 69.0, True)
        # A saved gold design entered again: Customize shows Stainless Steel; the saved records stay byte-identical
        await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"material_id": "gold_18k_yellow", "ring_size": 6})

        def Saved():
            return (H.Ctx.Db.One("SELECT * FROM customizations WHERE id = ?", (Cus["id"],)),
                    H.Ctx.Db.One("SELECT * FROM designs WHERE id = ?", (B["design_id"],)))
        Before = Saved()
        Again = await H.Proceed(B["design_id"], B["candidates"][0]["id"])
        assert (Again["material_id"], Again["ring_size"], Again["quote"]["unit_price"], Again["can_add_to_bag"]) == ("stainless_steel", 6.0, 90.0, True)
        assert (await H.Design(B["design_id"]))["customization"]["material_id"] == "stainless_steel"     # a reload shows the same
        assert (await H.Client.get(f"/api/customizations/{Cus['id']}")).json()["material_id"] == "stainless_steel"
        assert Saved() == Before
        # The customer's first change makes it the saved choice
        After = (await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"ring_size": 7})).json()
        assert (After["material_id"], After["ring_size"]) == ("stainless_steel", 7.0)
        assert (Saved()[0]["material_id"], Saved()[0]["ring_size"]) == ("stainless_steel", 7.0)
        # … or Add to Bag at once: the line is what Customize showed
        await H.Client.patch(f"/api/customizations/{Ch['id']}", json={"material_id": "gold_14k_rose", "charm_size": 18})
        await H.Proceed(C["design_id"], C["candidates"][0]["id"])
        Bag = (await H.Client.post("/api/bag", json={"customization_id": Ch["id"]})).json()
        assert [(L["material_id"], L["charm_size"], L["unit_price"]) for L in Bag["lines"]] == [("stainless_steel", 18.0, 99.0)]
        # My Designs never says Preview Only
        assert (await _Statuses(H, _Activation())).status_code == 200
        assert "preview" not in (await H.Client.get("/api/designs")).text.lower()
    finally:
        await H.Close()


async def test_the_materials_page_faq_description_and_crawler_list_follow_the_statuses(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _Charms(H)
        Terms = H.Ctx.Accounts.Consent.Current()
        assert (await _Statuses(H, _Activation())).status_code == 200
        Page = (await H.Client.get("/materials")).text
        assert "Gold — preview only" in Page and "Gold — available by request" not in Page and "— by request (quoted" not in Page
        assert "<li>Sterling Silver — available to order</li><li>14K Gold over Stainless Steel — preview only (not available to order)</li>" in Page
        assert Page.count('gold-ph" aria-hidden') == 7                            # the room of the seven preview cards (CLS)
        Text = _Description(Page)
        assert Text.endswith("Stainless Steel and Sterling Silver — at a fixed price per piece, and gold as a preview only: appearance, finish, wear and care.") and len(Text) <= 200
        assert "Gold can be previewed in Customize but is not available to order." in Page
        Faq = (await H.Client.get("/faq")).text
        assert ("Fashion jewelry: Stainless Steel, Sterling Silver. Preview only: 14K Gold over Stainless Steel and solid gold in 10K "
                "Yellow, 14K Yellow, 18K Yellow, 10K Rose, 14K Rose, 18K Rose — not available to order.") in Faq
        # A metal whose status differs between the products says so, per product
        assert (await _Statuses(H, [{"product": "ring", "material_id": "vermeil", "status": "available"}])).status_code == 200
        Page = (await H.Client.get("/materials")).text
        assert "<li>14K Gold over Stainless Steel — preview only (not available to order) for charms; available to order for rings</li>" in Page
        assert H.Ctx.Accounts.Consent.Current() == Terms and (await H.Client.get("/terms")).status_code == 200
    finally:
        await H.Close()
