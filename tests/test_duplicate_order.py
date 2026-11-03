"""The duplicate-order guard (Open Tasks row 11; 22 Oct: one checkout run twice, 44 s apart, made ORD-10006 and ORD-10007 —
the same piece, address and total under two different client_request_ids). An order that repeats one of the customer's own
from the last day — the same pieces, metals, sizes and quantities to the same address, not cancelled — is placed only when
the customer confirms it (confirm_duplicate); the same click (client_request_id) still returns its first order."""

import pytest

from tests.conftest import Harness

AdminKey = "duplicate-admin-key"
Customer = {"first_name": "Dana", "last_name": "Levi", "email": "dana@atelier-customer.co.il", "phone": "+972 54 123 4567"}
Address = {"recipient": "Dana Levi", "line1": "12 Rothschild Blvd", "line2": "Apt 4", "city": "Tel Aviv", "region": "",
           "postal_code": "6688112", "country": "IL"}


@pytest.fixture
async def HD(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    yield Obj
    await Obj.Close()


async def _Customized(H, Prompt="Twisted rope band", Size=7, Material="silver", Qty=1) -> str:
    Batch = await H.NewDesign(Prompt, product="ring")
    Cus = (await H.Client.post(f"/api/designs/{Batch['design_id']}/customize", json={"candidate_id": Batch["candidates"][0]["id"]})).json()
    await H.Idle()
    await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"ring_size": Size, "material_id": Material, "quantity": Qty})
    return Cus["id"]


async def _Add(H, CusId: str) -> None:
    R = await H.Client.post("/api/bag", json={"customization_id": CusId})
    assert R.status_code == 200, R.text


def _Order(Click: str, **Over) -> dict:
    return {"customer": Customer, "address": Address, "shipping_method": "standard", "terms_accepted": True,
            "client_request_id": Click, **Over}


def _Count(H) -> int:
    return H.Ctx.Db.One("SELECT COUNT(*) AS n FROM orders")["n"]


async def test_an_order_repeating_one_of_the_day_is_placed_only_when_the_customer_confirms_it(HD):
    H = HD
    Cus = await _Customized(H)
    await _Add(H, Cus)
    First = (await H.Client.post("/api/orders", json=_Order("click-1"))).json()
    assert First["ref"] == "ORD-10001"
    # The same piece again, to the same address (typed with other spacing and case), from a new checkout
    await _Add(H, Cus)
    Same = {**Address, "line1": "12  rothschild blvd", "city": "TEL AVIV"}
    R = await H.Client.post("/api/orders", json=_Order("click-2", address=Same))
    assert R.status_code == 409, R.text
    E = R.json()["error"]
    assert E["code"] == "duplicate_order" and E["existing_order"] == {"id": First["id"], "ref": "ORD-10001"}
    assert "ORD-10001" in E["message"] and "a moment ago" in E["message"] and "second, identical order" in E["message"]
    # Nothing was ordered: one order, and the piece is still in the bag
    assert _Count(H) == 1 and len((await H.Client.get("/api/bag")).json()["lines"]) == 1
    # "Place it again": the customer's say-so places the second order
    R = await H.Client.post("/api/orders", json=_Order("click-2", address=Same, confirm_duplicate=True))
    assert R.status_code == 200, R.text
    assert R.json()["ref"] == "ORD-10002" and _Count(H) == 2 and (await H.Client.get("/api/bag")).json()["lines"] == []


async def test_the_same_click_still_returns_its_first_order(HD):
    H = HD
    await _Add(H, await _Customized(H))
    First = (await H.Client.post("/api/orders", json=_Order("click-1"))).json()
    Again = await H.Client.post("/api/orders", json=_Order("click-1"))
    assert Again.status_code == 200 and Again.json()["id"] == First["id"] and _Count(H) == 1


async def test_another_address_or_another_piece_is_not_a_repeat(HD):
    H = HD
    Cus = await _Customized(H)
    await _Add(H, Cus)
    assert (await H.Client.post("/api/orders", json=_Order("click-1"))).status_code == 200
    # The same piece to another address: a gift, say
    await _Add(H, Cus)
    Gift = {**Address, "recipient": "Noa Levi", "line1": "3 Herzl St", "postal_code": "6578901"}
    assert (await H.Client.post("/api/orders", json=_Order("click-2", address=Gift))).status_code == 200
    # Another piece to the first address
    await _Add(H, await _Customized(H, "Signet with hexagon face", Size=10, Material="vermeil"))
    assert (await H.Client.post("/api/orders", json=_Order("click-3"))).status_code == 200
    # The same piece in another quantity is another order too
    await H.Client.patch(f"/api/customizations/{Cus}", json={"quantity": 2})
    await _Add(H, Cus)
    assert (await H.Client.post("/api/orders", json=_Order("click-4"))).status_code == 200
    assert _Count(H) == 4


async def test_a_cancelled_order_or_one_older_than_a_day_is_not_repeated(HD):
    H = HD
    Cus = await _Customized(H)
    await _Add(H, Cus)
    First = (await H.Client.post("/api/orders", json=_Order("click-1"))).json()
    H.Ctx.Db.Execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (First["id"],))
    await _Add(H, Cus)
    Second = await H.Client.post("/api/orders", json=_Order("click-2"))
    assert Second.status_code == 200, Second.text
    # An identical order placed more than a day ago is not a repeat either
    H.Ctx.Db.Execute("UPDATE orders SET created_at = '2026-01-01T00:00:00.000+00:00' WHERE id = ?", (Second.json()["id"],))
    await _Add(H, Cus)
    assert (await H.Client.post("/api/orders", json=_Order("click-3"))).status_code == 200
    assert _Count(H) == 3


def test_the_checkout_page_offers_view_that_order_and_place_it_again():
    from p3.settings import WebDir
    Page = (WebDir / "index.html").read_text(encoding="utf-8")
    Script = (WebDir / "app.js").read_text(encoding="utf-8")
    Notice = Page[Page.index('data-testid="co-duplicate"') - 400:Page.index('data-testid="co-duplicate"') + 900]
    assert 'role="alert"' in Notice and "View that order" in Notice and "Place it again" in Notice
    assert "placeOrder({ confirmDuplicate: true })" in Notice and "viewEarlierOrder()" in Notice
    assert "confirm_duplicate: true" in Script and "ex.existingOrder = err.existing_order" in Script
    assert "e.code === 'duplicate_order'" in Script
