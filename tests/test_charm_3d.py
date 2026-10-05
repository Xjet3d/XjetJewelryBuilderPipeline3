"""Charms, phase 4 — a 3D path of their own. No bore and no ring size: a charm is measured once, the whole charm
(loop included — a charm's size is its total height) is scaled to the size in mm, priced from the charm price
book, and exported lying flat. No loop detection. The ring 3D path is unchanged (tests/test_sessions.py)."""

import math

import numpy as np
import pytest

from p3 import charmgeometry as CharmGeo
from p3.geometry import UsSizeToInnerDiameterMm, WriteStl
from tests.conftest import Harness

AdminKey = "charm-3d-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Stl = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])


@pytest.fixture
async def H3(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    assert (await Obj.Client.post("/api/admin/login", json={"key": AdminKey})).status_code == 200   # Admin preview of charms
    yield Obj
    await Obj.Close()


async def CharmJourney(H, Size=25, Material="silver"):
    B = await H.NewDesign("A crescent moon charm with a small star", product="charm")
    C = await H.Proceed(B["design_id"], B["candidates"][0]["id"])
    await H.Idle()
    if Size is not None:
        await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": Size, "material_id": Material})
    return B, C


async def Session(H, Did):
    R = await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)
    assert R.status_code == 200, R.text
    return R.json()


Prices = {"silver": {"fixed_prices": {"20": 145, "25": 165}, "price_per_g": 30, "cost_per_g": 12}}


async def test_a_charm_is_measured_once_and_scaled_by_its_height(H3):
    H = H3
    assert (await H.Client.put("/api/admin/charm-prices", json={"materials": Prices}, headers=Admin)).status_code == 200
    B, C = await CharmJourney(H, 25)
    Did = B["design_id"]
    D = await Session(H, Did)
    assert (D["three_d_defaults"]["customer_size"], D["three_d_defaults"]["production_size"]) == (25.0, 25.0)
    assert D["catalog"]["charm_sizes"] == [15.0, 20.0, 25.0, 30.0] and "ring_sizes" not in D["catalog"]
    assert [M["label"] for M in D["catalog"]["materials"]][:3] == ["Stainless Steel", "Sterling Silver", "14K Gold Vermeil"]
    R = await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={}, headers=Admin)
    assert R.status_code == 200, R.text
    assert (R.json()["production_size"], R.json()["size_source"], R.json()["material_id"]) == (25.0, "customer", "silver")
    await H.Idle()
    T = (await Session(H, Did))["three_d"][0]
    Prod = T["geometry"]["production"]
    assert Prod["method_version"] == CharmGeo.CharmMethodVersion and Prod["checks"]["up_source"] == "model_up"   # the mock charm stands
    assert Prod["size_y_mm"] == pytest.approx(25.0) and Prod["inner_diameter_mm"] is None                       # height = the size
    assert Prod["size_z_mm"] < Prod["size_x_mm"] < Prod["size_y_mm"]                       # thin, narrower than tall (loop on top)
    assert (T["product_type"], T["target_height_mm"], T["target_inner_diameter_mm"], T["size_label"], T["material_label"]) == \
        ("charm", 25.0, None, "25 mm", "Sterling Silver")
    # The size is the total height, loop included: a closed charm model is complete, nothing to review
    assert T["status"] == "measured" and T["production_state"] == "complete" and T["review"] == []
    assert T["height_basis"] == "The total height of the charm, including the attachment loop at the top."
    Price = T["price"]
    assert Price["cost_model_version"] == "charms-v2" and Price["breakdown"]["pricing"] == "charm"
    assert math.isclose(Price["weight_g"], Prod["volume_mm3"] / 1000 * Price["density_g_cm3"], rel_tol=1e-3)
    assert Price["production_cost"] == pytest.approx(Price["weight_g"] * 12, abs=0.01)
    assert Price["calculated_price"] == pytest.approx(Price["weight_g"] * 30, abs=0.01)
    assert (Price["fixed_price"], Price["fixed_price_version"]) == (165.0, "charms-v2")      # the customer's 25 mm price
    # The scaled STL: lying flat, height along Y = 25 mm, its own header and an mm file name
    E = (await H.Client.post(f"/api/admin/3d/{T['id']}/export", headers=Admin)).json()
    await H.Idle()
    E = (await H.Client.get(f"/api/admin/3d/{T['id']}/export/{E['job_id']}", headers=Admin)).json()
    assert E["status"] == "done", E
    File = await H.Client.get(E["url"].removeprefix(H.Ctx.Settings.BasePath))
    assert File.status_code == 200 and File.content[:80].rstrip() == b"XJet P3 scaled charm 25 mm total height incl. loop"
    assert "C-1001-A_Sterling-Silver_25mm.stl" in File.headers["content-disposition"]
    Tri = np.frombuffer(File.content[84:], dtype=Stl)["v"].reshape(-1, 3)
    Span = Tri.max(0) - Tri.min(0)
    assert Span[1] == pytest.approx(25.0, rel=1e-4) and Span[0] == pytest.approx(Prod["size_x_mm"], rel=1e-4)
    assert Span[2] == pytest.approx(Prod["size_z_mm"], rel=1e-4)
    assert abs(Tri.max(0)[1] + Tri.min(0)[1]) < 1e-3                                             # centred
    # Background jobs ran on the charm too (light preview, edge check)
    St = (await H.Client.get(f"/api/admin/3d/{T['id']}/status", headers=Admin)).json()
    assert St["raw"]["preview_ready"] and St["raw"]["integrity"] in ("closed", "open") and St["can_retry"] is False


