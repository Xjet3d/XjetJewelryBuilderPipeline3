"""Developer-only single-image 3D mesh (hitem3d/hi3d/v3.0/image-to-3d) and STL.

Never invoked by the customer flow. Input is exactly one selected candidate
image. The endpoint supports export_format="stl" natively (verified 2026-09-29),
which is the default; other formats can be converted to STL in a separate
developer operation. Generated meshes are NOT measured, NOT sized to the ring
size, and NOT validated as watertight or manufacturable.
"""

import io
import json
import logging

from p3 import assets
from p3 import stages as Stages
from p3.accounts import UsageMesh
from p3 import credits as Credits
from p3.context import Context, HttpError
from p3.db import Dumps, NewId, Now
from p3.providers import endpoints
from p3 import products as Products
from p3.modelconfig import ConfigError, ModelIdFor, Validate
from p3.runner import DownloadToFileWithRetry, DownloadWithRetry, FailureFor, PollUntilDone

Logger = logging.getLogger("p3.meshes")

AllowedSettings = {
    "resolution":     lambda V: V in ("2048quality", "2048master"),
    "face_count":     lambda V: isinstance(V, int) and not isinstance(V, bool) and 100_000 <= V <= 5_000_000,
    "export_format":  lambda V: V in ("glb", "obj", "stl", "fbx", "usdz"),
    "enable_texture": lambda V: isinstance(V, bool),
    "enable_pbr":     lambda V: isinstance(V, bool),
    "shading":        lambda V: isinstance(V, (int, float)) and not isinstance(V, bool) and 0 <= V <= 1,
}
ConvertibleFormats = ("glb", "obj")

Disclaimer = ("Generated from a single AI image. Not measured, not sized to the selected ring size, "
              "not checked for watertightness, and not approved for manufacturing.")


