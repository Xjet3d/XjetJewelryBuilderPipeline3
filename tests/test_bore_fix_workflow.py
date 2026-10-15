"""The FixBore workflow on the order side (the owner's decisions of 2026-10-15): accepting a bore correction activates
the corrected revision at once — no separate switch — for every order line of the same design, material and size that
may change, and the Admin sees the complete list before deciding. A line not yet with Fulfillment is switched (or
linked) at once; a line whose part is in Fulfillment's review is asked of Fulfillment (replace_file) and switched only
once Fulfillment confirms; an approved, production-assigned, cancelled or legacy-production line is never changed by
itself; a line chosen independently is left alone. Every activation is recorded with each line's outcome; Restore makes
the other file active again, with a reason, under the same rules; nothing is deleted or overwritten. An update across
Atelier and Fulfillment that cannot complete leaves nothing half done: a refusal keeps the line's file, a failed commit
is rolled back and applied when the answer comes again."""

import hashlib

from p3.context import HttpError
from tests.test_fulfillment_bridge import Admin, Prefix, Service, _Event, _On, _Placed, _Prepared, _Send
from tests.test_send_to_production_rules import _InBag, _Place, _State


def _Oval(Format):
    import trimesh
    M = trimesh.creation.torus(major_radius=9.0, minor_radius=1.5, major_sections=192, minor_sections=64)
    M.apply_scale([1.15, 1.0, 1.0])
    return M.export(file_type=Format)


async def _Propose(H, Sid: str) -> dict:
    assert (await H.Client.post(f"/api/admin/3d/{Sid}/fix-bore", headers=Admin)).status_code == 200
    await H.Idle()
    X = H.Ctx.Db.One("SELECT * FROM bore_fixes WHERE session_3d_id = ? ORDER BY created_at DESC LIMIT 1", (Sid,))
    assert X["status"] == "proposed", X
    return X


def _Line(H, OrderId: str) -> dict:
    return H.Svc.Orders.AdminGet(OrderId)["lines"][0]


def _Result(H, Sid: str) -> dict:
    return H.Svc.Production3D.Get(Sid)


async def _Another(H, Did: str, Cand: str, Click: str, Size=7, Material="silver", Link: str | None = None) -> dict:
    """One more order of the same piece, by the same customer (as ORD-10005 re-ordered ORD-10003's ring)."""
    await _InBag(H, Did, Cand, Material, Size=Size)
    O = await _Place(H, Click)
    if Link:
        L = _Line(H, O["id"])
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{L['id']}/link-stl", json={"three_d_id": Link}, headers=Admin)
        assert R.status_code == 200, R.text
    return O


async def _SendAcknowledged(H, OrderId: str) -> None:
    """Send an order's line, confirming the customer's other orders of the same piece (G9)."""
    L = (await _State(H, OrderId))["lines"][0]
    R = await H.Client.post(f"/api/admin/orders/{OrderId}/production", headers=Admin,
                            json={"line_ids": [L["line_id"]], "acknowledged_duplicates": {L["line_id"]: [D["order_id"] for D in L.get("duplicates") or []]}})
    assert R.status_code == 200, R.text


async def _Report(H, OrderId: str, No: int, Stage: str, Review: str = "not_started", Build=None, Version: int = 1) -> None:
    """Fulfillment reports the line's part: its stage (staff view) and review status (customer-safe view)."""
    L = _Line(H, OrderId)
    Display = f"MP-{H.Ctx.Db.One('SELECT order_no FROM orders WHERE id = ?', (OrderId,))['order_no']}-001"
    E = _Event(f"evt-{OrderId}-{Version}", OrderId, Version, "fulfillment.request_updated",
               parts=[{"display_no": Display, "part_id": f"mp_{No}", "line_id": L["id"], "review_status": Review, "status": "awaiting_review"}])
    E["aggregate_id"] = f"src_{OrderId}"
    E["admin_payload"] = {"parts": [{"display_no": Display, "part_id": f"mp_{No}", "line_id": L["id"], "stage": Stage, "build": Build}]}
    assert (await H.Client.post(Prefix + "/events", json={"events": [E]}, headers=Service)).json()["results"][0]["status"] == "applied"


