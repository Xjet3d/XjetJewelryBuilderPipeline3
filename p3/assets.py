"""Artifact storage: containment-checked paths, atomic writes, content validation.

Stored paths are always relative to a root (assets or dev) and are resolved and
checked for containment before use — client-supplied paths are never trusted.
"""

import hashlib
import io
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

MaxReferenceBytes = 10 * 1024 * 1024


class AssetError(ValueError):
    pass


def Resolve(Root: Path, RelPath: str) -> Path:
    """Resolve RelPath under Root, refusing anything that escapes it."""
    RootResolved = Path(Root).resolve()
    Candidate = (RootResolved / RelPath).resolve()
    if RootResolved != Candidate and RootResolved not in Candidate.parents:
        raise AssetError(f"Path escapes asset root: {RelPath!r}")
    return Candidate


def WriteAtomic(Root: Path, RelPath: str, Data: bytes) -> Path:
    """Write via temp file + rename so a reader never sees a partial artifact."""
    Target = Resolve(Root, RelPath)
    Target.parent.mkdir(parents=True, exist_ok=True)
    Fd, TmpName = tempfile.mkstemp(dir=Target.parent, prefix=".tmp-", suffix=Target.suffix)
    try:
        with os.fdopen(Fd, "wb") as Fh:
            Fh.write(Data)
        os.replace(TmpName, Target)
    except BaseException:
        if os.path.exists(TmpName):
            os.unlink(TmpName)
        raise
    return Target


def Sha256(Data: bytes) -> str:
    return hashlib.sha256(Data).hexdigest()


def ImageContentType(RelPath: str) -> str:
    """MIME type for a stored image, from the extension ValidateImage chose when it was written."""
    Ext = RelPath.rsplit(".", 1)[-1].lower()
    return {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "webp": "image/webp"}.get(Ext, "image/png")


def ValidateImage(Data: bytes) -> str:
    """Return the image extension ('png'|'jpeg'|'webp') or raise AssetError."""
    if not Data:
        raise AssetError("Empty image")
    try:
        with Image.open(io.BytesIO(Data)) as Img:
            Img.verify()
            Fmt = (Img.format or "").lower()
    except Exception as E:
        raise AssetError(f"Not a valid image: {E}") from E
    if Fmt not in ("png", "jpeg", "webp"):
        raise AssetError(f"Unsupported image format: {Fmt or 'unknown'}")
    return Fmt


def NormalizeReferenceImage(Data: bytes) -> bytes:
    """Validate a customer upload and re-encode it as PNG (drops metadata/odd modes)."""
    if len(Data) > MaxReferenceBytes:
        raise AssetError("Reference image is larger than 10 MB")
    ValidateImage(Data)
    with Image.open(io.BytesIO(Data)) as Img:
        Img = ImageOps.exif_transpose(Img) or Img            # a phone photo: the EXIF orientation applied, then dropped
        Img = Img.convert("RGBA") if Img.mode in ("RGBA", "LA", "P") else Img.convert("RGB")
        Buf = io.BytesIO()
        Img.save(Buf, format="PNG")
        return Buf.getvalue()


def ValidateMp4(Data: bytes) -> None:
    # ISO base media: bytes 4..8 are the 'ftyp' box type.
    if len(Data) < 12 or Data[4:8] != b"ftyp":
        raise AssetError("Downloaded movie is not an MP4 file")


def ValidateMeshFile(PathObj: Path, Format: str) -> None:
    """Validate a downloaded mesh on disk without reading it all (binary STL: size must match)."""
    Size = Path(PathObj).stat().st_size
    with open(PathObj, "rb") as F:
        Head = F.read(84)
    if Size < 84:
        raise AssetError("Downloaded mesh is empty or truncated")
    if Format == "glb" and Head[:4] != b"glTF":
        raise AssetError("Downloaded mesh is not a GLB file")
    if Format == "stl" and not Head[:5].lower().startswith(b"solid"):
        Count = int.from_bytes(Head[80:84], "little")
        if Count == 0 or Size < 84 + 50 * Count:
            raise AssetError("Downloaded STL is not a valid binary STL")


def ValidateMesh(Data: bytes, Format: str) -> None:
    if len(Data) < 84:
        raise AssetError("Downloaded mesh is empty or truncated")
    if Format == "glb" and Data[:4] != b"glTF":
        raise AssetError("Downloaded mesh is not a GLB file")
    if Format == "stl" and not Data[:5].lower().startswith(b"solid"):
        # Binary STL: 80-byte header, uint32 triangle count, 50 bytes per triangle.
        Count = int.from_bytes(Data[80:84], "little")
        if Count == 0 or len(Data) < 84 + 50 * Count:
            raise AssetError("Downloaded STL is not a valid binary STL")
