"""Gallery-only sync between two Pipeline 3 sites: proto's approved Inspiration Gallery onto atelier.

What moves: XJet's own published gallery masters (gallery items whose owner_kind is 'xjet', of designs made with the
real AI provider), the XJet design a variation came from (the homepage story tells it from there), their batches, their
images (ready and failed options, so every option letter of a Ring ID stays the same), their ready 360° movies, the files
behind them and the thumbnails, posters and clips already made of those files. Only the columns listed in `Columns` are
read or written. Nothing about who used, bought, paid for or asked for a design moves: no accounts or sign-in tokens, no
sessions or session events, no gallery uses or favourites, no customizations, bag, orders or quotes, no usage or credits,
no 3D models, nothing generated in mock mode, no customer's design.

A bundle is a directory (scripts/gallery-sync.sh carries it as the tree of a git commit, or as a tar):
  manifest.json   format, source, the gallery items, every file with its SHA-256, size and modification time
  rows.json       the rows per table, allow-listed columns only
  files/<sha256>  the files, content-addressed: a name never comes from the source, a file shared by two rows is stored once

Import is idempotent. Rows are matched by id and written only when they differ, files only when their content differs, and
every row the sync wrote is recorded (gallery_sync_items), so a second run changes nothing and a later run brings what
changed on the source, and takes off the site the synced items the source no longer publishes (their designs stay:
customers may be using them). A row that exists on the target but did not come from this sync is never touched: the
import stops and names it. Ring IDs and share link names are kept when they are free on the target and never change once
given. The synced tiles come first, in the source's order.

    python -m p3.gallerysync export DIR [--data-dir D] [--source proto]
    python -m p3.gallerysync check DIR
    python -m p3.gallerysync import DIR [--data-dir D] [--apply] [--keep-removed] [--ref COMMIT] [--json]
    python -m p3.gallerysync status [--data-dir D]
    python -m p3.gallerysync verify BASE_URL [--expect N]
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from p3 import assets
from p3 import ringids as RingIds
from p3.db import Database, NewId, Now

Format, Version = "p3-gallery-bundle", 1
Order = ("designs", "batches", "candidates", "movies", "gallery_items")
Columns = {
    "designs": ("id", "owner_account_id", "title", "prompt", "selected_candidate_id", "client_request_id", "created_at",
                "updated_at", "ai_mode", "ring_no", "charm_no", "source_design_id", "source_candidate_id", "share_slug",
                "product_type"),
    "batches": ("id", "design_id", "kind", "parent_candidate_id", "user_text", "effective_prompt", "endpoint",
                "reference_asset", "desired_count", "config_version", "client_request_id", "created_at"),
    "candidates": ("id", "batch_id", "slot", "status", "seed", "attempts", "duplicate_retries", "provider_request_id",
                   "asset_path", "content_sha256", "error", "error_code", "movie_id", "credit_ref", "created_at", "updated_at"),
    "movies": ("id", "candidate_id", "config_version", "endpoint", "status", "provider_request_id", "asset_path", "error",
               "error_code", "made_by_admin", "created_at", "updated_at"),
    "gallery_items": ("id", "design_id", "candidate_id", "position", "created_at", "created_by", "owner_kind"),
}
# Never changed on a row the target already has: the target's own numbering, link names and tile order stay
Keep = {"designs": {"owner_account_id", "ring_no", "charm_no", "share_slug", "product_type"}, "gallery_items": {"position"}}
Ints = {"slot", "seed", "attempts", "duplicate_retries", "desired_count", "ring_no", "charm_no", "position"}
IdCols = {"id", "design_id", "batch_id", "candidate_id", "movie_id", "selected_candidate_id", "parent_candidate_id",
          "source_design_id", "source_candidate_id"}
IdRe = re.compile(r"^[a-z]{2,8}_[0-9a-f]{32}$")
ShaRe = re.compile(r"^[0-9a-f]{64}$")
SourceRe = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
SegmentRe = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
MaxFileBytes, MaxTotalBytes, MaxFiles, MaxText = 300 * 2**20, 8 * 2**30, 50_000, 20_000
MinFreeBytes = 1 << 30                       # an import never leaves the site's disk with less than this free
UserAgent = "p3-gallery-sync/1"


class SyncError(Exception):
    """A bundle that may not be imported, or a target it may not be imported into; the message says why."""


# ── small helpers ──────────────────────────────────────────────────────────
def _Sha(PathObj: Path) -> str:
    H = hashlib.sha256()
    with open(PathObj, "rb") as F:
        for Chunk in iter(lambda: F.read(1 << 20), b""):
            H.update(Chunk)
    return H.hexdigest()


def _SafeRel(Rel) -> bool:
    """A stored asset path: under designs/ (or its _derived/ copy), plain segments only."""
    if not isinstance(Rel, str) or len(Rel) > 400 or "\\" in Rel:
        return False
    Parts = Rel.split("/")
    Start = 1 if Parts[0] == "_derived" else 0
    return (len(Parts) - Start >= 3 and Parts[Start] == "designs"
            and all(P not in (".", "..") and SegmentRe.match(P) for P in Parts))


def _ReadOnly(DbPath: Path) -> sqlite3.Connection:
    if not DbPath.is_file():
        raise SyncError(f"No database at {DbPath}")
    Conn = sqlite3.connect(DbPath.resolve().as_uri() + "?mode=ro", uri=True)
    Conn.row_factory = sqlite3.Row
    return Conn


def _All(Conn, Sql: str, Args=()) -> list[dict]:
    return [dict(R) for R in Conn.execute(Sql, Args).fetchall()]


def _One(Conn, Sql: str, Args=()) -> dict | None:
    R = Conn.execute(Sql, Args).fetchone()
    return dict(R) if R else None


def _Mock(Row: dict) -> bool:
    return (Row.get("provider_request_id") or "").startswith("mockreq_")


def _Mb(N: int) -> str:
    return f"{N / 1e6:.1f} MB"


def _ContentId(RowsSha: str, Files: list[dict]) -> str:
    """What the bundle carries, whatever its export time: unchanged rows and files give the same id."""
    Lines = [RowsSha] + [f"{F['path']} {F['sha256']}" for F in sorted(Files, key=lambda F: F["path"])]
    return hashlib.sha256("\n".join(Lines).encode("utf-8")).hexdigest()


# ── export (on the source; read-only) ──────────────────────────────────────
def Export(DataDir: Path, OutDir: Path, Source: str = "proto", Commit: str | None = None) -> dict:
    """Write the bundle of the source's approved gallery into OutDir (created; must be empty). The database is opened
    read-only and nothing on the source changes."""
    if not SourceRe.match(Source or ""):
        raise SyncError("The source name is lower-case letters, digits, '-' and '_'.")
    DataDir, OutDir = Path(DataDir), Path(OutDir)
    Assets = DataDir / "assets"
    if OutDir.exists() and any(OutDir.iterdir()):
        raise SyncError(f"{OutDir} is not empty")
    Conn = _ReadOnly(DataDir / "pipeline3.db")
    Skipped: list[dict] = []
    try:
        Items, Masters = [], []
        for G in _All(Conn, "SELECT * FROM gallery_items ORDER BY position, created_at"):
            D = _One(Conn, "SELECT * FROM designs WHERE id = ?", (G["design_id"],))
            C = _One(Conn, "SELECT * FROM candidates WHERE id = ?", (G["candidate_id"],))
            Why = ("a customer's design" if (G.get("owner_kind") or "xjet") != "xjet"
                   else "its design is missing" if D is None
                   else "made in mock mode" if (D.get("ai_mode") or "") == "mock"
                   else "its image is not ready" if C is None or C["status"] != "ready" or not C["asset_path"] or _Mock(C)
                   else "its image file is missing" if not (Assets / C["asset_path"]).is_file()
                   else None)
            if Why:
                Skipped.append({"gallery_item_id": G["id"], "title": (D or {}).get("title"), "why": Why})
                continue
            Items.append(G)
            Masters.append(D)

        # The designs: every master, and the XJet design a variation came from (same owner, real), up to 5 levels
        Designs: dict[str, dict] = {}
        for D in Masters:
            Designs[D["id"]] = D
            Cur, Depth = D, 0
            while Cur.get("source_design_id") and Depth < 5:
                Src = _One(Conn, "SELECT * FROM designs WHERE id = ?", (Cur["source_design_id"],))
                if Src is None or Src["owner_account_id"] != D["owner_account_id"] or (Src.get("ai_mode") or "") == "mock":
                    break
                Designs.setdefault(Src["id"], Src)
                Cur, Depth = Src, Depth + 1

        Files: dict[str, tuple[Path, str]] = {}

        def AddFile(Rel: str | None, Kind: str) -> bool:
            if not Rel or not _SafeRel(Rel):
                return False
            P = assets.Resolve(Assets, Rel)
            if not P.is_file():
                return False
            Files[Rel] = (P, Kind)
            Derived = Assets / "_derived" / Rel
            if Derived.parent.is_dir():                     # thumbnails, poster frame, clips already made of this file
                for X in sorted(Derived.parent.iterdir()):
                    if X.is_file() and X.name.startswith(Derived.name) and ".tmp" not in X.name[len(Derived.name):]:
                        Files[f"_derived/{Rel}{X.name[len(Derived.name):]}"] = (X, "derived")
            return True

        Batches, Cands, Movies = [], [], []
        for Did in Designs:
            for B in _All(Conn, "SELECT * FROM batches WHERE design_id = ? ORDER BY created_at, id", (Did,)):
                Kept = []
                for C in _All(Conn, "SELECT * FROM candidates WHERE batch_id = ? ORDER BY slot", (B["id"],)):
                    if C["status"] not in ("ready", "failed") or _Mock(C):
                        continue                               # in flight, or a mock placeholder: never moves
                    if C["status"] == "ready" and not AddFile(C["asset_path"], "image"):
                        Skipped.append({"candidate_id": C["id"], "why": "its image file is missing"})
                        continue
                    if C["status"] == "failed":
                        C["asset_path"] = None
                    Kept.append(C)
                if not Kept:
                    continue
                if B.get("reference_asset") and not AddFile(B["reference_asset"], "reference"):
                    B["reference_asset"] = None
                Batches.append(B)
                Cands += Kept
        Ids = {C["id"] for C in Cands}
        for C in Cands:
            for M in _All(Conn, "SELECT * FROM movies WHERE candidate_id = ? AND status = 'ready' AND asset_path IS NOT NULL "
                                "ORDER BY created_at, id", (C["id"],)):
                if not _Mock(M) and AddFile(M["asset_path"], "movie"):
                    Movies.append(M)
        MovieIds = {M["id"] for M in Movies}
        DesignIds = set(Designs)

        # References to anything that stays behind are cleared
        for D in Designs.values():
            if D.get("selected_candidate_id") not in Ids:
                D["selected_candidate_id"] = None
            if D.get("source_design_id") not in DesignIds:
                D["source_design_id"] = D["source_candidate_id"] = None
            elif D.get("source_candidate_id") not in Ids:
                D["source_candidate_id"] = None
        for B in Batches:
            if B.get("parent_candidate_id") not in Ids:
                B["parent_candidate_id"] = None
        for C in Cands:
            if C.get("movie_id") not in MovieIds:
                C["movie_id"] = None

        def Pick(Table: str, Row: dict) -> dict:
            return {K: Row.get(K) for K in Columns[Table]}

        Rows = {"designs": [Pick("designs", D) for D in Designs.values()], "batches": [Pick("batches", B) for B in Batches],
                "candidates": [Pick("candidates", C) for C in Cands], "movies": [Pick("movies", M) for M in Movies],
                "gallery_items": [Pick("gallery_items", G) for G in Items]}
        _CheckRows(Rows, set(Files))                       # what is written is what an import accepts

        OutDir.mkdir(parents=True, exist_ok=True)
        (OutDir / "files").mkdir(exist_ok=True)
        Entries, Total = [], 0
        for Rel, (P, Kind) in sorted(Files.items()):
            St = P.stat()
            Digest = _Sha(P)
            Blob = OutDir / "files" / Digest
            if not Blob.exists():
                shutil.copyfile(P, Blob)
            Entries.append({"path": Rel, "sha256": Digest, "bytes": St.st_size, "mtime": St.st_mtime, "kind": Kind})
            Total += St.st_size
        RowsBytes = json.dumps(Rows, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")
        (OutDir / "rows.json").write_bytes(RowsBytes)
        RowsSha = hashlib.sha256(RowsBytes).hexdigest()
        Manifest = {
            "format": Format, "version": Version,
            "source": {"name": Source, "commit": Commit, "exported_at": Now()},
            "content_id": _ContentId(RowsSha, Entries), "rows_sha256": RowsSha,
            "counts": {**{T: len(Rows[T]) for T in Order}, "files": len(Entries), "bytes": Total},
            "items": [{"gallery_item_id": G["id"], "design_id": G["design_id"], "title": Designs[G["design_id"]]["title"],
                       "product_type": Designs[G["design_id"]].get("product_type") or "ring",
                       "ring_id": RingIds.Ref(Designs[G["design_id"]]), "slug": Designs[G["design_id"]].get("share_slug")}
                      for G in Items],
            "skipped": Skipped, "files": Entries,
        }
        (OutDir / "manifest.json").write_text(json.dumps(Manifest, ensure_ascii=False, sort_keys=True, indent=1), encoding="utf-8")
        return Manifest
    finally:
        Conn.close()


# ── checking a bundle (no target needed) ───────────────────────────────────
def _CheckRows(Rows: dict, Paths: set) -> None:
    """Allow-listed tables and columns, safe ids, terminal states only, every reference inside the bundle, every file
    a row needs in the bundle. Raises SyncError naming the first problem."""
    if not isinstance(Rows, dict) or set(Rows) - set(Order):
        raise SyncError(f"Unexpected tables in the bundle: {sorted(set(Rows) - set(Order)) if isinstance(Rows, dict) else Rows!r}")
    Seen: dict[str, dict] = {}
    for T in Order:
        Seen[T] = {}
        for R in Rows.get(T, []):
            if not isinstance(R, dict) or set(R) - set(Columns[T]) or "id" not in R:
                raise SyncError(f"{T}: a row with columns outside the allow-list or without an id")
            for K, V in R.items():
                if V is None:
                    continue
                if K in Ints:
                    if isinstance(V, bool) or not isinstance(V, int):
                        raise SyncError(f"{T} {R['id']}: {K} must be a whole number")
                elif not isinstance(V, str) or len(V) > MaxText or any(ord(Ch) < 32 and Ch not in "\n\r\t" for Ch in V):
                    raise SyncError(f"{T} {R.get('id')}: {K} is not plain text")
                if K in IdCols and not IdRe.match(V):
                    raise SyncError(f"{T}: {K} {V!r} is not an id")
            if R["id"] in Seen[T]:
                raise SyncError(f"{T} {R['id']} appears twice")
            Seen[T][R["id"]] = R
    D, B, C, M, G = (Seen[T] for T in Order)

    def Ref(Table, Row, Col, Target):
        if Row.get(Col) is not None and Row[Col] not in Target:
            raise SyncError(f"{Table} {Row['id']}: {Col} points outside the bundle")

    for R in D.values():
        if R.get("product_type") not in ("ring", "charm") or not R.get("owner_account_id") or not R.get("title"):
            raise SyncError(f"designs {R['id']}: product, owner and title are required")
        if (R.get("ai_mode") or "") == "mock":
            raise SyncError(f"designs {R['id']}: made in mock mode")
        Ref("designs", R, "selected_candidate_id", C)
        Ref("designs", R, "source_design_id", D)
        Ref("designs", R, "source_candidate_id", C)
    for R in B.values():
        if R.get("kind") not in ("initial", "refine") or R.get("design_id") not in D:
            raise SyncError(f"batches {R['id']}: unknown kind or design")
        Ref("batches", R, "parent_candidate_id", C)
        if R.get("reference_asset") is not None and R["reference_asset"] not in Paths:
            raise SyncError(f"batches {R['id']}: its reference image is not in the bundle")
    for R in C.values():
        if R.get("batch_id") not in B or R.get("status") not in ("ready", "failed"):
            raise SyncError(f"candidates {R['id']}: unknown batch, or not ready or failed")
        if R["status"] == "ready" and R.get("asset_path") not in Paths:
            raise SyncError(f"candidates {R['id']}: its image is not in the bundle")
        if _Mock(R):
            raise SyncError(f"candidates {R['id']}: a mock placeholder")
        Ref("candidates", R, "movie_id", M)
    for R in M.values():
        if R.get("candidate_id") not in C or R.get("status") != "ready" or R.get("asset_path") not in Paths or _Mock(R):
            raise SyncError(f"movies {R['id']}: unknown image, not ready, a mock placeholder or its file is missing")
    Designs_ = set()
    for R in G.values():
        Cand = C.get(R.get("candidate_id"))
        if R.get("owner_kind") != "xjet" or R.get("design_id") not in D or Cand is None or Cand["status"] != "ready":
            raise SyncError(f"gallery_items {R['id']}: not XJet's own, or its design or ready image is missing")
        if B[Cand["batch_id"]]["design_id"] != R["design_id"]:
            raise SyncError(f"gallery_items {R['id']}: its image belongs to another design")
        if R["design_id"] in Designs_:
            raise SyncError(f"gallery_items {R['id']}: a second tile for the same design")
        Designs_.add(R["design_id"])


def Load(BundleDir: Path) -> tuple[dict, dict]:
    """The manifest and the rows of a bundle, after every check: format, rows hash, each file's path, size and SHA-256,
    and the rows themselves."""
    BundleDir = Path(BundleDir)
    try:
        M = json.loads((BundleDir / "manifest.json").read_text(encoding="utf-8"))
        RowsBytes = (BundleDir / "rows.json").read_bytes()
    except (OSError, ValueError) as E:
        raise SyncError(f"Not a gallery bundle: {E}") from E
    if M.get("format") != Format or not isinstance(M.get("version"), int) or not 1 <= M["version"] <= Version:
        raise SyncError(f"Not a gallery bundle this version can read (format {M.get('format')!r}, version {M.get('version')!r})")
    if not SourceRe.match(str((M.get("source") or {}).get("name") or "")):
        raise SyncError("The bundle does not name its source site")
    if hashlib.sha256(RowsBytes).hexdigest() != M.get("rows_sha256"):
        raise SyncError("rows.json does not match the manifest")
    Files = M.get("files")
    if not isinstance(Files, list) or len(Files) > MaxFiles:
        raise SyncError("The manifest's file list is missing or too long")
    Paths, Total = set(), 0
    for F in Files:
        if not isinstance(F, dict) or not _SafeRel(F.get("path")) or not ShaRe.match(str(F.get("sha256"))):
            raise SyncError(f"Unsafe or malformed file entry: {F.get('path') if isinstance(F, dict) else F!r}")
        if F["path"] in Paths:
            raise SyncError(f"{F['path']} appears twice")
        if not isinstance(F.get("bytes"), int) or not 0 <= F["bytes"] <= MaxFileBytes or not isinstance(F.get("mtime"), (int, float)):
            raise SyncError(f"{F['path']}: size or time missing, or the file is too large")
        Blob = BundleDir / "files" / F["sha256"]
        if not Blob.is_file() or Blob.stat().st_size != F["bytes"] or _Sha(Blob) != F["sha256"]:
            raise SyncError(f"{F['path']}: its file is missing from the bundle or its content does not match")
        Paths.add(F["path"])
        Total += F["bytes"]
    if Total > MaxTotalBytes:
        raise SyncError("The bundle is larger than a gallery can be")
    if M.get("content_id") != _ContentId(M["rows_sha256"], Files):
        raise SyncError("The manifest's content id does not match its rows and files")
    Rows = json.loads(RowsBytes)
    _CheckRows(Rows, Paths)
    return M, Rows


# ── import (on the target) ─────────────────────────────────────────────────
def _Plan(Conn, M: dict, Rows: dict, Assets: Path | None, KeepRemoved: bool) -> dict:
    """What an import would do on this target. Files are compared only when Assets is given."""
    Source = M["source"]["name"]
    Prov = {(R["kind"], R["id"]) for R in _All(Conn, "SELECT kind, id FROM gallery_sync_items WHERE source = ?", (Source,))}
    P = {"insert": {T: [] for T in Order}, "update": {T: [] for T in Order}, "same": {T: 0 for T in Order},
         "conflicts": [], "replace_items": [], "unpublish": [], "numbers": {}, "slugs": {},
         "files_write": [], "files_same": 0, "bytes_write": 0}
    for T in Order:
        for R in Rows.get(T, []):
            Old = _One(Conn, f"SELECT * FROM {T} WHERE id = ?", (R["id"],))
            if Old is None:
                P["insert"][T].append(R["id"])
            elif (T, R["id"]) not in Prov:
                P["conflicts"].append(f"{T} {R['id']} exists on this site and did not come from {Source}: it is left alone")
            else:
                Changed = {K: R.get(K) for K in Columns[T] if K not in Keep.get(T, ()) and K in Old and Old[K] != R.get(K)}
                if Changed:
                    P["update"][T].append((R["id"], Changed))
                else:
                    P["same"][T] += 1
    Bundle = {R["id"] for R in Rows["gallery_items"]}
    for R in Rows["gallery_items"]:
        Other = _One(Conn, "SELECT id FROM gallery_items WHERE design_id = ? AND id != ?", (R["design_id"], R["id"]))
        if Other:
            if ("gallery_items", Other["id"]) in Prov:
                P["replace_items"].append(Other["id"])           # re-published on the source under a new id
            else:
                P["conflicts"].append(f"gallery_items {Other['id']}: this site shows the design {R['design_id']} in a tile of "
                                      "its own; take it off the gallery here first")
    if not KeepRemoved:
        for Kind, Id in sorted(Prov):
            if Kind == "gallery_items" and Id not in Bundle and Id not in P["replace_items"] \
                    and _One(Conn, "SELECT id FROM gallery_items WHERE id = ?", (Id,)):
                P["unpublish"].append(Id)
    # Ring / charm numbers and link names of the designs new here: the source's when free, else the next free one
    Taken = {R["share_slug"] for R in _All(Conn, "SELECT share_slug FROM designs WHERE share_slug IS NOT NULL")}
    New = set(P["insert"]["designs"])
    for D in Rows["designs"]:
        if D["id"] not in New:
            continue
        Col, Retired = ("charm_no", "retired_charms") if D["product_type"] == "charm" else ("ring_no", "retired_rings")
        N = D.get(Col)
        Free = N is not None and not _One(Conn, f"SELECT 1 AS x FROM designs WHERE {Col} = ?", (N,)) \
            and not _One(Conn, f"SELECT 1 AS x FROM {Retired} WHERE {Col} = ?", (N,))
        P["numbers"][D["id"]] = (Col, N if Free else None)
        Slug = D.get("share_slug")
        if Slug:
            Want, K = Slug, 2
            while Want in Taken:
                Want, K = f"{Slug}-{K}", K + 1
            Taken.add(Want)
            P["slugs"][D["id"]] = Want
    if Assets is not None:
        for F in M["files"]:
            Dst = assets.Resolve(Assets, F["path"])
            if Dst.is_file() and Dst.stat().st_size == F["bytes"] and _Sha(Dst) == F["sha256"]:
                P["files_same"] += 1
            else:
                P["files_write"].append(F)
                P["bytes_write"] += F["bytes"]
    return P


def _WriteFiles(BundleDir: Path, Assets: Path, Files: list[dict]) -> None:
    """Each file atomically (a reader never sees half a file), with the source's modification time, so a thumbnail,
    poster or clip made on the source counts as current here (p3/media.py compares times)."""
    for F in Files:
        Dst = assets.Resolve(Assets, F["path"])
        Dst.parent.mkdir(parents=True, exist_ok=True)
        Tmp = Dst.with_name(f".{Dst.name}.sync-{os.getpid()}.tmp")
        try:
            shutil.copyfile(Path(BundleDir) / "files" / F["sha256"], Tmp)
            os.utime(Tmp, (F["mtime"], F["mtime"]))
            os.replace(Tmp, Dst)
        finally:
            Tmp.unlink(missing_ok=True)


def _Apply(Conn, M: dict, Rows: dict, P: dict, RunId: str, T: str) -> None:
    Source = M["source"]["name"]
    Conn.execute("PRAGMA defer_foreign_keys = ON")          # rows of one design refer to each other: checked at COMMIT
    for Id in P["replace_items"] + P["unpublish"]:
        Conn.execute("DELETE FROM gallery_items WHERE id = ?", (Id,))
        Conn.execute("DELETE FROM gallery_sync_items WHERE kind = 'gallery_items' AND id = ?", (Id,))
    Inserts = {T_: set(P["insert"][T_]) for T_ in Order}
    Updates = {T_: dict(P["update"][T_]) for T_ in Order}
    for Tab in Order:
        Rs = Rows.get(Tab, [])
        if Tab == "designs":                                 # the ones that keep their number first, then the new numbers
            Rs = sorted(Rs, key=lambda D: P["numbers"].get(D["id"], ("", 0))[1] is None)
        for R in Rs:
            if R["id"] in Inserts[Tab]:
                Row = dict(R)
                if Tab == "designs":
                    Col, N = P["numbers"][R["id"]]
                    Row["ring_no"] = Row["charm_no"] = None
                    Row[Col] = N                             # None: the trigger gives the next free number
                    Row["share_slug"] = P["slugs"].get(R["id"])
                if Tab == "gallery_items":
                    Row["position"] = 0                      # renumbered below
                Cols = list(Row)
                Conn.execute(f"INSERT INTO {Tab} ({', '.join(Cols)}) VALUES ({', '.join('?' * len(Cols))})",
                             [Row[C] for C in Cols])
            elif R["id"] in Updates[Tab]:
                Ch = Updates[Tab][R["id"]]
                Conn.execute(f"UPDATE {Tab} SET {', '.join(f'{C} = ?' for C in Ch)} WHERE id = ?", [*Ch.values(), R["id"]])
            Conn.execute("INSERT INTO gallery_sync_items (kind, id, source, run_id, first_at, last_at) VALUES (?,?,?,?,?,?) "
                         "ON CONFLICT(kind, id) DO UPDATE SET source = excluded.source, run_id = excluded.run_id, "
                         "last_at = excluded.last_at", (Tab, R["id"], Source, RunId, T, T))
    Synced = [R["id"] for R in Rows["gallery_items"]]
    Others = [R["id"] for R in _All(Conn, "SELECT id FROM gallery_items ORDER BY position, created_at") if R["id"] not in set(Synced)]
    for N, Id in enumerate(Synced + Others, start=1):
        Conn.execute("UPDATE gallery_items SET position = ? WHERE id = ?", (N, Id))


def _Report(M: dict, P: dict, Conn=None) -> dict:
    Out = {"source": M["source"], "content_id": M["content_id"], "counts": M["counts"],
           "insert": {T: len(V) for T, V in P["insert"].items()}, "update": {T: len(V) for T, V in P["update"].items()},
           "same": P["same"], "unpublish": P["unpublish"], "replace_items": P["replace_items"], "conflicts": P["conflicts"],
           "files_write": len(P["files_write"]), "files_same": P["files_same"], "bytes_write": P["bytes_write"],
           "skipped_on_source": M.get("skipped", [])}
    if Conn is not None:                                     # after an import: the IDs and link names the items have here
        Out["items"] = []
        for I in M["items"]:
            D = _One(Conn, "SELECT * FROM designs WHERE id = ?", (I["design_id"],)) or {}
            Out["items"].append({**I, "ring_id_here": RingIds.Ref(D), "slug_here": D.get("share_slug")})
    return Out


def Import(BundleDir: Path, DataDir: Path, Apply: bool = False, KeepRemoved: bool = False, Ref: str | None = None,
           By: str = "gallery-sync") -> dict:
    """Plan (dry run) or apply a bundle on the target whose data directory is DataDir. Raises SyncError when the bundle
    is invalid or the target has rows in the way (nothing is written then)."""
    M, Rows = Load(BundleDir)
    DataDir = Path(DataDir)
    Assets = DataDir / "assets"
    if not (DataDir / "pipeline3.db").is_file():
        raise SyncError(f"No database in {DataDir}: is this the site's data directory?")
    Db = Database(DataDir / "pipeline3.db")                  # the target's own schema, up to date
    with Db.Connect() as Conn:
        P = _Plan(Conn, M, Rows, Assets, KeepRemoved)
    if P["conflicts"]:
        raise SyncError("; ".join(P["conflicts"][:5]) + (f" (and {len(P['conflicts']) - 5} more)" if len(P["conflicts"]) > 5 else ""))
    if not Apply:
        return {**_Report(M, P), "applied": False}
    Need = 2 * P["bytes_write"] + MinFreeBytes                # the files, a backup of what they replace, and room to spare
    Free = shutil.disk_usage(Assets if Assets.is_dir() else DataDir).free
    if P["bytes_write"] and Free < Need:
        raise SyncError(f"Not enough free disk: {_Mb(Free)} free, {_Mb(Need)} needed; nothing was written")
    RunId, Started = NewId("gsr"), Now()
    try:
        _WriteFiles(BundleDir, Assets, P["files_write"])
        with Db.Transaction() as Conn:
            Fresh = _Plan(Conn, M, Rows, None, KeepRemoved)   # the rows as they are now, inside the write lock
            if Fresh["conflicts"]:
                raise SyncError("; ".join(Fresh["conflicts"][:5]))
            Fresh.update({K: P[K] for K in ("files_write", "files_same", "bytes_write")})
            _Apply(Conn, M, Rows, Fresh, RunId, Started)
            Report = {**_Report(M, Fresh, Conn), "applied": True, "run_id": RunId}
            Conn.execute("INSERT INTO gallery_sync_runs (id, source, bundle_ref, content_id, exported_at, started_at, finished_at, "
                         "status, summary_json, error, by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (RunId, M["source"]["name"], Ref, M["content_id"], M["source"].get("exported_at"), Started, Now(),
                          "ok", json.dumps(_Summary(Report), sort_keys=True), None, By))
        return Report
    except Exception as E:
        try:                                                 # the record of the failure never hides the failure itself
            Db.Execute("INSERT INTO gallery_sync_runs (id, source, bundle_ref, content_id, exported_at, started_at, finished_at, "
                       "status, summary_json, error, by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (RunId, M["source"]["name"], Ref, M["content_id"], M["source"].get("exported_at"), Started, Now(),
                        "failed", "{}", f"{type(E).__name__}: {E}"[:500], By))
        except sqlite3.Error:
            pass
        raise


def _Summary(R: dict) -> dict:
    return {"items": len(R.get("items") or []), "insert": R["insert"], "update": R["update"], "unpublished": len(R["unpublish"]),
            "files_written": R["files_write"], "bytes_written": R["bytes_write"]}


def LastRun(Db) -> dict | None:
    """The latest import on this site, for the health answer: when, from where, which bundle, and how it went."""
    try:
        R = Db.One("SELECT source, bundle_ref, content_id, finished_at, status, summary_json, error FROM gallery_sync_runs "
                   "ORDER BY started_at DESC LIMIT 1")
    except sqlite3.Error:
        return None
    if R is None:
        return None
    Summary = json.loads(R["summary_json"] or "{}")
    Out = {"source": R["source"], "status": R["status"], "at": R["finished_at"], "items": Summary.get("items"),
           "bundle": (R["bundle_ref"] or R["content_id"] or "")[:12] or None}
    if R["error"]:
        Out["error"] = R["error"][:200]
    return Out


# ── verify a site over HTTP(S) ─────────────────────────────────────────────
def _Fetch(Url: str, Range: bool = False) -> tuple[int, str, bytes]:
    Req = urllib.request.Request(Url, headers={"User-Agent": UserAgent, **({"Range": "bytes=0-2047"} if Range else {})})
    try:
        with urllib.request.urlopen(Req, timeout=60) as R:
            return R.status, R.headers.get("Content-Type", ""), R.read(2048 if Range else 50_000_000)
    except urllib.error.HTTPError as E:
        return E.code, E.headers.get("Content-Type", "") if E.headers else "", b""


def Verify(Base: str, Expect: int | None = None, Out=sys.stdout) -> bool:
    """The public answers of a site after a sync: the gallery, the homepage story and every file the story plays,
    each tile's thumbnail, and the homepage itself."""
    Base = Base.rstrip("/")
    Origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(Base))
    Ok = True

    def Check(Label: str, Good: bool, Detail: str = "") -> None:
        nonlocal Ok
        Ok &= Good
        print(f"  {'ok  ' if Good else 'FAIL'} {Label}{(' — ' + Detail) if Detail else ''}", file=Out)

    def Media(Label: str, Path_: str | None, Kind: str) -> None:
        if not Path_:
            Check(Label, False, "no URL")
            return
        Code, Ctype, _ = _Fetch(Origin + Path_, Range=Kind == "video")
        Check(Label, Code in (200, 206) and Ctype.startswith(Kind), f"{Code} {Ctype}")

    print(f"Verifying {Base}", file=Out)
    Code, _, Body = _Fetch(Base + "/api/gallery")
    Items = json.loads(Body or b"{}").get("items", []) if Code == 200 else []
    Check("/api/gallery answers", Code == 200, f"{len(Items)} tiles")
    if Expect is not None:
        Check(f"/api/gallery shows {Expect} tiles", len(Items) == Expect, f"{len(Items)}")
    else:
        Check("/api/gallery has tiles", bool(Items))
    for I in Items:
        Media(f"tile {I.get('title')!r} thumbnail", (I.get("image_url") or "").replace("/assets/", "/thumb/", 1) + "?w=320", "image")
    Code, _, Home = _Fetch(Base + "/")
    Check("homepage answers with the hero", Code == 200 and b'data-testid="hero-showcase"' in Home, f"{Code}")
    Asked = re.search(rb"P3Showcase\.mount\(\$el,\s*\{\s*design:\s*'([a-z0-9-]+)'", Home)
    Slug = Asked.group(1).decode() if Asked else ""
    Code, _, Body = _Fetch(Base + "/api/showcase" + (f"?design={Slug}" if Slug else ""))
    Story = (json.loads(Body or b"{}").get("story") if Code == 200 else None)
    Check("/api/showcase tells the homepage story", Code == 200 and bool(Story), (Story or {}).get("title") or "no story")
    if Story and Slug:
        print(f"  note the homepage asks for {Slug!r}: " + ("shown" if Story.get("slug") == Slug
                                                          else f"not available here, it shows {Story.get('slug')!r}"), file=Out)
    if Story:
        for N, U in enumerate(Story.get("options") or []):
            Media(f"story option {N + 1}", U, "image")
        if Story.get("refine"):
            Media("story refinement", Story["refine"].get("image_url"), "image")
        Media("story movie", Story.get("movie_url"), "video")
        Media("story poster", Story.get("poster_url"), "image")
        Media("story clip", Story.get("clip_url"), "video")
        Media("story still", Story.get("still_url"), "image")
    print("All checks passed." if Ok else "Some checks FAILED.", file=Out)
    return Ok


