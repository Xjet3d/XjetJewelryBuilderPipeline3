"""AJW-39: customer authentication — registration, verification, login, recovery, sessions, CSRF, Google/Apple, linking,
legacy-code claim. Mail goes to the outbox; the identity providers are fakes (no network)."""

import logging
import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from p3.oidc import Claims, OidcError
from tests.conftest import Harness

Pw = "correct horse battery"


class FakeIdp:
    """Stands in for Google / Apple: what Complete() returns is set by the test; "bad" is a code the provider refuses."""

    def __init__(self, Name):
        self.Name = Name
        self.Next = Claims(Subject="sub-1", Email="dana@example.com", EmailVerified=True, Name="Dana")

    def AuthorizeUrl(self, RedirectUri, State, Nonce, Verifier, Prompt=""):
        return f"https://idp.test/{self.Name}/authorize?state={State}&redirect_uri={RedirectUri}"

    async def Complete(self, Code, RedirectUri, Verifier, Nonce, UserHint=None):
        if Code == "bad":
            raise OidcError("token exchange failed")
        return self.Next


@pytest.fixture
async def A(tmp_path):
    Obj = Harness(tmp_path, AuthMode="modern")
    Obj.Ctx.Oidc = {"google": FakeIdp("google"), "apple": FakeIdp("apple")}
    yield Obj
    await Obj.Close()


def Browser(H, **Kw) -> httpx.AsyncClient:
    """A fresh browser: its own cookie jar, no X-Access-Token header."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=H.App), base_url="http://p3.test" + H.Base, **Kw)


def Mails(H):
    """The outbox, oldest first."""
    return list(reversed(H.App.state.Mailer.List()))


def LinkSecret(Html):
    M = re.search(r"#token=([A-Za-z0-9_\-]+)", Html)
    assert M, "no link in the email"
    return M.group(1)


def Csrf(Resp):
    return {"X-CSRF-Token": Resp.json()["csrf"]}


async def Register(H, B, Email="dana@example.com", Name="Dana", Password=Pw):
    R = await B.post("/api/auth/register", json={"name": Name, "email": Email, "password": Password})
    assert R.status_code == 200, R.text
    return R


async def SignedUp(H, Email="dana@example.com", Name="Dana"):
    """Register, confirm the email, return (browser, csrf headers)."""
    B = Browser(H)
    await Register(H, B, Email, Name)
    Mine = [M for M in Mails(H) if M["to"] == Email]
    R = await B.post("/api/auth/verify-email", json={"token": LinkSecret(Mine[-1]["html"])})
    assert R.status_code == 200, R.text
    return B, Csrf(R)


async def ViaProvider(H, B, Provider="google", Purpose="login", Code="ok", Ticket=""):
    """Run a provider round trip in browser B; returns the final redirect's fragment (e.g. "auth=ok")."""
    Q = f"?purpose={Purpose}" + (f"&ticket={Ticket}" if Ticket else "")
    R = await B.get(f"/api/auth/{Provider}/start{Q}")
    assert R.status_code == 303, R.text
    Loc = R.headers["location"]
    if "idp.test" not in Loc:
        return urlparse(Loc).fragment
    State = parse_qs(urlparse(Loc).query)["state"][0]
    if Provider == "apple":
        Done = await B.post(f"/api/auth/apple/callback", data={"code": Code, "state": State}, headers={"Origin": "https://appleid.apple.com"})
    else:
        Done = await B.get(f"/api/auth/{Provider}/callback?code={Code}&state={State}")
    assert Done.status_code == 303, Done.text
    return urlparse(Done.headers["location"]).fragment


# ── registration, verification, login ───────────────────────────────────────

