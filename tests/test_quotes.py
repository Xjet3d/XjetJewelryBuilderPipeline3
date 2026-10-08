"""Quote requests: the Admin's workspace (every detail, where the request went, a price suggested from the 3D model,
editable numbers, a suggested reply, notes), the quote email with Approve / Decline, the customer's page, an approved
quote becoming an order, a declined one marked rejected, revisions and expiry, and the history of it all."""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from p3.geometry import Scaled, UsSizeToInnerDiameterMm
from tests.conftest import Harness

AdminKey = "quotes-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Customer = {"first_name": "Dana", "last_name": "Levi", "email": "dana@example.com", "phone": "+972 54 123 4567"}
Address = {"recipient": "Dana Levi", "line1": "12 Rothschild Blvd", "line2": "", "city": "Tel Aviv", "region": "",
           "postal_code": "6688112", "country": "IL"}


def _Days(N: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=N)).isoformat()


async def _Request(H, Size=7, Material="gold_18k_yellow", Prompt="A signet ring with a crescent"):
    B = await H.NewDesign(Prompt)
    R = await H.Client.post("/api/quote-requests", json={"design_id": B["design_id"], "candidate_id": B["candidates"][0]["id"],
                                                         "material_id": Material, "ring_size": Size, "quantity": 2,
                                                         "customer": Customer, "message": "Engrave D&L inside"})
    assert R.status_code == 200, R.text
    return B, R.json()


async def _Offer(H, Q, **Over):
    Body = {"size": 7, "weight_g": 6.5, "price_per_g": 400, "unit_price": 2600, "quantity": 2, "valid_until": _Days(10),
            "message": "Hello Dana,\n\nHere is your quote.", **Over}
    return await H.Client.post(f"/api/admin/quote-requests/{Q['id']}/offer", json=Body, headers=Admin)


def _Link(H, Version=1):
    """The customer's link from the quote email of this version (the newest such email)."""
    for M in H.App.state.Mailer.List(200):
        if M["to"] == "dana@example.com" and M["subject"].startswith("Your quote"):
            Found = re.search(r'href="([^"]+/quote\?token=[^"&]+)&amp;action=approve"', M["html"])
            if Found:
                return Found.group(1)
    raise AssertionError("no quote email")


async def test_the_workspace_shows_every_detail_and_a_price_from_the_3d_model(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey, StaffNotifyEmails=("ops@example.com",))
    try:
        B, Q = await _Request(H)
        D = (await H.Client.get(f"/api/admin/quote-requests/{Q['id']}", headers=Admin)).json()
        assert D["ref"] == "Q-5001" and D["email_to"] == "dana@example.com" and D["notification"]["staff"] == ["ops@example.com"]
        assert D["customer"] == Customer and D["message"] == "Engrave D&L inside" and D["quantity"] == 2 and D["size_label"] == "US 7"
        assert D["notification"]["customer_confirmation"]["to"] == "dana@example.com"
        Kinds = [E["kind"] for E in D["history"]]
        assert Kinds[0] == "created" and "staff_notified" in Kinds and "email" in Kinds
        assert D["history"][0]["data"]["message"] == "Engrave D&L inside"
        assert not D["suggestion"]["available"] and "no 3D model" in D["suggestion"]["warning"] and D["suggestion"]["price_per_g"] == 400
        assert D["suggested_reply"].startswith("Hello Dana,") and "$" not in D["suggested_reply"]
        assert D["can_send"] and D["defaults"]["size"] == 7 and 7.0 in D["sizes"]
        # The same request by its reference
        assert (await H.Client.get("/api/admin/quote-requests/Q-5001", headers=Admin)).json()["id"] == Q["id"]
        # With a 3D model (made at the default US 10): the weight at the requested US 7 in 18K gold × its price per g
        T = await H.Client.post(f"/api/admin/sessions/{B['design_id']}/3d", json={}, headers=Admin)
        assert T.status_code == 200, T.text
        await H.Idle()
        S = (await H.Client.get(f"/api/admin/quote-requests/{Q['id']}", headers=Admin)).json()["suggestion"]
        Raw = json.loads(H.Ctx.Db.One("SELECT r.measurement_json FROM raw_geometry r JOIN session_3d s ON s.mesh_id = r.mesh_id "
                                      "WHERE s.design_id = ?", (B["design_id"],))["measurement_json"])
        Weight = round(Scaled(Raw, UsSizeToInnerDiameterMm(7))["volume_mm3"] / 1000 * 15.3, 2)
        assert S["available"] and S["size"] == 7 and S["weight_g"] == Weight and S["unit_price"] == pytest.approx(round(Weight * 400, 2))
        assert "g/cm³" in S["basis"] and S["same_option"]
        # The customer page says where the quote will go
        Page = (await H.Client.get("/")).text
        assert "'We will email the quote to ' + quoteReq.customer.email" in Page
    finally:
        await H.Close()


