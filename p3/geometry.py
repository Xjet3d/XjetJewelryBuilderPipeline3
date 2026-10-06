"""Ring geometry: find the bore, scale to a ring size, measure — memory-lean for multi-million-face models.

Hi3D returns a mesh in arbitrary units and orientation (an STL at 5,000,000 faces is ~250 MB). The
whole pipeline works on one compact float32 triangle array (faces × 3 corners × xyz), processed in
chunks, so peak memory stays a small multiple of the file size:

  1. load: binary STL is read straight into the array (no per-vertex objects); GLB/OBJ via trimesh;
  2. watertight check: every edge must be shared by exactly two triangles (vertices quantised so the
     unmerged STL corners match). Small open meshes (≤ RepairMaxFaces) get trimesh's repair; large
     open ones are reported as not watertight (volume unreliable) instead of an unbounded repair;
  3. ring axis = direction of least surface spread (area-weighted triangle sample);
  4. bore: slice at three heights through the band, nearest wall point per 5° bin around the centre,
     circle fit with an iterated centre; inner diameter = 2 × median wall radius at the narrowest height;
  5. align (bore centre → origin, ring axis → Z) and scale uniformly to the target inner diameter,
     in place; measure X/Y/Z, inner diameter (re-checked), volume (divergence theorem) and area;
  6. write the scaled STL and a light preview STL (vertex-clustered, ~PreviewFaces) for the viewer.

Uniform scaling also scales band width and thickness — a known limitation, recorded in the checks.
"""

import io
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

MethodVersion = "ring-bore-sections-v2"
RoundnessLimit = 0.04           # (std of bore radius) / radius above this → needs review
Directions = 72
Chunk = 1_000_000               # triangles per chunk for whole-mesh passes
RepairMaxFaces = 1_000_000      # trimesh repair (graph based) only below this size
PreviewFaces = 120_000          # browser viewer copy (~6 MB STL)


def UsSizeToInnerDiameterMm(Size: float) -> float:
    """US/Canada ring size → inner diameter (standard linear table, size 0 = 11.63 mm)."""
    return round(11.63 + 0.8128 * float(Size), 3)


@dataclass
class Measurement:
    size_x_mm: float | None = None
    size_y_mm: float | None = None
    size_z_mm: float | None = None
    inner_diameter_mm: float | None = None
    volume_mm3: float | None = None
    surface_area_mm2: float | None = None
    watertight: bool = False
    checks: dict = field(default_factory=dict)


@dataclass
class RingGeometry:
    raw: Measurement
    production: Measurement | None
    scale_factor: float | None
    production_stl: bytes | None          # only filled by MeasureRing (in-memory convenience)
    status: str                           # measured | needs_review | failed
    problems: list
    faces: int = 0


# ── loading ─────────────────────────────────────────────────────────────────
StlRecord = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])


def LoadTriangles(PathObj: Path, Fmt: str) -> np.ndarray:
    """(F, 3, 3) float32 triangles."""
    PathObj = Path(PathObj)
    if Fmt == "stl":
        with open(PathObj, "rb") as F:
            Head = F.read(84)
        Count = int(np.frombuffer(Head[80:84], "<u4")[0]) if len(Head) == 84 else 0
        if Count and os.path.getsize(PathObj) == 84 + 50 * Count:      # binary STL
            Rec = np.memmap(PathObj, dtype=StlRecord, mode="r", offset=84, shape=(Count,))
            Tri = np.empty((Count, 3, 3), np.float32)
            for S in range(0, Count, Chunk):
                Tri[S:S + Chunk] = Rec["v"][S:S + Chunk]
            del Rec
            return Tri
    import trimesh                                                     # ASCII STL, GLB, OBJ
    Loaded = trimesh.load(str(PathObj), file_type=Fmt, force=None, process=False)
    if isinstance(Loaded, trimesh.Scene):
        Loaded = Loaded.to_geometry() if hasattr(Loaded, "to_geometry") else Loaded.dump(concatenate=True)
    if not isinstance(Loaded, trimesh.Trimesh) or len(Loaded.faces) == 0:
        raise ValueError("The 3D file contains no mesh")
    return np.asarray(Loaded.triangles, dtype=np.float32)


# ── whole-mesh quantities (chunked) ───────────────────────────────────────────
def _Bounds(Tri) -> tuple[np.ndarray, np.ndarray]:
    Lo, Hi = np.full(3, np.inf), np.full(3, -np.inf)
    for S in range(0, len(Tri), Chunk):
        C = Tri[S:S + Chunk].reshape(-1, 3)
        Lo, Hi = np.minimum(Lo, C.min(0)), np.maximum(Hi, C.max(0))
    return Lo, Hi


def _VolumeArea(Tri) -> tuple[float, float]:
    Vol = Area = 0.0
    for S in range(0, len(Tri), Chunk):
        C = Tri[S:S + Chunk].astype(np.float64)
        Cr = np.cross(C[:, 1] - C[:, 0], C[:, 2] - C[:, 0])
        Area += float(np.linalg.norm(Cr, axis=1).sum()) / 2
        Vol += float(np.einsum("ij,ij->i", C[:, 0], np.cross(C[:, 1], C[:, 2])).sum()) / 6
    return abs(Vol), Area