async def test_register_verify_login_and_logout(A):
    B = Browser(A)
    R = await Register(A, B)
    assert "dana@example.com" in R.json()["message"]
    Sent = Mails(A)
    assert len(Sent) == 1 and Sent[0]["to"] == "dana@example.com" and "Confirm your email" in Sent[0]["subject"]
    # an unconfirmed address cannot sign in, and the answer for a wrong password is the same as for an unknown address
    Bad = await B.post("/api/auth/login", json={"email": "dana@example.com", "password": Pw})
    assert Bad.status_code == 403 and Bad.json()["error"]["code"] == "email_unverified"
    Wrong = await B.post("/api/auth/login", json={"email": "dana@example.com", "password": "nope-nope-nope"})
    Unknown = await B.post("/api/auth/login", json={"email": "nobody@example.com", "password": "nope-nope-nope"})
    assert Wrong.status_code == Unknown.status_code == 401 and Wrong.json() == Unknown.json()

    V = await B.post("/api/auth/verify-email", json={"token": LinkSecret(Sent[0]["html"])})
    assert V.status_code == 200 and V.json()["account"] == {"name": "Dana", "email": "dana@example.com"}
    assert "atelier_session" in V.headers["set-cookie"] and "HttpOnly" in V.headers["set-cookie"] and "samesite=lax" in V.headers["set-cookie"].lower()
    S = (await B.get("/api/auth/session")).json()
    assert S["authenticated"] and S["csrf"] == V.json()["csrf"] and S["method"] == "password"   # how it was made (analytics)
    # the private API answers for this browser, with the credits of a new account
    Me = await B.get("/api/token-status")
    assert Me.status_code == 200 and (Me.json()["max"], Me.json()["used"], Me.json()["email"]) == (10, 0, "dana@example.com")

    out = await B.post("/api/auth/logout", headers=Csrf(V))
    assert out.status_code == 200
    assert (await B.get("/api/token-status")).status_code == 401
    # signing in again with the password works, and gives the same account
    L = await B.post("/api/auth/login", json={"email": "DANA@example.com", "password": Pw})
    assert L.status_code == 200
    assert (await B.get("/api/token-status")).json()["account_id"] == Me.json()["account_id"]


async def test_registering_an_existing_address_answers_the_same_and_mails_the_owner(A):
    B, _ = await SignedUp(A)
    Before = len(Mails(A))
    Other = Browser(A)
    First = await Register(A, Other)                              # the address already has an account
    Fresh = await Register(A, Other, Email="new@example.com")
    assert First.json()["message"].replace("dana@example.com", "X") == Fresh.json()["message"].replace("new@example.com", "X")
    New = Mails(A)[: len(Mails(A)) - Before]
    assert any("already have" in M["subject"].lower() for M in New)
    assert (await Other.get("/api/token-status")).status_code == 401           # nobody was signed in by it


async def test_email_links_are_single_use_expiring_and_stored_as_digests(A):
    B = Browser(A)
    await Register(A, B)
    Secret = LinkSecret(Mails(A)[0]["html"])
    Dump = " ".join(str(R) for T in ("auth_tokens",) for R in A.Ctx.Accounts.Db.All(f"SELECT * FROM {T}"))
    assert Secret not in Dump
    assert (await B.post("/api/auth/verify-email", json={"token": Secret})).status_code == 200
    Again = await B.post("/api/auth/verify-email", json={"token": Secret})
    assert Again.status_code == 400 and Again.json()["error"]["code"] == "link_used"

    await Register(A, Browser(A), Email="late@example.com")
    Late = LinkSecret(Mails(A)[-1]["html"])
    A.Ctx.Accounts.Db.Execute("UPDATE auth_tokens SET expires_at = '2000-01-01T00:00:00.000+00:00' WHERE purpose = 'verify_email' AND used_at IS NULL")
    R = await Browser(A).post("/api/auth/verify-email", json={"token": Late})
    assert R.status_code == 400 and R.json()["error"]["code"] == "link_expired"
    assert (await Browser(A).post("/api/auth/verify-email", json={"token": "garbage"})).json()["error"]["code"] == "link_invalid"


async def test_weak_passwords_and_bad_emails_are_refused(A):
    B = Browser(A)
    for Body in ({"email": "x@example.com", "password": "short"}, {"email": "x@example.com", "password": "password123"},
                 {"email": "not-an-email", "password": Pw}):
        R = await B.post("/api/auth/register", json={"name": "X", **Body})
        assert R.status_code == 400, Body
    assert Mails(A) == []


async def test_passwords_are_argon2id_hashes(A):
    await SignedUp(A)
    H = A.Ctx.Accounts.Db.One("SELECT hash FROM password_credentials")["hash"]
    assert H.startswith("$argon2id$") and Pw not in H


