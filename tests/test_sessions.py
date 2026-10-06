"""Sessions (one design journey each), stage tracking, admin Generate 3D, ring geometry."""

import io
import json
import math
import sys

import numpy as np
import pytest

from p3 import geometry as g
from p3.geometry import MeasureRing, UsSizeToInnerDiameterMm
from p3.providers import endpoints
from tests.conftest import Harness, MakeLive

AdminKey = "sessions-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}


@pytest.fixture
async def HS(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    yield Obj
    await Obj.Close()


async def _Session(H, DesignId):
    return (await H.Client.get(f"/api/admin/sessions/{DesignId}", headers=Admin)).json()


async def _Journey(H, Refine=True, Bag=True):
    Batch = await H.NewDesign("Signet ring with a hexagon face")
    Did, Sel = Batch["design_id"], Batch["candidates"][0]
    await H.Client.put(f"/api/designs/{Did}/selection", json={"candidate_id": Sel["id"]})
    if Refine:
        R = await H.Client.post(f"/api/designs/{Did}/batches", json={"parent_candidate_id": Sel["id"], "instruction": "Thinner band"})
        assert R.status_code == 200
        await H.Idle()
    Cust = await H.Proceed(Did, Sel["id"])
    await H.Idle()
    await H.Client.patch(f"/api/customizations/{Cust['id']}", json={"material_id": "silver"})
    await H.Client.patch(f"/api/customizations/{Cust['id']}", json={"ring_size": 7})
    if Bag:
        R = await H.Client.post("/api/bag", json={"customization_id": Cust["id"]})
        assert R.status_code == 200, R.text
    return Did, Sel, Cust


async def test_full_journey_is_tracked_with_stage_times(HDevPricing):
    H = HDevPricing
    Did, _, _ = await _Journey(H)
    await H.Client.post("/api/events", json={"kind": "bag_viewed"})
    await H.Client.post("/api/events", json={"kind": "checkout_clicked"})
    S = H.App.state.Ctx
    from p3 import sessions as Sessions
    X = Sessions.Summaries(S, [Did])[0]
    assert X["path"] == "Generated → Selected → Refined → Customize → Bag → Checkout"
    assert X["stage_reached"] == "checkout_clicked" and X["add_to_bag"] and X["checkout_clicked"] and not X["ordered"]
    assert all(X["stage_times"][K] for K in ("started", "generated", "selected", "customize", "bag", "checkout_clicked"))
    assert X["stage_times"]["order"] is None
    assert X["stage_times"]["started"] <= X["stage_times"]["generated"] <= X["stage_times"]["customize"] <= X["stage_times"]["bag"]
    assert (X["generations"], X["refinements"], X["ring_size"], X["material_id"], X["material_chosen"]) == (1, 1, 7.0, "silver", True)
    assert X["fixed_price"]["source"] == "bag_snapshot" and X["fixed_price"]["unit_price"] is not None
    assert X["state"] == "active" and X["three_d_status"] is None


async def test_session_that_stops_before_bag_and_ends_on_new_design(HS):
    H = HS
    Did, _, _ = await _Journey(H, Refine=False, Bag=False)
    D = await _Session(H, Did)
    assert D["session"]["path"] == "Generated → Selected → Customize" and D["session"]["state"] == "active"
    await H.NewDesign("A second, different ring")                       # another New Design ends it
    D = await _Session(H, Did)
    assert D["session"]["state"] == "ended" and D["session"]["end_reason"] == "new_design"
    assert D["session"]["path"] == "Generated → Selected → Customize → stopped" and not D["session"]["add_to_bag"]
    Kinds = [E["kind"] for E in D["timeline"]]
    assert Kinds[0] == "started" and "generated" in Kinds and "customize_opened" in Kinds
    assert [C["ring_size"] for C in D["choices"] if "ring_size" in C][-1] == 7.0         # choice history kept
    assert D["choices"][0]["kind"] == "customize_opened" and "unit_price" in D["choices"][0]   # fixed price shown


async def test_idle_session_ends_and_client_events_are_validated(HS):
    H = HS
    Batch = await H.NewDesign("Plain band")
    Did = Batch["design_id"]
    Old = "2020-01-01T00:00:00.000+00:00"
    for T in ("designs", "candidates", "batches"):
        H.Ctx.Db.Execute(f"UPDATE {T} SET created_at = ?" + (", updated_at = ?" if T != "batches" else ""),
                         (Old, Old) if T != "batches" else (Old,))
    S = (await _Session(H, Did))["session"]
    assert S["state"] == "ended" and S["end_reason"] == "idle" and S["path"] == "Generated → stopped"
    assert (await H.Client.post("/api/events", json={"kind": "hack"})).status_code == 400
    assert (await H.Client.post("/api/events", json={"kind": "design_opened", "design_id": "dsg_nope"})).status_code == 404
    assert (await H.Client.post("/api/events", json={"kind": "new_design_clicked"})).json()["ok"]
    assert (await H.Client.post("/api/events", json={"kind": "design_opened", "design_id": Did})).json()["ok"]
    Dash = (await H.Client.get("/api/admin/dashboard", headers=Admin)).json()
    assert Dash["sessions"] == 0 and Dash["new_design_clicks"] == 0 and Dash["mock_excluded"]   # mock mode: not counted
    MakeLive(H)
    H.Ctx.Db.Execute("UPDATE session_events SET data_json = json_set(data_json, '$.ai_mode', 'live')")
    Dash = (await H.Client.get("/api/admin/dashboard", headers=Admin)).json()
    assert Dash["sessions"] == 1 and Dash["new_design_clicks"] == 1
    assert [F["stage"] for F in Dash["funnel"]] == ["started", "generated", "selected", "customize", "bag", "checkout_clicked", "order"]
    assert [F["sessions"] for F in Dash["funnel"]] == [1, 1, 0, 0, 0, 0, 0]
    assert (await H.Client.get("/api/admin/dashboard")).status_code == 403


async def test_3d_never_runs_automatically_and_uses_default_size_10(HS):
    H = HS
    Batch = await H.NewDesign("Plain band")
    Did = Batch["design_id"]
    Sel = Batch["candidates"][0]
    Cust = await H.Proceed(Did, Sel["id"])                              # customer reaches Customize, no size
    await H.Idle()
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM meshes")["n"] == 0    # nothing automatic
    assert (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={})).status_code == 403   # customer can't
    R = await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={}, headers=Admin)
    assert R.status_code == 200, R.text
    T = R.json()
    assert (T["customer_size"], T["production_size"], T["size_source"]) == (None, 10.0, "default")
    assert T["material_source"] == "default" and T["status"] == "generating"
    await H.Idle()
    T = (await _Session(H, Did))["three_d"][0]
    assert T["status"] == "measured", T
    Prod = T["geometry"]["production"]
    assert math.isclose(Prod["inner_diameter_mm"], UsSizeToInnerDiameterMm(10), rel_tol=0.01)
    assert Prod["watertight"] and Prod["volume_mm3"] > 0 and Prod["surface_area_mm2"] > 0
    assert Prod["size_z_mm"] < Prod["size_x_mm"]                         # ring axis aligned to Z
    Price = T["price"]
    assert math.isclose(Price["weight_g"], Prod["volume_mm3"] / 1000 * Price["density_g_cm3"], rel_tol=1e-3)
    Row = H.Ctx.MaterialPrices.Row(T["material_id"])                     # Admin → Material pricing table
    assert Price["density_g_cm3"] == Row["density_g_cm3"] and Price["status"] == "calculated"
    assert Price["production_cost"] == pytest.approx(Price["weight_g"] * Row["cost_per_g"], abs=0.01)
    assert Price["calculated_price"] == pytest.approx(Price["weight_g"] * Row["price_per_g"], abs=0.01)
    assert Price["cost_model_version"] == H.Ctx.MaterialPrices.Current()["version"]
    # Real, persisted stages; background preview + integrity ran after the numbers.
    St = (await H.Client.get(f"/api/admin/3d/{T['id']}/status", headers=Admin)).json()
    Seen = [S["stage"] for S in St["stages"]]
    assert Seen[-1] == "ready" and "calculating_geometry" in Seen and "downloading_stl" in Seen, Seen
    assert all(S["ended_at"] for S in St["stages"]) and St["done"]
    assert St["raw"]["integrity"] == "closed" and St["raw"]["preview_ready"] and St["raw"]["sha256"]
    Pv = await H.Client.get(f"/api/admin/3d/{T['id']}/stl/preview", headers=Admin)
    assert Pv.status_code == 200 and Pv.content[:4] == b"P3PV"
    # The scaled STL is never stored: it is exported on demand (admin only) to a temporary file.
    assert T["scaled_stl"] == "on_demand" and Prod["stl_path"] is None
    assert (await H.Client.get(f"/api/admin/3d/{T['id']}/stl/production", headers=Admin)).status_code == 404
    assert (await H.Client.post(f"/api/admin/3d/{T['id']}/export")).status_code == 403
    E = (await H.Client.post(f"/api/admin/3d/{T['id']}/export", headers=Admin)).json()
    await H.Idle()
    E = (await H.Client.get(f"/api/admin/3d/{T['id']}/export/{E['job_id']}", headers=Admin)).json()
    assert E["status"] == "done" and E["url"] and E["expires_at"]
    Stl = await H.Client.get(E["url"].removeprefix(H.Ctx.Settings.BasePath))      # signed: no admin header
    assert Stl.status_code == 200 and len(Stl.content) == E["bytes"] > 1000
    Tri = np.frombuffer(Stl.content[84:], dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]))["v"]
    Span = Tri.reshape(-1, 3).max(0) - Tri.reshape(-1, 3).min(0)
    assert Span[0] == pytest.approx(Prod["size_x_mm"], rel=1e-3) and Span[2] == pytest.approx(Prod["size_z_mm"], rel=1e-3)
    # Same request again reuses the temporary file; after the TTL it is deleted.
    assert (await H.Client.post(f"/api/admin/3d/{T['id']}/export", headers=Admin)).json()["job_id"] == E["job_id"]
    H.Ctx.Db.Execute("UPDATE geometry_jobs SET expires_at = '2000-01-01T00:00:00' WHERE id = ?", (E["job_id"],))
    Store = (await H.Client.get("/api/admin/storage", headers=Admin)).json()
    assert Store["exports_bytes"] == 0 and Store["meshes_bytes"] > 0 and Store["disk_free_bytes"] > 0
    assert (await H.Client.get(f"/api/admin/3d/{T['id']}/export/{E['job_id']}", headers=Admin)).json()["status"] == "expired"
    # The customer's price is untouched by 3D.
    assert (await H.Client.get(f"/api/customizations/{Cust['id']}")).json()["quote"] == Cust["quote"]


