"""The manual FixBore on the order side (the owner's decision of 2026-10-15): an accepted bore correction is a new
revision of the result it corrects, and it never replaces a line's STL by itself. A line linked to the original is
offered the revision and switched only by the Admin's explicit action, recorded in the order history; a line already in
Production is never switched (its file changes only through a revised file, with a reason); a line not yet linked
chooses between the original and the revision explicitly, never silently. What reaches Fulfillment is the very file
reviewed — the hash recorded when it was proposed."""

import pytest

from tests.test_fulfillment_bridge import Admin, _On, _Placed, _Prepared, _Send
from tests.test_send_to_production_rules import _InBag, _Place, _State


def _Oval(Format):
    import trimesh
    M = trimesh.creation.torus(major_radius=9.0, minor_radius=1.5, major_sections=192, minor_sections=64)
    M.apply_scale([1.15, 1.0, 1.0])
    return M.export(file_type=Format)


async def _Propose(H, Sid: str) -> dict:
    assert (await H.Client.post(f"/api/admin/3d/{Sid}/fix-bore", headers=Admin)).status_code == 200
    await H.Idle()
    X = H.Ctx.Db.One("SELECT id, status, error FROM bore_fixes WHERE session_3d_id = ? ORDER BY created_at DESC LIMIT 1", (Sid,))
    assert X["status"] == "proposed", X
    return X


def _Line(H, OrderId: str) -> dict:
    return H.Svc.Orders.AdminGet(OrderId)["lines"][0]