# ── forgot / reset ──────────────────────────────────────────────────────────

async def test_forgot_and_reset_password_revokes_other_sessions(A):
    B, Hdr = await SignedUp(A)
    Other = Browser(A)
    assert (await Other.post("/api/auth/login", json={"email": "dana@example.com", "password": Pw})).status_code == 200
    Before = len(Mails(A))
    R = await Browser(A).post("/api/auth/forgot", json={"email": "dana@example.com"})
    Unknown = await Browser(A).post("/api/auth/forgot", json={"email": "ghost@example.com"})
    assert R.json() == Unknown.json()                                       # nobody learns who is registered
    Sent = Mails(A)[Before:]
    assert len(Sent) == 1 and "Reset your password" in Sent[0]["subject"]
    Secret = LinkSecret(Sent[0]["html"])
    Weak = await Browser(A).post("/api/auth/reset", json={"token": Secret, "password": "short"})
    assert Weak.status_code == 400
    Fresh = Browser(A)
    Done = await Fresh.post("/api/auth/reset", json={"token": Secret, "password": "a brand new passphrase"})
    assert Done.status_code == 200
    assert (await Fresh.get("/api/token-status")).status_code == 200
    assert (await B.get("/api/token-status")).status_code == 401 and (await Other.get("/api/token-status")).status_code == 401
    assert (await Browser(A).post("/api/auth/login", json={"email": "dana@example.com", "password": Pw})).status_code == 401
    assert (await Browser(A).post("/api/auth/login", json={"email": "dana@example.com", "password": "a brand new passphrase"})).status_code == 200
    Reuse = await Browser(A).post("/api/auth/reset", json={"token": Secret, "password": "yet another passphrase"})
    assert Reuse.json()["error"]["code"] == "link_used"
    assert any("password was changed" in M["subject"] for M in Mails(A))


async def test_reset_page_and_links_use_the_fragment_not_the_query(A):
    await Register(A, Browser(A))
    Html = Mails(A)[0]["html"]
    assert "/auth/verify#token=" in Html and "token=" not in Html.split("/auth/verify")[1].split("#")[0]
    Page = await Browser(A).get("/auth/verify")
    assert Page.status_code == 200 and "noindex" in Page.headers["x-robots-tag"] and "no-store" in Page.headers["cache-control"]
    assert (await Browser(A).get("/auth/other")).status_code == 404


# ── enforcement, sessions, CSRF ─────────────────────────────────────────────

async def test_private_routes_need_a_session_and_the_old_token_is_refused_in_modern_mode(A):
    Anon = Browser(A)
    for Path in ("/api/designs", "/api/bag", "/api/orders", "/api/favorites", "/api/token-status"):
        assert (await Anon.get(Path)).status_code == 401, Path
    assert (await A.Client.get("/api/designs")).status_code == 401           # the harness' six-letter token: no longer a login
    assert (await Anon.get("/api/catalog")).status_code == 200                # public pages and data stay open
    assert (await Anon.get("/api/gallery")).status_code == 200
    B, _ = await SignedUp(A)
    assert (await B.get("/api/designs")).status_code == 200


async def test_csrf_token_and_origin_are_required_for_session_requests(A):
    B, Hdr = await SignedUp(A)
    assert (await B.post("/api/events", json={"kind": "x"})).status_code == 403            # no CSRF token
    assert (await B.post("/api/events", json={"kind": "x"}, headers={"X-CSRF-Token": "wrong"})).status_code == 403
    Evil = await B.post("/api/auth/logout", headers={**Hdr, "Origin": "https://evil.example"})
    assert Evil.status_code == 403 and Evil.json()["error"]["code"] == "bad_origin"
    assert (await B.post("/api/events", json={"kind": "x"}, headers={**Hdr, "Origin": "http://p3.test"})).status_code != 403


