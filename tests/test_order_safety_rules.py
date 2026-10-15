"""The owner's decisions of 2026-10-15 after the end-to-end validation (Fulfillment's docs/next-steps-review.md):

G1 — a cancellation after the hand-over is Fulfillment's decision too. Cancelling an order with lines in Production needs
a reason; undoing it is refused until Operations rejected the cancellation in Fulfillment for every line sent (accepted,
not yet decided, or not yet received: it stays cancelled, each line says why) — found locally: an order set back to New
while Fulfillment cancelled its part could never be sent again. Undoing any cancellation needs a reason (ORD-10003 was
set back to New with none). The Admin is offered only the moves the server accepts; an undone cancellation takes
Fulfillment's progress again at once.

G9 — the customer's other orders that hold the same piece (same item, size and material) are named on the line, and the
hand-over goes only when the Admin confirmed exactly those orders, kept in the history; nothing is cancelled (on tron,
ORD-10005 re-ordered ORD-10003's ring and ORD-10004's charm)."""

from tests.test_fulfillment_bridge import Admin, Prefix, Service, _Enrolled, _Event, _On, _Placed, _Send
from tests.test_send_to_production_rules import _Design, _InBag, _Model, _Place, _Prepare, _State, _Status, _Tron


async def _Report(H, OrderId: str, Version: int, Type: str, **Public) -> None:
    """Fulfillment reporting on the order, as its events arrive (the service credential)."""
    R = await H.Client.post(Prefix + "/events", json={"events": [_Event(f"evt-{OrderId[-8:]}-{Version}", OrderId, Version, Type, **Public)]}, headers=Service)
    assert R.status_code == 200, R.text


def _Part(LineId: str, No: str = "MP-10001-001") -> dict:
    return {"line_id": LineId, "display_no": No, "part_id": "mp_" + No[-3:], "status": "ready", "summary": "0/1 passed QC"}


async def _SetStatus(H, OrderId: str, Status: str, Note: str = ""):
    return await H.Client.post(f"/api/admin/orders/{OrderId}/status", json={"status": Status, "note": Note}, headers=Admin)


async def test_a_cancellation_in_production_is_undone_only_after_fulfillment_rejected_it(tmp_path):
    H = _On(tmp_path)
    try:
        Did, O = await _Placed(H, Qty=1)
        await _Enrolled(H, Did, O)
        LineId = H.Svc.Orders.AdminGet(O["id"])["lines"][0]["id"]
        await _Report(H, O["id"], 1, "fulfillment.request_enrolled", parts=[_Part(LineId)])
        await _Report(H, O["id"], 2, "fulfillment.production_started", parts=[_Part(LineId)], order_status="production")
        assert H.Svc.Orders.AdminGet(O["id"])["status"] == "production"
        assert H.Svc.Orders.AdminGet(O["id"])["allowed_statuses"] == ["production", "cancelled"]       # progress is Fulfillment's
        # Cancelling an order with lines in Production needs a reason: Operations decide on it
        R = await _SetStatus(H, O["id"], "cancelled")
        assert R.status_code == 400 and R.json()["error"]["code"] == "reason_required", R.text
        assert (await _SetStatus(H, O["id"], "cancelled", "the customer changed their mind")).status_code == 200
        # Not yet decided in Fulfillment: it stays cancelled, and the line says why
        S = await _State(H, O["id"])
        assert S["reinstate"]["possible"] is False and [L["decision"] for L in S["reinstate"]["lines"]] == ["pending"]
        assert H.Svc.Orders.AdminGet(O["id"])["allowed_statuses"] == ["cancelled"]
        R = await _SetStatus(H, O["id"], "new", "the customer called back")
        assert R.status_code == 409 and R.json()["error"]["code"] == "cancellation_with_fulfillment", R.text
        assert "waits for Operations' decision" in R.json()["error"]["message"] and R.json()["error"]["lines"][0]["part"] == "MP-10001-001"
        # Operations reject it (production goes on): it can be undone — with a reason — and takes Fulfillment's progress again
        await _Report(H, O["id"], 3, "fulfillment.cancellation_rejected", parts=[_Part(LineId)], order_status="production",
                      cancellation={"part": "MP-10001-001", "decision": "rejected", "quantity": 0, "remaining": 1, "reason": "already printing"})
        S = await _State(H, O["id"])
        assert S["reinstate"]["possible"] is True and S["reinstate"]["lines"][0]["decision"] == "rejected"
        assert H.Svc.Orders.AdminGet(O["id"])["allowed_statuses"] == ["new", "payment_confirmed", "three_d_ready", "cancelled"]
        R = await _SetStatus(H, O["id"], "new")
        assert R.status_code == 400 and R.json()["error"]["code"] == "reason_required"
        R = await _SetStatus(H, O["id"], "new", "the customer called back: they still want it")
        assert R.status_code == 200 and R.json()["status"] == "production", R.text          # not New while the part is being made
        Steps = [(E["data"].get("from"), E["data"].get("to"), E["data"].get("note")) for E in R.json()["events"] if E["kind"] == "status"]
        assert Steps[-2:] == [("cancelled", "new", "the customer called back: they still want it"), ("new", "production", "Fulfillment progress")]
        # A decision about an earlier cancellation never counts for a later one
        assert (await _SetStatus(H, O["id"], "cancelled", "cancelled again")).status_code == 200
        R = await _SetStatus(H, O["id"], "new", "once more")
        assert R.status_code == 409 and R.json()["error"]["code"] == "cancellation_with_fulfillment"
    finally:
        await H.Close()


