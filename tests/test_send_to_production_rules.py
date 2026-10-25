"""Send to Production — the owner's decisions of 2026-10-14 (Fulfillment's decision I-35), after the audit of tron's
orders (docs/send-to-production-audit.md in the Fulfillment repository).

A line's print file (its 3D version, result, link and state) and its order's hand-over (status, pilot allowlist, an
earlier hand-over) are judged apart and named together; a valid STL is never called "required". A ready STL not yet
linked to its line is linked by the hand-over — only the exact file the Admin confirmed, recorded in the history, never
over an existing link and never when several different files exist. Preparation stays on the line (no new AI model).
A completed order that was never handed over can be reopened for Production (Admin only, with a reason); a cancelled
order stays protected. The first test rebuilds tron's ORD-10001 … ORD-10005 as they were found."""

import httpx
import pytest

from tests.conftest import OfferCharmSizes
from tests.test_charm_3d import Prices
from tests.test_fulfillment_bridge import Admin, AdminKey, Prefix, Service, _On, _Send
from tests.test_orders import _Order


async def _Design(H, Prompt: str, Charm: bool = False) -> tuple[str, str]:
    B = await H.NewDesign(Prompt, **({"product": "charm"} if Charm else {}))
    return B["design_id"], B["candidates"][0]["id"]


async def _InBag(H, Did: str, Cand: str, Material: str, Size=None, Charm=None, Qty: int = 1) -> None:
    C = await H.Proceed(Did, Cand)
    await H.Idle()
    R = await H.Client.patch(f"/api/customizations/{C['id']}", json={"material_id": Material, "quantity": Qty,
                                                                      **({"charm_size": Charm} if Charm else {"ring_size": Size})})
    assert R.status_code == 200, R.text
    assert (await H.Client.post("/api/bag", json={"customization_id": C["id"]})).status_code == 200


async def _Place(H, Click: str) -> dict:
    R = await H.Client.post("/api/orders", json=_Order(client_request_id=Click))
    assert R.status_code == 200, R.text
    return R.json()


async def _Model(H, Did: str, Size, Material: str) -> None:
    """A result made from the design's 3D page — for no order line in particular (as on tron at 18:04)."""
    R = await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": Size, "material_id": Material}, headers=Admin)
    assert R.status_code == 200, R.text
    await H.Idle()


async def _Prepare(H, OrderId: str, LineId: str, Expect: int = 200) -> dict:
    R = await H.Client.post(f"/api/admin/orders/{OrderId}/lines/{LineId}/3d", headers=Admin)
    assert R.status_code == Expect, R.text
    await H.Idle()
    return R.json()


async def _Status(H, OrderId: str, Status: str, Note: str = "") -> None:
    R = await H.Client.post(f"/api/admin/orders/{OrderId}/status", json={"status": Status, "note": Note}, headers=Admin)
    assert R.status_code == 200, R.text


async def _State(H, OrderId: str) -> dict:
    R = await H.Client.get(f"/api/admin/orders/{OrderId}/fulfillment", headers=Admin)
    assert R.status_code == 200, R.text
    return R.json()


def _Line(State: dict, ItemRef: str) -> dict:
    return next(X for X in State["lines"] if X["item_ref"] == ItemRef)


def _Meshes(H) -> int:
    return H.Ctx.Db.One("SELECT COUNT(*) AS n FROM meshes")["n"]