# ── command line ───────────────────────────────────────────────────────────
def _DataDir(Arg: str | None) -> Path:
    if Arg:
        return Path(Arg)
    from p3.settings import LoadSettings
    return LoadSettings().DataDir


def _Print(Report: dict, Out=sys.stdout) -> None:
    S, C = Report["source"], Report["counts"]
    print(f"Bundle from {S['name']} (exported {S.get('exported_at')}, code {str(S.get('commit') or '?')[:7]}): "
          f"{C['gallery_items']} gallery items, {C['designs']} designs, {C['batches']} batches, {C['candidates']} images, "
          f"{C['movies']} movies, {C['files']} files ({_Mb(C['bytes'])})", file=Out)
    for T in Order:
        print(f"  {T:<14} new {Report['insert'][T]:>3} · changed {Report['update'][T]:>3} · unchanged {Report['same'][T]:>3}", file=Out)
    print(f"  files          to write {Report['files_write']} ({_Mb(Report['bytes_write'])}) · unchanged {Report['files_same']}", file=Out)
    if Report["unpublish"]:
        print(f"  taken off the gallery here (no longer published on {S['name']}): {', '.join(Report['unpublish'])}", file=Out)
    for X in Report.get("skipped_on_source") or []:
        print(f"  left behind on {S['name']}: {X.get('title') or X.get('gallery_item_id') or X.get('candidate_id')} — {X['why']}", file=Out)
    for I in Report.get("items") or []:
        Moved = "" if I.get("ring_id_here") == I.get("ring_id") else f" (was {I.get('ring_id')} on {S['name']})"
        print(f"  {I.get('ring_id_here') or '—':<8} {I['title']}{Moved} · /design/{I.get('slug_here') or ''}", file=Out)
    print("Imported." if Report["applied"] else "Plan only: nothing written yet (--apply imports it).", file=Out)


