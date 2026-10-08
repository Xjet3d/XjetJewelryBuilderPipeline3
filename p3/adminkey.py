"""The Admin key as the Admin can change it (Settings → Keys).

The Admin key is what signs people in to the Admin (`P3_ADMIN_KEY`). A key set here is saved in
`<data dir>/admin_key.secret` (mode 0600, never returned by any API) and replaces `P3_ADMIN_KEY` from the environment;
removing it returns to the environment's key. In production the key belongs to the server configuration
(`/etc/xjet-atelier/env`, 24+ random characters): the Admin shows where it comes from but does not change it, and a
saved file is ignored there. Changing the key signs every other browser out.
"""

from pathlib import Path

from p3.context import HttpError
from p3.falkey import ReadSecretFile, WriteSecretFile

MinChars, MaxChars = 12, 200


def SecretPath(DataDir: Path) -> Path:
    return DataDir / "admin_key.secret"


def ReadSaved(DataDir: Path) -> str | None:
    return ReadSecretFile(SecretPath(DataDir))


def WriteSaved(DataDir: Path, Key: str) -> None:
    WriteSecretFile(SecretPath(DataDir), Key)


def RemoveSaved(DataDir: Path) -> None:
    try:
        SecretPath(DataDir).unlink()
    except FileNotFoundError:
        pass


def Normalize(Raw, Current: str | None) -> str:
    Key = str(Raw or "").strip()
    if not (MinChars <= len(Key) <= MaxChars) or any(C.isspace() for C in Key):
        raise HttpError(400, "invalid_admin_key", f"The new Admin key is {MinChars}-{MaxChars} characters with no spaces.")
    if Key == Current:
        raise HttpError(400, "admin_key_unchanged", "The new Admin key is the same as the current one.")
    return Key