async def test_an_accepted_correction_reaches_a_line_only_by_the_admin_s_choice(tmp_path, monkeypatch):
    from p3.providers import mock
    monkeypatch.setattr(mock, "_MeshBytes", _Oval)
    H = _On(tmp_path)
    try:
        # A: its line linked to the flagged result, accepted as measured, sent to Production — before any correction
        Did, A = await _Placed(H, Qty=1, Click="bf-a")
        LA = await _Prepared(H, Did, A["id"])
        Orig = LA["three_d_id"]
        assert LA["three_d_state"] == "review_required"
        assert (await H.Client.post(f"/api/admin/3d/{Orig}/accept", json={"note": "sent as measured"}, headers=Admin)).status_code == 200
        await _Send(H, A["id"])
        # The correction, proposed and accepted: a revision of its own; A's line keeps its STL
        Pid = (await _Propose(H, Orig))["id"]
        R = await H.Client.post(f"/api/admin/bore-fixes/{Pid}/accept", json={"note": "round US 7"}, headers=Admin)
        assert R.status_code == 200, R.text
        Rev = R.json()["revision"]
        Sha = H.Ctx.Db.One("SELECT file_sha256 FROM bore_fixes WHERE id = ?", (Pid,))["file_sha256"]
        LA = _Line(H, A["id"])
        assert LA["three_d_id"] == Orig and LA["bore_correction"]["revision_id"] == Rev["id"]
        # … and a line in Production is never switched (only a revised file, with a reason, from the Production page)
        R = await H.Client.post(f"/api/admin/orders/{A['id']}/lines/{LA['id']}/use-bore-correction", json={"revision_id": Rev["id"]}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "line_in_production", R.text
        assert _Line(H, A["id"])["three_d_id"] == Orig
        # B: the same piece, linked to the original by the Admin's choice — offered the revision, switched only explicitly
        Cand = LA["candidate_id"]
        await _InBag(H, Did, Cand, "silver", Size=7)
        B = await _Place(H, "bf-b")
        LB = _Line(H, B["id"])
        R = await H.Client.post(f"/api/admin/orders/{B['id']}/lines/{LB['id']}/link-stl", json={"three_d_id": Orig}, headers=Admin)
        assert R.status_code == 200, R.text
        LB = _Line(H, B["id"])
        assert LB["three_d_id"] == Orig and LB["bore_correction"]["revision_id"] == Rev["id"]
        S = await _State(H, B["id"])
        assert S["lines"][0]["stl"]["bore_correction"]["revision_id"] == Rev["id"]                # named on the hand-over's verdict too
        R = await H.Client.post(f"/api/admin/orders/{B['id']}/lines/{LB['id']}/use-bore-correction", json={"revision_id": "s3d_other"}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "not_this_line_s_correction"
        R = await H.Client.post(f"/api/admin/orders/{B['id']}/lines/{LB['id']}/use-bore-correction", json={"revision_id": Rev["id"]}, headers=Admin)
        assert R.status_code == 200, R.text
        LB = _Line(H, B["id"])
        assert LB["three_d_id"] == Rev["id"] and LB["bore_correction"] is None and LB["three_d_state"] == "complete"
        Ev = [E for E in H.Svc.Orders.AdminGet(B["id"])["events"] if E["kind"] == "3d_pinned"][-1]
        assert (Ev["data"]["linked"], Ev["data"]["replaces"], Ev["data"]["bore_fix_id"]) == ("bore_corrected", Orig, Pid)
        # Sent: what reaches Fulfillment is the very file reviewed (the duplicate of A confirmed, G9)
        R = await H.Client.post(f"/api/admin/orders/{B['id']}/production", json={"line_ids": [LB["id"]], "acknowledged_duplicates": {LB["id"]: [A["id"]]}},
                                headers=Admin)
        assert R.status_code == 200, R.text
        Row = H.Ctx.Db.One("SELECT three_d_id, file_sha256 FROM fulfillment_line_production WHERE line_id = ?", (LB["id"],))
        assert (Row["three_d_id"], Row["file_sha256"]) == (Rev["id"], Sha)
        # C: not linked yet — the original (accepted) and the revision are two different files: the Admin chooses, never silently
        await _InBag(H, Did, Cand, "silver", Size=7)
        C = await _Place(H, "bf-c")
        LC = (await _State(H, C["id"]))["lines"][0]
        assert LC["stl"]["state"] == "ambiguous" and {X["id"] for X in LC["stl"]["choices"]} == {Orig, Rev["id"]}
        assert next(X for X in LC["stl"]["choices"] if X["id"] == Rev["id"])["bore_corrected"] is True
        R = await H.Client.post(f"/api/admin/orders/{C['id']}/lines/{LC['line_id']}/3d", headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "stl_ambiguous"
        assert _Line(H, C["id"])["three_d_pinned"] is False or _Line(H, C["id"])["three_d_id"] != Rev["id"]
    finally:
        await H.Close()


async def test_an_open_or_accepted_correction_is_never_proposed_twice_and_a_revision_is_never_corrected_again(tmp_path, monkeypatch):
    from p3.providers import mock
    monkeypatch.setattr(mock, "_MeshBytes", _Oval)
    H = _On(tmp_path)
    try:
        Did, O = await _Placed(H, Qty=1, Click="bf-d")
        Orig = (await _Prepared(H, Did, O["id"]))["three_d_id"]
        assert (await H.Client.post(f"/api/admin/3d/{Orig}/fix-bore", headers=Admin)).status_code == 200
        R = await H.Client.post(f"/api/admin/3d/{Orig}/fix-bore", headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] in ("proposal_open", "busy")
        await H.Idle()
        Pid = H.Ctx.Db.One("SELECT id FROM bore_fixes WHERE session_3d_id = ?", (Orig,))["id"]
        assert (await H.Client.post(f"/api/admin/3d/{Orig}/fix-bore", headers=Admin)).json()["error"]["code"] == "proposal_open"
        Rev = (await H.Client.post(f"/api/admin/bore-fixes/{Pid}/accept", json={}, headers=Admin)).json()["revision"]
        assert (await H.Client.post(f"/api/admin/3d/{Orig}/fix-bore", headers=Admin)).json()["error"]["code"] == "already_corrected"
        assert (await H.Client.post(f"/api/admin/3d/{Rev['id']}/fix-bore", headers=Admin)).json()["error"]["code"] == "already_a_revision"
        assert (await H.Client.post(f"/api/admin/bore-fixes/{Pid}/reject", json={}, headers=Admin)).json()["error"]["code"] == "not_proposed"
        assert (await H.Client.post("/api/admin/bore-fixes/bfx_nope/accept", json={}, headers=Admin)).status_code == 404
        # The line linked to the original keeps it: nothing happened to it by itself
        assert _Line(H, O["id"])["three_d_id"] == Orig
    finally:
        await H.Close()