async def _Tron(H) -> dict:
    """tron's orders as the audit found them (2026-10-14 18:15 UTC), with the same order numbers and the same shared
    designs: A (R-1012-D) in ORD-10001 and ORD-10002, B (R-1019-R1C) in ORD-10003 and ORD-10005, the charm C (C-1010-R1C)
    in ORD-10004 and ORD-10005. The charm is 20 mm silver here (tron's is 14 mm stainless): the test catalogue's sizes."""
    assert (await H.Client.post("/api/admin/login", json={"key": AdminKey})).status_code == 200
    await OfferCharmSizes(H, AdminKey)
    assert (await H.Client.put("/api/admin/charm-prices", json={"materials": Prices}, headers=Admin)).status_code == 200
    A, CandA = await _Design(H, "A fil twist band")
    B, CandB = await _Design(H, "A rope band with a twist")
    C, CandC = await _Design(H, "A crescent moon charm with a small star", Charm=True)
    await _InBag(H, A, CandA, "stainless_steel", Size=8.5)
    O1 = await _Place(H, "o1")
    await _InBag(H, A, CandA, "silver", Size=10)
    O2 = await _Place(H, "o2")
    await _InBag(H, B, CandB, "silver", Size=7)
    O3 = await _Place(H, "o3")
    await _InBag(H, C, CandC, "silver", Charm=20)
    O4 = await _Place(H, "o4")
    await _InBag(H, C, CandC, "silver", Charm=20)
    await _InBag(H, B, CandB, "silver", Size=7)
    O5 = await _Place(H, "o5")
    assert [O["ref"] for O in (O1, O2, O3, O4, O5)] == ["ORD-10001", "ORD-10002", "ORD-10003", "ORD-10004", "ORD-10005"]
    L = {O["ref"]: H.Svc.Orders.AdminGet(O["id"])["lines"] for O in (O1, O2, O3, O4, O5)}
    # ORD-10001: prepared, linked — sent (the owner's walkthrough)
    await _Model(H, A, 8.5, "stainless_steel")
    await _Prepare(H, O1["id"], L["ORD-10001"][0]["id"])
    await _Send(H, O1["id"])
    # ORD-10002: prepared and linked, then marked Completed by hand without production (2026-10-08)
    await _Model(H, A, 10, "silver")
    await _Prepare(H, O2["id"], L["ORD-10002"][0]["id"])
    await _Status(H, O2["id"], "completed")
    # ORD-10003: design B measured at US 8.5 and US 10 only; cancelled, then set back to New (today 18:07)
    await _Model(H, B, 8.5, "silver")
    await _Model(H, B, 10, "silver")
    await _Status(H, O3["id"], "cancelled", "cancelled by the Admin")
    await _Status(H, O3["id"], "new", "set back to New by the Admin")          # undoing a cancellation needs a reason (G1, 2026-10-15)
    # ORD-10004 / ORD-10005: the charm's result at the ordered size, made from the design page — linked to no line;
    # ORD-10004 set to Payment confirmed by hand (no payment recorded)
    await _Model(H, C, 20, "silver")
    await _Status(H, O4["id"], "payment_confirmed")
    return {"orders": {O["ref"]: O for O in (O1, O2, O3, O4, O5)}, "lines": L, "designs": {"A": A, "B": B, "C": C}}