async def test_the_customer_approves_from_the_email_and_the_quote_becomes_an_order(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        _B, Q = await _Request(H)
        assert (await _Offer(H, Q, unit_price=0)).status_code == 400
        assert (await _Offer(H, Q, quantity=11)).status_code == 400
        assert (await _Offer(H, Q, valid_until=_Days(-1))).status_code == 400
        assert (await _Offer(H, Q, size=7.3)).status_code == 400
        R = await _Offer(H, Q, note="Weight checked with the workshop")
        assert R.status_code == 200, R.text
        D = R.json()
        assert D["status"] == "quoted" and D["offer"]["total"] == 5200 and D["offer"]["version"] == 1 and "token" in D["offer"]["link"]
        D = (await H.Client.get(f"/api/admin/quote-requests/{Q['id']}", headers=Admin)).json()     # the email goes after the answer
        assert [E["kind"] for E in D["history"]][-3:] == ["note", "offer_sent", "email"]
        assert D["history"][-2]["data"]["weight_g"] == 6.5 and D["history"][-2]["by"]
        Link = _Link(H)
        Page = await H.Client.get(Link)
        assert Page.status_code == 200 and "noindex" in Page.headers["x-robots-tag"] and Page.headers["cache-control"] == "no-store"
        for Text in ("$2,600.00", "$5,200.00", "Approve the quote", "Decline the quote", "Here is your quote.", "US 7"):
            assert Text in Page.text, Text
        Token = Link.split("token=")[1]
        # Without the terms nothing is ordered
        R = await H.Client.post("/quote/approve", data={"token": Token, **Address, "shipping_method": "express"})
        assert R.status_code == 400 and "Please accept the Terms of Service" in R.text
        assert (await H.Client.get("/api/orders")).json()["orders"] == []
        R = await H.Client.post("/quote/approve", data={"token": Token, **Address, "shipping_method": "express", "terms": "1",
                                                        "note": "Please gift wrap"})
        assert R.status_code == 200 and "ORD-10001" in R.text and "your order is placed" in R.text
        O = (await H.Client.get("/api/orders")).json()["orders"]
        assert len(O) == 1 and O[0]["total"] == 5245 and O[0]["shipping_method"] == "express"
        L = O[0]["lines"][0]
        assert (L["unit_price"], L["quantity"], L["material_id"], L["ring_size"]) == (2600, 2, "gold_18k_yellow", 7)
        Ad = (await H.Client.get("/api/admin/orders/ORD-10001", headers=Admin)).json()
        assert "From quote Q-5001" in Ad["notes"] and "Please gift wrap" in Ad["notes"]
        assert H.Ctx.Db.One("SELECT pricing_version FROM order_lines")["pricing_version"] == "quote:Q-5001:v1"
        D = (await H.Client.get(f"/api/admin/quote-requests/{Q['id']}", headers=Admin)).json()
        assert D["status"] == "approved" and D["order"]["ref"] == "ORD-10001" and D["decision"]["note"] == "Please gift wrap"
        assert D["history"][-1]["kind"] == "approved" and D["history"][-1]["by"] == "customer" and not D["can_send"]
        # Approving again places no second order; the page tells what happened
        R = await H.Client.post("/quote/approve", data={"token": Token, **Address, "shipping_method": "standard", "terms": "1"})
        assert "You approved this quote" in R.text and len((await H.Client.get("/api/orders")).json()["orders"]) == 1
        # An approved quote is changed through its order; another quote cannot be sent
        assert (await H.Client.post(f"/api/admin/quote-requests/{Q['id']}/status", json={"status": "closed"}, headers=Admin)).status_code == 409
        assert (await _Offer(H, Q)).status_code == 409
    finally:
        await H.Close()


async def test_decline_revisions_expiry_and_notes(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey, StaffNotifyEmails=("ops@example.com",))
    try:
        _B, Q = await _Request(H)
        assert (await _Offer(H, Q)).status_code == 200
        First = _Link(H).split("token=")[1]
        R = await _Offer(H, Q, unit_price=2400)                       # a revised quote replaces the link
        assert R.status_code == 200 and R.json()["offer"]["version"] == 2 and R.json()["history"][-1]["data"]["revised"]
        Second = R.json()["offer"]["link"].split("token=")[1]
        assert Second != First
        Old = await H.Client.get(f"/quote?token={First}")
        assert "A newer quote replaced this one" in Old.text
        R = await H.Client.post("/quote/approve", data={"token": First, **Address, "shipping_method": "standard", "terms": "1"})
        assert "A newer quote replaced this one" in R.text and (await H.Client.get("/api/orders")).json()["orders"] == []
        # The customer declines the current quote, with a note; the team is told
        R = await H.Client.post("/quote/reject", data={"token": Second, "note": "Too expensive for me"})
        assert R.status_code == 200 and "we have noted your answer" in R.text
        D = (await H.Client.get(f"/api/admin/quote-requests/{Q['id']}", headers=Admin)).json()
        assert D["status"] == "rejected" and D["decision"]["note"] == "Too expensive for me" and D["history"][-2]["kind"] == "rejected"
        Staff = [M for M in H.App.state.Mailer.List(200) if M["to"] == "ops@example.com" and "declined" in M["subject"]]
        assert Staff and "Too expensive for me" in Staff[0]["html"]
        assert "You declined this quote" in (await H.Client.get(f"/quote?token={Second}")).text
        assert (await _Offer(H, Q)).status_code == 409
        # Notes are kept in the history; the Admin's own status changes too
        D = (await H.Client.post(f"/api/admin/quote-requests/{Q['id']}/note", json={"note": "Called her back"}, headers=Admin)).json()
        assert D["history"][-1]["kind"] == "note" and D["history"][-1]["data"]["note"] == "Called her back"
        D = (await H.Client.post(f"/api/admin/quote-requests/{Q['id']}/status", json={"status": "closed"}, headers=Admin)).json()
        assert D["status"] == "closed"
        # Another request: an expired quote cannot be approved; an invalid link is a 404 page
        _B2, Q2 = await _Request(H, Prompt="A twisted band")
        R = await _Offer(H, Q2, valid_until=_Days(0))
        Token = R.json()["offer"]["link"].split("token=")[1]
        Offer = json.loads(H.Ctx.Db.One("SELECT offer_json FROM quote_requests WHERE id = ?", (Q2["id"],))["offer_json"])
        Offer["valid_until"] = _Days(-1)
        H.Ctx.Db.Execute("UPDATE quote_requests SET offer_json = ? WHERE id = ?", (json.dumps(Offer), Q2["id"]))
        assert "This quote has expired" in (await H.Client.get(f"/quote?token={Token}")).text
        R = await H.Client.post("/quote/approve", data={"token": Token, **Address, "shipping_method": "standard", "terms": "1"})
        assert "This quote has expired" in R.text and (await H.Client.get("/api/orders")).json()["orders"] == []
        assert (await H.Client.get("/quote?token=nonsense")).status_code == 404
        # The list carries the quote and the decision
        L = (await H.Client.get("/api/admin/orders", headers=Admin)).json()["quote_requests"]
        assert {X["ref"]: X["status"] for X in L} == {"Q-5001": "closed", "Q-5002": "quoted"}
        assert next(X for X in L if X["ref"] == "Q-5002")["offer"]["total"] == 5200
    finally:
        await H.Close()
