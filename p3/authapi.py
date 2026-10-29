"""Customer authentication over HTTP (AJW-39): sessions, CSRF, email/password, Google, Apple, linking, legacy claim.

Pieces
  * Session middleware: the HttpOnly cookie "atelier_session" is the only credential a browser carries. A valid one
    becomes an internal "X-Access-Token: s1.<id>" header, so every existing customer route keeps asking the same
    seam (p3/auth.py RequirePrincipal) who is calling. Whatever X-Access-Token a client sent itself is discarded
    when it is a session marker, and in "modern" mode always.
  * CSRF: every unsafe request that carries a session needs the session's X-CSRF-Token (returned by GET
    /api/auth/session and by every sign-in); every unsafe API request also has its Origin checked.
  * Routes under /api/auth/*. Pages for emailed links: /auth/verify, /auth/reset, /auth/confirm (single-use secrets
    live in the URL fragment, never in a log, and are consumed by a POST, never by a GET a mail scanner could do).
  * Terms acceptance (2026-10-29, p3/accounts/consent.py): a new account is made only together with its acceptance —
    an email sign-up when its link is used, a new Google / Apple customer after they accept on the page they return
    to (the sign-up waits in an HttpOnly cookie) — and an account XJet made accepts at its first sign-in.

Nothing here logs a secret, a token, a password or an unmasked email address.
"""

import html
import logging
from urllib.parse import urlparse

from fastapi import BackgroundTasks, Body, Form, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from p3 import mail as Mail
from p3 import sitepages as SitePages
from p3.accounts.consent import AcceptedOf, ConsentError, PendingTtlHours, TermsChanged
from p3.accounts.identity import (EmailPattern, IdentityError, IsRelayEmail, NormalizeEmail, PasswordProblem, Providers,
                                  SessionCookieMark)
from p3.adminauth import BrowserScheme
from p3.auth import RequirePrincipal
from p3.context import Context, HttpError
from p3.oidc import OidcError, ParseUserHint
from p3.registration import MaskEmail, PublicOrigin
from p3.settings import WebDir

Log = logging.getLogger("p3.auth")

SessionCookie = "atelier_session"
OAuthCookie = "atelier_oauth"
PendingCookie = "atelier_pending"                     # a new Google / Apple customer's sign-up, until they accept the Terms
UnsafeMethods = ("POST", "PUT", "PATCH", "DELETE")
NoOriginCheck = ("/api/auth/apple/callback",)        # Apple posts the sign-in result back from appleid.apple.com
# Requests that happen before (or instead of) a session carry no CSRF token: login CSRF is stopped by the Origin check and
# each of these also needs a secret the attacker does not have (a password, or a single-use emailed link / ticket / cookie).
NoCsrf = ("/api/auth/login", "/api/auth/register", "/api/auth/forgot", "/api/auth/reset", "/api/auth/verify-email",
          "/api/auth/resend-verification", "/api/auth/confirm-link", "/api/auth/claim", "/api/auth/claim/password",
          "/api/auth/apple/callback", "/api/register-token", "/api/register", "/api/auth/accept", "/api/auth/pending")
Generic = "If that email can be used, we've sent you a message. Check your inbox."
TermsRequiredMessage = "Please accept the Terms of Service to create your account."
TermsChangedMessage = ("Our Terms of Service or Privacy Policy changed while you were signing up. Please read them and "
                       "accept them again.")


def CookiePath(Ctx: Context) -> str:
    return Ctx.Settings.BasePath or "/"


def LegalNow(Ctx: Context) -> dict:
    """The versions of the Terms and the Privacy Policy the site serves now (archived automatically)."""
    return Ctx.Accounts.Consent.Current()


def TermsChangedError(Ctx: Context, Current: dict) -> HttpError:
    return HttpError(409, "terms_changed", TermsChangedMessage, {"legal": Current})


def RequireAcceptance(Ctx: Context, Body_: dict) -> dict:
    """What a sign-up step sends: the box ticked (accept_terms) and the versions the page showed (terms, privacy) —
    which must be the ones served now: 400 terms_required, or 409 terms_changed carrying the current ones to show."""
    if Body_.get("accept_terms") is not True:
        raise HttpError(400, "terms_required", TermsRequiredMessage)
    try:
        return Ctx.Accounts.Consent.Check({"terms": Body_.get("terms"), "privacy": Body_.get("privacy")})
    except TermsChanged as E:
        raise TermsChangedError(Ctx, E.Current) from None