async def test_tron_s_orders_get_their_true_verdict_and_a_valid_stl_is_never_required(tmp_path):
    H = _On(tmp_path, FulfillmentOrderAllowlist=("ORD-10001", "ORD-10008"))
    try:
        T = await _Tron(H)
        O = T["orders"]
        S = {Ref: await _State(H, O[Ref]["id"]) for Ref in O}
        Seen = {(Ref, X["item_ref"]): (X["stl"]["state"], X["handover"]["state"], X["eligible"], X["message"]) for Ref in S for X in S[Ref]["lines"]}
        A, B, C = (S["ORD-10001"]["lines"][0]["item_ref"], S["ORD-10003"]["lines"][0]["item_ref"], S["ORD-10004"]["lines"][0]["item_ref"])
        assert Seen[("ORD-10001", A)] == ("ready", "sent", False, "In Production")
        assert Seen[("ORD-10002", A)] == ("ready", "order_completed", False, "STL ready · Order completed — production disabled")
        assert Seen[("ORD-10003", B)] == ("needs_preparation", "not_enabled", False,
                                          "STL not yet prepared at US 7 · Sterling Silver — the 3D model exists · Production hand-over restricted by the pilot allowlist")
        assert Seen[("ORD-10004", C)] == ("ready_unlinked", "not_enabled", False,
                                          "STL ready — not yet linked to this line · Production hand-over restricted by the pilot allowlist")
        assert Seen[("ORD-10005", C)][:2] == ("ready_unlinked", "not_enabled") and Seen[("ORD-10005", B)][:2] == ("needs_preparation", "not_enabled")
        # The audit's complaint: no line with a valid STL is ever said to need a print file — not on the page, not from the server
        assert not any("required" in M.lower() and "preparation required" not in M for (_S, _H, _E, M) in Seen.values())
        assert all(S[Ref]["lines"][0]["print_ready"] for Ref in ("ORD-10001", "ORD-10002", "ORD-10004"))
        # The order-level words, and the pilot said as such
        assert S["ORD-10002"]["ineligible_reason"] == "Order completed — production disabled (Reopen for Production first)"
        assert S["ORD-10002"]["reopen"] == {"possible": True, "to": "new", "why_not": None}
        assert S["ORD-10003"]["not_enabled"] and S["ORD-10003"]["pilot"] == {"active": True, "orders": ["ORD-10001", "ORD-10008"], "allowed": False}
        for Ref in ("ORD-10002", "ORD-10003", "ORD-10004"):
            R = await H.Client.post(f"/api/admin/orders/{O[Ref]['id']}/production", json={"line_ids": [S[Ref]["lines"][0]["line_id"]]}, headers=Admin)
            assert R.status_code == 409 and R.json()["error"]["code"] == "not_eligible" and "Print file required" not in R.json()["error"]["message"]
        assert "Order completed" in (await H.Client.post(f"/api/admin/orders/{O['ORD-10002']['id']}/production",
                                                         json={"line_ids": [S["ORD-10002"]["lines"][0]["line_id"]]}, headers=Admin)).json()["error"]["message"]
        # Nothing of this changed an order, a line or the feed
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_line_production")["n"] == 1
    finally:
        await H.Close()


