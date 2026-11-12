"""Standard shipping only (owner's P1, 12 Nov 2026): Express is retired for new orders.

Checkout offers one method (no choice to make); a quote or a saved draft asking for Express is priced with Standard; an
order or an approved quote sent with Express is refused with a clear message (never placed at a price the customer did
not see); an order placed with Express before keeps its method, cost and total; the pages and emails customers read
no longer offer Express."""

import pytest

from p3 import mail as Mail
from tests.conftest import Harness
from tests.test_orders import AdminKey, _InBag, _Order

Admin = {"Authorization": f"Bearer {AdminKey}"}


@pytest.fixture
async def H(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    yield Obj
    await Obj.Close()


async def test_checkout_offers_standard_only_and_a_draft_with_express_is_priced_standard(H):
    Info = (await H.Client.get("/api/checkout")).json()
    assert [S["id"] for S in Info["shipping_options"]] == ["standard"]
    await _InBag(H)
    Express = (await H.Client.post("/api/checkout/quote", json={"shipping_method": "express"})).json()
    Plain = (await H.Client.post("/api/checkout/quote", json={})).json()
    assert Express["shipping_method"] == "standard" and Express["shipping"] == 0 and Express["ok"]
    assert Express["total"] == Plain["total"] == Plain["subtotal"]


async def test_an_order_or_api_call_with_express_is_refused_and_nothing_is_placed(H):
    await _InBag(H)
    R = await H.Client.post("/api/orders", json=_Order(shipping_method="express"))
    assert R.status_code == 400
    P = R.json()["error"]["problems"][0]
    assert P["field"] == "shipping_method" and P["code"] == "shipping_method_unavailable"
    assert "Express delivery is no longer available" in P["message"]
    assert (await H.Client.get("/api/orders")).json()["orders"] == []
    Shown = (await H.Client.post("/api/checkout/quote", json={})).json()
    O = (await H.Client.post("/api/orders", json=_Order(expected_total=Shown["total"]))).json()
    assert O["shipping_method"] == "standard" and O["shipping"] == 0 and O["total"] == Shown["total"]
    assert O["shipping_label"] == "Standard delivery"


async def test_an_order_placed_with_express_keeps_its_method_cost_and_total(H):
    await _InBag(H)
    O = (await H.Client.post("/api/orders", json=_Order())).json()
    # an order from before the change: Express, $45, its total including it
    H.Ctx.Db.Execute("UPDATE orders SET shipping_method = 'express', shipping = 45, total = total + 45 WHERE id = ?", (O["id"],))
    Mine = (await H.Client.get(f"/api/orders/{O['id']}")).json()
    assert (Mine["shipping_method"], Mine["shipping"], Mine["shipping_label"], Mine["shipping_eta"]) == \
        ("express", 45, "Express delivery", "about 5 business days")
    assert Mine["total"] == O["total"] + 45
    Ad = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()
    assert Ad["shipping_label"] == "Express delivery" and Ad["total"] == O["total"] + 45


async def test_pages_and_emails_no_longer_offer_express(H):
    for Path in ("/", "/faq", "/shipping-returns", "/terms"):
        Html = (await H.Client.get(Path)).text.lower()
        assert "express delivery" not in Html and "express is $45" not in Html, Path
        assert "about 5 business days" not in Html, Path
    _Subject, Body = Mail.QuoteOfferEmail({"title": "Laurel Twist", "ring_id": "R-1001-A", "material_label": "18K Yellow Gold",
                                           "ref": "Q-5001"},
                                          {"size_label": "US 7", "quantity": 1, "unit_price": 2600, "total": 2600,
                                           "valid_until": "2026-12-01", "message": "", "link": "https://example.test/quote?token=x"})
    assert "Express" not in Body and "standard delivery is free" in Body
