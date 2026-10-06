"""Persistent queue for heavy local STL work (geometry_jobs): exactly one job runs at a time.

Jobs survive restarts: a job that was running when the server stopped goes back to the queue (and
fails after MaxAttempts, so a model that always crashes cannot loop). Queued jobs can be cancelled.
Remote Hi3D generation is not queued here — only local processing of the raw STL is serialised.

Priorities (lower runs first): export 10 (an admin is waiting for a download) · measure 20 ·
preview 30 · integrity 90 (background, never blocks results).
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from p3.context import Context, HttpError
from p3.db import Dumps, NewId, Now

Logger = logging.getLogger("p3.geoqueue")
Priority = {"export": 10, "fix_bore": 15, "measure": 20, "preview": 30, "integrity": 90}
MaxAttempts = 2
JobTimeoutS = 1800
ExportTtlS = 3600


def WorkerCommand() -> list[str]:
    return [sys.executable, "-m", "p3.geometry_worker"]


class GeometryQueue:
    def __init__(self, Ctx: Context):
        self.Ctx = Ctx
        self.Handlers = {}              # kind -> callback(job_row, result_dict | None, error | None)
        self._Kick = False

    # ── queue operations ─────────────────────────────────────────────────
    def Enqueue(self, Kind: str, MeshId: str, Session3dId: str | None = None, Params: dict | None = None,
                Dedupe: bool = True) -> dict:
        Db = self.Ctx.Db
        if Dedupe and Kind in ("measure", "preview", "integrity"):
            Live = Db.One("SELECT * FROM geometry_jobs WHERE kind = ? AND mesh_id = ? AND status IN ('queued','running') "
                          "ORDER BY created_at LIMIT 1", (Kind, MeshId))
            if Live:
                return Live
        Jid = NewId("gjob")
        Db.Execute("INSERT INTO geometry_jobs (id, kind, mesh_id, session_3d_id, priority, status, params_json, created_at) "
                   "VALUES (?,?,?,?,?, 'queued', ?, ?)", (Jid, Kind, MeshId, Session3dId, Priority[Kind], Dumps(Params or {}), Now()))
        self.Kick()
        return self.Get(Jid)

    def Get(self, Jid: str) -> dict:
        J = self.Ctx.Db.One("SELECT * FROM geometry_jobs WHERE id = ?", (Jid,))
        if J is None:
            raise HttpError(404, "job_not_found", "Job not found.")
        return J

    def Ahead(self, Job: dict) -> int:
        """Jobs that will run before this queued job (including the one running now)."""
        if Job["status"] != "queued":
            return 0
        R = self.Ctx.Db.One("SELECT COUNT(*) AS n FROM geometry_jobs WHERE status = 'running' OR (status = 'queued' AND "
                            "(priority < ? OR (priority = ? AND created_at < ?)))",
                            (Job["priority"], Job["priority"], Job["created_at"]))
        return int(R["n"])

    def Cancel(self, Jid: str) -> dict:
        Job = self.Get(Jid)
        if Job["status"] != "queued":
            raise HttpError(409, "job_not_cancellable", "Only a job that has not started can be cancelled.")
        self.Ctx.Db.Execute("UPDATE geometry_jobs SET status = 'cancelled', finished_at = ? WHERE id = ? AND status = 'queued'",
                            (Now(), Jid))
        Job = self.Get(Jid)
        self._Call(Job, None, "cancelled")
        return Job

    def Reconcile(self) -> int:
        """After a restart: running → queued again (or failed after MaxAttempts); then process."""
        Db = self.Ctx.Db
        for J in Db.All("SELECT * FROM geometry_jobs WHERE status = 'running'"):
            if J["attempts"] >= MaxAttempts:
                Db.Execute("UPDATE geometry_jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                           ("Stopped twice by a server restart.", Now(), J["id"]))
                self._Call(self.Get(J["id"]), None, "Stopped twice by a server restart.")
            else:
                Db.Execute("UPDATE geometry_jobs SET status = 'queued', started_at = NULL WHERE id = ?", (J["id"],))
        self.CleanupExports()
        N = Db.One("SELECT COUNT(*) AS n FROM geometry_jobs WHERE status = 'queued'")["n"]
        if N:
            self.Kick()
        return int(N)

    def CleanupExports(self) -> int:
        """Delete temporary scaled-STL exports whose TTL has passed."""
        N = 0
        for J in self.Ctx.Db.All("SELECT * FROM geometry_jobs WHERE kind = 'export' AND status = 'done' AND output_path IS NOT NULL "
                                 "AND expires_at < ?", (Now(),)):
            P = self.Ctx.Settings.DevDir / J["output_path"]
            try:
                P.unlink(missing_ok=True)
            except OSError:
                continue
            self.Ctx.Db.Execute("UPDATE geometry_jobs SET output_path = NULL WHERE id = ?", (J["id"],))
            N += 1
        return N

    # ── processing ───────────────────────────────────────────────────────
    def Kick(self) -> None:
        self._Kick = True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return                                     # no loop (CLI / sync context): picked up on startup
        self.Ctx.Runner.Spawn("geometry-queue", self._Drain())

    async def _Drain(self) -> None:
        while True:
            self._Kick = False
            Job = self.Ctx.Db.One("SELECT * FROM geometry_jobs WHERE status = 'queued' ORDER BY priority, created_at LIMIT 1")
            if Job is None:
                if self._Kick:                          # something was enqueued meanwhile
                    continue
                self.CleanupExports()
                return
            await self._Run(Job)

    async def _Run(self, Job: dict) -> None:
        Db, Dev = self.Ctx.Db, self.Ctx.Settings.DevDir
        Db.Execute("UPDATE geometry_jobs SET status = 'running', started_at = ?, attempts = attempts + 1 WHERE id = ?",
                   (Now(), Job["id"]))
        Job = self.Get(Job["id"])
        self._Call(Job, None, None, Started=True)
        Params = json.loads(Job["params_json"] or "{}")
        Work = Dev / "jobs"
        Work.mkdir(parents=True, exist_ok=True)
        ArgsPath, ResultPath = Work / f"{Job['id']}.args.json", Work / f"{Job['id']}.result.json"
        Args = {**Params, "kind": Job["kind"], "result": str(ResultPath)}
        for Key in ("source", "output", "convert_to"):          # params hold paths relative to the dev dir
            if Args.get(Key):
                Args[Key] = str(Dev / Args[Key])
                if Key != "source":
                    Path(Args[Key]).parent.mkdir(parents=True, exist_ok=True)
        ArgsPath.write_text(json.dumps(Args), encoding="utf-8")
        Error, Result = None, None
        try:
            Env = {**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
            Proc = await asyncio.create_subprocess_exec(*WorkerCommand(), str(ArgsPath), env=Env,
                                                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            try:
                _, Err = await asyncio.wait_for(Proc.communicate(), JobTimeoutS)
            except asyncio.TimeoutError:
                Proc.kill()
                await Proc.wait()
                raise RuntimeError(f"took longer than {JobTimeoutS // 60} minutes and was stopped")
            Doc = json.loads(ResultPath.read_text(encoding="utf-8")) if ResultPath.is_file() else {"ok": False}
            if Proc.returncode == 3 or Doc.get("error") == "memory" or Proc.returncode in (-9, 137):
                raise MemoryError
            if not Doc.get("ok"):
                raise RuntimeError(Doc.get("error") or (Err or b"").decode("utf-8", "replace")[-400:] or "failed")
            Result = Doc
        except MemoryError:
            Error = "needed more memory than this server allows for one job"
        except Exception as E:  # noqa: BLE001
            Error = str(E)
        finally:
            for P in (ArgsPath, ResultPath):
                P.unlink(missing_ok=True)
        T = Now()
        if Error:
            Logger.warning("Geometry job %s (%s) failed: %s", Job["id"], Job["kind"], Error)
            Db.Execute("UPDATE geometry_jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?", (Error, T, Job["id"]))
        else:
            Exp = (datetime.now(timezone.utc) + timedelta(seconds=ExportTtlS)).isoformat(timespec="milliseconds") \
                if Job["kind"] == "export" else None
            Db.Execute("UPDATE geometry_jobs SET status = 'done', result_json = ?, finished_at = ?, output_path = ?, "
                       "expires_at = ? WHERE id = ?", (Dumps(Result), T, Params.get("output"), Exp, Job["id"]))
        self._Call(self.Get(Job["id"]), Result, Error)

    def _Call(self, Job: dict, Result, Error, Started: bool = False) -> None:
        H = self.Handlers.get(Job["kind"])
        if H:
            try:
                H(Job, Result, Error, Started)
            except Exception:  # noqa: BLE001
                Logger.exception("Geometry job handler failed for %s", Job["id"])

    def Storage(self) -> dict:
        import shutil
        Dev = Path(self.Ctx.Settings.DevDir)

        def Size(P: Path) -> int:
            Total = 0
            if P.is_dir():
                for Root, _, Files in os.walk(P):
                    for F in Files:
                        try:
                            Total += os.path.getsize(os.path.join(Root, F))
                        except OSError:
                            pass
            return Total
        Usage = shutil.disk_usage(self.Ctx.Settings.DataDir)
        Meshes, Exports = Size(Dev / "meshes"), Size(Dev / "exports")
        Count = self.Ctx.Db.One("SELECT COUNT(*) AS n FROM meshes WHERE status = 'ready'")["n"]
        return {"meshes_bytes": Meshes, "exports_bytes": Exports, "total_3d_bytes": Meshes + Exports,
                "models": int(Count), "disk_free_bytes": Usage.free, "disk_total_bytes": Usage.total}