def Install(App_: "FastAPI", Ctx: Context, Mailer) -> None:      # noqa: F821
    S = Ctx.Settings
    Base = S.BasePath
    Id = Ctx.Accounts.Identity
    Consent = Ctx.Accounts.Consent

    def _Rel(Req: Request) -> str:
        return Req.url.path[len(Base):] if Base and Req.url.path.startswith(Base) else Req.url.path

    def _Origin(Req: Request) -> str:
        return PublicOrigin(Req)

    def _SameOrigin(Req: Request) -> bool:
        """The Origin (or Referer) of an unsafe request is this site. A request with neither (a script, curl) is not a
        browser acting for a signed-in customer and falls through to the CSRF-token check."""
        Given = Req.headers.get("origin") or ""
        if not Given:
            Ref = Req.headers.get("referer") or ""
            Given = f"{urlparse(Ref).scheme}://{urlparse(Ref).netloc}" if Ref else ""
        if not Given:
            return True
        Host = (Req.headers.get("x-forwarded-host") or Req.headers.get("host") or "").split(",")[0].strip().lower()
        Allowed = {urlparse(_Origin(Req)).netloc.lower(), Host}
        if S.PublicBaseUrl:
            Allowed.add(urlparse(S.PublicBaseUrl).netloc.lower())
        return urlparse(Given).netloc.lower() in Allowed

    # ── session middleware ───────────────────────────────────────────────
    @App_.middleware("http")
    async def _Session(Req: Request, CallNext):
        Rel = _Rel(Req)
        if not Rel.startswith("/api/") or Rel.startswith(("/api/admin/", "/api/dev/")):
            return await CallNext(Req)
        Unsafe = Req.method in UnsafeMethods
        if Unsafe and Rel not in NoOriginCheck and not _SameOrigin(Req):
            return JSONResponse(status_code=403, content={"error": {"code": "bad_origin", "message": "This request did not come from the site."}})
        Cookie = Req.cookies.get(SessionCookie)
        Session = Id.ResolveSession(Cookie) if Cookie else None
        Drop = {b"x-access-token"} if (Session or S.AuthMode == "modern") else set()
        Headers = [(K, V) for K, V in Req.scope["headers"]
                   if K not in Drop and not (K == b"x-access-token" and V.startswith(SessionCookieMark.encode()))]
        if Session:
            if Unsafe and Rel not in NoCsrf and not Id.CsrfOk(Session, Req.headers.get("x-csrf-token")):
                return JSONResponse(status_code=403, content={"error": {"code": "csrf", "message": "Your session needs refreshing. Reload the page and try again."}})
            Headers.append((b"x-access-token", (SessionCookieMark + Cookie).encode("latin-1")))
            Req.scope["p3_session"] = Session
        Req.scope["headers"] = Headers
        Resp = await CallNext(Req)
        if Rel.startswith("/api/auth/"):
            Resp.headers.setdefault("Cache-Control", "no-store")             # nothing about who is signed in is ever cached
        return Resp

    # ── helpers ──────────────────────────────────────────────────────────
    def Secure(Req: Request) -> bool:
        return BrowserScheme(Req) == "https"

    def SetSession(Resp: Response, Req: Request, Sess: dict) -> None:
        Resp.set_cookie(SessionCookie, Sess["token"], max_age=Sess["max_age"], path=CookiePath(Ctx), httponly=True,
                        samesite="lax", secure=Secure(Req))

    def ClearSession(Resp: Response, Req: Request) -> None:
        Resp.delete_cookie(SessionCookie, path=CookiePath(Ctx), httponly=True, samesite="lax", secure=Secure(Req))

    def Reply(Req: Request, Body: dict, Sess: dict | None = None, Status: int = 200) -> JSONResponse:
        Resp = JSONResponse(Body, status_code=Status)
        if Sess:
            SetSession(Resp, Req, Sess)
        return Resp

    def Account(AccountId: str) -> dict:
        A = Id.Account(AccountId)
        return {"name": A["display_name"] or "", "email": A["email"] or ""}

    def SignedIn(Req: Request, AccountId: str, Method: str) -> JSONResponse:
        Sess = Id.CreateSession(AccountId, Method, Req.headers.get("user-agent"))
        return Reply(Req, {"ok": True, "csrf": Sess["csrf"], "account": Account(AccountId),
                           "terms_required": Consent.Required(AccountId)}, Sess)      # the page asks for the Terms first

    def Required(Req: Request) -> dict:
        Session = Req.scope.get("p3_session")
        if not Session:
            raise HttpError(401, "login_required", "Please sign in.")
        return Session

    def Recent(Req: Request) -> dict:
        Session = Required(Req)
        if not Id.IsRecent(Session):
            raise HttpError(403, "reauth_required", "Please confirm it's you to make this change.")
        return Session

    def Send(Defer, To: str, Content: tuple[str, str]) -> None:
        def _Do(To_: str, Subject: str, Body: str) -> None:
            try:
                MailId = Mailer.Send(To_, Subject, Body)
                Log.info("Security email via %s to %s: %s (%s)", Mailer.Mode, MaskEmail(To_), Subject, MailId)
            except Exception:
                Log.exception("Security email to %s FAILED (%s)", MaskEmail(To_), Subject)
        Defer(_Do, To, *Content)

    def Link(Req: Request, Page: str, Secret: str) -> str:
        return f"{_Origin(Req)}{Base}/auth/{Page}#token={Secret}"

    def Email_(Body_: dict, Key: str = "email") -> str:
        E = NormalizeEmail(str(Body_.get(Key) or ""))
        if not EmailPattern.match(E) or len(E) > 254:
            raise HttpError(400, "invalid_email", "Please enter a valid email address.")
        return E

    def Password_(Body_: dict, Email: str = "", Key: str = "password") -> str:
        Pw = Body_.get(Key)
        Problem = PasswordProblem(Pw, Email)
        if Problem:
            raise HttpError(400, "weak_password", Problem)
        return Pw

    # ── Terms acceptance ─────────────────────────────────────────────────
    def Legal() -> dict:
        return LegalNow(Ctx)

    def Changed(Current: dict) -> HttpError:
        return TermsChangedError(Ctx, Current)

    def Accepted(Body_: dict) -> dict:
        return RequireAcceptance(Ctx, Body_)

    def Created(Req: Request, Secret: str, Versions: dict, Method: str) -> JSONResponse:
        """A pending registration becomes its account (with its login, welcome credits and acceptance), signed in."""
        try:
            R = Consent.CreateAccount(Secret, Versions)
        except ConsentError as E:
            raise HttpError(409 if E.Code in ("email_in_use", "already_linked") else 400, E.Code, E.Message) from E
        Sess = Id.CreateSession(R["account_id"], Method, Req.headers.get("user-agent"))
        return Reply(Req, {"ok": True, "csrf": Sess["csrf"], "account": Account(R["account_id"]), "created": True}, Sess)

    def ClearPending(Resp: Response, Req: Request) -> None:
        Resp.delete_cookie(PendingCookie, path=CookiePath(Ctx), httponly=True, samesite="lax", secure=Secure(Req))

    # ── anonymous information ────────────────────────────────────────────
    @App_.get("/api/auth/config")
    async def AuthConfig():
        return {"mode": S.AuthMode, "providers": {P: P in Ctx.Oidc for P in ("google", "apple")}}

    @App_.get("/api/auth/session")
    async def AuthSession(Req: Request):
        Session = Req.scope.get("p3_session")
        if not Session:
            return {"authenticated": False, "mode": S.AuthMode, "providers": {P: P in Ctx.Oidc for P in ("google", "apple")}}
        return {"authenticated": True, "csrf": Session["csrf"], "account": Account(Session["account_id"]),
                "recent": Id.IsRecent(Session), "mode": S.AuthMode, "method": Session["auth_method"],
                "terms_required": bool(Session.get("terms_required"))}

    @App_.get("/api/auth/terms")
    async def AuthTerms():
        """The versions a sign-up accepts (sent back with the acceptance, so a change during the sign-up is noticed)."""
        return Legal()

    # ── email and password ───────────────────────────────────────────────
    @App_.post("/api/auth/register")
    async def AuthRegister(Req: Request, Background: BackgroundTasks, Body_: dict = Body(...)):
        Email = Email_(Body_)
        Name = str(Body_.get("name") or "").strip()[:100]
        Password = Password_(Body_, Email)
        Versions = Accepted(Body_)                        # the same answer for every address: a refusal says nothing of it
        Ctx.RateLimiter.Register(Req, Email)
        Ctx.RateLimiter.Mail(Req, Email)
        Existing = Id.AccountsByEmail(Email)
        Live = Id.LiveAccountByEmail(Email)
        Pw = Id.PasswordIdentity(Email)
        Hash = Id.HashPassword(Password)                  # the same work whichever branch follows (a branch that stores it reuses it)
        if Pw and Pw["email_verified"]:
            Send(Background.add_task, Email, Mail.AccountExistsEmail(Name, f"{_Origin(Req)}{Base}/"))
        elif Pw:                                          # a registration (or claim) nobody confirmed yet: only a new link is sent;
                                                          # an unauthenticated request never replaces the password already stored
            Send(Background.add_task, Email, Mail.AuthVerifyEmail(Name, Link(Req, "verify", Id.NewToken("verify_email", Pw["account_id"], Email))))
        elif Live:                                        # an account that has no password yet: the mailbox's owner sets one
            Send(Background.add_task, Email, Mail.ResetPasswordEmail(Live["display_name"] or Name,
                                                                       Link(Req, "reset", Id.NewToken("reset_password", Live["account_id"], Email)), True))
        elif Existing:                                    # only removed / switched-off accounts have this address
            Send(Background.add_task, Email, Mail.AccountUnavailableEmail(Name, all(A["removed_at"] for A in Existing), S.SupportEmail))
        else:                                             # a new address: a pending registration — the account, its credits and
                                                          # the acceptance are made together when the emailed link is used
            Secret = Consent.NewPending("password", Email, Name, PasswordHash=Hash, Accepted=Versions)
            Send(Background.add_task, Email, Mail.AuthVerifyEmail(Name, Link(Req, "verify", Secret)))
        return {"ok": True, "message": f"We've sent a message to {Email}. Open it to finish creating your account."}

    @App_.post("/api/auth/login")
    async def AuthLogin(Req: Request, Body_: dict = Body(...)):
        Email = NormalizeEmail(str(Body_.get("email") or ""))
        Ctx.RateLimiter.Login(Req, Email)
        try:
            R = Id.VerifyPassword(Email, str(Body_.get("password") or ""))
        except IdentityError as E:
            Log.warning("Password sign-in refused for %s", MaskEmail(Email))
            raise HttpError(401, "invalid_credentials", "The email or password is not right.") from E
        if not R["verified"]:
            raise HttpError(403, "email_unverified", "Please confirm your email first. We can send the link again.")
        if not Id.Usable(R["account_id"]):
            raise HttpError(403, "account_inactive", f"This account is not active. Please contact {S.SupportEmail}.")
        return SignedIn(Req, R["account_id"], "password")

    @App_.post("/api/auth/resend-verification")
    async def AuthResend(Req: Request, Background: BackgroundTasks, Body_: dict = Body(...)):
        Email = Email_(Body_)
        Ctx.RateLimiter.Mail(Req, Email)
        Pw = Id.PasswordIdentity(Email)
        if Pw and not Pw["email_verified"] and Id.Usable(Pw["account_id"]):
            Send(Background.add_task, Email, Mail.AuthVerifyEmail("", Link(Req, "verify", Id.NewToken("verify_email", Pw["account_id"], Email))))
        elif not Pw and not Id.AccountsByEmail(Email):    # a sign-up waiting for its link: a new link for the same sign-up
            Again = Consent.Renew("password", Email)
            if Again:
                Send(Background.add_task, Email, Mail.AuthVerifyEmail(Again["name"], Link(Req, "verify", Again["secret"])))
        return {"ok": True, "message": Generic}

    @App_.post("/api/auth/verify-email")
    async def AuthVerify(Req: Request, Body_: dict = Body(...)):
        Ctx.RateLimiter.Token(Req)
        Secret = str(Body_.get("token") or "")
        P = Consent.Pending(Secret)
        if P is not None and P["method"] == "password":
            # A sign-up's link: its account now, with the acceptance its form made — or, when the Terms or the Privacy
            # Policy changed since, with the one this page sends after showing them (409 terms_changed asks for it)
            Given = ({"terms": Body_.get("terms"), "privacy": Body_.get("privacy")} if Body_.get("accept_terms") is True
                     else AcceptedOf(P))
            try:
                Versions = Consent.Check(Given)
            except TermsChanged as E:
                raise Changed(E.Current) from None
            return Created(Req, Secret, Versions, "password")
        if Consent.PendingState(Secret) in ("used", "expired"):          # a sign-up's link opened again, or too late
            Gone = Consent.PendingState(Secret)
            raise HttpError(400, "link_" + Gone, LinkMessage(Gone))
        State = Id.TokenState("verify_email", Secret)
        Row = Id.UseToken("verify_email", Secret) if State == "valid" else None
        if Row is None:
            raise HttpError(400, "link_" + State if State != "valid" else "link_invalid", LinkMessage(State))
        if not Id.Usable(Row["account_id"]):
            raise HttpError(403, "account_inactive", f"This account is not active. Please contact {S.SupportEmail}.")
        Id.MarkPasswordEmailVerified(Row["account_id"])
        return SignedIn(Req, Row["account_id"], "password")

    @App_.post("/api/auth/forgot")
    async def AuthForgot(Req: Request, Background: BackgroundTasks, Body_: dict = Body(...)):
        Email = Email_(Body_)
        Ctx.RateLimiter.Mail(Req, Email)
        A = Id.LiveAccountByEmail(Email)
        if A:
            HasPw = Id.HasVerifiedPassword(A["account_id"])
            Send(Background.add_task, Email, Mail.ResetPasswordEmail(A["display_name"] or "",
                                                                     Link(Req, "reset", Id.NewToken("reset_password", A["account_id"], Email)), not HasPw))
        return {"ok": True, "message": Generic}

    @App_.post("/api/auth/reset")
    async def AuthReset(Req: Request, Background: BackgroundTasks, Body_: dict = Body(...)):
        Ctx.RateLimiter.Token(Req)
        Secret = str(Body_.get("token") or "")
        State = Id.TokenState("reset_password", Secret)
        if State != "valid":
            raise HttpError(400, "link_" + State, LinkMessage(State))
        Peek = Id.PeekToken("reset_password", Secret)
        Password = Password_(Body_, Peek["email"] or "")
        Row = Id.UseToken("reset_password", Secret)
        if Row is None:
            raise HttpError(400, "link_used", LinkMessage("used"))
        if not Id.Usable(Row["account_id"]):
            raise HttpError(403, "account_inactive", f"This account is not active. Please contact {S.SupportEmail}.")
        try:
            Id.SetPassword(Row["account_id"], Password, Row["email"] or Id.Account(Row["account_id"])["email"], True)
        except IdentityError as E:
            raise HttpError(409, E.Code, E.Message) from E
        Id.RevokeAll(Row["account_id"], Reason="password_reset")           # every browser signed in before is signed out
        A = Id.Account(Row["account_id"])
        if A["email"]:
            Send(Background.add_task, A["email"], Mail.PasswordChangedEmail(A["display_name"] or "", S.SupportEmail))
        return SignedIn(Req, Row["account_id"], "password")

    @App_.post("/api/auth/logout")
    async def AuthLogout(Req: Request):
        Id.Revoke(Req.cookies.get(SessionCookie))
        Resp = JSONResponse({"ok": True})
        ClearSession(Resp, Req)
        return Resp

    # ── the account page: connected methods, password, sessions ──────────
    @App_.get("/api/auth/account")
    async def AuthAccount(Req: Request):
        Session = Required(Req)
        return {**Id.Summary(Session["account_id"], Session["id"]), "csrf": Session["csrf"], "recent": Id.IsRecent(Session),
                "providers": {P: P in Ctx.Oidc for P in ("google", "apple")}}

    @App_.post("/api/auth/reauth")
    async def AuthReauth(Req: Request, Body_: dict = Body(...)):
        Session = Required(Req)
        Ctx.RateLimiter.Login(Req, Session["account_id"])
        if not Id.CheckPassword(Session["account_id"], str(Body_.get("password") or "")):
            raise HttpError(401, "invalid_credentials", "That password is not right.")
        Id.Reauthenticated(Session["id"], "password")
        return {"ok": True}

    @App_.post("/api/auth/password")
    async def AuthPassword(Req: Request, Background: BackgroundTasks, Body_: dict = Body(...)):
        Session = Recent(Req)
        AccountId = Session["account_id"]
        A = Id.Account(AccountId)
        if not Id.EmailVerified(AccountId):
            raise HttpError(409, "email_unverified", "Your account has no confirmed email address yet, so a password can't be added.")
        Password = Password_(Body_, A["email"] or "", "new_password")
        try:
            Id.SetPassword(AccountId, Password, A["email"], True)
        except IdentityError as E:
            raise HttpError(409, E.Code, E.Message) from E
        New = Id.Rotate(Session, "password_changed")
        Send(Background.add_task, A["email"], Mail.PasswordChangedEmail(A["display_name"] or "", S.SupportEmail))
        return Reply(Req, {"ok": True, "csrf": New["csrf"]}, New)

    @App_.delete("/api/auth/methods/{MethodId}")
    async def AuthUnlink(Req: Request, MethodId: str):
        Session = Recent(Req)
        try:
            Id.Unlink(Session["account_id"], MethodId)
        except IdentityError as E:
            raise HttpError(409 if E.Code == "last_method" else 404, E.Code, E.Message) from E
        New = Id.Rotate(Session, "method_disconnected")
        return Reply(Req, {"ok": True, "csrf": New["csrf"]}, New)

    @App_.post("/api/auth/sessions/revoke-others")
    async def AuthRevokeOthers(Req: Request):
        Session = Required(Req)
        return {"ok": True, "revoked": Id.RevokeAll(Session["account_id"], ExceptId=Session["id"], Reason="signed_out_everywhere")}

    @App_.delete("/api/auth/sessions/{SessionId}")
    async def AuthRevokeOne(Req: Request, SessionId: str):
        Session = Required(Req)
        if SessionId == Session["id"]:
            raise HttpError(400, "current_session", "Use Sign out for this browser.")
        return {"ok": Id.RevokeById(Session["account_id"], SessionId)}

    # ── legacy six-letter code → a real login ────────────────────────────
    @App_.post("/api/auth/claim")
    async def AuthClaim(Req: Request, Body_: dict = Body(...)):
        Ctx.RateLimiter.Claim(Req)
        Ctx.RateLimiter.Auth(Req)
        Token = str(Body_.get("token") or "").strip()
        A = Id.LegacyAccountForToken(Token) if 0 < len(Token) <= 64 else None
        if A is None:
            raise HttpError(404, "code_not_found", "That code was not found, or it has already been replaced by a sign-in. "
                            "Use Forgot password with your email, or contact us.")
        Ticket = Id.NewToken("claim", A["account_id"], A["email"])
        Id.Event(A["account_id"], "claim_started")
        return {"ok": True, "ticket": Ticket, "name": A["display_name"] or "", "email": A["email"] or ""}

    @App_.post("/api/auth/claim/password")
    async def AuthClaimPassword(Req: Request, Background: BackgroundTasks, Body_: dict = Body(...)):
        Ctx.RateLimiter.Token(Req)
        Ticket = Id.PeekToken("claim", str(Body_.get("ticket") or ""))
        if Ticket is None:
            raise HttpError(400, "link_expired", "That step took too long. Enter your code again.")
        A = Id.Account(Ticket["account_id"])
        Email = Email_(Body_)
        Password = Password_(Body_, Email)
        Ctx.RateLimiter.Mail(Req, Email)
        Known = NormalizeEmail(A["email"]) == Email and bool(A["verified_at"]) and A["source"] != "admin"
        try:
            Id.SetPassword(A["account_id"], Password, Email, Known)
        except IdentityError as E:
            raise HttpError(409, E.Code, "That email already has a login. Sign in with it instead.") from E
        Id.UseToken("claim", str(Body_.get("ticket")))
        if Known:
            return SignedIn(Req, A["account_id"], "claim")
        Send(Background.add_task, Email, Mail.AuthVerifyEmail(A["display_name"] or "", Link(Req, "verify", Id.NewToken("verify_email", A["account_id"], Email))))
        return {"ok": True, "verification_sent": True, "message": f"We've sent a message to {Email}. Open it to finish — your code stops working once you have."}

    # ── Google / Apple ───────────────────────────────────────────────────
    def RedirectUri(Req: Request, Provider: str) -> str:
        return f"{_Origin(Req)}{Base}/api/auth/{Provider}/callback"

    def Back(Req: Request, Status: str) -> RedirectResponse:
        Resp = RedirectResponse(f"{Base}/#auth={Status}", status_code=303)
        Resp.delete_cookie(OAuthCookie, path=CookiePath(Ctx), samesite="none" if Secure(Req) else "lax", secure=Secure(Req))
        return Resp

    @App_.get("/api/auth/{Provider}/start")
    async def OAuthStart(Req: Request, Provider: str, purpose: str = Query("login"), ticket: str = Query("")):
        Prov = Ctx.Oidc.get(Provider)
        if Provider not in ("google", "apple") or Prov is None:
            return RedirectResponse(f"{Base}/#auth=unavailable", status_code=303)
        Ctx.RateLimiter.Auth(Req)
        AccountId = None
        if purpose in ("link", "reauth"):
            Session = Req.scope.get("p3_session")
            if not Session:
                return RedirectResponse(f"{Base}/#auth=expired", status_code=303)
            if purpose == "link" and not Id.IsRecent(Session):
                return RedirectResponse(f"{Base}/#auth=reauth_required", status_code=303)
            AccountId = Session["account_id"]
        elif purpose == "claim":
            Row = Id.PeekToken("claim", ticket)
            if Row is None:
                return RedirectResponse(f"{Base}/#auth=expired", status_code=303)
            AccountId = Row["account_id"]
        elif purpose != "login":
            return RedirectResponse(f"{Base}/#auth=failed", status_code=303)
        St = Id.NewOAuthState(Provider, purpose, AccountId, ticket if purpose == "claim" else "")
        Resp = RedirectResponse(Prov.AuthorizeUrl(RedirectUri(Req, Provider), St["state"], St["nonce"], St["verifier"]), status_code=303)
        Resp.set_cookie(OAuthCookie, St["state"], max_age=600, path=CookiePath(Ctx), httponly=True, secure=Secure(Req),
                        samesite="none" if Secure(Req) else "lax")             # Apple posts back cross-site
        return Resp

    async def Finish(Req: Request, Background: BackgroundTasks, Provider: str, Code: str, State: str, Error: str, UserRaw: str) -> RedirectResponse:
        Prov = Ctx.Oidc.get(Provider)
        if Prov is None or Provider not in ("google", "apple"):
            return Back(Req, "unavailable")
        if Error:
            return Back(Req, "cancelled")
        if not State or not Code or Req.cookies.get(OAuthCookie) != State:       # the round trip must end in the browser that began it
            return Back(Req, "failed")
        Row = Id.UseOAuthState(Provider, State)
        if Row is None:
            return Back(Req, "failed")
        try:
            C = await Prov.Complete(Code, RedirectUri(Req, Provider), Row["verifier"], Row["nonce"], ParseUserHint(UserRaw))
        except OidcError as E:
            Log.warning("%s sign-in refused: %s", Provider, E)
            return Back(Req, "failed")
        if not C.Subject:
            return Back(Req, "failed")
        Purpose, AccountId = Row["purpose"], Row["account_id"]
        if Purpose not in ("login", "link", "claim", "reauth"):                  # e.g. a state made for the Admin sign-in
            return Back(Req, "failed")
        try:
            if Purpose == "reauth":
                Known = Id.Identity(Provider, C.Subject)
                if not Known or Known["account_id"] != AccountId:
                    return Back(Req, "wrong_account")
                Session = Req.scope.get("p3_session")
                if Session and Session["account_id"] == AccountId:
                    Id.Reauthenticated(Session["id"], Provider)
                return Back(Req, "reauthenticated")
            if Purpose == "link":
                Id.LinkProvider(AccountId, Provider, C.Subject, C.Email, C.EmailVerified, C.Name)
                return Back(Req, "connected")
            if Purpose == "claim":
                Id.ClaimWithProvider(AccountId, Provider, C.Subject, C.Email, C.EmailVerified, C.Name)
                Id.UseToken("claim", Row["target"])
                R = Back(Req, "ok")
                SetSession(R, Req, Id.CreateSession(AccountId, Provider, Req.headers.get("user-agent")))
                return R
            Outcome = Id.SignInWithProvider(Provider, C.Subject, C.Email, C.EmailVerified, C.Name)
        except IdentityError as E:
            return Back(Req, E.Code)
        if Outcome["status"] == "confirm":
            Secret = Id.NewToken("confirm_link", Outcome["account_id"], Outcome["email"],
                                 {"provider": Provider, "subject": C.Subject, "name": C.Name})
            Send(Background.add_task, Outcome["email"], Mail.ConfirmLinkEmail(C.Name, Provider, Link(Req, "confirm", Secret)))
            return Back(Req, "confirm_sent")
        if Outcome["status"] == "new":
            # A new customer, identified: no account yet. It is made when they accept the Terms on the page they return to;
            # until then the sign-up waits in this browser (an HttpOnly cookie holding its secret), for 24 hours at most
            Secret = Consent.NewPending(Provider, Outcome["email"], Outcome["name"], Subject=C.Subject,
                                        EmailVerified=Outcome["email_verified"])
            R = Back(Req, "accept_terms")
            R.set_cookie(PendingCookie, Secret, max_age=PendingTtlHours * 3600, path=CookiePath(Ctx), httponly=True,
                         samesite="lax", secure=Secure(Req))
            return R
        if Outcome["status"] != "ok":
            return Back(Req, "blocked")
        R = Back(Req, "ok")
        SetSession(R, Req, Id.CreateSession(Outcome["account_id"], Provider, Req.headers.get("user-agent")))
        return R

    @App_.get("/api/auth/{Provider}/callback")
    async def OAuthCallback(Req: Request, Background: BackgroundTasks, Provider: str, code: str = "", state: str = "", error: str = ""):
        return await Finish(Req, Background, Provider, code, state, error, "")

    @App_.post("/api/auth/apple/callback")
    async def AppleCallback(Req: Request, Background: BackgroundTasks, code: str = Form(""), state: str = Form(""),
                            error: str = Form(""), user: str = Form("")):
        return await Finish(Req, Background, "apple", code, state, error, user)

    @App_.post("/api/auth/confirm-link")
    async def AuthConfirmLink(Req: Request, Body_: dict = Body(...)):
        Ctx.RateLimiter.Token(Req)
        Secret = str(Body_.get("token") or "")
        State = Id.TokenState("confirm_link", Secret)
        Row = Id.UseToken("confirm_link", Secret) if State == "valid" else None
        if Row is None:
            raise HttpError(400, "link_" + State, LinkMessage(State))
        if not Id.Usable(Row["account_id"]):
            raise HttpError(403, "account_inactive", f"This account is not active. Please contact {S.SupportEmail}.")
        D = Row["data"]
        try:
            Id.ConfirmLink(Row["account_id"], str(D.get("provider")), str(D.get("subject")), Row["email"], str(D.get("name") or ""))
        except IdentityError as E:
            raise HttpError(409, E.Code, E.Message) from E
        return SignedIn(Req, Row["account_id"], str(D.get("provider")))

    # ── accepting the Terms: a new Google / Apple customer, and an account that accepts at its sign-in ──
    @App_.get("/api/auth/pending")
    async def AuthPending(Req: Request):
        """The Google / Apple sign-up waiting in this browser, if any: who it is (as the provider said) and what to accept."""
        P = Consent.Pending(Req.cookies.get(PendingCookie))
        if P is None or P["method"] not in Providers:
            return {"pending": False}
        return {"pending": True, "method": P["method"], "name": P["display_name"] or "",
                "email": "" if IsRelayEmail(P["email"]) else (P["email"] or ""), "legal": Legal()}

    @App_.post("/api/auth/accept")
    async def AuthAccept(Req: Request, Body_: dict = Body(...)):
        """The new Google / Apple customer accepts: their account is made now, with the acceptance, and signed in."""
        Ctx.RateLimiter.Token(Req)
        Secret = Req.cookies.get(PendingCookie) or ""
        P = Consent.Pending(Secret)
        if P is None or P["method"] not in Providers:
            raise HttpError(400, "link_expired", "That step took too long. Please sign in again.")
        Resp = Created(Req, Secret, Accepted(Body_), P["method"])
        ClearPending(Resp, Req)
        return Resp

    @App_.delete("/api/auth/pending")
    async def AuthPendingCancel(Req: Request):
        """They did not accept: the sign-up is forgotten (no account was ever made)."""
        Consent.DropPending(Req.cookies.get(PendingCookie))
        Resp = JSONResponse({"ok": True})
        ClearPending(Resp, Req)
        return Resp

    @App_.post("/api/auth/accept-terms")
    async def AuthAcceptTerms(Body_: dict = Body(...), x_access_token: str | None = Header(None)):
        """A signed-in account that accepts at its sign-in (one XJet made) accepts now. Any other account records nothing:
        an account made before acceptance existed is never recorded after the fact."""
        Who = RequirePrincipal(Ctx, x_access_token)
        try:
            Status = Consent.AcceptForAccount(Who.AccountId, Accepted(Body_))
        except TermsChanged as E:
            raise Changed(E.Current) from None
        return {"ok": True, "status": Status}

    # ── pages the emailed links open ─────────────────────────────────────
    PageModes = {"verify": ("Confirm your email", "Confirm email"), "reset": ("Choose a new password", "Save password"),
                 "confirm": ("Connect your sign-in", "Confirm")}

    @App_.get("/auth/{Mode}", include_in_schema=False)
    async def AuthPage(Mode: str):
        if Mode not in PageModes:
            raise HttpError(404, "not_found", "Not found.")
        Title, Button = PageModes[Mode]
        Page = (WebDir / "authpage.html").read_text(encoding="utf-8")
        DesignUse = f'<p class="hint">{html.escape(SitePages.DesignUseAcceptance)}</p>'     # part of every acceptance
        for K, V in {"{{MODE}}": Mode, "{{TITLE}}": html.escape(Title), "{{BUTTON}}": html.escape(Button),
                     "{{DESIGN_USE}}": DesignUse, "{{BASE}}": Base}.items():
            Page = Page.replace(K, V)
        return HTMLResponse(Page, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow"})


def LinkMessage(State: str) -> str:
    return {"used": "This link was already used. If that was you, just sign in.",
            "expired": "This link has expired. Request a new one from the sign-in page."}.get(State, "This link is not valid. Request a new one from the sign-in page.")
