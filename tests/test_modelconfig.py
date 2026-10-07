"""AI prompts & parameters: seeded from the previous files (same requests), validation, versioning,
pinned versions per request, preview without provider calls, export without secrets."""

import json

import pytest

from p3.config import LoadGenerationConfig
from p3.modelconfig import SeedConfigs
from p3.providers import endpoints
from p3.config import _ReadText
from tests.conftest import Harness

AdminKey = "models-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}


@pytest.fixture
async def HM(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey, FalKey="fal-SECRET-key-should-never-be-exported")
    yield Obj
    await Obj.Close()


def _Subs(H, Endpoint):
    return [A for E, A, _ in H.Provider.Submissions if E == Endpoint]


async def _Activate(H, Model, Params, Status=200):
    R = await H.Client.post(f"/api/admin/models/{Model}/activate", json={"params": Params}, headers=Admin)
    assert R.status_code == Status, R.text
    return R.json()


async def test_seeded_version_sends_exactly_the_previous_requests(HM):
    H = HM
    Raw = json.loads((__import__("p3.settings", fromlist=["ConfigDir"]).ConfigDir / "generation.json").read_text("utf-8"))
    Suffix = _ReadText(Raw["images"]["prompt_suffix_file"])
    Batch = await H.NewDesign("A slim band with a leaf")
    Subs = _Subs(H, endpoints.ImageGenerate)
    assert len(Subs) == 4                                                    # four separate requests…
    assert {A["num_images"] for A in Subs} == {1} and len({A["seed"] for A in Subs}) == 4   # …one image each
    A = Subs[0]
    assert A["prompt"] == f"A slim band with a leaf\n\n{Suffix}"           # the former "<text>\n\n<suffix>"
    assert A["system_prompt"] == _ReadText(Raw["images"]["generate_system_prompt_file"])
    for K, V in Raw["images"]["params"].items():
        assert A[K] == V
    assert "sync_mode" not in A
    State = (await H.Client.get("/api/admin/models/nano-banana-pro", headers=Admin)).json()
    assert Batch["config_version"] == State["active"]["id"] and State["active"]["number"] == 1
    # Refinement → edit endpoint with the edit system prompt and the selected image.
    Sel = Batch["candidates"][0]
    R = await H.Client.post(f"/api/designs/{Batch['design_id']}/batches",
                            json={"parent_candidate_id": Sel["id"], "instruction": "Thinner band"})
    await H.Idle()
    E = _Subs(H, endpoints.ImageEdit)[0]
    from p3.modelconfig import VariationDefaults
    assert E["prompt"].startswith(f"Thinner band\n\n{Suffix}") and len(E["image_urls"]) == 1
    assert E["prompt"][len(f"Thinner band\n\n{Suffix}"):].strip() in VariationDefaults.values()    # + this image's directive (v2)
    assert E["system_prompt"] == _ReadText(Raw["images"]["edit_system_prompt_file"])
    # Movie and mesh: exactly the previous parameters plus the runtime image.
    await H.Proceed(Batch["design_id"], Sel["id"])
    await H.Idle()
    M = _Subs(H, endpoints.Movie)[0]
    assert {K: V for K, V in M.items() if K != "image_url"} == Raw["movie"]["params"] and M["image_url"]


