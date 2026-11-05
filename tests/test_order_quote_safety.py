"""Order and quote safety (WP1, 1 Nov 2026). A quote never turns a product switched off for customers, or a material with a
fixed price, into an order: the Admin's Send, the customer's quote page, Approve and the order made from a quote all
refuse it (H1, M4); a quote request is accepted only for a quoted material (H3). An Admin's preview of a hidden product
is for testing only: on a test account (an address at a reserved test domain) it works and its orders are Test orders;
on a customer's own account every step that makes, bags, orders or quotes it is refused before any credit or AI call
(H2). A Test order reaches Production only where P3_FULFILLMENT_TEST_ORDERS allows it (proto), never on Atelier. The preview
marker shows on phones too and says when a product is paused rather than hidden (L6)."""

import json

import pytest

from p3.context import HttpError
from p3.fulfillment_bridge import TestOrderReason
from p3.settings import WebDir
from tests.conftest import Harness
from tests.test_charm_foundation import CharmDesign
from tests.test_fulfillment_bridge import Admin as BridgeAdmin, _On
from tests.test_orders import _InBag, _Order
from tests.test_quotes import Address, Admin, AdminKey, Customer, _Link, _Offer, _Request

Preview = "preview_test_account_only"


async def _Set(H, Product, On):
    R = await H.Client.put("/api/admin/products/availability", json={"product": Product, "available": On}, headers=Admin)
    assert R.status_code == 200, R.text


def _Count(H, Table):
    return H.Ctx.Db.One(f"SELECT COUNT(*) AS n FROM {Table}")["n"]


def _Credits(H):
    P = H.Ctx.Accounts.Profile(H.Who)
    return P["used"], P["remaining"]


