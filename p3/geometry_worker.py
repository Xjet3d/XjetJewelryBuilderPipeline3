"""Run one heavy local STL job in its own process (python -m p3.geometry_worker <args.json>).

Multi-million-face models need a lot of memory. Running each job in a child process with a hard
address-space cap (P3_GEOMETRY_MEMORY_MB, Linux) means a model that does not fit only fails this job —
it can never take the web server down.

args.json: {"kind": "measure" | "preview" | "integrity" | "export", "source": <raw STL>, "result": <json out>,
            "product": "charm" for a charm's measure / export (p3/charmgeometry.py; rings: absent),
            "raw": {measurement}, "target_mm": <float>, "output": <file out>,
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
        Out = {"faces": cg.ExportScaledCharmStl(A["source"], A["raw"], float(A["target_mm"]), A["output"])}
    elif Kind == "export":
        Out = {"faces": g.ExportScaledStl(A["source"], A["raw"], float(A["target_mm"]), A["output"])}
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