def _CornerKeys(Tri, Lo, Step, Round=True) -> np.ndarray:
    """One int64 per triangle corner: the corner quantised to a grid (21 bits per axis), packed."""
    Out = np.empty(len(Tri) * 3, np.int64)
    for S in range(0, len(Tri), Chunk):
        C = (Tri[S:S + Chunk].reshape(-1, 3) - Lo) / Step
        Q = (np.round(C) if Round else np.floor(C)).astype(np.int64)
        np.clip(Q, 0, (1 << 21) - 1, out=Q)
        Out[S * 3:(S + len(C) // 3) * 3] = (Q[:, 0] << 42) | (Q[:, 1] << 21) | Q[:, 2]
    return Out


def _Watertight(Tri) -> bool:
    """Every edge used by exactly two triangles (corners quantised so unmerged STL corners match)."""
    Lo, Hi = _Bounds(Tri)
    Step = max(float((Hi - Lo).max()), 1e-12) / ((1 << 21) - 2)
    _, Inv = np.unique(_CornerKeys(Tri, Lo, Step), return_inverse=True)
    Ids = Inv.reshape(-1, 3)
    del Inv
    E = np.concatenate([Ids[:, [0, 1]], Ids[:, [1, 2]], Ids[:, [2, 0]]])
    del Ids
    E.sort(axis=1)
    Key = E[:, 0] * (int(E.max()) + 1) + E[:, 1]
    del E
    Key.sort()
    Change = np.flatnonzero(np.diff(Key)) + 1
    Counts = np.diff(np.r_[0, Change, len(Key)])
    return bool(len(Counts)) and bool((Counts == 2).all())


def _TrimeshRepair(Tri) -> tuple[np.ndarray, dict]:
    import trimesh
    M = trimesh.Trimesh(vertices=Tri.reshape(-1, 3), faces=np.arange(len(Tri) * 3).reshape(-1, 3), process=True)
    Before = bool(M.is_watertight)
    M.update_faces(M.nondegenerate_faces())
    M.update_faces(M.unique_faces())
    M.remove_unreferenced_vertices()
    trimesh.repair.fix_normals(M)
    if not M.is_watertight:
        trimesh.repair.fill_holes(M)
    return np.asarray(M.triangles, dtype=np.float32), {"watertight_before_repair": Before,
                                                       "watertight_after_repair": bool(M.is_watertight)}


# ── frame and bore ───────────────────────────────────────────────────────────
def _Frame(Tri):
    """Ring axis (least surface spread) and two in-plane axes, from an area-weighted sample."""
    Rng = np.random.default_rng(7)
    Idx = Rng.choice(len(Tri), size=min(len(Tri), 200_000), replace=False) if len(Tri) > 200_000 else np.arange(len(Tri))
    T = Tri[Idx].astype(np.float64)
    W = np.linalg.norm(np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0]), axis=1)
    if W.sum() <= 0:
        W = np.ones(len(T))
    Pick = Rng.choice(len(T), size=4000, p=W / W.sum())
    A, B = Rng.random((2, 4000, 1))
    Flip = (A + B) > 1
    A, B = np.where(Flip, 1 - A, A), np.where(Flip, 1 - B, B)
    Points = T[Pick, 0] + A * (T[Pick, 1] - T[Pick, 0]) + B * (T[Pick, 2] - T[Pick, 0])
    Centre = Points.mean(axis=0)
    _, _, Vt = np.linalg.svd(Points - Centre, full_matrices=False)
    return Centre, Vt[0], Vt[1], Vt[2], (Points - Centre) @ Vt.T


def _FitCircle(P: np.ndarray) -> tuple[np.ndarray, float]:
    A = np.c_[2 * P, np.ones(len(P))]
    Sol, *_ = np.linalg.lstsq(A, (P ** 2).sum(axis=1), rcond=None)
    C = Sol[:2]
    return C, float(np.sqrt(max(Sol[2] + C @ C, 0.0)))


def _Section(Tri, Origin, Axis, U, V, PerSegment: int = 8) -> np.ndarray:
    """Cross-section with the plane through Origin normal to Axis, as (U, V) points sampled along
    each crossing triangle's segment. Only the crossing triangles are copied."""
    Off = float(Origin @ Axis)
    Parts = []
    for S in range(0, len(Tri), Chunk):
        C = Tri[S:S + Chunk]
        D = C @ Axis.astype(np.float32) - Off
        Cross = (D.min(axis=1) < 0) & (D.max(axis=1) > 0)
        if Cross.any():
            Parts.append((C[Cross].astype(np.float64) - Origin, D[Cross].astype(np.float64)))
    if not Parts:
        return np.zeros((0, 2))
    T = np.concatenate([P[0] for P in Parts])
    D = np.concatenate([P[1] for P in Parts])
    Ends = np.full((len(T), 3, 3), np.nan)
    for K, (A, B) in enumerate(((0, 1), (1, 2), (2, 0))):
        M = (D[:, A] * D[:, B]) < 0
        F = D[M, A] / (D[M, A] - D[M, B])
        Ends[M, K] = T[M, A] + F[:, None] * (T[M, B] - T[M, A])
    Valid = ~np.isnan(Ends[:, :, 0])
    Keep = Valid.sum(axis=1) == 2
    Ends, Valid = Ends[Keep], Valid[Keep]
    Order = np.argsort(~Valid, axis=1, kind="stable")[:, :2]
    P0 = np.take_along_axis(Ends, Order[:, :1, None], axis=1)[:, 0]
    P1 = np.take_along_axis(Ends, Order[:, 1:2, None], axis=1)[:, 0]
    W = np.linspace(0, 1, PerSegment)[None, :, None]
    P3 = (P0[:, None] * (1 - W) + P1[:, None] * W).reshape(-1, 3)
    if len(P3) > 400_000:                                     # dense meshes: plenty of wall points
        P3 = P3[np.random.default_rng(3).choice(len(P3), 400_000, replace=False)]
    return np.c_[P3 @ U, P3 @ V]