async def test_charm_3d_sizes_defaults_and_materials(H3):
    H = H3
    B, _C = await CharmJourney(H, None)                           # the customer chose no size
    Did = B["design_id"]
    D = await Session(H, Did)
    assert (D["three_d_defaults"]["customer_size"], D["three_d_defaults"]["production_size"]) == (None, 20.0)
    assert D["catalog"]["charm_default_size"] == 20.0
    T = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={}, headers=Admin)).json()
    assert (T["production_size"], T["size_source"]) == (20.0, "default")
    await H.Idle()
    # Recalculating for another size and material reuses the model: arithmetic, no new Hi3D request
    Meshes = H.Ctx.Db.One("SELECT COUNT(*) AS n FROM meshes")["n"]
    T2 = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 22.5, "material_id": "gold_14k_yellow"},
                              headers=Admin)).json()
    await H.Idle()
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM meshes")["n"] == Meshes
    T2 = next(X for X in (await Session(H, Did))["three_d"] if X["id"] == T2["id"])
    assert (T2["size_source"], T2["material_source"], T2["size_label"]) == ("admin_override", "admin_override", "22.5 mm")
    assert T2["geometry"]["production"]["size_y_mm"] == pytest.approx(22.5)
    assert T2["price"]["status"] == "cost_model_not_configured" and "charm" in T2["price"]["breakdown"]["reason"]
    for Bad in (2, 101, "abc"):
        R = await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": Bad}, headers=Admin)
        assert R.status_code == 400 and R.json()["error"]["code"] == "invalid_charm_size", Bad


async def test_a_waiting_charm_result_is_priced_from_the_charm_book_only(H3):
    H = H3
    B, _C = await CharmJourney(H, 20)
    await H.Client.post(f"/api/admin/sessions/{B['design_id']}/3d", json={}, headers=Admin)
    await H.Idle()
    T = (await Session(H, B["design_id"]))["three_d"][0]
    assert T["price"]["status"] == "cost_model_not_configured" and T["price"]["production_cost"] is None
    # A ring price change never prices a charm (silver has ring $/g)
    Ring = (await H.Client.get("/api/admin/material-prices", headers=Admin)).json()
    Rows = {R["id"]: {K: R[K] for K in ("density_g_cm3", "price_per_g", "cost_per_g", "fixed_price")} for R in Ring["rows"]}
    assert Rows["silver"]["price_per_g"] and Rows["silver"]["cost_per_g"]
    await H.Client.put("/api/admin/material-prices", json={"materials": Rows, "note": "same"}, headers=Admin)
    T = (await Session(H, B["design_id"]))["three_d"][0]
    assert T["price"]["production_cost"] is None
    # Setting the charm $/g prices it
    await H.Client.put("/api/admin/charm-prices", json={"materials": Prices}, headers=Admin)
    T = (await Session(H, B["design_id"]))["three_d"][0]
    assert T["price"]["status"] == "calculated" and T["price"]["cost_model_version"] == "charms-v2"
    assert T["price"]["production_cost"] == pytest.approx(T["price"]["weight_g"] * 12, abs=0.01)


async def test_the_ring_3d_path_is_unchanged_beside_a_charm(H3):
    H = H3
    Ring = await H.NewDesign("Plain band")
    await H.Proceed(Ring["design_id"], Ring["candidates"][0]["id"])
    B, _C = await CharmJourney(H, 25)
    for Did in (Ring["design_id"], B["design_id"]):
        assert (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={}, headers=Admin)).status_code == 200
    await H.Idle()
    T = (await Session(H, Ring["design_id"]))["three_d"][0]
    assert T["status"] == "measured" and "product_type" not in T and "target_height_mm" not in T
    assert T["geometry"]["production"]["method_version"].startswith("ring-measure-once")
    assert math.isclose(T["geometry"]["production"]["inner_diameter_mm"], UsSizeToInnerDiameterMm(10), rel_tol=0.01)
    assert T["review"] == [] and T["price"]["cost_model_version"].startswith("materials-v")
    C = (await Session(H, B["design_id"]))["three_d"][0]
    assert C["geometry"]["production"]["method_version"] == CharmGeo.CharmMethodVersion
    # A ring size for a charm, a charm size for a ring: refused
    assert (await H.Client.post(f"/api/admin/sessions/{Ring['design_id']}/3d", json={"production_size": 22.5},
                                headers=Admin)).json()["error"]["code"] == "invalid_ring_size"
    # The session pipeline names the 3D size in each product's own unit
    RingSteps = [S["text"] for S in (await Session(H, Ring["design_id"]))["pipeline"]["steps"] if S["kind"] == "3d"]
    CharmSteps = [S["text"] for S in (await Session(H, B["design_id"]))["pipeline"]["steps"] if S["kind"] == "3d"]
    assert (RingSteps, CharmSteps) == (["US 10"], ["25 mm"])
    # … and so do the Admin's 3D viewer title and journey / activity lines (a ring reads "US 10" as before)
    Js = (await H.Client.get("/static/admin.js")).text
    assert "(t.size_label || 'US ' + t.production_size)" in Js
    assert Js.count("d.charm_size != null && d.charm_size + ' mm'") == 2
    assert "d.production_size && (charm ? d.production_size + ' mm' : 'US ' + d.production_size)" in Js
    Page = (await H.Client.get("/admin/")).text
    assert "stepText({ kind: e.kind, data: e.data, product_type: e.product_type })" in Page


