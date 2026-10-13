"""The Atelier ↔ Fulfillment bridge (docs/FULFILLMENT.md): nothing exists while the flag is off; with it on, the service
credential, the explicit hand-over, the changes feed and its cursor, the snapshot (payment basis, address version,
the model's availability — or its absence — per line), the triggers on commercial changes and on 3D results, the
production file through the restricted asset route, the events inbox (idempotent, stale versions ignored, conflicts
recorded), the projection the Admin sees, the ownership guard, and the quote → order linkage."""

import hashlib
import json

from p3.db import Now
from p3.settings import LoadSettings
from tests.conftest import Harness
from tests.test_orders import Address, Customer, _InBag, _Order   # noqa: F401 — Address and Customer are shared with the notification tests
from tests.test_quotes import _Link, _Request

AdminKey = "bridge-admin-key-0123456789"
ServiceKey = "bridge-service-key-0123456789"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Service = {"Authorization": f"Bearer {ServiceKey}"}
Prefix = "/api/integrations/fulfillment/v1"


def _On(TmpPath, **Over):
    return Harness(TmpPath, AdminKey=AdminKey, FulfillmentIntegration=True, FulfillmentServiceKey=ServiceKey, **Over)


async def _Placed(H, Prompt="Twisted rope band", Size=7, Material="silver", Qty=2, Click="click-1") -> tuple[str, dict]:
    Did, Cand, _ = await _InBag(H, Prompt, Size=Size, Material=Material, Qty=Qty)
    R = await H.Client.post("/api/orders", json=_Order(client_request_id=Click))
    assert R.status_code == 200, R.text
    return Did, R.json()


def _Event(Id, OrderId, Version=1, Type="fulfillment.request_enrolled", **Public):
    return {"schema_version": 1, "event_id": Id, "source_environment": "fulfillment-test", "event_type": Type, "aggregate_type": "source_request",
            "aggregate_id": "src_1", "aggregate_version": Version, "source_entity": {"type": "order", "id": OrderId}, "occurred_at": Now(),
            "correlation_id": "corr-1", "public_payload": {"customer_status": "UNDER_REVIEW", "summary": "1 part under review", **Public}}


async def _Prepared(H, Did: str, OrderId: str, Size=7, Material: str = "silver", LineIndex: int = 0) -> dict:
    """A line's print file, the way the Admin makes one: the design's model (mock Hi3D here) at the ordered size and
    material, then Prepare STL on the line — which pins the line to the version and the sized result."""
    T = await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": Size, "material_id": Material}, headers=Admin)
    assert T.status_code == 200, T.text
    await H.Idle()
    L = H.Svc.Orders.AdminGet(OrderId)["lines"][LineIndex]
    R = await H.Client.post(f"/api/admin/orders/{OrderId}/lines/{L['id']}/3d", headers=Admin)
    assert R.status_code == 200, R.text
    await H.Idle()
    return next(X for X in H.Svc.Orders.AdminGet(OrderId)["lines"] if X["id"] == L["id"])


async def _Send(H, OrderId: str, LineIds=None, Note: str = "", Expect: int = 200) -> dict:
    """Send to Production: the given lines, or every line of the order."""
    Ids = LineIds or [L["id"] for L in H.Svc.Orders.AdminGet(OrderId)["lines"]]
    R = await H.Client.post(f"/api/admin/orders/{OrderId}/production", json={"line_ids": Ids, "note": Note}, headers=Admin)
    assert R.status_code == Expect, R.text
    return R.json()


async def _Enrolled(H, Did: str, O: dict, **Kw) -> dict:
    """An order with Fulfillment the only way there is: a line with a valid print file, sent to Production."""
    await _Prepared(H, Did, O["id"], **Kw)
    return await _Send(H, O["id"])


