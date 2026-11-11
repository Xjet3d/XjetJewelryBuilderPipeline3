"""Audit B2 (SEC-04): SMTP_STARTTLS=true encrypts the relay connection with the certificate and the relay's name
verified; a relay that cannot do it gets nothing (no plain-text fallback); off by default, the P2 path is unchanged."""

import smtplib
import ssl

import pytest

from p3 import mail as Mail
from p3.mail import MailNotAccepted, RelaySmtp, SmtpMailer


def Fake(Calls, StartTlsError=None):
    class FakeSmtp:
        def __init__(self, Host, Port, timeout=30):
            Calls.append(("connect", Host, Port))

        def __enter__(self):
            return self

        def __exit__(self, *A):
            return False

        def starttls(self, context=None):
            Calls.append(("starttls", context))
            if StartTlsError:
                raise StartTlsError

        def send_message(self, Msg, from_addr=None):
            Calls.append(("send", Msg["To"]))
            return {}
    return FakeSmtp


@pytest.fixture
def Env(monkeypatch):
    monkeypatch.setenv("SMTP_SERVER", "relay.example.test")
    monkeypatch.setattr(Mail.socket, "getaddrinfo", lambda *A, **K: [(None, None, None, None, ("52.0.0.1", 0))])
    return monkeypatch


def test_starttls_verifies_the_relays_certificate_and_name_before_sending(Env, tmp_path):
    Env.setenv("SMTP_STARTTLS", "true")
    Calls = []
    Env.setattr(Mail, "RelaySmtp", Fake(Calls))
    M = SmtpMailer(Record=Mail.OutboxMailer(tmp_path / "outbox"))
    M.Send("dana@example.com", "s", "<p>x</p>")
    assert [C[0] for C in Calls] == ["connect", "starttls", "send"]
    assert Calls[0][1:] == ("relay.example.test", 25)               # the name, so the certificate is checked against it
    Ctx = Calls[1][1]
    assert isinstance(Ctx, ssl.SSLContext) and Ctx.check_hostname and Ctx.verify_mode == ssl.CERT_REQUIRED
    assert M.List()[0]["delivery"] == "smtp accepted by relay.example.test:25 over TLS"


@pytest.mark.parametrize("Error", [smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server."),
                                   ssl.SSLCertVerificationError("certificate verify failed")])
def test_a_relay_without_working_tls_gets_nothing_in_plain_text(Env, tmp_path, Error):
    Env.setenv("SMTP_STARTTLS", "true")
    Calls = []
    Env.setattr(Mail, "RelaySmtp", Fake(Calls, StartTlsError=Error))
    M = SmtpMailer(Record=Mail.OutboxMailer(tmp_path / "outbox"))
    with pytest.raises(MailNotAccepted):                             # a known non-delivery, before any data left
        M.Send("dana@example.com", "s", "<p>x</p>")
    assert "send" not in [C[0] for C in Calls]
    assert M.List()[0]["delivery"].startswith("smtp FAILED (connect)")


def test_off_by_default_the_relay_is_used_as_before(Env, tmp_path):
    Env.delenv("SMTP_STARTTLS", raising=False)
    Calls = []
    Env.setattr(Mail.smtplib, "SMTP", Fake(Calls, StartTlsError=AssertionError("starttls must not be called")))
    M = SmtpMailer(Record=Mail.OutboxMailer(tmp_path / "outbox"))
    M.Send("dana@example.com", "s", "<p>x</p>")
    assert [C[0] for C in Calls] == ["connect", "send"] and Calls[0][1] == "52.0.0.1"
    assert M.List()[0]["delivery"] == "smtp accepted by relay.example.test:25"


def test_the_relay_connection_uses_ipv4_and_keeps_the_name_for_tls(monkeypatch):
    Seen = {}
    monkeypatch.setattr(Mail.socket, "getaddrinfo", lambda Host, Port, Family, Type: Seen.update(lookup=(Host, Family)) or
                        [(None, None, None, None, ("52.0.0.1", Port))])
    monkeypatch.setattr(Mail.socket, "create_connection", lambda Addr, Timeout, Source: Seen.update(addr=Addr) or "sock")
    S = RelaySmtp()                                                  # not connected yet
    S._host = "relay.example.test"
    assert S._get_socket("relay.example.test", 25, 5) == "sock"
    assert Seen == {"lookup": ("relay.example.test", Mail.socket.AF_INET), "addr": ("52.0.0.1", 25)}
    assert S._host == "relay.example.test"                           # what starttls() checks the certificate against