def _Bore(Tri, Centre, U, V, Axis, Spread) -> dict:
    Lo, Hi = np.percentile(Spread[:, 2], [5, 95])
    Results = []
    for Frac in (0.3, 0.5, 0.7):
        H = Lo + (Hi - Lo) * Frac
        P = _Section(Tri, Centre + H * Axis, Axis, U, V)
        C2, Radius, Std, Filled = np.zeros(2), None, None, 0
        for _ in range(6):
            if len(P) < Directions:
                break
            Rel = P - C2
            Ang = np.arctan2(Rel[:, 1], Rel[:, 0])
            Dist = np.hypot(Rel[:, 0], Rel[:, 1])
            Bin = ((Ang + np.pi) / (2 * np.pi) * Directions).astype(int) % Directions
            Near = np.full(Directions, np.inf)
            np.minimum.at(Near, Bin, Dist)
            Ok = np.isfinite(Near)
            Filled = int(Ok.sum())
            if Filled < Directions * 0.9:
                break
            Mid = (np.arange(Directions) + 0.5) / Directions * 2 * np.pi - np.pi
            Wall = C2 + np.c_[np.cos(Mid), np.sin(Mid)][Ok] * Near[Ok][:, None]
            NewC, _ = _FitCircle(Wall)
            R = np.linalg.norm(Wall - NewC, axis=1)
            Radius, Std = float(np.median(R)), float(R.std())
            Moved = float(np.linalg.norm(NewC - C2))
            C2 = NewC
            if Moved < 1e-4 * Radius:
                break
        Results.append({"height": float(H), "centre": C2.tolist(), "radius": Radius, "std": Std, "bins_filled": Filled})
    Good = [R for R in Results if R["radius"] and R["bins_filled"] >= Directions * 0.9]
    if not Good:
        return {"ok": False, "slices": Results}
    Best = min(Good, key=lambda R: R["radius"])
    return {"ok": True, "diameter": 2 * Best["radius"], "roundness": Best["std"] / Best["radius"],
            "centre": Best["centre"], "height": Best["height"], "slices": Results}


# ── output ───────────────────────────────────────────────────────────────────
def WriteStl(Tri, PathObj: Path) -> None:
    """Binary STL, streamed in chunks."""
    with open(PathObj, "wb") as F:
        F.write(b"XJet P3 ring geometry".ljust(80, b" "))
        F.write(np.uint32(len(Tri)).tobytes())
        for S in range(0, len(Tri), Chunk):
            C = Tri[S:S + Chunk]
            N = np.cross(C[:, 1] - C[:, 0], C[:, 2] - C[:, 0])
            L = np.linalg.norm(N, axis=1, keepdims=True)
            Rec = np.zeros(len(C), StlRecord)
            Rec["n"] = N / np.where(L > 0, L, 1)
            Rec["v"] = C
            F.write(Rec.tobytes())


def Preview(Tri, Target: int = PreviewFaces) -> np.ndarray:
    """A light copy for the browser viewer: snap corners to a grid, drop collapsed / duplicate faces."""
    if len(Tri) <= Target:
        return Tri
    Lo, Hi = _Bounds(Tri)
    Size = float((Hi - Lo).max())
    Out = Tri
    Cells = int(np.sqrt(Target / 2.0) * 2.2)                  # grid resolution along the longest side
    for _ in range(6):
        Step = Size / max(Cells, 8)
        Keys, Inv = np.unique(_CornerKeys(Tri, Lo, Step, Round=False), return_inverse=True)
        Cell = np.c_[Keys >> 42, (Keys >> 21) & ((1 << 21) - 1), Keys & ((1 << 21) - 1)]
        Cent = (Cell + 0.5) * Step + Lo
        Ids = Inv.reshape(-1, 3)
        Ok = (Ids[:, 0] != Ids[:, 1]) & (Ids[:, 1] != Ids[:, 2]) & (Ids[:, 0] != Ids[:, 2])
        Ids = np.unique(np.sort(Ids[Ok], axis=1), axis=0)
        Out = Cent[Ids].astype(np.float32)
        if len(Out) <= Target * 1.25:
            break
        Cells = int(Cells * 0.8)
    return Out


# ── main entry points ────────────────────────────────────────────────────────
def _Measure(Tri, Water: bool) -> Measurement:
    Lo, Hi = _Bounds(Tri)
    Vol, Area = _VolumeArea(Tri)
    Ext = Hi - Lo
    return Measurement(size_x_mm=float(Ext[0]), size_y_mm=float(Ext[1]), size_z_mm=float(Ext[2]),
                       volume_mm3=Vol if Water else None, surface_area_mm2=Area, watertight=Water)


def MeasureRingFile(Source: Path, Fmt: str, TargetInnerDiameterMm: float,
                    OutStl: Path | None = None, OutPreview: Path | None = None) -> RingGeometry:
    Problems: list[str] = []
    Tri = LoadTriangles(Source, Fmt)
    Faces = len(Tri)
    Water = _Watertight(Tri)
    Repair = {"watertight_before_repair": Water, "watertight_after_repair": Water, "faces": Faces}
    if not Water and Faces <= RepairMaxFaces:
        Tri, R = _TrimeshRepair(Tri)
        Repair.update(R)
        Water = R["watertight_after_repair"]
    elif not Water:
        Repair["repair_skipped"] = f"open mesh with {Faces:,} faces — too large for automatic repair"
    Raw = _Measure(Tri, Water)
    Centre, U, V, Axis, Spread = _Frame(Tri)
    Bore = _Bore(Tri, Centre, U, V, Axis, Spread)
    Raw.checks = {**Repair, "bore": {K: Bore[K] for K in ("ok", "slices")}}
    if not Bore["ok"]:
        return RingGeometry(Raw, None, None, None, "needs_review", ["No closed ring bore was found — check the model."], Faces)
    Raw.inner_diameter_mm = Bore["diameter"]
    Scale = TargetInnerDiameterMm / Bore["diameter"]
    # Align (bore centre → origin, ring axis → Z) and scale to millimetres — in place, in chunks.
    C3 = Centre + Bore["centre"][0] * U + Bore["centre"][1] * V + Bore["height"] * Axis
    M = (np.vstack([U, V, Axis]).T * Scale).astype(np.float32)          # row-vector transform
    C3 = C3.astype(np.float32)
    for S in range(0, len(Tri), Chunk):
        Tri[S:S + Chunk] = (Tri[S:S + Chunk] - C3) @ M
    P = _Measure(Tri, Water)
    Centre2, U2, V2, Axis2, Spread2 = _Frame(Tri)
    Check = _Bore(Tri, Centre2, U2, V2, Axis2, Spread2)
    P.inner_diameter_mm = Check["diameter"] if Check["ok"] else None
    P.checks = {
        **Repair, "bore_roundness": Bore["roundness"], "bore_angular_bins": Directions,
        "target_inner_diameter_mm": TargetInnerDiameterMm, "inner_diameter_after_scaling_mm": P.inner_diameter_mm,
        "uniform_scaling": "band width and thickness scale with the bore (known limitation)",
    }
    if not P.watertight:
        Problems.append("Mesh is not watertight — volume and weight are not reliable.")
    if Bore["roundness"] > RoundnessLimit:
        Problems.append(f"Bore is not round (deviation {Bore['roundness']:.1%}) — check the inner diameter.")
    if P.inner_diameter_mm is None or abs(P.inner_diameter_mm - TargetInnerDiameterMm) > 0.02 * TargetInnerDiameterMm:
        Problems.append("Inner diameter after scaling does not match the target — check the model.")
    if OutStl:
        WriteStl(Tri, OutStl)
    if OutPreview:
        Pv = Preview(Tri)
        P.checks["preview_faces"] = int(len(Pv))
        WriteStl(Pv, OutPreview)
    return RingGeometry(Raw, P, Scale, None, "needs_review" if Problems else "measured", Problems, Faces)