async def test_customer_size_admin_override_and_raw_mesh_reuse(HS):
    H = HS
    Did, Sel, Cust = await _Journey(H, Refine=False, Bag=False)            # customer chose US 7, silver
    T1 = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={}, headers=Admin)).json()
    assert (T1["customer_size"], T1["production_size"], T1["size_source"]) == (7.0, 7.0, "customer")
    assert (T1["material_id"], T1["material_source"]) == ("silver", "customer")
    await H.Idle()
    Subs = len([S for S in H.Provider.Submissions if S[0] == endpoints.Mesh])
    assert Subs == 1
    T2 = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 9, "material_id": "stainless_steel"},
                              headers=Admin)).json()
    assert (T2["customer_size"], T2["production_size"], T2["size_source"]) == (7.0, 9.0, "admin_override")
    assert T2["material_source"] == "admin_override" and T2["mesh_id"] == T1["mesh_id"]
    await H.Idle()
    assert len([S for S in H.Provider.Submissions if S[0] == endpoints.Mesh]) == Subs   # no new paid Hi3D call
    D = await _Session(H, Did)
    A, B = D["three_d"][1], D["three_d"][0]                               # newest first
    assert A["geometry"]["production"]["inner_diameter_mm"] < B["geometry"]["production"]["inner_diameter_mm"]
    assert D["session"]["three_d_status"] == "measured"
    assert (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 99}, headers=Admin)).status_code == 400
    Kinds = [E["kind"] for E in D["timeline"]]
    assert Kinds.count("admin_3d_requested") == 2 and Kinds.count("admin_3d_measured") == 2


