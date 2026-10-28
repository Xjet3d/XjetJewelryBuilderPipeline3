"""Terms acceptance (2026-10-29): every new account accepts the Terms of Service and the Privacy Policy, and the
acceptance is recorded with the account in one transaction — an email sign-up when its link is used (no account before),
a new Google or Apple customer after identification, an account XJet made at its first sign-in. Existing accounts are
never asked or recorded after the fact. The legal texts are versioned and archived automatically; a change during a
sign-up asks for the new version; the server refuses designs, credits and orders until an acceptance is required and
missing; the Admin sees each account's status read only, and pending sign-ups with masked addresses. Mail goes to the
outbox, the identity providers are fakes: no network, no paid call."""

import re

import pytest

from p3.accounts import TermsRequired
from p3.accounts.consent import MaskEmail, Sha256
from p3.oidc import Claims
from tests.conftest import Harness
from tests.test_auth import Browser, Csrf, FakeIdp, LinkSecret, Mails, Pw, ViaProvider

AdminKey = "consent-admin-key"
AdminHeaders = {"Authorization": f"Bearer {AdminKey}"}


@pytest.fixture
async def A(tmp_path):
    """The site as Atelier runs it: email and password, Google and (in tests only) Apple."""
    Obj = Harness(tmp_path, AuthMode="dual", AdminKey=AdminKey)
    Obj.Ctx.Oidc = {"google": FakeIdp("google"), "apple": FakeIdp("apple")}
    yield Obj
    await Obj.Close()


@pytest.fixture
async def L(tmp_path):
    """The site as proto runs it: the older sign-in (an emailed six-letter code)."""
    Obj = Harness(tmp_path, AuthMode="legacy", AdminKey=AdminKey)
    yield Obj
    await Obj.Close()


async def Legal(B) -> dict:
    R = await B.get("/api/auth/terms")
    assert R.status_code == 200, R.text
    return R.json()


def Accept(L: dict) -> dict:
    return {"accept_terms": True, "terms": L["terms"], "privacy": L["privacy"]}


def Rows(H, Sql, Params=()) -> list[dict]:
    return H.Ctx.Accounts.Db.All(Sql, Params)


def Consents(H, AccountId=None) -> list[dict]:
    return Rows(H, "SELECT * FROM consents" + (" WHERE account_id = ?" if AccountId else "") + " ORDER BY id",
                (AccountId,) if AccountId else ())


def AccountsFor(H, Email) -> list[dict]:
    return Rows(H, "SELECT * FROM accounts WHERE lower(email) = ?", (Email.lower(),))


async def SignUp(H, B, Email="dana@example.com", Name="Dana", **Extra) -> dict:
    L_ = await Legal(B)
    R = await B.post("/api/auth/register", json={"name": Name, "email": Email, "password": Pw, **Accept(L_), **Extra})
    assert R.status_code == 200, R.text
    return L_


def Link(H, Email):
    return LinkSecret([M for M in Mails(H) if M["to"] == Email][-1]["html"])


def PublishDesignUse(H, On=True):
    """The owner's switch for the design-use clause (P3_TERMS_DESIGN_USE): off unless approved."""
    H.Settings.TermsDesignUse = On


# ── the legal texts ─────────────────────────────────────────────────────────────

