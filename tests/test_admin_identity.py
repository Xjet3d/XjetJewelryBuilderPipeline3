"""AJW-39: administrator access by verified identity. Customer authentication and administrator authorization are separate:
only an operator-enrolled Google / Apple identity opens the Admin; an email match, a customer session or a social login never does."""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from p3 import adminauth as AdminAuth
from p3.cli import Main as Cli
from p3.oidc import Claims
from tests.conftest import Harness
from tests.test_auth import FakeIdp, Browser, Csrf, LinkSecret, Mails, Register

Key = "break-glass-key-of-24-plus-characters"
DovClaims = Claims(Subject="google-dov-123", Email="dov.grobgeld@xjetdigital.com", EmailVerified=True, Name="Dov")


@pytest.fixture
async def E(tmp_path):
    Obj = Harness(tmp_path, AuthMode="modern", AdminKey=Key, AdminIdentityEnforced=True)
    Obj.Ctx.Oidc = {"google": FakeIdp("google"), "apple": FakeIdp("apple")}
    Obj.Ctx.Oidc["google"].Next = DovClaims
    yield Obj
    await Obj.Close()


async def AdminSignIn(H, B, Provider="google") -> str:
    R = await B.get(f"/api/admin/auth/{Provider}/start")
    assert R.status_code == 303
    State = parse_qs(urlparse(R.headers["location"]).query)["state"][0]
    if Provider == "apple":
        Done = await B.post("/api/admin/auth/apple/callback", data={"code": "ok", "state": State})
    else:
        Done = await B.get(f"/api/admin/auth/{Provider}/callback?code=ok&state={State}")
    assert Done.status_code == 303
    return urlparse(Done.headers["location"]).fragment


def Enrol(H, Claims_=DovClaims, Name="Dov", Provider="google"):
    return AdminAuth.Enroll(H.Ctx.Db, Name, Provider, Claims_.Subject, Claims_.Email, "test")


async def test_the_shared_key_opens_nothing_on_the_web_once_identity_is_enforced(E):
    B = Browser(E)
    for Path in ("/api/admin/session", "/api/admin/users", "/api/dev/status"):
        R = await B.get(Path, headers={"Authorization": f"Bearer {Key}"})
        assert R.status_code == 403 and R.json()["error"]["code"] == "admin_identity_required", Path
    L = await B.post("/api/admin/login", json={"key": Key})
    assert L.status_code == 403 and L.json()["error"]["code"] == "admin_identity_required"
    Cfg = (await B.get("/api/admin/auth/config")).json()
    assert Cfg == {"identity_enforced": True, "key_login": False, "providers": {"google": True, "apple": True}}


async def test_an_email_match_alone_does_not_grant_admin_and_the_attempt_is_recorded(E):
    # a real customer with exactly Dov's address, signed in with Google, is still only a customer
    B = Browser(E)
    R = await B.get("/api/auth/google/start")
    State = parse_qs(urlparse(R.headers["location"]).query)["state"][0]
    await B.get(f"/api/auth/google/callback?code=ok&state={State}")
    assert (await B.get("/api/token-status")).status_code == 200
    assert (await B.get("/api/admin/users")).status_code == 403                          # the customer cookie opens no admin route
    assert (await B.get("/api/admin/session")).status_code == 403

    Admin = Browser(E)
    assert await AdminSignIn(E, Admin) == "auth=denied"                                  # same verified address, but not enrolled
    assert (await Admin.get("/api/admin/session")).status_code == 403
    Seen = AdminAuth.Candidates(E.Ctx.Db)
    assert [(C["provider"], C["subject"], C["email"], C["outcome"]) for C in Seen] == \
           [("google", "google-dov-123", "dov.grobgeld@xjetdigital.com", "not_enrolled")]


async def test_an_enrolled_identity_signs_in_and_every_change_is_audited(E):
    Enrol(E)
    B = Browser(E)
    assert await AdminSignIn(E, B) == "auth=ok"
    S = await B.get("/api/admin/session")
    assert S.status_code == 200 and S.json()["admin"] == "Dov" and S.json()["method"] == "identity"
    Cookie = next(C for C in B.cookies.jar if C.name == AdminAuth.CookieName)
    assert Cookie.path.endswith("/api/")
    assert (await B.get("/api/admin/users")).status_code == 200
    R = await B.post("/api/admin/users", json={"Name": "Eve", "Email": "eve@example.com", "MaxGenerations": 10})
    assert R.status_code in (200, 201), R.text
    Rows = E.Ctx.Db.All("SELECT admin, action FROM admin_audit ORDER BY id")
    assert any(X["admin"] == "Dov" and X["action"].startswith("POST /api/admin/users") for X in Rows)
    # the customer API still treats this browser as an anonymous visitor: admin is not a customer session
    assert (await B.get("/api/token-status")).status_code == 401