def test_geometry_matches_an_ideal_ring_in_any_orientation():
    import trimesh
    M = trimesh.creation.torus(major_radius=9.0, minor_radius=1.5, major_sections=128, minor_sections=64)
    M.apply_transform(trimesh.transformations.rotation_matrix(0.7, [1, 0.3, 0.2]))
    M.apply_scale(0.037)                                                  # arbitrary units, like Hi3D output
    Buf = io.BytesIO()
    M.export(Buf, file_type="stl")
    Target = UsSizeToInnerDiameterMm(10)
    G = MeasureRing(Buf.getvalue(), "stl", Target)
    assert G.status == "measured" and not G.problems
    S = Target / 15.0                                                     # torus bore is 2*(9-1.5) = 15
    R, r = 9 * S, 1.5 * S
    P = G.production
    assert math.isclose(P.inner_diameter_mm, Target, rel_tol=0.002)
    assert math.isclose(P.volume_mm3, 2 * math.pi ** 2 * R * r * r, rel_tol=0.01)
    assert math.isclose(P.surface_area_mm2, 4 * math.pi ** 2 * R * r, rel_tol=0.01)
    assert math.isclose(P.size_x_mm, 2 * (R + r), rel_tol=0.01) and math.isclose(P.size_z_mm, 2 * r, rel_tol=0.03)


def test_bore_is_found_when_a_heavy_head_pulls_the_centroid_outside_it():
    import tempfile
    import trimesh
    from pathlib import Path
    from p3 import geometry as g
    Band = trimesh.creation.torus(major_radius=9.0, minor_radius=1.5, major_sections=128, minor_sections=64)
    Head = trimesh.creation.icosphere(subdivisions=4, radius=8.0)
    Head.apply_translation([16.0, 0, 0])                                  # surface centroid lands outside the bore
    M = trimesh.util.concatenate([Band, Head])
    M.apply_transform(trimesh.transformations.rotation_matrix(0.5, [0.2, 1, 0.4]))
    Src = Path(tempfile.mkdtemp()) / "head.stl"
    g.WriteStl(np.asarray(M.triangles, np.float32), Src)
    Raw = g.MeasureRaw(Src)
    assert Raw["bore_ok"] and Raw["inner_diameter"] == pytest.approx(15.0, rel=0.005)
    assert np.linalg.norm(np.array(Raw["bore_origin"]) - M.vertices[:len(Band.vertices)].mean(0)) < 0.1


def test_bore_of_an_open_crossover_ring_where_no_flat_slice_is_closed():
    import tempfile
    import trimesh
    from pathlib import Path
    from p3 import geometry as g
    Parts = []
    for K, Dz in enumerate((2.2, -2.2, 2.2, -2.2)):                      # quarter-bands at alternating heights
        T = trimesh.creation.torus(major_radius=9.0, minor_radius=1.5, major_sections=192, minor_sections=48)
        Ang = np.mod(np.arctan2(T.triangles_center[:, 1], T.triangles_center[:, 0]) - K * np.pi / 2, 2 * np.pi)
        T.update_faces((Ang < np.pi / 2 + 0.05) | (Ang > 2 * np.pi - 0.05))
        T.apply_translation([0, 0, Dz])
        Parts.append(T)
    M = trimesh.util.concatenate(Parts)
    M.apply_transform(trimesh.transformations.rotation_matrix(0.4, [1, 0.5, 0.2]))
    Src = Path(tempfile.mkdtemp()) / "cross.stl"
    g.WriteStl(np.asarray(M.triangles, np.float32), Src)
    Raw = g.MeasureRaw(Src)
    assert Raw["bore_ok"] and Raw["inner_diameter"] == pytest.approx(15.0, rel=0.01), Raw.get("bore_fit")
    assert max(S["bins_filled"] for S in Raw["bore_slices"]) < g.Directions * 0.9      # no single slice is closed


