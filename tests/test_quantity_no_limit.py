"""Owner's approved plan (14 Nov 2026), Task 3: no purchase limit of 10 per piece. Customize, the bag, checkout and the
order take any whole quantity of at least 1; prices, the promotion, a promo code, VAT and the order's totals follow the
quantity exactly; only a technical guard (Products.MaxQuantity) stops nonsense values. Mock provider only."""

from decimal import Decimal

import pytest

from p3 import products as Products
from tests.conftest import Harness
from tests.test_b6_ils_pricing import Admin as IlsAdmin, AdminKey as IlsAdminKey, Israeli, _Order, _TurnOn
from tests.test_charm_promotion import HC, Promotion  # noqa: F401  (HC is a fixture)
from tests.test_charm_pricing import CharmCustomize


@pytest.fixture
async def H(tmp_path):
    Obj = Harness(tmp_path, AdminKey=IlsAdminKey)
    yield Obj
    await Obj.Close()


async def _RingCustomized(H, Qty):
    B = await H.NewDesign("Twisted rope band", product="ring")
    C = await H.Proceed(B["design_id"], B["candidates"][0]["id"])
    await H.Idle()
    R = await H.Client.patch(f"/api/customizations/{C['id']}", json={"ring_size": 7, "material_id": "silver", "quantity": Qty})
    assert R.status_code == 200, R.text
    assert R.json()["quantity"] == Qty
    return C["id"]


def test_the_guard_is_technical_not_a_purchase_limit():
    assert Products.MaxQuantity >= 1000
    assert Products.CheckQuantity(11) == 11 and Products.CheckQuantity(250) == 250


async def test_customize_bag_checkout_and_order_take_more_than_ten(H):
    Cid = await _RingCustomized(H, 25)
    R = await H.Client.post("/api/bag", json={"customization_id": Cid, "client_request_id": "q-1"})
    assert R.status_code == 200, R.text
    Line = R.json()["lines"][0]
    assert Line["quantity"] == 25 and not R.json()["added"]["limited"]
    Unit = Line["unit_price"]
    assert Line["line_total"] == pytest.approx(25 * Unit)
    # the same piece again: one line, 50
    R = (await H.Client.post("/api/bag", json={"customization_id": Cid, "client_request_id": "q-2"})).json()
    assert [L["quantity"] for L in R["lines"]] == [50] and not R["added"]["limited"]
    # the bag's stepper: any quantity of at least 1
    R = await H.Client.patch(f"/api/bag/{Line['id']}", json={"quantity": 37})
    assert R.status_code == 200, R.text
    Bag = R.json()
    assert Bag["lines"][0]["quantity"] == 37 and Bag["lines"][0]["line_total"] == pytest.approx(37 * Unit)
    # checkout: the quote and the order carry 37, and the totals agree
    Q = (await H.Client.post("/api/checkout/quote", json={})).json()
    assert Q["subtotal"] == pytest.approx(37 * Unit)
    O = await H.Client.post("/api/orders", json=_Order(expected_total=Q["total"]))
    assert O.status_code == 200, O.text
    O = O.json()
    assert O["total"] == Q["total"] and [L["quantity"] for L in O["lines"]] == [37]
    Row = H.Ctx.Db.One("SELECT quantity, unit_price FROM order_lines WHERE order_id = ?", (O["id"],))
    assert Row["quantity"] == 37 and Row["unit_price"] == Unit


async def test_invalid_quantities_are_still_refused(H):
    Cid = await _RingCustomized(H, 3)
    for Bad in (0, -2, "12", 2.5, True, Products.MaxQuantity + 1):
        R = await H.Client.patch(f"/api/customizations/{Cid}", json={"quantity": Bad})
        assert R.status_code == 400 and R.json()["error"]["code"] == "invalid_quantity", (Bad, R.text)
    R = await H.Client.patch(f"/api/customizations/{Cid}", json={"quantity": Products.MaxQuantity + 1})
    assert "contact us" in R.json()["error"]["message"]
    Line = (await H.Client.post("/api/bag", json={"customization_id": Cid})).json()["lines"][0]
    for Bad in (0, "4", Products.MaxQuantity + 1):
        R = await H.Client.patch(f"/api/bag/{Line['id']}", json={"quantity": Bad})
        assert R.status_code == 400 and R.json()["error"]["code"] == "invalid_quantity", (Bad, R.text)


async def test_shekels_vat_and_a_percent_code_follow_a_large_quantity(H):
    await _TurnOn(H)
    R = await H.Client.post("/api/admin/promo-codes", headers=IlsAdmin, json={"code": "TEN", "kind": "percent", "value": 10, "active": True})
    assert R.status_code == 200, R.text
    Cid = await _RingCustomized(H, 12)
    assert (await H.Client.post("/api/bag", json={"customization_id": Cid}, headers=Israeli)).status_code == 200
    Bag = (await H.Client.get("/api/bag", headers=Israeli)).json()
    L = Bag["lines"][0]
    assert L["quantity"] == 12 and L["currency"] == "ILS"
    Q = (await H.Client.post("/api/checkout/quote", json={"country": "IL", "promo_code": "TEN"}, headers=Israeli)).json()
    assert Q["currency"] == "ILS" and Q["subtotal"] == pytest.approx(12 * L["unit_price"])
    assert Q["discount"] == round(Q["subtotal"] * 0.10, 2) and Q["total"] == round(Q["subtotal"] - Q["discount"], 2)
    assert Q["vat_amount"] > 0
    # 18 % VAT inside the total: total = net + VAT
    assert Decimal(str(Q["total"])) == pytest.approx(Decimal(str(Q["vat_amount"])) * Decimal("118") / Decimal("18"), abs=Decimal("0.05"))
    O = await H.Client.post("/api/orders", json=_Order(expected_total=Q["total"], promo_code="TEN"), headers=Israeli)
    assert O.status_code == 200, O.text
    O = O.json()
    assert O["total"] == Q["total"] and O["vat_amount"] == Q["vat_amount"] and O["lines"][0]["quantity"] == 12


async def test_the_promotional_price_applies_to_every_piece_of_a_large_quantity(HC):
    H = HC
    await Promotion(H)                                 # the promotional price is charged (reference prices not needed)
    B, C = await CharmCustomize(H, Material="stainless_steel")
    R = await H.Client.patch(f"/api/customizations/{C['id']}", json={"quantity": 15})
    assert R.status_code == 200, R.text
    Bag = (await H.Client.post("/api/bag", json={"customization_id": C["id"]})).json()
    L = Bag["lines"][0]
    assert L["quantity"] == 15 and L["unit_price"] == 39.0 and L["line_total"] == pytest.approx(15 * 39.0)
    Q = (await H.Client.post("/api/checkout/quote", json={})).json()
    assert Q["subtotal"] == pytest.approx(15 * 39.0)