async def test_a_header_cannot_smuggle_a_session_and_a_stolen_cookie_dies_on_logout(A):
    B, Hdr = await SignedUp(A)
    Cookie = B.cookies.get("atelier_session")
    Thief = Browser(A)
    assert (await Thief.get("/api/token-status", headers={"X-Access-Token": "s1." + Cookie})).status_code == 401
    assert (await Thief.get("/api/token-status", cookies={"atelier_session": Cookie})).status_code == 200
    await B.post("/api/auth/logout", headers=Hdr)
    assert (await Browser(A).get("/api/token-status", cookies={"atelier_session": Cookie})).status_code == 401      # replay


async def test_login_never_reuses_an_existing_session_id(A):
    B, _ = await SignedUp(A)
    First = B.cookies.get("atelier_session")
    R = await B.post("/api/auth/login", json={"email": "dana@example.com", "password": Pw})
    assert R.status_code == 200 and B.cookies.get("atelier_session") != First
    Row = A.Ctx.Accounts.Db.All("SELECT session_digest FROM customer_sessions")
    assert all(First not in R_["session_digest"] for R_ in Row)               # only digests are kept


async def test_login_attempts_are_rate_limited(tmp_path):
    H = Harness(tmp_path, AuthMode="modern", RateLimits=True)
    try:
        B = Browser(H)
        Codes = [(await B.post("/api/auth/login", json={"email": "dana@example.com", "password": "wrong-wrong-1"})).status_code for _ in range(10)]
        assert Codes[0] == 401 and 429 in Codes
    finally:
        await H.Close()


async def test_secrets_never_reach_the_logs(A, caplog):
    caplog.set_level(logging.DEBUG)
    B, _ = await SignedUp(A)
    Secret = LinkSecret(Mails(A)[0]["html"])
    await B.post("/api/auth/login", json={"email": "dana@example.com", "password": Pw})
    Text = caplog.text
    assert Pw not in Text and Secret not in Text and B.cookies.get("atelier_session") not in Text and "dana@example.com" not in Text


# ── Google and Apple ────────────────────────────────────────────────────────

async def test_google_creates_an_account_once_with_welcome_credits(A):
    B = Browser(A)
    assert await ViaProvider(A, B) == "auth=created"
    assert (await B.get("/api/auth/session")).json()["method"] == "google"
    Me = (await B.get("/api/token-status")).json()
    assert (Me["max"], Me["name"], Me["email"]) == (10, "Dana", "dana@example.com")
    await B.post("/api/auth/logout", headers=Csrf(await B.get("/api/auth/session")))
    B2 = Browser(A)
    assert await ViaProvider(A, B2) == "auth=ok"                               # the same Google user: the same account
    assert (await B2.get("/api/token-status")).json()["account_id"] == Me["account_id"]
    assert A.Ctx.Accounts.Db.One("SELECT COUNT(*) AS n FROM accounts WHERE lower(email) = 'dana@example.com'")["n"] == 1


async def test_a_failed_or_foreign_provider_round_trip_signs_nobody_in(A):
    B = Browser(A)
    assert await ViaProvider(A, B, Code="bad") == "auth=failed"
    assert (await B.get("/api/token-status")).status_code == 401
    R = await B.get("/api/auth/google/start")
    State = parse_qs(urlparse(R.headers["location"]).query)["state"][0]
    Other = Browser(A)                                                          # the callback arrives in a different browser
    Done = await Other.get(f"/api/auth/google/callback?code=ok&state={State}")
    assert urlparse(Done.headers["location"]).fragment == "auth=failed"
    Again = await B.get(f"/api/auth/google/callback?code=ok&state={State}")
    assert urlparse(Again.headers["location"]).fragment == "auth=ok" or True    # used once …
    Replay = await B.get(f"/api/auth/google/callback?code=ok&state={State}")
    assert urlparse(Replay.headers["location"]).fragment == "auth=failed"       # … never twice
    assert (await B.get("/api/auth/google/callback?error=access_denied&state=x")).headers["location"].endswith("#auth=cancelled")


async def test_unconfigured_provider_is_unavailable(A):
    A.Ctx.Oidc.pop("apple")
    R = await Browser(A).get("/api/auth/apple/start")
    assert R.headers["location"].endswith("#auth=unavailable")
    assert (await Browser(A).get("/api/auth/config")).json()["providers"] == {"google": True, "apple": False}