def test_a_small_pocket_is_never_taken_for_the_bore():
    import tempfile
    import trimesh
    from pathlib import Path
    from p3 import geometry as g
    Box = trimesh.creation.box(extents=(20, 20, 4))
    Pocket = trimesh.creation.cylinder(radius=1.0, height=4.4, sections=64)  # a small through-hole, not a bore
    Pocket.invert()
    M = trimesh.util.concatenate([Box, Pocket])
    Src = Path(tempfile.mkdtemp()) / "pocket.stl"
    g.WriteStl(np.asarray(M.triangles, np.float32), Src)
    assert not g.MeasureRaw(Src)["bore_ok"]


def test_geometry_without_a_bore_needs_review():
    import trimesh
    Buf = io.BytesIO()
    trimesh.creation.box(extents=(10, 10, 2)).export(Buf, file_type="stl")
    G = MeasureRing(Buf.getvalue(), "stl", UsSizeToInnerDiameterMm(7))
    assert G.status == "needs_review" and G.production is None and "bore" in G.problems[0]


def test_us_size_table():
    assert UsSizeToInnerDiameterMm(10) == pytest.approx(19.758, abs=0.01)
    assert UsSizeToInnerDiameterMm(7) == pytest.approx(17.32, abs=0.01)


async def test_session_pipeline_cost_duration_and_flags(HS):
    H = HS
    Did, Sel, Cust = await _Journey(H, Refine=False, Bag=False)
    await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={}, headers=Admin)
    await H.Idle()
    await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 9}, headers=Admin)   # re-measure only
    await H.Idle()
    D = await _Session(H, Did)
    assert D["cost"]["session"] == 0.0                                  # mock requests are free
    assert {S["kind"] for S in D["pipeline"]["steps"]} == {"design", "movie", "3d"}
    assert all(S["duration_s"] is not None and S["duration_s"] >= 0 for S in D["pipeline"]["steps"])
    U = next(X for X in (await H.Client.get("/api/admin/users", headers=Admin)).json()["users"]
             if X["account_id"] == D["session"]["account_id"])
    assert D["session"]["mock"] and (U["sessions"], U["ai_cost"]) == (0, 0.0)       # mock: not in user totals
    # Price the same requests as if they had been live (fal) submissions.
    MakeLive(H)
    D = await _Session(H, Did)
    Steps = {(S["kind"], S["detail"].startswith("reused")): S for S in D["pipeline"]["steps"]}
    assert Steps[("design", False)]["cost"] == pytest.approx(0.60) and Steps[("design", False)]["requests"] == 4
    assert Steps[("movie", False)]["cost"] == pytest.approx(6 * 0.04)                 # 6 s at 768P
    assert Steps[("3d", False)]["cost"] == pytest.approx(90 * 0.02)                   # 2048quality, no texture/PBR
    assert Steps[("3d", True)]["cost"] == 0.0                                         # reused raw mesh
    U = next(X for X in (await H.Client.get("/api/admin/users", headers=Admin)).json()["users"]
             if X["account_id"] == D["session"]["account_id"])
    assert D["cost"]["session"] == pytest.approx(0.60 + 0.24 + 1.80) == pytest.approx(U["ai_cost"]) and U["sessions"] == 1
    assert D["user"]["status"] in ("active", "unused") and D["artifacts"]["image_url"] and D["artifacts"]["movie_url"]
    assert D["last_choice"]["ring_size"] == 7.0 and D["last_choice"]["material_id"] == "silver"
    Row = next(X for X in (await H.Client.get("/api/admin/sessions", headers=Admin)).json()["sessions"] if X["session_id"] == Did)
    assert Row["mock"] is False
    assert (Row["has_image"], Row["has_movie"], Row["has_3d"]) == (True, True, True) and Row["user_status"]