async def test_the_legal_texts_are_versioned_and_archived_automatically(A):
    B = Browser(A)
    L1 = await Legal(B)
    assert L1["terms"]["version"] == 1 and L1["privacy"]["version"] == 1 and L1["design_use"] is False
    Archive = {R["doc"]: R for R in Rows(A, "SELECT * FROM legal_documents")}
    assert set(Archive) == {"terms", "privacy"}
    # The text a reader of each page gets (served text, base path left out), and its hash is what customers accept
    assert "You create an account and sign in with your email address and a password" in Archive["terms"]["text"]
    assert "What we collect" in Archive["privacy"]["text"] and "Terms of Service" not in Archive["privacy"]["text"][:40]
    assert Sha256(Archive["terms"]["text"]) == L1["terms"]["sha256"] and Sha256(Archive["privacy"]["text"]) == L1["privacy"]["sha256"]
    assert "royalty-free" not in Archive["terms"]["text"]                       # the design-use clause is not published
    assert "royalty-free" not in (await B.get("/terms")).text and "royalty-free" not in (await B.get("/")).text
    # The owner publishes it: the Terms' text changes, so new sign-ups accept version 2; the Privacy Policy stays version 1
    PublishDesignUse(A)
    L2 = await Legal(B)
    assert (L2["terms"]["version"], L2["privacy"]["version"], L2["design_use"]) == (2, 1, True)
    Terms2 = Rows(A, "SELECT text FROM legal_documents WHERE doc = 'terms' AND version = 2")[0]["text"]
    assert "worldwide, non-exclusive, royalty-free license" in Terms2 and "royalty-free" in (await B.get("/terms")).text
    # The same text again is the version it already was: nothing new is archived
    PublishDesignUse(A, False)
    assert (await Legal(B))["terms"] == L1["terms"]
    assert len(Rows(A, "SELECT 1 FROM legal_documents")) == 3


# ── email and password ──────────────────────────────────────────────────────────

async def test_an_email_sign_up_is_no_account_until_its_link_is_used_and_then_carries_the_acceptance(A):
    B = Browser(A)
    L_ = await Legal(B)
    Base = {"name": "Dana", "email": "dana@example.com", "password": Pw}
    No = await B.post("/api/auth/register", json=Base)
    assert No.status_code == 400 and No.json()["error"]["code"] == "terms_required"
    Text = await B.post("/api/auth/register", json={**Base, "accept_terms": "true", "terms": L_["terms"], "privacy": L_["privacy"]})
    assert Text.status_code == 400                                              # only a real tick (true) counts
    Old = await B.post("/api/auth/register", json={**Base, **Accept(L_), "terms": {"version": 1, "sha256": "0" * 64}})
    assert Old.status_code == 409 and Old.json()["error"]["code"] == "terms_changed"
    assert Old.json()["error"]["legal"]["terms"] == L_["terms"]
    assert not Mails(A)

    await SignUp(A, B)
    assert AccountsFor(A, "dana@example.com") == []                             # no account, no credits
    Pending = Rows(A, "SELECT * FROM pending_registrations")
    assert len(Pending) == 1 and Pending[0]["method"] == "password" and Pending[0]["terms_version"] == 1
    Bad = await B.post("/api/auth/login", json={"email": "dana@example.com", "password": Pw})
    assert Bad.status_code == 401                                               # nothing to sign in to yet

    V = await B.post("/api/auth/verify-email", json={"token": Link(A, "dana@example.com")})
    assert V.status_code == 200 and V.json()["created"] is True and "atelier_session" in V.headers["set-cookie"]
    (Acc,) = AccountsFor(A, "dana@example.com")
    assert Acc["source"] == "auth" and Acc["verified_at"] and Acc["max_generations"] == 10 and not Acc["terms_required"]
    (C,) = Consents(A, Acc["account_id"])
    assert (C["method"], C["terms_version"], C["terms_sha256"], C["privacy_version"], C["privacy_sha256"]) == \
        ("password", 1, L_["terms"]["sha256"], 1, L_["privacy"]["sha256"])
    assert C["accepted_at"].endswith("+00:00")                                  # UTC
    # The sign-up is used up: only its digest and times stay (for "already used"), nothing of the person, gone in 24 h
    (Used,) = Rows(A, "SELECT * FROM pending_registrations")
    assert Used["used_at"] and Used["email"] is None and Used["password_hash"] is None and not Used["display_name"]
    assert A.Ctx.Accounts.Consent.ListPending() == []
    # Signed in, the customer designs at once; the link was used once
    assert (await B.post("/api/designs", data={"prompt": "A slim band with a leaf"}, headers=Csrf(V))).status_code == 200
    Again = await Browser(A).post("/api/auth/verify-email", json={"token": Link(A, "dana@example.com")})
    assert Again.status_code == 400 and Again.json()["error"]["code"] == "link_used" and len(Consents(A)) == 1
    # And the password works from now on
    assert (await Browser(A).post("/api/auth/login", json={"email": "dana@example.com", "password": Pw})).status_code == 200