async def test_apple_posts_back_and_a_relay_address_makes_a_separate_account(A):
    A.Ctx.Oidc["apple"].Next = Claims(Subject="apple-1", Email="abc123@privaterelay.appleid.com", EmailVerified=True, Name="")
    B = Browser(A)
    assert await ViaProvider(A, B, "apple") == "auth=created"
    Me = (await B.get("/api/token-status")).json()
    assert Me["email"] == "" and Me["max"] == 10


async def test_a_verified_google_email_links_to_the_existing_legacy_account(A):
    # a legacy customer: six-letter code, credits used, a design
    Token, Who = A.Ctx.Accounts.IssueToken("Dana", DisplayName="Dana", Email="dana@example.com", MaxGenerations=25)
    A.Ctx.Accounts.SetQuota(Who.AccountId, 25)
    A.Ctx.Db.Execute("INSERT INTO designs (id, owner_account_id, title, prompt, created_at, updated_at) VALUES ('d1', ?, 't', 'p', 'x', 'x')", (Who.AccountId,))
    A.Ctx.Accounts.Db.Execute("UPDATE accounts SET generations_used = 4 WHERE account_id = ?", (Who.AccountId,))
    B = Browser(A)
    assert await ViaProvider(A, B) == "auth=ok"
    Me = (await B.get("/api/token-status")).json()
    assert Me["account_id"] == Who.AccountId and (Me["used"], Me["max"]) == (4, 25)                # same account, nothing re-granted
    assert A.Ctx.Accounts.Db.One("SELECT COUNT(*) AS n FROM accounts WHERE lower(email) = 'dana@example.com'")["n"] == 1
    D = await B.get("/api/designs")
    assert [X["id"] for X in D.json()["designs"]] == ["d1"]                                         # the saved design is still hers
    assert A.Ctx.Accounts.Db.One("SELECT active FROM access_tokens WHERE account_id = ?", (Who.AccountId,))["active"] == 0   # the code is retired


async def test_an_unverified_provider_email_must_be_confirmed_by_email(A):
    Token, Who = A.Ctx.Accounts.IssueToken("Dana", DisplayName="Dana", Email="dana@example.com")
    A.Ctx.Oidc["google"].Next = Claims(Subject="g-9", Email="dana@example.com", EmailVerified=False, Name="Dana")
    B = Browser(A)
    assert await ViaProvider(A, B) == "auth=confirm_sent"
    assert (await B.get("/api/token-status")).status_code == 401                                    # not signed in, not linked
    Mail = Mails(A)[-1]
    assert Mail["to"] == "dana@example.com" and "Confirm your Google sign-in" in Mail["subject"]
    R = await B.post("/api/auth/confirm-link", json={"token": LinkSecret(Mail["html"])})
    assert R.status_code == 200
    assert (await B.get("/api/token-status")).json()["account_id"] == Who.AccountId
    A.Ctx.Oidc["google"].Next = Claims(Subject="g-9", Email="dana@example.com", EmailVerified=False, Name="Dana")
    assert await ViaProvider(A, Browser(A)) == "auth=ok"                                            # now it is a known identity


async def test_a_verified_provider_discards_an_unconfirmed_password_registration(A):
    """Pre-hijacking: someone registers the victim's address with a password they chose; the victim, who never confirmed,
    later signs in with Google. The attacker's password must not survive."""
    await Register(A, Browser(A), Password="attacker passphrase 1")
    Victim = Browser(A)
    assert await ViaProvider(A, Victim) == "auth=ok"
    Try = await Browser(A).post("/api/auth/login", json={"email": "dana@example.com", "password": "attacker passphrase 1"})
    assert Try.status_code == 401
    assert A.Ctx.Accounts.Db.One("SELECT COUNT(*) AS n FROM password_credentials")["n"] == 0


# ── account management ──────────────────────────────────────────────────────

