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
from tests.test_orders import Address, Customer, _InBag, _Order
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


async def test_off_by_default_nothing_of_the_integration_exists(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        _Did, O = await _Placed(H)
        for P in (Prefix + "/identity", Prefix + "/changes", Prefix + f"/requests/order/{O['id']}"):
            assert (await H.Client.get(P, headers=Service)).status_code == 404, P
        assert (await H.Client.post(Prefix + "/events", json={"events": []}, headers=Service)).status_code == 404
        assert (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json() == {"enabled": False}
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/fulfillment/enroll", headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "fulfillment_disabled"
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
        assert State["enabled"] and State["eligible"] and not State["enrolled"] and State["projection"] is None
        assert (await H.Client.get(Prefix + f"/requests/order/{O['id']}", headers=Service)).status_code == 404     # not enrolled: nothing to read
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/fulfillment/enroll", json={"note": "  rush  "}, headers=Admin)
        assert R.status_code == 200, R.text
        State = R.json()
        assert State["enrolled"] and State["awaiting_sync"] and State["enrollment"]["enrolled_by"] == "developer-key" and State["enrollment"]["note"] == "rush"
        assert State["enrollment"]["source_version"] == 1 and not State["eligible"]
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/fulfillment/enroll", headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "already_enrolled"
        Events = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()["events"]
        assert Events[-1]["kind"] == "fulfillment" and Events[-1]["data"]["action"] == "enrolled" and Events[-1]["by"] == "developer-key"
        # The feed: one change, a cursor that Fulfillment keeps; reading it records the pull
        C = (await H.Client.get(Prefix + "/changes?cursor=0", headers=Service)).json()
        assert [ (X["seq"], X["kind"], X["reason"], X["source_version"]) for X in C["changes"]] == [(1, "enrolled", "admin", 1)]
        assert C["next_cursor"] == 1 and not C["has_more"] and C["environment_id"]
        assert (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()["awaiting_sync"]      # read with cursor 0: not yet
        assert (await H.Client.get(Prefix + "/changes?cursor=1", headers=Service)).json()["changes"] == []
        assert not (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()["awaiting_sync"]
        # The snapshot: commercial facts, no notes, no history; the model is honestly absent
        S = (await H.Client.get(Prefix + f"/requests/order/{O['id']}", headers=Service)).json()
        assert S["entity_type"] == "order" and S["display_ref"] == "ORD-10001" and S["source_version"] == 1 and S["enrollment"]["state"] == "enrolled"
        Od = S["order"]
        assert Od["status"] == "new" and Od["payment"] == {"status": "pending", "basis": None, "provider": None, "ref": None, "updated_at": Od["placed_at"]}
        assert Od["customer"] == {"name": "Dana Levi", "email": "dana@example.com"} and Od["address"]["city"] == "Tel Aviv"
        assert len(Od["address_version"]) == 16 and Od["cancellation"] == {"state": "none", "at": None, "reason": None} and Od["promised_delivery"] is None
        assert "notes" not in S and "events" not in S and "notes" not in Od
        L = S["lines"][0]
        assert (L["item_ref"], L["product_type"], L["material_id"], L["size"], L["quantity"]) == ("R-1001-A", "ring", "silver", {"ring_size": 7, "label": "US 7"}, 2)
        assert L["image"] == {"design_id": Did, "candidate_id": L["candidate_id"]} and L["quote_request_id"] is None
        assert L["model"]["available"] is False and L["model"]["state"] == "no_model" and "paid" in L["model"]["note"]
        # Commercial changes reach the feed in the transaction of the change; notes and legacy statuses do not
        await H.Client.post(f"/api/admin/orders/{O['id']}/note", json={"note": "gift wrap"}, headers=Admin)
        assert (await H.Client.get(Prefix + "/changes?cursor=1", headers=Service)).json()["changes"] == []
        await H.Client.post(f"/api/admin/orders/{O['id']}/payment", json={"status": "paid", "note": "transfer", "ref": "TR-1"}, headers=Admin)
        C = (await H.Client.get(Prefix + "/changes?cursor=1", headers=Service)).json()["changes"]
        assert [(X["kind"], X["reason"], X["source_version"]) for X in C] == [("updated", "payment", 2), ("updated", "status", 3)]   # paid → payment confirmed
        S = (await H.Client.get(Prefix + f"/requests/order/{O['id']}", headers=Service)).json()
        assert S["source_version"] == 3 and S["order"]["payment"]["status"] == "paid" and S["order"]["payment"]["basis"] == "paid" and S["order"]["payment"]["ref"] == "TR-1"
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
        _Did, O = await _Placed(H)
        await H.Client.post(f"/api/admin/orders/{O['id']}/fulfillment/enroll", headers=Admin)
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
        R = await H.Client.post(f"/api/admin/orders/{O2['id']}/fulfillment/enroll", headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "not_eligible"                     # already in the legacy production flow
    finally:
        await H.Close()


async def test_the_production_file_follows_the_3d_result_and_is_served_with_its_checksum(tmp_path):
    H = _On(tmp_path)
    try:
        Did, O = await _Placed(H, "A twisted band", Size=10, Material="silver", Qty=1)
        await H.Client.post(f"/api/admin/orders/{O['id']}/fulfillment/enroll", headers=Admin)
        Line = (await H.Client.get(Prefix + f"/requests/order/{O['id']}", headers=Service)).json()["lines"][0]
        R = await H.Client.post(Prefix + "/assets/production-stl", json={"line_id": Line["line_id"]}, headers=Service)
        assert R.status_code == 200 and R.json() == {"status": "unavailable", "reason": "no_model", "message": R.json()["message"]}
        # The Admin generates the model on the session page (mock Hi3D here) — at another size and material
        T = await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 7, "material_id": "vermeil"}, headers=Admin)
        assert T.status_code == 200, T.text
        await H.Idle()
        C = (await H.Client.get(Prefix + "/changes?cursor=1", headers=Service)).json()["changes"]
        assert C and all(X["reason"] == "model" for X in C)                                                  # the 3D result reached the feed
        M = (await H.Client.get(Prefix + f"/requests/order/{O['id']}", headers=Service)).json()["lines"][0]["model"]
        assert M["state"] == "not_prepared" and not M["available"] and M["latest_other"]["size"] == 7
        # Fulfillment asks for the file: the ordered size and material are prepared by arithmetic, then exported
        R = await H.Client.post(Prefix + "/assets/production-stl", json={"line_id": Line["line_id"]}, headers=Service)
        assert R.status_code == 200, R.text
        await H.Idle()
        M = (await H.Client.get(Prefix + f"/requests/order/{O['id']}", headers=Service)).json()["lines"][0]["model"]
        assert M["available"] and M["state"] == "complete" and len(M["raw_sha256"]) == 64 and M["geometry"]["volume_mm3"] > 0 and M["production_size"] == 10
        assert M["weight"]["g"] > 0 and M["weight"]["density_g_cm3"] == 10.36 and "calculated" in M["weight"]["source"]
        R = await H.Client.post(Prefix + "/assets/production-stl", json={"line_id": Line["line_id"]}, headers=Service)
        assert R.status_code == 200 and R.json()["status"] in ("queued", "running", "done")
        Sid, Jid = R.json()["session_3d_id"], R.json()["job_id"]
        await H.Idle()
        F = await H.Client.get(Prefix + f"/assets/production-stl/{Sid}/{Jid}", headers=Service)
        assert F.status_code == 200, F.text
        assert F.headers["content-type"].startswith("model/stl") and len(F.content) > 84 and F.headers["x-content-sha256"] == hashlib.sha256(F.content).hexdigest()
        assert int(F.headers["x-content-bytes"]) == len(F.content)
        assert (await H.Client.get(Prefix + f"/assets/production-stl/{Sid}/{Jid}")).status_code == 403
        assert (await H.Client.get(Prefix + f"/assets/production-stl/{Sid}/nope", headers=Service)).status_code == 404
        assert len(H.Provider.SubmissionsFor(__import__("p3.providers.endpoints", fromlist=["Mesh"]).Mesh)) == 1       # one Hi3D request, the Admin's
    finally:
        await H.Close()


async def test_events_are_idempotent_stale_ones_are_ignored_and_conflicts_are_recorded(tmp_path):
    H = _On(tmp_path)
    try:
        _Did, O = await _Placed(H)
        await H.Client.post(f"/api/admin/orders/{O['id']}/fulfillment/enroll", headers=Admin)
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
        assert (await H.Client.post(f"/api/admin/orders/{OrderId}/fulfillment/enroll", headers=Admin)).json()["enrolled"]
        S = (await H.Client.get(Prefix + f"/requests/order/{OrderId}", headers=Service)).json()
        assert S["order"]["quote"]["ref"] == "Q-5001" and S["lines"][0]["quote_request_id"] == Q["id"] and S["enrollment"]["inherited_from"] is None
        assert S["lines"][0]["material_id"] == "gold_18k_yellow" and S["lines"][0]["quantity"] == 2 and S["order"]["payment"]["status"] == "pending"
        C = (await H.Client.get(Prefix + "/changes?cursor=0", headers=Service)).json()["changes"]
        assert [(X["entity_type"], X["kind"], X["reason"]) for X in C] == [("order", "enrolled", "admin")]
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
        _Did, O = await _Placed(H, Qty=2)
        LineId = H.Svc.Orders.AdminGet(O["id"])["lines"][0]["id"]
        Url = Prefix + "/release-checks"
        assert (await H.Client.post(Url, json={"items": [{"line_id": LineId, "quantity": 2}]})).status_code == 403
        R = await H.Client.post(Url, json={"items": [{"line_id": LineId, "quantity": 2}]}, headers=Service)
        assert R.status_code == 200 and R.json()["items"][0]["blockers"][0]["code"] == "LINE_NOT_FOUND"        # not enrolled: nothing is told
        await H.Client.post(f"/api/admin/orders/{O['id']}/fulfillment/enroll", headers=Admin)
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


async def test_shipping_checks_answer_the_authoritative_address_and_its_version_live(tmp_path):
    """Batch 7 (spec §23, C05): Fulfillment packs against the address Atelier holds now; a changed address has a new version."""
    H = _On(tmp_path)
    try:
        _Did, O = await _Placed(H, Qty=1)
        Url = Prefix + "/shipping-checks"
        assert (await H.Client.post(Url, json={"order_id": O["id"]})).status_code == 403
        R = (await H.Client.post(Url, json={"order_id": O["id"]}, headers=Service)).json()
        assert R["ok"] is False and R["blockers"][0]["code"] == "ORDER_NOT_FOUND" and "address" not in R                # not enrolled: nothing is told
        await H.Client.post(f"/api/admin/orders/{O['id']}/fulfillment/enroll", headers=Admin)
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
        _Did, O = await _Placed(H, Qty=2)
        LineId = H.Svc.Orders.AdminGet(O["id"])["lines"][0]["id"]
        Url = f"/api/admin/orders/{O['id']}/fulfillment/commands"
        R = await H.Client.post(Url, json={"action": "cancel_line", "line_id": LineId, "quantity": 1, "reason": "one ring fewer"}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "not_enrolled"
        await H.Client.post(f"/api/admin/orders/{O['id']}/fulfillment/enroll", headers=Admin)
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
        assert Cmd["payload"]["quantity"] == 1 and Cmd["payload"]["line_id"] == LineId and Cmd["expected_version"] == 1 and Cmd["decided_at"] is None
        Again = (await H.Client.post(Url, json={"action": "cancel_line", "line_id": LineId, "quantity": 2, "reason": "again"}, headers=Admin)).json()
        assert Again["id"] == Cmd["id"] and Again["payload"]["quantity"] == 1                   # a repeated click: the same pending request
        State = (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()
        assert [C["state"] for C in State["commands"]] == ["pending"] and State["awaiting_sync"] and State["command_actions"] == ["cancel_line"]
        # The feed carries it with its payload; Fulfillment answers with an event; the first answer stands
        C = (await H.Client.get(Prefix + "/changes?cursor=1", headers=Service)).json()["changes"]
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
        _Did, O = await _Placed(H)
        await H.Client.post(f"/api/admin/orders/{O['id']}/fulfillment/enroll", headers=Admin)
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
        _D2, O2 = await _Placed(H, "Band two", Click="c2")
        await H.Client.post(f"/api/admin/orders/{O2['id']}/fulfillment/enroll", headers=Admin)
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
