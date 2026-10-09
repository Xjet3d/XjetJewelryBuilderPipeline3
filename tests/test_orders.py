"""Checkout and orders: server-side prices, promo codes, address validation seam, payment adapter,
order snapshot, confirmation email, admin lifecycle, gold quote requests."""

import json

import pytest

from p3.providers import endpoints
from tests.conftest import Harness

AdminKey = "orders-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Customer = {"first_name": "Dana", "last_name": "Levi", "email": "dana@example.com", "phone": "+972 54 123 4567"}
Address = {"recipient": "Dana Levi", "line1": "12 Rothschild Blvd", "line2": "Apt 4", "city": "Tel Aviv", "region": "",
           "postal_code": "6688112", "country": "IL"}


@pytest.fixture
async def HO(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    yield Obj
    await Obj.Close()


async def _InBag(H, Prompt="Twisted rope band", Size=7, Material="silver", Qty=1, Headers=None):
    Batch = await H.NewDesign(Prompt) if Headers is None else await _NewDesignAs(H, Prompt, Headers)
    Cand = Batch["candidates"][0]
    Hd = Headers or {}
    Cus = (await H.Client.post(f"/api/designs/{Batch['design_id']}/customize", json={"candidate_id": Cand["id"]}, headers=Hd)).json()
    await H.Idle()
    await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"ring_size": Size, "material_id": Material, "quantity": Qty}, headers=Hd)
    R = await H.Client.post("/api/bag", json={"customization_id": Cus["id"]}, headers=Hd)
    assert R.status_code == 200, R.text
    return Batch["design_id"], Cand, R.json()


async def _NewDesignAs(H, Prompt, Headers):
    R = await H.Client.post("/api/designs", data={"prompt": Prompt}, headers=Headers)
    assert R.status_code == 200, R.text
    await H.Idle()
    return (await H.Client.get(f"/api/batches/{R.json()['id']}", headers=Headers)).json()


def _Order(Extra=None, **Over):
    Body = {"customer": Customer, "address": Address, "shipping_method": "standard", "terms_accepted": True,
            "client_request_id": "click-1"}
    Body.update(Over)
    if Extra:
        Body.update(Extra)
    return Body


async def test_checkout_info_quote_and_bag_flags(HO):
    H = HO
    Info = (await H.Client.get("/api/checkout")).json()
    assert Info["customer"]["email"] == "" and Info["terms_version"] and not Info["payment"]["available"]
    assert {C["code"] for C in Info["countries"]} >= {"US", "IL", "GB", "DE", "JP"} and "US" in Info["region_required"]
    assert [S["id"] for S in Info["shipping_options"]] == ["standard", "express"] and Info["shipping_options"][1]["price"] == 45
    assert Info["quote"]["lines"] == [] and not Info["quote"]["ok"]
    Did, Cand, Bag = await _InBag(H, Size=7, Qty=2)
    assert Bag["checkout_available"] and Bag["lines"][0]["orderable"] and Bag["lines"][0]["ring_id"] == "R-1001-A"
    Q = (await H.Client.post("/api/checkout/quote", json={"shipping_method": "express"})).json()
    L = Q["lines"][0]
    Unit = H.Ctx.Pricing.QuoteFor("silver").unit_price
    assert Q["ok"] and L["unit_price"] == Unit and L["line_total"] == pytest.approx(2 * Unit) and not L["repriced"]
    assert Q["subtotal"] == pytest.approx(2 * Unit) and Q["shipping"] == 45 and Q["total"] == pytest.approx(2 * Unit + 45)
    assert Q["discount"] == 0 and Q["promo"] is None and Q["promo_error"] is None
    # The price changes in Admin → the bag flags it and checkout charges today's price
    Doc = H.Ctx.MaterialPrices.Current()
    H.Ctx.MaterialPrices.Save({"materials": {M: {**R, "fixed_price": (R["fixed_price"] or 0) + 10 if M == "silver" else R["fixed_price"]}
                                             for M, R in Doc["materials"].items()}}, "test", "silver +10")
    Bag = (await H.Client.get("/api/bag")).json()
    assert Bag["lines"][0]["price_is_stale"] and Bag["lines"][0]["current_unit_price"] == pytest.approx(Unit + 10)
    Q = (await H.Client.post("/api/checkout/quote", json={})).json()
    assert Q["lines"][0]["repriced"] and Q["lines"][0]["unit_price"] == pytest.approx(Unit + 10) and Q["repriced"]