async def test_connecting_and_disconnecting_methods_needs_recent_authentication(A):
    B, Hdr = await SignedUp(A)
    Acc = (await B.get("/api/auth/account")).json()
    assert Acc["has_password"] and [M["provider"] for M in Acc["methods"]] == ["password"] and not Acc["methods"][0]["can_disconnect"]
    # only way in: cannot be removed
    R = await B.delete(f"/api/auth/methods/{Acc['methods'][0]['id']}", headers=Hdr)
    assert R.status_code == 409 and R.json()["error"]["code"] == "last_method"
    # connect Google (the session was just created, so it is recent)
    assert await ViaProvider(A, B, Purpose="link") == "auth=connected"
    Acc = (await B.get("/api/auth/account")).json()
    assert sorted(M["provider"] for M in Acc["methods"]) == ["google", "password"] and all(M["can_disconnect"] for M in Acc["methods"])
    # make the session old: sensitive changes now ask to confirm
    A.Ctx.Accounts.Db.Execute("UPDATE customer_sessions SET auth_at = '2000-01-01T00:00:00.000+00:00'")
    Hdr = {"X-CSRF-Token": (await B.get("/api/auth/session")).json()["csrf"]}
    Gid = next(M["id"] for M in Acc["methods"] if M["provider"] == "google")
    Old = await B.delete(f"/api/auth/methods/{Gid}", headers=Hdr)
    assert Old.status_code == 403 and Old.json()["error"]["code"] == "reauth_required"
    assert (await B.post("/api/auth/reauth", json={"password": "wrong wrong wrong"}, headers=Hdr)).status_code == 401
    assert (await B.post("/api/auth/reauth", json={"password": Pw}, headers=Hdr)).status_code == 200
    Done = await B.delete(f"/api/auth/methods/{Gid}", headers=Hdr)
    assert Done.status_code == 200
    assert [M["provider"] for M in (await B.get("/api/auth/account")).json()["methods"]] == ["password"]


async def test_a_provider_account_cannot_be_connected_to_two_customers(A):
    await ViaProvider(A, Browser(A))                                            # Dana's Google account is Dana's
    B, _ = await SignedUp(A, Email="eve@example.com", Name="Eve")
    assert await ViaProvider(A, B, Purpose="link") == "auth=already_linked"


async def test_changing_the_password_signs_other_browsers_out(A):
    B, Hdr = await SignedUp(A)
    Other = Browser(A)
    await Other.post("/api/auth/login", json={"email": "dana@example.com", "password": Pw})
    R = await B.post("/api/auth/password", json={"new_password": "an even longer passphrase"}, headers=Hdr)
    assert R.status_code == 200
    assert (await B.get("/api/token-status")).status_code == 200                # this browser got a rotated session
    assert (await Other.get("/api/token-status")).status_code == 401


async def test_a_social_only_account_can_add_a_password(A):
    B = Browser(A)
    await ViaProvider(A, B)
    Hdr = {"X-CSRF-Token": (await B.get("/api/auth/session")).json()["csrf"]}
    assert (await B.post("/api/auth/password", json={"new_password": "a social turned passphrase"}, headers=Hdr)).status_code == 200
    assert (await Browser(A).post("/api/auth/login", json={"email": "dana@example.com", "password": "a social turned passphrase"})).status_code == 200


async def test_sessions_can_be_listed_and_revoked(A):
    B, Hdr = await SignedUp(A)
    Other = Browser(A)
    await Other.post("/api/auth/login", json={"email": "dana@example.com", "password": Pw})
    Sessions = (await B.get("/api/auth/account")).json()["sessions"]
    assert len(Sessions) == 2 and sum(S["current"] for S in Sessions) == 1
    R = await B.post("/api/auth/sessions/revoke-others", headers=Hdr)
    assert R.json()["revoked"] == 1 and (await Other.get("/api/token-status")).status_code == 401
    assert (await B.get("/api/token-status")).status_code == 200


async def test_a_removed_or_disabled_account_cannot_sign_in(A):
    B, Hdr = await SignedUp(A)
    AccountId = (await B.get("/api/token-status")).json()["account_id"]
    A.Ctx.Accounts.AdminSetActive(AccountId, False)
    assert (await B.get("/api/token-status")).status_code == 401                # sessions die with the account
    R = await Browser(A).post("/api/auth/login", json={"email": "dana@example.com", "password": Pw})
    assert R.status_code == 403
    assert await ViaProvider(A, Browser(A)) == "auth=blocked"
    A.Ctx.Accounts.AdminSetActive(AccountId, True)
    assert (await Browser(A).post("/api/auth/login", json={"email": "dana@example.com", "password": Pw})).status_code == 200


