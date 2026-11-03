"""Production posture (docs/PRODUCTION-READINESS-HANDOFF.md): P3_ENV=production refuses development defaults and locks
the AI mode; developer tools, the API docs and the showcase prototype exist only where developer tools are on; with
P3_ADMIN_HOST the Admin page and its API answer on that host alone; every answer carries the security headers; the
public health says only that the service is up; robots/sitemap; a missing media file is never cached."""

import pytest

from p3.providers.mock import MockProvider
from p3.settings import LoadSettings, RepoRoot, ValidateProduction
from tests.conftest import Harness

AdminKey = "production-admin-key-0123456789"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Dev = dict(Provider="mock", FalKey=None, PublicBaseUrl="", AdminHost="", AdminKey="292811", SigningSecret=None,
           AllowUnapprovedPricing=True, MailMode="outbox", DailyAiSpendCapUsd=None, BasePath="/JewelryB2C3")
Good = dict(Provider="fal", FalKey="fal-test-key", PublicBaseUrl="https://atelier.example.com", AdminHost="admin.atelier.example.com",
            AdminKey=AdminKey, SigningSecret="signing-secret-0123456789abcdef", AllowUnapprovedPricing=False, MailMode="smtp",
            DailyAiSpendCapUsd=50.0, BasePath="", SupportEmail="help@example.com")


def test_production_refuses_development_defaults_and_accepts_a_complete_configuration(tmp_path):
    with pytest.raises(ValueError) as E:
        LoadSettings(Env="production", DataDir=tmp_path, **Dev)
    Msg = str(E.value)
    for Needle in ("P3_PROVIDER", "P3_PUBLIC_BASE_URL", "P3_ADMIN_HOST", "P3_ADMIN_KEY", "P3_SIGNING_SECRET",
                   "P3_ALLOW_UNAPPROVED_PRICING", "P3_MAIL_MODE", "P3_DAILY_AI_SPEND_CAP_USD"):
        assert Needle in Msg, Needle
    assert "P3_SUPPORT_EMAIL" not in Msg                 # unset, production names the shared inbox atelier@xjet3d.com
    assert "P3_BASE_PATH" not in Msg                     # a base path is fine: the site may share its host (Atelier)
    S = LoadSettings(Env="production", DataDir=tmp_path, **Good)
    assert S.Production and S.LockMode and not S.DevTools and ValidateProduction(S) == []
    # The same host for both sites, the admin key as signing secret, or data inside the checkout (outside its ignored
    # var/) are refused too
    Bad = LoadSettings(Env="development", DataDir=RepoRoot / "data", **{**Good, "AdminHost": "atelier.example.com", "SigningSecret": AdminKey})
    Problems = ValidateProduction(Bad)
    assert any("differ" in P for P in Problems) and any("P3_SIGNING_SECRET" in P for P in Problems) and any("P3_DATA_DIR" in P for P in Problems)
    assert LoadSettings(Env="development", DataDir=tmp_path, **Dev).DevTools          # development keeps its tools


def test_atelier_runs_in_production_under_its_base_path_with_its_admin_behind_the_ssh_tunnel(tmp_path):
    """Atelier (2026-11-03): the customer site under /JewelryB2C3 on a host it shares, its data in the checkout's git-ignored
    var/, and the Admin reached only through an SSH tunnel (P3_ADMIN_HOST=localhost) whose sign-in returns to the tunnel's
    own address (P3_ADMIN_BASE_URL, plain http on the operator's machine). Every other production rule still holds."""
    Atelier = {**Good, "PublicBaseUrl": "https://xjetatelier.xjet3d.com", "BasePath": "/JewelryB2C3", "AdminHost": "localhost",
               "AdminBaseUrl": "http://localhost:8340", "AuthMode": "modern", "AdminIdentityEnforced": True}
    S = LoadSettings(Env="production", DataDir=RepoRoot / "var" / "pytest-production", **Atelier)
    assert S.Production and S.BasePath == "/JewelryB2C3" and ValidateProduction(S) == []
    # the tunnel host needs its sign-in address, on that host
    for Wrong in ("", "https://admin.example.com", "http://127.0.0.1:8340"):
        Problems = ValidateProduction(LoadSettings(Env="development", DataDir=tmp_path, **{**Atelier, "AdminBaseUrl": Wrong}))
        assert any("P3_ADMIN_BASE_URL" in P for P in Problems), Wrong
    # modern sign-in still needs the Admin's own identities (the shared key opens nothing through the web)
    Problems = ValidateProduction(LoadSettings(Env="development", DataDir=tmp_path, **{**Atelier, "AdminIdentityEnforced": False}))
    assert any("P3_ADMIN_IDENTITY_ENFORCED" in P for P in Problems)
    # a short admin key is refused as before (Atelier's today is 21 characters)
    Problems = ValidateProduction(LoadSettings(Env="development", DataDir=tmp_path, **{**Atelier, "AdminKey": "x" * 21}))
    assert any("P3_ADMIN_KEY" in P for P in Problems)


