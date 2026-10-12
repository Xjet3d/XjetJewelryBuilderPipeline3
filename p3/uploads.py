"""Large files sent to the Admin in pieces — an STL made outside Atelier (docs/EXTERNAL-PARTS.md).

  POST   /api/admin/uploads                 {filename, bytes}      → the upload (its id, the piece size)
  PUT    /api/admin/uploads/{id}?offset=N   the next piece (raw)   → how much has arrived
  POST   /api/admin/uploads/{id}/complete                          → checked (an STL) and hashed
  DELETE /api/admin/uploads/{id}                                   → cancelled, the partial file deleted

Each piece (at most ChunkBytes) is written straight to disk at its offset, so a 300 MB file is never held in memory and
every request stays under the reverse proxies' body limit (20 MB). A finished upload is kept until it is used: using it
MOVES the file into place (no copy, the same disk). An upload nobody used is deleted after MaxAgeS.
"""

import hashlib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from p3.context import Context, HttpError
from p3.db import NewId, Now

Logger = logging.getLogger("p3.uploads")
ChunkBytes = 8 * 1024 * 1024
MaxAgeS = 24 * 3600
DefaultMaxMb = 600
Extensions = (".stl",)


def MaxBytes() -> int:
    """P3_MAX_UPLOAD_MB (default 600 MB): the largest file the Admin accepts."""
    try:
        return int(float(os.environ.get("P3_MAX_UPLOAD_MB", DefaultMaxMb)) * 1024 * 1024)
    except ValueError:
        return DefaultMaxMb * 1024 * 1024


def InspectStl(PathObj: Path) -> tuple[str, int | None]:
    """('binary', faces) or ('ascii', None) — read from the head of the file only. Anything else is refused."""
    Size = PathObj.stat().st_size
    with open(PathObj, "rb") as F:
        Head = F.read(65536)
    if Size >= 84:
        Count = int.from_bytes(Head[80:84], "little")
        if Count and Size == 84 + 50 * Count:              # binary (its 80-byte header may itself start with "solid")
            return "binary", Count
    Text = Head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if Text[:5].lower() == b"solid" and b"facet" in Text and b"vertex" in Text:
        return "ascii", None
    raise HttpError(422, "not_an_stl", "This file is not an STL (binary or ASCII). Export the part as STL and try again.")


def _Sha256(PathObj: Path) -> str:
    H = hashlib.sha256()
    with open(PathObj, "rb") as F:
        for Block in iter(lambda: F.read(1 << 20), b""):
            H.update(Block)
    return H.hexdigest()


