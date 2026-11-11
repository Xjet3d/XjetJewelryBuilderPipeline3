"""B7 — My Account and the order's own page (owner's approval, 11 Nov 2026): an order by its Order ID for its owner only,
the Confirmed status, one customer status per order (3D ready, In production and Quality check all read "Production"),
progress that ticks only what the history records, the customer's own checkout note, the /account addresses (never
indexed or cached), and the email's View Your Order link to the order's page — while the old /#account still opens."""

import re

from p3 import mail as Mail
from p3 import orders as Orders
from p3 import sitepages as SitePages
from tests.conftest import Harness
from tests.test_orders import _InBag, _Order

AdminKey = "b7-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}


async def _Placed(H, **Over):
    await _InBag(H, Size=7, Qty=1)
    R = await H.Client.post("/api/orders", json=_Order(**Over))
    assert R.status_code == 200, R.text
    return R.json()


async def _Status(H, O, Status, **Extra):
    R = await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": Status, "note": Extra.pop("note", ""), **Extra}, headers=Admin)
    assert R.status_code == 200, R.text
    return (await H.Client.get(f"/api/orders/{O['ref']}")).json()


def _Steps(O):
    return [(P["key"], P["state"]) for P in O["progress"]]


async def test_an_order_is_found_by_its_order_id_for_its_owner_only(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        O = await _Placed(H)
        assert O["ref"] == "ORD-10001"
        for Key in ("ORD-10001", "ord-10001", O["id"]):
            R = await H.Client.get(f"/api/orders/{Key}")
            assert R.status_code == 200 and R.json()["id"] == O["id"], Key
        # Another account, an unknown Order ID, a bare number: the same "not found" — an Order ID reveals nothing
        Other = {"X-Access-Token": H.Ctx.Accounts.IssueToken("other")[0]}
        for Key, Hd in (("ORD-10001", Other), (O["id"], Other), ("ORD-10002", {}), ("10001", {}), ("ORD-1x", {})):
            R = await H.Client.get(f"/api/orders/{Key}", headers=Hd)
            assert R.status_code == 404 and R.json()["error"]["code"] == "order_not_found", Key
        # An Admin's key is not a customer: no order through the customer API
        assert (await H.Client.get("/api/orders/ORD-10001", headers={**Admin, "X-Access-Token": ""})).status_code in (401, 403, 404)
    finally:
        await H.Close()


async def test_each_status_in_the_customers_words_with_progress_that_ticks_only_what_happened(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        O = await _Placed(H)
        C = O["customer_status"]
        assert (C["key"], C["label"], C["tone"]) == ("new", "Design review", "progress") and "about 2 business days" in C["next"]
        assert "You can still cancel free of charge" in C["now"] and O["cancel_free"] is True
        assert _Steps(O) == [("received", "done"), ("review", "current"), ("payment", "upcoming"), ("production", "upcoming"),
                             ("shipping", "upcoming"), ("completed", "upcoming")]
        assert O["progress"][0]["at"] == O["created_at"] and all(P["at"] is None for P in O["progress"][1:])
        assert O["payment_view"]["label"] == "Not due yet" and "No payment is taken online" in O["payment_view"]["text"]
        # Confirmed: the design review passed — awaiting payment; free cancellation ends
        O = await _Status(H, O, "confirmed")
        assert O["status"] == "confirmed" and O["status_label"] == "Confirmed"
        assert (O["customer_status"]["label"], O["customer_status"]["tone"]) == ("Awaiting payment", "action") and O["cancel_free"] is False
        assert _Steps(O)[1:3] == [("review", "done"), ("payment", "current")] and O["progress"][1]["at"]
        assert O["payment_view"]["label"] == "Awaiting payment"
        # The payment recorded: Payment received, production next — the date of the payment, never invented
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/payment", json={"status": "paid", "note": "bank transfer"}, headers=Admin)
        assert R.status_code == 200 and R.json()["status"] == "payment_confirmed"
        O = (await H.Client.get(f"/api/orders/{O['ref']}")).json()
        assert O["customer_status"]["label"] == "Payment received" and O["paid_at"] and O["payment_view"] == {
            "tone": "paid", "label": "Paid", "text": "Your payment is confirmed.", "at": O["paid_at"]}
        assert _Steps(O)[2:4] == [("payment", "done"), ("production", "current")] and O["progress"][2]["at"] == O["paid_at"]
        assert O["progress"][3]["text"] == "Starts now · up to 14 business days"
        # 3D ready, In production and Quality check are all "Production" for the customer
        for Status in ("three_d_ready", "production", "qc"):
            O = await _Status(H, O, Status, force=True, note="test: made outside the pipeline")
            assert O["customer_status"]["label"] == "Production" and O["status_label"] != "Production", Status
            assert ("production", "current") in _Steps(O) and O["progress"][3]["since"]
        O = await _Status(H, O, "shipped", force=True, note="test")
        assert O["customer_status"]["label"] == "Shipped" and _Steps(O)[3:5] == [("production", "done"), ("shipping", "current")]
        assert "Standard delivery, up to 14 business days" in O["customer_status"]["next"]
        O = await _Status(H, O, "completed", force=True, note="test")
        assert O["customer_status"]["label"] == "Completed" and {S for _K, S in _Steps(O)} == {"done"} and O["progress"][-1]["at"]
        # Nothing internal in the customer's words
        for X in ("3D ready", "Quality check", "New"):
            assert X not in {O["customer_status"]["label"], *(P["label"] for P in O["progress"])}
    finally:
        await H.Close()


async def test_a_skipped_review_is_passed_without_a_date_and_a_cancelled_order_shows_only_what_happened(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        O = await _Placed(H)
        await H.Client.post(f"/api/admin/orders/{O['id']}/payment", json={"status": "paid", "note": "paid before confirming"}, headers=Admin)
        O = (await H.Client.get(f"/api/orders/{O['ref']}")).json()
        Review = O["progress"][1]
        assert (Review["state"], Review["at"]) == ("done", None)                  # passed (paid after it), no date it never had
        await _InBag(H, "Plain band", Size=8)
        O2 = (await H.Client.post("/api/orders", json=_Order(client_request_id="click-2"))).json()
        O2 = await _Status(H, O2, "cancelled", note="customer asked")
        assert O2["customer_status"]["label"] == "Cancelled" and _Steps(O2) == [("received", "done"), ("cancelled", "done")]
        assert O2["payment_view"]["label"] == "Not due" and O2["cancel_free"] is False
        # A failed payment says so, and that we get in touch
        await _InBag(H, "Leaf band", Size=6)
        O3 = (await H.Client.post("/api/orders", json=_Order(client_request_id="click-3"))).json()
        await _Status(H, O3, "confirmed")
        await H.Client.post(f"/api/admin/orders/{O3['id']}/payment", json={"status": "failed", "note": "card declined"}, headers=Admin)
        O3 = (await H.Client.get(f"/api/orders/{O3['ref']}")).json()
        assert "didn’t go through" in O3["customer_status"]["next"] and O3["payment_view"]["label"] == "Payment didn’t go through"
    finally:
        await H.Close()


async def test_the_customer_sees_their_own_checkout_note_and_never_another(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        O = await _Placed(H, customer_note="  Please engrave nothing — it is a gift.  ")
        assert O["customer_note"] == "Please engrave nothing — it is a gift."
        await H.Client.post(f"/api/admin/orders/{O['id']}/note", json={"note": "Internal: check the bore"}, headers=Admin)
        assert (await H.Client.get(f"/api/orders/{O['ref']}")).json()["customer_note"] == "Please engrave nothing — it is a gift."
        assert Orders.CustomerNote("From quote Q-5001 (version 2). Customer's note: Size 7 please") == "Size 7 please"
        assert Orders.CustomerNote("From quote Q-5001 (version 2).") is None
        assert Orders.CustomerNote("Internal: an Admin note that replaced the field before notes were append-only") is None
        assert Orders.CustomerNote(None) is None
    finally:
        await H.Close()


async def test_the_account_addresses_are_the_app_never_indexed_or_cached_and_the_old_link_still_opens(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey, BasePath="/JewelryB2C3")
    try:
        for Path in ("/account", "/account/orders", "/account/quotes", "/account/security", "/account/orders/ORD-10001"):
            R = await H.Client.get(Path)
            assert R.status_code == 200, Path
            assert R.headers["cache-control"] == "no-store" and "noindex" in R.headers["x-robots-tag"], Path
            assert re.search(r"<title>(.*?)</title>", R.text, re.S).group(1) == "My Account · XJet Atelier", Path
            assert '<meta name="robots" content="noindex">' in R.text and "ORD-10001" not in R.text, Path
        assert SitePages.IsAccountPath("/account/orders/ORD-1") and not SitePages.IsAccountPath("/accounts")
        # The home page (the old /#account of emails sent before) is unchanged: the app opens My Orders from the fragment
        assert (await H.Client.get("/")).status_code == 200
        Sitemap = (await H.Client.get("/sitemap.xml")).text
        assert "/account" not in Sitemap
    finally:
        await H.Close()


async def test_the_confirmation_email_opens_the_order_page_and_keeps_its_site_links(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey, PublicBaseUrl="https://atelier.example", BasePath="/JewelryB2C3")
    try:
        O = await _Placed(H)
        Html = H.App.state.Mailer.List()[0]["html"]
        assert 'href="https://atelier.example/JewelryB2C3/account/orders/ORD-10001"' in Html and "View Your Order" in Html
        assert "#account" not in Html and O["id"] not in Html                       # the Order ID only, never the internal id
        assert "https://atelier.example/JewelryB2C3/terms" in Html and "https://atelier.example/JewelryB2C3/warranty" in Html
    finally:
        await H.Close()
    # An old-style call (…/#account) still gives the site's links (tests and tools that pass it)
    _S, Html = Mail.OrderConfirmationEmail({**O, "customer": O["customer"]}, "https://a.example", "https://a.example/JewelryB2C3/#account")
    assert "https://a.example/JewelryB2C3/terms" in Html
    assert Orders.OrderPageUrl("https://a.example/JewelryB2C3/", "ORD-10002") == "https://a.example/JewelryB2C3/account/orders/ORD-10002"
    assert Orders.OrderPageUrl("", "ORD-10002") == ""


async def test_fulfillment_approval_of_every_part_confirms_a_new_order_and_nothing_else(tmp_path):
    from tests.test_fulfillment_bridge import Admin as FAdmin, _Enrolled, _Event, _On, _Placed as _FPlaced
    from tests.test_fulfillment_notifications import _Parts, _Post
    H = _On(tmp_path)
    try:
        Did, O = await _FPlaced(H, Qty=1)
        await _Enrolled(H, Did, O)
        Approved = {"part": "MP-10001-001", "decision": "approved", "revision_no": 1, "reason": None, "customer_message": None, "preliminary": False}
        Pending = [{**P, "review_status": "in_review"} for P in _Parts("Under review")]
        await _Post(H, _Event("c1", O["id"], 1, parts=Pending))
        assert (await H.Client.get(f"/api/orders/{O['id']}")).json()["status"] == "new"
        await _Post(H, _Event("c2", O["id"], 2, "fulfillment.review_approved", customer_status="Preparing for production", parts=_Parts("Preparing for production"), review=Approved))
        One = (await H.Client.get(f"/api/orders/{O['ref']}")).json()
        assert One["status"] == "confirmed" and One["customer_status"]["label"] == "Awaiting payment"
        Ev = H.Ctx.Db.One("SELECT by, data_json FROM order_events WHERE order_id = ? AND kind = 'status' ORDER BY id DESC LIMIT 1", (O["id"],))
        assert Ev["by"] == "fulfillment" and '"to": "confirmed"' in Ev["data_json"]
        # A later approval (a new revision) never moves an order that went on
        await H.Client.post(f"/api/admin/orders/{O['id']}/payment", json={"status": "paid", "note": "transfer"}, headers=FAdmin)
        await _Post(H, _Event("c3", O["id"], 3, "fulfillment.review_approved", customer_status="Preparing for production", parts=_Parts("Preparing for production"),
                              review={**Approved, "revision_no": 2}))
        assert (await H.Client.get(f"/api/orders/{O['id']}")).json()["status"] == "payment_confirmed"
    finally:
        await H.Close()


def test_the_status_lists_know_confirmed():
    from p3 import fulfillment_bridge as Bridge
    assert Orders.StatusOrder.index("confirmed") == 1 and "confirmed" in Bridge.EligibleOrderStatuses
    assert set(Orders.CustomerStatusLabels) == set(Orders.StatusOrder) | {"cancelled"}
    assert {Orders.CustomerStatusLabels[S] for S in ("three_d_ready", "production", "qc")} == {"Production"}


def test_the_app_knows_the_account_addresses_the_old_link_and_the_admin_can_confirm():
    import pathlib
    Web = pathlib.Path(__file__).resolve().parents[1] / "web"
    App, Page = (Web / "app.js").read_text(encoding="utf-8"), (Web / "index.html").read_text(encoding="utf-8")
    Admin, Consent = (Web / "admin.html").read_text(encoding="utf-8"), (Web / "consent.js").read_text(encoding="utf-8")
    assert "const ACCOUNT_TABS = ['orders', 'quotes', 'security'];" in App
    assert "if (v === 'account-order') return BASE + '/account/orders/' + encodeURIComponent(this.orderRef);" in App
    assert "location.hash === '#account' ? { tab: 'orders' }" in App                    # the link of emails sent before
    assert "const ACCOUNT_RETURN_MS = 30 * 60 * 1000;" in App and "ORDER_REF.test(ref)" in App
    for Href in ("{{BASE}}/account/orders", "{{BASE}}/account/quotes", "{{BASE}}/account/security"):
        assert f'href="{Href}"' in Page, Href
    assert Page.count("Back to My Orders</a>") == 2 and "We can’t find this order in your account." in Page
    assert '@click="confirmOrder()"' in Admin and "Confirm order</button>" in Admin
    assert "'$1order')" in Consent and "'$1order')" in App                                # no Order ID to the analytics tools
