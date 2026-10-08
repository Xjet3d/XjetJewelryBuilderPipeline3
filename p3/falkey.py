"""The fal.ai API key as the Admin can set it (Settings → System).

Where the key comes from, in order: the key saved here (`<data dir>/fal_key.secret`, mode 0600, never returned by
any API) → `FAL_KEY` in the server environment. In production the key belongs to the server configuration
(`/etc/xjet-atelier/env`): the Admin shows where it comes from but neither changes nor removes it, and a saved file
is ignored there, like the AI mode switch.
"""

import io
import os
from pathlib import Path

from p3.context import HttpError

MinChars, MaxChars = 8, 200


def SecretPath(DataDir: Path) -> Path:
    return DataDir / "fal_key.secret"


def ReadSaved(DataDir: Path) -> str | None:
    try:
        Key = SecretPath(DataDir).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Key or None


def WriteSaved(DataDir: Path, Key: str) -> None:
    Path_ = SecretPath(DataDir)
    Path_.parent.mkdir(parents=True, exist_ok=True)
    Tmp = Path_.with_name(Path_.name + ".tmp")
    Fd = os.open(Tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(Fd, "w", encoding="utf-8") as F:
        F.write(Key + "\n")
    os.replace(Tmp, Path_)


def RemoveSaved(DataDir: Path) -> None:
    try:
        SecretPath(DataDir).unlink()
    except FileNotFoundError:
        pass


def Last4(Key: str | None) -> str:
    return Key[-4:] if Key and len(Key) >= 12 else ""


def Normalize(Raw) -> str:
    Key = str(Raw or "").strip()
    if not (MinChars <= len(Key) <= MaxChars) or any(C.isspace() for C in Key):
        raise HttpError(400, "invalid_fal_key", f"A fal.ai key is {MinChars}-{MaxChars} characters with no spaces.")
    return Key


async def CheckKey(Key: str) -> None:
    """A free storage upload of a 1x1 PNG: fal.ai rejects a bad key here, and no model runs (no cost)."""
    from PIL import Image
    from p3.providers.fal import FalProvider
    Buf = io.BytesIO()
    Image.new("RGB", (1, 1), (255, 255, 255)).save(Buf, format="PNG")
    try:
        await FalProvider(Key).Upload(Buf.getvalue(), "image/png")
    except Exception as E:
        raise HttpError(400, "fal_key_rejected", "fal.ai did not accept this key (a test upload failed). "
                                                 "Nothing was saved.") from E
