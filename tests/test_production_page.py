"""The Production page (docs/order-to-production-review.md §H, the owner's decisions K9, K12): every line sent to
Production with Atelier's facts and Fulfillment's side of it from the projection — the staff-only payload kept apart
from the customer-safe one, each event filed under the parts it is about, a sent line flipped to received when its
part is reported; the list with its filters and search, grouped by order; one line's detail with its timeline."""

from tests.test_fulfillment_bridge import Admin, Prefix, Service, _Enrolled, _Event, _InBag, _On, _Order, _Placed, _Prepared, _Send


def _AdminPart(DisplayNo, LineId, Stage="received", **More):
    return {"display_no": DisplayNo, "part_id": "mp_" + DisplayNo.lower().replace("-", ""), "line_id": LineId, "stage": Stage, "stage_label": Stage.replace("_", " ").title(),
            "stage_detail": More.pop("detail", None), "build": More.pop("build", None), "holds": More.pop("holds", []), "attention": More.pop("attention", None),
            "latest_note": More.pop("note", None), "revision": {"no": 1, "approved": False, "handoff_no": 1, "version_label": "v1 · Hi3D", "file_state": "ready"},
            "quantities": {"ordered": 2, "target": 2, "in_progress": 0, "passed": 0, "rework": 0, "shipped": 0, "delivered": 0}, "updated_at": "2026-10-13T12:00:00.000+00:00", **More}


def _Full(Id, OrderId, Version, Type, Public, AdminParts, About=None, **AdminMore):
    E = _Event(Id, OrderId, Version, Type, **Public)
    E["admin_payload"] = {"parts": AdminParts, **AdminMore}
    if About is not None:
        E["about_parts"] = About
    return E