async def test_with_the_owner_s_go_the_blocked_orders_reach_production_the_right_way(tmp_path):
    """The same orders, then each one's own fix: ORD-10002 reopened, ORD-10003 and ORD-10005's ring prepared on the line
    (no new AI model), ORD-10004 and ORD-10005's charm linked by the hand-over after the confirmation; the pilot allowlist
    lifted here only to show the path (on tron it stays until the owner approves)."""
    H = _On(tmp_path, FulfillmentOrderAllowlist=("ORD-10001", "ORD-10008"))
    try:
        T = await _Tron(H)
        O, Lines = T["orders"], T["lines"]
        Url = lambda Ref: f"/api/admin/orders/{O[Ref]['id']}/reopen-for-production"          # noqa: E731
        # Reopen for Production: Admin only, a reason, a completed order only — back to the status it had before
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=H.App), base_url="http://p3.test" + H.Base) as Anyone:   # no Admin key, no cookie
            assert (await Anyone.post(Url("ORD-10002"), json={"reason": "marked completed by mistake"})).status_code in (401, 403)
        R = await H.Client.post(Url("ORD-10002"), json={"reason": " "}, headers=Admin)
        assert R.status_code == 400 and R.json()["error"]["code"] == "reason_required"
        R = await H.Client.post(Url("ORD-10003"), json={"reason": "try"}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "not_reopenable" and "only a completed order" in R.json()["error"]["message"]
        R = await H.Client.post(Url("ORD-10002"), json={"reason": "marked completed by mistake — it still has to be made"}, headers=Admin)
        assert R.status_code == 200, R.text
        S2 = R.json()
        assert H.Svc.Orders.AdminGet(O["ORD-10002"]["id"])["status"] == "new" and S2["reopen"] is None
        assert _Line(S2, Lines["ORD-10002"][0]["ring_id"])["message"] == "STL ready · Production hand-over restricted by the pilot allowlist"
        Ev = [E for E in H.Svc.Orders.AdminGet(O["ORD-10002"]["id"])["events"] if E["kind"] == "status"][-1]
        assert Ev["data"]["reopened_for_production"] and Ev["data"]["from"] == "completed" and Ev["data"]["to"] == "new"
        assert Ev["data"]["reason"] == "marked completed by mistake — it still has to be made" and Ev["by"] == "developer-key"
        assert (await H.Client.post(Url("ORD-10002"), json={"reason": "again"}, headers=Admin)).status_code == 409    # once only
        # The owner lifts the pilot allowlist (simulated here: normal operation is an empty list on both sides)
        H.Settings.FulfillmentOrderAllowlist = ()
        Before = _Meshes(H)
        # ORD-10002: its linked STL, sent
        S = await _Send(H, O["ORD-10002"]["id"])
        assert S["lines"][0]["production"]["sent"]
        # ORD-10003: Prepare STL from the line — scaled from the existing model, no new AI model — then sent
        R = await _Prepare(H, O["ORD-10003"]["id"], Lines["ORD-10003"][0]["id"])
        assert R["prepared"] and _Meshes(H) == Before
        L3 = _Line(await _State(H, O["ORD-10003"]["id"]), Lines["ORD-10003"][0]["ring_id"])
        assert (L3["stl"]["state"], L3["eligible"], L3["pinned"]) == ("ready", True, True)
        # G9 (2026-10-15): ORD-10005 holds the same ring (the same customer ordered it again) — named, and sent only once confirmed
        L3Id = Lines["ORD-10003"][0]["id"]
        R = await H.Client.post(f"/api/admin/orders/{O['ORD-10003']['id']}/production", json={"line_ids": [L3Id]}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "duplicate_confirmation_required"
        assert [D["order_ref"] for D in R.json()["error"]["blocked"][0]["duplicates"]] == ["ORD-10005"]
        R = await H.Client.post(f"/api/admin/orders/{O['ORD-10003']['id']}/production", json={"line_ids": [L3Id], "acknowledged_duplicates": {L3Id: [O["ORD-10005"]["id"]]}},
                                headers=Admin)
        assert R.status_code == 200 and R.json()["lines"][0]["production"]["sent"], R.text
        # ORD-10004: the ready STL is named and must be confirmed; the hand-over links exactly that one, in the history
        S4 = await _State(H, O["ORD-10004"]["id"])
        L4 = S4["lines"][0]
        assert L4["eligible"] and L4["stl"]["state"] == "ready_unlinked" and not L4["pinned"]
        Link = L4["stl"]["link"]
        assert (Link["size_label"], Link["material_label"], Link["version_label"]) == ("20 mm", "Sterling Silver", "v1 · Hi3D")
        R = await H.Client.post(f"/api/admin/orders/{O['ORD-10004']['id']}/production", json={"line_ids": [L4["line_id"]]}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "link_confirmation_required" and R.json()["error"]["blocked"][0]["link"]["id"] == Link["id"]
        assert H.Ctx.Db.One("SELECT three_d_id FROM order_lines WHERE id = ?", (L4["line_id"],))["three_d_id"] is None           # nothing linked by a refusal
        R = await H.Client.post(f"/api/admin/orders/{O['ORD-10004']['id']}/production", json={"line_ids": [L4["line_id"]], "links": {L4["line_id"]: "s3d_other"}}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "link_confirmation_required"                                # a stale or wrong confirmation
        assert [D["order_ref"] for D in L4["duplicates"]] == ["ORD-10005"]                                              # the same charm (G9)
        R = await H.Client.post(f"/api/admin/orders/{O['ORD-10004']['id']}/production", json={"line_ids": [L4["line_id"]], "links": {L4["line_id"]: Link["id"]},
                                                                                         "acknowledged_duplicates": {L4["line_id"]: [O["ORD-10005"]["id"]]}}, headers=Admin)
        assert R.status_code == 200, R.text
        S4 = R.json()
        assert S4["lines"][0]["production"]["sent"] and S4["lines"][0]["pinned"] and S4["lines"][0]["production"]["pinned"]["three_d_id"] == Link["id"]
        Linked = [E for E in H.Svc.Orders.AdminGet(O["ORD-10004"]["id"])["events"] if E["kind"] == "3d_pinned"]
        assert len(Linked) == 1 and Linked[0]["data"]["linked_at_handover"] and Linked[0]["data"]["session_3d_id"] == Link["id"]
        # ORD-10005: both lines now have a ready STL — the charm's made from the design page, the ring's made by ORD-10003's
        # preparation (the same design, size and material) — each linked at the hand-over to the file the Admin confirmed
        S5 = await _State(H, O["ORD-10005"]["id"])
        Charm, Ring = S5["lines"][0], S5["lines"][1]
        assert Charm["stl"]["state"] == "ready_unlinked" and Ring["stl"]["state"] == "ready_unlinked" and Charm["eligible"] and Ring["eligible"]
        assert Ring["stl"]["link"]["id"] == H.Ctx.Db.One("SELECT three_d_id FROM order_lines WHERE id = ?", (Lines["ORD-10003"][0]["id"],))["three_d_id"]
        Links = {Charm["line_id"]: Charm["stl"]["link"]["id"], Ring["line_id"]: Ring["stl"]["link"]["id"]}
        # Both pieces are already in Production for the same customer (ORD-10004's charm, ORD-10003's ring): named, confirmed (G9)
        assert [(D["order_ref"], D["sent"]) for D in Charm["duplicates"]] == [("ORD-10004", True)] and [(D["order_ref"], D["sent"]) for D in Ring["duplicates"]] == [("ORD-10003", True)]
        Seen = {Charm["line_id"]: [O["ORD-10004"]["id"]], Ring["line_id"]: [O["ORD-10003"]["id"]]}
        R = await H.Client.post(f"/api/admin/orders/{O['ORD-10005']['id']}/production", json={"line_ids": [Charm["line_id"], Ring["line_id"]], "links": Links,
                                                                                         "acknowledged_duplicates": Seen}, headers=Admin)
        assert R.status_code == 200, R.text
        assert [X["production"]["sent"] for X in R.json()["lines"]] == [True, True] and _Meshes(H) == Before
        # One hand-over per line, ever: ORD-10002 cannot be reopened again, nor sent twice
        assert (await H.Client.post(Url("ORD-10002"), json={"reason": "again"}, headers=Admin)).status_code == 409
        R = await H.Client.post(f"/api/admin/orders/{O['ORD-10002']['id']}/production", json={"line_ids": [Lines["ORD-10002"][0]["id"]]}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "already_sent"
        # Fulfillment reads one enrollment per order, in order
        C = (await H.Client.get(Prefix + "/changes?cursor=0", headers=Service)).json()["changes"]
        assert sorted({X["entity_id"] for X in C if X["kind"] == "enrolled"}) == sorted(O[R_]["id"] for R_ in ("ORD-10001", "ORD-10002", "ORD-10003", "ORD-10004", "ORD-10005"))
    finally:
        await H.Close()


async def test_a_completed_order_that_went_through_production_is_never_reopened(tmp_path):
    H = _On(tmp_path)
    try:
        A, Cand = await _Design(H, "A fil twist band")
        await _InBag(H, A, Cand, "silver", Size=7)
        O = await _Place(H, "c1")
        await _Model(H, A, 7, "silver")
        await _Send(H, O["id"], Expect=409)                                    # unlinked: the confirmation is needed …
        L = (await _State(H, O["id"]))["lines"][0]
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/production", json={"line_ids": [L["line_id"]], "links": {L["line_id"]: L["stl"]["link"]["id"]}}, headers=Admin)
        assert R.status_code == 200, R.text
        # … sent; Fulfillment later completes it (its projection writes `completed`)
        H.Ctx.Db.Execute("UPDATE orders SET status = 'completed' WHERE id = ?", (O["id"],))
        S = await _State(H, O["id"])
        assert S["reopen"] == {"possible": False, "to": None, "why_not": "it went through Production already — a remake is a new order"}
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/reopen-for-production", json={"reason": "remake"}, headers=Admin)
        assert R.status_code == 409 and "a remake is a new order" in R.json()["error"]["message"]
        # A completed order reopened goes back to the status it had before completion
        await _InBag(H, A, Cand, "silver", Size=7)
        O2 = await _Place(H, "c2")
        await _Prepare(H, O2["id"], H.Svc.Orders.AdminGet(O2["id"])["lines"][0]["id"])
        await _Status(H, O2["id"], "payment_confirmed")
        await _Status(H, O2["id"], "completed")
        R = await H.Client.post(f"/api/admin/orders/{O2['id']}/reopen-for-production", json={"reason": "produce after all"}, headers=Admin)
        assert R.status_code == 200 and H.Svc.Orders.AdminGet(O2["id"])["status"] == "payment_confirmed"
    finally:
        await H.Close()


async def test_a_cancelled_order_stays_protected_whatever_its_stl(tmp_path):
    H = _On(tmp_path)
    try:
        A, Cand = await _Design(H, "A fil twist band")
        await _InBag(H, A, Cand, "silver", Size=7)
        O = await _Place(H, "x1")
        await _Model(H, A, 7, "silver")
        await _Prepare(H, O["id"], H.Svc.Orders.AdminGet(O["id"])["lines"][0]["id"])
        await _Status(H, O["id"], "cancelled", "the customer changed their mind")
        S = await _State(H, O["id"])
        L = S["lines"][0]
        assert (L["stl"]["state"], L["handover"]["state"], L["eligible"]) == ("ready", "order_cancelled", False)
        assert L["message"] == "STL ready · Order cancelled — production disabled" and S["reopen"] is None
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/production", json={"line_ids": [L["line_id"]]}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "not_eligible" and "Order cancelled" in R.json()["error"]["message"]
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/reopen-for-production", json={"reason": "please"}, headers=Admin)
        assert R.status_code == 409 and "only a completed order" in R.json()["error"]["message"]
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_enrollments")["n"] == 0
    finally:
        await H.Close()


async def test_different_results_are_never_chosen_silently_and_a_link_is_never_replaced(tmp_path):
    H = _On(tmp_path)
    try:
        A, Cand = await _Design(H, "A fil twist band")
        await _InBag(H, A, Cand, "silver", Size=7)
        O = await _Place(H, "d1")
        LineId = H.Svc.Orders.AdminGet(O["id"])["lines"][0]["id"]
        # Two results of the same size and material, the very same file: no choice to make — the newest is linked
        await _Model(H, A, 7, "silver")
        await _Model(H, A, 7, "silver")
        L = (await _State(H, O["id"]))["lines"][0]
        assert L["stl"]["state"] == "ready_unlinked" and L["stl"]["duplicates"] == 1
        Newest = H.Ctx.Db.One("SELECT id FROM session_3d WHERE design_id = ? ORDER BY created_at DESC LIMIT 1", (A,))["id"]
        assert L["stl"]["link"]["id"] == Newest
        # One of them gets a bore made round — a different file: now the Admin chooses, nothing is linked silently
        Older = H.Ctx.Db.One("SELECT id FROM session_3d WHERE design_id = ? AND id <> ?", (A, Newest))["id"]
        H.Ctx.Db.Execute("UPDATE geometry_results SET checks_json = '{\"bore_corrected\": true}' WHERE session_3d_id = ? AND stage = 'production'", (Older,))
        L = (await _State(H, O["id"]))["lines"][0]
        assert L["stl"]["state"] == "ambiguous" and not L["eligible"] and len(L["stl"]["choices"]) == 2 and "Link STL" in L["stl"]["action"]
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/production", json={"line_ids": [LineId], "links": {LineId: Newest}}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["blocked"][0]["reason"] == "ambiguous"
        R = await _Prepare(H, O["id"], LineId, Expect=409)
        assert R["error"]["code"] == "stl_ambiguous" and len(R["error"]["choices"]) == 2
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{LineId}/link-stl", json={"three_d_id": "s3d_nope"}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "not_a_candidate"
        # The Admin chooses: linked, recorded; a later result never replaces the link, neither does Prepare STL nor Link STL
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{LineId}/link-stl", json={"three_d_id": Older}, headers=Admin)
        assert R.status_code == 200 and R.json()["three_d_id"] == Older
        assert [E["data"]["linked"] for E in H.Svc.Orders.AdminGet(O["id"])["events"] if E["kind"] == "3d_pinned"] == ["chosen"]
        await _Model(H, A, 7, "silver")
        L = (await _State(H, O["id"]))["lines"][0]
        assert L["stl"]["state"] == "ready" and L["pinned"] and H.Ctx.Db.One("SELECT three_d_id FROM order_lines WHERE id = ?", (LineId,))["three_d_id"] == Older
        assert (await _Prepare(H, O["id"], LineId))["three_d_id"] == Older
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{LineId}/link-stl", json={"three_d_id": Newest}, headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "already_linked"
        S = await _Send(H, O["id"])                                                   # linked: no confirmation of a link needed
        assert S["lines"][0]["production"]["pinned"]["three_d_id"] == Older
    finally:
        await H.Close()


async def test_a_missing_file_and_a_hand_set_status_are_named_for_what_they_are(tmp_path):
    H = _On(tmp_path)
    try:
        A, Cand = await _Design(H, "A fil twist band")
        await _InBag(H, A, Cand, "silver", Size=7)
        O = await _Place(H, "f1")
        LineId = H.Svc.Orders.AdminGet(O["id"])["lines"][0]["id"]
        await _Model(H, A, 7, "silver")
        await _Prepare(H, O["id"], LineId)
        # The order's status set to Production by hand (the flow before the integration): not handed over
        await _Status(H, O["id"], "production")
        L = (await _State(H, O["id"]))["lines"][0]
        assert L["message"] == "STL ready · In the existing production flow — not handed over" and L["handover"]["state"] == "legacy_status"
        # The master STL gone from the disk: said as such — the record exists, the file does not
        Raw = H.Ctx.Db.One("SELECT r.stl_path FROM raw_geometry r JOIN session_3d s ON s.mesh_id = r.mesh_id WHERE s.design_id = ? LIMIT 1", (A,))
        (H.Settings.DevDir / Raw["stl_path"]).unlink()
        L = (await _State(H, O["id"]))["lines"][0]
        assert L["stl"]["state"] == "file_missing" and L["stl"]["label"] == "STL file missing on disk" and not L["print_ready"]
    finally:
        await H.Close()


@pytest.mark.parametrize("Allow", [(), ("ORD-10001",)])
async def test_an_empty_allowlist_is_normal_operation_and_a_set_one_is_the_pilot(tmp_path, Allow):
    H = _On(tmp_path, FulfillmentOrderAllowlist=Allow)
    try:
        A, Cand = await _Design(H, "A fil twist band")
        await _InBag(H, A, Cand, "silver", Size=7)
        await _Place(H, "p1")
        await _InBag(H, A, Cand, "silver", Size=7)
        O2 = await _Place(H, "p2")
        await _Model(H, A, 7, "silver")
        await _Prepare(H, O2["id"], H.Svc.Orders.AdminGet(O2["id"])["lines"][0]["id"])
        S = await _State(H, O2["id"])
        assert S["pilot"] == {"active": bool(Allow), "orders": list(Allow), "allowed": not Allow}
        assert S["lines"][0]["eligible"] is (not Allow)
    finally:
        await H.Close()
