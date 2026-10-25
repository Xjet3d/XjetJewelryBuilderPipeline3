"""An explicit Test order (the owner's decision of 2026-10-16, item 6): an Admin marks an order Test — never an address
alone — with a reason kept in its history, before it is sent to Production and only while it is unpaid; since R-47
(2026-10-26) a QA order placed from a customer account too, the Admin warned that its customer is not internal. Fulfillment then receives `order.test` (it may be shipped and delivered like
any Test order); the order is kept out of the dashboard's figures; Fulfillment's milestones never e-mail its customer;
the customer's own view shows nothing of the marking."""

from p3.settings import NotificationMilestones as AllMilestones
from tests.test_fulfillment_bridge import Admin, Prefix, Service, _Enrolled, _Event, _On, _Prepared, _Send
from tests.test_orders import Customer, _InBag, _Order

Internal = {**Customer, "email": "qa.trials@xjet3d.com"}


async def _Internal(H, Click: str, Who=Internal) -> tuple[str, dict]:
    Did, _Cand, _ = await _InBag(H, "Twisted rope band", Size=7, Material="silver", Qty=1)
    R = await H.Client.post("/api/orders", json=_Order(client_request_id=Click, customer=Who))
    assert R.status_code == 200, R.text
    return Did, R.json()


async def _Admin(H, OrderId: str) -> dict:
    R = await H.Client.get(f"/api/admin/orders/{OrderId}", headers=Admin)
    assert R.status_code == 200, R.text
    return R.json()


async def _Mark(H, OrderId: str, Test: bool, Reason: str = "internal trial of the whole walk"):
    return await H.Client.post(f"/api/admin/orders/{OrderId}/test", json={"test": Test, "reason": Reason}, headers=Admin)


async def test_an_internal_order_is_test_only_once_an_admin_marks_it_and_fulfillment_receives_it(tmp_path):
    H = _On(tmp_path)
    try:
        Did, O = await _Internal(H, "t-1")
        A = await _Admin(H, O["id"])
        assert A["test"] is False and A["test_marked"] is None and A["test_marking"] == {"possible": True, "why": None, "internal": True}    # an internal address alone is not Test
        R = await _Mark(H, O["id"], True, Reason="")
        assert R.status_code == 400 and R.json()["error"]["code"] == "reason_required"
        R = await _Mark(H, O["id"], True)
        assert R.status_code == 200, R.text
        A = R.json()
        assert A["test"] is True and A["test_marked"]["reason"] == "internal trial of the whole walk" and A["test_marked"]["by"]
        Ev = H.Ctx.Db.One("SELECT kind, data_json, by FROM order_events WHERE order_id = ? ORDER BY id DESC LIMIT 1", (O["id"],))
        assert Ev["kind"] == "test_marked" and "internal trial" in Ev["data_json"] and Ev["by"]
        assert (await _Mark(H, O["id"], True)).json()["test"] is True                                        # the same again: once
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM order_events WHERE order_id = ? AND kind = 'test_marked'", (O["id"],))["n"] == 1
        # The customer's own view: the order, never who marked it or why
        Mine = (await H.Client.get(f"/api/orders/{O['id']}")).json()
        assert Mine["test"] is False and "test_marked" not in Mine and "internal trial" not in str(Mine)
        # Out of the dashboard's figures (the summary behind them; this suite's designs are mock ones, so they are counted here)
        Figures = H.Svc.Orders.Summary(ExcludeMock=False)
        assert Figures["test_orders"] == 1 and Figures["orders"] == 0
        # Unmarked again before the hand-over, with a reason
        R = await _Mark(H, O["id"], False, Reason="the trial uses another order")
        assert R.status_code == 200 and R.json()["test"] is False and R.json()["test_marked"] is None
        assert (await _Mark(H, O["id"], True)).status_code == 200
        # Sent to Production: Fulfillment receives it as a Test order; from now on the marking is fixed
        await _Prepared(H, Did, O["id"])
        await _Send(H, O["id"])
        Snap = (await H.Client.get(Prefix + f"/requests/order/{O['id']}", headers=Service)).json()
        assert Snap["order"]["test"] is True
        Rows = (await H.Client.get(f"/api/admin/production?order_id={O['id']}", headers=Admin)).json()      # the Production page's Test badge
        assert Rows["rows"] and all(R["order"]["test"] is True for R in Rows["rows"])
        R = await _Mark(H, O["id"], False, Reason="trying to change it after the hand-over")
        assert R.status_code == 409 and R.json()["error"]["code"] == "test_marking_not_allowed" and "sent to Production" in R.json()["error"]["message"]
        assert (await _Admin(H, O["id"]))["test_marking"]["possible"] is False
    finally:
        await H.Close()


async def test_a_qa_order_from_a_customer_account_can_be_marked_with_a_reason_a_paid_one_never(tmp_path):
    H = _On(tmp_path)
    try:
        _Did, Qa = await _Internal(H, "t-real", Who=Customer)                                               # a customer account: dana@atelier-customer.co.il
        A = await _Admin(H, Qa["id"])
        assert A["test_marking"] == {"possible": True, "why": None, "internal": False}                     # the Admin is warned (R-47)
        assert (await _Mark(H, Qa["id"], True, Reason="")).status_code == 400                               # never without a reason
        R = await _Mark(H, Qa["id"], True, Reason="QA run from a customer account (R-47)")
        assert R.status_code == 200 and R.json()["test"] is True and R.json()["test_marked"]["reason"] == "QA run from a customer account (R-47)"
        Ev = H.Ctx.Db.One("SELECT kind, by FROM order_events WHERE order_id = ? ORDER BY id DESC LIMIT 1", (Qa["id"],))
        assert Ev["kind"] == "test_marked" and Ev["by"]
        _Did, Paid = await _Internal(H, "t-paid")
        assert (await H.Client.post(f"/api/admin/orders/{Paid['id']}/payment", json={"status": "paid", "note": "transfer"}, headers=Admin)).status_code == 200
        R = await _Mark(H, Paid["id"], True)
        assert R.status_code == 409 and "paid" in R.json()["error"]["message"]
        _Did, Reserved = await _Internal(H, "t-reserved", Who={**Customer, "email": "trial@example.com"})   # Test by its address already
        A = await _Admin(H, Reserved["id"])
        assert A["test"] is True and A["test_marking"]["possible"] is False
    finally:
        await H.Close()


async def test_fulfillment_milestones_never_email_a_marked_test_order(tmp_path):
    H = _On(tmp_path, FulfillmentCustomerNotifications=True, FulfillmentNotificationMilestones=AllMilestones, FulfillmentNotificationWindowS=0)
    try:
        Did, O = await _Internal(H, "t-n")
        assert (await _Mark(H, O["id"], True)).status_code == 200
        await _Enrolled(H, Did, O)
        Did2, Real = await _Internal(H, "t-n2")                                                             # the same customer, not marked
        await _Enrolled(H, Did2, Real)
        for Oid, Id in ((O["id"], "n-1"), (Real["id"], "n-2")):
            R = await H.Client.post(Prefix + "/events", headers=Service, json={"events": [
                _Event(Id, Oid, 5, "fulfillment.production_started", customer_status="In production", order_status="production")]})
            assert R.status_code == 200 and R.json()["results"][0]["status"] == "applied", R.text
        States = {R["order_id"]: R["state"] for R in H.Ctx.Db.All("SELECT order_id, state FROM fulfillment_notification_intents")}
        assert States == {O["id"]: "suppressed", Real["id"]: "pending"}
    finally:
        await H.Close()
