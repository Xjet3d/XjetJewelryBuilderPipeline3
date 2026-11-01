"""Audit B1 (SEC-01, SEC-03): Atelier's settings after the change — P3_AUTH_MODE=modern and P3_NO_DEV_TOOLS=true under
the /JewelryB2C3 base path, in development mode, with no separate Admin host (Atelier's Admin is reached through a
tunnel). A six-letter code is no longer a login, the developer tools and their details are gone, the Admin and the
customer journey keep working, and a code holder can still move the account to a password once."""

import httpx

from tests.conftest import Harness
from tests.test_auth import Pw, SignedUp

AdminKey = "atelier-b1-admin-key-0123456789"
Admin = {"Authorization": f"Bearer {AdminKey}"}


def AtelierB1(TmpPath) -> Harness:
    return Harness(TmpPath, AuthMode="modern", NoDevTools=True, BasePath="/JewelryB2C3", AdminKey=AdminKey)


def Browser(H) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=H.App), base_url="http://p3.test" + H.Base)


async def test_a_six_letter_code_is_refused_on_every_customer_api(tmp_path):
    H = AtelierB1(tmp_path)
    try:
        for Path in ("/api/designs", "/api/bag", "/api/orders", "/api/favorites", "/api/token-status"):
            assert (await H.Client.get(Path)).status_code == 401, Path          # the harness client sends X-Access-Token
        assert (await Browser(H).get("/api/auth/config")).json()["mode"] == "modern"
        B, _ = await SignedUp(H)
        for Path in ("/api/designs", "/api/bag", "/api/orders", "/api/favorites"):
            assert (await B.get(Path)).status_code == 200, Path               # email sign-in works
    finally:
        await H.Close()


async def test_developer_tools_and_their_details_are_gone_and_the_admin_stays(tmp_path):
    H = AtelierB1(tmp_path)
    try:
        C = Browser(H)
        for P in ("/dev", "/api/dev/mode", "/api/dev/outbox", "/showcase", "/static/dev.html", "/docs", "/openapi.json"):
            assert (await C.get(P, headers=Admin)).status_code == 404, P
        assert (await C.get("/api/health")).json() == {"ok": True}
        Page = (await C.get("/")).text
        for Gone in ("Developer access", "P3_ADMIN_KEY", "<!--dev-->", "<!--legacy-->"):
            assert Gone not in Page, Gone
        assert (await C.get("/api/showcase")).status_code == 200              # the home story's data stays public
        assert (await C.get("/admin/", headers=Admin)).status_code == 200      # Admin: no admin host, so served here
        assert (await C.get("/api/admin/health", headers=Admin)).status_code == 200
        assert (await C.get("/api/admin/health")).status_code in (401, 403)
    finally:
        await H.Close()


async def test_a_code_holder_moves_to_a_password_once_and_the_code_then_stops(tmp_path):
    H = AtelierB1(tmp_path)
    try:
        B = Browser(H)
        Ticket = (await B.post("/api/auth/claim", json={"token": H.Token})).json()["ticket"]
        R = await B.post("/api/auth/claim/password", json={"ticket": Ticket, "email": "staff@example.com", "password": Pw})
        assert R.status_code == 200 and R.json()["verification_sent"], R.text   # the code stays until the email is confirmed
        assert (await Browser(H).post("/api/auth/claim", json={"token": H.Token})).status_code == 200
        Link = H.App.state.Mailer.List()[0]["html"]
        V = await B.post("/api/auth/verify-email", json={"token": Link.split("#token=")[1].split('"')[0]})
        assert V.status_code == 200, V.text
        assert (await Browser(H).post("/api/auth/claim", json={"token": H.Token})).status_code == 404
        assert (await H.Client.get("/api/designs")).status_code == 401
    finally:
        await H.Close()