async def test_off_by_default_nothing_of_the_integration_exists(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        _Did, O = await _Placed(H)
        for P in (Prefix + "/identity", Prefix + "/changes", Prefix + f"/requests/order/{O['id']}"):
            assert (await H.Client.get(P, headers=Service)).status_code == 404, P
        assert (await H.Client.post(Prefix + "/events", json={"events": []}, headers=Service)).status_code == 404
        assert (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json() == {"enabled": False}
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/production", json={"line_ids": ["x"]}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "fulfillment_disabled"
        assert (await H.Client.post(f"/api/admin/orders/{O['id']}/fulfillment/enroll", headers=Admin)).status_code == 404   # the order-level hand-over is gone
        # The legacy flow is untouched: statuses step through, and no outbox row is ever written
        await H.Client.post(f"/api/admin/orders/{O['id']}/payment", json={"status": "paid", "note": "transfer"}, headers=Admin)
        assert (await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": "production", "note": "ok", "force": True}, headers=Admin)).status_code == 200
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_outbox")["n"] == 0
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_enrollments")["n"] == 0
        assert (await H.Client.get("/api/admin/health", headers=Admin)).json()["fulfillment_integration"] is False
        L = (await H.Client.get("/api/admin/orders?fulfillment=enrolled", headers=Admin)).json()      # the lists know nothing of it
        assert L["total"] == 1 and "fulfillment" not in L["orders"][0] and L["fulfillment_filters"] == []
        assert (await H.Client.post(Prefix + "/release-checks", json={"items": [{"line_id": "x", "quantity": 1}]}, headers=Service)).status_code == 404
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/fulfillment/commands", json={"action": "cancel_line", "line_id": "x", "reason": "r"}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "fulfillment_disabled"
    finally:
        await H.Close()


def test_the_service_credential_is_validated(tmp_path):
    import pytest
    Base = dict(DataDir=tmp_path / "var", Provider="mock", AdminKey=AdminKey, FulfillmentIntegration=True)
    with pytest.raises(ValueError, match="P3_FULFILLMENT_SERVICE_KEY"):
        LoadSettings(**Base, FulfillmentServiceKey="short")
    with pytest.raises(ValueError, match="differ"):
        LoadSettings(**Base, FulfillmentServiceKey=AdminKey)
    assert LoadSettings(**Base, FulfillmentServiceKey=ServiceKey).FulfillmentIntegration
    assert not LoadSettings(DataDir=tmp_path / "var", Provider="mock", AdminKey=AdminKey, FulfillmentServiceKey=None).FulfillmentIntegration


async def test_identity_needs_the_service_credential_and_never_the_admin_key(tmp_path):
    H = _On(tmp_path)
    try:
        assert (await H.Client.get(Prefix + "/identity")).status_code == 403
        assert (await H.Client.get(Prefix + "/identity", headers=Admin)).status_code == 403                 # the Admin key is not a service credential
        assert (await H.Client.get(Prefix + "/identity", headers={"Authorization": "Basic abc"})).status_code == 403
        R = await H.Client.get(Prefix + "/identity", headers=Service)
        assert R.status_code == 200, R.text
        I = R.json()
        assert I["contract_version"] == "atelier-fulfillment/v1" and I["environment_id"].startswith("atelier-") and len(I["environment_id"]) == 18
        assert ServiceKey not in R.text and AdminKey not in R.text and "changes" in I["capabilities"]
        assert (await H.Client.get("/api/admin/health", headers=Admin)).json()["fulfillment_integration"] is True
        # The id is kept with the data: a second app over the same data directory has the same one
        Again = Harness(tmp_path, AdminKey=AdminKey, FulfillmentIntegration=True, FulfillmentServiceKey=ServiceKey)
        try:
            assert (await Again.Client.get(Prefix + "/identity", headers=Service)).json()["environment_id"] == I["environment_id"]
        finally:
            await Again.Close()
    finally:
        await H.Close()


async def test_the_hand_over_the_changes_feed_and_the_snapshot(tmp_path):
    H = _On(tmp_path)
    try:
        Did, O = await _Placed(H)
        State = (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()
        assert State["enabled"] and not State["eligible"] and not State["enrolled"] and State["projection"] is None
        assert State["lines"][0]["reason"] == "no_model" and not State["lines"][0]["eligible"] and State["lines_sent"] == 0 and State["lines_total"] == 1
        assert "no line has a valid print file" in State["ineligible_reason"] and "Hi3D" in State["lines"][0]["message"]
        assert (await H.Client.get(Prefix + f"/requests/order/{O['id']}", headers=Service)).status_code == 404     # not enrolled: nothing to read
        # A print file makes the line eligible — and nothing more: nothing is ever sent by itself
        Line = await _Prepared(H, Did, O["id"])
        State = (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()
        assert State["eligible"] and State["lines"][0]["eligible"] and State["lines"][0]["print_version"]["label"] == "v1 · Hi3D" and State["lines"][0]["pinned"]
        assert not State["enrolled"] and H.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_outbox")["n"] == 0
        # Send to Production: the order is enrolled by its first line, the line's version and result are pinned on the row
        State = await _Send(H, O["id"], Note="  rush  ")
        assert State["enrolled"] and State["awaiting_sync"] and State["enrollment"]["enrolled_by"] == "developer-key" and State["enrollment"]["note"] == "rush"
        assert State["enrollment"]["source_version"] == 1 and not State["eligible"] and State["lines_sent"] == 1 and "every line is in Production" in State["ineligible_reason"]
        P = State["lines"][0]["production"]
        assert P["sent"] and P["handoff_no"] == 1 and P["state"] == "sent" and P["note"] == "rush" and P["sent_by"] == "developer-key"
        assert P["pinned"]["mesh_id"] == Line["three_d_version"]["mesh_id"] and P["pinned"]["three_d_id"] == Line["three_d_id"] and P["pinned"]["version_no"] == 1
        assert P["pinned"]["source"] == "hi3d" and P["pinned"]["scaling"] == "scalable" and len(P["pinned"]["master_sha256"]) == 64 and P["pinned"]["version_label"] == "v1 · Hi3D"
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/production", json={"line_ids": [Line["id"]]}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "already_sent" and R.json()["error"]["blocked"][0]["reason"] == "already_sent"
        assert (await H.Client.post(f"/api/admin/orders/{O['id']}/production", json={"line_ids": []}, headers=Admin)).status_code == 400
        assert (await H.Client.post(f"/api/admin/orders/{O['id']}/production", json={"line_ids": ["nope"]}, headers=Admin)).status_code == 404
        Events = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()["events"]
        Sent = next(E for E in Events if E["kind"] == "production_sent")
        assert Sent["by"] == "developer-key" and Sent["data"]["version_label"] == "v1 · Hi3D" and Sent["data"]["session_3d_id"] == Line["three_d_id"] and Sent["data"]["note"] == "rush"
        assert any(E["kind"] == "fulfillment" and E["data"]["action"] == "enrolled" for E in Events)
        # The export of the pinned result finishes after the hand-over: the file's hash is recorded, and the feed says so
        await H.Idle()
        State = (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()
        Pin = State["lines"][0]["production"]["pinned"]
        assert len(Pin["file_sha256"]) == 64 and Pin["file_bytes"] > 84 and Pin["file_job_id"]
        # The feed: the enrollment, then the file; a cursor that Fulfillment keeps; reading it records the pull
        C = (await H.Client.get(Prefix + "/changes?cursor=0", headers=Service)).json()
        assert [(X["seq"], X["kind"], X["reason"], X["source_version"]) for X in C["changes"]] == [(1, "enrolled", "production", 1), (2, "updated", "production_file", 2)]
        assert C["next_cursor"] == 2 and not C["has_more"] and C["environment_id"]
        assert (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()["awaiting_sync"]      # read with cursor 0: not yet
        assert (await H.Client.get(Prefix + "/changes?cursor=2", headers=Service)).json()["changes"] == []
        assert not (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()["awaiting_sync"]
        # The snapshot: commercial facts, no notes, no history; the line's hand-over with the exact pin; the model as it is
        S = (await H.Client.get(Prefix + f"/requests/order/{O['id']}", headers=Service)).json()
        assert S["entity_type"] == "order" and S["display_ref"] == "ORD-10001" and S["source_version"] == 2 and S["enrollment"]["state"] == "enrolled"
        Od = S["order"]
        assert Od["status"] == "new" and Od["payment"] == {"status": "pending", "basis": None, "provider": None, "ref": None, "updated_at": Od["placed_at"]}
        assert Od["customer"] == {"name": "Dana Levi", "email": "dana@example.com"} and Od["address"]["city"] == "Tel Aviv"
        assert len(Od["address_version"]) == 16 and Od["cancellation"] == {"state": "none", "at": None, "reason": None} and Od["promised_delivery"] is None
        assert "notes" not in S and "events" not in S and "notes" not in Od
        L = S["lines"][0]
        assert (L["item_ref"], L["product_type"], L["material_id"], L["size"], L["quantity"]) == ("R-1001-A", "ring", "silver", {"ring_size": 7, "label": "US 7"}, 2)
        assert L["image"] == {"design_id": Did, "candidate_id": L["candidate_id"]} and L["quote_request_id"] is None
        assert L["production"]["sent"] and L["production"]["pinned"]["file_sha256"] == Pin["file_sha256"] and L["production"]["pinned"]["three_d_id"] == Line["three_d_id"]
        assert L["model"]["available"] and L["model"]["state"] == "complete" and L["model"]["mesh_id"] == Pin["mesh_id"] and L["model"]["raw_sha256"] == Pin["master_sha256"]
        assert L["model"]["version"] == {"mesh_id": Pin["mesh_id"], "version_no": 1, "label": "v1 · Hi3D", "source": "hi3d", "scaling": "scalable", "original_filename": None, "pinned": True}
        # Commercial changes reach the feed in the transaction of the change; notes and legacy statuses do not
        await H.Client.post(f"/api/admin/orders/{O['id']}/note", json={"note": "gift wrap"}, headers=Admin)
        assert (await H.Client.get(Prefix + "/changes?cursor=2", headers=Service)).json()["changes"] == []
        await H.Client.post(f"/api/admin/orders/{O['id']}/payment", json={"status": "paid", "note": "transfer", "ref": "TR-1"}, headers=Admin)
        C = (await H.Client.get(Prefix + "/changes?cursor=2", headers=Service)).json()["changes"]
        assert [(X["kind"], X["reason"], X["source_version"]) for X in C] == [("updated", "payment", 3), ("updated", "status", 4)]   # paid → payment confirmed
        S = (await H.Client.get(Prefix + f"/requests/order/{O['id']}", headers=Service)).json()
        assert S["source_version"] == 4 and S["order"]["payment"]["status"] == "paid" and S["order"]["payment"]["basis"] == "paid" and S["order"]["payment"]["ref"] == "TR-1"
        # The image of a line is served to the service only, by ids, never by a path
        Img = await H.Client.get(Prefix + f"/assets/image/{Did}/{L['candidate_id']}", headers=Service)
        assert Img.status_code == 200 and Img.headers["content-type"].startswith("image/") and Img.headers["cache-control"] == "private, no-store"
        assert (await H.Client.get(Prefix + f"/assets/image/{Did}/{L['candidate_id']}")).status_code == 403
        assert (await H.Client.get(Prefix + f"/assets/image/{Did}/nope", headers=Service)).status_code == 404
        assert (await H.Client.get(Prefix + "/assets/image/../../x/y", headers=Service)).status_code in (404, 403)
    finally:
        await H.Close()


async def test_an_enrolled_order_belongs_to_fulfillment_for_production_and_the_projection_never_loops(tmp_path):
    H = _On(tmp_path)
    try:
        Did, O = await _Placed(H)
        await _Enrolled(H, Did, O)
        await H.Client.post(f"/api/admin/orders/{O['id']}/payment", json={"status": "paid", "note": "transfer"}, headers=Admin)
        for S in ("production", "qc", "shipped", "completed"):
            R = await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": S, "note": "x", "force": True}, headers=Admin)
            assert R.status_code == 409 and R.json()["error"]["code"] == "owned_by_fulfillment", S
        Before = H.Ctx.Db.One("SELECT MAX(seq) AS s FROM fulfillment_outbox")["s"]
        # The projection's own status writes are not commercial changes: no outbox row (no loop)
        H.Svc.Orders._SetStatus(O["id"], "production", "fulfillment", "projection")
        H.Svc.Orders._SetStatus(O["id"], "qc", "fulfillment", "projection")
        assert H.Ctx.Db.One("SELECT MAX(seq) AS s FROM fulfillment_outbox")["s"] == Before
        # A cancellation by the Admin is a commercial change and does reach the feed
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": "cancelled", "note": "customer changed their mind"}, headers=Admin)
        assert R.status_code == 200
        Last = H.Ctx.Db.One("SELECT * FROM fulfillment_outbox ORDER BY seq DESC LIMIT 1")
        assert Last["seq"] == Before + 1 and Last["reason"] == "status"
        S = (await H.Client.get(Prefix + f"/requests/order/{O['id']}", headers=Service)).json()
        assert S["order"]["cancellation"]["state"] == "cancelled" and S["order"]["cancellation"]["reason"] == "customer changed their mind"
        # An order that is not enrolled keeps the legacy flow exactly (force with a note)
        _Did2, O2 = await _Placed(H, "Plain band", Click="click-2")
        await H.Client.post(f"/api/admin/orders/{O2['id']}/payment", json={"status": "paid", "note": "transfer"}, headers=Admin)
        assert (await H.Client.post(f"/api/admin/orders/{O2['id']}/status", json={"status": "production", "note": "legacy", "force": True}, headers=Admin)).status_code == 200
        R = await _Send(H, O2["id"], Expect=409)
        assert R["error"]["code"] == "not_eligible"                                                      # already in the legacy production flow
        # There is no un-send (K7): no release route, the enrollment stays; a line leaves Production through the cancellation request
        assert (await H.Client.post(f"/api/admin/orders/{O['id']}/fulfillment/release", json={"note": "oops"}, headers=Admin)).status_code == 404
        assert (await H.Client.delete(f"/api/admin/orders/{O['id']}/production", headers=Admin)).status_code in (404, 405)
        assert H.Ctx.Db.One("SELECT state FROM fulfillment_enrollments WHERE entity_id = ?", (O["id"],))["state"] == "enrolled"
    finally:
        await H.Close()


async def test_the_production_file_is_pinned_at_the_hand_over_and_served_with_its_checksum(tmp_path):
    H = _On(tmp_path)
    try:
        Did, O = await _Placed(H, "A twisted band", Size=10, Material="silver", Qty=1)
        LineId = H.Svc.Orders.AdminGet(O["id"])["lines"][0]["id"]
        # No model: "print file required", with the reason — nothing is enrolled, nothing prepared, nothing exported
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/production", json={"line_ids": [LineId]}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "print_file_required" and "Print file required" in R.json()["error"]["message"]
        assert R.json()["error"]["blocked"] == [{"line_id": LineId, "item_ref": "R-1001-A", "reason": "no_model", "message": R.json()["error"]["blocked"][0]["message"]}]
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_enrollments")["n"] == 0 and H.Ctx.Db.One("SELECT COUNT(*) AS n FROM geometry_jobs WHERE kind = 'export'")["n"] == 0
        # The Admin generates the model on the session page (mock Hi3D here) at another size and material: still no print file for THIS line
        T = await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 7, "material_id": "vermeil"}, headers=Admin)
        assert T.status_code == 200, T.text
        await H.Idle()
        State = (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()
        assert State["lines"][0]["reason"] == "not_prepared" and not State["eligible"]
        assert (await _Send(H, O["id"], [LineId], Expect=409))["error"]["blocked"][0]["reason"] == "not_prepared"
        # Prepare STL on the line: the ordered size and material by arithmetic (no paid call), the line pinned — eligible now
        assert (await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{LineId}/3d", headers=Admin)).status_code == 200
        await H.Idle()
        State = (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()
        assert State["lines"][0]["eligible"] and State["lines"][0]["pinned"] and State["lines"][0]["print_state"] == "complete"
        # Before the hand-over Fulfillment gets no file for the line, whatever its model
        R = await H.Client.post(Prefix + "/assets/production-stl", json={"line_id": LineId}, headers=Service)
        assert R.status_code == 404 and R.json()["error"]["code"] == "line_not_found"
        # Sent: the export of the pinned result is made; its hash is the file's identity on both sides from here on
        await _Send(H, O["id"], [LineId])
        await H.Idle()
        P = (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()["lines"][0]["production"]["pinned"]
        assert len(P["file_sha256"]) == 64 and P["file_bytes"] > 84 and P["file_job_id"]
        L = (await H.Client.get(Prefix + f"/requests/order/{O['id']}", headers=Service)).json()["lines"][0]
        assert L["production"]["sent"] and L["production"]["pinned"]["file_sha256"] == P["file_sha256"] and L["production"]["pinned"]["three_d_id"] == P["three_d_id"]
        M = L["model"]
        assert M["available"] and M["state"] == "complete" and M["session_3d_id"] == P["three_d_id"] and M["production_size"] == 10 and M["mesh_id"] == P["mesh_id"]
        assert M["raw_sha256"] == P["master_sha256"] and M["version"]["label"] == "v1 · Hi3D" and M["version"]["pinned"] and M["geometry"]["volume_mm3"] > 0
        assert M["weight"]["g"] > 0 and M["weight"]["density_g_cm3"] == 10.36 and "calculated" in M["weight"]["source"]
        R = await H.Client.post(Prefix + "/assets/production-stl", json={"line_id": LineId}, headers=Service)
        assert R.status_code == 200 and R.json()["status"] == "done" and R.json()["sha256"] == P["file_sha256"] == R.json()["pinned_sha256"] and R.json()["handoff_no"] == 1
        Sid, Jid = R.json()["session_3d_id"], R.json()["job_id"]
        F = await H.Client.get(Prefix + f"/assets/production-stl/{Sid}/{Jid}", headers=Service)
        assert F.status_code == 200, F.text
        assert F.headers["content-type"].startswith("model/stl") and F.headers["x-content-sha256"] == hashlib.sha256(F.content).hexdigest() == P["file_sha256"]
        assert int(F.headers["x-content-bytes"]) == len(F.content) == P["file_bytes"]
        assert (await H.Client.get(Prefix + f"/assets/production-stl/{Sid}/{Jid}")).status_code == 403
        assert (await H.Client.get(Prefix + f"/assets/production-stl/{Sid}/nope", headers=Service)).status_code == 404
        # A result no sent line pins is never served, service credential or not (the Admin's vermeil result of the same design)
        Other = H.Ctx.Db.One("SELECT id FROM session_3d WHERE design_id = ? AND material_id = 'vermeil'", (Did,))["id"]
        assert (await H.Client.get(Prefix + f"/assets/production-stl/{Other}/stored", headers=Service)).status_code == 404
        assert (await H.Client.post(Prefix + "/assets/production-stl", json={"line_id": "nope"}, headers=Service)).status_code == 404
        # The file served is the one recorded at the hand-over: a different export is an integration fault, refused on both routes
        H.Ctx.Db.Execute("UPDATE fulfillment_line_production SET file_sha256 = ? WHERE line_id = ?", ("0" * 64, LineId))
        R = await H.Client.post(Prefix + "/assets/production-stl", json={"line_id": LineId}, headers=Service)
        assert R.status_code == 409 and R.json()["error"]["code"] == "file_changed" and R.json()["error"]["export_sha256"] == P["file_sha256"]
        assert (await H.Client.get(Prefix + f"/assets/production-stl/{Sid}/{Jid}", headers=Service)).status_code == 409
        assert len(H.Provider.SubmissionsFor(__import__("p3.providers.endpoints", fromlist=["Mesh"]).Mesh)) == 1       # one Hi3D request, the Admin's
    finally:
        await H.Close()


async def test_send_selected_lines_a_partly_sent_order_keeps_its_commercial_status(tmp_path):
    """K1, K11: two lines, one with a print file. The order-level action sends only the selected eligible lines and names
    the blocked one; Fulfillment's order status is applied only once every line is in Production."""
    H = _On(tmp_path)
    try:
        D1, _C1, _ = await _InBag(H, "A twisted band", Size=7, Material="silver", Qty=2)
        D2, _C2, _ = await _InBag(H, "A plain band", Size=8, Material="silver", Qty=1)
        R = await H.Client.post("/api/orders", json=_Order(client_request_id="two-lines"))
        assert R.status_code == 200, R.text
        O = R.json()
        L1, L2 = H.Svc.Orders.AdminGet(O["id"])["lines"]
        assert (L1["design_id"], L2["design_id"]) == (D1, D2)
        await _Prepared(H, D1, O["id"], Size=7, Material="silver", LineIndex=0)
        # Both at once: refused as a whole, the blocked line named with its reason — nothing sent, nothing enrolled
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/production", json={"line_ids": [L1["id"], L2["id"]]}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "print_file_required"
        assert [(B["line_id"], B["reason"]) for B in R.json()["error"]["blocked"]] == [(L2["id"], "no_model")]
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_line_production")["n"] == 0
        # The eligible line alone: 1 of 2 in Production; the other stays commercial-only and is still eligible later
        State = await _Send(H, O["id"], [L1["id"]], Note="complimentary sample — approved for production")
        assert State["enrolled"] and State["lines_sent"] == 1 and State["lines_total"] == 2 and not State["eligible"]
        assert State["lines"][0]["production"]["sent"] and not State["lines"][1]["production"]["sent"] and State["lines"][1]["reason"] == "no_model"
        S = (await H.Client.get(Prefix + f"/requests/order/{O['id']}", headers=Service)).json()
        assert [X["production"]["sent"] for X in S["lines"]] == [True, False] and S["lines"][1]["model"]["state"] == "no_model"
        # The legacy statuses are refused already (a line is in Production); Fulfillment's order status is not applied yet (K1)
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": "production", "note": "x", "force": True}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "owned_by_fulfillment"
        E = _Event("evt-k1", O["id"], 1, Type="fulfillment.production_started", order_status="production", customer_status="In production")
        assert (await H.Client.post(Prefix + "/events", json={"events": [E]}, headers=Service)).json()["results"][0]["status"] == "applied"
        assert H.Svc.Orders.AdminGet(O["id"])["status"] == "new"
        assert (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()["projection"]["order_status"] == "production"   # shown per line, not written
        # The second line gets its print file and goes too: 2 of 2 — from now on Fulfillment's order status is written forward
        await _Prepared(H, D2, O["id"], Size=8, Material="silver", LineIndex=1)
        State = await _Send(H, O["id"], [L2["id"]])
        assert State["lines_sent"] == 2 and all(X["production"]["sent"] for X in State["lines"])
        C = (await H.Client.get(Prefix + "/changes?cursor=0", headers=Service)).json()["changes"]
        assert [(X["kind"], X["reason"]) for X in C if X["reason"] in ("production", "production_file")][:1] == [("enrolled", "production")]
        assert ("updated", "production") in [(X["kind"], X["reason"]) for X in C]
        E = _Event("evt-k2", O["id"], 2, Type="fulfillment.production_started", order_status="production", customer_status="In production")
        await H.Client.post(Prefix + "/events", json={"events": [E]}, headers=Service)
        assert H.Svc.Orders.AdminGet(O["id"])["status"] == "production"
        Rows = (await H.Client.get("/api/admin/orders", headers=Admin)).json()["orders"]
        assert next(X for X in Rows if X["id"] == O["id"])["fulfillment"]["lines_sent"] == 2
    finally:
        await H.Close()


async def test_events_are_idempotent_stale_ones_are_ignored_and_conflicts_are_recorded(tmp_path):
    H = _On(tmp_path)
    try:
        Did, O = await _Placed(H)
        await _Enrolled(H, Did, O)
        E1 = _Event("evt-1", O["id"], 1, parts=[{"display_no": "MP-10001-001", "summary": "0/2 passed QC", "file_state": "unavailable"}])
        R = await H.Client.post(Prefix + "/events", json={"events": [E1]}, headers=Service)
        assert R.status_code == 200 and R.json()["results"] == [{"event_id": "evt-1", "status": "applied", "result": "applied"}]
        State = (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()
        assert State["projection"]["customer_status"] == "UNDER_REVIEW" and State["projection"]["parts"][0]["display_no"] == "MP-10001-001"
        Hist = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()["events"]
        assert Hist[-1]["kind"] == "fulfillment" and Hist[-1]["by"] == "fulfillment" and Hist[-1]["data"]["summary"] == "1 part under review"
        # The same event again: a duplicate, applied once; the same id with another payload: a conflict, kept aside
        assert (await H.Client.post(Prefix + "/events", json={"events": [E1]}, headers=Service)).json()["results"][0]["status"] == "duplicate"
        Other = {**E1, "public_payload": {"customer_status": "IN_PRODUCTION"}}
        assert (await H.Client.post(Prefix + "/events", json={"events": [Other]}, headers=Service)).json()["results"][0]["status"] == "conflict"
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_inbox_conflicts")["n"] == 1
        assert (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()["projection"]["customer_status"] == "UNDER_REVIEW"
        # Versions: 3 applies, then 2 (older) is ignored and never rolls the state back
        E3 = _Event("evt-3", O["id"], 3, Type="fulfillment.review_approved", customer_status="APPROVED", summary="approved for revision 1")
        E2 = _Event("evt-2", O["id"], 2, Type="fulfillment.review_started", customer_status="UNDER_REVIEW", summary="review started")
        R = (await H.Client.post(Prefix + "/events", json={"events": [E3, E2]}, headers=Service)).json()["results"]
        assert [X["status"] for X in R] == ["applied", "accepted"] and R[1]["result"] == "ignored_stale"
        assert (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()["projection"]["customer_status"] == "APPROVED"
        # Validation: schema version, missing fields, unknown entity, size
        Bad = (await H.Client.post(Prefix + "/events", json={"events": [{**E1, "event_id": "evt-9", "schema_version": 2}, {"event_id": "evt-10"},
                                                                       _Event("evt-11", "ord_nope")]}, headers=Service)).json()["results"]
        assert [X["status"] for X in Bad] == ["rejected", "rejected", "accepted"] and Bad[2]["result"] == "unknown_entity"
        assert (await H.Client.post(Prefix + "/events", json={"events": "x"}, headers=Service)).status_code == 400
        assert (await H.Client.post(Prefix + "/events", json={"events": [E1]})).status_code == 403
    finally:
        await H.Close()


async def test_a_quotation_stays_with_the_admin_and_the_order_it_becomes_carries_the_reference(tmp_path):
    H = _On(tmp_path)
    try:
        _B, Q = await _Request(H)
        # A quotation is never sent to Fulfillment (D-10): no action, no snapshot, no change in the feed
        assert (await H.Client.post(f"/api/admin/quote-requests/{Q['id']}/fulfillment/enroll", headers=Admin)).status_code == 404
        State = (await H.Client.get(f"/api/admin/quote-requests/{Q['id']}/fulfillment", headers=Admin)).json()
        assert State["quotation"] and not State["eligible"] and not State["enrolled"] and State["order"] is None and "managed here" in State["ineligible_reason"]
        assert (await H.Client.get(Prefix + f"/requests/quote_request/{Q['id']}", headers=Service)).status_code == 404
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_outbox")["n"] == 0
        assert H.Ctx.Db.One("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = 'fulfillment_quotes_changed'") is None
        # The quote is sent and approved by the customer: it becomes an order — that order is what goes to Fulfillment
        from datetime import datetime, timedelta, timezone
        Offer = {"size": 7, "weight_g": 6.5, "price_per_g": 400, "unit_price": 2600, "quantity": 2, "message": "Hello Dana,\n\nHere is your quote.",
                 "valid_until": (datetime.now(timezone.utc).date() + timedelta(days=10)).isoformat()}
        assert (await H.Client.post(f"/api/admin/quote-requests/{Q['id']}/offer", json=Offer, headers=Admin)).status_code == 200
        Token = _Link(H).split("token=")[1]
        R = await H.Client.post("/quote/approve", data={"token": Token, **Address, "shipping_method": "standard", "terms": "1"})
        assert R.status_code == 200 and "ORD-10001" in R.text
        State = (await H.Client.get(f"/api/admin/quote-requests/{Q['id']}/fulfillment", headers=Admin)).json()
        assert State["order"]["ref"] == "ORD-10001" and State["order_fulfillment"] is None
        OrderId = State["order"]["id"]
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_outbox")["n"] == 0                       # the conversion itself is no change
        await _Prepared(H, Q["design_id"], OrderId, Size=7, Material="gold_18k_yellow")
        assert (await _Send(H, OrderId))["enrolled"]
        S = (await H.Client.get(Prefix + f"/requests/order/{OrderId}", headers=Service)).json()
        assert S["order"]["quote"]["ref"] == "Q-5001" and S["lines"][0]["quote_request_id"] == Q["id"] and S["enrollment"]["inherited_from"] is None
        assert S["lines"][0]["material_id"] == "gold_18k_yellow" and S["lines"][0]["quantity"] == 2 and S["order"]["payment"]["status"] == "pending"
        C = (await H.Client.get(Prefix + "/changes?cursor=0", headers=Service)).json()["changes"]
        assert [(X["entity_type"], X["kind"], X["reason"]) for X in C] == [("order", "enrolled", "production")]
        # The quote page and the quote inbox show the order's Fulfillment state; a note on the quote changes nothing
        assert (await H.Client.get(f"/api/admin/quote-requests/{Q['id']}/fulfillment", headers=Admin)).json()["order_fulfillment"]["enrolled"]
        Rows = (await H.Client.get("/api/admin/quote-requests", headers=Admin)).json()["quote_requests"]
        assert Rows[0]["id"] == Q["id"] and Rows[0]["fulfillment"]["enrolled"]
        assert (await H.Client.post(f"/api/admin/quote-requests/{Q['id']}/note", json={"note": "x"}, headers=Admin)).status_code == 200
        assert len((await H.Client.get(Prefix + "/changes?cursor=0", headers=Service)).json()["changes"]) == 1
    finally:
        await H.Close()


async def test_release_checks_answer_payment_cancellation_and_quantity_live(tmp_path):
    H = _On(tmp_path)
    try:
        Did, O = await _Placed(H, Qty=2)
        LineId = H.Svc.Orders.AdminGet(O["id"])["lines"][0]["id"]
        Url = Prefix + "/release-checks"
        assert (await H.Client.post(Url, json={"items": [{"line_id": LineId, "quantity": 2}]})).status_code == 403
        R = await H.Client.post(Url, json={"items": [{"line_id": LineId, "quantity": 2}]}, headers=Service)
        assert R.status_code == 200 and R.json()["items"][0]["blockers"][0]["code"] == "LINE_NOT_FOUND"        # not enrolled: nothing is told
        await _Enrolled(H, Did, O)
        assert (await H.Client.get(Prefix + "/identity", headers=Service)).json()["payment_gate"] is True     # the deployed rule, until activation (K8)
        R = (await H.Client.post(Url, json={"items": [{"line_id": LineId, "quantity": 2}]}, headers=Service)).json()
        assert R["ok"] is False and [B["code"] for B in R["items"][0]["blockers"]] == ["PAYMENT_PENDING"] and R["items"][0]["source_version"] == 1
        assert R["check_id"].startswith("chk_") and R["checked_at"] and R["environment_id"]
        await H.Client.post(f"/api/admin/orders/{O['id']}/payment", json={"status": "paid", "note": "transfer"}, headers=Admin)
        R = (await H.Client.post(Url, json={"items": [{"line_id": LineId, "quantity": 2}, {"line_id": LineId, "quantity": 3}, {"line_id": "nope", "quantity": 1}]},
                                 headers=Service)).json()
        assert R["ok"] is False and R["items"][0]["ok"] and R["items"][0]["payment"] == {"status": "paid", "basis": "paid"} and R["items"][0]["source_version"] == 3
        assert [B["code"] for B in R["items"][1]["blockers"]] == ["QUANTITY_EXCEEDS_ORDER"] and [B["code"] for B in R["items"][2]["blockers"]] == ["LINE_NOT_FOUND"]
        await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": "cancelled", "note": "changed their mind"}, headers=Admin)
        R = (await H.Client.post(Url, json={"items": [{"line_id": LineId, "quantity": 1}]}, headers=Service)).json()
        assert [B["code"] for B in R["items"][0]["blockers"]] == ["ORDER_CANCELLED"]
        assert (await H.Client.post(Url, json={"items": []}, headers=Service)).status_code == 400
    finally:
        await H.Close()


async def test_with_the_payment_gate_off_payment_is_shown_and_never_blocks(tmp_path):
    """§J, K8: the rule Atelier owns, advertised in the identity; off, the live checks carry the payment but no PAYMENT_PENDING."""
    H = _On(tmp_path, FulfillmentPaymentGate=False)
    try:
        Did, O = await _Placed(H, Qty=1)
        await _Enrolled(H, Did, O)
        LineId = H.Svc.Orders.AdminGet(O["id"])["lines"][0]["id"]
        assert (await H.Client.get(Prefix + "/identity", headers=Service)).json()["payment_gate"] is False
        R = (await H.Client.post(Prefix + "/release-checks", json={"items": [{"line_id": LineId, "quantity": 1}]}, headers=Service)).json()
        assert R["ok"] is True and R["items"][0]["blockers"] == [] and R["items"][0]["payment"] == {"status": "pending", "basis": None}
        R = (await H.Client.post(Prefix + "/shipping-checks", json={"order_id": O["id"]}, headers=Service)).json()
        assert R["ok"] is True and R["blockers"] == [] and R["payment"] == {"status": "pending", "basis": None}
        # Cancellation and quantities still gate; the checkout and the payment records are untouched
        R = (await H.Client.post(Prefix + "/release-checks", json={"items": [{"line_id": LineId, "quantity": 2}]}, headers=Service)).json()
        assert [B["code"] for B in R["items"][0]["blockers"]] == ["QUANTITY_EXCEEDS_ORDER"]
        assert H.Svc.Orders.AdminGet(O["id"])["payment_status"] == "pending" and (await H.Client.get(Prefix + f"/requests/order/{O['id']}", headers=Service)).json()["order"]["payment"]["basis"] is None
    finally:
        await H.Close()


async def test_shipping_checks_answer_the_authoritative_address_and_its_version_live(tmp_path):
    """Batch 7 (spec §23, C05): Fulfillment packs against the address Atelier holds now; a changed address has a new version."""
    H = _On(tmp_path)
    try:
        Did, O = await _Placed(H, Qty=1)
        Url = Prefix + "/shipping-checks"
        assert (await H.Client.post(Url, json={"order_id": O["id"]})).status_code == 403
        R = (await H.Client.post(Url, json={"order_id": O["id"]}, headers=Service)).json()
        assert R["ok"] is False and R["blockers"][0]["code"] == "ORDER_NOT_FOUND" and "address" not in R                # not enrolled: nothing is told
        await _Enrolled(H, Did, O)
        Snap = (await H.Client.get(Prefix + f"/requests/order/{O['id']}", headers=Service)).json()
        R = (await H.Client.post(Url, json={"order_id": O["id"]}, headers=Service)).json()
        assert R["ok"] is False and [B["code"] for B in R["blockers"]] == ["PAYMENT_PENDING"]
        assert R["address"] == Snap["order"]["address"] and R["address_version"] == Snap["order"]["address_version"] and len(R["address_version"]) == 16
        assert R["order_ref"] == O["ref"] and R["check_id"].startswith("shk_") and R["source_version"] == Snap["source_version"] and R["cancellation"]["state"] == "none"
        await H.Client.post(f"/api/admin/orders/{O['id']}/payment", json={"status": "paid", "note": "transfer"}, headers=Admin)
        R = (await H.Client.post(Url, json={"order_id": O["id"]}, headers=Service)).json()
        assert R["ok"] is True and R["payment"] == {"status": "paid", "basis": "paid"} and R["blockers"] == []
        # The customer's address changed (as a change of the stored shipping data): a new version, the new address, and the feed knows too
        Shipping = json.loads(H.Ctx.Db.One("SELECT shipping_json FROM orders WHERE id = ?", (O["id"],))["shipping_json"])
        Shipping.update({"line1": "5 Herzl St", "city": "Haifa"})
        H.Ctx.Db.Execute("UPDATE orders SET shipping_json = ?, updated_at = ? WHERE id = ?", (json.dumps(Shipping), Now(), O["id"]))
        R2 = (await H.Client.post(Url, json={"order_id": O["id"]}, headers=Service)).json()
        assert R2["address_version"] != R["address_version"] and R2["address"]["city"] == "Haifa"
        assert (await H.Client.get(Prefix + "/changes?cursor=0", headers=Service)).json()["changes"][-1]["reason"] == "address"
        await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": "cancelled", "note": "changed their mind"}, headers=Admin)
        R = (await H.Client.post(Url, json={"order_id": O["id"]}, headers=Service)).json()
        assert [B["code"] for B in R["blockers"]] == ["ORDER_CANCELLED"] and R["cancellation"]["state"] == "cancelled"
        assert (await H.Client.post(Url, json={}, headers=Service)).json()["blockers"][0]["code"] == "ORDER_NOT_FOUND"
    finally:
        await H.Close()


async def test_d07_a_command_stays_pending_until_fulfillment_answers_and_a_repeated_click_makes_one(tmp_path):
    H = _On(tmp_path)
    try:
        Did, O = await _Placed(H, Qty=2)
        LineId = H.Svc.Orders.AdminGet(O["id"])["lines"][0]["id"]
        Url = f"/api/admin/orders/{O['id']}/fulfillment/commands"
        R = await H.Client.post(Url, json={"action": "cancel_line", "line_id": LineId, "quantity": 1, "reason": "one ring fewer"}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "not_enrolled"
        await _Enrolled(H, Did, O)
        await H.Idle()
        await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)                  # the production file recorded (seq 2); the command is next
        for Bad, Code in (({"action": "cancel_line", "line_id": LineId, "quantity": 1}, "reason_required"),
                          ({"action": "cancel_line", "line_id": "x", "reason": "r"}, "line_not_found"),
                          ({"action": "cancel_line", "line_id": LineId, "quantity": 5, "reason": "r"}, "invalid_quantity"),
                          ({"action": "expedite", "line_id": LineId, "reason": "r"}, "invalid_action")):
            R = await H.Client.post(Url, json=Bad, headers=Admin)
            assert R.json()["error"]["code"] == Code, Bad
        R = await H.Client.post(Url, json={"action": "cancel_line", "line_id": LineId, "quantity": 1, "reason": "one ring fewer"}, headers=Admin)
        assert R.status_code == 200, R.text
        Cmd = R.json()
        assert Cmd["state"] == "pending" and Cmd["id"].startswith("cmd_") and Cmd["actor"] == "developer-key" and Cmd["delegated_permission"] == "decide_cancellation"
        assert Cmd["payload"]["quantity"] == 1 and Cmd["payload"]["line_id"] == LineId and Cmd["expected_version"] == 2 and Cmd["decided_at"] is None
        Again = (await H.Client.post(Url, json={"action": "cancel_line", "line_id": LineId, "quantity": 2, "reason": "again"}, headers=Admin)).json()
        assert Again["id"] == Cmd["id"] and Again["payload"]["quantity"] == 1                   # a repeated click: the same pending request
        State = (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()
        assert [C["state"] for C in State["commands"]] == ["pending"] and State["awaiting_sync"] and State["command_actions"] == ["cancel_line"]
        # The feed carries it with its payload; Fulfillment answers with an event; the first answer stands
        C = (await H.Client.get(Prefix + "/changes?cursor=2", headers=Service)).json()["changes"]
        assert [(X["kind"], X["reason"]) for X in C] == [("command", "cancel_line")] and C[0]["payload"]["command_id"] == Cmd["id"] and C[0]["payload"]["reason"] == "one ring fewer"
        E = _Event("evt-c1", O["id"], 2, Type="fulfillment.command_applied",
                   command={"command_id": Cmd["id"], "action": "cancel_line", "result": "partially_accepted", "reason": None, "part": "MP-10001-001"})
        assert (await H.Client.post(Prefix + "/events", json={"events": [E]}, headers=Service)).json()["results"][0]["status"] == "applied"
        Cmd = (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment/commands", headers=Admin)).json()["commands"][0]
        assert Cmd["state"] == "applied" and Cmd["result"]["result"] == "partially_accepted" and Cmd["decided_at"]
        E2 = _Event("evt-c2", O["id"], 3, Type="fulfillment.command_rejected", command={"command_id": Cmd["id"], "result": "rejected", "reason": "late"})
        await H.Client.post(Prefix + "/events", json={"events": [E2]}, headers=Service)
        assert (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment/commands", headers=Admin)).json()["commands"][0]["state"] == "applied"
        Hist = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()["events"]
        assert any(X["kind"] == "fulfillment" and X["data"].get("action") == "command_outcome" and X["data"]["state"] == "applied" for X in Hist)
        # A new request for the same line is possible once the first is answered
        assert (await H.Client.post(Url, json={"action": "cancel_line", "line_id": LineId, "reason": "the rest too"}, headers=Admin)).json()["id"] != Cmd["id"]
    finally:
        await H.Close()


async def test_the_projection_moves_the_order_status_forward_only_and_never_loops(tmp_path):
    H = _On(tmp_path)
    try:
        Did, O = await _Placed(H)
        await _Enrolled(H, Did, O)
        await H.Client.post(f"/api/admin/orders/{O['id']}/payment", json={"status": "paid", "note": "transfer"}, headers=Admin)
        Before = H.Ctx.Db.One("SELECT MAX(seq) AS s FROM fulfillment_outbox")["s"]

        async def Post(Id, Version, Status):
            E = _Event(Id, O["id"], Version, Type="fulfillment.request_updated", order_status=Status)
            return (await H.Client.post(Prefix + "/events", json={"events": [E]}, headers=Service)).json()["results"][0]["status"]

        assert await Post("evt-p1", 2, "production") == "applied"
        Od = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()
        assert Od["status"] == "production" and any(E["kind"] == "status" and E["by"] == "fulfillment" and E["data"]["to"] == "production" for E in Od["events"])
        assert H.Ctx.Db.One("SELECT MAX(seq) AS s FROM fulfillment_outbox")["s"] == Before            # the projection's status is no commercial change
        await Post("evt-p2", 3, "qc")
        assert H.Svc.Orders.AdminGet(O["id"])["status"] == "qc"
        await Post("evt-p3", 4, "production")                                                         # never a step back
        assert H.Svc.Orders.AdminGet(O["id"])["status"] == "qc"
        assert await Post("evt-p4", 1, "shipped") == "accepted"                                       # stale: ignored
        assert H.Svc.Orders.AdminGet(O["id"])["status"] == "qc"
        await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": "cancelled", "note": "x"}, headers=Admin)
        await Post("evt-p5", 5, "shipped")
        assert H.Svc.Orders.AdminGet(O["id"])["status"] == "cancelled"                                # a cancelled order is left alone
    finally:
        await H.Close()


async def test_the_lists_carry_the_fulfillment_state_and_filter_by_it(tmp_path):
    H = _On(tmp_path)
    try:
        _D1, O1 = await _Placed(H, "Band one", Click="c1")
        D2, O2 = await _Placed(H, "Band two", Click="c2")
        await _Enrolled(H, D2, O2)
        E = _Event("evt-l1", O2["id"], 1, parts=[{"display_no": "MP-10002-001", "status": "awaiting_review", "review_status": "in_review", "summary": "0/2 passed QC",
                                                  "file_state": "unavailable", "blockers": ["PAYMENT_PENDING", "REVISION_NOT_APPROVED"]}], commercial_action_pending=False)
        await H.Client.post(Prefix + "/events", json={"events": [E]}, headers=Service)
        L = (await H.Client.get("/api/admin/orders", headers=Admin)).json()
        ById = {X["id"]: X for X in L["orders"]}
        assert ById[O1["id"]]["fulfillment"] is None
        F = ById[O2["id"]]["fulfillment"]
        assert F["enrolled"] and F["customer_status"] == "UNDER_REVIEW" and F["review_status"] == "in_review" and F["blocker"] == "PAYMENT_PENDING" and F["pending_commands"] == 0
        assert F["parts"][0]["display_no"] == "MP-10002-001" and {X["id"] for X in L["fulfillment_filters"]} >= {"enrolled", "review", "blocked", "shipped"}
        for Filter, Expected in (("enrolled", [O2["id"]]), ("review", [O2["id"]]), ("shipped", []), ("blocked", []), ("commercial_action", [])):
            R = (await H.Client.get(f"/api/admin/orders?fulfillment={Filter}", headers=Admin)).json()
            assert [X["id"] for X in R["orders"]] == Expected and R["total"] == len(Expected), Filter
        assert (await H.Client.get("/api/admin/orders?fulfillment=nope", headers=Admin)).status_code == 400
        # The quote inbox: a quotation has no Fulfillment state of its own (its order will)
        _B, Q = await _Request(H)
        Rows = (await H.Client.get("/api/admin/quote-requests", headers=Admin)).json()["quote_requests"]
        assert Rows[0]["id"] == Q["id"] and Rows[0]["fulfillment"] is None
    finally:
        await H.Close()