async def test_an_accepted_or_unseen_cancellation_keeps_the_order_cancelled(tmp_path):
    H = _On(tmp_path)
    try:
        Did, O = await _Placed(H, Qty=1)
        await _Enrolled(H, Did, O)
        # Fulfillment has not received the line yet: nothing to undo against
        assert (await _SetStatus(H, O["id"], "cancelled", "ordered twice")).status_code == 200
        S = await _State(H, O["id"])
        assert [L["decision"] for L in S["reinstate"]["lines"]] == ["not_received"]
        R = await _SetStatus(H, O["id"], "new", "a mistake")
        assert R.status_code == 409 and "has not received the line yet" in R.json()["error"]["message"]
        # Fulfillment accepted it (the part is cancelled there): it stays cancelled for good — a new order is the way
        LineId = H.Svc.Orders.AdminGet(O["id"])["lines"][0]["id"]
        await _Report(H, O["id"], 1, "fulfillment.cancellation_accepted", parts=[_Part(LineId)],
                      cancellation={"part": "MP-10001-001", "decision": "accepted", "quantity": 1, "remaining": 0, "reason": "ordered twice"})
        S = await _State(H, O["id"])
        assert S["reinstate"]["possible"] is False and S["reinstate"]["lines"][0]["decision"] == "accepted"
        R = await _SetStatus(H, O["id"], "new", "a mistake")
        assert R.status_code == 409 and "accepted the cancellation" in R.json()["error"]["message"] and "a new order" in R.json()["error"]["message"]
        assert H.Svc.Orders.AdminGet(O["id"])["status"] == "cancelled"
    finally:
        await H.Close()


async def test_undoing_any_cancellation_needs_a_reason_and_the_choices_are_what_the_server_accepts(tmp_path):
    H = _On(tmp_path)
    try:
        _Did, O = await _Placed(H, Qty=1)                                       # never handed over
        Every = ["new", "payment_confirmed", "three_d_ready", "production", "qc", "shipped", "completed", "cancelled"]
        assert H.Svc.Orders.AdminGet(O["id"])["allowed_statuses"] == Every
        assert (await _SetStatus(H, O["id"], "cancelled")).status_code == 200      # before the hand-over a reason stays optional
        R = await _SetStatus(H, O["id"], "new")
        assert R.status_code == 400 and R.json()["error"]["code"] == "reason_required"
        R = await _SetStatus(H, O["id"], "payment_confirmed", "cancelled by mistake")
        assert R.status_code == 200 and R.json()["status"] == "payment_confirmed"
        assert R.json()["events"][-1]["data"] == {"from": "cancelled", "to": "payment_confirmed", "note": "cancelled by mistake"}
        # Completed is final: nothing else is offered (Reopen for Production is its own action)
        D = H.Svc.Orders.SetStatus(O["id"], "completed", "test", "made outside the system", Force=True)
        assert D["allowed_statuses"] == ["completed"]
    finally:
        await H.Close()


