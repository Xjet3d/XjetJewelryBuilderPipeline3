"""Test records and the hand-over in words (the owner's UI/UX request of 2026-10-13, §1, §4–§6; p3/testrecords.py).

An order whose customer is at a reserved test domain is marked Test on the Admin pages and in the snapshot Fulfillment
reads, sends its customer no email (the staff still learn of it, marked [TEST]), is kept out of the dashboard's
figures and counted apart, and its Fulfillment notifications are recorded suppressed — a real customer's order is
untouched. A sent line says whether Fulfillment has confirmed it, then names its part and links to it; the line's
timeline keeps the synchronisation's technical entries apart (shown under "Detailed history")."""

from p3.testrecords import IsTestCustomer, ReservedEmail
from tests.test_fulfillment_bridge import Admin, Prefix, Service, _Event, _On, _Prepared, _Send
from tests.test_orders import Customer, _InBag, _Order

Synthetic = {**Customer, "email": "synthetic-pilot-test@example.invalid"}
Real = {**Customer, "email": "dana@atelier-customer.co.il"}


async def _PlacedAs(H, Who: dict, Click: str, Prompt: str = "Twisted rope band"):
    Did, _Cand, _ = await _InBag(H, Prompt, Qty=1)
    R = await H.Client.post("/api/orders", json=_Order(customer=Who, client_request_id=Click))
    assert R.status_code == 200, R.text
    return Did, R.json()


def test_the_reserved_domains_are_the_test_customers():
    for Email in ("synthetic-pilot-test@example.invalid", "dana@example.com", "X@Example.ORG", "a@shop.test", "a@localhost", "a@x.example"):
        assert ReservedEmail(Email), Email
    for Email in ("dana@atelier-customer.co.il", "x@example.com.au", "x@notexample.com", "x@test.com", None, "", "no-at-sign"):
        assert not ReservedEmail(Email), Email
    assert IsTestCustomer({"email": "a@x.localhost"}) and not IsTestCustomer({}) and not IsTestCustomer(None)


async def test_a_test_order_is_marked_never_emails_its_customer_and_stays_out_of_the_figures(tmp_path):
    H = _On(tmp_path, StaffNotifyEmails=("ops@xjet3d.com",), FulfillmentCustomerNotifications=True, FulfillmentUrl="https://tron/atelier-fulfillment")
    try:
        DidT, T = await _PlacedAs(H, Synthetic, "click-t")
        DidR, R = await _PlacedAs(H, Real, "click-r", "Plain band")
        assert T["test"] is True and R["test"] is False
        assert T["confirmation_email"]["status"] == "skipped" and R["confirmation_email"]["status"] != "skipped"
        Mails = H.App.state.Mailer.List()
        assert [M["to"] for M in Mails if M["to"] == Synthetic["email"]] == []                          # the test customer: never emailed
        assert [M["to"] for M in Mails if M["to"] == Real["email"]] == [Real["email"]]                  # the real customer: as before
        Staff = sorted(M["subject"] for M in Mails if M["to"] == "ops@xjet3d.com")
        assert len(Staff) == 2 and sum(S.startswith("[TEST] ") for S in Staff) == 1                    # the staff learn of both, the test one marked
        Kinds = [E["kind"] for E in H.Svc.Orders.AdminGet(T["id"])["events"]]
        assert "email_skipped" in Kinds and "email" not in Kinds
        # The Admin's list and the dashboard: marked, and counted apart from the figures
        Listed = {X["ref"]: X for X in (await H.Client.get("/api/admin/orders", headers=Admin)).json()["orders"]}
        assert Listed[T["ref"]]["test"] is True and Listed[R["ref"]]["test"] is False
        S = H.Svc.Orders.Summary(ExcludeMock=False)
        assert S["orders"] == 1 and S["test_orders"] == 1 and [X["ref"] for X in S["latest"]] == [R["ref"]]
        # Both go to Production: the snapshot carries Atelier's verdict for Fulfillment
        await _Prepared(H, DidT, T["id"])
        St = await _Send(H, T["id"])
        await _Prepared(H, DidR, R["id"])
        await _Send(H, R["id"])
        assert (await H.Client.get(Prefix + f"/requests/order/{T['id']}", headers=Service)).json()["order"]["test"] is True
        assert (await H.Client.get(Prefix + f"/requests/order/{R['id']}", headers=Service)).json()["order"]["test"] is False
        # The hand-over in words: sent and waiting for Fulfillment — no part number, no link yet
        Line = St["lines"][0]
        assert Line["production"]["state"] == "sent" and Line["production"]["part_display_no"] is None and Line["fulfillment_url"] is None
        # Fulfillment reports the parts: received, with the part number and the link; the milestones become intents —
        # the test order's suppressed (never sent), the real one's pending
        for O, No, Ev in ((T, "MP-10001-001", "t"), (R, "MP-10002-001", "r")):
            L0 = H.Svc.Orders.AdminGet(O["id"])["lines"][0]
            Parts = [{"display_no": No, "line_id": L0["id"], "part_id": "mp_" + Ev, "customer_stage": "Preparing for production"}]
            Res = (await H.Client.post(Prefix + "/events", json={"events": [
                _Event(Ev + "1", O["id"], 1, parts=Parts),
                _Event(Ev + "2", O["id"], 2, "fulfillment.review_approved", customer_status="Preparing for production", parts=Parts,
                       review={"part": No, "decision": "approved", "revision_no": 1, "reason": None, "customer_message": None, "preliminary": False}),
                _Event(Ev + "3", O["id"], 3, "fulfillment.request_updated", parts=Parts)]}, headers=Service)).json()["results"]
            assert all(X["result"] == "applied" for X in Res), Res
        State = (await H.Client.get(f"/api/admin/orders/{T['id']}/fulfillment", headers=Admin)).json()
        L = State["lines"][0]
        assert L["production"]["state"] == "received" and L["production"]["part_display_no"] == "MP-10001-001"
        assert L["fulfillment_url"] == "https://tron/atelier-fulfillment/#/orders/mp_t"
        Intents = {I["order_id"]: I for I in (await H.Client.get("/api/admin/fulfillment/notifications", headers=Admin)).json()["intents"]}
        assert Intents[T["id"]]["state"] == "suppressed" and "test order" in Intents[T["id"]]["note"]
        assert Intents[R["id"]]["state"] == "pending"
        # The line's timeline: Fulfillment's generic "Updated" is a technical entry, kept apart, never deleted
        Detail = (await H.Client.get(f"/api/admin/production/{L['line_id']}", headers=Admin)).json()
        Kinds = {X["kind"]: X["significant"] for X in Detail["timeline"]}
        assert Kinds["fulfillment.request_updated"] is False and Kinds["fulfillment.review_approved"] is True and Kinds["production_sent"] is True
    finally:
        await H.Close()
