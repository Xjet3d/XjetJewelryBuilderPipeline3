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
        E1 = _Event("evt-1", O["id"], 1, parts=[{"display_no": "MP-10001-01", "summary": "0/2 passed QC", "file_state": "unavailable"}])
        R = await H.Client.post(Prefix + "/events", json={"events": [E1]}, headers=Service)
        assert R.status_code == 200 and R.json()["results"] == [{"event_id": "evt-1", "status": "applied", "result": "applied"}]
        State = (await H.Client.get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Admin)).json()
        assert State["projection"]["customer_status"] == "UNDER_REVIEW" and State["projection"]["parts"][0]["display_no"] == "MP-10001-01"
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


async def test_a_quote_request_is_the_same_requirement_before_and_after_it_becomes_an_order(tmp_path):
    H = _On(tmp_path)
    try:
        B, Q = await _Request(H)
        R = await H.Client.post(f"/api/admin/quote-requests/{Q['id']}/fulfillment/enroll", headers=Admin)
        assert R.status_code == 200 and R.json()["enrolled"]
        assert (await H.Client.post(f"/api/admin/quote-requests/Q-5001/fulfillment/enroll", headers=Admin)).json()["error"]["code"] == "already_enrolled"
        S = (await H.Client.get(Prefix + f"/requests/quote_request/{Q['id']}", headers=Service)).json()
        assert S["display_ref"] == "Q-5001" and S["quote_request"]["status"] == "new" and S["quote_request"]["order"] is None
        assert S["quote_request"]["payment"] == {"status": "not_applicable", "basis": None}
        L = S["lines"][0]
        assert L["quote_request_id"] == Q["id"] and L["material_id"] == "gold_18k_yellow" and L["size"] == {"ring_size": 7, "label": "US 7"} and L["quantity"] == 2
        assert L["model"]["state"] == "no_model"
        # The quote is sent and approved by the customer: it becomes an order, which inherits the enrollment
        from datetime import datetime, timedelta, timezone
        Offer = {"size": 7, "weight_g": 6.5, "price_per_g": 400, "unit_price": 2600, "quantity": 2, "message": "Hello Dana,\n\nHere is your quote.",
                 "valid_until": (datetime.now(timezone.utc).date() + timedelta(days=10)).isoformat()}
        assert (await H.Client.post(f"/api/admin/quote-requests/{Q['id']}/offer", json=Offer, headers=Admin)).status_code == 200
        C = (await H.Client.get(Prefix + "/changes?cursor=1", headers=Service)).json()["changes"]
        assert [X["reason"] for X in C] == ["status"] and C[0]["entity_type"] == "quote_request"            # new → quoted
        Token = _Link(H).split("token=")[1]
        R = await H.Client.post("/quote/approve", data={"token": Token, **Address, "shipping_method": "standard", "terms": "1"})
        assert R.status_code == 200 and "ORD-10001" in R.text
        C = (await H.Client.get(Prefix + "/changes?cursor=2", headers=Service)).json()["changes"]
        assert [(X["entity_type"], X["reason"]) for X in C] == [("quote_request", "converted")]
        S = (await H.Client.get(Prefix + f"/requests/quote_request/{Q['id']}", headers=Service)).json()
        OrderId = S["quote_request"]["order"]["id"]
        assert S["quote_request"]["order"]["ref"] == "ORD-10001" and S["quote_request"]["status"] == "approved"
        # Reading the order enrolls it as the quote's continuation, with the linkage on its line
        Os = (await H.Client.get(Prefix + f"/requests/order/{OrderId}", headers=Service)).json()
        assert Os["enrollment"]["inherited_from"] == Q["id"] and Os["lines"][0]["quote_request_id"] == Q["id"]
        assert Os["order"]["quote"]["ref"] == "Q-5001" and Os["order"]["payment"]["status"] == "pending"
        C = (await H.Client.get(Prefix + "/changes?cursor=3", headers=Service)).json()["changes"]
        assert [(X["entity_type"], X["kind"], X["reason"]) for X in C] == [("order", "enrolled", "inherited")]
        State = (await H.Client.get(f"/api/admin/orders/{OrderId}/fulfillment", headers=Admin)).json()
        assert State["enrolled"] and State["enrollment"]["inherited_from"] == Q["id"]
        # An approved quote can no longer be sent on its own; a new quote request can
        _B2, Q2 = await _Request(H, Prompt="Another signet")
        assert (await H.Client.post(f"/api/admin/quote-requests/{Q['id']}/fulfillment/enroll", headers=Admin)).json()["error"]["code"] == "already_enrolled"
        assert (await H.Client.post(f"/api/admin/quote-requests/{Q2['id']}/fulfillment/enroll", headers=Admin)).json()["enrolled"]
    finally:
        await H.Close()