def MeasureRing(Data: bytes, Fmt: str, TargetInnerDiameterMm: float) -> RingGeometry:
    """In-memory convenience (tests, small files): same as MeasureRingFile, returns the STL bytes."""
    with tempfile.TemporaryDirectory() as D:
        Src = Path(D) / f"in.{Fmt}"
        Src.write_bytes(Data)
        Out = Path(D) / "out.stl"
        G = MeasureRingFile(Src, Fmt, TargetInnerDiameterMm, Out)
        if G.production is not None:
            G.production_stl = Out.read_bytes()
        return G


# ══ Measure-once flow (v3) ═══════════════════════════════════════════════════
# The raw Hi3D STL is the master geometry. It is measured ONCE, exactly (full precision, every
# triangle); the values for any ring size then follow from the uniform scale factor s:
#   lengths × s · area × s² · volume × s³ · weight = volume × density.
# No scaled copy is written during processing — ExportScaledStl builds one only when it is downloaded,
# from the stored transform, so every export of the same request is identical.
FastMethodVersion = "ring-measure-once-v3.5"      # v3.1: bore heights from the 5–95% surface span; v3.4: bore diameters
                                                  # through the centre and the ellipse fit with what its correction leaves;
                                                  # v3.5: the inner diameter is the largest circle that passes (a ring gauge)
                                                  # v3.2: bore centre searched (heavy heads move the centroid)
                                                  # v3.3: bore = inner wall of 9 heights combined (open designs)
BoreMinShare = 0.35        # a ring bore spans at least this share of the ring's smaller in-plane size
PreviewTargetFaces = 25_000


def _Moments(Tri):
    """One exact pass: bounds, area, area-weighted first/second surface moments, and the enclosed
    volume about two reference points (equal for a closed mesh — a cheap heuristic, not a proof)."""
    Lo, Hi = _Bounds(Tri)
    O1 = (Lo + Hi) / 2
    O2 = O1 + (Hi - Lo) * np.array([0.37, 0.61, 0.83])
    Area, S1, S2, V1, V2 = 0.0, np.zeros(3), np.zeros((3, 3)), 0.0, 0.0
    for S in range(0, len(Tri), Chunk):
        C = Tri[S:S + Chunk].astype(np.float64)
        A_, B_, C_ = C[:, 0], C[:, 1], C[:, 2]
        Ar = np.linalg.norm(np.cross(B_ - A_, C_ - A_), axis=1) / 2
        Area += float(Ar.sum())
        Sum = A_ + B_ + C_
        S1 += (Ar[:, None] * Sum / 3).sum(axis=0)
        # E[x xT] over a triangle = ((a+b+c)(a+b+c)T + aaT + bbT + ccT) / 12
        W = Ar[:, None]
        S2 += (np.einsum("ni,nj->ij", W * Sum, Sum) + np.einsum("ni,nj->ij", W * A_, A_)
               + np.einsum("ni,nj->ij", W * B_, B_) + np.einsum("ni,nj->ij", W * C_, C_)) / 12
        a, b, c = A_ - O1, B_ - O1, C_ - O1
        V1 += float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum()) / 6
        a, b, c = A_ - O2, B_ - O2, C_ - O2
        V2 += float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum()) / 6
    Mu = S1 / Area
    Cov = S2 / Area - np.outer(Mu, Mu)
    return {"lo": Lo, "hi": Hi, "area": Area, "centroid": Mu, "cov": Cov, "v1": abs(V1), "v2": abs(V2)}


