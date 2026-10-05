"""Charms, phase 3 — sizes and pricing. A charm has its own sizes (Admin → Settings → Products) and its own price
book (Admin → Pricing & Materials → Charm): a fixed price per material and size, price and cost per gram. It starts
empty, never falls back to a ring price, and a change to either product's prices never touches the other's."""

import json

import httpx
import pytest

from p3.mail import OrderConfirmationEmail
from tests.conftest import Harness
from tests.test_orders import Address, Customer

AdminKey = "charm-pricing-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}


@pytest.fixture
async def HP(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    assert (await Obj.Client.post("/api/admin/login", json={"key": AdminKey})).status_code == 200   # Admin preview of charms
    yield Obj
    await Obj.Close()


async def CharmCustomize(H, Prompt="A crescent moon charm with a small star"):
    B = await H.NewDesign(Prompt, product="charm")
    C = await H.Proceed(B["design_id"], B["candidates"][0]["id"])
    await H.Idle()
    return B, C


async def RingInBag(H, Size=7, Material="silver"):
    B = await H.NewDesign()
    C = await H.Proceed(B["design_id"], B["candidates"][0]["id"])
    await H.Idle()
    assert (await H.Client.patch(f"/api/customizations/{C['id']}", json={"ring_size": Size, "material_id": Material})).status_code == 200
    assert (await H.Client.post("/api/bag", json={"customization_id": C["id"]})).status_code == 200
    return B, C


async def SetCharmPrices(H, Materials: dict, Note="test prices"):
    R = await H.Client.put("/api/admin/charm-prices", json={"materials": Materials, "note": Note}, headers=Admin)
    assert R.status_code == 200, R.text
    return R.json()


Prices = {"silver": {"fixed_prices": {"20": 145, "25": 165}, "price_per_g": 30, "cost_per_g": 12},
          "stainless_steel": {"fixed_prices": {"20": 90}}}


async def test_charm_prices_start_empty_and_never_fall_back_to_ring_prices(HP):
    H = HP
    T = (await H.Client.get("/api/admin/charm-prices", headers=Admin)).json()
    assert T["version"] == "charms-v1" and T["sizes"] == [15.0, 20.0, 25.0, 30.0]
    assert [R["id"] for R in T["rows"]][:3] == ["stainless_steel", "silver", "vermeil"]
    assert {R["id"]: R["label"] for R in T["rows"]}["silver"] == "Sterling Silver"
    assert {R["id"]: R["label"] for R in T["rows"]}["vermeil"] == "14K Gold Vermeil"
    assert all(R["fixed_prices"] == {} and R["price_per_g"] is None and R["cost_per_g"] is None for R in T["rows"])
    assert [R["fixed_price_allowed"] for R in T["rows"]] == [True, True, True] + [False] * (len(T["rows"]) - 3)
    # The ring price of silver exists — and is never a charm's price
    Ring = (await H.Client.get("/api/quote?material_id=silver")).json()
    assert Ring["unit_price"] == 200.0 and Ring["pricing_status"] == "available"
    Q = (await H.Client.get("/api/quote?material_id=silver&product=charm&charm_size=20")).json()
    assert Q["pricing_status"] == "unavailable" and Q["unit_price"] is None and Q["unavailable_reason"] == "charm_price_not_set"
    assert Q["pricing_version"] == "charms-v1" and Q["product"] == "charm"
    _B, C = await CharmCustomize(H)
    assert C["can_add_to_bag"] is False and C["add_to_bag_blocked_reason"] == "charm_size_required"
    C = (await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": 20})).json()
    assert C["quote"]["unit_price"] is None and C["add_to_bag_blocked_reason"] == "price_unavailable" and C["line_total"] is None
    R = await H.Client.post("/api/bag", json={"customization_id": C["id"]})
    assert R.status_code == 409 and R.json()["error"]["code"] == "price_unavailable"


async def test_charm_prices_differ_by_material_and_size_and_leave_ring_prices_alone(HP):
    H = HP
    RingTable = (await H.Client.get("/api/admin/material-prices", headers=Admin)).json()
    T = await SetCharmPrices(H, Prices)
    assert T["version"] == "charms-v2" and {R["id"]: R["fixed_prices"] for R in T["rows"]}["silver"] == {"20": 145.0, "25": 165.0}
    assert (await H.Client.get("/api/admin/material-prices", headers=Admin)).json() == RingTable      # the ring table untouched

    async def Q(M, S):
        return (await H.Client.get(f"/api/quote?material_id={M}&product=charm&charm_size={S}")).json()
    assert ((await Q("silver", 20))["unit_price"], (await Q("silver", 25))["unit_price"], (await Q("stainless_steel", 20))["unit_price"]) \
        == (145.0, 165.0, 90.0)
    assert (await Q("stainless_steel", 25))["unavailable_reason"] == "charm_price_not_set"
    assert (await Q("vermeil", 20))["unavailable_reason"] == "charm_price_not_set"
    assert (await Q("silver", 22))["unavailable_reason"] == "charm_size_not_offered"
    assert (await Q("gold_14k_yellow", 20))["unavailable_reason"] == "luxury_pricing_unavailable"
    _B, C = await CharmCustomize(H)
    assert [(S["label"], S["unit_price"]) for S in C["charm_sizes"]] == [("15 mm", None), ("20 mm", 145.0), ("25 mm", 165.0), ("30 mm", None)]
    C = (await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": 25, "quantity": 2})).json()
    assert C["quote"]["unit_price"] == 165.0 and C["line_total"] == 330.0 and C["can_add_to_bag"] is True and C["size_label"] == "25 mm"
    # A ring is priced exactly as before
    B = await H.NewDesign()
    R = await H.Proceed(B["design_id"], B["candidates"][0]["id"])
    assert R["quote"]["unit_price"] == 200.0 and R["quote"]["pricing_version"] == RingTable["version"] and "charm_sizes" not in R


async def test_the_charm_price_table_refuses_what_it_cannot_sell(HP):
    H = HP
    for Bad, Text in (({"gold_14k_yellow": {"fixed_prices": {"20": 900}}}, "quoted individually"),
                      ({"silver": {"fixed_prices": {"20": -5}}}, "greater than 0"),
                      ({"silver": {"fixed_prices": {"2": 50}}}, "between 3 and 100 mm"),
                      ({"silver": {"price_per_g": "abc"}}, "must be a number"),
                      ({"platinum": {"fixed_prices": {"20": 50}}}, "is not a charm material")):
        R = await H.Client.put("/api/admin/charm-prices", json={"materials": Bad}, headers=Admin)
        assert R.status_code == 400 and Text in R.json()["error"]["message"], (Bad, R.text)
    assert (await H.Client.get("/api/admin/charm-prices", headers=Admin)).json()["version"] == "charms-v1"   # nothing saved
    # A customer (a browser without the Admin session) cannot read or change charm prices
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=H.App), base_url="http://p3.test",
                                 headers={"X-Access-Token": H.Ctx.Accounts.IssueToken("customer")[0]}) as Customer_:
        assert (await Customer_.put("/api/admin/charm-prices", json={"materials": {}})).status_code == 403
        assert (await Customer_.get("/api/admin/charm-prices")).status_code == 403
    # A partial table keeps every charm material as a row (the ones left out have no prices)
    T = await SetCharmPrices(H, {"silver": {"fixed_prices": {"20": 145}}})
    assert len(T["rows"]) == 9 and {R["id"]: R["fixed_prices"] for R in T["rows"]}["stainless_steel"] == {}


async def test_charm_sizes_are_set_in_admin_and_checked_everywhere(HP):
    H = HP
    await SetCharmPrices(H, Prices)
    R = await H.Client.put("/api/admin/products/charm-sizes", json={"sizes": [30, 15, 20, 25, 35], "note": "a larger size"}, headers=Admin)
    assert R.status_code == 200 and R.json()["charm_sizes"] == [15.0, 20.0, 25.0, 30.0, 35.0]
    assert R.json()["log"][0]["value"] == [15.0, 20.0, 25.0, 30.0, 35.0] and R.json()["log"][0]["note"] == "a larger size"
    for Bad in ([20, 20], [2], [], "20", [150]):
        assert (await H.Client.put("/api/admin/products/charm-sizes", json={"sizes": Bad}, headers=Admin)).status_code == 400, Bad
    assert (await H.Client.get("/api/catalog")).json()["products"]["charm"]["sizes"] == [15.0, 20.0, 25.0, 30.0, 35.0]
    _B, C = await CharmCustomize(H)
    C35 = (await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": 35})).json()
    assert C35["charm_size"] == 35.0 and C35["add_to_bag_blocked_reason"] == "price_unavailable"          # no 35 mm price yet
    Bad = await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": 40})
    assert Bad.status_code == 400 and Bad.json()["error"]["code"] == "invalid_charm_size"
    await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": 20})
    assert (await H.Client.post("/api/bag", json={"customization_id": C["id"]})).status_code == 200
    # Removing a size a bag still holds: the Admin is told, and that line asks for another size
    R = await H.Client.put("/api/admin/products/charm-sizes", json={"sizes": [15, 25, 30, 35]}, headers=Admin)
    assert R.json()["removed_in_bags"] == [{"size": 20.0, "bag_lines": 1}]
    Bag = (await H.Client.get("/api/bag")).json()
    assert Bag["lines"][0]["orderable"] is False and Bag["checkout_available"] is False
    Line = (await H.Client.post("/api/checkout/quote", json={})).json()["lines"][0]
    assert Line["problem"] == "Please choose one of the charm sizes."
    Gone = (await H.Client.get(f"/api/customizations/{C['id']}")).json()
    assert Gone["add_to_bag_blocked_reason"] == "charm_size_not_offered"


async def test_a_charm_customization_has_a_charm_size_and_never_a_ring_size(HP):
    H = HP
    B, C = await CharmCustomize(H)
    assert (C["product_type"], C["ring_size"], C["charm_size"], C["material_id"], C["material_label"]) == \
        ("charm", None, None, "silver", "Sterling Silver")
    assert C["size_definition"]["text"] == "The height of the main charm body, excluding the standard attachment loop."
    assert [M["label"] for M in C["materials"]][:3] == ["Stainless Steel", "Sterling Silver", "14K Gold Vermeil"]
    assert all(not M["purchasable"] for M in C["materials"][3:])                      # gold: the quote flow
    R = await H.Client.patch(f"/api/customizations/{C['id']}", json={"ring_size": 7})
    assert R.status_code == 400 and R.json()["error"]["code"] == "invalid_size"
    Gold = (await H.Client.patch(f"/api/customizations/{C['id']}", json={"material_id": "gold_14k_yellow", "charm_size": 20})).json()
    assert Gold["add_to_bag_blocked_reason"] == "luxury_preview_only"
    # A ring customization never takes a charm size
    RB = await H.NewDesign()
    RC = await H.Proceed(RB["design_id"], RB["candidates"][0]["id"])
    assert (await H.Client.patch(f"/api/customizations/{RC['id']}", json={"charm_size": 20})).status_code == 400
    assert RC["ring_size"] == 10.0 and "product_type" not in RC                      # a ring opens on US 10, as before
    Ev = [json.loads(E["data_json"]) for E in H.Ctx.Db.All("SELECT data_json FROM session_events WHERE kind = 'customize_opened' "
                                                            "AND design_id = ?", (B["design_id"],))]
    assert Ev[0]["product_type"] == "charm" and Ev[0]["charm_size"] is None and "ring_size" not in Ev[0]


async def test_one_bag_and_one_order_hold_a_ring_and_a_charm(HP):
    H = HP
    await SetCharmPrices(H, Prices)
    Ring, _ = await RingInBag(H, 7)
    Charm, C = await CharmCustomize(H)
    await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": 20})
    assert (await H.Client.post("/api/bag", json={"customization_id": C["id"]})).status_code == 200
    Bag = (await H.Client.get("/api/bag")).json()
    assert [(L["product_type"], L["ring_id"], L["size_label"], L["material_label"], L["unit_price"]) for L in Bag["lines"]] == [
        ("ring", "R-1001-A", "US 7", "Silver", 200.0), ("charm", "C-1001-A", "20 mm", "Sterling Silver", 145.0)]
    assert Bag["totals"] == {"USD": 345.0} and Bag["checkout_available"] is True
    O = (await H.Client.post("/api/orders", json={"customer": Customer, "address": Address, "terms_accepted": True,
                                                  "client_request_id": "mixed"})).json()
    assert O["product_types"] == ["ring", "charm"] and O["subtotal"] == 345.0
    assert [L["size_label"] for L in O["lines"]] == ["US 7", "20 mm"]
    Rows = H.Ctx.Db.All("SELECT * FROM order_lines ORDER BY position")
    assert [(R["product_type"], R["ring_size"], R["charm_size"]) for R in Rows] == [("ring", 7.0, None), ("charm", None, 20.0)]
    Snap = json.loads(Rows[1]["purchase_json"])
    assert Snap["product"] == "charm" and Snap["size"]["label"] == "20 mm" and Snap["size"]["unit"] == "mm"
    assert Snap["size"]["definition"].startswith("The height of the main charm body")
    assert Snap["material"] == {"id": "silver", "label": "Sterling Silver"}
    assert Snap["price"] == {"unit_price": 145.0, "currency": "USD", "pricing_version": "charms-v2"}
    assert json.loads(Rows[0]["purchase_json"])["price"]["pricing_version"].startswith("materials-v")
    _Subject, Html = OrderConfirmationEmail(O)
    assert "Ring ID R-1001-A" in Html and "US 7" in Html and "Charm ID C-1001-A" in Html and "20 mm" in Html
    assert "Your pieces are reserved" in Html
    for P in ("ring", "charm"):
        assert [X["ref"] for X in (await H.Client.get(f"/api/admin/orders?product={P}", headers=Admin)).json()["orders"]] == [O["ref"]]
    S = {X["design_id"]: X for X in (await H.Client.get("/api/admin/sessions?include_mock=true", headers=Admin)).json()["sessions"]}
    assert (S[Charm["design_id"]]["charm_size"], S[Charm["design_id"]]["charm_size_chosen"]) == (20.0, True)
    assert S[Charm["design_id"]]["material_label"] == "Sterling Silver" and S[Charm["design_id"]]["ring_size"] is None
    assert (S[Ring["design_id"]]["ring_size"], S[Ring["design_id"]]["ring_size_chosen"]) == (7.0, True)
    assert "charm_size" not in S[Ring["design_id"]]
    Placed = [json.loads(E["data_json"]) for E in H.Ctx.Db.All("SELECT data_json FROM session_events WHERE kind = 'order_placed' ORDER BY id")]
    assert [(E.get("ring_size"), E.get("charm_size"), E.get("product_type")) for E in Placed] == [(7.0, None, None), (None, 20.0, "charm")]


async def test_a_price_change_reprices_only_its_own_product(HP):
    H = HP
    await SetCharmPrices(H, Prices)
    await RingInBag(H, 7)
    _B, C = await CharmCustomize(H)
    await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": 20})
    await H.Client.post("/api/bag", json={"customization_id": C["id"]})

    async def Stale():
        return [(L["product_type"], L["price_is_stale"], L["current_unit_price"]) for L in (await H.Client.get("/api/bag")).json()["lines"]]
    assert await Stale() == [("ring", False, 200.0), ("charm", False, 145.0)]
    # A ring price change: only the ring line is repriced
    Ring = (await H.Client.get("/api/admin/material-prices", headers=Admin)).json()
    Rows = {R["id"]: {K: R[K] for K in ("density_g_cm3", "price_per_g", "cost_per_g", "fixed_price")} for R in Ring["rows"]}
    Rows["silver"]["fixed_price"] = 210
    assert (await H.Client.put("/api/admin/material-prices", json={"materials": Rows}, headers=Admin)).status_code == 200
    assert await Stale() == [("ring", True, 210.0), ("charm", False, 145.0)]
    # A charm price change: only the charm line
    await SetCharmPrices(H, {**Prices, "silver": {**Prices["silver"], "fixed_prices": {"20": 150, "25": 165}}})
    assert await Stale() == [("ring", True, 210.0), ("charm", True, 150.0)]
    Q = (await H.Client.post("/api/checkout/quote", json={})).json()
    assert [(L["unit_price"], L["repriced"]) for L in Q["lines"]] == [(210.0, True), (150.0, True)] and Q["subtotal"] == 360.0


async def test_a_gold_charm_is_quoted_with_its_charm_size(HP):
    H = HP
    B, C = await CharmCustomize(H)
    Body = {"design_id": B["design_id"], "candidate_id": C["candidate_id"], "material_id": "gold_14k_yellow", "quantity": 1,
            "customer": Customer, "charm_size": 25}
    R = await H.Client.post("/api/quote-requests", json=Body)
    assert R.status_code == 200, R.text
    J = R.json()
    assert (J["product_type"], J["charm_size"], J["ring_size"], J["size_label"], J["material_label"]) == \
        ("charm", 25.0, None, "25 mm", "14K Yellow Gold")
    assert (await H.Client.post("/api/quote-requests", json={**Body, "charm_size": 26})).json()["error"]["code"] == "invalid_charm_size"
    Rows = (await H.Client.get("/api/admin/orders", headers=Admin)).json()["quote_requests"]
    assert Rows[0]["size_label"] == "25 mm" and Rows[0]["product_type"] == "charm"
    # A ring request reads as before
    RB = await H.NewDesign()
    RC = await H.Proceed(RB["design_id"], RB["candidates"][0]["id"])
    R = (await H.Client.post("/api/quote-requests", json={**Body, "design_id": RB["design_id"], "candidate_id": RC["candidate_id"],
                                                          "charm_size": None, "ring_size": 7})).json()
    assert (R["product_type"], R["ring_size"], R["size_label"], R["charm_size"]) == ("ring", 7.0, "US 7", None)


async def test_charm_quotes_stay_hidden_from_customers_while_charms_are_hidden(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        R = await H.Client.get("/api/quote?material_id=silver&product=charm&charm_size=20")
        assert R.status_code == 400 and R.json()["error"]["code"] == "unknown_product"
        Ring = (await H.Client.get("/api/quote?material_id=silver&ring_size=7")).json()
        assert Ring["ring_size"] == 7.0 and Ring["unit_price"] == 200.0 and "product" not in Ring
        assert "products" not in (await H.Client.get("/api/catalog")).json()
    finally:
        await H.Close()
