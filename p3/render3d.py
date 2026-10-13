"""A still picture of a 3D model from its light preview (P3PV, ~25k faces) — the image of an external part's design.

Runs in the geometry worker (python -m p3.geometry_worker, kind "render"): numpy for the geometry, PIL to paint the faces
far-to-near with simple metal shading on a light background. No AI call, no GPU, about a second. The preview is in the
model's own frame (p3/geometry.py WritePreview): a ring's axis along Z, a charm's height along Y and thickness along Z.
"""

import numpy as np


def ReadPreview(Source) -> tuple[np.ndarray, np.ndarray]:
    """P3PV: b"P3PV" · uint32 vertices · uint32 faces · float32 xyz… · uint32 i j k…"""
    with open(Source, "rb") as F:
        Data = F.read()
    if Data[:4] != b"P3PV":
        raise ValueError("not a P3PV preview")
    Nv, Nf = (int(X) for X in np.frombuffer(Data[4:12], "<u4"))
    V = np.frombuffer(Data[12:12 + 12 * Nv], "<f4").reshape(Nv, 3).astype(np.float64)
    F_ = np.frombuffer(Data[12 + 12 * Nv:12 + 12 * Nv + 12 * Nf], "<u4").reshape(Nf, 3)
    return V, F_


def _Rot(Axis: str, Degrees: float) -> np.ndarray:
    C, S = np.cos(np.radians(Degrees)), np.sin(np.radians(Degrees))
    return {"x": np.array([[1, 0, 0], [0, C, -S], [0, S, C]]), "y": np.array([[C, 0, S], [0, 1, 0], [-S, 0, C]]),
            "z": np.array([[C, -S, 0], [S, C, 0], [0, 0, 1]])}[Axis]


def RenderPng(Source, Out, Product: str = "ring", Size: int = 1024) -> dict:
    """Write a Size × Size PNG of the preview: a ring three-quarters from the front (its axis turned toward the viewer, so
    the band and its width show), a charm from the front, turned a little to show its thickness."""
    from PIL import Image, ImageDraw
    V, F = ReadPreview(Source)
    if not len(F):
        raise ValueError("the preview has no faces")
    V = V - (V.min(0) + V.max(0)) / 2
    View = (_Rot("y", 25) @ _Rot("x", -12)) if Product == "charm" else (_Rot("y", 30) @ _Rot("x", -25))
    P = V @ View.T                                         # screen: x right, y up, z toward the viewer
    Tri = P[F]
    N = np.cross(Tri[:, 1] - Tri[:, 0], Tri[:, 2] - Tri[:, 0])
    N /= np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-12)
    N *= np.where(N[:, 2:3] < 0, -1.0, 1.0)                # two-sided: every visible face lit from the front
    Light = np.array([-0.45, 0.6, 0.66])
    Light /= np.linalg.norm(Light)
    Half = Light + np.array([0, 0, 1.0])
    Half /= np.linalg.norm(Half)
    Shade = 0.28 + 0.62 * np.clip(N @ Light, 0, 1) + 0.45 * np.clip(N @ Half, 0, 1) ** 28
    Base = np.array([196.0, 197.0, 202.0])
    Colours = np.clip(Base * Shade[:, None], 0, 255).astype(np.uint8)
    S2 = Size * 2                                           # drawn at twice the size, then scaled down (smooth edges)
    Span = max(float(np.ptp(P[:, 0])), float(np.ptp(P[:, 1])), 1e-9)
    Scale = S2 * 0.8 / Span
    Xy = np.empty((len(Tri), 3, 2))
    Xy[:, :, 0] = S2 / 2 + Tri[:, :, 0] * Scale
    Xy[:, :, 1] = S2 / 2 - Tri[:, :, 1] * Scale
    Img = Image.new("RGB", (S2, S2), (245, 245, 244))
    Draw = ImageDraw.Draw(Img)
    for I in np.argsort(Tri[:, :, 2].mean(1), kind="stable"):      # far to near
        C = tuple(int(X) for X in Colours[I])
        Draw.polygon([tuple(Pt) for Pt in Xy[I]], fill=C, outline=C)
    Img = Img.resize((Size, Size), Image.LANCZOS)
    Img.save(Out, "PNG", optimize=True)
    return {"faces": int(len(F)), "size": Size}