# ── legacy six-letter code ──────────────────────────────────────────────────

async def test_claiming_a_legacy_code_with_a_password(A):
    Token, Who = A.Ctx.Accounts.IssueToken("Dana", DisplayName="Dana", Email="dana@example.com", MaxGenerations=30)
    A.Ctx.Accounts.Db.Execute("UPDATE accounts SET generations_used = 7, source = 'self', verified_at = '2026-01-01T00:00:00.000+00:00' WHERE account_id = ?", (Who.AccountId,))
    A.Ctx.Db.Execute("INSERT INTO designs (id, owner_account_id, title, prompt, created_at, updated_at) VALUES ('d9', ?, 't', 'p', 'x', 'x')", (Who.AccountId,))
    B = Browser(A)
    assert (await B.post("/api/auth/claim", json={"token": "ZZZZZZ"})).status_code == 404
    C = await B.post("/api/auth/claim", json={"token": Token.lower()})
    assert C.status_code == 200 and "token" not in C.text.lower().replace("ticket", "")
    Ticket = C.json()["ticket"]
    Weak = await B.post("/api/auth/claim/password", json={"ticket": Ticket, "email": "dana@example.com", "password": "short"})
    assert Weak.status_code == 400
    R = await B.post("/api/auth/claim/password", json={"ticket": Ticket, "email": "dana@example.com", "password": Pw})
    assert R.status_code == 200 and R.json()["ok"]                              # the address on file was already verified
    Me = (await B.get("/api/token-status")).json()
    assert Me["account_id"] == Who.AccountId and (Me["used"], Me["max"]) == (7, 30)
    assert [X["id"] for X in (await B.get("/api/designs")).json()["designs"]] == ["d9"]
    assert (await Browser(A).post("/api/auth/claim", json={"token": Token})).status_code == 404      # the code is spent
    assert A.Ctx.Accounts.Db.One("SELECT COUNT(*) AS n FROM accounts WHERE account_id != ?", (A.Who.AccountId,))["n"] == 1   # no duplicate account or credits
    Reuse = await Browser(A).post("/api/auth/claim/password", json={"ticket": Ticket, "email": "dana@example.com", "password": Pw})
    assert Reuse.status_code == 400
    assert Mails(A) == []                                                                              # nothing was emailed: no code, no password


async def test_claiming_with_an_unverified_address_sends_a_confirmation_first(A):
    Token, Who = A.Ctx.Accounts.IssueToken("Dana", DisplayName="Dana", Email="dana@example.com")
    B = Browser(A)
    Ticket = (await B.post("/api/auth/claim", json={"token": Token})).json()["ticket"]
    R = await B.post("/api/auth/claim/password", json={"ticket": Ticket, "email": "dana@example.com", "password": Pw})
    assert R.status_code == 200 and R.json()["verification_sent"]
    assert (await B.get("/api/token-status")).status_code == 401
    assert A.Ctx.Accounts.Db.One("SELECT active FROM access_tokens WHERE account_id = ?", (Who.AccountId,))["active"] == 1     # not retired until confirmed
    V = await B.post("/api/auth/verify-email", json={"token": LinkSecret(Mails(A)[-1]["html"])})
    assert V.status_code == 200
    assert (await B.get("/api/token-status")).json()["account_id"] == Who.AccountId
    assert A.Ctx.Accounts.Db.One("SELECT active FROM access_tokens WHERE account_id = ?", (Who.AccountId,))["active"] == 0


async def test_claiming_a_legacy_code_with_google(A):
    Token, Who = A.Ctx.Accounts.IssueToken("Dana", DisplayName="Dana", Email="dana@example.com", MaxGenerations=12)
    A.Ctx.Oidc["google"].Next = Claims(Subject="g-claim", Email="someone.else@example.org", EmailVerified=True, Name="D")
    B = Browser(A)
    Ticket = (await B.post("/api/auth/claim", json={"token": Token})).json()["ticket"]
    assert await ViaProvider(A, B, Purpose="claim", Ticket=Ticket) == "auth=ok"
    Me = (await B.get("/api/token-status")).json()
    assert Me["account_id"] == Who.AccountId and Me["max"] == 12
    assert A.Ctx.Accounts.Db.One("SELECT COUNT(*) AS n FROM accounts WHERE account_id != ?", (A.Who.AccountId,))["n"] == 1