async def test_a_change_of_the_terms_during_a_sign_up_asks_for_the_new_version(A):
    B = Browser(A)
    L1 = await SignUp(A, B)
    PublishDesignUse(A)                                                         # the Terms change before the link is opened
    Secret = Link(A, "dana@example.com")
    V = await B.post("/api/auth/verify-email", json={"token": Secret})
    assert V.status_code == 409 and V.json()["error"]["code"] == "terms_changed"
    L2 = V.json()["error"]["legal"]
    assert L2["terms"]["version"] == 2 and L2["design_use"] is True and AccountsFor(A, "dana@example.com") == []
    Stale = await B.post("/api/auth/verify-email", json={"token": Secret, **Accept(L1)})
    assert Stale.status_code == 409                                             # the old version is no longer accepted
    Ok = await B.post("/api/auth/verify-email", json={"token": Secret, **Accept(L2)})
    assert Ok.status_code == 200
    (C,) = Consents(A)
    assert (C["terms_version"], C["terms_sha256"]) == (2, L2["terms"]["sha256"])


async def test_a_pending_sign_up_expires_after_24_hours_and_makes_nothing(A):
    B = Browser(A)
    await SignUp(A, B)
    (P,) = Rows(A, "SELECT created_at, expires_at FROM pending_registrations")
    assert P["expires_at"][:13] > P["created_at"][:13]                          # a day later
    A.Ctx.Accounts.Db.Execute("UPDATE pending_registrations SET expires_at = '2000-01-01T00:00:00.000+00:00'")
    V = await B.post("/api/auth/verify-email", json={"token": Link(A, "dana@example.com")})
    assert V.status_code == 400 and V.json()["error"]["code"] == "link_expired"
    assert AccountsFor(A, "dana@example.com") == [] and Consents(A) == []
    assert A.Ctx.Accounts.Consent.ListPending() == [] and Rows(A, "SELECT 1 FROM pending_registrations") == []


# ── Google and Apple ────────────────────────────────────────────────────────────

async def test_google_identifies_first_and_a_new_customer_accepts_before_the_account_exists(A):
    B = Browser(A)
    Before = len(Rows(A, "SELECT 1 FROM accounts"))
    assert await ViaProvider(A, B, Accept=False) == "auth=accept_terms"
    assert "atelier_pending" in B.cookies and len(Rows(A, "SELECT 1 FROM accounts")) == Before
    assert (await B.get("/api/auth/session")).json()["authenticated"] is False  # not signed in: no account yet
    P = (await B.get("/api/auth/pending")).json()
    assert P["pending"] and P["method"] == "google" and P["email"] == "dana@example.com" and P["legal"]["terms"]["version"] == 1

    assert (await B.post("/api/auth/accept", json={})).status_code == 400
    Stale = await B.post("/api/auth/accept", json={**Accept(P["legal"]), "privacy": {"version": 1, "sha256": "x"}})
    assert Stale.status_code == 409
    R = await B.post("/api/auth/accept", json=Accept(P["legal"]))
    assert R.status_code == 200 and R.json()["created"] is True and "atelier_session" in R.headers["set-cookie"]
    assert "atelier_pending" not in B.cookies
    (Acc,) = AccountsFor(A, "dana@example.com")
    assert Acc["verified_at"] and Acc["max_generations"] == 10
    (C,) = Consents(A, Acc["account_id"])
    assert C["method"] == "google"
    Me = await B.get("/api/token-status")
    assert Me.status_code == 200 and Me.json()["terms_required"] is False

    # The same Google user again is an existing customer: signed in at once, no step, nothing recorded again
    Other = Browser(A)
    assert await ViaProvider(A, Other) == "auth=ok"
    assert len(Consents(A)) == 1 and A.Ctx.Accounts.Consent.ListPending() == []
    assert Rows(A, "SELECT provider_subject, email FROM pending_registrations") == [{"provider_subject": None, "email": None}]
    assert (await B.post("/api/auth/accept", json=Accept(P["legal"]))).status_code == 400   # the sign-up is used up


