"""The flagged STL on the order line, and an accepted bore correction reused across materials (the owner's request of
2026-10-16 after ORD-10003): a line's verdict says why its STL is flagged and offers what fixes it; FixBore starts from
the line; a correction is the very same file in every material — it is computed from the model and the size only,
verified here by computing it for two materials — so one a person accepted is reused, explicitly, for the same design,
version and size in another material: recorded as that result's own correction (naming the one reused) with that
material's weight and price, and activated for its lines. Nothing unaccepted is reused; a restored correction, another
size and an already corrected result are not offered."""

import json

from tests.test_bore_fix_workflow import _Another, _Line, _Oval, _Propose
from tests.test_fulfillment_bridge import Admin, _On, _Placed, _Prepared
from tests.test_send_to_production_rules import _State


async def _PrepareLine(H, OrderId: str) -> dict:
    L = _Line(H, OrderId)
    R = await H.Client.post(f"/api/admin/orders/{OrderId}/lines/{L['id']}/3d", headers=Admin)
    assert R.status_code == 200, R.text
    await H.Idle()
    return _Line(H, OrderId)


async def _Stl(H, OrderId: str) -> dict:
    return (await _State(H, OrderId))["lines"][0]["stl"]


async def test_a_bore_correction_is_the_same_file_in_every_material(tmp_path, monkeypatch):
    from p3.providers import mock
    monkeypatch.setattr(mock, "_MeshBytes", _Oval)
    H = _On(tmp_path)
    try:
        Did, A = await _Placed(H, Qty=1, Click="same-a")
        LA = await _Prepared(H, Did, A["id"])                                                     # silver US 7: flagged (the oval bore)
        F = await _Another(H, Did, LA["candidate_id"], "same-f", Material="vermeil")
        LF = await _PrepareLine(H, F["id"])                                                       # vermeil US 7: its own result, flagged too
        assert (LA["three_d_state"], LF["three_d_state"]) == ("review_required", "review_required") and LA["three_d_id"] != LF["three_d_id"]
        Silver, Vermeil = await _Propose(H, LA["three_d_id"]), await _Propose(H, LF["three_d_id"])
        assert (Silver["file_sha256"], Silver["file_bytes"]) == (Vermeil["file_sha256"], Vermeil["file_bytes"])     # byte for byte the same file
        AS, AV = json.loads(Silver["after_json"]), json.loads(Vermeil["after_json"])
        for K in ("volume_mm3", "surface_mm2", "extents_mm", "roundness", "gauge_mm", "faces", "bore_min_mm", "bore_max_mm"):
            assert AS[K] == AV[K], K
        assert AS["weight_g"] != AV["weight_g"]                                                    # only the weight differs: the material's density
        assert json.loads(Silver["correction_json"]) == json.loads(Vermeil["correction_json"])
    finally:
        await H.Close()


async def test_a_flagged_line_says_why_and_offers_fixbore(tmp_path, monkeypatch):
    from p3.providers import mock
    monkeypatch.setattr(mock, "_MeshBytes", _Oval)
    H = _On(tmp_path)
    try:
        Did, A = await _Placed(H, Qty=1, Click="flag-a")
        LA = await _Prepared(H, Did, A["id"])
        S = await _Stl(H, A["id"])
        assert S["state"] == "review_required" and "not round" in S["reason"] and S["bore_not_round"] and S["roundness"] > 0.04
        assert S["result_id"] == LA["three_d_id"] and S["correction"] is None and S["reusable_correction"] is None
        assert "prepare it again" not in (S["action"] or "") and "FixBore" in S["action"]
        R = await H.Client.post(f"/api/admin/3d/{S['result_id']}/fix-bore", headers=Admin)       # what the line's Fix bore… calls
        assert R.status_code == 200, R.text
        assert (await _Stl(H, A["id"]))["correction"]["status"] in ("computing", "proposed")
        await H.Idle()
        assert (await _Stl(H, A["id"]))["correction"]["status"] == "proposed"
    finally:
        await H.Close()


