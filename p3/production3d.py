"""Admin-only 3D production geometry for a session: Hi3D v3.0 → measure the raw STL once → exact
values for any ring size and material by arithmetic → weight / cost / 3D price.

Rules:
  * 3D never starts automatically — only Production3D.Request(), called from the Admin.
  * Every request has a target ring size: the customer's size, else US 10 (default), and the admin
    may override it. Both the customer size and the production size are stored with their source.
  * The raw Hi3D STL (5M faces) is the master geometry. It is measured exactly once (raw_geometry):
    a new size or material is pure arithmetic — length × s, area × s², volume × s³, weight = volume ×
    density — with no file read and no paid Hi3D call.
  * Heavy local work runs one job at a time in the persistent geometry queue (p3.geoqueue).
    Retrying a failed local stage never repeats Hi3D when the raw STL exists.
  * The light preview and the mesh-integrity check run in the background after the numbers are shown;
    the preview is visual only and never used for geometry or pricing.
  * A scaled STL is never stored permanently: it is exported on demand to a temporary file (TTL).
  * The customer's fixed price is copied in for comparison only; nothing here changes it.

Charms have a path of their own (p3/charmgeometry.py), chosen by the design's product: no bore and no ring
size — the size is the charm's total height in mm, loop included (products.CharmSizeDefinition), the whole
charm is scaled to it (products.Charm3DHeight), and its weight, cost and 3D price come from the charm price
book. There is no loop detection or measurement. The ring path below is unchanged.
"""

import json
import logging
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from p3 import assets
from p3 import charmgeometry as CharmGeo
from p3 import charmprices as CharmPrices
from p3 import products as Products
from p3 import ringids as RingIds
from p3 import sessions as Sessions
from p3 import stages as Stages
from p3.context import Context, HttpError
from p3.db import Dumps, NewId, Now
from p3.geometry import FastMethodVersion, Scaled, UsSizeToInnerDiameterMm

Logger = logging.getLogger("p3.production3d")
DefaultSize = 10.0
Terminal = ("measured", "needs_review", "failed", "cancelled")
NewModelConfirmation = "GENERATE NEW 3D"      # typed by the admin to allow a second paid Hi3D model for a design
Waiting = ("requested", "generating", "queued", "measuring")
MaxRoundness = 0.04            # bore deviation from a circle above this → needs review


def _Seconds(A: str | None, B: str | None) -> float | None:
    if not A or not B:
        return None
    return round((datetime.fromisoformat(B) - datetime.fromisoformat(A)).total_seconds(), 3)


def ProductionState(Status: str, Integrity: str | None = None) -> str:
    """Production readiness, kept apart from processing: processing · complete · review_required ·
    failed · cancelled. "Complete" means the numbers exist AND nothing was flagged; any warning
    (no bore, bore not round, open mesh heuristic, open edges found later in the background)
    makes it review_required — inspecting or downloading the model never approves it."""
    if Status in ("failed", "cancelled"):
        return Status
    if Status in Waiting:
        return "processing"
    if Status == "needs_review" or Integrity == "open":
        return "review_required"
    return "complete"


def ReviewItems(Status: str, Raw: dict | None, Integrity: str | None) -> list[dict]:
    """Why a result needs production review, each with the recommended next step (shown before the
    measurements). Empty when the model passed every check."""
    Items = []
    if Status in ("failed", "cancelled") or Status in Waiting:
        return Items
    Raw = Raw or {}
    if not Raw.get("bore_ok", True):
        Items.append({"code": "no_bore", "text": "No ring bore was found — the model may not be a ring.",
                      "action": "Inspect the model in 3D. If it is not a ring, do not produce it: model a different "
                                "option (a new Hi3D model) instead."})
    elif Raw.get("roundness") is not None and Raw["roundness"] > MaxRoundness:
        Items.append({"code": "bore_not_round", "text": f"The bore is not round ({Raw['roundness'] * 100:.1f}% deviation) — "
                                                        "the inner diameter is uncertain.",
                      "action": "Check the inner diameter against the target size in the 3D view before production; "
                                "size by hand if needed."})
    if Raw and not Raw.get("closed_heuristic", True):
        Items.append({"code": "open_mesh_heuristic", "text": "The volume reference check suggests the mesh may not be closed — "
                                                             "volume and weight may be wrong.",
                      "action": "Wait for the edge check, then repair the mesh (close the holes) before production."})
    if Integrity == "open":
        Items.append({"code": "open_edges", "text": "Open edges were found — the mesh is not watertight.",
                      "action": "Repair the mesh (close the holes) before production; treat weight and cost as estimates."})
    return Items


def SlugPart(Text: str, Max: int = 40) -> str:
    """File-name-safe words joined by '-': 'The Orion Ring' → 'The-Orion-Ring', 'Éternité' → 'Eternite'
    (accents transliterated, never dropped)."""
    Ascii = unicodedata.normalize("NFKD", Text or "").encode("ascii", "ignore").decode("ascii")
    return "-".join(re.findall(r"[A-Za-z0-9]+", Ascii))[:Max].rstrip("-")