async def test_an_existing_customer_signs_in_with_google_without_a_new_step_or_record(A):
    _Token, Who = A.Ctx.Accounts.IssueToken("Dana", DisplayName="Dana", Email="dana@example.com")
    B = Browser(A)
    assert await ViaProvider(A, B) == "auth=ok"                                 # linked to the account the address has
    assert (await B.get("/api/token-status")).json()["account_id"] == Who.AccountId
    assert Consents(A) == [] and A.Ctx.Accounts.Consent.Status(Who.AccountId)["status"] == "legacy"


async def test_apple_follows_the_same_path_and_stays_off_where_it_is_not_configured(A, tmp_path):
    A.Ctx.Oidc["apple"].Next = Claims(Subject="apple-1", Email="x7@privaterelay.appleid.com", EmailVerified=True, Name="")
    B = Browser(A)
    assert await ViaProvider(A, B, Provider="apple", Accept=False) == "auth=accept_terms"
    P = (await B.get("/api/auth/pending")).json()
    assert P["method"] == "apple" and P["email"] == ""                         # a relay address is not shown
    assert (await B.post("/api/auth/accept", json=Accept(P["legal"]))).status_code == 200
    (C,) = Consents(A)
    assert C["method"] == "apple"
    # Without Apple's credentials (as on proto and Atelier today) there is no Apple sign-in at all
    Plain = Harness(tmp_path / "plain", AuthMode="dual")
    try:
        assert (await Browser(Plain).get("/api/auth/config")).json()["providers"]["apple"] is False
        assert await ViaProvider(Plain, Browser(Plain), Provider="apple") == "auth=unavailable"
    finally:
        await Plain.Close()


async def test_a_google_sign_up_that_is_not_accepted_leaves_nothing_behind(A):
    B = Browser(A)
    assert await ViaProvider(A, B, Accept=False) == "auth=accept_terms"
    assert (await B.delete("/api/auth/pending")).status_code == 200
    assert (await B.get("/api/auth/pending")).json() == {"pending": False}
    assert AccountsFor(A, "dana@example.com") == [] and Rows(A, "SELECT 1 FROM pending_registrations") == []


# ── accounts XJet makes ─────────────────────────────────────────────────────────

async def test_an_account_xjet_creates_accepts_at_its_first_sign_in_before_designing(L):
    Made = (await L.Client.post("/api/admin/users", json={"Name": "Noa", "Email": "noa@example.com", "MaxGenerations": 5},
                                headers=AdminHeaders)).json()
    assert Made["terms"]["status"] == "pending"
    Code = {"X-Access-Token": Made["token"]}
    B = Browser(L)
    In = await B.post("/api/register-token", json={"Token": Made["token"]})
    assert In.status_code == 200                                                 # the sign-in itself works
    Me = await B.get("/api/token-status", headers=Code)
    assert Me.status_code == 200 and Me.json()["terms_required"] is True
    # Nothing else until it accepts: designs, credits, the bag, orders, the gallery's "Make it yours"
    for Method, Path, Kw in (("post", "/api/designs", {"data": {"prompt": "A band"}}), ("get", "/api/designs", {}),
                             ("get", "/api/bag", {}), ("post", "/api/orders", {"json": {}}),
                             ("post", "/api/gallery/any/start", {"json": {}}), ("get", "/api/favorites", {})):
        R = await getattr(B, Method)(Path, headers=Code, **Kw)
        assert R.status_code == 403 and R.json()["error"]["code"] == "terms_required", (Path, R.text)
    with pytest.raises(TermsRequired):
        L.Ctx.Accounts.AuthorizeSpend(L.Ctx.Accounts.Authenticate(Made["token"]), "design", 1)
    L_ = await Legal(B)
    assert (await B.post("/api/auth/accept-terms", json={}, headers=Code)).status_code == 400
    assert (await B.post("/api/auth/accept-terms", json={**Accept(L_), "terms": {"version": 9, "sha256": "x"}}, headers=Code)).status_code == 409
    Ok = await B.post("/api/auth/accept-terms", json=Accept(L_), headers=Code)
    assert Ok.status_code == 200 and Ok.json()["status"] == "accepted"
    (C,) = Consents(L, Made["account_id"])
    assert (C["method"], C["terms_version"]) == ("admin", 1)
    assert (await B.post("/api/designs", data={"prompt": "A band"}, headers=Code)).status_code == 200
    # Accepting again records nothing more
    assert (await B.post("/api/auth/accept-terms", json=Accept(L_), headers=Code)).json()["status"] == "not_required"
    assert len(Consents(L, Made["account_id"])) == 1


