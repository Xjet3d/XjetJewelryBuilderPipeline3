"""AJW-51: an Admin deletes an order or a quote request — an archive, never an erase. The exact Order / Quote ID is the
confirmation, a reason is kept, one transaction writes the flag, the history and the audit entry; a deleted record leaves
the lists, the figures, the customer's My Account and the emailed quote link, and Restore brings it back unchanged."""

import re

import pytest

from tests.conftest import Harness
from tests.test_quotes import Address, Admin, AdminKey, Customer, _Link, _Offer, _Request

OrderBody = {"customer": Customer, "address": Address, "shipping_method": "standard", "terms_accepted": True}


@pytest.fixture
async def HD(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    yield Obj
    await Obj.Close()


async def _Order(H, Click="c1", Prompt="Twisted rope band") -> dict:
    B = await H.NewDesign(Prompt, product="ring")
    Cus = (await H.Client.post(f"/api/designs/{B['design_id']}/customize", json={"candidate_id": B["candidates"][0]["id"]})).json()
    await H.Idle()
    await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"ring_size": 7, "material_id": "silver", "quantity": 1})
    assert (await H.Client.post("/api/bag", json={"customization_id": Cus["id"]})).status_code == 200
    R = await H.Client.post("/api/orders", json={**OrderBody, "client_request_id": Click})
    assert R.status_code == 200, R.text
    return R.json()


def _Del(H, Kind, Id, **Over):
    return H.Client.post(f"/api/admin/{Kind}/{Id}/delete", json=Over, headers=Admin)


async def test_deleting_an_order_needs_its_exact_id_and_a_reason_and_is_audited(HD):
    H = HD
    O = await _Order(H)
    assert (await _Del(H, "orders", O["id"], confirm_ref="ORD-99999", reason="test data")).status_code == 400   # wrong record
    assert (await _Del(H, "orders", O["id"], confirm_ref=O["ref"], reason="")).status_code == 400              # no reason
    assert H.Ctx.Db.One("SELECT deleted_at FROM orders WHERE id = ?", (O["id"],))["deleted_at"] is None         # nothing changed
    R = await _Del(H, "orders", O["id"], confirm_ref=O["ref"], reason="QA order placed by mistake")
    assert R.status_code == 200, R.text
    D = R.json()["deleted"]
    assert D["by"] == "developer-key" and D["reason"] == "QA order placed by mistake" and D["at"]
    Audit = [A["action"] for A in H.Ctx.Db.All("SELECT * FROM admin_audit")]
    assert any(A.startswith(f"delete order {O['ref']} ({O['id']}): QA order") for A in Audit)
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM order_events WHERE order_id = ? AND kind = 'deleted'", (O["id"],))["n"] == 1
    # the second click (double submit) and the changed record are refused, the data stays as it is
    again = await _Del(H, "orders", O["id"], confirm_ref=O["ref"], reason="QA order placed by mistake")
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_deleted"
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM order_events WHERE order_id = ? AND kind = 'deleted'", (O["id"],))["n"] == 1
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM order_lines WHERE order_id = ?", (O["id"],))["n"] == 1    # related rows stay