def _ExactSections(Tri, Centre, U, V, Axis, Heights) -> list:
    """Full-precision cross-sections at several heights in ONE pass (no point thinning)."""
    Ax = Axis.astype(np.float32)
    Offs = [float((Centre + H * Axis) @ Axis) for H in Heights]
    Parts = [[] for _ in Heights]
    for S in range(0, len(Tri), Chunk):
        C = Tri[S:S + Chunk]
        D0 = C @ Ax
        for K, Off in enumerate(Offs):
            D = D0 - Off
            X = (D.min(axis=1) < 0) & (D.max(axis=1) > 0)
            if X.any():
                Parts[K].append((C[X].astype(np.float64), D[X].astype(np.float64)))
    Out = []
    for K, H in enumerate(Heights):
        if not Parts[K]:
            Out.append(np.zeros((0, 2)))
            continue
        Origin = Centre + H * Axis
        T = np.concatenate([P[0] for P in Parts[K]]) - Origin
        D = np.concatenate([P[1] for P in Parts[K]])
        Ends = np.full((len(T), 3, 3), np.nan)
        for J, (A, B) in enumerate(((0, 1), (1, 2), (2, 0))):
            M = (D[:, A] * D[:, B]) < 0
            F = D[M, A] / (D[M, A] - D[M, B])
            Ends[M, J] = T[M, A] + F[:, None] * (T[M, B] - T[M, A])
        Valid = ~np.isnan(Ends[:, :, 0])
        Keep = Valid.sum(axis=1) == 2
        Ends, Valid = Ends[Keep], Valid[Keep]
        Order = np.argsort(~Valid, axis=1, kind="stable")[:, :2]
        P0 = np.take_along_axis(Ends, Order[:, :1, None], axis=1)[:, 0]
        P1 = np.take_along_axis(Ends, Order[:, 1:2, None], axis=1)[:, 0]
        Wt = np.linspace(0, 1, 8)[None, :, None]
        P3 = (P0[:, None] * (1 - Wt) + P1[:, None] * Wt).reshape(-1, 3)
        Out.append(np.c_[P3 @ U, P3 @ V])
    return Out


def _Bins(P, C) -> int:
    if len(P) == 0:
        return 0
    Rel = P - np.asarray(C)
    return int(len(np.unique(((np.arctan2(Rel[:, 1], Rel[:, 0]) + np.pi) / (2 * np.pi) * Directions).astype(int) % Directions)))


def _BoreSeed(P, Grid: int = 25, Sample: int = 20_000) -> np.ndarray:
    """A start point inside the bore. The surface centroid is not always inside it (a heavy head pulls
    it into the metal), so search a grid over the section for points enclosed by wall in every
    direction and take the one farthest from any wall (centre of the largest empty circle)."""
    if len(P) == 0:
        return np.zeros(2)
    S = P if len(P) <= Sample else P[np.random.default_rng(5).choice(len(P), Sample, replace=False)]
    Lo, Hi = S.min(0), S.max(0)
    Gx, Gy = np.meshgrid(np.linspace(Lo[0], Hi[0], Grid), np.linspace(Lo[1], Hi[1], Grid))
    Best, BestClear = np.zeros(2), -1.0
    for C in np.c_[Gx.ravel(), Gy.ravel()]:
        Rel = S - C
        Dist = np.hypot(Rel[:, 0], Rel[:, 1])
        Bin = ((np.arctan2(Rel[:, 1], Rel[:, 0]) + np.pi) / (2 * np.pi) * Directions).astype(int) % Directions
        if len(np.unique(Bin)) < Directions:                       # not enclosed: outside the bore
            continue
        Clear = float(Dist.min())
        if Clear > BestClear:
            Best, BestClear = C, Clear
    return Best


def _BoreProfile(P, C) -> np.ndarray:
    """The nearest wall distance per direction bin around the centre C (inf where no wall was cut)."""
    Rel = P - np.asarray(C)
    Dist = np.hypot(Rel[:, 0], Rel[:, 1])
    Bin = ((np.arctan2(Rel[:, 1], Rel[:, 0]) + np.pi) / (2 * np.pi) * Directions).astype(int) % Directions
    Near = np.full(Directions, np.inf)
    np.minimum.at(Near, Bin, Dist)
    return Near


def _BoreWall(P, C) -> np.ndarray:
    """The bore wall as one nearest point per direction bin around the centre C (what the circle is fitted to)."""
    Near = _BoreProfile(P, C)
    Ok = np.isfinite(Near)
    Mid = (np.arange(Directions) + 0.5) / Directions * 2 * np.pi - np.pi
    return np.asarray(C) + np.c_[np.cos(Mid), np.sin(Mid)][Ok] * Near[Ok][:, None]


def _InscribedCircle(P, C0, Span: float) -> dict | None:
    """The largest circle that passes the bore — what a ring gauge (mandrel) reads, so what the size means: the centre
    near C0 whose nearest wall, over every direction, is farthest away. A coarse grid within ±Span, then a fine one
    around the best; the wall must be there in 90% of the directions (an open design is combined over its heights)."""
    Best, BestR, BestN = None, -1.0, 0
    Base = np.asarray(C0, np.float64)
    for Half, Grid in ((Span, 13), (Span / 6, 13)):
        Centre = Best if Best is not None else Base
        for Dx in np.linspace(-Half, Half, Grid):
            for Dy in np.linspace(-Half, Half, Grid):
                C = Centre + (Dx, Dy)
                Near = _BoreProfile(P, C)
                Ok = np.isfinite(Near)
                if Ok.sum() < Directions * 0.9:
                    continue
                R = float(Near[Ok].min())
                if R > BestR:
                    Best, BestR, BestN = C, R, int(Ok.sum())
    return {"centre": Best.tolist(), "radius": BestR, "bins_filled": BestN} if Best is not None else None


def _BoreDiameters(Near) -> tuple | None:
    """The narrowest and the widest diameter of the bore through its centre (opposite direction bins summed):
    what a caliper reads across the hole, as opposed to the fitted circle."""
    H = Directions // 2
    D = Near[:H] + Near[H:]
    D = D[np.isfinite(D)]
    return (float(D.min()), float(D.max())) if len(D) else None