async def test_an_ordered_charm_line_gets_its_3d_at_the_ordered_size(H3):
    from tests.test_orders import Address, Customer
    H = H3
    await H.Client.put("/api/admin/charm-prices", json={"materials": Prices}, headers=Admin)
    B, C = await CharmJourney(H, 20)
    await H.Client.post("/api/bag", json={"customization_id": C["id"]})
    O = (await H.Client.post("/api/orders", json={"customer": Customer, "address": Address, "terms_accepted": True,
                                                  "client_request_id": "c3d"})).json()
    Line = O["lines"][0]
    assert (await H.Client.post(f"/api/admin/sessions/{B['design_id']}/3d", json={"production_size": 25}, headers=Admin)).status_code == 200
    await H.Idle()
    L = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()["lines"][0]
    assert L["three_d_match"] is False and L["three_d_latest"]["size"] == 25.0          # a 25 mm result is not this 20 mm line
    R = await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{Line['id']}/3d", headers=Admin)
    assert R.status_code == 200 and R.json()["prepared"]
    await H.Idle()
    L = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()["lines"][0]
    assert L["three_d_match"] is True and L["three_d_state"] == "complete"
    T = H.Ctx.Db.One("SELECT production_size, material_id FROM session_3d WHERE id = ?", (L["three_d_id"],))
    assert (T["production_size"], T["material_id"]) == (20.0, "silver")


def test_the_charm_frame_follows_the_model_up_direction(tmp_path):
    def Box(Lo, Hi):
        (x0, y0, z0), (x1, y1, z1) = Lo, Hi
        V = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0], [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]])
        F = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
        return V[np.array(F)]
    # Wider than tall (a horizontal bar charm): the height is still the model's up direction, not the longest one
    Tri = np.concatenate([Box((-0.5, -0.02, 0.0), (0.5, 0.02, 0.3)), Box((-0.05, -0.01, 0.3), (0.05, 0.01, 0.4))]).astype(np.float32)
    P = tmp_path / "bar.stl"
    WriteStl(Tri, P)
    Raw = CharmGeo.MeasureCharmRaw(P)
    assert (round(Raw["extent_x"], 4), round(Raw["height"], 4), round(Raw["extent_z"], 4), Raw["up_source"]) == (1.0, 0.4, 0.04, "model_up")
    G = CharmGeo.CharmScaled(Raw, 20.0)
    assert G["size_y_mm"] == pytest.approx(20.0) and G["size_x_mm"] == pytest.approx(50.0) and G["inner_diameter_mm"] is None
    assert G["volume_mm3"] == pytest.approx(Raw["volume"] * G["scale_factor"] ** 3)
    # Lying flat (the face level): the longest direction is used instead
    WriteStl(Tri[:, :, [0, 2, 1]].copy(), P)
    assert CharmGeo.MeasureCharmRaw(P)["up_source"] == "largest_spread"


async def test_charm_results_flagged_only_for_the_loop_become_complete(H3):
    H = H3
    B, _C = await CharmJourney(H, 20)
    await H.Client.post(f"/api/admin/sessions/{B['design_id']}/3d", json={}, headers=Admin)
    await H.Idle()
    T = (await Session(H, B["design_id"]))["three_d"][0]
    # As the first charm path left it: flagged only for the loop in the height
    Note = H.Svc.Production3D.LegacyLoopNote
    H.Ctx.Db.Update("session_3d", T["id"], status="needs_review", error=Note)
    Other = H.Ctx.Db.One("SELECT * FROM session_3d WHERE id = ?", (T["id"],))
    assert Other["status"] == "needs_review"
    assert H.Svc.Production3D.ClearLegacyLoopReviews() == 1 and H.Svc.Production3D.ClearLegacyLoopReviews() == 0
    T2 = (await Session(H, B["design_id"]))["three_d"][0]
    assert (T2["status"], T2["production_state"], T2["error"], T2["review"]) == ("measured", "complete", None, [])
    assert T2["geometry"]["production"] == T["geometry"]["production"] and T2["price"]["weight_g"] == T["price"]["weight_g"]
    # Any other problem keeps the flag
    H.Ctx.Db.Update("session_3d", T["id"], status="needs_review", error=Note + " The volume reference check suggests the mesh may not be closed — check the volume.")
    assert H.Svc.Production3D.ClearLegacyLoopReviews() == 0
