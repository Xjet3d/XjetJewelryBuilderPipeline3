"""Sign-in and self-service registration — the JewelryB2C2 flow (P2 main.py 986-1132).

Messages, status codes and register-once rules are kept identical to Pipeline 2. Mail goes
through p3.mail (an outbox by default), and links point at this deployment's base path.
"""

import html
import json
import logging
import os

from fastapi import Request

from p3.accounts import AuthError
from p3.accounts.local import EmailPattern
from p3.context import Context, HttpError
from p3.mail import TokenEmail, VerificationEmail
from p3.settings import WebDir

Log = logging.getLogger("p3.registration")


def PublicOrigin(Request_: Request) -> str:
    """Origin used in emailed links. P3_PUBLIC_BASE_URL overrides it (P2: PUBLIC_BASE_URL),
    which is needed behind proto -> tron, where the request host is tron."""
    Base = os.environ.get("P3_PUBLIC_BASE_URL", "").strip().rstrip("/")
    return Base or f"{Request_.url.scheme}://{Request_.url.netloc}"


class RegistrationService:
    def __init__(self, Ctx: Context, Mailer):
        self.Ctx = Ctx
        self.Mailer = Mailer

    def _Send(self, To: str, Subject: str, Body: str) -> None:
        """Runs as a background task after the response (P2 BackgroundTasks): a relay failure is
        logged, never shown to the customer — exactly as in P2."""
        try:
            Id = self.Mailer.Send(To, Subject, Body)
            Log.info("Registration email sent via %s to %s: %s (%s)", self.Mailer.Mode, MaskEmail(To), Subject, Id)
        except Exception:
            Log.exception("Registration email to %s FAILED (%s)", MaskEmail(To), Subject)

    def _Queue(self, Defer, To: str, Mail: tuple[str, str]) -> None:
        (Defer or (lambda F, *A: F(*A)))(self._Send, To, *Mail)

    def _StudioUrl(self, Request_: Request) -> str:
        return f"{PublicOrigin(Request_)}{self.Ctx.Settings.BasePath}/"

    # POST /api/register  {Name, Email}
    def Register(self, Request_: Request, Name: str, Email: str, Defer=None) -> dict:
        Email = (Email or "").strip()
        if not EmailPattern.match(Email):
            raise HttpError(400, "invalid_email", "Please enter a valid email address.")
        R = self.Ctx.Accounts.StartEmailRegistration(Name, Email)
        if R["status"] == "removed":                     # no dead code is e-mailed; the page says what to do
            raise HttpError(403, "account_removed", RemovedMessage(self.Ctx.Settings.SupportEmail))
        if R["status"] == "inactive":
            raise HttpError(403, "account_inactive", InactiveMessage(self.Ctx.Settings.SupportEmail))
        if R["status"] == "already_registered":
            self._Queue(Defer, Email, TokenEmail(R["name"], R["token"], f"{self._StudioUrl(Request_)}#token={R['token']}"))
        else:
            Link = f"{PublicOrigin(Request_)}{self.Ctx.Settings.BasePath}/verify?token={R['verify_secret']}"
            self._Queue(Defer, Email, VerificationEmail(R["name"], Link))
        # The same answer whether the address is new, pending or registered: the site never tells who is registered
        return {"status": "verification_sent", "message": RegisterMessage(Email)}

    # POST /api/register-token  {Token, Name, Email}  — "Already have a token? Enter it here"
    def SignInWithToken(self, Token: str, Via: str = "token") -> dict:
        try:
            Who = self.Ctx.Accounts.Authenticate(Token)
        except AuthError as E:
            if E.Code == "token_inactive":
                raise HttpError(403, "token_inactive", "This token has been deactivated. Contact XJet.") from E
            raise HttpError(404, "token_not_found", "Token not found. Check the code and try again.") from E
        self.Ctx.Accounts.RecordSignIn(Who.AccountId, "link" if Via == "link" else "token")
        P = self.Ctx.Accounts.Profile(Who)
        return {"ok": True, "used": P["used"], "max": P["max"], "remaining": P["remaining"], "name": P["name"]}

    # GET /verify?token=
    def VerifyPage(self, Request_: Request, Secret: str, Defer=None) -> str:
        R = self.Ctx.Accounts.VerifyEmail(Secret)
        Studio = self._StudioUrl(Request_)
        if R["status"] == "verified":
            self._Queue(Defer, R["email"], TokenEmail(R["name"], R["token"], f"{Studio}#token={R['token']}"))
        # A link is used once: opened again it confirms the verification and reveals nothing (the code was emailed)
        return RenderVerifyPage(R["status"], R.get("token") if R["status"] == "verified" else None, R["name"], Studio,
                                self.Ctx.Settings.BasePath, self.Ctx.Settings.SupportEmail)