async def test_the_tunnel_admin_answers_only_through_the_tunnel(tmp_path):
    """P3_ADMIN_HOST=localhost: the Admin page and its API answer for a request that came through the SSH tunnel (Host
    localhost:8340) and nowhere else, whatever the public host's proxy sends; the customer site is unchanged."""
    H = Harness(tmp_path, AdminKey=AdminKey, AdminHost="localhost", AdminBaseUrl="http://localhost:8340", NoDevTools=True,
                BasePath="/JewelryB2C3")
    try:
        C = H.Client                                     # its base URL carries /JewelryB2C3
        Public = {**Admin, "Host": "xjetatelier.xjet3d.com", "X-Forwarded-Host": "xjetatelier.xjet3d.com"}
        for P in ("/admin/", "/api/admin/session", "/api/admin/health"):
            assert (await C.get(P, headers=Public)).status_code == 404, P
        Tunnel = {**Admin, "Host": "localhost:8340"}
        assert (await C.get("/admin/", headers=Tunnel)).status_code == 200
        assert (await C.get("/api/admin/session", headers=Tunnel)).status_code == 200
        assert (await C.get("/", headers={"Host": "xjetatelier.xjet3d.com"})).status_code == 200
    finally:
        await H.Close()


async def test_the_admin_and_the_dev_tools_answer_only_on_the_admin_host_and_outside_production(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey, AdminHost="admin.p3.test", NoDevTools=True)
    try:
        C = H.Client
        for P in ("/admin", "/admin/", "/static/admin.html", "/static/admin.js", "/api/admin/session", "/api/admin/health",
                  "/dev", "/api/dev/mode", "/docs", "/openapi.json", "/redoc", "/showcase", "/static/dev.html"):
            assert (await C.get(P, headers=Admin)).status_code == 404, P             # the public customer host has none of it
        assert (await C.get("/api/showcase")).status_code == 200                     # the homepage hero's data stays public
        OnAdminHost = {**Admin, "Host": "admin.p3.test"}
        assert (await C.get("/admin/", headers=OnAdminHost)).status_code == 200
        assert (await C.get("/api/admin/session", headers=OnAdminHost)).status_code == 200
        assert (await C.get("/api/dev/mode", headers=OnAdminHost)).status_code == 404   # dev tools are off, host or not
        Page = (await C.get("/")).text                                                   # customers still get the site
        assert "window.__p3" in Page and '"dev_tools": false' in Page
    finally:
        await H.Close()


async def test_security_headers_public_health_robots_and_media_cache(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        R = await H.Client.get("/")
        for Name, Value in (("x-content-type-options", "nosniff"), ("x-frame-options", "DENY"),
                            ("referrer-policy", "strict-origin-when-cross-origin")):
            assert R.headers.get(Name) == Value, Name
        assert "frame-ancestors 'none'" in R.headers.get("content-security-policy", "")
        assert "strict-transport-security" not in R.headers                               # plain http
        assert (await H.Client.get("/", headers={"X-Forwarded-Proto": "https"})).headers.get("strict-transport-security", "").startswith("max-age=")
        # The public health: development says the AI mode (the customer page's mock banner); the full picture is admin-only
        Hd = (await H.Client.get("/api/health")).json()
        assert Hd["ok"] is True and Hd["mode"] == "mock" and "pricing_profile" not in Hd and "config_versions" not in Hd
        assert (await H.Client.get("/api/admin/health")).status_code == 403
        Full = (await H.Client.get("/api/admin/health", headers=Admin)).json()
        assert Full["mode"] == "mock" and "config_versions" in Full and "pricing_profile_approved" in Full
        # A development copy is never indexed; the sitemap and the favicon exist
        assert "Disallow: /" in (await H.Client.get("/robots.txt")).text
        assert '<meta name="robots" content="noindex">' in R.text and '"dev_tools": true' in R.text
        Sm = await H.Client.get("/sitemap.xml")
        assert Sm.status_code == 200 and "<urlset" in Sm.text
        assert (await H.Client.get("/favicon.ico")).status_code == 200
        # A missing thumbnail is an error, not something to remember for a year
        Miss = await H.Client.get("/thumb/designs/nope/candidates/nope.png?w=320")
        assert Miss.status_code == 404 and Miss.headers.get("cache-control") == "no-store"
    finally:
        await H.Close()


async def test_a_locked_ai_mode_ignores_the_developer_switch(tmp_path):
    (tmp_path / "var").mkdir(parents=True, exist_ok=True)
    (tmp_path / "var" / "runtime.json").write_text('{"ai_mode": "live"}', encoding="utf-8")       # a saved developer choice
    H = Harness(tmp_path, AdminKey=AdminKey, LockMode=True,
                Factories={"mock": lambda: MockProvider(LatencyS=0.0, RenderVideo=False), "live": lambda: MockProvider(LatencyS=0.0, RenderVideo=False)})
    try:
        assert H.App.state.Modes.Mode == "mock" and "locked" in H.App.state.Modes.Source      # the configuration decides
        R = await H.Client.post("/api/dev/mode", json={"mode": "mock"}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "mode_locked"
    finally:
        await H.Close()