async def test_a_deleted_order_leaves_the_lists_and_the_customer_and_restore_brings_it_back(HD):
    H = HD
    O = await _Order(H)
    assert (await H.Client.get("/api/admin/orders", headers=Admin)).json()["total"] == 1
    assert (await H.Client.get(f"/api/orders/{O['id']}")).status_code == 200
    await _Del(H, "orders", O["id"], confirm_ref=O["ref"], reason="duplicate test order")
    L = (await H.Client.get("/api/admin/orders", headers=Admin)).json()
    assert L["total"] == 0 and L["orders"] == []
    Shown = (await H.Client.get("/api/admin/orders?deleted=true", headers=Admin)).json()
    assert Shown["total"] == 1 and Shown["orders"][0]["deleted"]["reason"] == "duplicate test order"
    assert (await H.Client.get(f"/api/orders/{O['id']}")).status_code == 404                                  # the customer's page
    assert (await H.Client.get("/api/admin/dashboard", headers=Admin)).json()["orders"]["orders"] == 0
    assert (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()["deleted"]["by"]       # the Admin can still open it
    Blocked = await H.Client.post(f"/api/admin/orders/{O['id']}/note", json={"note": "hello"}, headers=Admin)
    assert Blocked.status_code == 409 and Blocked.json()["error"]["code"] == "order_deleted"
    R = await H.Client.post(f"/api/admin/orders/{O['id']}/restore", headers=Admin)
    assert R.status_code == 200 and R.json()["deleted"] is None
    assert (await H.Client.get("/api/admin/orders", headers=Admin)).json()["total"] == 1
    assert (await H.Client.get(f"/api/orders/{O['id']}")).status_code == 200
    assert (await H.Client.post(f"/api/admin/orders/{O['id']}/restore", headers=Admin)).status_code == 409   # not deleted
    Audit = [A["action"] for A in H.Ctx.Db.All("SELECT * FROM admin_audit")]
    assert any(A.startswith(f"restore order {O['ref']}") for A in Audit)


async def test_a_paid_order_and_an_enrolled_one_are_not_deleted(HD):
    H = HD
    O = await _Order(H)
    await H.Client.post(f"/api/admin/orders/{O['id']}/payment", json={"status": "paid", "note": "bank transfer"}, headers=Admin)
    R = await _Del(H, "orders", O["id"], confirm_ref=O["ref"], reason="should not work")
    assert R.status_code == 409 and R.json()["error"]["code"] == "order_paid"
    assert H.Ctx.Db.One("SELECT deleted_at FROM orders WHERE id = ?", (O["id"],))["deleted_at"] is None


async def test_deleting_needs_an_admin(HD):
    H = HD
    O = await _Order(H)
    for Headers in ({}, {"Authorization": "Bearer not-the-key"}):
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/delete", json={"confirm_ref": O["ref"], "reason": "no right"}, headers=Headers)
        assert R.status_code in (401, 403), R.text
    # the customer's own token is not an Admin's
    R = await H.Client.post(f"/api/admin/orders/{O['id']}/delete", json={"confirm_ref": O["ref"], "reason": "no right"})
    assert R.status_code in (401, 403)
    assert H.Ctx.Db.One("SELECT deleted_at FROM orders WHERE id = ?", (O["id"],))["deleted_at"] is None


async def test_a_deleted_quote_request_loses_its_link_and_leaves_the_inbox(HD):
    H = HD
    _B, Q = await _Request(H)
    assert (await _Offer(H, Q)).status_code == 200
    Link = _Link(H)
    assert (await H.Client.get(Link)).status_code == 200
    assert (await _Del(H, "quote-requests", Q["id"], confirm_ref="Q-1", reason="a wrong one")).status_code == 400
    R = await _Del(H, "quote-requests", Q["id"], confirm_ref=Q["ref"], reason="test quote request")
    assert R.status_code == 200 and R.json()["deleted"]["by"] == "developer-key"
    assert (await H.Client.get(Link)).status_code == 404                                                      # the emailed link is dead
    Token = re.search(r"token=([^&]+)", Link).group(1)
    assert (await H.Client.post("/quote/approve", data={"token": Token, **Address, "shipping_method": "standard", "terms": "1"})).status_code == 404
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM orders")["n"] == 0                                          # no order from it
    Inbox = (await H.Client.get("/api/admin/quote-requests", headers=Admin)).json()
    assert Inbox["total"] == 0 and sum(Inbox["counts"].values()) == 0
    assert (await H.Client.get("/api/admin/quote-requests?deleted=true", headers=Admin)).json()["total"] == 1
    Send = await _Offer(H, Q)
    assert Send.status_code == 409 and Send.json()["error"]["code"] == "quote_deleted"                        # no mail for a deleted one
    assert (await H.Client.post(f"/api/admin/quote-requests/{Q['id']}/restore", headers=Admin)).json().get("deleted") is None
    assert (await H.Client.get(Link)).status_code == 200
    assert (await H.Client.get("/api/admin/quote-requests", headers=Admin)).json()["total"] == 1


async def test_a_quote_with_a_live_order_waits_for_the_order(HD):
    H = HD
    _B, Q = await _Request(H)
    await _Offer(H, Q)
    Token = re.search(r"token=([^&]+)", _Link(H)).group(1)
    R = await H.Client.post("/quote/approve", data={"token": Token, **Address, "shipping_method": "standard", "terms": "1"})
    assert R.status_code == 200, R.text
    Order = (await H.Client.get("/api/admin/orders", headers=Admin)).json()["orders"][0]
    Blocked = await _Del(H, "quote-requests", Q["id"], confirm_ref=Q["ref"], reason="remove the quote")
    assert Blocked.status_code == 409 and Blocked.json()["error"]["code"] == "quote_has_order"
    assert (await _Del(H, "orders", Order["id"], confirm_ref=Order["ref"], reason="remove the order")).status_code == 200
    assert (await _Del(H, "quote-requests", Q["id"], confirm_ref=Q["ref"], reason="remove the quote")).status_code == 200
    # nothing is orphaned: both rows still exist and still point at each other
    Row = H.Ctx.Db.One("SELECT order_id FROM quote_requests WHERE id = ?", (Q["id"],))
    assert Row["order_id"] == Order["id"] and H.Ctx.Db.One("SELECT id FROM orders WHERE id = ?", (Order["id"],))


async def test_an_order_enrolled_in_fulfillment_is_not_deleted(HD):
    H = HD
    O = await _Order(H)
    Tables = {R["name"] for R in H.Ctx.Db.All("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "fulfillment_enrollments" not in Tables:
        pytest.skip("Fulfillment tables are not installed in this configuration")
    H.Ctx.Db.Execute("INSERT INTO fulfillment_enrollments (id, entity_type, entity_id, display_ref, state, source_version, enrolled_at, "
                     "enrolled_by, updated_at) VALUES ('en1', 'order', ?, ?, 'enrolled', 1, '2026-11-12T00:00:00+00:00', 'test', '2026-11-12T00:00:00+00:00')",
                     (O["id"], O["ref"]))
    R = await _Del(H, "orders", O["id"], confirm_ref=O["ref"], reason="must be refused")
    assert R.status_code == 409 and R.json()["error"]["code"] == "fulfillment_hold"
    assert H.Ctx.Db.One("SELECT deleted_at FROM orders WHERE id = ?", (O["id"],))["deleted_at"] is None