async def test_an_account_created_with_the_cli_accepts_at_its_first_sign_in(L):
    Token, Who = L.Ctx.Accounts.IssueToken("QA", DisplayName="QA", Email="qa@xjet3d.com", TermsRequired=True)
    R = await Browser(L).post("/api/designs", data={"prompt": "A band"}, headers={"X-Access-Token": Token})
    assert R.status_code == 403 and L.Ctx.Accounts.Consent.Status(Who.AccountId)["status"] == "pending"


# ── existing accounts ───────────────────────────────────────────────────────────

async def test_existing_accounts_are_unaffected_and_never_recorded_after_the_fact(L):
    # The harness's own account was made like every account before acceptance existed
    assert (await L.Client.post("/api/designs", data={"prompt": "A slim band"})).status_code == 200
    Me = (await L.Client.get("/api/token-status")).json()
    assert Me["terms_required"] is False
    L_ = await Legal(Browser(L))
    R = await L.Client.post("/api/auth/accept-terms", json=Accept(L_))
    assert R.status_code == 200 and R.json()["status"] == "not_required"
    assert Consents(L) == [] and L.Ctx.Accounts.Consent.Status(L.Who.AccountId)["status"] == "legacy"
    assert (await L.Client.get("/api/bag")).status_code == 200 and (await L.Client.get("/api/designs")).status_code == 200


async def test_a_registration_begun_before_and_finished_now_accepts_at_its_first_sign_in(A):
    """An address that registered with a password before acceptance existed and confirms only now: the account was
    made then, so it accepts at its first sign-in (nothing is recorded for it until it does)."""
    Id = A.Ctx.Accounts.Identity
    AccountId = "p3local:acct_" + "0" * 32
    A.Ctx.Accounts.Db.Execute("INSERT INTO accounts (account_id, display_name, email, created_at, source, max_generations, "
                              "activated_at) VALUES (?, 'Old', 'old@example.com', '2026-10-01T00:00:00.000+00:00', 'auth', 10, "
                              "'2026-10-01T00:00:00.000+00:00')", (AccountId,))
    Id.SetPassword(AccountId, Pw, "old@example.com", False)
    B = Browser(A)
    V = await B.post("/api/auth/verify-email", json={"token": Id.NewToken("verify_email", AccountId, "old@example.com")})
    assert V.status_code == 200 and (await B.get("/api/auth/session")).json()["terms_required"] is True
    assert (await B.post("/api/designs", data={"prompt": "A band"}, headers=Csrf(V))).status_code == 403
    L_ = await Legal(B)
    assert (await B.post("/api/auth/accept-terms", json=Accept(L_))).status_code == 403     # a session needs its CSRF token
    Ok = await B.post("/api/auth/accept-terms", json=Accept(L_), headers=Csrf(V))
    assert Ok.status_code == 200 and Ok.json()["status"] == "accepted"
    (C,) = Consents(A, AccountId)
    assert C["method"] == "password"
    assert (await B.post("/api/designs", data={"prompt": "A band"}, headers=Csrf(V))).status_code == 200