async def test_validation_messages(HM):
    H = HM
    async def Problems(Model, Params):
        R = await H.Client.post(f"/api/admin/models/{Model}/validate", json={"params": Params}, headers=Admin)
        return R.json().get("problems", [])
    P = await Problems("nano-banana-pro", {"prompt": "no placeholder", "num_images": 4, "resolution": "8K",
                                           "bogus": 1, "safety_tolerance": 7})
    assert any("must contain {{user_text}}" in X for X in P)
    assert any("'num_images' is set by the pipeline" in X for X in P)
    assert any("'resolution' must be one of: 1K, 2K, 4K" in X for X in P)
    assert any("'bogus' is not a supported parameter" in X for X in P)
    assert any("'safety_tolerance' must be one of" in X for X in P)
    P = await Problems("nano-banana-pro", {"prompt": "{{user_text}} {{customer_name}}"})
    assert any("unknown placeholder(s): {{customer_name}}" in X for X in P)
    P = await Problems("minimax-camera", {"prompt_expansion_mode": "disabled", "duration": 20,
                                          "camera_trajectory": [{"time": 0.5, "azimuth": 0, "elevation": 100, "distance": 1},
                                                                {"time": 0.2, "azimuth": 9, "elevation": 0, "distance": 0}]})
    assert any("'duration' must be between 3 and 15" in X for X in P)
    assert any("elevation must be between" in X for X in P) and any("time order" in X for X in P)
    assert any("distance must be greater than 0" in X for X in P)
    assert any("'prompt_expansion_mode' is required" in X for X in await Problems("minimax-camera", {}))
    P = await Problems("hi3d", {"export_format": "fbx", "face_count": 50})
    assert any("not supported by the pipeline" in X for X in P) and any("'face_count' must be between" in X for X in P)
    R = await H.Client.post("/api/admin/models/hi3d/activate", json={"params": {"export_format": "fbx"}}, headers=Admin)
    assert R.status_code == 400 and R.json()["error"]["problems"]
    Ok = await H.Client.post("/api/admin/models/any-llm/validate",
                             json={"params": {"prompt": "Check: {{user_prompt}}", "temperature": 0.2, "system_prompt": None}},
                             headers=Admin)
    assert Ok.json() == {"ok": True, "params": {"prompt": "Check: {{user_prompt}}", "temperature": 0.2}}  # None = omitted


async def test_activation_applies_to_new_requests_and_pins_created_ones(HM):
    H = HM
    First = await H.NewDesign("Plain band")                                   # created under v1
    Old = H.Ctx.Db.One("SELECT * FROM batches WHERE id = ?", (First["id"],))
    Cand = H.Ctx.Db.One("SELECT * FROM candidates WHERE batch_id = ? AND slot = 0", (Old["id"],))
    V1 = (await H.Client.get("/api/admin/models/nano-banana-pro", headers=Admin)).json()["active"]
    New = dict(V1["params"], prompt="Ring: {{user_text}}", resolution="2K", system_prompt="Be precise.")
    S = await _Activate(H, "nano-banana-pro", New)
    assert S["changed"] and S["active"]["number"] == 2 and S["history"][0]["active"] and not S["history"][1]["active"]
    assert (await _Activate(H, "nano-banana-pro", New))["changed"] is False   # unchanged → no new version
    # A request created before activation still builds from v1, even if submitted now.
    Args = await H.Svc.Images._Arguments(Old, Cand)
    assert Args["resolution"] == "1K" and Args["system_prompt"] == V1["params"]["system_prompt"]
    # New requests use v2 and record it.
    N = len(_Subs(H, endpoints.ImageGenerate))
    Second = await H.NewDesign("Twisted band")
    A = _Subs(H, endpoints.ImageGenerate)[N]
    assert A["prompt"] == "Ring: Twisted band" and A["resolution"] == "2K" and A["system_prompt"] == "Be precise."
    assert Second["config_version"] == S["active"]["id"]
    # Restore v1 → a new version (v3) with v1's parameters becomes active.
    R = (await H.Client.post("/api/admin/models/nano-banana-pro/restore", json={"version_id": V1["id"]}, headers=Admin)).json()
    assert R["active"]["number"] == 3 and R["active"]["params"] == V1["params"] and "Restored from v1" in R["active"]["note"]
    # Settings survive a restart (new app on the same data).
    H2 = Harness(H.TmpPath, AdminKey=AdminKey)
    try:
        assert H2.Ctx.Models.Active("nano-banana-pro").Number == 3
    finally:
        await H2.Close()