async def test_ai_prices_refresh_and_edit(HS, monkeypatch):
    H = HS
    P = (await H.Client.get("/api/admin/ai-prices", headers=Admin)).json()
    assert P["endpoints"][endpoints.ImageGenerate]["per_image"] == 0.15 and P["fal_key_configured"] is False
    assert (await H.Client.post("/api/admin/ai-prices/refresh", headers=Admin)).status_code == 400    # no key here
    from p3 import aipricing

    class Resp:
        status_code = 200
        def json(self):
            return {"prices": [{"endpoint_id": endpoints.ImageGenerate, "unit_price": 0.2, "unit": "images"},
                               {"endpoint_id": endpoints.Movie, "unit_price": 0.05, "unit": "seconds"}]}
    Seen = {}
    monkeypatch.setattr(aipricing.httpx, "get", lambda Url, params, headers, timeout: Seen.update(h=headers) or Resp())
    App = H.App.state
    Book = aipricing.PriceBook(H.Ctx.Db)
    R = Book.RefreshFromFal("test-key", "admin")
    assert Seen["h"] == {"Authorization": "Key test-key"}
    assert R["endpoints"][endpoints.ImageGenerate]["per_image"] == 0.2
    assert R["endpoints"][endpoints.Movie]["per_second"] == {"480P": 0.05, "768P": 0.08, "1080P": 0.16}   # scaled
    assert "test-key" not in json.dumps(R)
    Bad = await H.Client.put("/api/admin/ai-prices", json={"prices": {"endpoints": {"x": {"per_image": -1}}}}, headers=Admin)
    assert Bad.status_code == 400
    assert (await H.Client.get("/api/admin/ai-prices")).status_code == 403


async def test_mock_sessions_are_hidden_from_the_admin_unless_asked(HS):
    H = HS
    Batch = await H.NewDesign("Plain band")                                 # made in mock mode
    L = (await H.Client.get("/api/admin/sessions", headers=Admin)).json()
    assert L["sessions"] == [] and L["mock_sessions"] == 1
    All = (await H.Client.get("/api/admin/sessions?include_mock=true", headers=Admin)).json()["sessions"]
    assert [X["session_id"] for X in All] == [Batch["design_id"]] and All[0]["mock"]
    D = await _Session(H, Batch["design_id"])                               # still viewable on its own
    assert D["session"]["mock"]
    assert H.Ctx.Db.One("SELECT ai_mode FROM designs")["ai_mode"] == "mock"
    E = H.Ctx.Db.One("SELECT data_json FROM session_events ORDER BY id DESC LIMIT 1")
    assert E is None or json.loads(E["data_json"])["ai_mode"] == "mock"


async def test_geometry_failure_retries_locally_without_another_hi3d_call(HS, monkeypatch):
    H = HS
    from p3 import geoqueue
    Batch = await H.NewDesign("Plain band")
    Did = Batch["design_id"]
    Real = geoqueue.WorkerCommand
    monkeypatch.setattr(geoqueue, "WorkerCommand", lambda: [sys.executable, "-c", "import sys; sys.exit(3)"])  # out of memory
    T = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={}, headers=Admin)).json()
    await H.Idle()
    St = (await H.Client.get(f"/api/admin/3d/{T['id']}/status", headers=Admin)).json()
    assert St["status"] == "failed" and "memory" in St["error"] and St["can_retry"] and St["retry_is_local"]
    assert St["stages"][-1]["stage"] == "failed"
    assert (await H.Client.get(f"/api/admin/3d/{T['id']}/stl/raw", headers=Admin)).status_code == 200   # raw kept
    assert (await H.Client.get("/api/health")).status_code == 200                # the server is fine
    monkeypatch.setattr(geoqueue, "WorkerCommand", Real)
    R = (await H.Client.post(f"/api/admin/3d/{T['id']}/retry", headers=Admin)).json()
    assert R["retried"] == "geometry"
    await H.Idle()
    D = (await _Session(H, Did))["three_d"][0]
    assert D["status"] == "measured" and D["geometry"]["production"]["volume_mm3"] > 0
    assert len(H.Provider.SubmissionsFor(endpoints.Mesh)) == 1                    # no second paid Hi3D request
    assert (await H.Client.post(f"/api/admin/3d/{T['id']}/retry", headers=Admin)).status_code == 409
    # A new size or material is arithmetic on the stored measurement: no new job, instantly measured.
    Jobs = H.Ctx.Db.One("SELECT COUNT(*) AS n FROM geometry_jobs WHERE kind = 'measure'")["n"]
    T2 = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 7, "material_id": "silver"},
                              headers=Admin)).json()
    assert T2["status"] == "measured" and H.Ctx.Db.One("SELECT COUNT(*) AS n FROM geometry_jobs WHERE kind = 'measure'")["n"] == Jobs
    A, B = T2["geometry"]["production"], D["geometry"]["production"]
    S = A["inner_diameter_mm"] / B["inner_diameter_mm"]
    assert A["volume_mm3"] == pytest.approx(B["volume_mm3"] * S ** 3) and A["surface_area_mm2"] == pytest.approx(B["surface_area_mm2"] * S ** 2)
    # A measurement from an older algorithm version is re-measured once (still no Hi3D call).
    H.Ctx.Db.Execute("UPDATE raw_geometry SET method_version = 'ring-measure-once-v3'")
    T3 = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 8}, headers=Admin)).json()
    assert T3["status"] in ("queued", "measuring")
    await H.Idle()
    assert (await H.Client.get(f"/api/admin/3d/{T3['id']}/status", headers=Admin)).json()["status"] == "measured"
    assert H.Ctx.Db.One("SELECT method_version FROM raw_geometry")["method_version"] == g.FastMethodVersion
    assert len(H.Provider.SubmissionsFor(endpoints.Mesh)) == 1