def _Answer(OrderId: str, Cid: str, Applied: bool = True, Version: int = 50, EventId: str | None = None, **Result) -> dict:
    """Fulfillment's answer to a command, as its outbox sends it."""
    Cmd = {"command_id": Cid, "action": "replace_file", "part": "MP-10001-001", **Result}
    E = _Event(EventId or f"evt-answer-{Cid}", OrderId, Version, "fulfillment.command_applied" if Applied else "fulfillment.command_rejected", command=Cmd)
    E["aggregate_id"] = f"src_{OrderId}"
    return E


def _Pending(H, OrderId: str) -> dict:
    R = H.Ctx.Db.One("SELECT * FROM fulfillment_commands WHERE entity_id = ? AND action = 'replace_file' ORDER BY created_at DESC LIMIT 1", (OrderId,))
    assert R is not None
    return R


async def test_accepting_activates_the_corrected_stl_for_every_eligible_line_and_fulfillment_confirms_the_line_in_review(tmp_path, monkeypatch):
    from p3.providers import mock
    monkeypatch.setattr(mock, "_MeshBytes", _Oval)
    H = _On(tmp_path)
    try:
        # A: linked to the flagged result, accepted as measured, sent to Production; Fulfillment has it in review
        Did, A = await _Placed(H, Qty=1, Click="bf-a")
        LA = await _Prepared(H, Did, A["id"])
        Orig, Cand = LA["three_d_id"], LA["candidate_id"]
        assert (await H.Client.post(f"/api/admin/3d/{Orig}/accept", json={"note": "sent as measured"}, headers=Admin)).status_code == 200
        await _Send(H, A["id"])
        await _Report(H, A["id"], 1, "received")
        # B linked to the same result · C not linked yet · E linked, then cancelled · D another size · F another material (vermeil)
        B = await _Another(H, Did, Cand, "bf-b", Link=Orig)
        C = await _Another(H, Did, Cand, "bf-c")
        E = await _Another(H, Did, Cand, "bf-e", Link=Orig)
        H.Svc.Orders.SetStatus(E["id"], "cancelled", "developer-key", Note="the customer changed their mind")
        D = await _Another(H, Did, Cand, "bf-d", Size=8)
        F = await _Another(H, Did, Cand, "bf-f", Material="vermeil")
        assert (await _State(H, C["id"]))["lines"][0]["stl"]["state"] == "ready_unlinked"
        Before = {"row": H.Ctx.Db.One("SELECT * FROM session_3d WHERE id = ?", (Orig,)),
                  "geo": H.Ctx.Db.All("SELECT * FROM geometry_results WHERE session_3d_id = ? ORDER BY id", (Orig,))}
        # The correction proposed: before accepting, the complete list of the lines it changes, with their status
        Pid = (await _Propose(H, Orig))["id"]
        Fix = _Result(H, Orig)["bore_fix"]
        Plan = {X["order_id"]: X for X in Fix["affected"]}
        assert set(Plan) == {A["id"], B["id"], C["id"], E["id"]}                              # D (US 8) and F (vermeil) are not of this size and material
        assert (Plan[A["id"]]["action"], Plan[A["id"]]["transferred"], Plan[A["id"]]["stage"]) == ("request", True, "received")
        assert (Plan[B["id"]]["action"], Plan[B["id"]]["order_status_label"]) == ("switch", "New")
        assert Plan[C["id"]]["action"] == "link" and Plan[E["id"]]["action"] == "skip" and Plan[E["id"]]["reason"] == "order cancelled"
        # Accepted: active at once — no separate switch
        R = await H.Client.post(f"/api/admin/bore-fixes/{Pid}/accept", json={"note": "round US 7"}, headers=Admin)
        assert R.status_code == 200, R.text
        Rev, Act = R.json()["revision"]["id"], R.json()["activation"]
        assert Act["counts"] == {"switched": 1, "linked": 1, "pending": 1, "skipped": 1} and Act["kind"] == "accept" and Act["by"] == "developer-key"
        assert _Line(H, B["id"])["three_d_id"] == Rev and _Line(H, C["id"])["three_d_id"] == Rev
        assert _Line(H, E["id"])["three_d_id"] == Orig and _Line(H, D["id"])["three_d_id"] != Rev and _Line(H, F["id"])["three_d_id"] != Rev
        assert (await _State(H, B["id"]))["lines"][0]["stl"]["state"] == "ready"
        assert _Line(H, B["id"])["stl_activation"]["outcome"] == "switched" and _Line(H, C["id"])["stl_activation"]["outcome"] == "linked"
        # The line with Fulfillment waits for its answer: unchanged here, the request in the feed with the exact file it names
        Sha = H.Ctx.Db.One("SELECT file_sha256 FROM bore_fixes WHERE id = ?", (Pid,))["file_sha256"]
        assert _Line(H, A["id"])["three_d_id"] == Orig and _Line(H, A["id"])["stl_activation"]["outcome"] == "pending"
        Prod = H.Ctx.Db.One("SELECT * FROM fulfillment_line_production WHERE line_id = ?", (LA["id"],))
        assert (Prod["three_d_id"], Prod["handoff_no"]) == (Orig, 1)
        Cmd = _Pending(H, A["id"])
        P = (await H.Client.get(Prefix + "/changes?cursor=0&limit=100", headers=Service)).json()["changes"]
        Feed = next(X for X in P if X["kind"] == "command" and X["payload"]["command_id"] == Cmd["id"])["payload"]
        assert (Feed["action"], Feed["delegated_permission"], Feed["expected_version"], Feed["handoff_no"], Feed["three_d_id"]) == ("replace_file", "review_part", None, 2, Rev)
        assert Feed["pinned"]["geometry_sha256"] == Sha and Feed["pinned"]["file_sha256"] == Sha and Feed["pinned"]["three_d_id"] == Rev
        assert Feed["model"]["session_3d_id"] == Rev and Feed["model"]["available"] is True and Feed["from_three_d_id"] == Orig
        # Nothing was overwritten: the original result, its numbers and files are as they were; the revision's file is stored
        assert H.Ctx.Db.One("SELECT * FROM session_3d WHERE id = ?", (Orig,)) == Before["row"]
        assert H.Ctx.Db.All("SELECT * FROM geometry_results WHERE session_3d_id = ? ORDER BY id", (Orig,)) == Before["geo"]
        Stored = H.Ctx.Settings.DevDir / H.Ctx.Db.One("SELECT file_path FROM bore_fixes WHERE id = ?", (Pid,))["file_path"]
        assert hashlib.sha256(Stored.read_bytes()).hexdigest() == Sha
        # Recorded: the activation with each line, the order histories, the result page's view
        Page = _Result(H, Orig)["bore_fix"]
        assert Page["activated"] and Page["active_three_d_id"] == Rev and Page["pending"] == 1 and Page["restore_to"] == Orig
        assert {X["order_id"]: X["outcome"] for X in Page["activations"][0]["lines"]} == {A["id"]: "pending", B["id"]: "switched", C["id"]: "linked", E["id"]: "skipped"}
        assert [Ev["data"]["outcome"] for Ev in H.Svc.Orders.AdminGet(B["id"])["events"] if Ev["kind"] == "stl_activated"] == ["switched"]
        assert _Result(H, Rev)["stl_pair"]["this_active"] is True and _Result(H, Orig)["stl_pair"]["this_active"] is False
        # A new order of the piece, not linked yet: its candidate is the active file only
        G = await _Another(H, Did, Cand, "bf-g")
        S = (await _State(H, G["id"]))["lines"][0]["stl"]
        assert S["state"] == "ready_unlinked" and S["link"]["id"] == Rev
        # … the other file stays a valid choice the Admin may make explicitly for one line (an independent selection)
        LG = _Line(H, G["id"])
        R = await H.Client.post(f"/api/admin/orders/{G['id']}/lines/{LG['id']}/link-stl", json={"three_d_id": Orig}, headers=Admin)
        assert R.status_code == 200, R.text
        assert _Line(H, G["id"])["three_d_id"] == Orig
        # Fulfillment confirms (its part was in review): the line, its hand-over and the snapshot take the file — once
        Ans = _Answer(A["id"], Cmd["id"], result="applied", revision_no=2, replaces=1, feasibility_needs_review=True)
        assert (await H.Client.post(Prefix + "/events", json={"events": [Ans]}, headers=Service)).json()["results"][0]["status"] == "applied"
        Prod = H.Ctx.Db.One("SELECT * FROM fulfillment_line_production WHERE line_id = ?", (LA["id"],))
        assert (Prod["three_d_id"], Prod["handoff_no"], Prod["file_sha256"], Prod["state"]) == (Rev, 2, Sha, "received")
        assert _Line(H, A["id"])["three_d_id"] == Rev and _Line(H, A["id"])["stl_activation"]["outcome"] == "applied"
        assert _Line(H, A["id"])["stl_activation"]["revision_no"] == 2
        Ev = [X for X in H.Svc.Orders.AdminGet(A["id"])["events"] if X["kind"] == "production_file_replaced"]
        assert len(Ev) == 1 and Ev[0]["data"]["revision_no"] == 2 and Ev[0]["data"]["feasibility_needs_review"] is True and Ev[0]["by"] == "fulfillment"
        Snap = (await H.Client.get(Prefix + f"/requests/order/{A['id']}", headers=Service)).json()["lines"][0]
        assert Snap["production"]["handoff_no"] == 2 and Snap["production"]["pinned"]["geometry_sha256"] == Sha and Snap["production"]["pinned"]["three_d_id"] == Rev
        assert Snap["model"]["session_3d_id"] == Rev and Snap["model"] == Feed["model"]                    # the revision Fulfillment made is the one the snapshot names
        File = (await H.Client.post(Prefix + "/assets/production-stl", json={"line_id": LA["id"]}, headers=Service)).json()
        assert (File["status"], File["sha256"], File["pinned_sha256"], File["session_3d_id"]) == ("done", Sha, Sha, Rev)
        # The same answer again: nothing more
        assert (await H.Client.post(Prefix + "/events", json={"events": [Ans]}, headers=Service)).json()["results"][0]["status"] == "duplicate"
        assert H.Ctx.Db.One("SELECT handoff_no FROM fulfillment_line_production WHERE line_id = ?", (LA["id"],))["handoff_no"] == 2
        # A line never sent keeps the master's identity in the snapshot (nothing changes for the parts made before)
        Other = (await H.Client.get(Prefix + f"/requests/order/{A['id']}", headers=Service)).json()
        assert Other["lines"][0]["production"]["pinned"]["master_sha256"] != Sha
    finally:
        await H.Close()