async def test_a_quote_for_a_switched_off_product_is_never_sent_approved_or_ordered(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        _B, Q = await _Request(H)                                            # a gold ring: quoted, as today
        assert (await _Offer(H, Q)).status_code == 200
        Link = _Link(H)
        Token = Link.split("token=")[1]
        Approve = {"token": Token, **Address, "shipping_method": "standard", "terms": "1"}
        await _Set(H, "ring", False)                                         # rings switched off for customers
        D = (await H.Client.get(f"/api/admin/quote-requests/{Q['id']}", headers=Admin)).json()
        assert D["offer_problem"] == "Rings are not on offer to customers (Admin → Settings → Products)." and D["can_send"]
        R = await _Offer(H, Q, unit_price=2500)                              # no new or revised quote
        assert R.status_code == 409 and R.json()["error"]["code"] == "quote_not_offerable", R.text
        Page = (await H.Client.get(Link)).text
        assert "can't be approved online" in Page and "Approve the quote" not in Page and "Q-5001" in Page
        R = await H.Client.post("/quote/approve", data=Approve)              # the quote sent before the switch: not approved
        assert "can't be approved online" in R.text and _Count(H, "orders") == 0
        Row = H.Ctx.Db.One("SELECT * FROM quote_requests WHERE id = ?", (Q["id"],))
        with pytest.raises(HttpError) as E:                                  # the last guard: the order made from a quote
            H.Svc.Orders.CreateFromQuote(Row, json.loads(Row["offer_json"]), Address, "standard")
        assert E.value.Code == "quote_not_orderable" and _Count(H, "orders") == 0
        assert Row["status"] == "quoted" and json.loads(Row["offer_json"])["version"] == 1      # the request and its offer kept
        await _Set(H, "ring", True)                                          # back on offer: the same link works again
        R = await H.Client.post("/quote/approve", data=Approve)
        assert R.status_code == 200 and "your order is placed" in R.text and _Count(H, "orders") == 1
    finally:
        await H.Close()


async def test_a_fixed_price_material_is_never_quoted(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        B = await H.NewDesign("A signet ring with a crescent")
        Body = {"design_id": B["design_id"], "candidate_id": B["candidates"][0]["id"], "ring_size": 7, "quantity": 1, "customer": Customer}
        for Material in ("silver", "stainless_steel", "vermeil"):            # only the browser hid the button before
            R = await H.Client.post("/api/quote-requests", json={**Body, "material_id": Material})
            assert R.status_code == 409 and R.json()["error"]["code"] == "not_quotable", (Material, R.text)
            assert "has a fixed price: it is ordered from Customize, not quoted." in R.json()["error"]["message"]
        assert _Count(H, "quote_requests") == 0
        assert (await H.Client.post("/api/quote-requests", json={**Body, "material_id": "gold_14k_rose"})).status_code == 200
        # A request in a fixed-price material made before this check gets no quote, below or above the price table (M4)
        _B, Q = await _Request(H)
        H.Ctx.Db.Execute("UPDATE quote_requests SET material_id = 'silver', material_label = 'Sterling Silver' WHERE id = ?", (Q["id"],))
        D = (await H.Client.get(f"/api/admin/quote-requests/{Q['id']}", headers=Admin)).json()
        assert D["offer_problem"].endswith("has a fixed price: it is ordered from Customize, not quoted.")
        R = await _Offer(H, Q, unit_price=120)
        assert R.status_code == 409 and R.json()["error"]["code"] == "quote_not_offerable", R.text
        assert '!!qd.offer_problem' in (WebDir / "admin.html").read_text(encoding="utf-8")       # Send disabled, the reason shown
    finally:
        await H.Close()


async def test_an_admin_preview_on_a_customer_account_makes_nothing_and_spends_nothing(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await H.Client.put("/api/admin/charm-prices", json={"materials": {"silver": {"fixed_prices": {"14": 109}}}}, headers=Admin)
        Ring = await H.NewDesign()
        Asset = Ring["candidates"][0]["image_url"].split("/assets/", 1)[1]
        Cid, Cands = CharmDesign(H, Asset)                                   # a charm design on this customer's own account
        Item = (await H.Client.post("/api/admin/gallery", json={"design_id": Cid}, headers=Admin)).json()
        # While charms were on offer, the customer took it to the bag; charms are now paused for customers
        await _Set(H, "charm", True)
        assert (await H.Client.put(f"/api/designs/{Cid}/selection", json={"candidate_id": Cands[0]})).status_code == 200
        C = (await H.Client.post(f"/api/designs/{Cid}/customize", json={"candidate_id": Cands[0]})).json()
        await H.Idle()
        assert (await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": 14, "material_id": "silver"})).status_code == 200
        assert (await H.Client.post("/api/bag", json={"customization_id": C["id"]})).json()["lines"][0]["orderable"] is True
        await _Set(H, "charm", False)
        assert (await H.Client.post("/api/admin/login", json={"key": AdminKey})).status_code == 200   # this browser previews charms
        H.Client.headers["X-P3-Preview"] = "1"                              # this tab opened the site in preview
        assert (await H.Client.get("/api/catalog")).json()["products"]["previewed"] == ["charm"]
        Before, Designs = _Credits(H), _Count(H, "designs")
        Attempts = {
            "new design": await H.Client.post("/api/designs", data={"prompt": "A star charm", "product": "charm"}),
            "refinement": await H.Client.post(f"/api/designs/{Cid}/batches", json={"instruction": "Thinner", "parent_candidate_id": Cands[0]}),
            "another option": await H.Client.post(f"/api/candidates/{Cands[1]}/retry"),
            "movie": await H.Client.post(f"/api/candidates/{Cands[0]}/movie"),
            "selection": await H.Client.put(f"/api/designs/{Cid}/selection", json={"candidate_id": Cands[1]}),
            "customize": await H.Client.post(f"/api/designs/{Cid}/customize", json={"candidate_id": Cands[1]}),
            "customization": await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": 10}),
            "bag": await H.Client.post("/api/bag", json={"customization_id": C["id"]}),
            "make it yours": await H.Client.post(f"/api/gallery/{Item['id']}/start", json={}),
            "quote request": await H.Client.post("/api/quote-requests", json={"design_id": Cid, "candidate_id": Cands[0],
                                                                               "material_id": "gold_14k_yellow", "charm_size": 14,
                                                                               "customer": Customer}),
        }
        for What, R in Attempts.items():
            assert R.status_code == 409 and R.json()["error"]["code"] == Preview, (What, R.status_code, R.text[:200])
        Bag = (await H.Client.get("/api/bag")).json()                       # the charm line is kept, not orderable
        assert [L["orderable"] for L in Bag["lines"]] == [False] and Bag["checkout_available"] is False
        R = await H.Client.post("/api/orders", json=_Order(customer={**Customer, "email": "qa@example.com"}))
        assert R.status_code == 409 and R.json()["error"]["code"] == "bag_not_orderable", R.text
        assert _Credits(H) == Before and _Count(H, "designs") == Designs
        assert _Count(H, "orders") == 0 and _Count(H, "quote_requests") == 0
        # Rings, on offer to customers, work as always in the same browser
        assert (await H.Client.post("/api/designs", data={"prompt": "A slim band"})).status_code == 200
    finally:
        await H.Close()


async def test_an_admin_preview_on_a_test_account_works_and_its_orders_are_test_orders(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await H.Client.put("/api/admin/charm-prices", json={"materials": {"silver": {"fixed_prices": {"14": 109}}}}, headers=Admin)
        Qa = {"X-Access-Token": H.Ctx.Accounts.IssueToken("qa", Email="qa@example.com", MaxGenerations=100)[0]}
        assert (await H.Client.post("/api/admin/login", json={"key": AdminKey})).status_code == 200
        H.Client.headers["X-P3-Preview"] = "1"                              # this tab opened the site in preview
        R = await H.Client.post("/api/designs", data={"prompt": "A star charm", "product": "charm"}, headers=Qa)
        assert R.status_code == 200, R.text
        await H.Idle()
        B = (await H.Client.get(f"/api/batches/{R.json()['id']}", headers=Qa)).json()
        Did, Cand = B["design_id"], B["candidates"][0]["id"]
        assert (await H.Client.put(f"/api/designs/{Did}/selection", json={"candidate_id": Cand}, headers=Qa)).status_code == 200
        C = (await H.Client.post(f"/api/designs/{Did}/customize", json={"candidate_id": Cand}, headers=Qa)).json()
        await H.Idle()
        assert (await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": 14, "material_id": "silver"}, headers=Qa)).status_code == 200
        Bag = (await H.Client.post("/api/bag", json={"customization_id": C["id"]}, headers=Qa)).json()
        assert [L["orderable"] for L in Bag["lines"]] == [True]
        R = await H.Client.post("/api/orders", json=_Order(), headers=Qa)    # a customer's address: never a real order
        assert R.status_code == 409 and R.json()["error"]["code"] == "preview_test_address", R.text
        R = await H.Client.post("/api/quote-requests", json={"design_id": Did, "candidate_id": Cand, "material_id": "gold_14k_yellow",
                                                             "charm_size": 14, "customer": Customer}, headers=Qa)
        assert R.status_code == 409 and R.json()["error"]["code"] == "preview_test_address", R.text
        assert _Count(H, "orders") == 0 and _Count(H, "quote_requests") == 0
        R = await H.Client.post("/api/orders", json=_Order(customer={**Customer, "email": "qa@example.com"}), headers=Qa)
        assert R.status_code == 200, R.text
        Ad = (await H.Client.get(f"/api/admin/orders/{R.json()['ref']}", headers=Admin)).json()
        assert Ad["test"] is True                                            # a Test order: out of the figures, no customer email
    finally:
        await H.Close()


async def test_a_test_order_reaches_production_only_where_the_setting_allows_it(tmp_path):
    """Off by default (the live site, Atelier): a Test order is never handed to Production, and the Admin's order page says
    so. On (a staging copy such as proto, P3_FULFILLMENT_TEST_ORDERS=true): the Fulfillment workflow is tested with
    isolated Test orders. A real order is handed over either way."""
    for Allowed in (False, True):
        (tmp_path / f"t{Allowed}").mkdir()
        H = _On(tmp_path / f"t{Allowed}", **({"FulfillmentTestOrders": True} if Allowed else {}))
        try:
            await _InBag(H, "Twisted rope band", Size=7, Material="silver", Qty=1)
            R = await H.Client.post("/api/orders", json=_Order(client_request_id="t-1", customer={**Customer, "email": "dana@example.com"}))
            assert R.status_code == 200, R.text
            O = R.json()
            Detail = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=BridgeAdmin)).json()
            assert Detail["test"] is True and Detail["test_orders_to_production"] is Allowed
            State = (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=BridgeAdmin)).json()
            if Allowed:
                assert State["lines"][0]["handover"]["state"] == "allowed"
                continue
            assert State["eligible"] is False and State["ineligible_reason"] == TestOrderReason
            assert State["lines"][0]["handover"]["state"] == "test_order"
            R = await H.Client.post(f"/api/admin/orders/{O['id']}/production", json={"line_ids": [O["lines"][0]["id"]], "note": ""},
                                    headers=BridgeAdmin)
            assert R.status_code == 409 and R.json()["error"]["code"] == "not_eligible" and TestOrderReason in R.json()["error"]["message"]
            assert _Count(H, "fulfillment_line_production") == 0
            await _InBag(H, "Twisted rope band", Size=8, Material="silver", Qty=1)                  # a real order: handed over
            R = await H.Client.post("/api/orders", json=_Order(client_request_id="r-1"))
            assert R.status_code == 200, R.text
            Real = (await H.Client.get(f"/api/admin/orders/{R.json()['id']}/fulfillment", headers=BridgeAdmin)).json()
            assert Real["lines"][0]["handover"]["state"] == "allowed"
        finally:
            await H.Close()
    Html, Js = (WebDir / "admin.html").read_text(encoding="utf-8"), (WebDir / "admin.js").read_text(encoding="utf-8")
    assert "od.test_orders_to_production ?" in Html and "Test orders are never sent to Production on this site" in Js


async def test_the_preview_marker_says_paused_or_hidden_and_shows_on_phones(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        assert (await H.Client.post("/api/admin/login", json={"key": AdminKey})).status_code == 200
        H.Client.headers["X-P3-Preview"] = "1"                              # this tab opened the site in preview
        P = (await H.Client.get("/api/catalog")).json()["products"]
        assert P["previewed"] == ["charm"] and P["paused"] == []             # never on offer: hidden
        await _Set(H, "charm", True)
        await _Set(H, "charm", False)
        P = (await H.Client.get("/api/catalog")).json()["products"]
        assert P["previewed"] == ["charm"] and P["paused"] == ["charm"]      # on offer before: paused
        Page = (WebDir / "index.html").read_text(encoding="utf-8")
        Badge = Page.split("<!-- ── Admin preview:", 1)[1].split("</template>", 1)[0]
        assert 'class="flex fixed' in Badge and "sm:flex" not in Badge       # on phones too
        Js = (WebDir / "app.js").read_text(encoding="utf-8")
        assert "' off for customers'" in Js and "' hidden from customers'" in Js
    finally:
        await H.Close()