class Production3D:
    def __init__(self, Ctx: Context, Meshes, Queue):
        self.Ctx = Ctx
        self.Meshes = Meshes
        self.Queue = Queue
        Meshes.OnReady.append(self._MeshFinished)
        Queue.Handlers.update({"measure": self._Measured, "preview": self._Previewed, "integrity": self._Integrity})

    # ── admin request ────────────────────────────────────────────────────
    def Request(self, DesignId: str, ProductionSize=None, MaterialId: str | None = None,
                CandidateId: str | None = None, RequestedBy: str = "admin", Customer: dict | None = None,
                Override: str | None = None) -> dict:
        """Customer = the journey whose size/material are the defaults (a gallery customer's session on a
        shared master design); otherwise the design owner's session.

        Safeguard: a design that already has a valid Hi3D model never gets a second paid model by
        default — the existing raw model is reused for any size and material, whichever option is
        selected. A new model for another option requires Override == NewModelConfirmation (typed by
        the admin) and is recorded as an admin override. A refined design (new images, its own design)
        is a different design and gets its own first model normally."""
        Db, Cat = self.Ctx.Db, self.Ctx.Catalog
        Summary = Customer or (Sessions.Summaries(self.Ctx, [DesignId]) or [None])[0]
        if Summary is None:
            raise HttpError(404, "session_not_found", "Session not found.")
        Design = Db.One("SELECT * FROM designs WHERE id = ?", (DesignId,))
        Existing = Db.One("SELECT m.* FROM meshes m JOIN candidates c ON c.id = m.candidate_id JOIN batches b ON b.id = c.batch_id "
                          "WHERE b.design_id = ? AND m.status = 'ready' ORDER BY m.created_at DESC LIMIT 1", (DesignId,))
        if not CandidateId and Existing:
            CandidateId = Existing["candidate_id"]              # the modelled option: reuse, never a new Hi3D call
        # No model yet: the journey's own selected option (a gallery customer's pick), else the design's.
        CandidateId = CandidateId or Summary.get("selected_candidate_id") or Design["selected_candidate_id"]
        if not CandidateId:
            First = Db.One("SELECT c.id FROM candidates c JOIN batches b ON b.id = c.batch_id WHERE b.design_id = ? "
                           "AND c.status = 'ready' ORDER BY b.created_at DESC, c.slot LIMIT 1", (DesignId,))
            CandidateId = First["id"] if First else None
        Cand = Db.One("SELECT c.* FROM candidates c JOIN batches b ON b.id = c.batch_id WHERE c.id = ? AND b.design_id = ?",
                      (CandidateId, DesignId)) if CandidateId else None
        if Cand is None or Cand["status"] != "ready":
            raise HttpError(409, "no_ready_image", "This session has no ready design image to turn into 3D.")
        Charm = (Design.get("product_type") or Products.Ring) == Products.Charm
        if Charm:
            Size, SizeSource, CustomerSize = self._CharmSize(Summary, ProductionSize)
        else:
            CustomerSize = Summary["ring_size"] if Summary["ring_size_chosen"] else None   # the default US 10 isn't a choice
            if ProductionSize in (None, ""):
                Size, SizeSource = (float(CustomerSize), "customer") if CustomerSize is not None else (DefaultSize, "default")
            else:
                Size = float(ProductionSize)
                if not Cat.IsValidSize(Size):
                    raise HttpError(400, "invalid_ring_size", "Please choose a standard US ring size.")
                SizeSource = ("customer" if CustomerSize is not None and Size == float(CustomerSize)
                              else "default" if CustomerSize is None and Size == DefaultSize else "admin_override")
        CustomerMaterial = Summary["material_id"] if Summary["material_chosen"] else None
        if MaterialId:
            if Cat.Get(MaterialId) is None:
                raise HttpError(400, "unknown_material", "Unknown material.")
            if Charm and not CharmPrices.IsOffered(Cat, MaterialId):
                raise HttpError(400, "material_not_offered", "This material is not offered for charms.")
            Mat, MatSource = MaterialId, ("customer" if MaterialId == CustomerMaterial else "admin_override")
        elif CustomerMaterial:
            Mat, MatSource = CustomerMaterial, "customer"
        else:
            Mat, MatSource = (Summary["material_id"] or Cat.DefaultMaterialId), "default"
        Raw = Db.One("SELECT * FROM meshes WHERE candidate_id = ? AND status = 'ready' ORDER BY created_at DESC LIMIT 1",
                     (CandidateId,))
        if Raw is None and Existing is not None:
            ExistingRef = RingIds.CandidateRef(Db, Existing["candidate_id"])
            if (Override or "").strip() != NewModelConfirmation:
                raise HttpError(409, "hi3d_model_exists",
                                f"A 3D model already exists for this design ({ExistingRef}). Generating a new model would "
                                f"create another paid Hi3D request. Type {NewModelConfirmation} to confirm.")
            Sessions.Record(self.Ctx, Design["owner_account_id"], "admin_3d_new_model_override", DesignId,
                            candidate_id=CandidateId, candidate_ring_id=RingIds.CandidateRef(Db, CandidateId),
                            existing_mesh_id=Existing["id"], existing_ring_id=ExistingRef, by=RequestedBy)
        # A variation of a gallery master (a customer's refinement): its own design, but the master's model
        # already exists — a new paid model needs the same typed confirmation, so the admin decides on purpose
        # whether the shape really changed or the master's model should be used (from the master's session).
        Source = self.SourceModel(Design) if Raw is None and Existing is None else None
        if Source is not None:
            if (Override or "").strip() != NewModelConfirmation:
                raise HttpError(409, "hi3d_source_model_exists",
                                f"This design is a variation of {Source['title']} ({Source['ring_id']}), which already has a 3D model. "
                                f"If the shape changed, type {NewModelConfirmation} to create a new paid Hi3D model; otherwise use "
                                f"the source design's model from its session ({Source['design_ring_id']}).")
            Sessions.Record(self.Ctx, Design["owner_account_id"], "admin_3d_new_model_override", DesignId,
                            candidate_id=CandidateId, candidate_ring_id=RingIds.CandidateRef(Db, CandidateId),
                            source_design_id=Source["design_id"], source_ring_id=Source["ring_id"], by=RequestedBy)
        Mesh = Raw or self.Meshes.Create(CandidateId, None)      # the paid Hi3D call, only when needed
        Sid, T = NewId("s3d"), Now()
        Db.Execute("INSERT INTO session_3d (id, design_id, candidate_id, mesh_id, customer_size, production_size, "
                   "size_source, customer_material, material_id, material_source, status, requested_by, created_at, "
                   "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (Sid, DesignId, CandidateId, Mesh["id"], CustomerSize, Size, SizeSource, CustomerMaterial, Mat,
                    MatSource, "queued" if Raw else "generating", RequestedBy, T, T))
        Sessions.Record(self.Ctx, Design["owner_account_id"], "admin_3d_requested", DesignId, session_3d_id=Sid,
                        mesh_id=Mesh["id"], reused_raw_mesh=bool(Raw), production_size=Size, size_source=SizeSource,
                        material_id=Mat, material_source=MatSource, by=RequestedBy)
        if Raw:
            self._Continue(Mesh["id"])           # measured already → instant; else one queued measure job
        return self.Get(Sid)

    def _CharmSize(self, Summary: dict, ProductionSize) -> tuple:
        """A charm's 3D size in mm (the height it is scaled to): the customer's size, else the middle size on offer.
        The Admin may choose any size between the limits (e.g. one no longer offered, for an existing order)."""
        CustomerSize = Summary.get("charm_size") if Summary.get("charm_size_chosen") else None
        Default = Products.CharmDefaultSize(self.Ctx.Products.CharmSizes)
        if ProductionSize in (None, ""):
            return (float(CustomerSize), "customer", CustomerSize) if CustomerSize is not None else (Default, "default", None)
        try:
            Size = float(ProductionSize)
        except (TypeError, ValueError):
            raise HttpError(400, "invalid_charm_size", "A charm size is a number in mm.")
        if not Products.CharmSizeMin <= Size <= Products.CharmSizeMax:
            raise HttpError(400, "invalid_charm_size", f"A charm size is between {Products.CharmSizeMin:g} and "
                                                       f"{Products.CharmSizeMax:g} mm.")
        Source = ("customer" if CustomerSize is not None and Size == float(CustomerSize)
                  else "default" if CustomerSize is None and Size == Default else "admin_override")
        return Size, Source, CustomerSize

    def _IsCharmMesh(self, MeshId: str) -> bool:
        M = self.Ctx.Db.One("SELECT candidate_id FROM meshes WHERE id = ?", (MeshId,))
        return bool(M) and Products.OfCandidate(self.Ctx.Db, M["candidate_id"]) == Products.Charm

    def SourceModel(self, Design: dict) -> dict | None:
        """For a variation (source_design_id): the source design's ready Hi3D model, if it has one."""
        if not Design or not Design.get("source_design_id"):
            return None
        Db = self.Ctx.Db
        M = Db.One("SELECT m.id, m.candidate_id, d.id AS design_id, d.title, d.ring_no, d.charm_no, d.product_type FROM meshes m "
                   "JOIN candidates c ON c.id = m.candidate_id "
                   "JOIN batches b ON b.id = c.batch_id JOIN designs d ON d.id = b.design_id WHERE b.design_id = ? AND m.status = 'ready' "
                   "ORDER BY m.created_at DESC LIMIT 1", (Design["source_design_id"],))
        if M is None:
            return None
        return {"design_id": M["design_id"], "title": M["title"], "design_ring_id": RingIds.Ref(M),
                "ring_id": RingIds.CandidateRef(Db, M["candidate_id"]), "mesh_id": M["id"]}

    # ── pipeline (event driven: mesh ready → measure job → finalize) ─────
    def _WaitingFor(self, MeshId: str) -> list[dict]:
        return self.Ctx.Db.All(f"SELECT * FROM session_3d WHERE mesh_id = ? AND status IN ({','.join('?' * len(Waiting))}) "
                               "ORDER BY created_at", (MeshId, *Waiting))

    def _MeshFinished(self, MeshId: str, Ok: bool) -> None:
        if not self._WaitingFor(MeshId):
            return                                # e.g. a /dev mesh: nothing waits to be measured
        if Ok:
            self._Continue(MeshId)
            return
        Mesh = self.Ctx.Db.One("SELECT status, error FROM meshes WHERE id = ?", (MeshId,))
        for R in self._WaitingFor(MeshId):
            self._Fail(R["id"], f"Hi3D: {Mesh['error'] or Mesh['status']}")

    def _RawRow(self, MeshId: str) -> dict | None:
        """raw_geometry for a ready mesh — created for meshes downloaded before measure-once existed."""
        Db = self.Ctx.Db
        Row = Db.One("SELECT * FROM raw_geometry WHERE mesh_id = ?", (MeshId,))
        if Row:
            return Row
        Mesh = Db.One("SELECT * FROM meshes WHERE id = ? AND status = 'ready'", (MeshId,))
        if not Mesh or not Mesh["original_path"]:
            return None
        P = assets.Resolve(self.Ctx.Settings.DevDir, Mesh["original_path"])
        if not P.is_file():
            return None
        T = Now()
        Db.Execute("INSERT OR IGNORE INTO raw_geometry (mesh_id, stl_path, sha256, bytes, faces, status, created_at, updated_at) "
                   "VALUES (?,?,NULL,?,NULL,'downloaded',?,?)", (MeshId, Mesh["original_path"], P.stat().st_size, T, T))
        return Db.One("SELECT * FROM raw_geometry WHERE mesh_id = ?", (MeshId,))

    def _Continue(self, MeshId: str) -> None:
        """The raw STL exists: finalize from its measurement, or queue the (single) measure job."""
        Db = self.Ctx.Db
        Raw = self._RawRow(MeshId)
        if Raw is None:
            for R in self._WaitingFor(MeshId):
                self._Fail(R["id"], "The Hi3D model file is missing.")
            return
        Charm = self._IsCharmMesh(MeshId)
        if Raw["status"] == "measured" and Raw["method_version"] == (CharmGeo.CharmMethodVersion if Charm else FastMethodVersion):
            for R in self._WaitingFor(MeshId):
                self._Finalize(R, Raw)
            return
        # Not measured yet, or measured by an older algorithm version: measure (again) once.
        Mesh = Db.One("SELECT original_format FROM meshes WHERE id = ?", (MeshId,))
        Job = self.Queue.Enqueue("measure", MeshId, Params={
            "source": Raw["stl_path"], "format": Mesh["original_format"] or "stl",
            "convert_to": f"meshes/{MeshId}/raw.stl", "hash": not Raw["sha256"], **({"product": "charm"} if Charm else {})})
        Running = Job["status"] == "running"
        for R in self._WaitingFor(MeshId):
            Db.Update("session_3d", R["id"], status="measuring" if Running else "queued", error=None)
            Stages.Begin(Db, R["id"], "calculating_geometry" if Running else "queued")

    def _Measured(self, Job: dict, Result, Error, Started: bool = False) -> None:
        Db, MeshId = self.Ctx.Db, Job["mesh_id"]
        if Started:
            for R in self._WaitingFor(MeshId):
                Db.Update("session_3d", R["id"], status="measuring")
                Stages.Begin(Db, R["id"], "calculating_geometry")
            return
        T = Now()
        if Error == "cancelled":
            for R in self._WaitingFor(MeshId):
                Db.Update("session_3d", R["id"], status="cancelled", error="Cancelled before it started.")
                Stages.Begin(Db, R["id"], "cancelled")
            return
        if Error:
            Db.Execute("UPDATE raw_geometry SET status = 'failed', error = ?, updated_at = ? WHERE mesh_id = ?", (Error, T, MeshId))
            for R in self._WaitingFor(MeshId):
                self._Fail(R["id"], f"Geometry: {Error}. The Hi3D model was kept — a retry costs no Hi3D credits.")
            return
        Raw = Result["raw"]
        Old = Db.One("SELECT * FROM raw_geometry WHERE mesh_id = ?", (MeshId,))
        Timings = {**json.loads(Old["timings_json"] or "{}"), "measure_s": Result.get("seconds"),
                   "queue_wait_s": _Seconds(Job["created_at"], Job["started_at"])}
        Db.Execute("UPDATE raw_geometry SET status = 'measured', measurement_json = ?, method_version = ?, measured_at = ?, "
                   "faces = ?, sha256 = COALESCE(?, sha256), bytes = COALESCE(?, bytes), stl_path = ?, timings_json = ?, "
                   "error = NULL, updated_at = ? WHERE mesh_id = ?",
                   (Dumps(Raw), Raw["method_version"], T, Raw["faces"], Result.get("sha256"), Result.get("bytes"),
                    f"meshes/{MeshId}/raw.stl" if Result.get("converted") else Old["stl_path"], Dumps(Timings), T, MeshId))
        Row = Db.One("SELECT * FROM raw_geometry WHERE mesh_id = ?", (MeshId,))
        for R in self._WaitingFor(MeshId):
            self._Finalize(R, Row)
        # Background, never blocking the numbers already shown: light preview (always — also when the
        # model needs review, so it can be looked at), then the integrity check.
        self.Queue.Enqueue("preview", MeshId, Params={"source": Row["stl_path"], "raw": Raw,
                                                      "output": f"meshes/{MeshId}/preview.p3pv"})
        self.Queue.Enqueue("integrity", MeshId, Params={"source": Row["stl_path"]})

    def _Timings(self, MeshId: str, **New) -> str:
        Old = self.Ctx.Db.One("SELECT timings_json FROM raw_geometry WHERE mesh_id = ?", (MeshId,))
        return Dumps({**json.loads((Old or {}).get("timings_json") or "{}"), **New})

    def _Previewed(self, Job: dict, Result, Error, Started: bool = False) -> None:
        if Started or Error:
            return
        self.Ctx.Db.Execute("UPDATE raw_geometry SET preview_path = ?, timings_json = ?, updated_at = ? WHERE mesh_id = ?",
                            (json.loads(Job["params_json"])["output"],
                             self._Timings(Job["mesh_id"], preview_s=Result.get("seconds"),
                                           preview_faces=Result.get("preview_faces")), Now(), Job["mesh_id"]))

    def _Integrity(self, Job: dict, Result, Error, Started: bool = False) -> None:
        if Started:
            return
        State, Doc = ("unknown", {"error": Error}) if Error else (("closed" if Result.get("watertight") else "open"), Result)
        self.Ctx.Db.Execute("UPDATE raw_geometry SET integrity = ?, integrity_json = ?, timings_json = ?, updated_at = ? "
                            "WHERE mesh_id = ?", (State, Dumps(Doc), self._Timings(Job["mesh_id"],
                                                  integrity_s=(Result or {}).get("seconds")), Now(), Job["mesh_id"]))

    def _Fail(self, Sid: str, Error: str) -> None:
        self.Ctx.Db.Update("session_3d", Sid, status="failed", error=Error)
        Stages.Begin(self.Ctx.Db, Sid, "failed", error=Error)

    def _Finalize(self, Row: dict, RawRow: dict) -> None:
        """Exact values for this request's size and material from the one raw measurement — arithmetic only."""
        Ctx, Db, Sid = self.Ctx, self.Ctx.Db, Row["id"]
        Raw = json.loads(RawRow["measurement_json"])
        if Raw.get("method_version") == CharmGeo.CharmMethodVersion:
            return self._FinalizeCharm(Row, RawRow, Raw)
        T = Now()
        Db.Execute("DELETE FROM price_calculations WHERE session_3d_id = ?", (Sid,))     # a retry replaces results
        Db.Execute("DELETE FROM geometry_results WHERE session_3d_id = ?", (Sid,))
        Checks = {"closed_heuristic": Raw["closed_heuristic"], "volume": Raw["volume"],
                  "volume_alt_reference": Raw["volume_alt_reference"], "roundness": Raw.get("roundness"),
                  "bore_slices": Raw.get("bore_slices"), "faces": Raw["faces"], "raw_sha256": RawRow["sha256"]}

        def Insert(Stage, G, Scale, StlPath):
            Gid = NewId("geo")
            Db.Execute("INSERT INTO geometry_results (id, session_3d_id, stage, size_x_mm, size_y_mm, size_z_mm, "
                       "inner_diameter_mm, volume_mm3, surface_area_mm2, watertight, scale_factor, stl_path, "
                       "method_version, checks_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (Gid, Sid, Stage, G["size_x_mm"], G["size_y_mm"], G["size_z_mm"], G["inner_diameter_mm"],
                        G["volume_mm3"], G["surface_area_mm2"], int(Raw["closed_heuristic"]), Scale, StlPath,
                        Raw["method_version"], Dumps(Checks), T))
            return Gid

        Insert("raw", {"size_x_mm": Raw["extent_x"], "size_y_mm": Raw["extent_y"], "size_z_mm": Raw["extent_z"],
                       "inner_diameter_mm": Raw.get("inner_diameter"), "volume_mm3": Raw["volume"],
                       "surface_area_mm2": Raw["area"]}, None, RawRow["stl_path"])
        Problems = []
        if not Raw.get("bore_ok"):
            Problems.append("No ring bore was found — the model may not be a ring.")
        elif Raw["roundness"] > MaxRoundness:
            Problems.append(f"The bore is not round ({Raw['roundness'] * 100:.1f}% deviation) — check the inner diameter.")
        if not Raw["closed_heuristic"]:
            Problems.append("The volume reference check suggests the mesh may not be closed — check the volume.")
        if not Raw.get("bore_ok"):
            Db.Update("session_3d", Sid, status="needs_review", error=" ".join(Problems))
            Stages.Begin(Db, Sid, "review_required", problems=Problems)
            return
        G = Scaled(Raw, UsSizeToInnerDiameterMm(Row["production_size"]))
        Gid = Insert("production", G, G["scale_factor"], None)          # scaled STL: exported on demand only
        Weight = self._Price(Row, Gid, G["volume_mm3"] if Raw["closed_heuristic"] else None)
        Status = "needs_review" if Problems else "measured"
        Db.Update("session_3d", Sid, status=Status, error=" ".join(Problems) or None)
        # Processing is complete either way; the production decision is a separate state.
        Stages.Begin(Db, Sid, "review_required" if Problems else "ready", **({"problems": Problems} if Problems else {}))
        Owner = Db.One("SELECT owner_account_id FROM designs WHERE id = ?", (Row["design_id"],))
        Sessions.Record(Ctx, Owner["owner_account_id"], "admin_3d_measured", Row["design_id"], session_3d_id=Sid,
                        status=Status, inner_diameter_mm=G["inner_diameter_mm"], volume_mm3=G["volume_mm3"], weight_g=Weight)

    def _FinalizeCharm(self, Row: dict, RawRow: dict, Raw: dict) -> None:
        """A charm: the whole charm, loop included, scaled to the height its size stands for (products.Charm3DHeight)
        — arithmetic only, no bore, no ring size, no loop measurement."""
        Ctx, Db, Sid = self.Ctx, self.Ctx.Db, Row["id"]
        T = Now()
        Db.Execute("DELETE FROM price_calculations WHERE session_3d_id = ?", (Sid,))     # a retry replaces results
        Db.Execute("DELETE FROM geometry_results WHERE session_3d_id = ?", (Sid,))
        Checks = {"closed_heuristic": Raw["closed_heuristic"], "volume": Raw["volume"],
                  "volume_alt_reference": Raw["volume_alt_reference"], "faces": Raw["faces"], "raw_sha256": RawRow["sha256"],
                  "height_basis": Products.CharmSizeDefinition["text"], "up_source": Raw.get("up_source")}

        def Insert(Stage, G, Scale, StlPath):
            Gid = NewId("geo")
            Db.Execute("INSERT INTO geometry_results (id, session_3d_id, stage, size_x_mm, size_y_mm, size_z_mm, "
                       "inner_diameter_mm, volume_mm3, surface_area_mm2, watertight, scale_factor, stl_path, "
                       "method_version, checks_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (Gid, Sid, Stage, G["size_x_mm"], G["size_y_mm"], G["size_z_mm"], None, G["volume_mm3"],
                        G["surface_area_mm2"], int(Raw["closed_heuristic"]), Scale, StlPath, Raw["method_version"], Dumps(Checks), T))
            return Gid

        Insert("raw", {"size_x_mm": Raw["extent_x"], "size_y_mm": Raw["extent_y"], "size_z_mm": Raw["extent_z"],
                       "volume_mm3": Raw["volume"], "surface_area_mm2": Raw["area"]}, None, RawRow["stl_path"])
        G = CharmGeo.CharmScaled(Raw, Products.Charm3DHeight(Row["production_size"]))
        Gid = Insert("production", G, G["scale_factor"], None)          # scaled STL: exported on demand only
        Weight = self._Price(Row, Gid, G["volume_mm3"] if Raw["closed_heuristic"] else None)
        Problems = [] if Raw["closed_heuristic"] else \
            ["The volume reference check suggests the mesh may not be closed — check the volume."]
        Status = "needs_review" if Problems else "measured"
        Db.Update("session_3d", Sid, status=Status, error=" ".join(Problems) or None)
        Stages.Begin(Db, Sid, "review_required" if Problems else "ready", **({"problems": Problems} if Problems else {}))
        Owner = Db.One("SELECT owner_account_id FROM designs WHERE id = ?", (Row["design_id"],))
        Sessions.Record(Ctx, Owner["owner_account_id"], "admin_3d_measured", Row["design_id"], session_3d_id=Sid,
                        status=Status, height_mm=G["height_mm"], volume_mm3=G["volume_mm3"], weight_g=Weight)

    def _Price(self, Row: dict, Gid: str, VolumeMm3: float | None) -> float | None:
        """Weight, production cost and 3D price from Admin → Material pricing (current version); a charm's from the
        charm price book."""
        Ctx = self.Ctx
        Mat = Ctx.Catalog.Get(Row["material_id"])
        Weight = round(VolumeMm3 / 1000.0 * Mat.DensityGCm3, 3) if VolumeMm3 else None
        Charm = Products.Of(Ctx.Db, Row["design_id"]) == Products.Charm
        Prices = Ctx.CharmPrices if Charm else Ctx.MaterialPrices
        Book = Prices.Current()
        P = Prices.Price3D(Row["material_id"], Weight)               # weight × cost $/g and × price $/g
        Summary = (Sessions.Summaries(Ctx, [Row["design_id"]]) or [{}])[0]
        Fixed = Summary.get("fixed_price") or {}
        if Charm:
            # A charm's price is per material AND size: compare against the size and material actually produced.
            if Row["material_id"] != Summary.get("material_id") or float(Row["production_size"]) != float(Summary.get("charm_size") or -1):
                Q = Ctx.CharmPrices.QuoteFor(Row["material_id"], Row["production_size"])
                Fixed = {"unit_price": Q.unit_price, "pricing_version": Q.pricing_version, "source": "current_quote"}
        # The fixed price is per material; compare against the material actually used for production.
        elif Row["material_id"] != Summary.get("material_id"):
            Q = Ctx.Pricing.QuoteFor(Row["material_id"])
            Fixed = {"unit_price": Q.unit_price, "pricing_version": Q.pricing_version, "source": "current_quote"}
        Ctx.Db.Execute("INSERT INTO price_calculations (id, session_3d_id, geometry_id, material_id, density_g_cm3, weight_g, "
                       "production_cost, calculated_price, currency, cost_model_version, breakdown_json, fixed_price, "
                       "fixed_price_version, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (NewId("prc"), Row["id"], Gid, Row["material_id"], Mat.DensityGCm3, Weight, P.get("production_cost"),
                        P.get("calculated_price"), Book.get("currency"), Book["version"],
                        Dumps({**P.get("breakdown", {}), "reason": P.get("reason"), "fixed_price_source": Fixed.get("source")}),
                        Fixed.get("unit_price"), Fixed.get("pricing_version"), P["status"], Now()))
        return Weight

    def RepriceMissing(self) -> int:
        """Results whose latest price has no production cost / 3D price (made before the material table had
        the numbers) are priced again from the current table. Complete prices are never changed."""
        N = 0
        for R in self.Ctx.Db.All(
                "SELECT s.*, g.id AS gid, g.volume_mm3, g.watertight FROM session_3d s JOIN geometry_results g "
                "ON g.session_3d_id = s.id AND g.stage = 'production' WHERE s.status IN ('measured', 'needs_review')"):
            Last = self.Ctx.Db.One("SELECT production_cost, calculated_price FROM price_calculations WHERE session_3d_id = ? "
                                   "ORDER BY created_at DESC, rowid DESC LIMIT 1", (R["id"],))
            if Last and Last["production_cost"] is not None and Last["calculated_price"] is not None:
                continue
            Charm = Products.Of(self.Ctx.Db, R["design_id"]) == Products.Charm
            Need = (self.Ctx.CharmPrices if Charm else self.Ctx.MaterialPrices).Row(R["material_id"])
            if not (Need.get("cost_per_g") or Need.get("price_per_g")):
                continue                                  # still nothing to price with
            self._Price(R, R["gid"], R["volume_mm3"] if R["watertight"] else None)
            N += 1
        return N

    # ── admin actions: cancel / retry / export ───────────────────────────
    def _MeasureJob(self, MeshId: str, Statuses=("queued", "running")) -> dict | None:
        return self.Ctx.Db.One(f"SELECT * FROM geometry_jobs WHERE kind = 'measure' AND mesh_id = ? AND status IN "
                               f"({','.join('?' * len(Statuses))}) ORDER BY created_at DESC LIMIT 1", (MeshId, *Statuses))

    def Cancel(self, Sid: str) -> dict:
        Row = self._Row(Sid)
        Job = self._MeasureJob(Row["mesh_id"], ("queued",))
        if Row["status"] != "queued" or Job is None:
            raise HttpError(409, "not_cancellable", "Only a request that is still queued can be cancelled.")
        if len(self._WaitingFor(Row["mesh_id"])) > 1:        # other requests share the job: keep it for them
            self.Ctx.Db.Update("session_3d", Sid, status="cancelled", error="Cancelled before it started.")
            Stages.Begin(self.Ctx.Db, Sid, "cancelled")
        else:
            self.Queue.Cancel(Job["id"])
        return self.Get(Sid)

    def Retry(self, Sid: str) -> dict:
        """Retry from the last successful stage. With the raw STL on disk this is local only (no Hi3D charge)."""
        Row = self._Row(Sid)
        if Row["status"] not in ("failed", "cancelled", "needs_review"):
            raise HttpError(409, "not_retryable", "This request is not in a state that can be retried.")
        Mesh = self.Ctx.Db.One("SELECT * FROM meshes WHERE id = ?", (Row["mesh_id"],))
        if Mesh and Mesh["status"] == "ready":
            self.Ctx.Db.Execute("UPDATE raw_geometry SET status = 'downloaded' WHERE mesh_id = ? AND status = 'failed'",
                                (Row["mesh_id"],))
            self.Ctx.Db.Update("session_3d", Sid, status="queued", error=None)
            self._Continue(Row["mesh_id"])
            return {**self.Get(Sid), "retried": "geometry"}
        if Row["status"] != "failed":
            raise HttpError(409, "not_retryable", "This request is not in a state that can be retried.")
        New = self.Meshes.Create(Row["candidate_id"], None)       # Hi3D itself failed: a new (paid) Hi3D request
        self.Ctx.Db.Update("session_3d", Sid, status="generating", mesh_id=New["id"], error=None)
        return {**self.Get(Sid), "retried": "hi3d"}

    def StartExport(self, Sid: str) -> dict:
        """Queue a temporary scaled STL (deleted after the TTL); the download itself never holds the queue."""
        Row = self._Row(Sid)
        Raw = self.Ctx.Db.One("SELECT * FROM raw_geometry WHERE mesh_id = ? AND status = 'measured'", (Row["mesh_id"],))
        if Raw is None or Row["status"] not in ("measured", "needs_review"):
            raise HttpError(409, "geometry_not_ready", "The geometry is not measured yet.")
        Measured = json.loads(Raw["measurement_json"])
        Charm = Measured.get("method_version") == CharmGeo.CharmMethodVersion
        if not Charm and not Measured.get("bore_ok"):
            raise HttpError(409, "no_bore", "No ring bore was found, so the model cannot be scaled to a ring size.")
        self.Queue.CleanupExports()
        Target = Products.Charm3DHeight(Row["production_size"]) if Charm else UsSizeToInnerDiameterMm(Row["production_size"])
        for J in self.Ctx.Db.All("SELECT * FROM geometry_jobs WHERE kind = 'export' AND session_3d_id = ? AND status IN "
                                 "('queued','running','done') ORDER BY created_at DESC", (Sid,)):
            P = json.loads(J["params_json"])
            Usable = J["status"] != "done" or (J["output_path"] and (self.Ctx.Settings.DevDir / J["output_path"]).is_file())
            if Usable and P.get("target_mm") == Target and P.get("raw_sha256") == Raw["sha256"]:
                return self.ExportStatus(Sid, J["id"])
        Job = self.Queue.Enqueue("export", Row["mesh_id"], Sid, Dedupe=False, Params={
            "source": Raw["stl_path"], "raw": Measured, "target_mm": Target, "raw_sha256": Raw["sha256"],
            "output": f"exports/{Sid}_{NewId('x')}.stl",
            **({"product": "charm", "label": f"{float(Row['production_size']):g} mm {Products.CharmSizeDefinition['short']}"} if Charm else {})})
        return self.ExportStatus(Sid, Job["id"])

    def ExportStatus(self, Sid: str, Jid: str) -> dict:
        J = self.Queue.Get(Jid)
        if J["session_3d_id"] != Sid or J["kind"] != "export":
            raise HttpError(404, "job_not_found", "Export not found.")
        Out = self.Ctx.Settings.DevDir / J["output_path"] if J["output_path"] else None
        Ready = J["status"] == "done" and Out is not None and Out.is_file()
        return {"job_id": J["id"], "status": "expired" if J["status"] == "done" and not Ready else J["status"],
                "ahead": self.Queue.Ahead(J), "created_at": J["created_at"], "started_at": J["started_at"],
                "finished_at": J["finished_at"], "expires_at": J["expires_at"], "error": J["error"],
                "bytes": Out.stat().st_size if Ready else None, "server_now": Now()}

    # ── recovery ─────────────────────────────────────────────────────────
    # The only review note the first charm path (until 2026-10-05) gave every charm result
    LegacyLoopNote = "Scaled by the overall height with the attachment loop included: check the main body's height."

    def ClearLegacyLoopReviews(self) -> int:
        """Charm results made before the size became the total height were flagged only because the loop is part of
        the measured height. A charm's size is now its total height, loop included — exactly how those results were
        scaled — so they are complete; their numbers stay as they are. A result with any other problem stays flagged."""
        N = 0
        for R in self.Ctx.Db.All("SELECT s.id FROM session_3d s JOIN designs d ON d.id = s.design_id WHERE d.product_type = 'charm' "
                                 "AND s.status = 'needs_review' AND s.error = ?", (self.LegacyLoopNote,)):
            self.Ctx.Db.Update("session_3d", R["id"], status="measured", error=None)
            Stages.Begin(self.Ctx.Db, R["id"], "ready")
            N += 1
        if N:
            Logger.info("Cleared the former loop review flag of %d charm 3D result(s)", N)
        return N

    def Reconcile(self) -> int:
        """After a restart: continue every request whose raw STL is on disk; Hi3D ones resume via the mesh."""
        self.RepriceMissing()
        self.ClearLegacyLoopReviews()
        N = 0
        for R in self.Ctx.Db.All("SELECT DISTINCT s.mesh_id, m.status FROM session_3d s JOIN meshes m ON m.id = s.mesh_id "
                                 f"WHERE s.status IN ({','.join('?' * len(Waiting))})", Waiting):
            if R["status"] == "ready":
                if not self._MeasureJob(R["mesh_id"]):
                    self._Continue(R["mesh_id"])
                N += 1
            elif R["status"] in ("failed", "interrupted"):
                self._MeshFinished(R["mesh_id"], False)
        return N

    # ── read ─────────────────────────────────────────────────────────────
    def _Row(self, Sid: str) -> dict:
        R = self.Ctx.Db.One("SELECT * FROM session_3d WHERE id = ?", (Sid,))
        if R is None:
            raise HttpError(404, "session_3d_not_found", "3D request not found.")
        return R

    def Status(self, Sid: str) -> dict:
        """Light, real state for the live status line (polled): stages with persisted start/end times."""
        Db, R = self.Ctx.Db, self._Row(Sid)
        HiStages = [S for S in Stages.List(Db, R["mesh_id"]) if S["stage"] != "failed"
                    and (S["ended_at"] is None or S["ended_at"] >= R["created_at"])] if R["mesh_id"] else []
        All = HiStages + Stages.List(Db, Sid)
        Job = self._MeasureJob(R["mesh_id"], ("queued",)) if R["status"] == "queued" else None
        Mesh = Db.One("SELECT status FROM meshes WHERE id = ?", (R["mesh_id"],))
        Raw = Db.One("SELECT status, method_version, integrity, preview_path, thumbnail_path, timings_json, faces, bytes, "
                     "sha256 FROM raw_geometry WHERE mesh_id = ?", (R["mesh_id"],))
        Current = bool(Raw and Raw["status"] == "measured" and Raw["method_version"] in (FastMethodVersion, CharmGeo.CharmMethodVersion))
        Bg = Db.All("SELECT kind, status FROM geometry_jobs WHERE mesh_id = ? AND kind IN ('preview','integrity') "
                    "AND status IN ('queued','running')", (R["mesh_id"],))
        return {
            "id": Sid, "status": R["status"], "error": R["error"], "server_now": Now(),
            "production_state": ProductionState(R["status"], Raw["integrity"] if Raw else None),
            "done": R["status"] in Terminal, "stages": All,
            "queue": {"ahead": self.Queue.Ahead(Job), "job_id": Job["id"]} if Job else None,
            "can_cancel": bool(Job),
            "can_retry": R["status"] in ("failed", "cancelled")
                         or (R["status"] == "needs_review" and not Current),   # e.g. an improved measurement
            "retry_is_local": bool(Mesh and Mesh["status"] == "ready"),
            "raw": Raw and {"faces": Raw["faces"], "bytes": Raw["bytes"], "sha256": Raw["sha256"],
                            "integrity": Raw["integrity"], "preview_ready": bool(Raw["preview_path"]),
                            "thumbnail": bool(Raw["thumbnail_path"]), "timings": json.loads(Raw["timings_json"] or "{}"),
                            "background": {J["kind"]: J["status"] for J in Bg}},
        }

    def Get(self, Sid: str) -> dict:
        Db, Url = self.Ctx.Db, self.Ctx.AssetUrl
        R = self._Row(Sid)
        Mesh = Db.One("SELECT id, status, provider_request_id, endpoint, error FROM meshes WHERE id = ?", (R["mesh_id"],))
        Geo = {G["stage"]: {**G, "checks": json.loads(G.pop("checks_json") or "{}")}
               for G in Db.All("SELECT * FROM geometry_results WHERE session_3d_id = ? ORDER BY created_at", (Sid,))}
        Calc = Db.One("SELECT * FROM price_calculations WHERE session_3d_id = ? ORDER BY created_at DESC LIMIT 1", (Sid,))
        if Calc:
            Calc["breakdown"] = json.loads(Calc.pop("breakdown_json") or "{}")
        Cand = Db.One("SELECT asset_path FROM candidates WHERE id = ?", (R["candidate_id"],))
        Mat = self.Ctx.Catalog.Get(R["material_id"])
        Prod = Geo.get("production") or {}
        RawRow = Db.One("SELECT measurement_json, integrity FROM raw_geometry WHERE mesh_id = ?", (R["mesh_id"],))
        RawMeasured = json.loads(RawRow["measurement_json"]) if RawRow and RawRow["measurement_json"] else None
        Integrity = RawRow["integrity"] if RawRow else None
        Charm = Products.Of(Db, R["design_id"]) == Products.Charm
        Out = {
            **R, "target_inner_diameter_mm": UsSizeToInnerDiameterMm(R["production_size"]),
            "material_label": Mat.Label if Mat else R["material_id"], "density_g_cm3": Mat.DensityGCm3 if Mat else None,
            "image_url": Url(Cand["asset_path"]) if Cand else None,
            # Production readiness is separate from processing: warnings → review_required, never "Ready".
            "production_state": ProductionState(R["status"], Integrity),
            "review": ReviewItems(R["status"], RawMeasured, Integrity),
            "raw_available": bool(Mesh and Mesh["status"] == "ready"),
            "hi3d": Mesh and {"mesh_id": Mesh["id"], "status": Mesh["status"], "endpoint": Mesh["endpoint"],
                              "provider": "mock" if (Mesh["provider_request_id"] or "").startswith("mockreq_") else
                              ("fal" if Mesh["provider_request_id"] else None), "error": Mesh["error"]},
            "geometry": Geo, "price": Calc,
            "scaled_stl": "stored" if Prod.get("stl_path") else ("on_demand" if Prod else None),   # v2 rows stored one
            "ring_id": RingIds.CandidateRef(Db, R["candidate_id"]),
            "live": self.Status(Sid),
        }
        if Charm:                    # a charm: scaled to a height in mm (rings keep exactly the fields of before)
            Out.update({"product_type": Products.Charm, "target_inner_diameter_mm": None, "target_height_mm": R["production_size"],
                        "size_label": Products.CharmSizeLabel(R["production_size"]),
                        "material_label": CharmPrices.Label(self.Ctx.Catalog, R["material_id"]),
                        "height_basis": Products.CharmSizeDefinition["text"]})
        return Out

    def ForDesign(self, DesignId: str) -> list[dict]:
        return [self.Get(R["id"]) for R in self.Ctx.Db.All(
            "SELECT id FROM session_3d WHERE design_id = ? ORDER BY created_at DESC", (DesignId,))]

    def FilePath(self, Sid: str, Stage: str) -> Path:
        """raw = the Hi3D model as delivered · production = a stored scaled STL (v2 requests only) ·
        export-<job> = a temporary scaled STL · preview = the light visual preview · thumbnail = Hi3D's image."""
        Dev, Db = self.Ctx.Settings.DevDir, self.Ctx.Db
        R = self._Row(Sid)
        Raw = Db.One("SELECT * FROM raw_geometry WHERE mesh_id = ?", (R["mesh_id"],))
        Rel = None
        if Stage == "raw":
            M = Db.One("SELECT original_path FROM meshes WHERE id = ? AND status = 'ready'", (R["mesh_id"],))
            Rel = M and M["original_path"]
        elif Stage == "thumbnail":
            Rel = Raw and Raw["thumbnail_path"]
        elif Stage.startswith("export-"):
            J = Db.One("SELECT output_path FROM geometry_jobs WHERE id = ? AND kind = 'export' AND session_3d_id = ? "
                       "AND status = 'done'", (Stage[len("export-"):], Sid))
            Rel = J and J["output_path"]
        elif Stage in ("production", "preview"):
            G = Db.One("SELECT stl_path FROM geometry_results WHERE session_3d_id = ? AND stage = 'production'", (Sid,))
            Legacy = G and G["stl_path"]
            if Stage == "production":
                Rel = Legacy
            elif Raw and Raw["preview_path"]:
                Rel = Raw["preview_path"]
            elif Legacy:                                        # v2: light STL next to the stored scaled STL
                P = assets.Resolve(Dev, Legacy)
                Pv = P.with_name(P.stem + "_preview.stl")
                return Pv if Pv.is_file() else P
        P = assets.Resolve(Dev, Rel) if Rel else None
        if P is None or not P.is_file():
            raise HttpError(404, "geometry_not_found", "This file is not available.")
        return P

    StlPath = FilePath

    def FileName(self, Sid: str, Stage: str, Suffix: str, OrderRef: str | None = None) -> str:
        """Operational download names, searchable months later:
             <Design-Name>_<Ring ID>[_<Order ID>]_<Material>_US<size>.stl   e.g. Aurora-Twist_R-1013-A_ORD-10482_Silver_US10.stl
             <Design-Name>_<Ring ID>_raw.stl                                 the Hi3D model as delivered
        A charm: <Design-Name>_<Charm ID>[_<Order ID>]_<Material>_<size>mm.stl, e.g. Lune-Drop_C-1003-B_Sterling-Silver_20mm.stl
        The Order ID appears only when an order exists — it is never invented."""
        Db = self.Ctx.Db
        R = self._Row(Sid)
        D = Db.One("SELECT title FROM designs WHERE id = ?", (R["design_id"],))
        Ring = RingIds.CandidateRef(Db, R["candidate_id"]) or Sid
        Mat = self.Ctx.Catalog.Get(R["material_id"])
        Parts = [SlugPart(D["title"]) if D else "", Ring]
        if (Stage.startswith("export-") or Stage == "production") and Products.Of(Db, R["design_id"]) == Products.Charm:
            Parts += [OrderRef or "", SlugPart(CharmPrices.Label(self.Ctx.Catalog, R["material_id"])), f"{R['production_size']:g}mm"]
        elif Stage.startswith("export-") or Stage == "production":
            Parts += [OrderRef or "", SlugPart(Mat.Label if Mat else R["material_id"]), f"US{R['production_size']:g}"]
        else:
            Parts.append(Stage)
        return "_".join(P for P in Parts if P) + Suffix

