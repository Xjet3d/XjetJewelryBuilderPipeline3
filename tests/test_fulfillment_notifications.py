"""Batch 8 (docs/FULFILLMENT.md): the customer's view of an enrolled order in My Account (the stage, the pieces, the
parcels, the milestones — never an internal note, C08) and the customer notifications derived from Fulfillment's
events: off by default (intents recorded as suppressed, nothing sent, never sent later — D12), one email per order
and milestone (duplicates and replays make no second intent — C09; parts reaching a milestone together make one
email — C10), to the test recipient when one is set, with the delivery outcome recorded: sent, a known
non-delivery retried a few times, an unknown outcome flagged and never retried blindly (D13)."""

import json
import smtplib

from p3 import mail as Mail
from p3.mail import MailNotAccepted, MailOutcomeUnknown, SmtpMailer
from tests.conftest import Harness
from tests.test_fulfillment_bridge import Admin, AdminKey, Prefix, Service, ServiceKey, _Event, _On, _Placed


async def _Post(H, *Events):
    R = await H.Client.post(Prefix + "/events", json={"events": list(Events)}, headers=Service)
    assert R.status_code == 200, R.text
    return R.json()["results"]


def _Mails(H) -> list[dict]:
    """The Fulfillment milestone emails in the outbox, newest first (the order confirmations of the existing flow stay as they are)."""
    return [M for M in H.Ctx.Mailer.List() if any(S in (M.get("subject") or "") for S in Mail.FulfillmentSubjects.values())]


async def _Intents(H, **Q) -> dict:
    R = await H.Client.get("/api/admin/fulfillment/notifications", params=Q, headers=Admin)
    assert R.status_code == 200, R.text
    return R.json()


def _Parts(*Stages, Review=None):
    return [{"display_no": f"MP-10001-{I:03d}", "customer_stage": S, "status": "x", "summary": f"0/1 passed QC", "review_status": "approved",
             "review": Review if I == 1 else None, "file_state": "ready", "blockers": [], "cancellation_state": "none",
             "quantities": {"target": 1, "passed": 0, "shipped": 0}, "commercial_action_pending": False} for I, S in enumerate(Stages, start=1)]


Shipment = {"display_no": "S-0001", "state": "SHIPPED", "carrier": "DHL Express", "tracking_no": "JD0146", "tracking_url": "https://www.dhl.com/track?id=JD0146",
            "packed_at": "2026-10-13T08:00:00.000+00:00", "shipped_at": "2026-10-13T09:00:00.000+00:00", "delivered_at": None, "delivery_source": None,
            "alternative_delivery": False, "units": 1, "items": [{"part": "MP-10001-001", "quantity": 1}]}


