"""Charm 3D geometry — a path of its own, separate from rings (no bore, no ring size).

The raw Hi3D model is measured once, exactly; every charm size after that is arithmetic:
  * the charm's frame: thickness = the direction of least spread (front to back); height = the model's up
    direction (Hi3D STL models are Z-up: the image's vertical) laid into the charm's face plane; width = across;
  * the height is the model's OVERALL height along that direction: the main body and the attachment loop
    together — which is what a charm's size means for now (products.CharmSizeDefinition). The loop is not
    detected or measured; the whole charm is scaled;
  * scale = target height / measured height: lengths × s, area × s², volume × s³.
The scaled STL is written on demand only: the charm lies flat (thickness along Z), width along X, height
along Y (the top of the charm toward +Y), centred on the middle of its box, in millimetres.
"""

import os

import numpy as np

from p3.geometry import Chunk, LoadTriangles, StlRecord, _Moments, _Transform

CharmMethodVersion = "charm-measure-once-v1"
Up = np.array([0.0, 0.0, 1.0])          # Hi3D models are Z-up
FlatLimit = 0.3                          # the up direction barely lies in the face plane: the charm is lying flat


def MeasureCharmRaw(Source) -> dict:
    """Measure the raw Hi3D STL of a charm once, exactly. Returns plain numbers (JSON-serialisable)."""
    Tri = LoadTriangles(Source, "stl")
    Mo = _Moments(Tri)
    _, Vecs = np.linalg.eigh(Mo["cov"])                         # ascending spread: the least = the thickness
    Thick = Vecs[:, 0]
    Height = Up - np.dot(Up, Thick) * Thick
    UpSource = "model_up"
    if np.linalg.norm(Height) < FlatLimit:                     # a charm lying flat: its longest direction is the height
        Height, UpSource = Vecs[:, 2] - np.dot(Vecs[:, 2], Thick) * Thick, "largest_spread"
    Height = Height / np.linalg.norm(Height)
    Width = np.cross(Height, Thick)                            # (width, height, thickness) is right-handed
    Centre = Mo["centroid"]
    R = np.vstack([Width, Height, Thick])
    Lo3, Hi3 = np.full(3, np.inf), np.full(3, -np.inf)
    for S in range(0, len(Tri), Chunk):
        P = (Tri[S:S + Chunk].astype(np.float64).reshape(-1, 3) - Centre) @ R.T
        Lo3, Hi3 = np.minimum(Lo3, P.min(0)), np.maximum(Hi3, P.max(0))
    Ext = Hi3 - Lo3
    BoxCentre = Centre + ((Lo3 + Hi3) / 2) @ R                  # the middle of the charm's box, in model coordinates
    Closed = abs(Mo["v1"] - Mo["v2"]) <= 1e-6 * max(Mo["v1"], 1e-30)
    return {
        "method_version": CharmMethodVersion, "faces": int(len(Tri)),
        "extent_x": float(Ext[0]), "extent_y": float(Ext[1]), "extent_z": float(Ext[2]), "height": float(Ext[1]),
        "volume": float(Mo["v1"]), "volume_alt_reference": float(Mo["v2"]), "area": float(Mo["area"]),
        "closed_heuristic": bool(Closed), "up_source": UpSource,
        "frame": {"centre": BoxCentre.tolist(), "u": Width.tolist(), "v": Height.tolist(), "axis": Thick.tolist()},
    }


def CharmScaled(Raw: dict, HeightMm: float) -> dict:
    """Exact values at the target height (uniform scaling): no file is read."""
    if not Raw.get("height"):
        raise ValueError("The model has no height to scale by.")
    S = HeightMm / Raw["height"]
    return {"scale_factor": S, "size_x_mm": Raw["extent_x"] * S, "size_y_mm": Raw["extent_y"] * S,
            "size_z_mm": Raw["extent_z"] * S, "inner_diameter_mm": None, "height_mm": Raw["height"] * S,
            "volume_mm3": Raw["volume"] * S ** 3, "surface_area_mm2": Raw["area"] * S ** 2}


def ExportScaledCharmStl(Source, Raw: dict, HeightMm: float, Out, Label: str = "") -> int:
    """Write the scaled STL of a charm: lying flat, width along X, height along Y, thickness along Z, millimetres.
    The 80-byte header says what it is: "XJet P3 scaled charm 20 mm total height incl. loop"."""
    Origin, M = _Transform(Raw, CharmScaled(Raw, HeightMm)["scale_factor"])
    Count = (os.path.getsize(Source) - 84) // 50
    Rec = np.memmap(Source, dtype=StlRecord, mode="r", offset=84, shape=(Count,))
    with open(Out, "wb") as F:
        F.write(" ".join(X for X in ("XJet P3 scaled charm", Label) if X).encode("ascii", "replace")[:80].ljust(80, b" "))
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
