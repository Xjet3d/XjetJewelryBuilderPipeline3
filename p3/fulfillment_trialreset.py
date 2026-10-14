"""Resetting one order's hand-over to Production after a pilot trial (the owner's request of 2026-10-14; Fulfillment's
decision I-34). An operator's tool, never a page or a route: the product itself has no un-send (K7) — a part leaves
Production through the cancellation request Fulfillment decides. This exists for the pilot's trials, once Fulfillment
has cleared the same order on its side (`python -m fulfillment.resettest <data_dir> --order ORD-…`), so the Admin can
send the line again and follow the hand-over from the start.

    python -m p3.fulfillment_trialreset ORD-10001 --db var/pipeline3.db                                   # the plan
    python -m p3.fulfillment_trialreset ORD-10001 --db var/pipeline3.db --apply --checkpoint <dir> --reason "<why>"

Cleared, for that order only: its hand-over rows (`fulfillment_line_production`), its enrollment and its projection
(what Fulfillment reported). Kept: the order and its lines, prices, payment, pins, 3D versions and files; its history
(`order_events` — one more entry per line records the reset, with the reason), the events received from Fulfillment
(`fulfillment_inbox`) and the changes feed (`fulfillment_outbox`: Fulfillment's cursor is past its rows, they are never
read again). The customer's timeline shows only the milestones of the next hand-over (fulfillment_bridge.CustomerView).

Refused: an order whose status Fulfillment's progress already moved (production, qc, shipped, completed — a real step
was recorded), a pending command, an order with nothing to reset. Before anything is deleted, a checkpoint: SQLite's
own backup of the database into an empty directory, its integrity checked and its counts compared. Pipeline 3 may keep
running — the reset is one short transaction."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from p3.db import Now

Touched = ("fulfillment_line_production", "fulfillment_enrollments", "fulfillment_projection", "order_events")
ProgressStatuses = ("production", "qc", "shipped", "completed")      # written by Fulfillment's progress (D-4): a real step happened


def Connect(DbPath: Path, ReadOnly: bool = True) -> sqlite3.Connection:
    if not Path(DbPath).is_file():
        raise SystemExit(f"no database at {DbPath}")
    Conn = sqlite3.connect(f"file:{Path(DbPath).as_posix()}?mode=ro", uri=True) if ReadOnly else sqlite3.connect(DbPath, isolation_level=None, timeout=30)
    Conn.row_factory = sqlite3.Row
    Conn.execute("PRAGMA busy_timeout = 30000")
    return Conn


def _Order(Conn: sqlite3.Connection, Ref: str) -> sqlite3.Row:
    """The order by its reference (ORD-10001) or its id (ord_…)."""
    R = Conn.execute("SELECT id, order_no, status FROM orders WHERE id = ?", (Ref,)).fetchone()
    if R is None and Ref.upper().startswith("ORD-") and Ref[4:].isdigit():
        R = Conn.execute("SELECT id, order_no, status FROM orders WHERE order_no = ?", (int(Ref[4:]),)).fetchone()
    if R is None:
        raise SystemExit(f"no order {Ref}")
    return R


def Plan(Conn: sqlite3.Connection, Ref: str) -> dict:
    """What the reset would clear for this order, read-only, and why it may not run."""
    O = _Order(Conn, Ref)
    OrderRef = f"ORD-{O['order_no']}"
    Lines = [dict(R) for R in Conn.execute("SELECT p.line_id, l.ring_id AS item_ref, p.state, p.part_display_no, p.handoff_no, p.sent_at, p.sent_by "
                                           "FROM fulfillment_line_production p LEFT JOIN order_lines l ON l.id = p.line_id WHERE p.order_id = ? ORDER BY p.sent_at",
                                           (O["id"],)).fetchall()]
    E = Conn.execute("SELECT id, state, source_version, enrolled_at, enrolled_by FROM fulfillment_enrollments WHERE entity_type = 'order' AND entity_id = ?",
                     (O["id"],)).fetchone()
    Proj = Conn.execute("SELECT aggregate_version, last_event_at FROM fulfillment_projection WHERE entity_type = 'order' AND entity_id = ?", (O["id"],)).fetchone()
    Pending = Conn.execute("SELECT COUNT(*) FROM fulfillment_commands WHERE entity_type = 'order' AND entity_id = ? AND state = 'pending'", (O["id"],)).fetchone()[0]
    Problems = []
    if not (Lines or E or Proj):
        Problems.append(f"{OrderRef} was never sent to Production: nothing to reset")
    if O["status"] in ProgressStatuses:
        Problems.append(f"{OrderRef} is {O['status']} — Fulfillment's progress was written into the order: not a trial to reset")
    if Pending:
        Problems.append(f"{OrderRef} has {Pending} pending command(s) to Fulfillment: wait for the answer first")
    return {"order": OrderRef, "order_id": O["id"], "status": O["status"], "lines": Lines, "enrollment": dict(E) if E else None,
            "projection": dict(Proj) if Proj else None, "pending_commands": Pending, "problems": Problems}


def Checkpoint(DbPath: Path, Dir: Path) -> dict:
    """SQLite's own backup of the database into an empty directory, verified: the integrity check and the counts of the
    tables the reset touches, equal to the live ones at the moment of the copy."""
    Dir = Path(Dir)
    if Dir.exists() and any(Dir.iterdir()):
        raise SystemExit(f"refused — the checkpoint directory {Dir} is not empty")
    Dir.mkdir(parents=True, exist_ok=True)
    Target = Dir / Path(DbPath).name
    Src = sqlite3.connect(DbPath, timeout=30)
    Dst = sqlite3.connect(Target)
    try:
        # One read transaction: the counts and the copy come from the same snapshot (a write lock would make SQLite refuse
        # the backup from this connection — SQLITE_LOCKED — and the copy wait forever)
        Src.execute("BEGIN")
        try:
            Live = {T: Src.execute(f"SELECT COUNT(*) FROM {T}").fetchone()[0] for T in Touched}
            Src.backup(Dst)
        finally:
            Src.execute("ROLLBACK")
    finally:
        Src.close()
    try:
        Ok = Dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        Copy = {T: Dst.execute(f"SELECT COUNT(*) FROM {T}").fetchone()[0] for T in Touched}
    finally:
        Dst.close()
    if not Ok or Copy != Live:
        raise SystemExit(f"refused — the checkpoint does not verify (integrity {'ok' if Ok else 'failed'}, counts {Copy} vs {Live})")
    Manifest = {"database": str(Target), "taken_at": Now(), "counts": Copy, "integrity": "ok"}
    (Dir / "checkpoint.json").write_text(json.dumps(Manifest, indent=2), encoding="utf-8")
    return Manifest


def Reset(DbPath: Path, Ref: str, Reason: str, CheckpointDir: Path, By: str = "operator (trial reset)") -> dict:
    """Clear the order's hand-over after every check and a verified checkpoint; returns what was cleared."""
    Reason = " ".join((Reason or "").split())[:500]
    if not Reason:
        raise SystemExit("refused — say why (--reason): the order's history keeps it")
    Conn = Connect(DbPath)
    try:
        P = Plan(Conn, Ref)
    finally:
        Conn.close()
    if P["problems"]:
        raise SystemExit("refused — " + "; ".join(P["problems"]))
    Cp = Checkpoint(DbPath, CheckpointDir)
    Conn = Connect(DbPath, ReadOnly=False)
    try:
        Conn.execute("BEGIN IMMEDIATE")
        try:
            P = Plan(Conn, Ref)                                     # again, inside the transaction: nothing changed meanwhile
            if P["problems"]:
                raise SystemExit("refused — " + "; ".join(P["problems"]))
            Id, T = P["order_id"], Now()
            Counts = {"fulfillment_line_production": Conn.execute("DELETE FROM fulfillment_line_production WHERE order_id = ?", (Id,)).rowcount,
                      "fulfillment_enrollments": Conn.execute("DELETE FROM fulfillment_enrollments WHERE entity_type = 'order' AND entity_id = ?", (Id,)).rowcount,
                      "fulfillment_projection": Conn.execute("DELETE FROM fulfillment_projection WHERE entity_type = 'order' AND entity_id = ?", (Id,)).rowcount}
            for L in P["lines"] or [{"line_id": None, "item_ref": None}]:
                Conn.execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                             (Id, "production_reset", json.dumps({"line_id": L["line_id"], "item_ref": L["item_ref"], "part": L.get("part_display_no"),
                                                                  "note": Reason, "checkpoint": str(CheckpointDir)}), By, T))
            Conn.execute("COMMIT")
        except BaseException:
            Conn.execute("ROLLBACK")
            raise
    finally:
        Conn.close()
    return {"order": P["order"], "lines": [L["item_ref"] for L in P["lines"]], "cleared": Counts, "reason": Reason, "checkpoint": Cp}


def Main(Argv=None) -> int:
    Parser = argparse.ArgumentParser(prog="p3.fulfillment_trialreset", description="Reset one order's hand-over to Production after a pilot trial.")
    Parser.add_argument("order", help="ORD-… or the order id")
    Parser.add_argument("--db", required=True, help="Pipeline 3's database (var/pipeline3.db)")
    Parser.add_argument("--apply", action="store_true", help="clear it (otherwise only the plan is shown)")
    Parser.add_argument("--checkpoint", help="an empty directory for the verified checkpoint (required with --apply)")
    Parser.add_argument("--reason", default="", help="why — kept in the order's history")
    Args = Parser.parse_args(Argv)
    if not Args.apply:
        Conn = Connect(Path(Args.db))
        try:
            print(json.dumps(Plan(Conn, Args.order), indent=2))
        finally:
            Conn.close()
        return 0
    if not Args.checkpoint:
        print("--apply needs --checkpoint <empty directory>")
        return 2
    print(json.dumps(Reset(Path(Args.db), Args.order, Args.reason, Path(Args.checkpoint)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(Main())