async def test_off_by_default_the_customer_sees_the_stage_and_the_parcels_and_no_email_is_intended_for_sending(tmp_path):
    H = _On(tmp_path)                                                                                  # the integration on, the notifications off (the default)
    try:
        _Did, O = await _Placed(H, Qty=1)
        _Did2, Other = await _Placed(H, "Plain band", Click="click-2")                                 # a second order, never enrolled
        await H.Client.post(f"/api/admin/orders/{O['id']}/fulfillment/enroll", headers=Admin)
        Msg = "Please confirm the ring size: the band looks like a 6.5 on the photo."
        await _Post(H, _Event("e1", O["id"], 1, parts=_Parts("Under review")),
                    _Event("e2", O["id"], 2, "fulfillment.review_changes_required", customer_status="Changes required",
                           parts=_Parts("Changes required", Review={"decision": "changes_required", "reason": "Size", "customer_message": Msg, "preliminary": False}),
                           review={"part": "MP-10001-001", "decision": "changes_required", "revision_no": 1, "reason": "Size", "customer_message": Msg, "preliminary": False}),
                    _Event("e3", O["id"], 3, "fulfillment.review_approved", customer_status="Preparing for production", parts=_Parts("Preparing for production"),
                           review={"part": "MP-10001-001", "decision": "approved", "revision_no": 2, "reason": None, "customer_message": None, "preliminary": False}),
                    _Event("e4", O["id"], 4, "fulfillment.production_started", customer_status="In production", order_status="production", parts=_Parts("In production")),
                    _Event("e5", O["id"], 5, "fulfillment.shipment_shipped", customer_status="Shipped", order_status="shipped", parts=_Parts("Shipped"),
                           shipment=Shipment, shipments=[Shipment]))
        # My Account: the stage, the pieces, the parcel with its tracking, the milestones — and nothing internal (C08)
        Orders = {X["ref"]: X for X in (await H.Client.get("/api/orders")).json()["orders"]}
        F = Orders[O["ref"]]["fulfillment"]
        assert Orders[O["ref"]]["fulfillment_stage"] == "Shipped" and F["stage"] == "Shipped" and F["parts"] == [{"display_no": "MP-10001-001", "stage": "Shipped"}]
        assert [T["label"] for T in F["timeline"]] == ["Received for production review", "Changes required", "Approved for production", "In production", "Shipped"]
        assert F["shipments"][0]["tracking_url"] == Shipment["tracking_url"] and F["shipments"][0]["carrier"] == "DHL Express" and F["message"] is None
        assert Orders[O["ref"]]["status"] == "shipped"                                                 # the projection moved the order along
        assert "fulfillment" not in Orders[Other["ref"]] and "fulfillment_stage" not in Orders[Other["ref"]]   # untouched
        Text = json.dumps(Orders[O["ref"]])
        for Forbidden in ("internal_note", "reason_code", "blockers", "summary", "priority", "passed QC"):
            assert Forbidden not in Text, Forbidden
        One = (await H.Client.get(f"/api/orders/{O['id']}")).json()
        assert One["fulfillment"]["stage"] == "Shipped"
        # The milestones became intents — suppressed, since sending is off; nothing left the server; the dispatcher does nothing
        N = await _Intents(H)
        assert N["enabled"] is False and N["counts"] == {"suppressed": 4} and {I["milestone"] for I in N["intents"]} == {"changes_required", "review_approved", "production_started", "shipped"}
        assert _Mails(H) == []
        R = (await H.Client.post("/api/admin/fulfillment/notifications/dispatch", headers=Admin)).json()
        assert R["sent"] == 0 and "off" in R["skipped"]
        Card = (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()
        assert [X["state"] for X in Card["notifications"]] == ["suppressed"] * 4
        # A customer message for a change is shown to the customer (it is the customer-facing text by contract)
        await _Post(H, _Event("e6", O["id"], 6, "fulfillment.review_changes_required", customer_status="Changes required",
                              parts=_Parts("Changes required", Review={"decision": "changes_required", "reason": "Size", "customer_message": Msg, "preliminary": False}),
                              review={"part": "MP-10001-001", "decision": "changes_required", "revision_no": 3, "reason": "Size", "customer_message": Msg, "preliminary": False}))
        F = (await H.Client.get(f"/api/orders/{O['id']}")).json()["fulfillment"]
        assert F["message"] == Msg and F["timeline"][-1]["label"] == "Changes required"
    finally:
        await H.Close()


async def test_on_one_email_per_order_and_milestone_deduplicated_coalesced_and_to_the_test_recipient(tmp_path):
    H = _On(tmp_path / "a", FulfillmentCustomerNotifications=True, FulfillmentNotificationWindowS=0, PublicBaseUrl="https://atelier.example")
    try:
        _Did, O = await _Placed(H, Qty=2)
        await H.Client.post(f"/api/admin/orders/{O['id']}/fulfillment/enroll", headers=Admin)
        Email = O["customer"]["email"]
        Approved = lambda Part, Rev: {"part": Part, "decision": "approved", "revision_no": Rev, "reason": None, "customer_message": None, "preliminary": False}   # noqa: E731
        # Two parts approved one after the other: two intents, one email (C10); the unpaid order is told about the payment
        A3 = _Event("a3", O["id"], 3, "fulfillment.review_approved", customer_status="Preparing for production", parts=_Parts("Preparing for production", "Preparing for production"), review=Approved("MP-10001-002", 1))
        await _Post(H, _Event("a1", O["id"], 1, parts=_Parts("Under review", "Under review")),
                    _Event("a2", O["id"], 2, "fulfillment.review_approved", customer_status="In fulfillment", parts=_Parts("Preparing for production", "Under review"), review=Approved("MP-10001-001", 1)),
                    A3)
        assert (await _Intents(H))["counts"] == {"pending": 2}
        R = (await H.Client.post("/api/admin/fulfillment/notifications/dispatch", headers=Admin)).json()
        assert R == {"sent": 1, "failed": 0, "unknown": 0, "coalesced": 1, "pending": 0}
        Sent = _Mails(H)
        assert len(Sent) == 1 and Sent[0]["to"] == Email and Sent[0]["subject"] == f"Order {O['ref']} — approved for production"
        Html = Sent[0]["html"]
        assert "approved it" in Html and "payment is confirmed" in Html and "MP-10001-001: Preparing for production" in Html and "https://atelier.example/#account" in Html
        States = {I["event_id"]: I["state"] for I in (await _Intents(H))["intents"]}
        assert States == {"a2": "coalesced", "a3": "sent"}
        assert H.Ctx.Db.One("SELECT data_json FROM order_events WHERE order_id = ? AND kind = 'email' ORDER BY id DESC LIMIT 1", (O["id"],))["data_json"].count("fulfillment_review_approved") == 1
        # The same event again (a replay), and a new event for the same part and revision: no second intent (C09)
        Res = await _Post(H, A3, _Event("a4", O["id"], 4, "fulfillment.review_approved", customer_status="Preparing for production", review=Approved("MP-10001-002", 1)))
        assert [X["status"] for X in Res] == ["duplicate", "applied"] and (await _Intents(H))["counts"] == {"sent": 1, "coalesced": 1}
        # A new revision of a part approved again is a genuine update: one more intent, one more email
        await _Post(H, _Event("a5", O["id"], 5, "fulfillment.review_approved", customer_status="Preparing for production", review=Approved("MP-10001-002", 2)))
        assert (await H.Client.post("/api/admin/fulfillment/notifications/dispatch", headers=Admin)).json()["sent"] == 1
        # Two builds start one after the other (one event per order per build): one intent, one email
        await H.Client.post(f"/api/admin/orders/{O['id']}/payment", json={"status": "paid", "note": "transfer"}, headers=Admin)
        await _Post(H, _Event("a6", O["id"], 6, "fulfillment.production_started", customer_status="In production", order_status="production", parts=_Parts("In production", "Preparing for production")),
                    _Event("a7", O["id"], 7, "fulfillment.production_started", customer_status="In production", order_status="production", parts=_Parts("In production", "In production")))
        assert (await _Intents(H, state="pending"))["counts"]["pending"] == 1
        assert (await H.Client.post("/api/admin/fulfillment/notifications/dispatch", headers=Admin)).json()["sent"] == 1
        assert "is in production" in _Mails(H)[0]["html"]
        # Shipped: the tracking link; a tracking correction after dispatch is a new update; delivered
        await _Post(H, _Event("a8", O["id"], 8, "fulfillment.shipment_shipped", customer_status="Shipped", order_status="shipped", parts=_Parts("Shipped", "Shipped"), shipment=Shipment, shipments=[Shipment]))
        assert (await H.Client.post("/api/admin/fulfillment/notifications/dispatch", headers=Admin)).json()["sent"] == 1
        Html = _Mails(H)[0]["html"]
        assert "on its way" in Html and Shipment["tracking_url"] in Html and "DHL Express" in Html and "MP-10001-001 ×1" in Html
        await _Post(H, _Event("a9", O["id"], 9, "fulfillment.shipment_updated", customer_status="Shipped", shipment={**Shipment, "tracking_no": "JD9999"}, shipments=[{**Shipment, "tracking_no": "JD9999"}]),
                    _Event("a10", O["id"], 10, "fulfillment.shipment_delivered", customer_status="Delivered", order_status="completed",
                           shipment={**Shipment, "state": "DELIVERED", "delivered_at": "2026-10-14T10:00:00.000+00:00", "delivery_source": "carrier_confirmation"}))
        assert (await H.Client.post("/api/admin/fulfillment/notifications/dispatch", headers=Admin)).json()["sent"] == 2
        assert {M["subject"] for M in _Mails(H)[:2]} == {f"Order {O['ref']} — delivered", f"Order {O['ref']} — on its way"}
        assert (await _Intents(H))["counts"] == {"sent": 6, "coalesced": 1}
        assert (await H.Client.get(f"/api/orders/{O['id']}")).json()["fulfillment"]["stage"] == "Delivered"
    finally:
        await H.Close()
    # The test recipient: every message goes there, the order's history says so; nothing reaches the customer
    H2 = _On(tmp_path / "b", FulfillmentCustomerNotifications=True, FulfillmentNotificationWindowS=0, FulfillmentNotificationRecipient="qa@example.test")
    try:
        _Did, O = await _Placed(H2, Qty=1)
        await H2.Client.post(f"/api/admin/orders/{O['id']}/fulfillment/enroll", headers=Admin)
        await _Post(H2, _Event("b1", O["id"], 1, "fulfillment.shipment_shipped", customer_status="Shipped", order_status="shipped", shipment=Shipment, shipments=[Shipment]))
        assert (await H2.Client.post("/api/admin/fulfillment/notifications/dispatch", headers=Admin)).json()["sent"] == 1
        assert [M["to"] for M in _Mails(H2)] == ["qa@example.test"]
        I = (await _Intents(H2))["intents"][0]
        assert I["sent_to"] == "qa@example.test" and I["recipient"] == O["customer"]["email"] and (await _Intents(H2))["recipient_override"] == "qa@example.test"
        Row = H2.Ctx.Db.One("SELECT data_json FROM order_events WHERE order_id = ? AND kind = 'email' ORDER BY id DESC LIMIT 1", (O["id"],))   # the newest: ours, not the order confirmation
        assert json.loads(Row["data_json"])["redirected"] is True and json.loads(Row["data_json"])["type"] == "fulfillment_shipped"
    finally:
        await H2.Close()


async def test_d12_what_arrived_while_sending_was_off_is_never_sent_after_it_is_switched_on(tmp_path):
    Off = _On(tmp_path)
    try:
        _Did, O = await _Placed(Off, Qty=1)
        await Off.Client.post(f"/api/admin/orders/{O['id']}/fulfillment/enroll", headers=Admin)
        H1 = _Event("h1", O["id"], 1, "fulfillment.review_approved", customer_status="Preparing for production",
                    review={"part": "MP-10001-001", "decision": "approved", "revision_no": 1, "reason": None, "customer_message": None, "preliminary": False})
        await _Post(Off, H1, _Event("h2", O["id"], 2, "fulfillment.shipment_shipped", customer_status="Shipped", order_status="shipped", shipment=Shipment, shipments=[Shipment]))
        assert (await _Intents(Off))["counts"] == {"suppressed": 2}
    finally:
        await Off.Close()
    On = _On(tmp_path, FulfillmentCustomerNotifications=True, FulfillmentNotificationWindowS=0)        # the same data, sending switched on
    try:
        assert (await On.Client.post("/api/admin/fulfillment/notifications/dispatch", headers=Admin)).json()["sent"] == 0
        assert (await _Intents(On))["counts"] == {"suppressed": 2} and _Mails(On) == []
        # A replay of the history (the same event ids) changes nothing; a resynchronisation that re-sends them is harmless
        Res = await _Post(On, H1)
        assert Res[0]["status"] == "duplicate" and (await _Intents(On))["counts"] == {"suppressed": 2}
        # Only what happens from now on is sent
        await _Post(On, _Event("h3", O["id"], 3, "fulfillment.shipment_delivered", customer_status="Delivered", order_status="completed",
                               shipment={**Shipment, "state": "DELIVERED", "delivered_at": "2026-10-14T10:00:00.000+00:00", "delivery_source": "manual"}))
        assert (await On.Client.post("/api/admin/fulfillment/notifications/dispatch", headers=Admin)).json()["sent"] == 1
        assert [M["subject"] for M in _Mails(On)] == [f"Order {O['ref']} — delivered"]
    finally:
        await On.Close()


class ScriptedMailer:
    Mode = "scripted"

    def __init__(self, Outcomes):
        self.Outcomes, self.Sent = list(Outcomes), []

    def Send(self, To, Subject, Html):
        Next = self.Outcomes.pop(0) if self.Outcomes else "ok"
        if isinstance(Next, Exception):
            raise Next
        self.Sent.append({"to": To, "subject": Subject, "html": Html})
        return f"msg-{len(self.Sent)}"

    def List(self, Limit=50):
        return self.Sent


async def test_d13_a_known_failure_is_retried_an_unknown_outcome_is_flagged_and_never_retried_blindly(tmp_path):
    H = _On(tmp_path, FulfillmentCustomerNotifications=True, FulfillmentNotificationWindowS=0)
    try:
        _Did, O = await _Placed(H, Qty=1)
        await H.Client.post(f"/api/admin/orders/{O['id']}/fulfillment/enroll", headers=Admin)
        Mailer = ScriptedMailer([MailNotAccepted("connection refused"), "ok", MailOutcomeUnknown("connection lost after DATA"), MailNotAccepted("x"), MailNotAccepted("x"), MailNotAccepted("x")])
        H.Ctx.Mailer = Mailer
        Dispatch = lambda: H.Client.post("/api/admin/fulfillment/notifications/dispatch", headers=Admin)   # noqa: E731
        # A known non-delivery: the intent stays pending with its error and is sent at the next dispatch
        await _Post(H, _Event("d1", O["id"], 1, "fulfillment.production_started", customer_status="In production", order_status="production"))
        assert (await Dispatch()).json() == {"sent": 0, "failed": 0, "unknown": 0, "coalesced": 0, "pending": 1}
        I = (await _Intents(H))["intents"][0]
        assert I["state"] == "pending" and I["attempts"] == 1 and "refused" in I["last_error"] and Mailer.Sent == []
        assert (await Dispatch()).json()["sent"] == 1 and len(Mailer.Sent) == 1 and (await _Intents(H))["intents"][0]["attempts"] == 2
        # An unknown outcome: flagged, not sent again by the dispatcher; a person resolves it, or sends it again with a note
        await _Post(H, _Event("d2", O["id"], 2, "fulfillment.shipment_shipped", customer_status="Shipped", order_status="shipped", shipment=Shipment, shipments=[Shipment]))
        assert (await Dispatch()).json()["unknown"] == 1
        Unknown = next(I for I in (await _Intents(H))["intents"] if I["milestone"] == "shipped")
        assert Unknown["state"] == "unknown" and "after DATA" in Unknown["last_error"] and Unknown["attempts"] == 1
        assert (await Dispatch()).json() == {"sent": 0, "failed": 0, "unknown": 0, "coalesced": 0, "pending": 0} and Unknown["attempts"] == 1
        R = await H.Client.post(f"/api/admin/fulfillment/notifications/{Unknown['id']}/retry", json={}, headers=Admin)
        assert R.status_code == 400 and R.json()["error"]["code"] == "note_required"
        R = await H.Client.post(f"/api/admin/fulfillment/notifications/{Unknown['id']}/resolve", json={"note": "the relay log shows it was accepted at 09:01"}, headers=Admin)
        assert R.status_code == 200 and R.json()["state"] == "resolved" and "relay log" in R.json()["note"]
        assert (await H.Client.post(f"/api/admin/fulfillment/notifications/{Unknown['id']}/retry", json={"note": "x" * 10}, headers=Admin)).status_code == 409
        # Three known failures in a row: failed, kept for a person; sent again by hand
        await _Post(H, _Event("d3", O["id"], 3, "fulfillment.shipment_delivered", customer_status="Delivered", order_status="completed",
                              shipment={**Shipment, "state": "DELIVERED", "delivered_at": "2026-10-14T10:00:00.000+00:00", "delivery_source": "manual"}))
        for Expected in ("pending", "pending", "failed"):
            await Dispatch()
            assert next(I for I in (await _Intents(H))["intents"] if I["milestone"] == "delivered")["state"] == Expected
        Failed = next(I for I in (await _Intents(H))["intents"] if I["milestone"] == "delivered")
        assert Failed["attempts"] == 3
        R = await H.Client.post(f"/api/admin/fulfillment/notifications/{Failed['id']}/retry", json={}, headers=Admin)
        assert R.status_code == 200 and R.json()["state"] == "pending" and R.json()["attempts"] == 0
        assert (await Dispatch()).json()["sent"] == 1 and len(Mailer.Sent) == 2
        Card = (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()
        assert sorted(N["state"] for N in Card["notifications"]) == ["resolved", "sent", "sent"]
        assert (await H.Client.get("/api/admin/fulfillment/notifications")).status_code == 403                 # the Admin key only
    finally:
        await H.Close()


def test_the_smtp_mailer_tells_a_failure_before_acceptance_from_an_unknown_outcome(monkeypatch, tmp_path):
    """Spec §24.3: a failure to connect is a known non-delivery; a connection lost while the data was being handed
    over leaves the outcome unknown; recipients the relay refuses are a known non-delivery."""
    Calls = {}

    class FakeSmtp:
        def __init__(self, Host, Port, timeout=30):
            if Calls.get("fail") == "connect":
                raise ConnectionRefusedError("refused")

        def __enter__(self):
            return self

        def __exit__(self, *A):
            return False

        def send_message(self, Msg):
            if Calls.get("fail") == "send":
                raise smtplib.SMTPServerDisconnected("connection lost")
            return Calls.get("refused", {})

    monkeypatch.setattr(Mail.smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(Mail.socket, "getaddrinfo", lambda *A, **K: [(None, None, None, None, ("127.0.0.1", 0))])
    M = SmtpMailer(Record=Mail.OutboxMailer(tmp_path / "outbox"))
    Calls["fail"] = "connect"
    try:
        M.Send("a@example.test", "s", "<p>x</p>")
        raise AssertionError("expected MailNotAccepted")
    except MailNotAccepted:
        pass
    Calls["fail"] = "send"
    try:
        M.Send("a@example.test", "s", "<p>x</p>")
        raise AssertionError("expected MailOutcomeUnknown")
    except MailOutcomeUnknown as E:
        assert "connection lost" in str(E)
    Calls["fail"] = None
    Calls["refused"] = {"a@example.test": (550, b"no such user")}
    try:
        M.Send("a@example.test", "s", "<p>x</p>")
        raise AssertionError("expected MailNotAccepted")
    except MailNotAccepted as E:
        assert "refused" in str(E)
    Calls["refused"] = {}
    assert M.Send("a@example.test", "s", "<p>x</p>").startswith("<")                                              # accepted: the Message-ID
    Deliveries = [X["delivery"] for X in M.List()]
    assert any("FAILED (connect)" in D for D in Deliveries) and any("FAILED (send)" in D for D in Deliveries) and any("accepted" in D for D in Deliveries)
