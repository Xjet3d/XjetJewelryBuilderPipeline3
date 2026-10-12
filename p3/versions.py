"""3D versions, external parts and sessions — Session → Part/Design → 3D version (docs/EXTERNAL-PARTS.md).

  Session S-1001 ─┬─ Design "Éclat Heart" C-1029 ─┬─ v1 Hi3D            (superseded)
                  │                               └─ v2 External · Rhino (current: the newest)
                  └─ Design (external) C-1031 ───── v1 External · CAD   (current)

* A 3D version is a `meshes` row — a Hi3D model, or an STL made outside Atelier — numbered per design (version_no),
  with where it came from (source, tool, file name, who, when, the version it revises). Its file is measured once
  (raw_geometry) exactly like a Hi3D model; every size and material after that is arithmetic (p3/production3d.py).
  An attached STL is a permanent asset of its part: nothing here ever deletes a version or its file.
* LIFO: the newest usable version is the design's current one by default (each new version becomes current); an Admin
  may choose any other existing version (designs.active_mesh_id) and every version is kept — superseded, or archived
  (hidden from the choices; the file stays). A version whose file cannot be measured is never current.
* Scaling: 'scalable' — the file is a shape, sized like a Hi3D model (a ring by its inner diameter, a charm by its total
  height); 'exact' — the file is the production geometry in millimetres, used as it is, whatever the size (checked
  against the size instead). External parts are exact unless the Admin says otherwise.
* Sessions start on the website (a customer's first prompt); the Admin never makes one. An external part is attached to
  an existing session: as a new version of one of its designs, or as a new design of that session — which then becomes
  an explicit session (sessions, S-1001 …) holding several designs. An external design is an ordinary part of its
  session (its owner sees and orders it like any design); its STL, like every 3D file, is for the Admin only.
* An order line is pinned to the 3D version current when it is ordered, then to the result it is produced from —
  p3/orders.py.
"""

import json
import logging

from p3 import assets
from p3 import products as Products
from p3 import ringids as RingIds
from p3 import sessions as Sessions
from p3.context import Context, HttpError
from p3.db import Dumps, NewId, Now

Logger = logging.getLogger("p3.versions")
Sources = ("hi3d", "external")
Scalings = ("scalable", "exact")
ExternalEndpoint = "external"           # meshes.endpoint / batches.endpoint / config_version of an external part
UnitsMinMm, UnitsMaxMm = 3.0, 200.0     # an exact part's largest side outside this is probably not in millimetres
MaxNote, MaxTool, MaxTitle = 300, 60, 80

_DesignMeshes = ("SELECT m.* FROM meshes m JOIN candidates c ON c.id = m.candidate_id JOIN batches b ON b.id = c.batch_id "
                 "WHERE b.design_id = ?")
# A usable version: ready, not archived, and its file not found unmeasurable (a version still being measured counts)
_Usable = (" AND m.status = 'ready' AND m.archived_at IS NULL AND NOT EXISTS (SELECT 1 FROM raw_geometry r "
           "WHERE r.mesh_id = m.id AND r.status = 'failed')")


def SessionRef(No) -> str | None:
    return f"S-{No}" if No is not None else None


def NewestMesh(Db, DesignId: str) -> dict | None:
    """The design's newest usable version — the current one unless an Admin chose another (LIFO)."""
    return Db.One(_DesignMeshes + _Usable + " ORDER BY m.created_at DESC LIMIT 1", (DesignId,))


def ActiveMesh(Db, DesignId: str) -> dict | None:
    """The design's current 3D version: the one designs.active_mesh_id names (each new version sets it — LIFO — and an
    Admin may set it to another), else, when that one is not usable (or a design from before versions), the newest
    usable version."""
    D = Db.One("SELECT active_mesh_id FROM designs WHERE id = ?", (DesignId,))
    if D and D.get("active_mesh_id"):              # still this design's (a split-off refinement takes its models along)
        M = Db.One(_DesignMeshes + " AND m.id = ?" + _Usable, (DesignId, D["active_mesh_id"]))
        if M:
            return M
    return NewestMesh(Db, DesignId)


def MeshFor(Db, DesignId: str, CandidateId: str) -> dict | None:
    """The version an option's 3D is made from: the design's current version when it is of this option, else the
    option's newest usable version."""
    A = ActiveMesh(Db, DesignId)
    if A and A["candidate_id"] == CandidateId:
        return A
    return Db.One("SELECT m.* FROM meshes m WHERE m.candidate_id = ?" + _Usable + " ORDER BY m.created_at DESC LIMIT 1",
                  (CandidateId,))