class UploadService:
    def __init__(self, Ctx: Context):
        self.Ctx = Ctx

    def _Dir(self) -> Path:
        return self.Ctx.Settings.DevDir / "uploads"

    def _Require(self, UploadId: str) -> dict:
        U = self.Ctx.Db.One("SELECT * FROM uploads WHERE id = ?", (UploadId,))
        if U is None:
            raise HttpError(404, "upload_not_found", "Upload not found.")
        return U

    def ToJson(self, U: dict) -> dict:
        return {"id": U["id"], "filename": U["filename"], "bytes": U["bytes"], "received": U["received"], "status": U["status"],
                "sha256": U["sha256"], "format": U["format"], "faces": U["faces"], "used_by": U["used_by"],
                "created_at": U["created_at"], "chunk_bytes": ChunkBytes}

    def Create(self, Filename: str, Bytes, By: str) -> dict:
        self.Cleanup()
        Name = re.sub(r"[\x00-\x1f/\\]+", "_", str(Filename or "").strip())[-200:]
        if not Name.lower().endswith(Extensions):
            raise HttpError(400, "unsupported_file", "Upload an STL file (.stl).")
        try:
            Size = int(Bytes)
        except (TypeError, ValueError):
            raise HttpError(400, "invalid_size", "The file size is missing.")
        if Size < 84:
            raise HttpError(400, "invalid_size", "This file is too small to be an STL.")
        if Size > MaxBytes():
            raise HttpError(413, "file_too_large", f"The file is {Size / 1e6:.0f} MB; the largest accepted is {MaxBytes() / 1e6:.0f} MB.")
        Uid, T = NewId("upl"), Now()
        Rel = f"uploads/{Uid}.part"
        self._Dir().mkdir(parents=True, exist_ok=True)
        (self.Ctx.Settings.DevDir / Rel).write_bytes(b"")
        self.Ctx.Db.Execute("INSERT INTO uploads (id, filename, bytes, received, path, status, created_by, created_at, updated_at) "
                            "VALUES (?,?,?,0,?, 'receiving', ?,?,?)", (Uid, Name, Size, Rel, By, T, T))
        return self.ToJson(self._Require(Uid))

    async def Append(self, UploadId: str, Offset: int, Chunks) -> dict:
        """One piece at its offset (pieces arrive in order: offset = what has arrived). Anything past the offset — a piece
        that broke off — is dropped first, so a piece can simply be sent again."""
        async with self.Ctx.Lock(f"upload:{UploadId}"):
            U = self._Require(UploadId)
            if U["status"] != "receiving":
                raise HttpError(409, "upload_closed", "This upload is finished or cancelled.")
            if Offset != U["received"]:
                raise HttpError(409, "offset_mismatch", f"Expected the piece at {U['received']}.", {"received": U["received"]})
            P = self.Ctx.Settings.DevDir / U["path"]
            Written = 0
            with open(P, "r+b") as F:
                F.seek(Offset)
                F.truncate()
                async for Piece in Chunks:
                    Written += len(Piece)
                    if Written > ChunkBytes or Offset + Written > U["bytes"]:
                        F.truncate(Offset)
                        raise HttpError(413, "piece_too_large", "A piece is larger than announced.")
                    F.write(Piece)
            self.Ctx.Db.Execute("UPDATE uploads SET received = ?, updated_at = ? WHERE id = ?", (Offset + Written, Now(), UploadId))
            return self.ToJson(self._Require(UploadId))

    def Complete(self, UploadId: str) -> dict:
        """Every byte has arrived: check that it is an STL and hash it (blocking — the route runs it off the event loop)."""
        U = self._Require(UploadId)
        if U["status"] == "complete":
            return self.ToJson(U)
        if U["status"] != "receiving":
            raise HttpError(409, "upload_closed", "This upload is finished or cancelled.")
        P = self.Ctx.Settings.DevDir / U["path"]
        if U["received"] != U["bytes"] or P.stat().st_size != U["bytes"]:
            raise HttpError(409, "upload_incomplete", f"{U['received']} of {U['bytes']} bytes have arrived.", {"received": U["received"]})
        try:
            Format, Faces = InspectStl(P)
        except HttpError:
            self.Cancel(UploadId)
            raise
        self.Ctx.Db.Execute("UPDATE uploads SET status = 'complete', sha256 = ?, format = ?, faces = ?, updated_at = ? WHERE id = ?",
                            (_Sha256(P), Format, Faces, Now(), UploadId))
        return self.ToJson(self._Require(UploadId))

    def Ready(self, UploadId: str) -> dict:
        U = self._Require(UploadId)
        if U["status"] != "complete":
            raise HttpError(409, "upload_not_complete", "The file has not finished uploading." if U["status"] == "receiving"
                            else "This upload was already used or has expired — upload the file again.")
        return U

    def Take(self, UploadId: str, RelTarget: str, UsedBy: str) -> dict:
        """Move a finished upload to its place under the dev dir (a rename on the same disk, never a copy)."""
        U = self.Ready(UploadId)
        Target = self.Ctx.Settings.DevDir / RelTarget
        Target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.Ctx.Settings.DevDir / U["path"], Target)
        self.Ctx.Db.Execute("UPDATE uploads SET status = 'used', path = ?, used_by = ?, updated_at = ? WHERE id = ?",
                            (RelTarget, UsedBy, Now(), UploadId))
        return U

    def Untake(self, UploadId: str, RelTarget: str) -> None:
        """Undo Take when what it was for failed: the file goes back and the upload can be used again."""
        U = self._Require(UploadId)
        Back = f"uploads/{UploadId}.part"
        try:
            os.replace(self.Ctx.Settings.DevDir / RelTarget, self.Ctx.Settings.DevDir / Back)
        except OSError:
            Logger.exception("Could not move upload %s back", UploadId)
            return
        self.Ctx.Db.Execute("UPDATE uploads SET status = 'complete', path = ?, used_by = NULL, updated_at = ? WHERE id = ?",
                            (Back, Now(), U["id"]))

    def Cancel(self, UploadId: str) -> dict:
        U = self._Require(UploadId)
        if U["status"] in ("receiving", "complete"):
            (self.Ctx.Settings.DevDir / U["path"]).unlink(missing_ok=True)
            self.Ctx.Db.Execute("UPDATE uploads SET status = 'cancelled', updated_at = ? WHERE id = ?", (Now(), UploadId))
        return self.ToJson(self._Require(UploadId))

    def Cleanup(self) -> int:
        """Delete the files of uploads nobody used within MaxAgeS (the rows stay, as expired)."""
        Before = (datetime.now(timezone.utc) - timedelta(seconds=MaxAgeS)).isoformat(timespec="milliseconds")
        N = 0
        for U in self.Ctx.Db.All("SELECT * FROM uploads WHERE status IN ('receiving', 'complete') AND updated_at < ?", (Before,)):
            (self.Ctx.Settings.DevDir / U["path"]).unlink(missing_ok=True)
            self.Ctx.Db.Execute("UPDATE uploads SET status = 'expired', updated_at = ? WHERE id = ?", (Now(), U["id"]))
            N += 1
        return N