async def test_wrong_subject_wrong_email_unverified_and_disabled_are_all_refused(E):
    Admin = Enrol(E)
    Other = Claims(Subject="someone-else", Email="dov.grobgeld@xjetdigital.com", EmailVerified=True, Name="Dov")
    E.Ctx.Oidc["google"].Next = Other
    assert await AdminSignIn(E, Browser(E)) == "auth=denied"                              # right email, wrong person
    E.Ctx.Oidc["google"].Next = Claims(Subject=DovClaims.Subject, Email="changed@xjetdigital.com", EmailVerified=True, Name="Dov")
    assert await AdminSignIn(E, Browser(E)) == "auth=denied"                              # the enrolled email must still match
    E.Ctx.Oidc["google"].Next = Claims(Subject=DovClaims.Subject, Email=DovClaims.Email, EmailVerified=False, Name="Dov")
    assert await AdminSignIn(E, Browser(E)) == "auth=denied"
    Outcomes = {R["outcome"] for R in E.Ctx.Db.All("SELECT outcome FROM admin_login_attempts")}
    assert Outcomes == {"not_enrolled", "email_mismatch", "unverified"}
    E.Ctx.Oidc["google"].Next = DovClaims
    B = Browser(E)
    assert await AdminSignIn(E, B) == "auth=ok"
    AdminAuth.SetEnabled(E.Ctx.Db, Admin["id"], False, "test")
    assert (await B.get("/api/admin/session")).status_code == 403                         # disabling takes effect on live sessions
    assert await AdminSignIn(E, Browser(E)) == "auth=denied"


async def test_an_identity_session_expires_after_twelve_hours(E):
    Enrol(E)
    B = Browser(E)
    await AdminSignIn(E, B)
    assert (await B.get("/api/admin/session")).status_code == 200
    E.Ctx.Db.Execute("UPDATE admin_sessions SET hard_expires_at = '2000-01-01T00:00:00.000+00:00' WHERE admin_identity_id IS NOT NULL")
    assert (await B.get("/api/admin/session")).status_code == 403


async def test_a_client_cannot_present_the_internal_credential(E):
    Enrol(E)
    Row = E.Ctx.Db.One("SELECT id FROM admin_identities")
    Forged = {"Authorization": f"Bearer {AdminAuth.InternalPrefix}{E.Ctx.AdminInternal}.{Row['id']}"}
    assert (await Browser(E).get("/api/admin/session", headers=Forged)).status_code == 403
    Guess = {"Authorization": f"Bearer {AdminAuth.InternalPrefix}guess.{Row['id']}"}
    assert (await Browser(E).get("/api/admin/session", headers=Guess)).status_code == 403


async def test_only_google_and_apple_can_be_enrolled(E):
    with pytest.raises(ValueError):
        AdminAuth.Enroll(E.Ctx.Db, "X", "password", "s", "x@example.com", "test")
    with pytest.raises(ValueError):
        AdminAuth.Enroll(E.Ctx.Db, "X", "google", "", "x@example.com", "test")


async def test_the_key_still_works_before_enforcement_and_enrolment_can_be_prepared(tmp_path):
    H = Harness(tmp_path, AdminKey=Key)                                                   # AdminIdentityEnforced is off
    try:
        assert (await H.Client.get("/api/admin/session", headers={"Authorization": f"Bearer {Key}"})).status_code == 200
        Cfg = (await Browser(H).get("/api/admin/auth/config")).json()
        assert Cfg["identity_enforced"] is False and Cfg["key_login"] is True
    finally:
        await H.Close()


async def test_break_glass_cli_needs_the_key_and_can_enrol_and_revoke(tmp_path, monkeypatch, capsys):
    Data = tmp_path / "var"
    monkeypatch.setenv("P3_DATA_DIR", str(Data))
    monkeypatch.setenv("P3_ADMIN_KEY", Key)
    monkeypatch.setenv("P3_ENV", "development")
    monkeypatch.chdir(tmp_path)
    Args = ["admin-enroll", "--name", "Yakir", "--provider", "google", "--subject", "g-yakir", "--email", "Yakir.Tubul@xjetdigital.com"]
    assert Cli(Args + ["--admin-key", "wrong"]) == 2                                      # without the key nothing changes
    assert Cli(["admin-list"]) == 0 and "Yakir" not in capsys.readouterr().out
    assert Cli(Args + ["--admin-key", Key]) == 0
    assert "enrolled" in capsys.readouterr().out
    Cli(["admin-list"])
    Out = capsys.readouterr().out
    assert "Yakir" in Out and "yakir.tubul@xjetdigital.com" in Out and "enabled" in Out
    assert Cli(["admin-revoke-sessions", "--admin-key", Key]) == 0
    Cid = Out.split("\t")[0]
    assert Cli(["admin-disable", Cid, "--admin-key", Key]) == 0
    Cli(["admin-list"])
    assert "DISABLED" in capsys.readouterr().out


async def test_without_a_key_on_the_server_break_glass_is_off(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("P3_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.delenv("P3_ADMIN_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    assert Cli(["admin-enroll", "--name", "X", "--provider", "google", "--subject", "s", "--email", "x@example.com", "--admin-key", "anything"]) == 2
    assert "disabled" in capsys.readouterr().out


async def test_admin_sign_in_returns_to_the_tunnel_address_when_one_is_configured(E):
    """An Admin reached through an SSH tunnel returns from the provider to http://localhost, not the public host."""
    E.Ctx.Settings.AdminBaseUrl = "http://localhost:8340"
    B = Browser(E)
    R = await B.get("/api/admin/auth/google/start")
    assert R.status_code == 303
    assert parse_qs(urlparse(R.headers["location"]).query)["redirect_uri"] == ["http://localhost:8340/api/admin/auth/google/callback"]