def MaskEmail(Email: str) -> str:
    """dana@example.com → d***@example.com: the log never carries a whole address."""
    Local, _, Domain = (Email or "").partition("@")
    return f"{Local[:1]}***@{Domain}" if Domain else "***"


def _Contact(Support: str = "") -> str:
    return f"XJet at {Support}" if Support and "@" in Support else "XJet"


def RemovedMessage(Support: str = "") -> str:
    """What a removed address sees when it tries to sign in or register again (no code is e-mailed)."""
    return f"This account was removed. Please contact {_Contact(Support)} to restore access."


def InactiveMessage(Support: str = "") -> str:
    """What an address sees whose account XJet switched off (no code is e-mailed: it would not work)."""
    return f"This account is not active. Please contact {_Contact(Support)} to restore access."


def RegisterMessage(Email: str) -> str:
    return (f"We've sent an email to {Email}. Open it to continue: its link verifies your email and signs you in — "
            "or, if you're already registered, it carries your sign-in code.")


def RenderVerifyPage(Status: str, Token: str | None, Name: str, StudioUrl: str, BasePath: str, Support: str = "") -> str:
    """P2 templates/verify.html, rendered without a template engine (all values escaped)."""
    E = lambda V: html.escape(V or "", quote=True)
    Ok = Status in ("verified", "already")
    if Ok and Token:
        # P2 templates/verify.html, word for word. The session key is web/app.js's (P2 uses
        # 'xjet_session'; P3 keeps its own key because both apps share the proto origin).
        SessionKey = "p3_session" + (":" + BasePath if BasePath else "")
        Js = lambda V: json.dumps(V).replace("<", "\\u003c")
        Body = f"""
      <h1>{'Email verified' if Status == 'verified' else 'Already verified'}</h1>
      <p>
        {E(Name) + ', your' if Name else 'Your'} email has been verified successfully.
        You're signed in — the button below takes you straight back to your design so you can
        continue creating your jewelry.
      </p>
      <a class="btn" href="{E(StudioUrl)}#token={E(Token)}">Start designing →</a>
      <p style="margin-top:1.75rem;">Your personal 6-letter sign-in code (you only need it to sign in on
         another device{' — a copy is on its way to your inbox' if Status == 'verified' else ''}):</p>
      <div class="token">{E(Token)}</div>
      <p>Keep it safe — it's tied to your account and its generation quota.</p>
      <script>
        // Auto-store the session so returning to XJet Atelier signs the user in.
        // The app refreshes the real quota numbers from the backend on load.
        try {{
          localStorage.setItem({Js(SessionKey)}, JSON.stringify({{
            token: {Js(Token)},
            name: {Js(Name or "")},
            quotaUsed: 0, quotaMax: 10
          }}));
        }} catch (e) {{ /* private-mode / storage disabled — token is shown above anyway */ }}
        // One click from the email: go straight to the Design screen, signed in. The page above
        // stays as the fallback if the redirect is blocked.
        location.replace({Js(f"{StudioUrl}#token={Token}")});
      </script>"""
    elif Ok:
        # The link was used already: it confirms the verification and reveals nothing (the code was emailed)
        Body = f"""
      <h1>Already verified</h1>
      <p>{E(Name) + ', your' if Name else 'Your'} email has already been verified. Sign in with the code we emailed
        you — or register again with the same email to have it sent once more.</p>
      <a class="btn" href="{E(StudioUrl)}">Start designing →</a>"""
    elif Status == "removed":
        Body = f"""
      <h1>Account removed</h1>
      <p>{E(RemovedMessage(Support))}</p>
      <a class="btn" href="{E(BasePath)}/">Back to XJet Atelier →</a>"""
    elif Status == "expired":
        Body = f"""
      <h1>Link expired</h1>
      <p>This verification link has expired. Please register again from XJet Atelier to
         receive a fresh link.</p>
      <a class="btn" href="{E(BasePath)}/">Back to XJet Atelier →</a>"""
    else:
        Body = f"""
      <h1>Invalid link</h1>
      <p>This verification link is not valid. It may have been mistyped or already used.
         Please register again from XJet Atelier.</p>
      <a class="btn" href="{E(BasePath)}/">Back to XJet Atelier →</a>"""
    Page = (WebDir / "verify.html").read_text(encoding="utf-8")
    return Page.replace("{{CARD_CLASS}}", "" if Ok else "bad").replace("{{BODY}}", Body).replace("{{BASE}}", BasePath)