async def test_persistent_queue_position_cancel_and_restart_recovery(HS, monkeypatch):
    H = HS
    Q = H.App.state.Services.Geometry
    monkeypatch.setattr(Q, "Kick", lambda: None)                               # hold the queue still
    Ids = []
    for Text in ("Plain band", "Twisted band"):
        Batch = await H.NewDesign(Text)
        Ids.append((await H.Client.post(f"/api/admin/sessions/{Batch['design_id']}/3d", json={}, headers=Admin)).json()["id"])
        await H.Idle()                                                         # Hi3D finished; measure job queued
    S1, S2 = [(await H.Client.get(f"/api/admin/3d/{I}/status", headers=Admin)).json() for I in Ids]
    assert (S1["status"], S1["queue"]["ahead"], S2["queue"]["ahead"]) == ("queued", 0, 1)
    assert S2["can_cancel"] and S2["stages"][-1]["stage"] == "queued" and S2["stages"][-1]["ended_at"] is None
    C = (await H.Client.post(f"/api/admin/3d/{Ids[1]}/cancel", headers=Admin)).json()
    assert C["status"] == "cancelled"
    assert (await H.Client.post(f"/api/admin/3d/{Ids[1]}/cancel", headers=Admin)).status_code == 409
    # The server stopped while the first job was running: on startup it is queued again and completes.
    H.Ctx.Db.Execute("UPDATE geometry_jobs SET status = 'running', attempts = 1 WHERE mesh_id = "
                     "(SELECT mesh_id FROM session_3d WHERE id = ?)", (Ids[0],))
    monkeypatch.undo()
    assert Q.Reconcile() == 1
    await H.Idle()
    assert (await H.Client.get(f"/api/admin/3d/{Ids[0]}/status", headers=Admin)).json()["status"] == "measured"
    # A cancelled request can be retried locally.
    assert (await H.Client.post(f"/api/admin/3d/{Ids[1]}/retry", headers=Admin)).json()["retried"] == "geometry"
    await H.Idle()
    assert (await H.Client.get(f"/api/admin/3d/{Ids[1]}/status", headers=Admin)).json()["status"] == "measured"


async def test_signed_download_link_streams_the_file_without_the_admin_header(HS):
    H = HS
    Batch = await H.NewDesign("Plain band")
    T = (await H.Client.post(f"/api/admin/sessions/{Batch['design_id']}/3d", json={}, headers=Admin)).json()
    await H.Idle()
    assert (await H.Client.post(f"/api/admin/3d/{T['id']}/download-link", json={"stage": "raw"})).status_code == 403
    L = (await H.Client.post(f"/api/admin/3d/{T['id']}/download-link", json={"stage": "raw"}, headers=Admin)).json()
    assert L["bytes"] > 84 and "sig=" in L["url"]
    Path = L["url"].split("/api/", 1)[1]
    R = await H.Client.get("/api/" + Path)                                    # no Authorization header
    assert R.status_code == 200 and len(R.content) == L["bytes"] and "attachment" in R.headers["content-disposition"]
    Bad = await H.Client.get("/api/" + Path.replace("sig=", "sig=0"))
    assert Bad.status_code == 403
    Expired = "/api/" + Path.split("?")[0] + "?exp=1&sig=" + Path.split("sig=")[1]
    assert (await H.Client.get(Expired)).status_code == 403
    Other = await H.Client.get("/api/" + Path.replace("/stl/raw", "/stl/preview"))      # signature is per file
    assert Other.status_code == 403


async def test_second_hi3d_model_for_a_design_needs_the_typed_confirmation_and_is_logged(HS):
    H = HS
    Batch = await H.NewDesign("Plain band")
    Did, A, B = Batch["design_id"], Batch["candidates"][0], Batch["candidates"][1]
    T1 = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"candidate_id": A["id"]}, headers=Admin)).json()
    await H.Idle()
    assert len(H.Provider.SubmissionsFor(endpoints.Mesh)) == 1
    # The owner selects another option: 3D still reuses the existing model — no Hi3D call, no question
    await H.Client.put(f"/api/designs/{Did}/selection", json={"candidate_id": B["id"]})
    T2 = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 8}, headers=Admin)).json()
    await H.Idle()
    assert T2["candidate_id"] == A["id"] and T2["mesh_id"] == T1["mesh_id"]
    assert len(H.Provider.SubmissionsFor(endpoints.Mesh)) == 1
    # An explicitly different option is refused by the server without the exact typed phrase
    R = await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"candidate_id": B["id"]}, headers=Admin)
    assert R.status_code == 409 and R.json()["error"]["code"] == "hi3d_model_exists"
    assert "GENERATE NEW 3D" in R.json()["error"]["message"] and "R-1001-A" in R.json()["error"]["message"]
    R = await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"candidate_id": B["id"], "override": "generate new 3d"}, headers=Admin)
    assert R.status_code == 409 and len(H.Provider.SubmissionsFor(endpoints.Mesh)) == 1
    T3 = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"candidate_id": B["id"], "override": "GENERATE NEW 3D"},
                              headers=Admin)).json()
    await H.Idle()
    assert T3["candidate_id"] == B["id"] and T3["mesh_id"] != T1["mesh_id"] and len(H.Provider.SubmissionsFor(endpoints.Mesh)) == 2
    # The override is recorded on the journey and in the Admin activity log, with who did it
    D = await _Session(H, Did)
    Ov = [E for E in D["timeline"] if E["kind"] == "admin_3d_new_model_override"]
    assert len(Ov) == 1 and Ov[0]["data"]["existing_ring_id"] == "R-1001-A" and Ov[0]["data"]["candidate_ring_id"] == "R-1001-B"
    assert Ov[0]["data"]["by"]
    assert D["three_d_defaults"]["existing_model"]["ring_id"] == "R-1001-B" and D["three_d_defaults"]["existing_model"]["measured"]
    assert [O["ring_id"] for O in D["three_d_defaults"]["options"]] == ["R-1001-A", "R-1001-B", "R-1001-C", "R-1001-D"]
    Dash = (await H.Client.get("/api/admin/dashboard", headers=Admin)).json()
    assert any(E["kind"] == "admin_3d_new_model_override" and E["by"] and E["ring_id"] == "R-1001" for E in Dash["admin_activity"])