async def test_movie_and_mesh_use_saved_config_and_existing_movies_are_reused(HM):
    H = HM
    Batch = await H.NewDesign("Plain band")
    Sel = Batch["candidates"][0]
    await H.Proceed(Batch["design_id"], Sel["id"])
    await H.Idle()
    Mov = H.Ctx.Db.One("SELECT * FROM movies")
    # A movie made before versioned configs (legacy content-hash version) is still reused: no new charge.
    Legacy = H.Ctx.Db.One("SELECT legacy_alias FROM model_config_versions WHERE model = 'minimax-camera'")["legacy_alias"]
    H.Ctx.Db.Execute("UPDATE movies SET config_version = ? WHERE id = ?", (Legacy, Mov["id"]))
    await H.Proceed(Batch["design_id"], Sel["id"])
    await H.Idle()
    assert len(_Subs(H, endpoints.Movie)) == 1
    V = (await H.Client.get("/api/admin/models/minimax-camera", headers=Admin)).json()["active"]
    await _Activate(H, "minimax-camera", dict(V["params"], duration=8))
    Other = Batch["candidates"][1]
    await H.Proceed(Batch["design_id"], Other["id"])
    await H.Idle()
    assert _Subs(H, endpoints.Movie)[-1]["duration"] == 8
    assert H.Ctx.Db.One("SELECT config_version FROM movies WHERE candidate_id = ?", (Other["id"],))["config_version"].startswith("minimax-camera@v2-")
    # Hi3D (admin Generate 3D) uses the saved mesh configuration.
    MV = (await H.Client.get("/api/admin/models/hi3d", headers=Admin)).json()["active"]
    await _Activate(H, "hi3d", dict(MV["params"], resolution="2048master", face_count=5_000_000, export_format="glb"))
    R = await H.Client.post(f"/api/admin/sessions/{Batch['design_id']}/3d", json={}, headers=Admin)
    assert R.status_code == 200
    await H.Idle()
    Mesh = _Subs(H, endpoints.Mesh)[-1]
    assert (Mesh["resolution"], Mesh["face_count"], Mesh["export_format"]) == ("2048master", 5_000_000, "glb")
    assert H.Ctx.Db.One("SELECT status FROM session_3d")["status"] == "measured"      # GLB measured too


async def test_preview_never_calls_the_provider_and_shows_runtime_inputs(HM):
    H = HM
    Before = len(H.Provider.Submissions)
    P = (await H.Client.post("/api/admin/models/nano-banana-pro-edit/preview", json={}, headers=Admin)).json()
    assert P["submitted"] is False and len(H.Provider.Submissions) == Before
    assert P["payload"]["prompt"].startswith("[SAMPLE customer text]") and P["payload"]["image_urls"] == [P["sample_runtime"]["image_url"]]
    assert P["payload"]["num_images"] == 1
    P = (await H.Client.post("/api/admin/models/minimax-camera/preview",
                             json={"params": {"prompt_expansion_mode": "quality"}}, headers=Admin)).json()
    assert P["payload"] == {"prompt_expansion_mode": "quality", "image_url": P["sample_runtime"]["image_url"]}
    assert "duration" in P["omitted"]
    Bad = await H.Client.post("/api/admin/models/minimax-camera/preview", json={"params": {"duration": 99}}, headers=Admin)
    assert Bad.status_code == 400


async def test_export_and_protection(HM):
    H = HM
    for Path in ("/api/admin/models", "/api/admin/models/hi3d", "/api/admin/models/export"):
        assert (await H.Client.get(Path)).status_code == 403
    J = await H.Client.get("/api/admin/models/export?format=json", headers=Admin)
    T = await H.Client.get("/api/admin/models/export?model=minimax-camera&format=txt", headers=Admin)
    assert J.status_code == T.status_code == 200 and "attachment" in J.headers["content-disposition"]
    Data = J.json()
    assert [M["endpoint"] for M in Data["models"]] == ["fal-ai/any-llm", "fal-ai/nano-banana-pro", "fal-ai/nano-banana-pro/edit",
                                                      "minimax/h3-max/camera-controls", "hitem3d/hi3d/v3.0/image-to-3d"]
    assert all(M["version"] and M["parameters"] is not None for M in Data["models"])
    assert "minimax/h3-max/camera-controls" in T.text and "camera_trajectory" in T.text and "fal-ai/any-llm" not in T.text
    assert "sync_mode=not sent" in T.text and "null" not in T.text.split("Set by the pipeline:")[-1]
    for Body in (J.text, T.text):
        assert "fal-SECRET" not in Body and "FAL_KEY" not in Body and AdminKey not in Body
    Health = (await H.Client.get("/api/admin/health", headers=Admin)).json()
    assert Health["config_versions"]["nano-banana-pro"].startswith("nano-banana-pro@v1-")