async def test_the_production_page_lists_the_lines_sent_with_fulfillment_s_side_and_files_events_per_part(tmp_path):
    H = _On(tmp_path, FulfillmentUrl="https://tron/atelier-fulfillment")
    try:
        R = (await H.Client.get("/api/admin/production", headers=Admin)).json()
        assert R["enabled"] and R["rows"] == [] and R["total"] == 0
        D1, _C1, _ = await _InBag(H, "A twisted band", Size=7, Material="silver", Qty=2)
        D2, _C2, _ = await _InBag(H, "A plain band", Size=8, Material="silver", Qty=1)
        O = (await H.Client.post("/api/orders", json=_Order(client_request_id="two"))).json()
        L1, L2 = H.Svc.Orders.AdminGet(O["id"])["lines"]
        await _Prepared(H, D1, O["id"], Size=7, LineIndex=0)
        await _Send(H, O["id"], [L1["id"]], Note="rush")
        # Sent, nothing reported yet: Atelier's facts, the stage "sent", no part number
        R = (await H.Client.get("/api/admin/production", headers=Admin)).json()
        assert R["total"] == 1 and R["rows"][0]["line_id"] == L1["id"] and R["rows"][0]["stage"] == "sent" and R["rows"][0]["part_display_no"] is None
        X = R["rows"][0]
        assert X["order"]["ref"] == "ORD-10001" and X["order"]["customer"] == "Dana Levi" and X["line"]["item_ref"] == "R-1001-A" and X["line"]["size_label"] == "US 7"
        assert X["pinned"]["version_label"] == "v1 · Hi3D" and X["note"] == "rush" and X["stage_detail"] == "awaiting synchronization" and X["fulfillment_url"] is None
        assert R["orders"][0]["lines_sent"] == 1 and R["orders"][0]["lines_total"] == 2 and R["orders"][0]["stage"] == "sent"
        # The export finishes after the hand-over: the detail records the file on the pin, as an event of the line
        await H.Idle()
        D0 = (await H.Client.get(f"/api/admin/production/{L1['id']}", headers=Admin)).json()
        assert len(D0["pinned"]["file_sha256"] or "") == 64 and ("atelier", "Production file recorded") in [(T["source"], T["label"]) for T in D0["timeline"]]
        # Fulfillment reports the part: received, with its number and id; the staff view is kept apart from the customer-safe one
        Pub = [{"display_no": "MP-10001-001", "part_id": "mp_1", "line_id": L1["id"], "customer_stage": "Under review", "status": "awaiting_review", "summary": "0/2 passed QC",
                "review_status": "not_started", "file_state": "ready", "blockers": ["REVISION_NOT_APPROVED"], "cancellation_state": "none",
                "quantities": {"target": 2, "passed": 0, "shipped": 0}, "commercial_action_pending": False}]
        E1 = _Full("evt-1", O["id"], 1, "fulfillment.request_enrolled", {"parts": Pub, "customer_status": "Under review"},
                   [_AdminPart("MP-10001-001", L1["id"], detail="under manufacturing review", part_id="mp_1")], About=["MP-10001-001"])
        assert (await H.Client.post(Prefix + "/events", json={"events": [E1]}, headers=Service)).json()["results"][0]["status"] == "applied"
        Row = H.Ctx.Db.One("SELECT * FROM fulfillment_line_production WHERE line_id = ?", (L1["id"],))
        assert Row["state"] == "received" and Row["received_at"] == E1["occurred_at"] and Row["part_id"] == "mp_1" and Row["part_display_no"] == "MP-10001-001"
        R = (await H.Client.get("/api/admin/production", headers=Admin)).json()
        X = R["rows"][0]
        assert X["stage"] == "received" and X["stage_label"] == "Received by Fulfillment" and X["stage_detail"] == "under manufacturing review" and X["part_display_no"] == "MP-10001-001"
        assert X["fulfillment_url"] == "https://tron/atelier-fulfillment/#/orders/mp_1" and X["file_state"] == "ready" and X["quantities"]["ordered"] == 2
        State = (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()
        assert State["lines"][0]["production"]["state"] == "received" and State["projection"]["admin"]["parts"][0]["stage"] == "received"
        Mine = (await H.Client.get("/api/orders")).json()["orders"][0]                              # the customer: nothing of the staff view
        assert "stage_label" not in str(Mine) and "latest_note" not in str(Mine) and "admin" not in str(Mine.get("fulfillment"))
        # Progress: a build, an attention request, a note — each event filed under its parts; the list's filters find them
        Note = {"part": "MP-10001-001", "id": "pnt_1", "text": "Surface requires additional polishing before QC.", "by": "Dana", "at": "2026-10-13T12:30:00.000+00:00"}
        Att = {"id": "atn_1", "part": "MP-10001-001", "kind": "revised_file_needed", "kind_label": "Revised STL needed", "text": "Wall thickness issue detected. Revised STL required.",
               "revision_no": 1, "hold_id": "hld_1", "state": "open", "since": "2026-10-13T13:00:00.000+00:00"}
        E2 = _Full("evt-2", O["id"], 2, "fulfillment.note_added", {"parts": Pub}, [_AdminPart("MP-10001-001", L1["id"], note=Note, part_id="mp_1")], About=["MP-10001-001"], note=Note)
        E3 = _Full("evt-3", O["id"], 3, "fulfillment.production_released", {"parts": Pub, "build": {"display_no": "B-0001", "parts": ["MP-10001-001"]}},
                   [_AdminPart("MP-10001-001", L1["id"], Stage="planned", detail="B-0001", build={"display_no": "B-0001", "state": "RELEASED"}, note=Note, part_id="mp_1")], About=["MP-10001-001"])
        E4 = _Full("evt-4", O["id"], 4, "fulfillment.attention_required", {"parts": Pub},
                   [_AdminPart("MP-10001-001", L1["id"], Stage="attention", detail="Revised STL needed: Wall thickness issue detected.", attention=Att, note=Note, part_id="mp_1",
                               build={"display_no": "B-0001", "state": "RELEASED"},
                               holds=[{"id": "hld_1", "reason": "file", "reason_label": "Production file", "scope": "part", "owner": "Operations", "since": Att["since"], "blocks_release": True, "blocks_start": True, "blocks_shipping": False}])],
                   About=["MP-10001-001"], attention=Att)
        for E in (E2, E3, E4):
            assert (await H.Client.post(Prefix + "/events", json={"events": [E]}, headers=Service)).json()["results"][0]["status"] == "applied"
        R = (await H.Client.get("/api/admin/production", headers=Admin)).json()
        X = R["rows"][0]
        assert X["stage"] == "attention" and X["attention"]["kind_label"] == "Revised STL needed" and X["build"]["display_no"] == "B-0001" and X["latest_note"]["text"].startswith("Surface")
        assert [S["id"] for S in R["stages"]] == ["attention"] and R["materials"] == [{"id": "silver", "label": "Silver"}] and R["products"] == ["ring"]
        assert (await H.Client.get("/api/admin/production?attention=1", headers=Admin)).json()["total"] == 1
        assert (await H.Client.get("/api/admin/production?stage=planned", headers=Admin)).json()["total"] == 0
        assert (await H.Client.get("/api/admin/production?build=b-0001", headers=Admin)).json()["total"] == 1
        assert (await H.Client.get("/api/admin/production?q=MP-10001", headers=Admin)).json()["total"] == 1
        assert (await H.Client.get("/api/admin/production?q=nobody", headers=Admin)).json()["total"] == 0
        assert (await H.Client.get("/api/admin/production?material=gold_18k_yellow", headers=Admin)).json()["total"] == 0
        # The detail: Atelier's pin and the choices, Fulfillment's part, the notes, the holds, the timeline in time order with its sources
        D = (await H.Client.get(f"/api/admin/production/{L1['id']}", headers=Admin)).json()
        assert D["part_display_no"] == "MP-10001-001" and D["stage"] == "attention" and D["attention"]["text"].startswith("Wall thickness") and D["holds"][0]["reason_label"] == "Production file"
        assert D["line"]["three_d_version"]["label"] == "v1 · Hi3D" and D["line"]["three_d_pinned"] and D["pinned"]["version_no"] == 1 and D["handoff_no"] == 1
        assert [N["text"] for N in D["notes"]] == ["Surface requires additional polishing before QC."] and D["order_lines_sent"] == 1 and D["order_lines_total"] == 2
        Labels = [(T["source"], T["label"]) for T in D["timeline"]]
        assert Labels[0][0] == "atelier" and Labels[0][1] in ("STL prepared", "3D version pinned") and ("atelier", "Sent to Production") in Labels
        assert Labels[-4:] == [("fulfillment", "Received by Fulfillment"), ("fulfillment", "Internal note"), ("fulfillment", "Released to a build"), ("fulfillment", "Attention required")]
        assert all(T["at"] <= U["at"] for T, U in zip(D["timeline"], D["timeline"][1:]))
        assert (await H.Client.get("/api/admin/production/nope", headers=Admin)).status_code == 404
        assert (await H.Client.get(f"/api/admin/production/{L2['id']}", headers=Admin)).status_code == 404          # not sent: not on the Production page
        # An event about another part of the order does not land in this line's timeline
        await _Prepared(H, D2, O["id"], Size=8, LineIndex=1)
        await _Send(H, O["id"], [L2["id"]])
        Pub2 = Pub + [{**Pub[0], "display_no": "MP-10001-002", "part_id": "mp_2", "line_id": L2["id"], "summary": "0/1 passed QC"}]
        E5 = _Full("evt-5", O["id"], 5, "fulfillment.review_started", {"parts": Pub2, "review": {"part": "MP-10001-002"}},
                   [_AdminPart("MP-10001-001", L1["id"], Stage="attention", attention=Att, part_id="mp_1"), _AdminPart("MP-10001-002", L2["id"], part_id="mp_2")], About=["MP-10001-002"])
        await H.Client.post(Prefix + "/events", json={"events": [E5]}, headers=Service)
        D = (await H.Client.get(f"/api/admin/production/{L1['id']}", headers=Admin)).json()
        assert ("fulfillment", "Manufacturing review started") not in [(T["source"], T["label"]) for T in D["timeline"]]
        D2_ = (await H.Client.get(f"/api/admin/production/{L2['id']}", headers=Admin)).json()
        assert D2_["stage"] == "received" and ("fulfillment", "Manufacturing review started") in [(T["source"], T["label"]) for T in D2_["timeline"]]
        R = (await H.Client.get("/api/admin/production", headers=Admin)).json()
        assert R["total"] == 2 and R["orders"][0]["lines_sent"] == 2 and R["orders"][0]["stage"] == "received"       # the least advanced line gives the order's stage
    finally:
        await H.Close()


async def test_a_revised_file_after_the_hand_over_is_explicit_with_a_reason_and_pins_the_next_version(tmp_path):
    """§B.6, §G.4: a line in Production keeps its pin; the plain version change is refused; the revision workflow re-pins
    with a reason, advances the hand-over number, records the new file and tells Fulfillment (production_revision)."""
    from tests.test_fulfillment_bridge import _Enrolled
    H = _On(tmp_path)
    try:
        Did, O = await _Placed(H, Qty=1)
        await _Enrolled(H, Did, O)
        await H.Idle()
        LineId = H.Svc.Orders.AdminGet(O["id"])["lines"][0]["id"]
        Before = (await H.Client.get(f"/api/admin/production/{LineId}", headers=Admin)).json()
        assert Before["handoff_no"] == 1 and Before["pinned"]["version_no"] == 1 and len(Before["pinned"]["file_sha256"] or "") == 64
        # A second 3D version of the option: an STL uploaded as a revision of the first version (the external-parts flow of
        # main) — a thicker band than the mock's model, so its print file differs
        import trimesh
        M = H.Ctx.Db.One("SELECT * FROM meshes WHERE status = 'ready' ORDER BY created_at LIMIT 1")
        Data = trimesh.creation.torus(major_radius=9.0, minor_radius=2.0).export(file_type="stl")
        Up = (await H.Client.post("/api/admin/uploads", json={"filename": "revised.stl", "bytes": len(Data)}, headers=Admin)).json()
        assert (await H.Client.put(f"/api/admin/uploads/{Up['id']}?offset=0", content=Data, headers=Admin)).status_code == 200
        assert (await H.Client.post(f"/api/admin/uploads/{Up['id']}/complete", headers=Admin)).status_code == 200
        R = await H.Client.post(f"/api/admin/designs/{Did}/versions", json={"upload_id": Up["id"], "candidate_id": M["candidate_id"], "scaling": "scalable", "source_tool": "Rhino", "note": "thicker"}, headers=Admin)
        assert R.status_code == 200, R.text
        await H.Idle()
        Line = next(X for X in H.Svc.Orders.AdminGet(O["id"])["lines"] if X["id"] == LineId)
        Other = next((V for V in Line["three_d_versions"] if V["mesh_id"] != Line["three_d_version"]["mesh_id"]), None)
        assert Other, Line["three_d_versions"]
        # The order page's plain "Use another version" is refused for a line in Production
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{LineId}/3d-version", json={"mesh_id": Other["mesh_id"], "note": "x"}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "line_in_production"
        Url = f"/api/admin/orders/{O['id']}/lines/{LineId}/production-revision"
        assert (await H.Client.post(Url, json={"mesh_id": Other["mesh_id"]}, headers=Admin)).json()["error"]["code"] == "reason_required"
        assert (await H.Client.post(Url, json={"reason": "thicker"}, headers=Admin)).json()["error"]["code"] == "version_required"
        R = await H.Client.post(Url, json={"mesh_id": Other["mesh_id"], "reason": "wall thickened to 1 mm as asked", "answers_request_id": "atn_1"}, headers=Admin)
        assert R.status_code == 200, R.text
        await H.Idle()
        D = (await H.Client.get(f"/api/admin/production/{LineId}", headers=Admin)).json()
        assert D["handoff_no"] == 2 and D["pinned"]["mesh_id"] == Other["mesh_id"] and D["pinned"]["version_no"] == Other["version_no"] and D["revision_reason"] == "wall thickened to 1 mm as asked"
        assert D["answers_request_id"] == "atn_1" and D["state"] == "revised" and D["line"]["three_d_version"]["mesh_id"] == Other["mesh_id"]
        assert len(D["pinned"]["file_sha256"] or "") == 64 and D["pinned"]["file_sha256"] != Before["pinned"]["file_sha256"]
        Atelier = [T["label"] for T in D["timeline"] if T["source"] == "atelier"]
        assert Atelier[-2:] == ["Revised file submitted to Production", "Production file recorded"] and Atelier.count("Production file recorded") == 2
        C = (await H.Client.get(Prefix + "/changes?cursor=0", headers=Service)).json()["changes"]
        assert "production_revision" in [X["reason"] for X in C]
        S = (await H.Client.get(Prefix + f"/requests/order/{O['id']}", headers=Service)).json()["lines"][0]
        assert S["production"]["handoff_no"] == 2 and S["production"]["pinned"]["mesh_id"] == Other["mesh_id"] and S["production"]["revision_reason"] == "wall thickened to 1 mm as asked"
        assert S["production"]["answers_request_id"] == "atn_1" and S["model"]["mesh_id"] == Other["mesh_id"]
        # The same version again: nothing changed; the file route serves the new pin only
        assert (await H.Client.post(Url, json={"mesh_id": Other["mesh_id"], "reason": "again"}, headers=Admin)).json()["error"]["code"] == "nothing_changed"
        R = (await H.Client.post(Prefix + "/assets/production-stl", json={"line_id": LineId}, headers=Service)).json()
        assert R["status"] == "done" and R["pinned_sha256"] == D["pinned"]["file_sha256"] and R["handoff_no"] == 2
        assert (await H.Client.get(Prefix + f"/assets/production-stl/{Before['pinned']['three_d_id']}/stored", headers=Service)).status_code == 404   # the former result is no longer served
    finally:
        await H.Close()


async def test_off_the_production_page_says_so(tmp_path):
    from tests.conftest import Harness
    from tests.test_fulfillment_bridge import AdminKey
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        _Did, _O = await _Placed(H)
        R = (await H.Client.get("/api/admin/production", headers=Admin)).json()
        assert R == {"enabled": False, "rows": [], "orders": [], "total": 0, "offset": 0, "limit": 50, "stages": [], "materials": [], "products": []}
        assert (await H.Client.get("/api/admin/production/x", headers=Admin)).status_code == 409
    finally:
        await H.Close()