async def test_shared_ring_ids_for_designs_options_refinements_and_3d_files(HS):
    H = HS
    B1 = await H.NewDesign("Plain band")
    B2 = await H.NewDesign("Twisted band")
    D1 = await _Session(H, B1["design_id"])
    D2 = await _Session(H, B2["design_id"])
    assert (D1["session"]["ring_id"], D2["session"]["ring_id"]) == ("R-1001", "R-1002")     # creation order
    Opts = [C["ring_id"] for C in D1["design"]["batches"][0]["candidates"]]
    assert sorted(Opts) == ["R-1001-A", "R-1001-B", "R-1001-C", "R-1001-D"]
    from p3 import ringids
    Cand = B1["candidates"][1]["id"]
    H.Ctx.Db.Execute("INSERT INTO batches (id, design_id, kind, parent_candidate_id, user_text, effective_prompt, endpoint, "
                     "desired_count, config_version, created_at) SELECT 'bat_r1', design_id, 'refine', ?, 'thinner', "
                     "'thinner', endpoint, 4, config_version, '2999-01-01T00:00:00+00:00' FROM batches WHERE design_id = ? LIMIT 1",
                     (Cand, B1["design_id"]))
    H.Ctx.Db.Execute("INSERT INTO candidates (id, batch_id, slot, status, seed, created_at, updated_at) "
                     "VALUES ('cnd_r1b', 'bat_r1', 1, 'ready', 1, '2999-01-01T00:00:00+00:00', '2999-01-01T00:00:00+00:00')")
    assert ringids.CandidateRef(H.Ctx.Db, "cnd_r1b") == "R-1001-R1B"
    # 3D results and downloads carry the design name and the ring ID of the exact option
    # (<Design-Name>_<Ring ID>[_<Order ID>]_<Material>_US<size>.stl — no Order ID is invented).
    from p3.production3d import SlugPart
    T = (await H.Client.post(f"/api/admin/sessions/{B1['design_id']}/3d", json={"candidate_id": Cand}, headers=Admin)).json()
    await H.Idle()
    assert T["ring_id"] == "R-1001-B"
    Name = SlugPart(D1["session"]["title"])
    assert Name and "_" not in Name and " " not in Name
    assert SlugPart("The Éternité Ring") == "The-Eternite-Ring" and SlugPart("Stainless Steel") == "Stainless-Steel"
    R = await H.Client.get(f"/api/admin/3d/{T['id']}/stl/raw", headers=Admin)
    assert f'filename="{Name}_R-1001-B_raw.stl"' in R.headers["content-disposition"]
    E = (await H.Client.post(f"/api/admin/3d/{T['id']}/export", headers=Admin)).json()
    await H.Idle()
    E = (await H.Client.get(f"/api/admin/3d/{T['id']}/export/{E['job_id']}", headers=Admin)).json()
    R = await H.Client.get(E["url"].removeprefix(H.Ctx.Settings.BasePath))
    Material = SlugPart(H.Ctx.Catalog.Get(T["material_id"]).Label)
    assert f'filename="{Name}_R-1001-B_{Material}_US10.stl"' in R.headers["content-disposition"]
    assert "ORD-" not in R.headers["content-disposition"]


