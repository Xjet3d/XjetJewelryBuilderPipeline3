"""Registration emails (verification link, access token): the P2 flow, with one clear call to action.

Delivery is pluggable:
  * OutboxMailer (default, P3_MAIL_MODE=outbox): writes each message to var/outbox/ and never
    contacts a mail server — the right behaviour while P3 runs in mock mode. Developers read
    the messages (and click the verification links) from the /dev page.
  * SmtpMailer (P3_MAIL_MODE=smtp): the xjet3d relay, configured exactly like P2
    (SMTP_SERVER, SMTP_PORT, SMTP_PREFER_IPV4, MAIL_FROM, MAIL_FROM_NAME).
"""

import html
import json
import logging
import os
import smtplib
import socket
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

Logger = logging.getLogger("p3.mail")

_Font = "font-family:Helvetica,Arial,sans-serif;"


def _Greeting(Name: str) -> str:
    Name = (Name or "").strip()
    return f"Hi {html.escape(Name)}," if Name else "Hello,"


def _Button(Url: str, Label: str) -> str:
    """A large "bulletproof" button: the colour sits on the table cell and the padding on a block
    link, so Outlook (which ignores padding on inline links) still renders a big, clickable button."""
    return f"""
<table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="margin:8px auto 4px;">
  <tr><td align="center" bgcolor="#9A7230" style="border-radius:6px;background:#9A7230;">
    <a href="{Url}" target="_blank"
       style="display:inline-block;padding:20px 56px;{_Font}font-size:18px;font-weight:700;letter-spacing:3px;
              text-transform:uppercase;color:#FFFFFF;text-decoration:none;border-radius:6px;">
      <span style="color:#FFFFFF;">{Label}</span></a>
  </td></tr>
</table>"""


def _Layout(Heading: str, Content: str) -> str:
    return f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#FFFFFF">
  <tr><td align="center" style="padding:24px 12px;">
    <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" bgcolor="#FAF8F4"
           style="width:560px;max-width:100%;background:#FAF8F4;border:1px solid #EDE8DF;border-top:3px solid #C9A96E;">
      <tr><td style="padding:36px 40px;{_Font}color:#2A2A2A;font-size:15px;line-height:1.6;">
        <p style="margin:0 0 6px;text-transform:uppercase;letter-spacing:3px;font-size:11px;color:#9A7230;font-weight:700;">XJet Atelier</p>
        <h1 style="margin:0 0 20px;font-size:24px;font-weight:600;color:#1A1A1A;">{Heading}</h1>
{Content}
      </td></tr>
    </table>
  </td></tr>