async def test_approved_production_assigned_cancelled_and_legacy_lines_are_never_changed_and_a_refusal_keeps_the_file(tmp_path, monkeypatch):
    from p3.providers import mock
    monkeypatch.setattr(mock, "_MeshBytes", _Oval)
    H = _On(tmp_path)
    try:
        Did, A = await _Placed(H, Qty=1, Click="bf-p1")
        LA = await _Prepared(H, Did, A["id"])
        Orig, Cand = LA["three_d_id"], LA["candidate_id"]
        assert (await H.Client.post(f"/api/admin/3d/{Orig}/accept", json={}, headers=Admin)).status_code == 200
        await _Send(H, A["id"])
        await _Report(H, A["id"], 1, "approved", Review="approved")                       # approved for manufacturing
        Bo = await _Another(H, Did, Cand, "bf-p2", Link=Orig)
        await _SendAcknowledged(H, Bo["id"])
        await _Report(H, Bo["id"], 2, "planned", Review="approved", Build={"display_no": "B-0001", "state": "DRAFT"})   # in a draft build
        Rv = await _Another(H, Did, Cand, "bf-p3", Link=Orig)
        await _SendAcknowledged(H, Rv["id"])
        await _Report(H, Rv["id"], 3, "received")                                           # in review
        Lg = await _Another(H, Did, Cand, "bf-p4", Link=Orig)
        H.Svc.Orders.SetStatus(Lg["id"], "production", "developer-key")                     # the existing production flow, never handed over
        Pid = (await _Propose(H, Orig))["id"]
        Plan = {X["order_id"]: X for X in _Result(H, Orig)["bore_fix"]["affected"]}
        assert Plan[A["id"]]["action"] == "skip" and Plan[A["id"]]["reason"].startswith("approved for manufacturing")
        assert Plan[Bo["id"]]["action"] == "skip" and Plan[Bo["id"]]["reason"].startswith("assigned to a build")
        assert Plan[Lg["id"]]["action"] == "skip" and Plan[Lg["id"]]["reason"].startswith("in the existing production flow")
        assert Plan[Rv["id"]]["action"] == "request"
        R = await H.Client.post(f"/api/admin/bore-fixes/{Pid}/accept", json={}, headers=Admin)
        assert R.status_code == 200, R.text
        Rev = R.json()["revision"]["id"]
        assert R.json()["activation"]["counts"] == {"pending": 1, "skipped": 3}
        for O in (A, Bo, Lg, Rv):
            assert _Line(H, O["id"])["three_d_id"] == Orig
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_commands WHERE action = 'replace_file'")["n"] == 1     # only the line in review is asked
        # While Fulfillment has not answered: no restore, no revised file for that line
        R = await H.Client.post(f"/api/admin/bore-fixes/{Pid}/restore", json={"note": "the customer prefers the original"}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "activation_pending"
        LR = _Line(H, Rv["id"])
        R = await H.Client.post(f"/api/admin/orders/{Rv['id']}/lines/{LR['id']}/production-revision", json={"reason": "x", "mesh_id": LR["three_d_version"]["mesh_id"]},
                                headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "stl_change_pending"
        # Fulfillment refuses (the part was approved meanwhile): the line keeps its file, the reason is kept
        Cmd = _Pending(H, Rv["id"])
        Ans = _Answer(Rv["id"], Cmd["id"], Applied=False, result="rejected", code="approved_for_manufacturing", reason="MP-10003-001 is approved for manufacturing")
        assert (await H.Client.post(Prefix + "/events", json={"events": [Ans]}, headers=Service)).json()["results"][0]["status"] == "applied"
        N = _Line(H, Rv["id"])["stl_activation"]
        assert _Line(H, Rv["id"])["three_d_id"] == Orig and N["outcome"] == "refused" and "approved for manufacturing" in N["reason"]
        assert H.Ctx.Db.One("SELECT three_d_id, handoff_no FROM fulfillment_line_production WHERE line_id = ?", (LR["id"],)) == {"three_d_id": Orig, "handoff_no": 1}
        # Restore: a reason is required; the original becomes active again (nothing was switched, so nothing changes back)
        R = await H.Client.post(f"/api/admin/bore-fixes/{Pid}/restore", json={"note": ""}, headers=Admin)
        assert R.status_code == 400 and R.json()["error"]["code"] == "reason_required"
        R = await H.Client.post(f"/api/admin/bore-fixes/{Pid}/restore", json={"note": "the customer prefers the original"}, headers=Admin)
        assert R.status_code == 200, R.text
        assert R.json()["activation"]["kind"] == "restore" and R.json()["activation"]["to_three_d_id"] == Orig and _Result(H, Orig)["stl_pair"]["this_active"] is True
        assert all(L["outcome"] == "skipped" for L in R.json()["activation"]["lines"])
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM bore_fixes WHERE revision_id = ?", (Rev,))["n"] == 1 and _Result(H, Rev)["id"] == Rev   # both files stay
    finally:
        await H.Close()


async def test_a_rejected_correction_changes_nothing_and_a_restore_switches_the_lines_back(tmp_path, monkeypatch):
    from p3.providers import mock
    monkeypatch.setattr(mock, "_MeshBytes", _Oval)
    H = _On(tmp_path)
    try:
        Did, A = await _Placed(H, Qty=1, Click="bf-r1")
        Orig = (await _Prepared(H, Did, A["id"]))["three_d_id"]
        # Rejected: the file deleted, the decision recorded, every line as it was, the original still the line's STL
        F = await _Propose(H, Orig)
        Path_ = H.Ctx.Settings.DevDir / F["file_path"]
        assert Path_.is_file()
        R = await H.Client.post(f"/api/admin/bore-fixes/{F['id']}/reject", json={"note": "keep the design as generated"}, headers=Admin)
        assert R.status_code == 200 and R.json()["bore_fix"]["status"] == "rejected"
        assert not Path_.exists() and _Line(H, A["id"])["three_d_id"] == Orig
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM stl_activations")["n"] == 0 and _Result(H, Orig)["stl_pair"] is None
        # Proposed again and accepted: the line switches; restored with a reason: it switches back; again: the correction once more
        Pid = (await _Propose(H, Orig))["id"]
        Rev = (await H.Client.post(f"/api/admin/bore-fixes/{Pid}/accept", json={}, headers=Admin)).json()["revision"]["id"]
        assert _Line(H, A["id"])["three_d_id"] == Rev
        R = await H.Client.post(f"/api/admin/bore-fixes/{Pid}/restore", json={"note": "the customer prefers the original"}, headers=Admin)
        assert R.status_code == 200 and R.json()["activation"]["counts"] == {"switched": 1}
        assert _Line(H, A["id"])["three_d_id"] == Orig and _Result(H, Orig)["bore_fix"]["restore_to"] == Rev
        R = await H.Client.post(f"/api/admin/bore-fixes/{Pid}/restore", json={"note": "rounder after all"}, headers=Admin)
        assert R.status_code == 200 and _Line(H, A["id"])["three_d_id"] == Rev
        Hist = _Result(H, Orig)["bore_fix"]["activations"]
        assert [X["kind"] for X in Hist] == ["restore", "restore", "accept"] and Hist[1]["note"] == "the customer prefers the original"
        Kinds = [E["data"]["kind"] for E in H.Svc.Orders.AdminGet(A["id"])["events"] if E["kind"] == "stl_activated"]
        assert Kinds == ["accept", "restore", "restore"]
        Journey = [E["kind"] for E in H.Ctx.Db.All("SELECT kind FROM session_events WHERE kind LIKE 'admin_3d_%' ORDER BY id")]
        assert Journey.count("admin_3d_stl_activated") == 3 and "admin_3d_bore_fix_rejected" in Journey
    finally:
        await H.Close()


async def test_an_update_that_cannot_complete_is_completed_later_and_never_half_done(tmp_path, monkeypatch):
    from p3.providers import mock
    monkeypatch.setattr(mock, "_MeshBytes", _Oval)
    H = _On(tmp_path)
    try:
        Did, A = await _Placed(H, Qty=1, Click="bf-f1")
        LA = await _Prepared(H, Did, A["id"])
        Orig, Cand = LA["three_d_id"], LA["candidate_id"]
        assert (await H.Client.post(f"/api/admin/3d/{Orig}/accept", json={}, headers=Admin)).status_code == 200
        await _Send(H, A["id"])
        await _Report(H, A["id"], 1, "received")
        B = await _Another(H, Did, Cand, "bf-f2", Link=Orig)
        Pid = (await _Propose(H, Orig))["id"]
        # The activation fails right after the revision is made: accepted, not activated — no line changed; accepting again completes it
        Real, Calls = H.Svc.Activations.Activate, []

        def Fails(*A_, **K):
            Calls.append(1)
            if len(Calls) == 1:
                raise HttpError(503, "test_failure", "the database was busy")
            return Real(*A_, **K)
        monkeypatch.setattr(H.Svc.Activations, "Activate", Fails)
        R = await H.Client.post(f"/api/admin/bore-fixes/{Pid}/accept", json={"note": "round"}, headers=Admin)
        assert R.status_code == 503
        Fix = _Result(H, Orig)["bore_fix"]
        assert Fix["status"] == "accepted" and Fix["activated"] is False and _Line(H, B["id"])["three_d_id"] == Orig
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_commands WHERE action = 'replace_file'")["n"] == 0
        R = await H.Client.post(f"/api/admin/bore-fixes/{Pid}/accept", json={}, headers=Admin)
        assert R.status_code == 200, R.text
        Rev = R.json()["revision"]["id"]
        assert R.json()["activation"]["note"] == "round" and _Line(H, B["id"])["three_d_id"] == Rev
        R2 = await H.Client.post(f"/api/admin/bore-fixes/{Pid}/accept", json={}, headers=Admin)          # a repeat: decided already, activated once
        assert R2.status_code == 409 and R2.json()["error"]["code"] == "not_proposed" and H.Ctx.Db.One("SELECT COUNT(*) AS n FROM stl_activations")["n"] == 1
        # Fulfillment's answer arrives, and storing it fails: nothing of it is kept — the line, the hand-over, the activation as they were
        Cmd = _Pending(H, A["id"])
        Ans = _Answer(A["id"], Cmd["id"], result="applied", revision_no=2, replaces=1, feasibility_needs_review=False)
        Bridge, Calls = H.App.state.Bridge, []
        Commit = Bridge._FileReplaced

        def Crashes(*A_, **K):
            Calls.append(1)
            if len(Calls) == 1:
                raise RuntimeError("disk full")
            return Commit(*A_, **K)
        monkeypatch.setattr(Bridge, "_FileReplaced", Crashes)
        try:
            Bridge.ReceiveEvents([Ans])
            raise AssertionError("the failed commit must answer with an error, so Fulfillment sends it again")
        except RuntimeError:
            pass
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_inbox WHERE event_id = ?", (Ans["event_id"],))["n"] == 0
        assert H.Ctx.Db.One("SELECT state FROM fulfillment_commands WHERE id = ?", (Cmd["id"],))["state"] == "pending"
        assert H.Ctx.Db.One("SELECT three_d_id, handoff_no FROM fulfillment_line_production WHERE line_id = ?", (LA["id"],)) == {"three_d_id": Orig, "handoff_no": 1}
        assert _Line(H, A["id"])["three_d_id"] == Orig and _Line(H, A["id"])["stl_activation"]["outcome"] == "pending"
        # Fulfillment sends it again: applied, once
        assert Bridge.ReceiveEvents([Ans])[0]["status"] == "applied"
        assert H.Ctx.Db.One("SELECT three_d_id, handoff_no FROM fulfillment_line_production WHERE line_id = ?", (LA["id"],)) == {"three_d_id": Rev, "handoff_no": 2}
        assert _Line(H, A["id"])["stl_activation"]["outcome"] == "applied" and H.Ctx.Db.One("SELECT state FROM fulfillment_commands WHERE id = ?", (Cmd["id"],))["state"] == "applied"
        # The command channel is the activation's own: the Admin's generic request cannot send replace_file
        R = await H.Client.post(f"/api/admin/orders/{A['id']}/fulfillment/commands", json={"action": "replace_file", "line_id": LA["id"], "reason": "x"}, headers=Admin)
        assert R.status_code == 400 and R.json()["error"]["code"] == "invalid_action"
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
        assert (await H.Client.post("/api/admin/bore-fixes/bfx_nope/restore", json={"note": "why not"}, headers=Admin)).status_code == 404
        # The order's own line took the correction with the acceptance; the explicit switch of before is gone
        assert _Line(H, O["id"])["three_d_id"] == Rev["id"]
        L = _Line(H, O["id"])
        assert (await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{L['id']}/use-bore-correction", json={"revision_id": Rev["id"]}, headers=Admin)).status_code in (404, 405)
    finally:
        await H.Close()