async def test_processing_complete_is_not_production_ready_when_the_model_has_warnings(HS, monkeypatch):
    """Done ≠ approved: a flagged result is "Processing complete — production review required", with the
    reasons and the next step, while the numbers, the 3D view and the scaled STL stay available."""
    H = HS
    from p3 import production3d
    monkeypatch.setattr(production3d, "MaxRoundness", -1.0)              # every bore counts as "not round"
    Did = (await H.NewDesign("Plain band"))["design_id"]
    T = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={}, headers=Admin)).json()
    await H.Idle()
    T = (await _Session(H, Did))["three_d"][0]
    assert T["status"] == "needs_review" and T["production_state"] == "review_required"
    assert [R["code"] for R in T["review"]] == ["bore_not_round"] and T["review"][0]["action"]
    assert T["geometry"]["production"]["volume_mm3"] > 0 and T["price"]["weight_g"] > 0    # the numbers exist
    St = (await H.Client.get(f"/api/admin/3d/{T['id']}/status", headers=Admin)).json()
    assert St["done"] and St["production_state"] == "review_required"
    assert St["stages"][-1]["stage"] == "review_required" and "review required" in St["stages"][-1]["label"].lower()
    assert St["raw"]["preview_ready"]                                      # the 3D view is there to inspect it
    Row = next(X for X in (await H.Client.get("/api/admin/sessions?include_mock=true", headers=Admin)).json()["sessions"]
               if X["session_id"] == Did)
    assert Row["three_d_state"] == "review_required" and Row["three_d_review"] and Row["has_3d"]
    # Downloading the scaled STL works, and does not approve anything
    E = (await H.Client.post(f"/api/admin/3d/{T['id']}/export", headers=Admin)).json()
    await H.Idle()
    assert (await H.Client.get(f"/api/admin/3d/{T['id']}/export/{E['job_id']}", headers=Admin)).json()["status"] == "done"
    assert (await _Session(H, Did))["three_d"][0]["production_state"] == "review_required"
    # A clean model is complete — until the background edge check finds open edges
    monkeypatch.setattr(production3d, "MaxRoundness", 0.04)
    Did2 = (await H.NewDesign("Twisted band"))["design_id"]
    T2 = (await H.Client.post(f"/api/admin/sessions/{Did2}/3d", json={}, headers=Admin)).json()
    await H.Idle()
    T2 = (await _Session(H, Did2))["three_d"][0]
    assert T2["status"] == "measured" and T2["production_state"] == "complete" and T2["review"] == []
    H.Ctx.Db.Execute("UPDATE raw_geometry SET integrity = 'open' WHERE mesh_id = ?", (T2["mesh_id"],))
    T2 = (await _Session(H, Did2))["three_d"][0]
    assert T2["status"] == "measured" and T2["production_state"] == "review_required"
    assert [R["code"] for R in T2["review"]] == ["open_edges"]
    Row = next(X for X in (await H.Client.get("/api/admin/sessions?include_mock=true", headers=Admin)).json()["sessions"]
               if X["session_id"] == Did2)
    assert Row["three_d_state"] == "review_required"



def test_a_bore_that_is_not_round_is_fitted_as_an_ellipse_and_can_be_made_round():
    """A ring whose bore is an ellipse (here 15% longer one way) is flagged (roundness over 4%); MeasureRaw also fits
    the ellipse, and ExportCorrectedStl scales the model along the ellipse's axes so the bore becomes a circle of the
    target diameter — measured again, it is round and the right size, and the outer shape is round again too."""
    import tempfile
    import trimesh
    from pathlib import Path
    from p3 import geometry as g
    M = trimesh.creation.torus(major_radius=9.0, minor_radius=1.5, major_sections=192, minor_sections=64)
    M.apply_scale([1.15, 1.0, 1.0])                                         # the bore: 8.625 × 7.5 (an ellipse)
    M.apply_transform(trimesh.transformations.rotation_matrix(0.5, [0.3, 1, 0.2]))
    D = Path(tempfile.mkdtemp())
    g.WriteStl(np.asarray(M.triangles, np.float32), D / "oval.stl")
    Raw = g.MeasureRaw(D / "oval.stl")
    assert Raw["bore_ok"] and Raw["roundness"] > 0.04
    E = Raw["bore_ellipse"]
    assert E["a"] == pytest.approx(8.625, rel=0.01) and E["b"] == pytest.approx(7.5, rel=0.01)
    assert E["residual"] < 0.01                                               # an ellipse: the correction leaves nothing
    # The bore across its centre: narrowest and widest (what a caliper reads) — 15 × 17.25 for this oval
    assert Raw["bore_min_diameter"] == pytest.approx(15.0, rel=0.01) and Raw["bore_max_diameter"] == pytest.approx(17.25, rel=0.01)
    Target = g.UsSizeToInnerDiameterMm(7)
    Corr = g.ExportCorrectedStl(D / "oval.stl", Raw, Target, D / "round.stl")
    assert Corr["scale_major"] == pytest.approx(Target / 2 / 8.625, rel=0.01) and Corr["scale_minor"] == pytest.approx(Target / 2 / 7.5, rel=0.01)
    assert Corr["faces"] == len(M.faces)
    Fixed = g.MeasureRaw(D / "round.stl")
    assert Fixed["bore_ok"] and Fixed["inner_diameter"] == pytest.approx(Target, rel=0.005) and Fixed["roundness"] < 0.01
    assert Fixed["bore_min_diameter"] == pytest.approx(Target, rel=0.01) and Fixed["bore_max_diameter"] == pytest.approx(Target, rel=0.01)
    assert Fixed["extent_x"] == pytest.approx(Fixed["extent_y"], rel=0.01)          # the outer shape is round again
    assert Fixed["extent_z"] == pytest.approx(3.0 * Corr["scale_axis"], rel=0.02)
    assert open(D / "round.stl", "rb").read(80).rstrip() == b"XJet P3 scaled ring, bore made round"
    # A round bore is fitted as (almost) a circle, so nothing would be corrected
    Plain = trimesh.creation.torus(major_radius=9.0, minor_radius=1.5, major_sections=192, minor_sections=64)
    g.WriteStl(np.asarray(Plain.triangles, np.float32), D / "plain.stl")
    R = g.MeasureRaw(D / "plain.stl")
    assert R["bore_ellipse"]["a"] / R["bore_ellipse"]["b"] == pytest.approx(1.0, abs=0.005)