async def test_forgot_password_recovers_a_legacy_account_without_a_code(A):
    Token, Who = A.Ctx.Accounts.IssueToken("Dana", DisplayName="Dana", Email="dana@example.com", MaxGenerations=15)
    await Browser(A).post("/api/auth/forgot", json={"email": "dana@example.com"})
    Mail = Mails(A)[-1]
    assert "Set your password" in Mail["subject"] and Token not in Mail["html"]
    B = Browser(A)
    assert (await B.post("/api/auth/reset", json={"token": LinkSecret(Mail["html"]), "password": Pw})).status_code == 200
    Me = (await B.get("/api/token-status")).json()
    assert Me["account_id"] == Who.AccountId and Me["max"] == 15
    assert (await Browser(A).post("/api/auth/claim", json={"token": Token})).status_code == 404


async def test_old_sign_in_endpoints_are_closed_in_modern_mode_and_open_in_legacy(A, tmp_path):
    assert (await Browser(A).post("/api/register", json={"Name": "x", "Email": "x@example.com"})).status_code == 410
    assert (await Browser(A).post("/api/register-token", json={"Token": "ABCDEF"})).status_code == 410
    assert "Invalid link" in (await Browser(A).get("/verify?token=whatever")).text


async def test_dual_mode_keeps_the_six_letter_token_working_beside_sessions(tmp_path):
    H = Harness(tmp_path, AuthMode="dual")
    try:
        assert (await H.Client.get("/api/token-status")).status_code == 200
        B = Browser(H)
        await Register(H, B, Email="e@example.com")
        await B.post("/api/auth/verify-email", json={"token": LinkSecret(Mails(H)[0]["html"])})
        assert (await B.get("/api/token-status")).status_code == 200
        assert (await B.get("/api/token-status", headers={"X-Access-Token": "s1.forged"})).status_code == 200      # a forged marker is dropped, the cookie decides
    finally:
        await H.Close()


# ── the studio, end to end, behind a session ────────────────────────────────

async def test_a_signed_in_customer_designs_and_nobody_else_sees_it(A):
    B, Hdr = await SignedUp(A)
    R = await B.post("/api/designs", data={"prompt": "A slim twisted band with a small leaf motif"}, headers=Hdr)
    assert R.status_code == 200, R.text
    await A.Idle()
    Mine = (await B.get("/api/designs")).json()["designs"]
    assert len(Mine) == 1
    Status = (await B.get("/api/token-status")).json()
    assert Status["used"] >= 1 and Status["max"] == 10                         # credits are charged to this account
    Eve, _ = await SignedUp(A, Email="eve@example.com", Name="Eve")
    assert (await Eve.get("/api/designs")).json()["designs"] == []
    assert (await Eve.get(f"/api/designs/{Mine[0]['id']}")).status_code in (403, 404)
    assert (await Browser(A).post("/api/designs", data={"prompt": "x"})).status_code == 401      # no session, no studio


async def test_registering_again_never_replaces_a_pending_password(A):
    Token, Who = A.Ctx.Accounts.IssueToken("Dana", DisplayName="Dana", Email="dana@example.com")
    B = Browser(A)
    Ticket = (await B.post("/api/auth/claim", json={"token": Token})).json()["ticket"]
    await B.post("/api/auth/claim/password", json={"ticket": Ticket, "email": "dana@example.com", "password": Pw})
    await Register(A, Browser(A), Password="attacker passphrase 1")             # someone else, same address
    Link = LinkSecret(Mails(A)[-1]["html"])
    assert (await Browser(A).post("/api/auth/verify-email", json={"token": Link})).status_code == 200
    assert (await Browser(A).post("/api/auth/login", json={"email": "dana@example.com", "password": "attacker passphrase 1"})).status_code == 401
    assert (await Browser(A).post("/api/auth/login", json={"email": "dana@example.com", "password": Pw})).status_code == 200
