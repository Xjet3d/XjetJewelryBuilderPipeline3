"""The Admin key in the Admin (Settings → Keys): changed with the current key, saved 0600, signs other browsers out,
survives a restart, goes back to the environment's key, locked in production — and never mixed up with the fal.ai key."""

import stat

import httpx
import pytest

from p3 import adminkey
from p3.app import CreateApp
from p3.providers.mock import MockProvider
from p3.settings import LoadSettings
from tests.conftest import Harness

Old = "old-admin-key-0123456789"
New = "New+Admin%Key_2026-xyz"
Auth = {"Authorization": f"Bearer {Old}"}


@pytest.fixture
async def H(tmp_path, monkeypatch):
    monkeypatch.setenv("P3_ADMIN_KEY", Old)
    Obj = Harness(tmp_path, AdminKey=Old)
    yield Obj
    await Obj.Close()


async def test_state_is_admin_only_and_never_shows_the_key(H):
    assert (await H.Client.get("/api/admin/admin-key")).status_code == 403
    R = await H.Client.get("/api/admin/admin-key", headers=Auth)
    assert R.json() == {"source": "environment", "length": len(Old), "editable": True, "min_chars": 12, "note": ""}
    assert Old not in R.text


async def test_change_needs_the_current_key_and_a_good_new_one(H):
    Put = lambda **B: H.Client.put("/api/admin/admin-key", json=B, headers=Auth)
    assert (await Put(current="wrong-key", new=New)).json()["error"]["code"] == "current_key_wrong"
    assert (await Put(current=Old, new="short")).json()["error"]["code"] == "invalid_admin_key"
    assert (await Put(current=Old, new="has a space 1234567890")).status_code == 400
    assert (await Put(current=Old, new=Old)).json()["error"]["code"] == "admin_key_unchanged"
    assert (await H.Client.get("/api/admin/admin-key", headers=Auth)).json()["source"] == "environment"      # nothing changed


async def test_change_takes_effect_signs_others_out_and_keeps_the_caller_in(H, tmp_path):
    Other = await H.Client.post("/api/admin/login", json={"key": Old})              # another browser's remembered session
    OtherCookie = Other.cookies.get("atelier_admin_session")
    assert OtherCookie
    R = await H.Client.put("/api/admin/admin-key", json={"current": Old, "new": f"  {New}  "}, headers=Auth)
    assert R.status_code == 200 and R.json()["source"] == "admin" and R.json()["length"] == len(New) and New not in R.text
    assert R.json()["signed_out_browsers"] == 1 and R.cookies.get("atelier_admin_session")       # the caller gets a fresh session
    File = tmp_path / "var" / "admin_key.secret"
    assert File.read_text().strip() == New and stat.S_IMODE(File.stat().st_mode) == 0o600
    # the old key no longer works; the new one does; the other browser's session is revoked
    assert (await H.Client.get("/api/admin/admin-key", headers=Auth)).status_code == 403
    NewAuth = {"Authorization": f"Bearer {New}"}
    assert (await H.Client.get("/api/admin/admin-key", headers=NewAuth)).status_code == 200
    Gone = httpx.AsyncClient(transport=httpx.ASGITransport(app=H.App), base_url=str(H.Client.base_url))
    try:
        Gone.cookies.set("atelier_admin_session", OtherCookie, path=H.Settings.BasePath + "/api/")
        assert (await Gone.get("/api/admin/admin-key")).status_code == 403
    finally:
        await Gone.aclose()
    # a restart keeps it, and it wins over P3_ADMIN_KEY in the environment
    S = LoadSettings(DataDir=tmp_path / "var", Provider="mock")
    assert S.AdminKey == New and S.AdminKeyFromAdmin is True
    # the fal.ai key is a different thing and untouched
    assert (await H.Client.get("/api/admin/fal-key", headers=NewAuth)).json()["source"] == "none"


async def test_go_back_to_the_environment_key(H, tmp_path):
    await H.Client.put("/api/admin/admin-key", json={"current": Old, "new": New}, headers=Auth)
    NewAuth = {"Authorization": f"Bearer {New}"}
    Bad = await H.Client.request("DELETE", "/api/admin/admin-key", json={"current": "nope"}, headers=NewAuth)
    assert Bad.status_code == 400 and Bad.json()["error"]["code"] == "current_key_wrong"
    R = await H.Client.request("DELETE", "/api/admin/admin-key", json={"current": New}, headers=NewAuth)
    assert R.status_code == 200 and R.json()["source"] == "environment"
    assert not (tmp_path / "var" / "admin_key.secret").exists()
    assert (await H.Client.get("/api/admin/admin-key", headers=Auth)).status_code == 200        # the environment's key again


async def test_no_environment_key_to_go_back_to(tmp_path, monkeypatch):
    monkeypatch.delenv("P3_ADMIN_KEY", raising=False)
    Obj = Harness(tmp_path, AdminKey=Old)
    try:
        await Obj.Client.put("/api/admin/admin-key", json={"current": Old, "new": New}, headers=Auth)
        R = await Obj.Client.request("DELETE", "/api/admin/admin-key", json={"current": New}, headers={"Authorization": f"Bearer {New}"})
        assert R.status_code == 409 and R.json()["error"]["code"] == "no_environment_key"
    finally:
        await Obj.Close()


async def test_production_locks_the_admin_key(tmp_path):
    from tests.test_production import AdminKey as ProdKey, Good
    (tmp_path / "var").mkdir()
    adminkey.WriteSaved(tmp_path / "var", "a-saved-key-that-must-be-ignored-in-production")
    S = LoadSettings(Env="production", DataDir=tmp_path / "var", **Good)
    assert S.AdminKey == ProdKey and S.AdminKeyFromAdmin is False
    App = CreateApp(S, MockProvider(LatencyS=0.0, RenderVideo=False))
    Host = {"Authorization": f"Bearer {ProdKey}", "Host": Good["AdminHost"]}
    Client = httpx.AsyncClient(transport=httpx.ASGITransport(app=App), base_url="http://" + Good["AdminHost"])
    try:
        R = await Client.put("/api/admin/admin-key", json={"current": ProdKey, "new": New}, headers=Host)
        assert R.status_code == 409 and R.json()["error"]["code"] == "admin_key_locked"
        State = (await Client.get("/api/admin/admin-key", headers=Host)).json()
        assert State["editable"] is False and State["note"]
    finally:
        await App.state.Ctx.Runner.Shutdown()
        await Client.aclose()