def Main(Argv=None) -> int:
    Ap = argparse.ArgumentParser(prog="python -m p3.gallerysync", description=__doc__.split("\n\n")[0])
    Sub = Ap.add_subparsers(dest="cmd", required=True)
    E = Sub.add_parser("export", help="write the source's approved gallery as a bundle directory")
    E.add_argument("out"); E.add_argument("--data-dir"); E.add_argument("--source", default="proto"); E.add_argument("--commit")
    C = Sub.add_parser("check", help="check a bundle without a target")
    C.add_argument("bundle")
    I = Sub.add_parser("import", help="plan (default) or apply a bundle on this site")
    I.add_argument("bundle"); I.add_argument("--data-dir"); I.add_argument("--apply", action="store_true")
    I.add_argument("--keep-removed", action="store_true", help="keep synced tiles the source no longer publishes")
    I.add_argument("--ref", help="the git commit the bundle came from (recorded)"); I.add_argument("--json", action="store_true")
    St = Sub.add_parser("status", help="the latest imports on this site")
    St.add_argument("--data-dir")
    V = Sub.add_parser("verify", help="check a site's gallery, homepage story and its files over HTTP(S)")
    V.add_argument("base_url"); V.add_argument("--expect", type=int)
    A = Ap.parse_args(Argv)
    try:
        if A.cmd == "export":
            M = Export(_DataDir(A.data_dir), Path(A.out), A.source, A.commit)
            print(f"Exported {M['counts']['gallery_items']} gallery items ({M['counts']['designs']} designs, {M['counts']['candidates']} "
                  f"images, {M['counts']['movies']} movies, {M['counts']['files']} files, {_Mb(M['counts']['bytes'])}) to {A.out}; "
                  f"content {M['content_id'][:12]}")
            for X in M["skipped"]:
                print(f"  left behind: {X.get('title') or X.get('gallery_item_id') or X.get('candidate_id')} — {X['why']}")
        elif A.cmd == "check":
            M, _ = Load(Path(A.bundle))
            print(f"Valid bundle from {M['source']['name']}: {M['counts']['gallery_items']} gallery items, {M['counts']['files']} files "
                  f"({_Mb(M['counts']['bytes'])}); content {M['content_id'][:12]}")
        elif A.cmd == "import":
            R = Import(Path(A.bundle), _DataDir(A.data_dir), Apply=A.apply, KeepRemoved=A.keep_removed, Ref=A.ref)
            print(json.dumps(R, indent=1, sort_keys=True)) if A.json else _Print(R)
        elif A.cmd == "status":
            Db = Database(_DataDir(A.data_dir) / "pipeline3.db")
            for R in Db.All("SELECT * FROM gallery_sync_runs ORDER BY started_at DESC LIMIT 10"):
                print(f"{R['finished_at']} {R['status']:<6} from {R['source']} bundle {(R['bundle_ref'] or R['content_id'] or '')[:12]} "
                      f"{R['summary_json'] if R['status'] == 'ok' else R['error']}")
        elif A.cmd == "verify":
            return 0 if Verify(A.base_url, A.expect) else 1
        return 0
    except SyncError as E:
        print(f"gallery sync: {E}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(Main())
