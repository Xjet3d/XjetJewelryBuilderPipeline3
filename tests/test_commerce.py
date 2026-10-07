"""Commerce truths: an order or a quote request says whether its confirmation email really went out; the emails
never say "reply to this email" at a no-reply address; staff are told about new orders when recipients are
configured; a double click adds one bag line; production statuses need complete 3D results; the STL is prepared
from the ordered option only; the support address is configured, never invented."""

import pytest

from p3 import mail as Mail
from tests.conftest import Harness

AdminKey = "commerce-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Customer = {"first_name": "Dana", "last_name": "Levi", "email": "dana@example.com", "phone": "+972 54 123 4567"}
Address = {"recipient": "Dana Levi", "line1": "12 Rothschild Blvd", "line2": "", "city": "Tel Aviv", "region": "",
           "postal_code": "6688112", "country": "IL"}


class _Smtp:
    """A stand-in for the SMTP mailer: mail "leaves the server" (or fails) without any network."""
    Mode = "smtp"

    def __init__(self, Fail=False):
        self.Sent, self.Fail = [], Fail

    def Send(self, To, Subject, Html):
        if self.Fail:
            raise RuntimeError("relay down")
        self.Sent.append((To, Subject, Html))
        return "<id@test>"

    def List(self, Limit=50):
        return [{"to": T, "subject": S, "html": H} for T, S, H in self.Sent]


async def _InBag(H, Prompt="Twisted rope band"):
    Batch = await H.NewDesign(Prompt)
    Cand = Batch["candidates"][0]
    Cus = (await H.Client.post(f"/api/designs/{Batch['design_id']}/customize", json={"candidate_id": Cand["id"]})).json()
    await H.Idle()
    await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"ring_size": 7, "material_id": "silver"})
    R = await H.Client.post("/api/bag", json={"customization_id": Cus["id"]})
    assert R.status_code == 200, R.text
    return Batch, Cand, Cus


def _Order(**Over):
    return {"customer": Customer, "address": Address, "shipping_method": "standard", "terms_accepted": True,
            "client_request_id": "click-1", **Over}