def Label(M: dict | None) -> str | None:
    """'v2 · External · Rhino · exact' / 'v1 · Hi3D'."""
    if not M:
        return None
    Parts = [f"v{M['version_no']}" if M.get("version_no") else "v?",
             "External" if M.get("source") == "external" else "Hi3D"]
    if M.get("source_tool"):
        Parts.append(M["source_tool"])
    if M.get("scaling") == "exact":
        Parts.append("exact")
    return " · ".join(Parts)


def Brief(M: dict | None, ActiveId: str | None = None) -> dict | None:
    if not M:
        return None
    return {"mesh_id": M["id"], "version_no": M.get("version_no"), "source": M.get("source") or "hi3d",
            "source_tool": M.get("source_tool"), "scaling": M.get("scaling") or "scalable", "label": Label(M),
            "original_filename": M.get("original_filename"), "candidate_id": M["candidate_id"],
            "archived": bool(M.get("archived_at")), "active": ActiveId is not None and M["id"] == ActiveId,
            "created_at": M["created_at"]}


def UsSizeFor(InnerDiameterMm: float | None) -> float | None:
    """The US ring size whose inner diameter this is (the inverse of geometry.UsSizeToInnerDiameterMm), unrounded."""
    return round((InnerDiameterMm - 11.63) / 0.8128, 2) if InnerDiameterMm else None


def Measurement(RawRow: dict | None, Product: str, Scaling: str, Job: dict | None = None) -> dict:
    """A version's one measurement, for the Admin: X/Y/Z, volume, area, a ring's inner diameter (and the US size it is)
    or a charm's total height. Millimetres for an exact part; a scalable model's own units otherwise."""
    if not RawRow:
        return {"status": "missing"}
    if RawRow["status"] == "failed":
        return {"status": "failed", "error": RawRow.get("error")}
    if RawRow["status"] != "measured" or not RawRow.get("measurement_json"):
        return {"status": "measuring" if (Job or {}).get("status") == "running" else "queued"}
    M = json.loads(RawRow["measurement_json"])
    Out = {"status": "measured", "x": M.get("extent_x"), "y": M.get("extent_y"), "z": M.get("extent_z"),
           "volume": M.get("volume"), "area": M.get("area"), "faces": M.get("faces"), "closed": M.get("closed_heuristic"),
           "integrity": RawRow.get("integrity"), "units": "mm" if Scaling == "exact" else "model",
           "method_version": M.get("method_version"), "measured_at": RawRow.get("measured_at")}
    if Product == Products.Charm:
        Out["height"] = M.get("height")
    else:
        Out.update({"bore_ok": bool(M.get("bore_ok")), "inner_diameter": M.get("inner_diameter") if M.get("bore_ok") else None,
                    "roundness": M.get("roundness")})
        Out["us_size"] = UsSizeFor(Out["inner_diameter"]) if Scaling == "exact" else None
    Sides = [X for X in (Out["x"], Out["y"], Out["z"]) if X is not None]
    Out["units_warning"] = bool(Scaling == "exact" and Sides and not UnitsMinMm <= max(Sides) <= UnitsMaxMm)
    return Out


def _Text(Value, Max: int) -> str:
    return " ".join(str(Value or "").split())[:Max]


def PlaceholderPng(Product: str) -> bytes:
    """The image an external design shows until its 3D render is ready (seconds): plain, no AI involved, nothing internal
    on it (its owner may see it)."""
    import io

    from PIL import Image, ImageDraw, ImageFont
    Img = Image.new("RGB", (1024, 1024), (245, 245, 244))
    Draw = ImageDraw.Draw(Img)
    Draw.rectangle((40, 40, 983, 983), outline=(214, 211, 209), width=4)

    def Font(Size):
        try:
            return ImageFont.load_default(size=Size)
        except TypeError:                                    # Pillow before 10.1: one small bitmap font
            return ImageFont.load_default()
    for Y, Text, Size, Colour in ((450, Products.Labels.get(Product, "Part"), 72, (68, 64, 60)),
                                  (550, "Picture in progress", 36, (120, 113, 108))):
        F = Font(Size)
        W = Draw.textlength(Text, font=F)
        Draw.text(((1024 - W) / 2, Y), Text, fill=Colour, font=F)
    Out = io.BytesIO()
    Img.save(Out, "PNG", optimize=True)
    return Out.getvalue()