# ── bypass attempts ─────────────────────────────────────────────────────────────

async def test_the_acceptance_cannot_be_bypassed(A):
    B = Browser(A)
    assert await ViaProvider(A, B, Accept=False) == "auth=accept_terms"
    Cookie = B.cookies["atelier_pending"]
    # The waiting sign-up's secret is no link and no session
    assert (await Browser(A).post("/api/auth/verify-email", json={"token": Cookie})).status_code == 400
    assert (await B.get("/api/token-status")).status_code == 401
    assert (await Browser(A).post("/api/auth/accept", json=Accept(await Legal(B)))).status_code == 400   # another browser
    # An email sign-up's link cannot finish a Google sign-up, nor the other way round
    E = Browser(A)
    await SignUp(A, E, Email="eli@example.com")
    Mixed = Browser(A)
    Mixed.cookies.set("atelier_pending", Link(A, "eli@example.com"))
    assert (await Mixed.post("/api/auth/accept", json=Accept(await Legal(B)))).status_code == 400
    assert AccountsFor(A, "eli@example.com") == [] and AccountsFor(A, "dana@example.com") == []
    # A legal text altered by one character is another version
    L_ = await Legal(B)
    Forged = {"version": L_["terms"]["version"], "sha256": L_["terms"]["sha256"][:-1] + "0"}
    assert (await B.post("/api/auth/accept", json={**Accept(L_), "terms": Forged})).status_code == 409
    assert Consents(A) == []


async def test_registration_answers_the_same_for_new_and_existing_addresses(A):
    B, _ = Browser(A), None
    await SignUp(A, B)
    await B.post("/api/auth/verify-email", json={"token": Link(A, "dana@example.com")})
    Other = Browser(A)
    L_ = await Legal(Other)
    First = await Other.post("/api/auth/register", json={"name": "Dana", "email": "dana@example.com", "password": Pw, **Accept(L_)})
    Fresh = await Other.post("/api/auth/register", json={"name": "Dana", "email": "new@example.com", "password": Pw, **Accept(L_)})
    assert First.status_code == Fresh.status_code == 200
    assert First.json()["message"].replace("dana@example.com", "X") == Fresh.json()["message"].replace("new@example.com", "X")
    assert len(AccountsFor(A, "dana@example.com")) == 1 and len(Consents(A)) == 1     # no second account or record


# ── the older sign-in (proto) ───────────────────────────────────────────────────

async def test_the_older_email_sign_up_makes_the_account_only_when_its_link_is_used(L):
    B = Browser(L)
    L_ = await Legal(B)
    No = await B.post("/api/register", json={"Name": "Ari", "Email": "ari@example.com"})
    assert No.status_code == 400 and No.json()["error"]["code"] == "terms_required"
    assert (await B.post("/api/register", json={"Name": "Ari", "Email": "ari@example.com", **Accept(L_)})).status_code == 200
    assert AccountsFor(L, "ari@example.com") == []
    (P,) = Rows(L, "SELECT method, email FROM pending_registrations")
    assert P == {"method": "email", "email": "ari@example.com"}
    Mail = [M for M in Mails(L) if M["to"] == "ari@example.com"][-1]
    Secret = re.search(r"/verify\?token=([A-Za-z0-9_\-]+)", Mail["html"]).group(1)
    Page = await B.get(f"/verify?token={Secret}")
    assert Page.status_code == 200 and "Email verified" in Page.text
    (Acc,) = AccountsFor(L, "ari@example.com")
    assert Acc["source"] == "self" and Acc["verified_at"] and Acc["delivery_token"] in Page.text
    (C,) = Consents(L, Acc["account_id"])
    assert C["method"] == "email" and C["terms_version"] == 1
    Code = {"X-Access-Token": Acc["delivery_token"]}
    assert (await B.post("/api/designs", data={"prompt": "A band"}, headers=Code)).status_code == 200
    assert any(M["to"] == "ari@example.com" and Acc["delivery_token"] in M["html"] for M in Mails(L))   # the code by email