async def test_the_order_says_whether_its_confirmation_email_really_went_out(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _InBag(H)
        O = (await H.Client.post("/api/orders", json=_Order())).json()
        # The outbox mode keeps a copy and sends nothing: the customer is not told an email was sent
        assert O["confirmation_email"] == {"status": "not_sent", "to": "dana@example.com"}
        assert (await H.Client.get(f"/api/orders/{O['id']}")).json()["confirmation_email"]["status"] == "not_sent"
        # Through SMTP the answer is "sent" once the mail left — or "failed", recorded on the order
        H.Svc.Orders.Mailer = _Smtp()
        await _InBag(H, "Second band")
        O2 = (await H.Client.post("/api/orders", json=_Order(client_request_id="click-2"))).json()
        assert (await H.Client.get(f"/api/orders/{O2['id']}")).json()["confirmation_email"]["status"] == "sent"
        assert [T for T, _S, _B in H.Svc.Orders.Mailer.Sent] == ["dana@example.com"]
        H.Svc.Orders.Mailer = _Smtp(Fail=True)
        await _InBag(H, "Third band")
        O3 = (await H.Client.post("/api/orders", json=_Order(client_request_id="click-3"))).json()
        assert (await H.Client.get(f"/api/orders/{O3['id']}")).json()["confirmation_email"]["status"] == "failed"
        D = (await H.Client.get(f"/api/admin/orders/{O3['id']}", headers=Admin)).json()
        assert "email_failed" in [E["kind"] for E in D["events"]]
    finally:
        await H.Close()


async def test_staff_are_told_about_new_orders_and_quote_requests_when_recipients_are_configured(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey, StaffNotifyEmails=("ops@example.com", "sales@example.com"))
    try:
        Batch, Cand, _Cus = await _InBag(H)
        O = (await H.Client.post("/api/orders", json=_Order())).json()
        Box = H.App.state.Mailer.List()
        Staff = [M for M in Box if M["to"] in ("ops@example.com", "sales@example.com")]
        assert len(Staff) == 2 and all(O["ref"] in M["subject"] and "dana@example.com" in M["html"] for M in Staff)
        assert all("cost" not in M["html"].lower() for M in Staff)
        D = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()
        assert [E["kind"] for E in D["events"]].count("staff_notified") == 2
        R = await H.Client.post("/api/quote-requests", json={"design_id": Batch["design_id"], "candidate_id": Cand["id"],
                                                             "material_id": "gold_18k_yellow", "ring_size": 7, "quantity": 1,
                                                             "customer": Customer, "message": "Gold please"})
        assert R.status_code == 200, R.text
        assert R.json()["email_status"] == "not_sent"                       # the outbox sends nothing
        Box = H.App.state.Mailer.List()
        assert any(M["to"] == "ops@example.com" and R.json()["ref"] in M["subject"] for M in Box)
    finally:
        await H.Close()


def test_the_emails_say_how_to_get_in_touch_and_never_ask_for_a_reply_to_no_reply(monkeypatch):
    monkeypatch.delenv("MAIL_REPLY_TO", raising=False)
    monkeypatch.delenv("P3_SUPPORT_EMAIL", raising=False)
    assert Mail.ContactLine("ORD-1") == "Questions? Use the Contact page on the site and quote ORD-1."
    monkeypatch.setenv("P3_SUPPORT_EMAIL", "help@example.com")
    assert Mail.ContactLine("ORD-1") == "Questions? Email help@example.com and quote ORD-1."
    monkeypatch.setenv("MAIL_REPLY_TO", "atelier@example.com")
    assert "atelier@example.com" in Mail.ContactLine("Q-1")
    assert Mail.SmtpMailer().ReplyTo == "atelier@example.com"
    monkeypatch.delenv("MAIL_REPLY_TO")
    assert Mail.SmtpMailer().ReplyTo.startswith("no-reply@")
    Order = {"ref": "ORD-9", "customer": {"first_name": "Dana"}, "lines": [], "subtotal": 0, "discount": 0, "shipping": 0, "total": 0,
             "currency": "USD", "address_lines": ["x"], "payment_label": "Payment pending", "payment_message": "",
             "shipping_label": "Standard delivery", "shipping_eta": "", "promo_code": None}
    assert "Reply to this email" not in Mail.OrderConfirmationEmail(Order)[1]


async def test_a_double_click_adds_one_bag_line_and_the_support_address_is_never_invented(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        _Batch, _Cand, Cus = await _InBag(H)
        Again = (await H.Client.post("/api/bag", json={"customization_id": Cus["id"]})).json()
        assert len(Again["lines"]) == 1
        Page = (await H.Client.get("/")).text
        assert '"support_email": "atelier@xjet3d.com"' in Page                 # development: the Pipeline 2 address
    finally:
        await H.Close()
    H = Harness(tmp_path / "b", AdminKey=AdminKey, SupportEmail="help@example.com")
    try:
        assert '"support_email": "help@example.com"' in (await H.Client.get("/")).text
    finally:
        await H.Close()


async def test_production_needs_complete_3d_results_and_the_stl_comes_from_the_ordered_option(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Batch, Cand, _Cus = await _InBag(H)
        O = (await H.Client.post("/api/orders", json=_Order())).json()
        Line = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()["lines"][0]
        # No model yet: the STL cannot be prepared; a model of ANOTHER option does not count for this line
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{Line['id']}/3d", headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "no_model"
        H.Svc.Meshes.Create(Batch["candidates"][1]["id"], None)              # a model of option B, not the ordered A
        await H.Idle()
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{Line['id']}/3d", headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "model_of_other_option"
        assert "R-1001-B" in R.json()["error"]["message"] and "R-1001-A" in R.json()["error"]["message"]
        # Production is refused while the line has no complete 3D result; an exception needs a note and is recorded as one
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": "production", "note": "go"}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "three_d_unresolved"
        assert (await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": "production", "force": True}, headers=Admin)).status_code == 400
        D = (await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": "production", "force": True, "note": "made by hand"},
                                 headers=Admin)).json()
        assert D["status"] == "production"
        assert any(E["kind"] == "status" and "exception" in (E["data"].get("note") or "") for E in D["events"])
        assert (await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": "three_d_ready", "note": ""}, headers=Admin)).status_code == 200
    finally:
        await H.Close()