class VersionService:
    def __init__(self, Ctx: Context, Meshes, Production, Queue, Uploads):
        self.Ctx = Ctx
        self.Production = Production
        self.Queue = Queue
        self.Uploads = Uploads
        Meshes.OnReady.append(self._HiReady)              # a new Hi3D model is a new version
        Production.OnPreview.append(self.MaybeRender)     # an external design's picture: a render of its current version
        Production.OnMeasureFailed.append(self._MeasureFailed)   # an unmeasurable file is never the current version
        Queue.Handlers["render"] = self._Rendered

    # ── reading ──────────────────────────────────────────────────────────
    def _Design(self, DesignId: str) -> dict:
        D = self.Ctx.Db.One("SELECT * FROM designs WHERE id = ?", (DesignId,))
        if D is None:
            raise HttpError(404, "design_not_found", "Design not found.")
        return D

    def _Mesh(self, MeshId: str) -> dict:
        M = self.Ctx.Db.One("SELECT m.*, b.design_id FROM meshes m JOIN candidates c ON c.id = m.candidate_id "
                            "JOIN batches b ON b.id = c.batch_id WHERE m.id = ?", (MeshId,))
        if M is None or M["status"] != "ready":
            raise HttpError(404, "version_not_found", "3D version not found.")
        return M

    def List(self, DesignId: str, IncludeArchived: bool = True) -> list[dict]:
        """Every ready version of the design, newest first, with its measurement and where it is used."""
        Db = self.Ctx.Db
        D = self._Design(DesignId)
        Product = D.get("product_type") or Products.Ring
        Rows = Db.All(_DesignMeshes + " AND m.status = 'ready'" + ("" if IncludeArchived else " AND m.archived_at IS NULL")
                      + " ORDER BY COALESCE(m.version_no, 0) DESC, m.created_at DESC", (DesignId,))
        if not Rows:
            return []
        Ids = [M["id"] for M in Rows]
        Q = ",".join("?" * len(Ids))
        Raw = {R["mesh_id"]: R for R in Db.All(f"SELECT * FROM raw_geometry WHERE mesh_id IN ({Q})", Ids)}
        Jobs = {J["mesh_id"]: J for J in Db.All(f"SELECT mesh_id, status FROM geometry_jobs WHERE kind = 'measure' AND mesh_id IN ({Q}) "
                                                 "AND status IN ('queued', 'running')", Ids)}
        Results = {R["mesh_id"]: R["n"] for R in Db.All(f"SELECT mesh_id, COUNT(*) AS n FROM session_3d WHERE mesh_id IN ({Q}) "
                                                         "GROUP BY mesh_id", Ids)}
        Orders = {}
        for R in Db.All(f"SELECT l.mesh_id, o.order_no FROM order_lines l JOIN orders o ON o.id = l.order_id WHERE l.mesh_id IN ({Q}) "
                        "ORDER BY o.order_no", Ids):
            Orders.setdefault(R["mesh_id"], []).append(f"ORD-{R['order_no']}")
        Refs = RingIds.CandidateRefs(Db, [DesignId])
        Numbers = {M["id"]: M.get("version_no") for M in Rows}
        Active = ActiveMesh(Db, DesignId)
        ActiveId = Active["id"] if Active else None
        Newest = NewestMesh(Db, DesignId)
        Out = []
        for M in Rows:
            R = Raw.get(M["id"])
            Prov = json.loads(M["provenance_json"] or "{}")
            Failed = bool(R and R["status"] == "failed")
            Out.append({
                **Brief(M, ActiveId),
                "id": M["id"], "state": "active" if M["id"] == ActiveId else ("archived" if M["archived_at"] else
                                                                            "unusable" if Failed else "superseded"),
                # LIFO: the newest usable version is the default; the current one differs only when an Admin chose it
                "newest": bool(Newest and Newest["id"] == M["id"]), "usable": not Failed and not M["archived_at"],
                "note": M.get("note"), "created_by": M.get("created_by") or ("Hi3D" if (M.get("source") or "hi3d") == "hi3d" else None),
                "archived_at": M.get("archived_at"), "archived_by": M.get("archived_by"),
                "parent": {"id": M["parent_mesh_id"], "version_no": Numbers.get(M["parent_mesh_id"]) or Prov.get("parent_version_no")}
                if M.get("parent_mesh_id") else None,
                "ring_id": Refs.get(M["candidate_id"]),
                "file": {"bytes": R["bytes"] if R else None, "sha256": R["sha256"] if R else None,
                         "faces": R["faces"] if R else None, "format": M["original_format"],
                         "uploaded_sha256": Prov.get("sha256"), "uploaded_format": Prov.get("format")},
                "measure": Measurement(R, Product, M.get("scaling") or "scalable", Jobs.get(M["id"])),
                "preview_ready": bool(R and R["preview_path"]),
                "results": Results.get(M["id"], 0), "orders": Orders.get(M["id"], []),
            })
        return Out

    def Get(self, MeshId: str) -> dict:
        M = self._Mesh(MeshId)
        return next(V for V in self.List(M["design_id"]) if V["id"] == MeshId)

    def FilePath(self, MeshId: str):
        """A version's own file (the binary STL it is measured from) and its download name:
        <Design-Name>_<Option ID>_v<n>.stl — e.g. Eclat-Heart_C-1029-B_v2.stl."""
        from p3.production3d import SlugPart
        M = self._Mesh(MeshId)
        P = assets.Resolve(self.Ctx.Settings.DevDir, M["original_path"]) if M["original_path"] else None
        if P is None or not P.is_file():
            raise HttpError(404, "version_file_missing", "This version's file is not available.")
        D = self._Design(M["design_id"])
        Name = "_".join(X for X in (SlugPart(D["title"]), RingIds.CandidateRef(self.Ctx.Db, M["candidate_id"]),
                                    f"v{M['version_no']}" if M["version_no"] else "") if X)
        return P, Name + (P.suffix or ".stl")

    def PreviewPath(self, MeshId: str):
        """A version's light preview file (P3PV), for the 3D view of a version that has no sized result yet."""
        self._Mesh(MeshId)
        R = self.Ctx.Db.One("SELECT preview_path FROM raw_geometry WHERE mesh_id = ?", (MeshId,))
        P = assets.Resolve(self.Ctx.Settings.DevDir, R["preview_path"]) if R and R["preview_path"] else None
        if P is None or not P.is_file():
            raise HttpError(404, "preview_not_ready", "The light 3D preview of this version is not ready yet.")
        return P

    # ── switching: active · archived · restored ──────────────────────────
    def Activate(self, MeshId: str, By: str, Note: str = "") -> dict:
        """Use this version: the design's current one from now on (rollback = choosing an older one), until the Admin chooses
        another or a newer version is added (LIFO). New 3D results and new orders use it; existing results and order lines
        pinned to another version keep theirs."""
        Db, M = self.Ctx.Db, self._Mesh(MeshId)
        if M["archived_at"]:
            raise HttpError(409, "version_archived", "This version is archived. Restore it first.")
        if Db.One("SELECT 1 AS x FROM raw_geometry WHERE mesh_id = ? AND status = 'failed'", (MeshId,)):
            raise HttpError(409, "version_unusable", "This version's file could not be measured, so it cannot be used. "
                                                     "Upload a corrected file as a new version.")
        D = self._Design(M["design_id"])
        Old = ActiveMesh(Db, D["id"])
        if not (Old and Old["id"] == MeshId and D.get("active_mesh_id") == MeshId):
            Db.Execute("UPDATE designs SET active_mesh_id = ? WHERE id = ?", (MeshId, D["id"]))     # not customer activity
            Sessions.Record(self.Ctx, D["owner_account_id"], "admin_3d_version_activated", D["id"], mesh_id=MeshId,
                            version_no=M["version_no"], label=Label(M), from_mesh_id=Old["id"] if Old else None,
                            from_version_no=Old["version_no"] if Old else None, note=_Text(Note, MaxNote), by=By)
            self.MaybeRender(MeshId)
        return {"versions": self.List(D["id"]), "active_mesh_id": MeshId}

    def Archive(self, MeshId: str, By: str) -> dict:
        Db, M = self.Ctx.Db, self._Mesh(MeshId)
        D = self._Design(M["design_id"])
        if (ActiveMesh(Db, D["id"]) or {}).get("id") == MeshId:
            raise HttpError(409, "version_active", "This is the active version. Make another version active first.")
        if not M["archived_at"]:
            Db.Execute("UPDATE meshes SET archived_at = ?, archived_by = ? WHERE id = ?", (Now(), By, MeshId))
            Sessions.Record(self.Ctx, D["owner_account_id"], "admin_3d_version_archived", D["id"], mesh_id=MeshId,
                            version_no=M["version_no"], label=Label(M), by=By)
        return {"versions": self.List(D["id"]), "active_mesh_id": (ActiveMesh(Db, D["id"]) or {}).get("id")}

    def Restore(self, MeshId: str, By: str) -> dict:
        Db, M = self.Ctx.Db, self._Mesh(MeshId)
        D = self._Design(M["design_id"])
        if M["archived_at"]:
            Db.Execute("UPDATE meshes SET archived_at = NULL, archived_by = NULL WHERE id = ?", (MeshId,))
            Sessions.Record(self.Ctx, D["owner_account_id"], "admin_3d_version_restored", D["id"], mesh_id=MeshId,
                            version_no=M["version_no"], label=Label(M), by=By)
        return {"versions": self.List(D["id"]), "active_mesh_id": (ActiveMesh(Db, D["id"]) or {}).get("id")}

    # ── a new version from an uploaded STL ───────────────────────────────
    def AddVersion(self, DesignId: str, UploadId: str, By: str, CandidateId: str | None = None, Scaling: str = "exact",
                   SourceTool: str = "", Note: str = "", Activate: bool = True, ParentMeshId: str | None = None,
                   Context_: dict | None = None) -> dict:
        """An STL made outside Atelier becomes the design's next version — a permanent asset of the part: moved into place
        (never copied), measured once in the geometry worker (an ASCII STL is converted to binary there), its preview and
        edge check in the background. Activate (default, LIFO) = the newest version becomes the current one; False keeps
        the current one (an Admin's explicit choice)."""
        Db = self.Ctx.Db
        D = self._Design(DesignId)
        if Scaling not in Scalings:
            raise HttpError(400, "invalid_scaling", "Scaling is 'exact' (use exactly as uploaded) or 'scalable'.")
        Parent = None
        if ParentMeshId:
            Parent = Db.One(_DesignMeshes + " AND m.id = ? AND m.status = 'ready'", (DesignId, ParentMeshId))
            if Parent is None:
                raise HttpError(404, "version_not_found", "The version this revises is not a version of this design.")
        if CandidateId:
            if not Db.One("SELECT 1 AS x FROM candidates c JOIN batches b ON b.id = c.batch_id WHERE c.id = ? AND b.design_id = ? "
                          "AND c.status = 'ready'", (CandidateId, DesignId)):
                raise HttpError(404, "candidate_not_found", "That option does not belong to this design.")
        else:                     # the option of the version it revises, else of the active version, else the selected option
            Base = Parent or ActiveMesh(Db, DesignId)
            CandidateId = (Base or {}).get("candidate_id") or D["selected_candidate_id"]
            if not CandidateId:
                First = Db.One("SELECT c.id FROM candidates c JOIN batches b ON b.id = c.batch_id WHERE b.design_id = ? "
                               "AND c.status = 'ready' ORDER BY b.created_at, c.slot LIMIT 1", (DesignId,))
                CandidateId = First["id"] if First else None
            if not CandidateId:
                raise HttpError(409, "no_option", "This design has no finished option yet to attach a 3D version to — add the "
                                                  "part as a new design of this session instead.")
        if Parent is None:
            Parent = MeshFor(Db, DesignId, CandidateId)
        U = self.Uploads.Ready(UploadId)
        MeshId = NewId("mesh")
        Rel = f"meshes/{MeshId}/original.stl"
        Tool = _Text(SourceTool, MaxTool) or None
        self.Uploads.Take(UploadId, Rel, MeshId)                  # moved into place
        try:
            T = Now()
            Prov = {"source": "external", "original_filename": U["filename"], "source_tool": Tool, "uploaded_by": By,
                    "uploaded_at": T, "upload_id": UploadId, "sha256": U["sha256"], "bytes": U["bytes"], "format": U["format"],
                    "faces": U["faces"], "scaling": Scaling, "design_id": DesignId, "candidate_id": CandidateId,
                    "parent_mesh_id": Parent["id"] if Parent else None,
                    "parent_version_no": Parent["version_no"] if Parent else None, **(Context_ or {})}
            with Db.Transaction() as Conn:
                No = Conn.execute("SELECT COALESCE(MAX(m.version_no), 0) + 1 FROM meshes m JOIN candidates c ON c.id = m.candidate_id "
                                  "JOIN batches b ON b.id = c.batch_id WHERE b.design_id = ?", (DesignId,)).fetchone()[0]
                Conn.execute("INSERT INTO meshes (id, candidate_id, endpoint, settings_json, config_version, status, original_path, "
                             "original_format, stl_path, provenance_json, created_at, updated_at, version_no, source, scaling, "
                             "parent_mesh_id, source_tool, original_filename, note, created_by) "
                             "VALUES (?,?,?,?,?, 'ready', ?, 'stl', ?, ?,?,?,?, 'external', ?,?,?,?,?,?)",
                             (MeshId, CandidateId, ExternalEndpoint, "{}", ExternalEndpoint, Rel, Rel, Dumps(Prov), T, T, No, Scaling,
                              Parent["id"] if Parent else None, Tool, U["filename"], _Text(Note, MaxNote) or None, By))
                # An ASCII STL is kept as the binary STL it is converted to: that file's hash and size are taken when it is
                # measured (the uploaded file's stay in the provenance)
                Binary = U["format"] == "binary"
                Conn.execute("INSERT INTO raw_geometry (mesh_id, stl_path, sha256, bytes, faces, status, timings_json, created_at, "
                             "updated_at) VALUES (?,?,?,?,?, 'downloaded', '{}', ?, ?)",
                             (MeshId, Rel, U["sha256"] if Binary else None, U["bytes"] if Binary else None, U["faces"], T, T))
                if Activate:
                    Conn.execute("UPDATE designs SET active_mesh_id = ? WHERE id = ?", (MeshId, DesignId))
        except Exception:
            self.Uploads.Untake(UploadId, Rel)
            raise
        Sessions.Record(self.Ctx, D["owner_account_id"], "admin_3d_version_added", DesignId, mesh_id=MeshId, version_no=No,
                        source="external", source_tool=Tool, filename=U["filename"], bytes=U["bytes"], scaling=Scaling,
                        parent_version_no=Parent["version_no"] if Parent else None, active=bool(Activate),
                        note=_Text(Note, MaxNote), by=By, **{K: V for K, V in (Context_ or {}).items() if K in ("order_ref", "line_id")})
        self.Production.MeasureVersion(MeshId)                    # one measure job (ASCII → binary), then preview + edge check
        return self.Get(MeshId)

    # ── sessions (they start on the website; one becomes explicit when it gets a second design) ──
    def _Session(self, SessionId: str) -> dict:
        S = self.Ctx.Db.One("SELECT * FROM sessions WHERE id = ?", (SessionId,))
        if S is None:
            raise HttpError(404, "session_not_found", "Session not found.")
        return S

    def EnsureSession(self, DesignId: str, By: str) -> dict:
        """The design's session as an explicit one — made now from the design's own journey (the session its owner started
        on the website; owner = the design's owner) when it gets a second design."""
        D = self._Design(DesignId)
        if D.get("session_id"):
            return self._Session(D["session_id"])
        Sid, T = NewId("ses"), Now()
        with self.Ctx.Db.Transaction() as Conn:
            Conn.execute("INSERT INTO sessions (id, owner_account_id, title, origin, created_by, created_at, updated_at) "
                         "VALUES (?,?,?, 'design', ?,?,?)", (Sid, D["owner_account_id"], _Text(D["title"], MaxTitle), By, T, T))
            Conn.execute("UPDATE designs SET session_id = ? WHERE id = ? AND session_id IS NULL", (Sid, DesignId))
        S = self._Session(self._Design(DesignId)["session_id"])
        Sessions.Record(self.Ctx, D["owner_account_id"], "admin_session_created", DesignId, session_id=S["id"],
                        session_ref=SessionRef(S["session_no"]), title=S["title"], from_design=True, by=By)
        return S

    def SessionJson(self, S: dict, WithDesigns: bool = False) -> dict:
        Db = self.Ctx.Db
        Designs = Db.All("SELECT id, title, product_type, ring_no, charm_no, origin, created_at FROM designs WHERE session_id = ? "
                         "ORDER BY created_at", (S["id"],))
        try:
            Owner = self.Ctx.Accounts.AdminGet(S["owner_account_id"])
        except Exception:  # noqa: BLE001 — an unknown owner must not hide the session
            Owner = None
        Out = {"id": S["id"], "ref": SessionRef(S["session_no"]), "title": S["title"], "origin": S["origin"],
               "owner_account_id": S["owner_account_id"], "owner_name": (Owner or {}).get("name") or "",
               "owner_email": (Owner or {}).get("email") or "", "created_by": S["created_by"], "created_at": S["created_at"],
               "design_count": len(Designs),
               "designs": [{"id": D["id"], "title": D["title"], "ring_id": RingIds.Ref(D), "product_type": D["product_type"] or "ring",
                            "external": D["origin"] == "external"} for D in Designs]}
        if WithDesigns:
            Rows = Sessions.Summaries(self.Ctx, DesignIds=[D["id"] for D in Designs]) if Designs else []
            for R in Rows:
                A = ActiveMesh(Db, R["design_id"])
                R["active_version"] = Brief(A, A["id"]) if A else None
            Out["journeys"] = Rows
        return Out

    def Containers(self) -> list[dict]:
        return [self.SessionJson(S) for S in self.Ctx.Db.All("SELECT * FROM sessions ORDER BY created_at DESC")]

    def Container(self, SessionId: str) -> dict:
        return self.SessionJson(self._Session(SessionId), WithDesigns=True)

    def SessionOf(self, DesignId: str) -> dict | None:
        D = self.Ctx.Db.One("SELECT session_id FROM designs WHERE id = ?", (DesignId,))
        return self.SessionJson(self._Session(D["session_id"])) if D and D.get("session_id") else None

    # ── a new external design ────────────────────────────────────────────
    def AddExternalDesign(self, SessionKey: str, UploadId: str, By: str, Product: str, Title: str, Scaling: str = "exact",
                          SourceTool: str = "", Note: str = "", Force: bool = False) -> dict:
        """A part made outside Atelier becomes a design of its own in an existing session (an explicit session, or the
        session of the design it is added next to — also one with no finished part yet): one option (its picture is a
        render of the part), version 1 = the file, current. It is an ordinary part of that session: the owner sees it in
        My Designs and can order it like any design (the order line is pinned to its current version); the STL itself,
        like every 3D file, stays with the Admin. Not offered in the Inspiration Gallery."""
        Db = self.Ctx.Db
        Product = Products.Normalize(Product)
        Title = _Text(Title, 60)
        if len(Title) < 2:
            raise HttpError(400, "invalid_title", "Give the part a name (2–60 characters).")
        if Scaling not in Scalings:
            raise HttpError(400, "invalid_scaling", "Scaling is 'exact' (use exactly as uploaded) or 'scalable'.")
        Dup = Db.One("SELECT id, ring_no, charm_no, product_type FROM designs WHERE lower(title) = lower(?)", (Title,))
        if Dup and not Force:
            raise HttpError(409, "duplicate_title", f"Another design is already called “{Title}” ({RingIds.Ref(Dup)}). "
                                                    "Give the part a distinctive name, or confirm to use it anyway.")
        if SessionKey.startswith("ses_"):
            S = self._Session(SessionKey)
        elif SessionKey.startswith("use_"):
            raise HttpError(409, "shared_design", "A shared gallery design is not part of the customer's own session: add the part "
                                                  "from one of the customer's own sessions.")
        else:
            S = self.EnsureSession(SessionKey, By)
        U = self.Uploads.Ready(UploadId)
        DesignId, BatchId, CandId, T = NewId("dsg"), NewId("bat"), NewId("cand"), Now()
        Image = PlaceholderPng(Product)
        Rel = f"designs/{DesignId}/external/placeholder.png"
        assets.WriteAtomic(self.Ctx.Settings.AssetsDir, Rel, Image)
        Tool = _Text(SourceTool, MaxTool)
        with Db.Transaction() as Conn:
            Conn.execute("INSERT INTO designs (id, owner_account_id, title, prompt, selected_candidate_id, created_at, updated_at, "
                         "ai_mode, product_type, origin, session_id) VALUES (?,?,?,?,?,?,?, 'external', ?, 'external', ?)",
                         (DesignId, S["owner_account_id"], Title, Title, CandId, T, T, Product, S["id"]))   # its words: the name
            Conn.execute("INSERT INTO batches (id, design_id, kind, parent_candidate_id, user_text, effective_prompt, endpoint, "
                         "reference_asset, desired_count, config_version, created_at) VALUES (?,?, 'initial', NULL, ?, '', ?, NULL, 1, ?, ?)",
                         (BatchId, DesignId, Title, ExternalEndpoint, ExternalEndpoint, T))     # file and tool: the version's
            Conn.execute("INSERT INTO candidates (id, batch_id, slot, status, seed, asset_path, content_sha256, credit_ref, created_at, "
                         "updated_at) VALUES (?,?, 0, 'ready', 0, ?,?,?,?,?)", (CandId, BatchId, Rel, assets.Sha256(Image), BatchId, T, T))
            Conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (T, S["id"]))
        Sessions.Record(self.Ctx, S["owner_account_id"], "admin_external_design_added", DesignId, session_id=S["id"],
                        session_ref=SessionRef(S["session_no"]), filename=U["filename"], source_tool=Tool or None, scaling=Scaling,
                        product_type=Product, by=By)
        V = self.AddVersion(DesignId, UploadId, By, CandidateId=CandId, Scaling=Scaling, SourceTool=Tool, Note=Note, Activate=True)
        return {"session": self.SessionJson(self._Session(S["id"])), "design_id": DesignId, "version": V}

    # ── hooks: a Hi3D model is a version too; an external design's picture ──
    def _HiReady(self, MeshId: str, Ok: bool) -> None:
        """A Hi3D model that became ready is the design's next version — and its current one (LIFO, like every new version)."""
        if not Ok:
            return
        Db = self.Ctx.Db
        M = Db.One("SELECT m.id, m.version_no, m.source, b.design_id, d.active_mesh_id, d.owner_account_id FROM meshes m "
                   "JOIN candidates c ON c.id = m.candidate_id JOIN batches b ON b.id = c.batch_id "
                   "JOIN designs d ON d.id = b.design_id WHERE m.id = ? AND m.status = 'ready'", (MeshId,))
        if M is None or (M["source"] or "hi3d") != "hi3d":
            return
        with Db.Transaction() as Conn:
            if M["version_no"] is None:
                Conn.execute("UPDATE meshes SET version_no = (SELECT COALESCE(MAX(m.version_no), 0) + 1 FROM meshes m "
                             "JOIN candidates c ON c.id = m.candidate_id JOIN batches b ON b.id = c.batch_id WHERE b.design_id = ?) "
                             "WHERE id = ?", (M["design_id"], MeshId))
            Conn.execute("UPDATE designs SET active_mesh_id = ? WHERE id = ?", (MeshId, M["design_id"]))
        No = Db.One("SELECT version_no FROM meshes WHERE id = ?", (MeshId,))["version_no"]
        Sessions.Record(self.Ctx, M["owner_account_id"], "admin_3d_version_added", M["design_id"], mesh_id=MeshId, version_no=No,
                        source="hi3d", active=True, by="hi3d")

    def _MeasureFailed(self, MeshId: str) -> None:
        """A version whose file could not be measured is not usable: when it was the current one, the newest usable version
        takes its place (recorded on the journey)."""
        Db = self.Ctx.Db
        M = Db.One("SELECT m.id, m.version_no, m.source, m.source_tool, m.scaling, b.design_id, d.active_mesh_id, d.owner_account_id "
                   "FROM meshes m JOIN candidates c ON c.id = m.candidate_id JOIN batches b ON b.id = c.batch_id "
                   "JOIN designs d ON d.id = b.design_id WHERE m.id = ?", (MeshId,))
        if M is None or M["active_mesh_id"] != MeshId:
            return
        New = ActiveMesh(Db, M["design_id"])             # the pointer names an unusable version: the newest usable one
        Db.Execute("UPDATE designs SET active_mesh_id = ? WHERE id = ?", (New["id"] if New else None, M["design_id"]))
        Sessions.Record(self.Ctx, M["owner_account_id"], "admin_3d_version_activated", M["design_id"],
                        mesh_id=New["id"] if New else None, version_no=New["version_no"] if New else None, label=Label(New),
                        from_mesh_id=MeshId, from_version_no=M["version_no"], note="its file could not be measured", by="system")

    def MaybeRender(self, MeshId: str) -> None:
        """An external design shows a render of its active version (once that version's light preview exists)."""
        Db = self.Ctx.Db
        R = Db.One("SELECT m.id, m.version_no, c.asset_path, d.id AS design_id, d.origin, d.product_type, r.preview_path FROM meshes m "
                   "JOIN candidates c ON c.id = m.candidate_id JOIN batches b ON b.id = c.batch_id JOIN designs d ON d.id = b.design_id "
                   "LEFT JOIN raw_geometry r ON r.mesh_id = m.id WHERE m.id = ?", (MeshId,))
        if not R or R["origin"] != "external" or not R["preview_path"]:
            return
        if (ActiveMesh(Db, R["design_id"]) or {}).get("id") != MeshId or (R["asset_path"] or "").endswith(f"/external/v{R['version_no']}.png"):
            return
        self.Queue.Enqueue("render", MeshId, Params={"source": R["preview_path"], "output": f"meshes/{MeshId}/render.png",
                                                     "product": R["product_type"] or Products.Ring})

    def _Rendered(self, Job: dict, Result, Error, Started: bool = False) -> None:
        if Started:
            return
        if Error:
            Logger.warning("Render of %s failed: %s", Job["mesh_id"], Error)
            return
        Db, S = self.Ctx.Db, self.Ctx.Settings
        P = S.DevDir / json.loads(Job["params_json"])["output"]
        R = Db.One("SELECT m.candidate_id, m.version_no, b.design_id, d.origin FROM meshes m JOIN candidates c ON c.id = m.candidate_id "
                   "JOIN batches b ON b.id = c.batch_id JOIN designs d ON d.id = b.design_id WHERE m.id = ?", (Job["mesh_id"],))
        try:
            if not R or R["origin"] != "external" or not P.is_file():
                return
            if (ActiveMesh(Db, R["design_id"]) or {}).get("id") != Job["mesh_id"]:
                return                                   # another version became active meanwhile: its own render follows
            Data = P.read_bytes()
            Rel = f"designs/{R['design_id']}/external/v{R['version_no']}.png"      # what its owner may see: no internal id
            assets.WriteAtomic(S.AssetsDir, Rel, Data)
            Db.Update("candidates", R["candidate_id"], asset_path=Rel, content_sha256=assets.Sha256(Data))
        finally:
            P.unlink(missing_ok=True)