async def test_the_older_sign_up_asks_again_when_the_terms_changed_before_its_link(L):
    B = Browser(L)
    L1 = await Legal(B)
    await B.post("/api/register", json={"Name": "Ari", "Email": "ari@example.com", **Accept(L1)})
    Secret = re.search(r"/verify\?token=([A-Za-z0-9_\-]+)", Mails(L)[-1]["html"]).group(1)
    PublishDesignUse(L)
    Page = await B.get(f"/verify?token={Secret}")
    assert "Accept the updated Terms" in Page.text and "royalty-free" in Page.text and AccountsFor(L, "ari@example.com") == []
    Fields = dict(re.findall(r'<input type="hidden" name="([a-z_0-9]+)" value="([^"]*)">', Page.text))
    NotTicked = await B.post("/verify", data=Fields)
    assert "Please tick the box" in NotTicked.text and AccountsFor(L, "ari@example.com") == []
    Done = await B.post("/verify", data={**Fields, "accept_terms": "1"})
    assert "Email verified" in Done.text
    (Acc,) = AccountsFor(L, "ari@example.com")
    assert Consents(L, Acc["account_id"])[0]["terms_version"] == 2


# ── the Admin ───────────────────────────────────────────────────────────────────

async def test_the_admin_sees_each_status_read_only_and_masked_pending_sign_ups(A):
    _T, Legacy = A.Ctx.Accounts.IssueToken("Old", DisplayName="Old", Email="old@example.com")
    B = Browser(A)
    await SignUp(A, B)
    await B.post("/api/auth/verify-email", json={"token": Link(A, "dana@example.com")})
    await SignUp(A, Browser(A), Email="waiting.person@example.com")
    Made = (await A.Client.post("/api/admin/users", json={"Name": "Noa", "Email": "noa@example.com", "MaxGenerations": 5},
                                headers=AdminHeaders)).json()
    Users = {U["email"]: U for U in (await A.Client.get("/api/admin/users", headers=AdminHeaders)).json()["users"]}
    assert Users["old@example.com"]["terms"] == {"status": "legacy"}
    assert Users["noa@example.com"]["terms"] == {"status": "pending"} and Made["terms"]["status"] == "pending"
    D = Users["dana@example.com"]["terms"]
    assert (D["status"], D["method"], D["terms_version"], D["privacy_version"]) == ("accepted", "password", 1, 1) and D["accepted_at"]
    assert "waiting.person@example.com" not in Users                         # a pending sign-up is no account
    T = (await A.Client.get("/api/admin/terms", headers=AdminHeaders)).json()
    (P,) = T["pending_registrations"]
    assert P["email"] == MaskEmail("waiting.person@example.com") == "w***@e***.com" and P["method"] == "password"
    assert set(P) == {"id", "method", "email", "created_at", "expires_at"}    # no password hash, secret or provider id
    assert T["current"]["terms"]["version"] == 1 and T["design_use_published"] is False
    assert all("text" not in R for R in T["archive"]) and {R["doc"] for R in T["archive"]} == {"terms", "privacy"}
    for Method in ("post", "put", "delete"):                                  # read only
        assert (await getattr(A.Client, Method)("/api/admin/terms", headers=AdminHeaders)).status_code == 405
    assert (await Browser(A).get("/api/admin/terms")).status_code in (401, 403)
    # An acceptance keeps only who, when, how and which versions: no address, password, token or IP
    Cols = {R["name"] for R in Rows(A, "PRAGMA table_info(consents)")}
    assert Cols == {"id", "account_id", "accepted_at", "method", "terms_version", "terms_sha256", "privacy_version", "privacy_sha256"}
    assert Legacy.AccountId not in {C["account_id"] for C in Consents(A)}
