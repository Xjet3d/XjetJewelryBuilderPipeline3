"""The fal.ai key in the Admin: set (after a free check), masked, removed; not in production; nothing leaks."""

import stat

import pytest

from p3 import falkey
from p3.context import HttpError
from p3.settings import LoadSettings
from tests.conftest import Harness

AdminKey = "falkey-admin-key-0123456789abcdef"
Admin = {"Authorization": f"Bearer {AdminKey}"}
NewKey = "00000000-aaaa-bbbb-cccc-1234567890ab:secretsecretsecretsecret1234"


@pytest.fixture
async def H(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    yield Obj
    await Obj.Close()


@pytest.fixture
def Accepted(monkeypatch):
    Seen = []

    async def Check(Key):
        Seen.append(Key)
    monkeypatch.setattr(falkey, "CheckKey", Check)
    return Seen


async def test_needs_admin_and_starts_unset(H):
    assert (await H.Client.get("/api/admin/fal-key")).status_code == 403
    S = (await H.Client.get("/api/admin/fal-key", headers=Admin)).json()
    assert S == {"configured": False, "source": "none", "last4": "", "editable": True, "note": ""}


async def test_set_masks_persists_and_never_leaks(H, Accepted, tmp_path):
    R = await H.Client.put("/api/admin/fal-key", json={"key": f"  {NewKey}  "}, headers=Admin)
    assert R.status_code == 200 and Accepted == [NewKey]
    assert R.json()["configured"] and R.json()["source"] == "admin" and R.json()["last4"] == NewKey[-4:]
    assert NewKey not in R.text and NewKey not in (await H.Client.get("/api/admin/fal-key", headers=Admin)).text
    assert NewKey not in (await H.Client.get("/api/admin/health", headers=Admin)).text
    File = tmp_path / "var" / "fal_key.secret"
    assert File.read_text().strip() == NewKey and stat.S_IMODE(File.stat().st_mode) == 0o600
    # a restart reads it back, and it wins over FAL_KEY
    S = LoadSettings(DataDir=tmp_path / "var", Provider="mock", FalKey="from-environment-key")
    assert S.FalKey == NewKey and S.FalKeyFromAdmin is True


async def test_rejected_key_is_not_saved(H, monkeypatch, tmp_path):
    async def Refuse(Key):
        raise HttpError(400, "fal_key_rejected", "no")
    monkeypatch.setattr(falkey, "CheckKey", Refuse)
    R = await H.Client.put("/api/admin/fal-key", json={"key": NewKey}, headers=Admin)
    assert R.status_code == 400 and R.json()["error"]["code"] == "fal_key_rejected"
    assert not (tmp_path / "var" / "fal_key.secret").exists()
    assert (await H.Client.get("/api/admin/fal-key", headers=Admin)).json()["configured"] is False


@pytest.mark.parametrize("Bad", ["", "short", "has a space in it 1234567890", None, "x" * 300])
async def test_malformed_key_is_refused(H, Accepted, Bad):
    R = await H.Client.put("/api/admin/fal-key", json={"key": Bad}, headers=Admin)
    assert R.status_code == 400 and Accepted == []


async def test_remove_falls_back_to_the_environment_key(tmp_path, monkeypatch, Accepted):
    monkeypatch.setenv("FAL_KEY", "environment-key-1234")
    Obj = Harness(tmp_path, AdminKey=AdminKey, FalKey="environment-key-1234")
    try:
        assert (await Obj.Client.get("/api/admin/fal-key", headers=Admin)).json()["source"] == "environment"
        await Obj.Client.put("/api/admin/fal-key", json={"key": NewKey}, headers=Admin)
        R = await Obj.Client.delete("/api/admin/fal-key", headers=Admin)
        assert R.json()["source"] == "environment" and R.json()["last4"] == "1234"
        assert not (tmp_path / "var" / "fal_key.secret").exists()
    finally:
        await Obj.Close()


async def test_a_live_provider_is_rebuilt_with_the_new_key_and_cannot_lose_its_only_key(tmp_path, monkeypatch, Accepted):
    from p3.providers.mock import MockProvider
    monkeypatch.delenv("FAL_KEY", raising=False)
    Made = []

    def Live():
        Made.append(1)
        return MockProvider(LatencyS=0.0, RenderVideo=False)
    Obj = Harness(tmp_path, AdminKey=AdminKey, FalKey=None,
                  Factories={"mock": lambda: MockProvider(LatencyS=0.0, RenderVideo=False), "live": Live})
    try:
        assert (await Obj.Client.put("/api/admin/fal-key", json={"key": NewKey}, headers=Admin)).status_code == 200
        Obj.App.state.Modes.Mode = "live"
        assert (await Obj.Client.put("/api/admin/fal-key", json={"key": NewKey[::-1]}, headers=Admin)).status_code == 200
        assert Made == [1]                                              # the live provider was rebuilt with the new key
        R = await Obj.Client.delete("/api/admin/fal-key", headers=Admin)  # no environment key to fall back to
        assert R.status_code == 409 and R.json()["error"]["code"] == "live_would_lose_key"
        assert (await Obj.Client.get("/api/admin/fal-key", headers=Admin)).json()["source"] == "admin"
    finally:
        await Obj.Close()


async def test_production_locks_the_key(tmp_path, Accepted):
    import httpx
    from p3.app import CreateApp
    from p3.providers.mock import MockProvider
    from tests.test_production import Good
    (tmp_path / "var").mkdir()
    falkey.WriteSaved(tmp_path / "var", "saved-key-should-be-ignored-9999")       # a saved file is ignored in production
    S = LoadSettings(Env="production", DataDir=tmp_path / "var", **{**Good, "AdminKey": AdminKey})
    assert S.FalKey == "fal-test-key" and S.FalKeyFromAdmin is False
    App = CreateApp(S, MockProvider(LatencyS=0.0, RenderVideo=False))
    Host = {**Admin, "Host": Good["AdminHost"]}
    Client = httpx.AsyncClient(transport=httpx.ASGITransport(app=App), base_url="http://" + Good["AdminHost"])
    try:
        assert (await Client.put("/api/admin/fal-key", json={"key": NewKey}, headers=Host)).status_code == 409
        assert (await Client.delete("/api/admin/fal-key", headers=Host)).status_code == 409
        State = (await Client.get("/api/admin/fal-key", headers=Host)).json()
        assert State["editable"] is False and State["source"] == "environment" and State["note"] and Accepted == []
    finally:
        await App.state.Ctx.Runner.Shutdown()
        await Client.aclose()


async def test_check_button_tests_the_key_in_use_for_free(H, Accepted, monkeypatch):
    assert (await H.Client.post("/api/admin/fal-key/check")).status_code == 403
    R = await H.Client.post("/api/admin/fal-key/check", headers=Admin)
    assert R.status_code == 409 and R.json()["error"]["code"] == "no_fal_key"              # nothing to check
    await H.Client.put("/api/admin/fal-key", json={"key": NewKey}, headers=Admin)
    Accepted.clear()
    R = await H.Client.post("/api/admin/fal-key/check", headers=Admin)
    assert R.status_code == 200 and R.json() == {"ok": True, "source": "admin", "last4": NewKey[-4:]}
    assert Accepted == [NewKey] and NewKey not in R.text

    async def Refuse(Key):
        raise HttpError(400, "fal_key_rejected", "fal.ai did not accept this key")
    monkeypatch.setattr(falkey, "CheckKey", Refuse)
    assert (await H.Client.post("/api/admin/fal-key/check", headers=Admin)).status_code == 400


async def test_admin_toggles_mock_mode(tmp_path):
    from tests.test_modes import _Factories
    Obj = Harness(tmp_path, AdminKey=AdminKey, FalKey="fake-key-123456", Factories=_Factories([]))
    try:
        C = Obj.Client
        assert (await C.get("/api/admin/ai-mode")).status_code == 403
        S = (await C.get("/api/admin/ai-mode", headers=Admin)).json()
        assert S["mode"] == "mock" and S["live_available"] and S["locked"] is False
        No = await C.put("/api/admin/ai-mode", json={"mode": "live"}, headers=Admin)                 # needs the typed confirmation
        assert No.status_code == 400 and No.json()["error"]["code"] == "live_confirmation_required"
        Live = await C.put("/api/admin/ai-mode", json={"mode": "live", "confirmation": S["live_confirmation"]}, headers=Admin)
        assert Live.status_code == 200 and Live.json()["mode"] == "live"
        assert (await C.get("/api/health")).json()["mode"] == "live"
        Back = await C.put("/api/admin/ai-mode", json={"mode": "mock"}, headers=Admin)
        assert Back.status_code == 200 and Back.json()["mode"] == "mock"
        assert (await C.get("/api/health")).json()["mode"] == "mock"
    finally:
        await Obj.Close()


async def test_mock_toggle_needs_a_key_and_is_locked_by_configuration(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey, FalKey=None)
    try:
        R = await Obj.Client.put("/api/admin/ai-mode", json={"mode": "live", "confirmation": "x"}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "live_unavailable"
        assert (await Obj.Client.get("/api/admin/ai-mode", headers=Admin)).json()["live_available"] is False
    finally:
        await Obj.Close()
    Locked = Harness(tmp_path / "b", AdminKey=AdminKey, FalKey="fake-key-123456", LockMode=True)
    try:
        assert (await Locked.Client.get("/api/admin/ai-mode", headers=Admin)).json()["locked"] is True
        R = await Locked.Client.put("/api/admin/ai-mode", json={"mode": "live"}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "mode_locked"
    finally:
        await Locked.Close()