def _EllipsePreview(W, P, Ell) -> dict | None:
    """What scaling along the ellipse's axes would give — a 2D preview of ExportCorrectedStl in units of the ellipse (a
    true ellipse becomes the unit circle): on the wall points W, `median` = the median wall radius then and `residual`
    = std / median (≈ 0 for a true ellipse, more for a lobed or stepped wall); on all section points P, `min` = the
    radius of the largest circle that passes then (the gauge: the correction scales by it, so the corrected bore's
    gauge circle is the target even for a lobed wall)."""
    Th = Ell["angle"]
    Rot = np.array([[np.cos(-Th), -np.sin(-Th)], [np.sin(-Th), np.cos(-Th)]])
    Ax = np.array([Ell["a"], Ell["b"]])
    Q = (W - np.asarray(Ell["centre"])) @ Rot.T / Ax
    R = np.hypot(Q[:, 0], Q[:, 1])
    if not len(R) or np.median(R) <= 0:
        return None
    Ins = _InscribedCircle((P - np.asarray(Ell["centre"])) @ Rot.T / Ax, np.zeros(2), 0.15)
    return {"median": float(np.median(R)), "min": Ins["radius"] if Ins else float(R.min()),
            "residual": float(R.std() / np.median(R))}


def _FitEllipse(P: np.ndarray) -> dict | None:
    """Least-squares ellipse through the bore wall points (relative to the bore centre): centre, semi-axes (a ≥ b)
    and the angle of the major axis in the (U, V) plane. None when the points do not describe an ellipse."""
    if len(P) < 8:
        return None
    X, Y = P[:, 0], P[:, 1]
    W, *_ = np.linalg.lstsq(np.c_[X * X, X * Y, Y * Y, X, Y], np.ones(len(P)), rcond=None)
    Pq, S, Qq, D, E = W
    Q = np.array([[Pq, S / 2], [S / 2, Qq]])
    Vals, Vecs = np.linalg.eigh(Q)                               # ascending: the smaller value belongs to the major axis
    if Vals[0] <= 0:
        return None
    C = -0.5 * np.linalg.solve(Q, np.array([D, E]))
    K = 1 + C @ Q @ C
    if K <= 0:
        return None
    return {"centre": C.tolist(), "a": float(np.sqrt(K / Vals[0])), "b": float(np.sqrt(K / Vals[1])),
            "angle": float(np.arctan2(Vecs[1, 0], Vecs[0, 0]))}


def _FitBore(P) -> dict:
    C2, Radius, Std, Filled = _BoreSeed(P), None, None, 0
    for _ in range(8):
        if len(P) < Directions:
            break
        Rel = P - C2
        Ang = np.arctan2(Rel[:, 1], Rel[:, 0])
        Dist = np.hypot(Rel[:, 0], Rel[:, 1])
        Bin = ((Ang + np.pi) / (2 * np.pi) * Directions).astype(int) % Directions
        Near = np.full(Directions, np.inf)
        np.minimum.at(Near, Bin, Dist)
        Ok = np.isfinite(Near)
        Filled = int(Ok.sum())
        if Filled < Directions * 0.9:
            break
        Mid = (np.arange(Directions) + 0.5) / Directions * 2 * np.pi - np.pi
        Wall = C2 + np.c_[np.cos(Mid), np.sin(Mid)][Ok] * Near[Ok][:, None]
        NewC, _ = _FitCircle(Wall)
        R = np.linalg.norm(Wall - NewC, axis=1)
        Radius, Std = float(np.median(R)), float(R.std())
        Moved = float(np.linalg.norm(NewC - C2))
        C2 = NewC
        if Moved < 1e-6 * Radius:
            break
    return {"centre": C2.tolist(), "radius": Radius, "std": Std, "bins_filled": Filled}