async def test_an_accepted_correction_is_reused_for_another_material_explicitly_and_only_where_it_is_the_same_file(tmp_path, monkeypatch):
    from p3.providers import mock
    monkeypatch.setattr(mock, "_MeshBytes", _Oval)
    H = _On(tmp_path)
    try:
        Did, A = await _Placed(H, Qty=1, Click="reuse-a")
        LA = await _Prepared(H, Did, A["id"])
        Cand = LA["candidate_id"]
        F = await _Another(H, Did, Cand, "reuse-f", Material="vermeil")                           # the same ring, vermeil, US 7
        D = await _Another(H, Did, Cand, "reuse-d", Size=8)                                         # the same ring, silver, US 8
        await _PrepareLine(H, F["id"])
        await _PrepareLine(H, D["id"])
        assert (await _Stl(H, F["id"]))["reusable_correction"] is None                             # nothing accepted yet: nothing offered
        P1 = await _Propose(H, LA["three_d_id"])
        assert (await _Stl(H, F["id"]))["reusable_correction"] is None                             # proposed is not accepted: never reused
        R = await H.Client.post(f"/api/admin/bore-fixes/{P1['id']}/accept", json={"note": "round US 7"}, headers=Admin)
        assert R.status_code == 200, R.text
        Rev1 = R.json()["revision"]["id"]
        SF = await _Stl(H, F["id"])
        assert SF["reusable_correction"]["bore_fix_id"] == P1["id"] and SF["reusable_correction"]["material_id"] == "silver"
        assert (await _Stl(H, D["id"]))["reusable_correction"] is None                             # another size: another file, not offered
        # Restored to the original: no longer offered; made active again: offered again
        R = await H.Client.post(f"/api/admin/bore-fixes/{P1['id']}/restore", json={"note": "checking the original first"}, headers=Admin)
        assert R.status_code == 200, R.text
        assert (await _Stl(H, F["id"]))["reusable_correction"] is None
        R = await H.Client.post(f"/api/admin/bore-fixes/{P1['id']}/restore", json={"note": "the correction is right after all"}, headers=Admin)
        assert R.status_code == 200, R.text
        assert (await _Stl(H, F["id"]))["reusable_correction"]["bore_fix_id"] == P1["id"]
        # Reused, explicitly: the result's own correction, the same file, vermeil's weight and price, the line switched
        R = await H.Client.post(f"/api/admin/3d/{SF['result_id']}/reuse-bore-fix", json={"note": "the same ring in vermeil"}, headers=Admin)
        assert R.status_code == 200, R.text
        Out = R.json()
        Rev2 = Out["revision"]["id"]
        assert Out["reused_from"] == P1["id"] and Out["bore_fix"]["status"] == "accepted" and Out["bore_fix"]["reused_from"] == P1["id"]
        F2 = H.Ctx.Db.One("SELECT * FROM bore_fixes WHERE id = ?", (Out["bore_fix"]["id"],))
        assert F2["session_3d_id"] == SF["result_id"] and F2["decided_by"] and "reused the correction accepted for Silver" in F2["decision_note"]
        assert F2["file_sha256"] == P1["file_sha256"] and F2["file_path"] == P1["file_path"]
        Row2 = H.Ctx.Db.One("SELECT * FROM session_3d WHERE id = ?", (Rev2,))
        assert (Row2["material_id"], Row2["status"]) == ("vermeil", "measured")
        G1, G2 = (H.Ctx.Db.One("SELECT * FROM geometry_results WHERE session_3d_id = ? AND stage = 'production'", (X,)) for X in (Rev1, Rev2))
        assert (G1["stl_path"], G1["volume_mm3"], G1["size_x_mm"]) == (G2["stl_path"], G2["volume_mm3"], G2["size_x_mm"])
        assert json.loads(G2["checks_json"])["reused_from"] == P1["id"]
        P_1, P_2 = (H.Ctx.Db.One("SELECT * FROM price_calculations WHERE session_3d_id = ? ORDER BY created_at DESC LIMIT 1", (X,)) for X in (Rev1, Rev2))
        assert (P_1["material_id"], P_2["material_id"]) == ("silver", "vermeil")
        assert P_2["weight_g"] == round(G2["volume_mm3"] / 1000.0 * H.Ctx.Catalog.Get("vermeil").DensityGCm3, 3) and P_2["weight_g"] != P_1["weight_g"]
        assert _Line(H, F["id"])["three_d_id"] == Rev2 and (await _Stl(H, F["id"]))["state"] == "ready"
        assert _Line(H, A["id"])["three_d_id"] == Rev1                                              # the silver line keeps its own revision
        # Once corrected, nothing more to reuse for that result
        R = await H.Client.post(f"/api/admin/3d/{SF['result_id']}/reuse-bore-fix", json={}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "nothing_to_reuse"
    finally:
        await H.Close()
