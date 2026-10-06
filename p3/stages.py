"""Persisted processing stages (stage_log) — real state with start/end times, so a status and its
elapsed-time clock stay correct after a refresh or a server restart.

A subject is a Hi3D mesh id (waiting_hi3d → generating_3d → downloading_stl) or a 3D production
request id (queued → calculating_geometry → ready / failed / cancelled). Starting a stage closes the
subject's open stage. Detail is optional real data (fal queue position, bytes downloaded, …) — never
an invented percentage.
"""

import json

from p3.db import Database, Dumps, Now

Labels = {
    "waiting_hi3d": "Waiting for Hi3D", "generating_3d": "Generating 3D", "downloading_stl": "Downloading STL",
    "queued": "Queued", "calculating_geometry": "Calculating Geometry", "correcting_bore": "Making the bore round", "ready": "Ready",
    # Processing finished but the result carries warnings (bore, roundness, open mesh): production
    # review is a separate decision from "the numbers are there".
    "review_required": "Processing complete — production review required",
    "failed": "Failed", "cancelled": "Cancelled", "exporting": "Preparing scaled STL",
}
Terminal = ("ready", "review_required", "failed", "cancelled")


def Begin(Db: Database, Subject: str, Stage: str, **Detail) -> None:
    T = Now()
    with Db.Transaction() as Conn:
        Open = Conn.execute("SELECT id, stage FROM stage_log WHERE subject = ? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
                            (Subject,)).fetchone()
        if Open and Open[1] == Stage:
            if Detail:
                Conn.execute("UPDATE stage_log SET detail_json = ? WHERE id = ?", (Dumps(Detail), Open[0]))
            return
        Conn.execute("UPDATE stage_log SET ended_at = ? WHERE subject = ? AND ended_at IS NULL", (T, Subject))
        Conn.execute("INSERT INTO stage_log (subject, stage, detail_json, started_at, ended_at) VALUES (?,?,?,?,?)",
                     (Subject, Stage, Dumps(Detail), T, T if Stage in Terminal else None))


def Detail(Db: Database, Subject: str, **Detail) -> None:
    """Update the open stage's detail (e.g. download progress) without starting a new stage."""
    Db.Execute("UPDATE stage_log SET detail_json = ? WHERE id = (SELECT id FROM stage_log WHERE subject = ? "
               "AND ended_at IS NULL ORDER BY id DESC LIMIT 1)", (Dumps(Detail), Subject))


def End(Db: Database, Subject: str) -> None:
    Db.Execute("UPDATE stage_log SET ended_at = ? WHERE subject = ? AND ended_at IS NULL", (Now(), Subject))


def List(Db: Database, Subject: str, Since: str | None = None) -> list[dict]:
    Rows = Db.All("SELECT stage, detail_json, started_at, ended_at FROM stage_log WHERE subject = ? "
                  + ("AND started_at >= ? " if Since else "") + "ORDER BY id", (Subject, Since) if Since else (Subject,))
    return [{"stage": R["stage"], "label": Labels.get(R["stage"], R["stage"]), "detail": json.loads(R["detail_json"] or "{}"),
             "started_at": R["started_at"], "ended_at": R["ended_at"]} for R in Rows]