class MeshService:
    def __init__(self, Ctx: Context):
        self.Ctx = Ctx
        self.OnReady = []           # callbacks(mesh_id, ok) — 3D production continues from here

    def Create(self, CandidateId: str, Overrides: dict | None) -> dict:
        Db = self.Ctx.Db
        Cand = Db.One("SELECT c.*, b.design_id, b.user_text, b.kind, b.config_version AS image_config "
                      "FROM candidates c JOIN batches b ON b.id = c.batch_id WHERE c.id = ?", (CandidateId,))
        if Cand is None:
            raise HttpError(404, "candidate_not_found", "Candidate not found.")
        if Cand["status"] != "ready":
            raise HttpError(409, "candidate_not_ready", "Candidate image is not ready.")
        Model = ModelIdFor(endpoints.Mesh, Products.OfCandidate(Db, CandidateId))     # hi3d for rings, hi3d-charm for charms
        Version = self.Ctx.Models.Active(Model)
        Settings_ = dict(Version.Params)
        for Key, Value in (Overrides or {}).items():         # explicit per-request developer overrides
            if Key not in AllowedSettings or not AllowedSettings[Key](Value):
                raise HttpError(400, "invalid_mesh_setting", f"Invalid mesh setting: {Key}")
            Settings_[Key] = Value
        try:
            Settings_ = Validate(Model, Settings_)
        except ConfigError as E:
            raise HttpError(400, "invalid_mesh_setting", str(E)) from E
        SettingsJson = Dumps(Settings_)
        # One Hi3D request at a time per image, whatever the settings: a second request joins the one in flight
        Live = Db.One("SELECT * FROM meshes WHERE candidate_id = ? AND status IN ('queued','running') "
                      "ORDER BY created_at DESC", (CandidateId,))
        if Live:
            return self.ToJson(Live)
        MeshId = NewId("mesh")
        Provenance = {
            "candidate_id": CandidateId, "batch_id": Cand["batch_id"], "design_id": Cand["design_id"],
            "batch_kind": Cand["kind"], "batch_text": Cand["user_text"], "image_config_version": Cand["image_config"],
            "source_image_sha256": Cand["content_sha256"], "endpoint": endpoints.Mesh,
            "settings": Settings_, "mesh_config_version": Version.Id, "developer_overrides": Overrides or {},
            "requested_at": Now(), "disclaimer": Disclaimer,
        }
        T = Now()
        Db.Execute("INSERT INTO meshes (id, candidate_id, endpoint, settings_json, config_version, status, "
                   "provenance_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   (MeshId, CandidateId, endpoints.Mesh, SettingsJson, Version.Id, "queued",
                    Dumps(Provenance), T, T))
        self.Ctx.Runner.Spawn(f"mesh:{MeshId}", self._Drive(MeshId))
        return self.ToJson(Db.One("SELECT * FROM meshes WHERE id = ?", (MeshId,)))

    async def _Drive(self, MeshId: str) -> None:
        Ctx = self.Ctx
        Db = Ctx.Db
        S = Ctx.Settings
        Mesh = Db.One("SELECT * FROM meshes WHERE id = ?", (MeshId,))
        if Mesh is None or Mesh["status"] not in ("queued", "running"):
            return
        Settings_ = json.loads(Mesh["settings_json"])
        Fmt = Settings_.get("export_format", "glb")          # provider default when not configured
        try:
            RequestId = Mesh["provider_request_id"]
            if not RequestId:
                Cand = Db.One("SELECT asset_path FROM candidates WHERE id = ?", (Mesh["candidate_id"],))
                ImagePath = assets.Resolve(S.AssetsDir, Cand["asset_path"])
                ImageUrl = await Ctx.Provider.Upload(ImagePath.read_bytes(), assets.ImageContentType(Cand["asset_path"]))
                Est = Credits.CheckSpendCap(Ctx, Mesh["endpoint"], Settings_)
                try:
                    RequestId = await Ctx.Provider.Submit(Mesh["endpoint"], {"image_url": ImageUrl, **Settings_})
                    Db.Update("meshes", MeshId, status="running", provider_request_id=RequestId)
                    Owner = Db.One("SELECT d.owner_account_id FROM candidates c JOIN batches b ON b.id = c.batch_id "
                                   "JOIN designs d ON d.id = b.design_id WHERE c.id = ?", (Mesh["candidate_id"],))
                    # Hi3D is XJet's production cost: recorded for the design's account as internal usage, never a credit
                    Credits.RecordUsage(Ctx, Owner["owner_account_id"] if Owner else None, UsageMesh, MeshId,
                                        Mesh["endpoint"], Settings_, Internal=True)
                finally:
                    Credits.SubmitDone(Ctx, Est)
            def OnStatus(St):
                if St.State == "queued":
                    Stages.Begin(Db, MeshId, "waiting_hi3d", **({"position": St.Position} if St.Position is not None else {}))
                elif St.State == "running":
                    Stages.Begin(Db, MeshId, "generating_3d")
            Stages.Begin(Db, MeshId, "waiting_hi3d")
            Result = await PollUntilDone(Ctx.Provider, Mesh["endpoint"], RequestId, Ctx.Gen.Mesh.RequestTimeoutS,
                                         S.PollIntervalS, S.MaxTransientPollErrors, OnStatus=OnStatus)
            Url = (Result.get("model_mesh") or {}).get("url")
            if not Url:
                raise assets.AssetError("Provider returned no model_mesh")
            # Stream the (up to ~250 MB) model straight to disk with real progress; never held in memory.
            RelPath = f"meshes/{MeshId}/original.{Fmt}"
            Target = assets.Resolve(S.DevDir, RelPath)
            Stages.Begin(Db, MeshId, "downloading_stl")
            Last = [0.0]

            def OnProgress(Done, Total):
                import time as _t
                if _t.monotonic() - Last[0] >= 0.5 or (Total and Done >= Total):
                    Last[0] = _t.monotonic()
                    Stages.Detail(Db, MeshId, bytes=Done, total=Total)
            Size, Sha = await DownloadToFileWithRetry(Ctx.Provider, Url, Target, S.MaxTransientPollErrors,
                                                      S.PollIntervalS, OnProgress)
            assets.ValidateMeshFile(Target, Fmt)
            Thumb = None
            ThumbUrl = (Result.get("thumbnail") or {}).get("url")
            if ThumbUrl:
                try:                                             # small; a failure never fails the mesh
                    Ext = (ThumbUrl.rsplit(".", 1)[-1].split("?")[0] or "webp")[:5]
                    Thumb = f"meshes/{MeshId}/thumbnail.{Ext}"
                    await DownloadToFileWithRetry(Ctx.Provider, ThumbUrl, assets.Resolve(S.DevDir, Thumb), 2, S.PollIntervalS)
                except Exception:  # noqa: BLE001
                    Thumb = None
            T = Now()
            Faces = (Size - 84) // 50 if Fmt == "stl" else None
            Db.Execute("INSERT OR REPLACE INTO raw_geometry (mesh_id, stl_path, sha256, bytes, faces, status, thumbnail_path, "
                       "timings_json, created_at, updated_at) VALUES (?,?,?,?,?, 'downloaded', ?, '{}', ?, ?)",
                       (MeshId, RelPath, Sha, Size, Faces, Thumb, T, T))
            Db.Update("meshes", MeshId, status="ready", original_path=RelPath, original_format=Fmt,
                      stl_path=RelPath if Fmt == "stl" else None, error=None, error_code=None)
            Stages.End(Db, MeshId)
            self._Notify(MeshId, True)
        except Exception as E:
            Message, Code = FailureFor(E)
            Logger.warning("Mesh %s failed (%s): %s", MeshId, Code, E)
            Db.Update("meshes", MeshId, status="failed", error=Message, error_code=Code)
            Stages.Begin(Db, MeshId, "failed", error=Message)
            self._Notify(MeshId, False)

    def _Notify(self, MeshId: str, Ok: bool) -> None:
        for Fn in self.OnReady:
            try:
                Fn(MeshId, Ok)
            except Exception:  # noqa: BLE001
                Logger.exception("Mesh ready hook failed for %s", MeshId)

    def ConvertToStl(self, MeshId: str) -> dict:
        """Separate developer operation: convert a GLB/OBJ result to STL with trimesh (no repair, no scaling)."""
        Mesh = self._Require(MeshId)
        if Mesh["status"] != "ready":
            raise HttpError(409, "mesh_not_ready", "Mesh is not ready.")
        if Mesh["stl_path"]:
            return self.ToJson(Mesh)
        if Mesh["original_format"] not in ConvertibleFormats:
            raise HttpError(400, "unsupported_conversion", f"Cannot convert {Mesh['original_format']} to STL.")
        import trimesh
        Source = assets.Resolve(self.Ctx.Settings.DevDir, Mesh["original_path"])
        try:
            Loaded = trimesh.load(io.BytesIO(Source.read_bytes()), file_type=Mesh["original_format"])
            Geometry = Loaded.to_geometry() if isinstance(Loaded, trimesh.Scene) else Loaded
            if not hasattr(Geometry, "faces") or len(Geometry.faces) == 0:
                raise ValueError("Mesh contains no faces")
            Data = Geometry.export(file_type="stl")
            assets.ValidateMesh(Data, "stl")
        except Exception as E:
            raise HttpError(422, "conversion_failed", f"STL conversion failed: {E}") from E
        RelPath = f"meshes/{MeshId}/converted.stl"
        assets.WriteAtomic(self.Ctx.Settings.DevDir, RelPath, Data)
        self.Ctx.Db.Update("meshes", MeshId, stl_path=RelPath)
        return self.ToJson(self._Require(MeshId))

    def FilePath(self, MeshId: str, Kind: str):
        Mesh = self._Require(MeshId)
        Rel = Mesh["stl_path"] if Kind == "stl" else Mesh["original_path"]
        if Mesh["status"] != "ready" or not Rel:
            raise HttpError(404, "mesh_file_unavailable", "That mesh file is not available.")
        return assets.Resolve(self.Ctx.Settings.DevDir, Rel), Rel.rsplit("/", 1)[-1]

    def List(self, CandidateId: str | None) -> list[dict]:
        if CandidateId:
            Rows = self.Ctx.Db.All("SELECT * FROM meshes WHERE candidate_id = ? ORDER BY created_at DESC", (CandidateId,))
        else:
            Rows = self.Ctx.Db.All("SELECT * FROM meshes ORDER BY created_at DESC LIMIT 50")
        return [self.ToJson(R) for R in Rows]

    def Reconcile(self) -> dict:
        Resumed = Interrupted = 0
        for M in self.Ctx.Db.All("SELECT id, provider_request_id FROM meshes WHERE status IN ('queued','running')"):
            if M["provider_request_id"] and not self.Ctx.Provider.Owns(M["provider_request_id"]):
                if self.Ctx.Provider.Name == "mock":
                    continue      # live request: resumes when live mode is active
                self.Ctx.Db.Update("meshes", M["id"], status="interrupted", error_code="interrupted",
                                   error="This mock-mode mesh cannot be resumed in live mode.")
                Interrupted += 1
            elif M["provider_request_id"]:
                self.Ctx.Runner.Spawn(f"mesh:{M['id']}", self._Drive(M["id"]))
                Resumed += 1
            else:
                self.Ctx.Db.Update("meshes", M["id"], status="interrupted", error_code="interrupted",
                                   error="Interrupted by a server restart before submission was confirmed.")
                Interrupted += 1
        return {"resumed": Resumed, "interrupted": Interrupted}

    def _Require(self, MeshId: str) -> dict:
        M = self.Ctx.Db.One("SELECT * FROM meshes WHERE id = ?", (MeshId,))
        if M is None:
            raise HttpError(404, "mesh_not_found", "Mesh not found.")
        return M

    def Get(self, MeshId: str) -> dict:
        return self.ToJson(self._Require(MeshId))

    def ToJson(self, M: dict) -> dict:
        return {"id": M["id"], "candidate_id": M["candidate_id"], "status": M["status"], "endpoint": M["endpoint"],
                "settings": json.loads(M["settings_json"]), "config_version": M["config_version"],
                "original_format": M["original_format"], "has_stl": bool(M["stl_path"]),
                "can_convert": M["status"] == "ready" and not M["stl_path"]
                               and M["original_format"] in ConvertibleFormats,
                "provenance": json.loads(M["provenance_json"]), "error": M["error"], "error_code": M["error_code"],
                "created_at": M["created_at"]}
