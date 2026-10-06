"""Run one heavy local STL job in its own process (python -m p3.geometry_worker <args.json>).

Multi-million-face models need a lot of memory. Running each job in a child process with a hard
address-space cap (P3_GEOMETRY_MEMORY_MB, Linux) means a model that does not fit only fails this job —
it can never take the web server down.

args.json: {"kind": "measure" | "preview" | "integrity" | "export" | "fix_bore", "source": <raw STL>, "result": <json out>,
            "product": "charm" for a charm's measure / export (p3/charmgeometry.py; rings: absent),
            "raw": {measurement}, "target_mm": <float>, "output": <file out>,
            export only: "round": true for a ring whose bore was made round (the corrected scaling, not the uniform one),
            measure only: "hash": bool (SHA-256 when not hashed at download), "convert_to": <binary STL path>,
            "format": <source format>}
exit: 0 = result written · 3 = out of memory · 1 = other error (message in the result file)
"""

import json
import os
import sys
import time

ExitMemory = 3


def _Cap() -> None:
    Mb = int(os.environ.get("P3_GEOMETRY_MEMORY_MB", "2500"))
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (Mb * 1024 * 1024, Mb * 1024 * 1024))
    except (ImportError, ValueError, OSError):      # not available on Windows: run uncapped
        pass


def _BinaryStl(PathStr: str) -> bool:
    with open(PathStr, "rb") as F:
        Head = F.read(84)
    return len(Head) == 84 and os.path.getsize(PathStr) == 84 + 50 * int.from_bytes(Head[80:84], "little")


def _Sha256(PathStr: str) -> str:
    import hashlib
    H = hashlib.sha256()
    with open(PathStr, "rb") as F:
        for Block in iter(lambda: F.read(1 << 20), b""):
            H.update(Block)
    return H.hexdigest()


def Run(A: dict) -> dict:
    from p3 import geometry as g
    T = time.perf_counter()
    Kind = A["kind"]
    if Kind == "measure":
        Source, Out = A["source"], {}
        if A.get("convert_to") and not _BinaryStl(Source):     # GLB / OBJ / ASCII STL → binary STL master
            g.WriteStl(g.LoadTriangles(Source, A.get("format") or "stl"), A["convert_to"])
            Source, Out["converted"] = A["convert_to"], True
        if A.get("hash"):
            Out["sha256"], Out["bytes"] = _Sha256(Source), os.path.getsize(Source)
        if A.get("product") == "charm":
            from p3 import charmgeometry as cg
            Out["raw"] = cg.MeasureCharmRaw(Source)
        else:
            Out["raw"] = g.MeasureRaw(Source)
    elif Kind == "preview":
        Out = {"preview_faces": g.WritePreview(A["source"], A["raw"], A["output"])}
    elif Kind == "integrity":
        Out = g.IntegrityCheck(A["source"])
    elif Kind == "export" and A.get("product") == "charm":
        from p3 import charmgeometry as cg
        Out = {"faces": cg.ExportScaledCharmStl(A["source"], A["raw"], float(A["target_mm"]), A["output"], A.get("label", ""))}
    elif Kind == "export" and A.get("round"):
        # A result whose bore was made round: the same correction again (the measurement is deterministic, so an
        # older measurement without the ellipse fit is simply redone)
        Raw = A["raw"] if A["raw"].get("bore_ellipse") else g.MeasureRaw(A["source"])
        Out = {"faces": g.ExportCorrectedStl(A["source"], Raw, float(A["target_mm"]), A["output"])["faces"]}
    elif Kind == "export":
        Out = {"faces": g.ExportScaledStl(A["source"], A["raw"], float(A["target_mm"]), A["output"])}
    elif Kind == "fix_bore":
        # A ring whose bore is not round: scale along the bore's two axes so it becomes a circle of the target
        # diameter and measure that model. The file is temporary — the STL is exported on demand like the uniform
        # scaling. An older measurement without the ellipse fit is redone first.
        Again = not A["raw"].get("bore_ellipse")
        Raw = g.MeasureRaw(A["source"]) if Again else A["raw"]
        Corr = g.ExportCorrectedStl(A["source"], Raw, float(A["target_mm"]), A["output"])
        try:
            Out = {"correction": Corr, "measured": g.MeasureRaw(A["output"]), **({"raw_measured": Raw} if Again else {})}
        finally:
            try:
                os.remove(A["output"])
            except OSError:
                pass
    else:
        raise ValueError(f"unknown job kind {Kind}")
    return {"ok": True, **Out, "seconds": round(time.perf_counter() - T, 3)}


def Main(Argv: list[str]) -> int:
    with open(Argv[0], encoding="utf-8") as F:
        A = json.load(F)
    _Cap()
    try:
        Doc, Code = Run(A), 0
    except MemoryError:
        Doc, Code = {"ok": False, "error": "memory"}, ExitMemory
    except Exception as E:  # noqa: BLE001
        Doc, Code = {"ok": False, "error": f"{type(E).__name__}: {E}"}, 1
    with open(A["result"], "w", encoding="utf-8") as F:
        json.dump(Doc, F, default=float)
    return Code


if __name__ == "__main__":
    sys.exit(Main(sys.argv[1:]))