def MeasureRaw(Source) -> dict:
    """Measure the raw Hi3D STL once, exactly. Returns plain numbers (JSON-serialisable)."""
    Tri = LoadTriangles(Source, "stl")
    Mo = _Moments(Tri)
    _, Vecs = np.linalg.eigh(Mo["cov"])                         # ascending: least spread = ring axis
    Axis, V, U = Vecs[:, 0], Vecs[:, 1], Vecs[:, 2]
    if np.dot(np.cross(U, V), Axis) < 0:                       # right-handed frame
        Axis = -Axis
    Centre = Mo["centroid"]
    R = np.vstack([U, V, Axis])
    Lo3, Hi3 = np.full(3, np.inf), np.full(3, -np.inf)          # extents in the ring frame
    Zc, Wa = np.empty(len(Tri), np.float32), np.empty(len(Tri), np.float32)   # axial height + area per face
    for S in range(0, len(Tri), Chunk):
        C = Tri[S:S + Chunk].astype(np.float64)
        P = (C.reshape(-1, 3) - Centre) @ R.T
        Lo3, Hi3 = np.minimum(Lo3, P.min(0)), np.maximum(Hi3, P.max(0))
        Zc[S:S + len(C)] = P[:, 2].reshape(-1, 3).mean(axis=1)
        Wa[S:S + len(C)] = np.linalg.norm(np.cross(C[:, 1] - C[:, 0], C[:, 2] - C[:, 0]), axis=1) / 2
    # Bore heights inside the band: the 5–95% (area-weighted) span of the surface along the axis, so
    # an ornament or a chamfered edge never decides the sizing slices.
    Order = np.argsort(Zc, kind="stable")
    Cum = np.cumsum(Wa[Order], dtype=np.float64)
    Zlo, Zhi = (float(Zc[Order[min(np.searchsorted(Cum, Q * Cum[-1]), len(Order) - 1)]]) for Q in (0.05, 0.95))
    del Zc, Wa, Order, Cum
    # The bore a finger must pass through, seen along the axis: the inner wall from 9 heights across the
    # band, combined. Works for open / crossover designs where no single flat slice has a closed wall;
    # for a closed band it is the narrowest bore.
    Heights = [Zlo + (Zhi - Zlo) * F for F in np.linspace(0.1, 0.9, 9)]
    Secs = _ExactSections(Tri, Centre, U, V, Axis, Heights)
    Comb = np.concatenate([P for P in Secs if len(P)]) if any(len(P) for P in Secs) else np.zeros((0, 2))
    Fit = _FitBore(Comb)
    Slices = [{"height": float(H), "points": int(len(P)), "bins_filled": _Bins(P, Fit["centre"])} for H, P in zip(Heights, Secs)]
    # The size: the largest circle that passes the bore — what a ring gauge reads (v3.5; before: the fitted circle,
    # which overstated an oval or stepped bore). The hole across that circle's centre (narrowest = the gauge, widest),
    # and the bore as an ellipse (its two axes and their directions: what a bore that is not round is corrected by,
    # with what that correction would leave)
    Ins, Dia, Ell = None, None, None
    if Fit["radius"] and len(Comb):
        Ins = _InscribedCircle(Comb, Fit["centre"], 0.15 * Fit["radius"])
        Dia = _BoreDiameters(_BoreProfile(Comb, Ins["centre"])) if Ins else None
        W = _BoreWall(Comb, Fit["centre"]) - np.asarray(Fit["centre"])
        Ell = _FitEllipse(W)
        if Ell:
            Ell.update(_EllipsePreview(W, Comb - np.asarray(Fit["centre"]), Ell) or {"median": None, "min": None, "residual": None})
            Ell["centre"] = (np.asarray(Fit["centre"]) + np.asarray(Ell["centre"])).tolist()
    del Secs, Comb
    MinExtent = float(min(Hi3[0] - Lo3[0], Hi3[1] - Lo3[1]))
    Good = bool(Fit["radius"] and Ins and Ins["radius"] > 0 and Fit["bins_filled"] >= Directions * 0.9
                and 2 * Fit["radius"] >= BoreMinShare * MinExtent)      # never a small pocket in the head
    Closed = abs(Mo["v1"] - Mo["v2"]) <= 1e-6 * max(Mo["v1"], 1e-30)
    Out = {
        "method_version": FastMethodVersion, "faces": int(len(Tri)),
        "extent_x": float(Hi3[0] - Lo3[0]), "extent_y": float(Hi3[1] - Lo3[1]), "extent_z": float(Hi3[2] - Lo3[2]),
        "volume": float(Mo["v1"]), "volume_alt_reference": float(Mo["v2"]), "area": float(Mo["area"]),
        "closed_heuristic": bool(Closed),
        "frame": {"centre": Centre.tolist(), "u": U.tolist(), "v": V.tolist(), "axis": Axis.tolist()},
        "bore_ok": Good, "bore_slices": Slices,
        "bore_fit": {"radius": Fit["radius"], "std": Fit["std"], "bins_filled": Fit["bins_filled"], "centre": Fit["centre"],
                     "inscribed": Ins},
    }
    if Good:
        Mid = (Zlo + Zhi) / 2
        Bc = Centre + Ins["centre"][0] * U + Ins["centre"][1] * V + Mid * Axis      # the gauge circle's centre
        Out.update({"inner_diameter": 2 * Ins["radius"], "fitted_diameter": 2 * Fit["radius"],
                    "roundness": Fit["std"] / Fit["radius"],
                    "bore_origin": Bc.tolist(), "bore_origin_uv": list(Ins["centre"]), "bore_ellipse": Ell,
                    "bore_min_diameter": 2 * Ins["radius"], "bore_max_diameter": Dia[1] if Dia else None})
    return Out


def Scaled(Raw: dict, TargetInnerDiameterMm: float) -> dict:
    """Exact values at the target size (uniform scaling): no file is read."""
    S = TargetInnerDiameterMm / Raw["inner_diameter"]
    return {"scale_factor": S, "size_x_mm": Raw["extent_x"] * S, "size_y_mm": Raw["extent_y"] * S,
            "size_z_mm": Raw["extent_z"] * S, "inner_diameter_mm": Raw["inner_diameter"] * S,
            "volume_mm3": Raw["volume"] * S ** 3, "surface_area_mm2": Raw["area"] * S ** 2,
            # the bore across its centre, narrowest and widest (v3.4 measurements; None before)
            "bore_min_diameter_mm": Raw["bore_min_diameter"] * S if Raw.get("bore_min_diameter") else None,
            "bore_max_diameter_mm": Raw["bore_max_diameter"] * S if Raw.get("bore_max_diameter") else None}


def _Transform(Raw: dict, Scale: float = 1.0):
    F = Raw["frame"]
    R = np.vstack([F["u"], F["v"], F["axis"]])
    Origin = Raw.get("bore_origin") or F["centre"]
    return np.asarray(Origin), (R.T * Scale)


def ExportScaledStl(Source, Raw: dict, TargetInnerDiameterMm: float, Out) -> int:
    """Write the scaled, aligned STL (bore centre at the origin, ring axis along Z, millimetres)."""
    Origin, M = _Transform(Raw, TargetInnerDiameterMm / Raw["inner_diameter"])
    Count = (os.path.getsize(Source) - 84) // 50
    Rec = np.memmap(Source, dtype=StlRecord, mode="r", offset=84, shape=(Count,))
    with open(Out, "wb") as F:
        F.write(b"XJet P3 scaled ring".ljust(80, b" "))
        F.write(np.uint32(Count).tobytes())
        for S in range(0, Count, Chunk):
            C = (Rec["v"][S:S + Chunk].astype(np.float64) - Origin) @ M
            N = np.cross(C[:, 1] - C[:, 0], C[:, 2] - C[:, 0])
            L = np.linalg.norm(N, axis=1, keepdims=True)
            Block = np.zeros(len(C), StlRecord)
            Block["n"] = N / np.where(L > 0, L, 1)
            Block["v"] = C
            F.write(Block.tobytes())
    del Rec
    return int(Count)