async def test_a_duplicate_order_is_named_on_its_line_and_sent_only_when_confirmed(tmp_path):
    H = _On(tmp_path)
    try:
        Did, Cand = await _Design(H, "A rope band with a twist")
        await _InBag(H, Did, Cand, "silver", Size=7)
        First = await _Place(H, "dup-1")
        await _InBag(H, Did, Cand, "silver", Size=7)
        Second = await _Place(H, "dup-2")
        await _InBag(H, Did, Cand, "silver", Size=8)                            # another size: another piece
        Bigger = await _Place(H, "dup-3")
        await _InBag(H, Did, Cand, "silver", Size=7)
        Gone = await _Place(H, "dup-4")
        await _Status(H, Gone["id"], "cancelled", "ordered twice")              # a cancelled order is never a duplicate
        await _Model(H, Did, 7, "silver")
        L1, L2 = (H.Svc.Orders.AdminGet(X["id"])["lines"][0] for X in (First, Second))
        await _Prepare(H, First["id"], L1["id"])
        await _Prepare(H, Second["id"], L2["id"])
        S = await _State(H, First["id"])
        D = S["lines"][0]["duplicates"]
        assert [(X["order_ref"], X["status"], X["size_label"], X["sent"]) for X in D] == [(Second["ref"], "new", L1["size_label"], False)]
        Cus = {R["order_id"]: R["customization_id"] for R in H.Ctx.Db.All("SELECT order_id, customization_id FROM order_lines WHERE order_id IN (?, ?)",
                                                                           (First["id"], Second["id"]))}
        assert D[0]["same_design"] is (Cus[First["id"]] is not None and Cus[First["id"]] == Cus[Second["id"]])
        assert (await _State(H, Bigger["id"]))["lines"][0]["duplicates"] == []
        # Not confirmed: nothing is sent, and the refusal names the other order
        R = await H.Client.post(f"/api/admin/orders/{First['id']}/production", json={"line_ids": [L1["id"]]}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "duplicate_confirmation_required", R.text
        assert R.json()["error"]["blocked"][0]["duplicates"][0]["order_ref"] == Second["ref"]
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_line_production")["n"] == 0
        # Confirmed exactly: sent, and the history keeps the confirmation
        R = await H.Client.post(f"/api/admin/orders/{First['id']}/production", json={"line_ids": [L1["id"]], "acknowledged_duplicates": {L1["id"]: [Second["id"]]}},
                                headers=Admin)
        assert R.status_code == 200, R.text
        Sent = [E for E in H.Svc.Orders.AdminGet(First["id"])["events"] if E["kind"] == "production_sent"][-1]["data"]
        assert Sent["duplicates_acknowledged"] == [Second["ref"]]
        # The other order now sees this one in Production; a confirmation that misses an order is refused
        D = (await _State(H, Second["id"]))["lines"][0]["duplicates"]
        assert [(X["order_ref"], X["sent"]) for X in D] == [(First["ref"], True)]
        R = await H.Client.post(f"/api/admin/orders/{Second['id']}/production", json={"line_ids": [L2["id"]], "acknowledged_duplicates": {L2["id"]: ["ord_other"]}},
                                headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "duplicate_confirmation_required"
        # Another customer's order with the same piece is not a duplicate
        H.Ctx.Db.Execute("UPDATE orders SET owner_account_id = 'acc_someone_else', customer_json = json_set(customer_json, '$.email', 'someone.else@atelier-customer.co.il') "
                         "WHERE id = ?", (Second["id"],))
        assert (await _State(H, Second["id"]))["lines"][0]["duplicates"] == []
        await _Send(H, Second["id"])
    finally:
        await H.Close()


async def test_tron_s_orders_name_their_duplicates(tmp_path):
    """tron's ORD-10001 … ORD-10005 rebuilt (tests/test_send_to_production_rules.py): ORD-10005 holds ORD-10003's ring and
    ORD-10004's charm — each pair named both ways; ORD-10001 and ORD-10002 share a design but not a size or a material."""
    H = _On(tmp_path)
    try:
        T = await _Tron(H)
        O = T["orders"]
        Seen = {}
        for Ref in O:
            for L in (await _State(H, O[Ref]["id"]))["lines"]:
                Seen[(Ref, L["item_ref"])] = [X["order_ref"] for X in L["duplicates"]]
        A, B, C = (T["lines"][R][0]["ring_id"] for R in ("ORD-10001", "ORD-10003", "ORD-10004"))
        assert Seen[("ORD-10001", A)] == [] and Seen[("ORD-10002", A)] == []
        assert Seen[("ORD-10003", B)] == ["ORD-10005"] and Seen[("ORD-10005", B)] == ["ORD-10003"]
        assert Seen[("ORD-10004", C)] == ["ORD-10005"] and Seen[("ORD-10005", C)] == ["ORD-10004"]
    finally:
        await H.Close()