async def test_place_order_snapshots_everything_clears_the_bag_and_emails(HO):
    H = HO
    Did, Cand, _ = await _InBag(H, Size=7, Qty=2)
    Did2, Cand2, _ = await _InBag(H, "Signet with hexagon face", Size=10, Material="vermeil")
    Unit, Unit2 = H.Ctx.Pricing.QuoteFor("silver").unit_price, H.Ctx.Pricing.QuoteFor("vermeil").unit_price
    # Validation: every field problem is reported, nothing is created
    Bad = await H.Client.post("/api/orders", json=_Order(customer={**Customer, "email": "nope", "phone": "12"},
                                                         address={**Address, "country": "US", "postal_code": "ABC"}, terms_accepted=False))
    assert Bad.status_code == 400 and Bad.json()["error"]["code"] == "invalid_order"
    Fields = {P["field"] for P in Bad.json()["error"]["problems"]}
    assert Fields == {"email", "phone", "region", "postal_code", "terms_accepted"}
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM orders")["n"] == 0
    R = await H.Client.post("/api/orders", json=_Order())
    assert R.status_code == 200, R.text
    O = R.json()
    assert O["ref"] == "ORD-10001" and O["status"] == "new" and O["status_label"] == "New"
    assert O["payment_status"] == "pending" and O["payment_provider"] is None and "not available yet" in O["payment_message"]
    assert O["count"] == 3 and len(O["lines"]) == 2
    L1, L2 = O["lines"]
    assert (L1["title"], L1["ring_id"], L1["material_label"], L1["ring_size"], L1["quantity"]) == (L1["title"], "R-1001-A", "Silver", 7, 2)
    assert L1["unit_price"] == Unit and L1["line_total"] == pytest.approx(2 * Unit) and L1["image_url"] == Cand["image_url"]
    assert L2["ring_id"] == "R-1002-A" and L2["material_label"] == "Vermeil" and L2["unit_price"] == Unit2
    assert O["subtotal"] == pytest.approx(2 * Unit + Unit2) and O["shipping"] == 0 and O["discount"] == 0
    assert O["total"] == pytest.approx(O["subtotal"]) and O["currency"] == "USD"
    assert O["address_lines"] == ["Dana Levi", "12 Rothschild Blvd", "Apt 4", "Tel Aviv 6688112", "Israel"]
    assert O["address_validation"] == "unverified" and O["terms_version"] and O["terms_accepted_at"]
    assert "production_cost" not in json.dumps(O) and "calculated_price" not in json.dumps(O)
    # The bag is empty; the order is listed; the same click never creates a second order
    assert (await H.Client.get("/api/bag")).json()["lines"] == []
    assert (await H.Client.post("/api/orders", json=_Order())).json()["id"] == O["id"]
    assert [X["ref"] for X in (await H.Client.get("/api/orders")).json()["orders"]] == ["ORD-10001"]
    assert (await H.Client.get(f"/api/orders/{O['id']}")).json()["ref"] == "ORD-10001"
    assert (await H.Client.post("/api/orders", json=_Order(client_request_id="click-2"))).status_code == 409   # bag empty now
    # A confirmation email went to the customer (outbox), without any internal pricing
    Mail = H.App.state.Mailer.List()[0]
    assert Mail["to"] == "dana@example.com" and "ORD-10001" in Mail["subject"]
    assert "R-1001-A" in Mail["html"] and "Tel Aviv" in Mail["html"] and "Payment pending" in Mail["html"]
    assert "cost" not in Mail["html"].lower()
    # The journey reached Order; the admin sees it on the session and in the funnel
    S = (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()["session"]
    assert S["ordered"] and S["order_refs"] == ["ORD-10001"] and S["path"].endswith("Bag → Order")
    Ev = [E for E in (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()["timeline"] if E["kind"] == "order_placed"]
    assert Ev and Ev[0]["data"]["order_ref"] == "ORD-10001" and Ev[0]["data"]["quantity"] == 2
    Other = {"X-Access-Token": H.Ctx.Accounts.IssueToken("other")[0]}
    assert (await H.Client.get(f"/api/orders/{O['id']}", headers=Other)).status_code == 404


async def test_promo_codes_are_validated_server_side_and_snapshotted(HO):
    H = HO
    Did, Cand, _ = await _InBag(H, Size=7, Qty=2)
    Unit = H.Ctx.Pricing.QuoteFor("silver").unit_price
    # Admin creates codes: validation, duplicates, restriction to materials
    assert (await H.Client.post("/api/admin/promo-codes", json={"code": "x", "kind": "percent", "value": 10}, headers=Admin)).status_code == 400
    assert (await H.Client.post("/api/admin/promo-codes", json={"code": "TEN", "kind": "percent", "value": 150}, headers=Admin)).status_code == 400
    P = (await H.Client.post("/api/admin/promo-codes", json={"code": "ten-off", "kind": "percent", "value": 10, "usage_limit": 1,
                                                             "note": "launch"}, headers=Admin)).json()
    assert P["code"] == "TEN-OFF" and P["active"] and P["usage_count"] == 0
    assert (await H.Client.post("/api/admin/promo-codes", json={"code": "TEN-OFF", "kind": "fixed", "value": 5}, headers=Admin)).status_code == 409
    V = (await H.Client.post("/api/admin/promo-codes", json={"code": "VERMEIL25", "kind": "fixed", "value": 25,
                                                             "materials": ["vermeil"]}, headers=Admin)).json()
    Old = (await H.Client.post("/api/admin/promo-codes", json={"code": "OLD", "kind": "percent", "value": 5,
                                                               "ends_at": "2020-01-01T00:00:00Z"}, headers=Admin)).json()
    assert (await H.Client.post("/api/admin/promo-codes", json={"code": "C", "kind": "percent", "value": 5})).status_code == 403
    # Customer quotes: friendly errors, correct discounts; the browser never computes a discount
    for Code, Err in (("NOPE", "promo_unknown"), ("OLD", "promo_expired"), ("VERMEIL25", "promo_not_applicable")):
        Q = (await H.Client.post("/api/checkout/quote", json={"promo_code": Code})).json()
        assert Q["promo"] is None and Q["promo_error"]["code"] == Err and Q["discount"] == 0, (Code, Q["promo_error"])
    Q = (await H.Client.post("/api/checkout/quote", json={"promo_code": " ten-off "})).json()
    assert Q["promo"]["code"] == "TEN-OFF" and Q["discount"] == pytest.approx(round(2 * Unit * 0.10, 2))
    assert Q["total"] == pytest.approx(2 * Unit - Q["discount"])
    # Placing the order with the code snapshots it and counts one use; the next customer is refused
    R = await H.Client.post("/api/orders", json=_Order(promo_code="ten-off"))
    assert R.status_code == 200, R.text
    O = R.json()
    assert O["promo_code"] == "TEN-OFF" and O["discount"] == Q["discount"] and O["total"] == pytest.approx(Q["total"])
    assert O["promo"]["original_amount"] == pytest.approx(2 * Unit) and O["promo"]["final_amount"] == pytest.approx(2 * Unit - Q["discount"])
    assert next(X for X in (await H.Client.get("/api/admin/promo-codes", headers=Admin)).json()["promo_codes"] if X["id"] == P["id"])["usage_count"] == 1
    await _InBag(H, "Plain band", Size=8)
    R = await H.Client.post("/api/orders", json=_Order(promo_code="TEN-OFF", client_request_id="click-2"))
    assert R.status_code == 400 and R.json()["error"]["code"] == "promo_exhausted"
    # Deactivate → "no longer active"; fixed discount never exceeds the eligible subtotal
    await H.Client.patch(f"/api/admin/promo-codes/{V['id']}", json={"active": False}, headers=Admin)
    Q = (await H.Client.post("/api/checkout/quote", json={"promo_code": "VERMEIL25"})).json()
    assert Q["promo_error"]["code"] == "promo_inactive"
    Big = (await H.Client.post("/api/admin/promo-codes", json={"code": "BIG", "kind": "fixed", "value": 100000}, headers=Admin)).json()
    Q = (await H.Client.post("/api/checkout/quote", json={"promo_code": "BIG"})).json()
    assert Q["discount"] == Q["subtotal"] and Q["total"] == 0
    assert Big["kind"] == "fixed"


async def test_address_validation_seam_and_country_rules(HO):
    H = HO
    V = (await H.Client.post("/api/checkout/address", json={"address": Address})).json()
    assert V["problems"] == [] and V["validation"]["status"] == "unverified" and V["validation"]["provider"] is None
    assert V["lines"][-1] == "Israel" and V["address"]["country"] == "IL"
    US = (await H.Client.post("/api/checkout/address", json={"address": {**Address, "country": "us", "region": "NY", "postal_code": "10001"}})).json()
    assert US["problems"] == [] and US["address"]["country"] == "US"
    Bad = (await H.Client.post("/api/checkout/address", json={"address": {**Address, "country": "US", "region": "", "postal_code": "1"}})).json()
    assert {P["field"] for P in Bad["problems"]} == {"region", "postal_code"}
    AE = (await H.Client.post("/api/checkout/address", json={"address": {**Address, "country": "AE", "postal_code": ""}})).json()
    assert AE["problems"] == []                                             # no postal codes there
    assert {P["field"] for P in (await H.Client.post("/api/checkout/address", json={"address": {}})).json()["problems"]} \
        == {"recipient", "line1", "city", "country"}
    # A provider that corrects the address: the suggestion is offered, and only a confirmed one is used
    from p3 import addressing

    class Fixer:
        Name = "fixer"
        def Check(self, A):
            return {"status": "corrected", "suggestion": {**A, "line1": "12 Rothschild Boulevard"}, "message": "We found a more precise address."}
    H.Svc.Orders.Validator = Fixer()
    V = (await H.Client.post("/api/checkout/address", json={"address": Address})).json()
    assert V["validation"]["status"] == "corrected" and V["validation"]["suggestion"]["line1"] == "12 Rothschild Boulevard"
    await _InBag(H, Size=7)
    O = (await H.Client.post("/api/orders", json=_Order())).json()
    assert O["address"]["line1"] == "12 Rothschild Blvd" and O["address_validation"] == "corrected"      # kept as typed, verdict stored
    assert O["address_validation_detail"]["status"] == "corrected"
    await _InBag(H, "Plain band", Size=8)
    O2 = (await H.Client.post("/api/orders", json=_Order(client_request_id="click-2", use_suggested_address=True))).json()
    assert O2["address"]["line1"] == "12 Rothschild Boulevard" and O2["address_lines"][1] == "12 Rothschild Boulevard"
    assert addressing.CountryNames["IL"] == "Israel"


async def test_bag_rules_at_checkout(HO):
    H = HO
    Did, Cand, _ = await _InBag(H, Size=7)
    # A size that is no longer standard (e.g. the catalog changed): the line is not orderable, the bag says so, the order is refused
    H.Ctx.Db.Execute("UPDATE bag_lines SET ring_size = 3.25")
    Bag = (await H.Client.get("/api/bag")).json()
    assert not Bag["checkout_available"] and not Bag["lines"][0]["orderable"] and Bag["checkout_note"]
    R = await H.Client.post("/api/orders", json=_Order())
    assert R.status_code == 409 and R.json()["error"]["code"] == "bag_not_orderable"
    assert (await H.Client.post("/api/orders", json=_Order(shipping_method="drone"))).status_code == 400
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM orders")["n"] == 0


async def test_admin_orders_list_search_lifecycle_payment_and_stl_name(HO):
    H = HO
    Did, Cand, _ = await _InBag(H, "a twisted band inspired by Aurora", Size=10, Qty=1)        # named "Aurora Twist"
    O = (await H.Client.post("/api/orders", json=_Order())).json()
    assert (await H.Client.get("/api/admin/orders")).status_code == 403
    L = (await H.Client.get("/api/admin/orders", headers=Admin)).json()
    assert [X["ref"] for X in L["orders"]] == ["ORD-10001"] and L["orders"][0]["mock"] and L["orders"][0]["three_d_state"] is None
    assert [S["id"] for S in L["statuses"]][:2] == ["new", "payment_confirmed"] and L["quote_counts"]["new"] == 0
    assert (L["total"], L["offset"], L["limit"]) == (1, 0, 50)
    Row = L["orders"][0]
    assert Row["customer"]["email"] == "dana@example.com" and Row["lines"][0]["ring_id"] == "R-1001-A" and Row["address_validation"] == "unverified"
    # Search by order ID, ring ID, design name, customer name and email
    for Q in ("ORD-10001", "10001", "R-1001-A", "aurora", "Dana", "dana@example"):
        assert [X["ref"] for X in (await H.Client.get(f"/api/admin/orders?q={Q}", headers=Admin)).json()["orders"]] == ["ORD-10001"], Q
    assert (await H.Client.get("/api/admin/orders?q=R-1099", headers=Admin)).json()["orders"] == []
    assert (await H.Client.get("/api/admin/orders?status=shipped", headers=Admin)).json()["orders"] == []
    D = (await H.Client.get("/api/admin/orders/ORD-10001", headers=Admin)).json()         # by reference or id
    assert D["id"] == O["id"] and D["events"][0]["kind"] == "placed" and D["lines"][0]["session_id"] == Did
    assert D["user"]["account_id"] == H.Who.AccountId and "ORD-10001" in json.dumps(D)
    # Lifecycle: payment recorded by hand (with a note) moves a new order on; statuses step through; completed is final
    assert (await H.Client.post(f"/api/admin/orders/{O['id']}/payment", json={"status": "paid"}, headers=Admin)).status_code == 400   # note required
    D = (await H.Client.post(f"/api/admin/orders/{O['id']}/payment", json={"status": "paid", "note": "Bank transfer received", "ref": "TR-77"},
                             headers=Admin)).json()
    assert D["payment_status"] == "paid" and D["payment_provider"] == "manual" and D["payment_ref"] == "TR-77"
    assert D["status"] == "payment_confirmed" and [E["kind"] for E in D["events"]] == ["placed", "email", "payment", "status"]
    assert D["events"][2]["data"]["manual"] and D["events"][2]["by"]
    # Production and later need complete 3D results for every line: refused without them, allowed as a noted exception
    R = await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": "production", "note": "ok"}, headers=Admin)
    assert R.status_code == 409 and R.json()["error"]["code"] == "three_d_unresolved"
    assert (await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": "production", "force": True}, headers=Admin)).status_code == 400
    for S in ("three_d_ready", "production", "qc", "shipped", "completed"):
        D = (await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": S, "note": "ok", "force": True}, headers=Admin)).json()
        assert D["status"] == S
    assert (await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": "production"}, headers=Admin)).status_code == 409
    assert (await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": "bogus"}, headers=Admin)).status_code == 400
    D = (await H.Client.post(f"/api/admin/orders/{O['id']}/note", json={"note": "Customer asked for gift wrap"}, headers=Admin)).json()
    assert D["notes"] is None and [N["note"] for N in D["notes_list"]] == ["Customer asked for gift wrap"]
    assert D["notes_list"][0]["by"] and D["notes_list"][0]["at"] and D["notes_list"][0]["kind"] == "note"
    # A later note joins the list: it never replaces an earlier one
    D = (await H.Client.post(f"/api/admin/orders/{O['id']}/note", json={"note": "Engraving checked"}, headers=Admin)).json()
    assert [N["note"] for N in D["notes_list"]] == ["Customer asked for gift wrap", "Engraving checked"]
    assert [E["kind"] for E in D["events"]][-2:] == ["note", "note"]
    Mine = (await H.Client.get(f"/api/orders/{O['id']}")).json()
    assert Mine["status_label"] == "Completed" and Mine["payment_label"] == "Paid" and "notes" not in Mine
    # 3D for the ordered design: only a result for the ORDERED size and material counts as the line's 3D.
    # Without any model the order page refuses (a paid Hi3D call belongs to the session page).
    Line = D["lines"][0]
    assert Line["three_d_id"] is None and not Line["has_model"] and Line["three_d_latest"] is None
    R = await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{Line['id']}/3d", headers=Admin)
    assert R.status_code == 409 and R.json()["error"]["code"] == "no_model"
    # The admin generated the model on the session page — but at another size and material (US 7 · vermeil)
    T = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 7, "material_id": "vermeil"}, headers=Admin)).json()
    await H.Idle()
    D = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()
    Line = D["lines"][0]
    assert Line["three_d_id"] is None and Line["three_d_state"] is None and Line["has_model"] and Line["model_ring_id"] == "R-1001-A"
    assert Line["three_d_latest"]["size"] == 7 and Line["three_d_latest"]["material_id"] == "vermeil"   # exists, but not this line's
    # Prepare: the model is scaled to the ordered US 10 · Silver by arithmetic — no second Hi3D call
    Subs = len(H.Provider.SubmissionsFor(endpoints.Mesh))
    P = (await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{Line['id']}/3d", headers=Admin)).json()
    await H.Idle()
    assert P["prepared"] and P["three_d_id"] != T["id"] and len(H.Provider.SubmissionsFor(endpoints.Mesh)) == Subs
    D = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()
    Line = D["lines"][0]
    assert Line["three_d_id"] == P["three_d_id"] and Line["three_d_match"] and Line["three_d_state"] == "complete" and D["three_d_state"] == "complete"
    assert any(E["kind"] == "3d_prepared" for E in D["events"])
    assert (await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{Line['id']}/3d", headers=Admin)).json()["prepared"] is False   # idempotent
    E = (await H.Client.post(f"/api/admin/3d/{Line['three_d_id']}/export", headers=Admin)).json()
    await H.Idle()
    E = (await H.Client.get(f"/api/admin/3d/{Line['three_d_id']}/export/{E['job_id']}?order=ORD-10001", headers=Admin)).json()
    assert "order=ORD-10001" in E["url"]
    R = await H.Client.get(E["url"].removeprefix(H.Ctx.Settings.BasePath))
    from p3.production3d import SlugPart
    Name = SlugPart((await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()["session"]["title"])
    assert f'filename="{Name}_R-1001-A_ORD-10001_Silver_US10.stl"' in R.headers["content-disposition"]   # the ORDERED size and material
    Bad = await H.Client.get(E["url"].removeprefix(H.Ctx.Settings.BasePath).replace("ORD-10001", "../evil"))
    assert "evil" not in Bad.headers.get("content-disposition", "") and Bad.status_code == 200    # never trusted
    # Dashboard: orders are counted (mock excluded by default, so none here) and gallery stats know orders
    Dash = (await H.Client.get("/api/admin/dashboard", headers=Admin)).json()
    assert Dash["orders"]["orders"] == 0
    from tests.conftest import MakeLive
    MakeLive(H)
    Dash = (await H.Client.get("/api/admin/dashboard", headers=Admin)).json()["orders"]
    assert Dash["orders"] == 1 and Dash["revenue"] == pytest.approx(O["total"]) and Dash["by_status"]["completed"] == 1


async def test_order_search_and_pages_run_in_sql(HO):
    """Search and paging happen in the database: every match counts (no 300-row window searched in Python), a page
    holds `limit` orders, the newest first."""
    H = HO
    for N in range(3):
        await _InBag(H, f"Band number {N}")
        assert (await H.Client.post("/api/orders", json=_Order(client_request_id=f"click-{N}"))).status_code == 200
    Page = (await H.Client.get("/api/admin/orders?limit=2", headers=Admin)).json()
    assert [O["ref"] for O in Page["orders"]] == ["ORD-10003", "ORD-10002"] and Page["total"] == 3
    Page = (await H.Client.get("/api/admin/orders?limit=2&offset=2", headers=Admin)).json()
    assert [O["ref"] for O in Page["orders"]] == ["ORD-10001"] and Page["total"] == 3
    # The full customer name, an Order ID, a design name (any case); LIKE wildcards in the box are plain text
    Title = Page["orders"][0]["lines"][0]["title"]
    for Q, Want in (("Dana Levi", 3), ("ORD-10002", 1), (Title.upper(), 1), ("dsg%", 0), ("ds_", 0), ("dsg_", 3), ("nobody@", 0)):
        R = (await H.Client.get("/api/admin/orders", params={"q": Q}, headers=Admin)).json()
        assert (R["total"], len(R["orders"])) == (Want, Want), Q
    assert (await H.Client.get("/api/admin/orders?limit=0", headers=Admin)).json()["limit"] == 1          # clamped
    assert (await H.Client.get("/api/admin/orders?limit=5000", headers=Admin)).json()["limit"] == 200


async def test_a_cancellation_keeps_its_reason_beside_the_notes(HO):
    H = HO
    await _InBag(H)
    O = (await H.Client.post("/api/orders", json=_Order())).json()
    await H.Client.post(f"/api/admin/orders/{O['id']}/note", json={"note": "Called the customer"}, headers=Admin)
    D = (await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": "cancelled", "note": "  Customer changed their mind  "},
                             headers=Admin)).json()
    assert D["status"] == "cancelled" and D["events"][-1]["data"] == {"from": "new", "to": "cancelled", "note": "  Customer changed their mind  "}
    assert [(N["kind"], N["note"]) for N in D["notes_list"]] == [("note", "Called the customer"), ("cancelled", "Customer changed their mind")]
    # A status note that is not a cancellation stays in the history only; a cancellation without a reason adds nothing
    D = (await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": "new", "note": "Reopened"}, headers=Admin)).json()
    D = (await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": "cancelled"}, headers=Admin)).json()
    assert [N["kind"] for N in D["notes_list"]] == ["note", "cancelled"] and D["events"][-2]["data"]["note"] == "Reopened"


async def test_gold_asks_for_a_quote_instead_of_ordering(HO):
    H = HO
    Batch = await H.NewDesign("Signet ring")
    Did, Cand = Batch["design_id"], Batch["candidates"][1]
    Cus = (await H.Client.post(f"/api/designs/{Did}/customize", json={"candidate_id": Cand["id"]})).json()
    Lux = (await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"material_id": "gold_18k_yellow", "ring_size": 9})).json()
    assert not Lux["can_add_to_bag"] and Lux["add_to_bag_blocked_reason"] == "luxury_preview_only"
    Bad = await H.Client.post("/api/quote-requests", json={"design_id": Did, "candidate_id": Cand["id"], "material_id": "gold_18k_yellow",
                                                           "ring_size": 9, "quantity": 1, "customer": {**Customer, "phone": ""}})
    assert Bad.status_code == 400 and Bad.json()["error"]["problems"][0]["field"] == "phone"
    R = await H.Client.post("/api/quote-requests", json={"design_id": Did, "candidate_id": Cand["id"], "material_id": "gold_18k_yellow",
                                                         "ring_size": 9, "quantity": 2, "customer": Customer, "message": "Engraving inside?"})
    assert R.status_code == 200, R.text
    Q = R.json()
    assert Q["ref"] == "Q-5001" and Q["ring_id"] == "R-1001-B" and Q["material_label"] == "18K Yellow Gold" and Q["status"] == "new"
    Mail = H.App.state.Mailer.List()[0]
    assert Mail["to"] == "dana@example.com" and "Q-5001" in Mail["subject"] and "Engraving inside?" in Mail["html"]
    L = (await H.Client.get("/api/admin/quote-requests", headers=Admin)).json()
    assert [X["ref"] for X in L["quote_requests"]] == ["Q-5001"] and L["quote_requests"][0]["customer"]["email"] == "dana@example.com"
    assert L["total"] == 1 and L["counts"] == {"new": 1, "quoted": 0, "answered": 0, "approved": 0, "rejected": 0, "closed": 0}
    assert (await H.Client.get("/api/admin/orders", headers=Admin)).json()["quote_counts"]["new"] == 1          # the inbox badge
    assert (await H.Client.post(f"/api/admin/quote-requests/{Q['id']}/status", json={"status": "answered"}, headers=Admin)).json()["status"] == "answered"
    S = (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()
    assert any(E["kind"] == "quote_requested" for E in S["timeline"])
    assert len(H.Provider.SubmissionsFor(endpoints.Mesh)) == 0            # nothing paid was triggered