</table>"""


def VerificationEmail(Name: str, VerifyUrl: str) -> tuple[str, str]:
    U = html.escape(VerifyUrl, quote=True)
    return ("Verify your email to continue designing — XJet Atelier", _Layout("Confirm your email", f"""\
        <p style="margin:0 0 14px;">{_Greeting(Name)}</p>
        <p style="margin:0 0 28px;">Thanks for registering at XJet Atelier. Click the button below to confirm your
           email address &mdash; you'll be signed in and taken straight to the design studio.</p>
        {_Button(U, "Start designing")}
        <p style="margin:32px 0 0;font-size:12px;color:#8F8F8F;">This link expires in 24&nbsp;hours. If you didn't
           register at XJet Atelier, you can safely ignore this email.</p>
        <p style="margin:12px 0 0;font-size:11px;color:#A0A0A0;word-break:break-all;">If the button doesn't work,
           copy this link into your browser:<br><a href="{U}" style="color:#9A7230;">{U}</a></p>"""))


def TokenEmail(Name: str, Token: str, LoginUrl: str) -> tuple[str, str]:
    U = html.escape(LoginUrl, quote=True)
    T = html.escape(Token)
    return ("Your XJet Atelier sign-in code", _Layout("You're all set", f"""\
        <p style="margin:0 0 14px;">{_Greeting(Name)}</p>
        <p style="margin:0 0 28px;">Your email is verified and your account is ready. Click the button below to
           start designing your ring &mdash; you'll be signed in automatically.</p>
        {_Button(U, "Start designing")}
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:36px;">
          <tr><td style="border-top:1px solid #E5DED0;padding-top:20px;{_Font}font-size:12px;color:#8F8F8F;line-height:1.6;">
            Signing in on another device? Enter this 6-letter sign-in code there:
            <span style="display:inline-block;margin-left:6px;padding:2px 10px;border:1px solid #C9A96E;background:#FFFFFF;
                         font-family:'Courier New',monospace;font-size:15px;font-weight:700;letter-spacing:3px;color:#1A1A1A;">{T}</span>
            <br>Keep it private &mdash; it's linked to your account and its movie allowance.
          </td></tr>
        </table>"""))


def _Money(V, Cur="USD") -> str:
    return ("$" if Cur == "USD" else Cur + " ") + f"{float(V or 0):,.2f}"


def _Esc(V) -> str:
    return html.escape(str(V if V is not None else ""))


def _IsCharm(L: dict) -> bool:
    return (L.get("product_type") or "ring") == "charm"


def _IdLabel(L: dict) -> str:
    return "Charm ID" if _IsCharm(L) else "Ring ID"


def _Size(L: dict) -> str:
    """'US 7' for a ring, '20 mm' for a charm; a line without a size says so instead of failing."""
    Value = L.get("charm_size") if _IsCharm(L) else L.get("ring_size")
    if Value is None:
        return "size to be confirmed"
    return f"{float(Value):g} mm" if _IsCharm(L) else f"US {float(Value):g}"


def OrderConfirmationEmail(Order: dict) -> tuple[str, str]:
    """Order received: what, where, totals, payment status, what happens next. Every customer value
    is escaped; nothing about production cost or 3D pricing is ever in it."""
    C = Order["customer"]
    Rows = "".join(f"""
          <tr>
            <td style="padding:8px 0;border-bottom:1px solid #EDE8DF;{_Font}font-size:14px;">
              <strong>{_Esc(L['title'])}</strong><br>
              <span style="color:#6F6F6F;font-size:12px;">{_Esc(_IdLabel(L))} {_Esc(L['ring_id'] or '—')} · {_Esc(L['material_label'])} · {_Esc(_Size(L))} · ×{_Esc(L['quantity'])}</span>
            </td>
            <td align="right" style="padding:8px 0;border-bottom:1px solid #EDE8DF;{_Font}font-size:14px;white-space:nowrap;">{_Esc(_Money(L['line_total'], L['currency']))}</td>
          </tr>""" for L in Order["lines"])
    Totals = [("Subtotal", Order["subtotal"])]
    if Order.get("discount"):
        Totals.append((f"Promo {Order.get('promo_code') or ''}", -Order["discount"]))
    Totals.append((Order.get("shipping_label") or "Shipping", Order["shipping"]))
    TotalRows = "".join(f"""
          <tr><td style="padding:4px 0;{_Font}font-size:13px;color:#6F6F6F;">{_Esc(K)}</td>
              <td align="right" style="padding:4px 0;{_Font}font-size:13px;">{_Esc(_Money(V, Order['currency']) if V else 'Free')}</td></tr>"""
                        for K, V in Totals)
    Address = "<br>".join(_Esc(L) for L in Order["address_lines"])
    return (f"Order {Order['ref']} received — XJet Atelier", _Layout(f"Thank you — order {_Esc(Order['ref'])} received", f"""\
        <p style="margin:0 0 14px;">{_Greeting(C.get('first_name') or '')}</p>
        <p style="margin:0 0 20px;">We have received your order. {_Esc(_Reserved(Order))}: our team now reviews the design for
           production feasibility and confirms it to you before anything is made.</p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{Rows}</table>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:10px;">{TotalRows}
          <tr><td style="padding:8px 0 0;{_Font}font-size:16px;font-weight:700;">Total</td>
              <td align="right" style="padding:8px 0 0;{_Font}font-size:16px;font-weight:700;">{_Esc(_Money(Order['total'], Order['currency']))}</td></tr>
        </table>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:24px;">
          <tr>
            <td valign="top" width="50%" style="{_Font}font-size:13px;line-height:1.6;">
              <p style="margin:0 0 4px;text-transform:uppercase;letter-spacing:2px;font-size:11px;color:#9A7230;font-weight:700;">Ships to</p>
              {Address}<br><span style="color:#6F6F6F;">{_Esc(Order.get('shipping_label') or '')} · {_Esc(Order.get('shipping_eta') or '')}</span>
            </td>
            <td valign="top" width="50%" style="{_Font}font-size:13px;line-height:1.6;">
              <p style="margin:0 0 4px;text-transform:uppercase;letter-spacing:2px;font-size:11px;color:#9A7230;font-weight:700;">Payment</p>
              <strong>{_Esc(Order['payment_label'])}</strong><br><span style="color:#6F6F6F;">{_Esc(Order.get('payment_message') or '')}</span>
            </td>
          </tr>
        </table>
        <p style="margin:28px 0 0;font-size:12px;color:#8F8F8F;">Prices in US dollars; import duties or VAT, where charged, are paid by the recipient.
           Questions? Reply to this email and quote {_Esc(Order['ref'])}.</p>"""))


def _Reserved(Order: dict) -> str:
    """'Your ring is reserved' — or charm, or pieces for an order that holds both (the wording of before for rings)."""
    Kinds = {"charm" if _IsCharm(L) else "ring" for L in Order.get("lines") or []} or {"ring"}
    Count = sum(int(L.get("quantity") or 1) for L in Order.get("lines") or []) or 1
    if len(Kinds) > 1:
        return "Your pieces are reserved"
    Noun = Kinds.pop()
    return f"Your {Noun} is reserved" if Count == 1 or Noun == "ring" else f"Your {Noun}s are reserved"


def QuoteRequestEmail(Request: dict) -> tuple[str, str]:
    C = Request["customer"]
    Size = _Size(Request)
    return (f"Your quote request {Request['ref']} — XJet Atelier", _Layout("We have received your request", f"""\
        <p style="margin:0 0 14px;">{_Greeting(C.get('first_name') or '')}</p>
        <p style="margin:0 0 16px;">Thank you for your interest in <strong>{_Esc(Request['title'])}</strong>
           ({_Esc(_IdLabel(Request))} {_Esc(Request['ring_id'] or '—')}) in <strong>{_Esc(Request['material_label'])}</strong>, {_Esc(Size)}, ×{_Esc(Request['quantity'])}.</p>
        <p style="margin:0 0 16px;">Gold pieces are quoted individually. A specialist will come back to you within one business day
           with a price and the next steps. Your reference is <strong>{_Esc(Request['ref'])}</strong>.</p>
        {('<p style="margin:0 0 16px;font-size:13px;color:#6F6F6F;">Your note: ' + _Esc(Request['message']) + '</p>') if Request.get('message') else ''}
        <p style="margin:20px 0 0;font-size:12px;color:#8F8F8F;">Questions? Reply to this email and quote {_Esc(Request['ref'])}.</p>"""))


class OutboxMailer:
    Mode = "outbox"

    def __init__(self, OutboxDir: Path):
        self.Dir = Path(OutboxDir)

    def Send(self, To: str, Subject: str, HtmlBody: str, Delivery: str = "outbox") -> str:
        self.Dir.mkdir(parents=True, exist_ok=True)
        Stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        Id = f"{Stamp}-{uuid.uuid4().hex[:8]}"
        (self.Dir / f"{Id}.json").write_text(json.dumps(
            {"id": Id, "to": To, "subject": Subject, "html": HtmlBody, "sent_at": Stamp, "delivery": Delivery},
            indent=2), encoding="utf-8")
        Logger.info("Outbox mail %s to %s: %s", Id, To, Subject)
        return Id

    def List(self, Limit: int = 50) -> list[dict]:
        if not self.Dir.is_dir():
            return []
        Files = sorted(self.Dir.glob("*.json"), reverse=True)[:Limit]
        return [json.loads(F.read_text(encoding="utf-8")) for F in Files]

    def Get(self, Id: str) -> dict | None:
        if not all(C.isalnum() or C in "-_" for C in Id):
            return None
        F = self.Dir / f"{Id}.json"
        return json.loads(F.read_text(encoding="utf-8")) if F.is_file() else None


class SmtpMailer:
    """P2 SendMailUtils.MailSender equivalent (xjet3d Exchange Online relay, no auth).

    Each sent message is also recorded in the developer outbox (var/outbox, readable only via
    the admin-key /api/dev/outbox) with the relay's verdict, so delivery can be diagnosed.
    """
    Mode = "smtp"

    def __init__(self, Record: "OutboxMailer | None" = None):
        self.Record = Record
        self.Server = os.environ.get("SMTP_SERVER", "xjet3d-com.mail.protection.outlook.com")
        self.Port = int(os.environ.get("SMTP_PORT", "25"))
        self.PreferIpv4 = os.environ.get("SMTP_PREFER_IPV4", "true").lower() in ("1", "true", "yes")
        self.From = os.environ.get("MAIL_FROM", "no-reply@xjet3d.com")
        self.FromName = os.environ.get("MAIL_FROM_NAME", "XJet Atelier")

    def Send(self, To: str, Subject: str, HtmlBody: str) -> str:
        Msg = EmailMessage()
        Msg["Subject"] = Subject
        Msg["From"] = formataddr((self.FromName, self.From))
        Msg["To"] = To
        Msg["Message-ID"] = make_msgid(domain=self.From.split("@")[-1])
        Msg["Reply-To"] = f"no-reply@{self.From.split('@', 1)[1]}"     # P2 no_reply=True
        Msg["Auto-Submitted"] = "auto-generated"
        Msg.set_content("This email requires an HTML-capable mail client.")
        Msg.add_alternative(HtmlBody, subtype="html")
        Host = socket.getaddrinfo(self.Server, None, socket.AF_INET)[0][4][0] if self.PreferIpv4 else self.Server
        try:
            with smtplib.SMTP(Host, self.Port, timeout=30) as Smtp:      # relay whitelists the IP: no auth/TLS
                Refused = Smtp.send_message(Msg)
        except Exception as E:
            if self.Record:
                self.Record.Send(To, Subject, HtmlBody, Delivery=f"smtp FAILED: {type(E).__name__}: {E}")
            raise
        Verdict = f"smtp refused: {Refused}" if Refused else f"smtp accepted by {self.Server}:{self.Port}"
        if self.Record:
            self.Record.Send(To, Subject, HtmlBody, Delivery=Verdict)
        Logger.info("SMTP mail to %s: %s — %s", To, Subject, Verdict)
        return Msg["Message-ID"]

    def List(self, Limit: int = 50) -> list[dict]:
        return self.Record.List(Limit) if self.Record else []

    def Get(self, Id: str) -> dict | None:
        return self.Record.Get(Id) if self.Record else None


def BuildMailer(DataDir: Path):
    Mode = os.environ.get("P3_MAIL_MODE", "outbox").strip().lower()
    if Mode == "smtp":
        return SmtpMailer(Record=OutboxMailer(Path(DataDir) / "outbox"))
    return OutboxMailer(Path(DataDir) / "outbox")