def CorrectionFor(Raw: dict, TargetInnerDiameterMm: float) -> dict:
    """The scaling that makes a bore that is not round a circle of the target diameter: along the bore ellipse's major
    and minor axes separately, and by their geometric mean along the ring axis (the band keeps the average in-plane
    scale). The outer shape stretches by the same few percent."""
    E = Raw.get("bore_ellipse")
    if not E or not Raw.get("bore_ok"):
        raise ValueError("The bore was not fitted as an ellipse — measure the model again first.")
    Rt = TargetInnerDiameterMm / 2
    # The smallest wall radius after the correction, in ellipse units (≈ 1 for a true ellipse): the gauge circle of the
    # corrected bore must be the target, as the size means (v3.5; before: the fitted circle's median radius)
    Ref = E.get("min") or E.get("median") or 1.0
    Sa, Sb = Rt / (E["a"] * Ref), Rt / (E["b"] * Ref)
    return {"a": E["a"], "b": E["b"], "ellipticity": E["a"] / E["b"], "angle_deg": float(np.degrees(E["angle"])),
            "scale_major": float(Sa), "scale_minor": float(Sb), "scale_axis": float(np.sqrt(Sa * Sb)),
            "uniform_scale": TargetInnerDiameterMm / Raw["inner_diameter"], "target_inner_diameter_mm": TargetInnerDiameterMm}


def _CorrectedTransform(Raw: dict, Corr: dict):
    """(origin, matrix) for `(P - origin) @ matrix`: into the ring frame, turned so the bore ellipse's axes are X and Y,
    scaled per axis — the output has the bore centre at the origin and the ring axis along Z."""
    F, E = Raw["frame"], Raw["bore_ellipse"]
    U, V, Ax = (np.asarray(F[K], np.float64) for K in ("u", "v", "axis"))
    R = np.vstack([U, V, Ax])
    Th = np.radians(Corr["angle_deg"])
    C, S = np.cos(Th), np.sin(Th)
    Sa, Sb, Sw = Corr["scale_major"], Corr["scale_minor"], Corr["scale_axis"]
    M = R.T @ np.array([[C * Sa, -S * Sb, 0.0], [S * Sa, C * Sb, 0.0], [0.0, 0.0, Sw]])
    # bore_origin is the gauge circle's centre (v3.5; the fitted circle's before): move to the ellipse's centre
    Oc, Ec = np.asarray(Raw.get("bore_origin_uv") or Raw["bore_fit"]["centre"]), np.asarray(E["centre"])
    Origin = np.asarray(Raw["bore_origin"]) + (Ec[0] - Oc[0]) * U + (Ec[1] - Oc[1]) * V
    return Origin, M


def ExportCorrectedStl(Source, Raw: dict, TargetInnerDiameterMm: float, Out) -> dict:
    """Write the ring with its bore made round (CorrectionFor): bore centre at the origin, ring axis along Z,
    millimetres. Returns the correction applied."""
    Corr = CorrectionFor(Raw, TargetInnerDiameterMm)
    Origin, M = _CorrectedTransform(Raw, Corr)
    Count = (os.path.getsize(Source) - 84) // 50
    Rec = np.memmap(Source, dtype=StlRecord, mode="r", offset=84, shape=(Count,))
    with open(Out, "wb") as F:
        F.write(b"XJet P3 scaled ring, bore made round".ljust(80, b" "))
        F.write(np.uint32(Count).tobytes())
        for S in range(0, Count, Chunk):
            C = (Rec["v"][S:S + Chunk].astype(np.float64) - Origin) @ M
            N = np.cross(C[:, 1] - C[:, 0], C[:, 2] - C[:, 0])
            L = np.linalg.norm(N, axis=1, keepdims=True)
            Block = np.zeros(len(C), StlRecord)
            Block["n"] = N / np.where(L > 0, L, 1)
            Block["v"] = C
            F.write(Block.tobytes())
    del Rec
    return {**Corr, "faces": int(Count)}


def WritePreview(Source, Raw: dict, Out, Target: int = PreviewTargetFaces) -> int:
    """Visual-only light preview (indexed, ~25k faces): one vertex-clustering pass in the ring frame.
    Format: b"P3PV" · uint32 vertices · uint32 faces · float32 xyz… · uint32 i j k…
    Never used for any measurement."""
    Tri = LoadTriangles(Source, "stl")
    Origin, M = _Transform(Raw)
    O32, M32 = Origin.astype(np.float32), M.astype(np.float32)
    for S in range(0, len(Tri), Chunk):
        Tri[S:S + Chunk] = (Tri[S:S + Chunk] - O32) @ M32
    Lo, Hi = _Bounds(Tri)
    Step = max(float(np.sqrt(2.0 * Raw["area"] / Target)), float((Hi - Lo).max()) / 2000)
    Keys = _CornerKeys(Tri, Lo, Step, Round=False)
    Uniq, Inv = np.unique(Keys, return_inverse=True)
    del Keys
    Cnt = np.bincount(Inv, minlength=len(Uniq)).astype(np.float64)
    Flat = Tri.reshape(-1, 3)
    Verts = np.stack([np.bincount(Inv, weights=Flat[:, K], minlength=len(Uniq)) / Cnt for K in range(3)], axis=1)
    del Tri, Flat
    Ids = Inv.reshape(-1, 3)
    Ok = (Ids[:, 0] != Ids[:, 1]) & (Ids[:, 1] != Ids[:, 2]) & (Ids[:, 0] != Ids[:, 2])
    Faces = Ids[Ok]
    Faces = Faces[np.unique(np.sort(Faces, axis=1), axis=0, return_index=True)[1]]
    Used, Remap = np.unique(Faces, return_inverse=True)
    Verts, Faces = Verts[Used].astype(np.float32), Remap.reshape(-1, 3).astype(np.uint32)
    with open(Out, "wb") as F:
        F.write(b"P3PV" + np.uint32(len(Verts)).tobytes() + np.uint32(len(Faces)).tobytes())
        F.write(Verts.tobytes())
        F.write(Faces.tobytes())
    return int(len(Faces))


def IntegrityCheck(Source) -> dict:
    """Background mesh-integrity check (edge manifoldness). Never blocks results."""
    Tri = LoadTriangles(Source, "stl")
    return {"watertight": _Watertight(Tri), "faces": int(len(Tri))}
