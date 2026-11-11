"""A1 (11 Nov 2026): every email can be followed. The relay's answer to the message data (Microsoft 365: "250 2.6.0
<id> [InternalId=…] Queued mail for delivery") and the Message-ID are kept with the delivery record and on the order
event; bounces go to a real inbox; the Admin lists the recent emails with the relay's answer."""

import pytest

from p3 import mail as Mail
from p3.mail import SmtpMailer, Trace
from tests.conftest import Harness
from tests.test_orders import _InBag, _Order

AdminKey = "mail-trace-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
QueueReply = b"2.6.0 <x@xjet3d.com> [InternalId=4011499213, Hostname=AM9PR03MB0000.eurprd03.prod.outlook.com] Queued mail for delivery"


def Relay(Sent):
    """A relay that behaves like smtplib.SMTP where it matters: send_message hands the data over with data()."""
    class FakeSmtp:
        def __init__(self, Host, Port, timeout=30):
            Sent.append({"host": Host, "port": Port})

        def __enter__(self):
            return self

        def __exit__(self, *A):
            return False

        def data(self, Msg):
            Sent[-1]["data"] = True
            return 250, QueueReply

        def send_message(self, Msg, from_addr=None):
            Sent[-1].update(envelope=from_addr, header_from=Msg["From"], message_id=Msg["Message-ID"])
            self.data(Msg.as_bytes())
            return {}
    return FakeSmtp


@pytest.fixture
def Smtp(monkeypatch):
    for V in ("SMTP_STARTTLS", "MAIL_RETURN_PATH", "MAIL_REPLY_TO", "P3_SUPPORT_EMAIL", "MAIL_FROM"):
        monkeypatch.delenv(V, raising=False)
    monkeypatch.setenv("SMTP_SERVER", "relay.example.test")
    monkeypatch.setattr(Mail.socket, "getaddrinfo", lambda *A, **K: [(None, None, None, None, ("52.0.0.1", 0))])
    Sent = []
    monkeypatch.setattr(Mail.smtplib, "SMTP", Relay(Sent))
    return monkeypatch, Sent


def test_the_relays_answer_and_the_message_id_are_kept_and_bounces_reach_the_support_inbox(Smtp, tmp_path):
    Env, Sent = Smtp
    M = SmtpMailer(Record=Mail.OutboxMailer(tmp_path / "outbox"))
    Mid = M.Send("dana@atelier-customer.co.il", "Order ORD-10001 received", "<p>x</p>")
    assert Sent[0]["envelope"] == "atelier@xjet3d.com"                 # a non-delivery report reaches someone who reads it
    assert Sent[0]["header_from"] == "XJet Atelier <no-reply@xjet3d.com>" and Sent[0]["message_id"] == Mid
    Rec = M.List()[0]
    assert Rec["message_id"] == Mid
    assert Rec["delivery"].startswith("smtp accepted by relay.example.test:25 — relay: 250 2.6.0 ")
    assert "InternalId=4011499213" in Rec["delivery"] and "Queued mail for delivery" in Rec["delivery"]
    assert Trace(M, Mid) == {"message_id": Mid, "relay": Rec["delivery"]}
    assert Trace(M, Mid) == {"message_id": Mid}                       # asked once: no memory grows with the traffic
    Env.setenv("MAIL_RETURN_PATH", "bounces@xjet3d.com")
    SmtpMailer().Send("dana@atelier-customer.co.il", "s", "<p>x</p>")
    assert Sent[1]["envelope"] == "bounces@xjet3d.com"


def test_the_outbox_mailer_traces_nothing(tmp_path):
    O = Mail.OutboxMailer(tmp_path / "outbox")
    assert Trace(O, O.Send("dana@atelier-customer.co.il", "s", "<p>x</p>")) == {}


async def test_the_order_event_and_the_admin_show_what_the_relay_answered(Smtp, tmp_path):
    Env, Sent = Smtp
    Env.setenv("P3_MAIL_MODE", "smtp")
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _InBag(H)
        O = (await H.Client.post("/api/orders", json=_Order())).json()
        await H.Idle()
        A = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()
        E = next(X for X in A["events"] if X["kind"] == "email")
        Data = E.get("data") or E
        assert Data["message_id"].startswith("<") and "relay: 250 2.6.0" in Data["relay"] and "InternalId=" in Data["relay"]
        Addr = (await H.Client.get("/api/admin/mail-addresses", headers=Admin)).json()
        assert Addr["mail_mode"] == "smtp" and Addr["relay"] == {"server": "relay.example.test", "port": 25, "starttls": False,
                                                                 "return_path": "atelier@xjet3d.com"}
        D = (await H.Client.get("/api/admin/mail-deliveries", headers=Admin)).json()
        assert D["mode"] == "smtp" and D["deliveries"][0]["message_id"] == Data["message_id"]
        assert "html" not in D["deliveries"][0] and "InternalId=" in D["deliveries"][0]["delivery"]
        assert (await H.Client.get("/api/admin/mail-deliveries")).status_code in (401, 403)
    finally:
        await H.Close()
