"""The owner's request of 2026-10-14 (Fulfillment's decision I-34) on this side: Fulfillment's manufacturing approval
is visible on the order's line and the Production page as its own stage; and one order's hand-over is reset after a
pilot trial by an operator's tool — a verified checkpoint first, only that order's hand-over, enrollment and
projection cleared, its history kept with one more entry, a late event from the cleared trial never projected again,
the line sendable again, the customer's timeline starting over with the next hand-over."""

import json
import sqlite3

import pytest

from p3 import fulfillment_trialreset as Trial
from tests.test_fulfillment_bridge import Admin, Prefix, Service, _Enrolled, _Event, _Placed, _Send
from tests.test_production_page import _AdminPart, _Full


def _Pub(LineId: str, Status: str = "ready", Review: str = "approved") -> list[dict]:
    return [{"display_no": "MP-10001-001", "part_id": "mp_1", "line_id": LineId, "customer_stage": "Preparing for production", "status": Status,
             "summary": "0/2 passed QC", "review_status": Review, "file_state": "ready", "blockers": [], "cancellation_state": "none",
             "quantities": {"target": 2, "passed": 0, "shipped": 0}, "commercial_action_pending": False}]


async def test_the_approval_shows_on_the_line_and_a_trial_is_reset_so_the_line_is_sent_again(tmp_path):
    H = _On_(tmp_path)
    try:
        Did, O = await _Placed(H)
        await _Enrolled(H, Did, O)
        await H.Idle()
        LineId = H.Svc.Orders.AdminGet(O["id"])["lines"][0]["id"]
        Url = f"/api/admin/orders/{O['id']}/fulfillment"
        # Fulfillment approves the part: the line and the Production page say so
        E1 = _Full("evt-1", O["id"], 1, "fulfillment.review_approved", {"parts": _Pub(LineId), "customer_status": "Preparing for production"},
                   [_AdminPart("MP-10001-001", LineId, Stage="approved", detail="ready for planning", part_id="mp_1")], About=["MP-10001-001"])
        assert (await H.Client.post(Prefix + "/events", json={"events": [E1]}, headers=Service)).json()["results"][0]["status"] == "applied"
        L = (await H.Client.get(Url, headers=Admin)).json()["lines"][0]
        assert (L["stage"], L["stage_label"], L["stage_detail"]) == ("approved", "Approved for production", "ready for planning")
        Row = (await H.Client.get("/api/admin/production", headers=Admin)).json()["rows"][0]
        assert (Row["stage"], Row["stage_label"]) == ("approved", "Approved for production")
        Timeline = (await H.Client.get(f"/api/admin/production/{LineId}", headers=Admin)).json()["timeline"]
        assert [T["label"] for T in Timeline if T["source"] == "fulfillment"] == ["Approved for production"]
        View = H.App.state.Bridge.CustomerView(O["id"])
        assert [X["label"] for X in View["timeline"]] == ["Approved for production"]
        # The plan, read-only: the order's hand-over, enrollment and projection
        Db = H.Settings.DbPath
        Plan = Trial.Plan(Trial.Connect(Db), "ORD-10001")
        assert Plan["problems"] == [] and [X["item_ref"] for X in Plan["lines"]] == ["R-1001-A"] and Plan["enrollment"]["state"] == "enrolled" and Plan["projection"]
        assert Trial.Plan(Trial.Connect(Db), O["id"])["order"] == "ORD-10001"
        with pytest.raises(SystemExit, match="say why"):
            Trial.Reset(Db, "ORD-10001", " ", tmp_path / "cp0")
        (tmp_path / "busy").mkdir()
        (tmp_path / "busy" / "x").write_text("x")
        with pytest.raises(SystemExit, match="not empty"):
            Trial.Reset(Db, "ORD-10001", "a re-test", tmp_path / "busy")
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_line_production")["n"] == 1                 # untouched
        Before = H.Ctx.Db.One("SELECT COUNT(*) AS n FROM order_events WHERE order_id = ?", (O["id"],))["n"]
        Out = Trial.Reset(Db, "ORD-10001", "the owner's re-test of 2026-10-14", tmp_path / "cp1")
        assert Out["cleared"] == {"fulfillment_line_production": 1, "fulfillment_enrollments": 1, "fulfillment_projection": 1} and Out["lines"] == ["R-1001-A"]
        Cp = json.loads((tmp_path / "cp1" / "checkpoint.json").read_text())
        assert Cp["integrity"] == "ok" and Cp["counts"]["fulfillment_line_production"] == 1
        Copy = sqlite3.connect(tmp_path / "cp1" / Db.name)
        assert Copy.execute("SELECT COUNT(*) FROM fulfillment_enrollments").fetchone()[0] == 1                     # the state before, kept
        Copy.close()
        # The line is sendable again; the history keeps everything, with the reset
        State = (await H.Client.get(Url, headers=Admin)).json()
        assert not State["enrolled"] and State["projection"] is None and State["eligible"] and State["lines_sent"] == 0
        L = State["lines"][0]
        assert L["eligible"] and L["print_ready"] and L["production"] == {"sent": False} and L["stage"] is None
        Events = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()["events"]
        assert len(Events) == Before + 1 and [E["kind"] for E in Events].count("production_sent") == 1
        Reset_ = next(E for E in Events if E["kind"] == "production_reset")
        assert Reset_["data"]["note"] == "the owner's re-test of 2026-10-14" and Reset_["data"]["item_ref"] == "R-1001-A" and Reset_["by"] == "operator (trial reset)"
        assert H.App.state.Bridge.CustomerView(O["id"]) is None and H.Svc.Orders.AdminGet(O["id"])["status"] == "new"
        # A late event of the cleared trial is recorded, never projected
        Late = _Full("evt-2", O["id"], 2, "fulfillment.review_started", {"parts": _Pub(LineId, "awaiting_review", "in_review")},
                     [_AdminPart("MP-10001-001", LineId, part_id="mp_1")])
        assert (await H.Client.post(Prefix + "/events", json={"events": [Late]}, headers=Service)).json()["results"][0]["result"] == "unknown_entity"
        assert H.App.state.Bridge.Projection("order", O["id"]) is None
        # Sent again: a new enrollment, at version 1 of a new hand-over, read by Fulfillment from the feed
        Cursor = H.Ctx.Db.One("SELECT MAX(seq) AS s FROM fulfillment_outbox")["s"]
        State = await _Send(H, O["id"], Note="re-test")
        assert State["enrolled"] and State["enrollment"]["source_version"] == 1 and State["lines"][0]["production"]["state"] == "sent"
        C = (await H.Client.get(Prefix + f"/changes?cursor={Cursor}", headers=Service)).json()["changes"]
        assert [(X["kind"], X["source_version"]) for X in C] == [("enrolled", 1)]
        assert H.App.state.Bridge.CustomerView(O["id"])["timeline"] == []                                       # the earlier trial's milestones stay out
        # Refused: an order Fulfillment's progress moved on
        H.Ctx.Db.Execute("UPDATE orders SET status = 'production' WHERE id = ?", (O["id"],))
        with pytest.raises(SystemExit, match="is production"):
            Trial.Reset(Db, "ORD-10001", "again", tmp_path / "cp2")
        with pytest.raises(SystemExit, match="no order ORD-10999"):
            Trial.Plan(Trial.Connect(Db), "ORD-10999")
    finally:
        await H.Close()


def _On_(TmpPath):
    from tests.test_fulfillment_bridge import _On
    return _On(TmpPath)


async def test_an_order_never_sent_has_nothing_to_reset(tmp_path):
    H = _On_(tmp_path)
    try:
        _Did, O = await _Placed(H)
        Plan = Trial.Plan(Trial.Connect(H.Settings.DbPath), O["id"])
        assert Plan["problems"] == ["ORD-10001 was never sent to Production: nothing to reset"] and Plan["lines"] == []
        with pytest.raises(SystemExit, match="never sent"):
            Trial.Reset(H.Settings.DbPath, "ORD-10001", "x", tmp_path / "cp")
        assert not (tmp_path / "cp").exists()                                                                  # no checkpoint for nothing
        assert _Event                                                                                           # the shared helpers stay importable
    finally:
        await H.Close()
