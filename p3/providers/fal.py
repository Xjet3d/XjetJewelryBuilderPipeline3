"""fal.ai queue adapter (live provider)."""

import httpx
import fal_client

from p3.providers.base import ProviderError, ProviderStatus, TransientProviderError, ClassifyErrorMessage

_TransientHttp = {408, 425, 429, 500, 502, 503, 504}
MaxDownloadBytes = 512 * 1024 * 1024
MaxErrorChars = 400                      # what a customer or the Admin sees of a provider error: a sentence, never a dump
NoMediaMessage = ("The AI model returned no image for this request. This happens now and then, with nothing wrong in "
                  "your description — you can retry it.")


def _Detail(E) -> tuple[str, str | None]:
    """fal.ai's error body as (text, error type). A validation-style body is a `detail` list of
    {loc, msg, type, url, input}: only the messages and the type are used — never `input`, which echoes the whole
    request (prompt, system prompt and all; a 13,000-character error once reached a customer's screen)."""
    M = getattr(E, "message", E)
    Items = [X for X in M if isinstance(X, dict)] if isinstance(M, (list, tuple)) else [M] if isinstance(M, dict) else []
    if Items:
        Text = "; ".join(str(X.get("msg") or X.get("message") or "") for X in Items if X.get("msg") or X.get("message"))
        Kind = next((str(X["type"]) for X in Items if X.get("type")), None)
        return Text or f"HTTP {getattr(E, 'status_code', '')}".strip(), Kind
    return str(M), getattr(E, "error_type", None)


def _Wrap(E: Exception) -> Exception:
    if isinstance(E, (httpx.TransportError, fal_client.FalClientTimeoutError)):
        return TransientProviderError(str(E))
    if isinstance(E, fal_client.FalClientHTTPError):
        Text, Kind = _Detail(E)
        if Kind == "no_media_generated" or "no_media_generated" in Text:
            # The model finished without an image (it answered with text, or declined this one attempt). Not a fault
            # of the input: only this slot is unavailable, and the app never re-requests it — the customer's "Generate
            # another option" is the only second request (p3/images.py).
            return ProviderError(NoMediaMessage, "no_media_generated")
        if Kind == "downstream_service_error" or "downstream_service_error" in Text or "Downstream service error" in Text:
            # fal.ai reports that the model's own (partner) service failed on this request. It is a
            # failed generation, not a connection problem, so it is not retried as transient.
            return ProviderError("The AI service failed to generate this result (fal.ai: downstream service error). "
                                 "You can retry; if it keeps failing, check the model settings.", "provider_failed")
        if E.status_code in _TransientHttp:
            return TransientProviderError(f"HTTP {E.status_code}: {Text[:MaxErrorChars]}")
        Message, Code = ClassifyErrorMessage(f"HTTP {E.status_code}: {Text}")
        return ProviderError(Message[:MaxErrorChars], Code)
    Message, Code = ClassifyErrorMessage(str(E))
    return ProviderError(Message[:MaxErrorChars], Code)


class FalProvider:
    Name = "fal"

    def __init__(self, Key: str):
        self.Client = fal_client.AsyncClient(key=Key)

    def Owns(self, RequestId: str) -> bool:
        return not RequestId.startswith("mockreq_")

    async def Upload(self, Data: bytes, ContentType: str) -> str:
        try:
            return await self.Client.upload(Data, ContentType)
        except Exception as E:
            raise _Wrap(E) from E

    async def Submit(self, Endpoint: str, Arguments: dict) -> str:
        try:
            Handle = await self.Client.submit(Endpoint, Arguments)
            return Handle.request_id
        except Exception as E:
            raise _Wrap(E) from E

    async def Status(self, Endpoint: str, RequestId: str) -> ProviderStatus:
        try:
            S = await self.Client.status(Endpoint, RequestId)
        except Exception as E:
            raise _Wrap(E) from E
        if isinstance(S, fal_client.Queued):
            return ProviderStatus("queued", Position=getattr(S, "position", None))
        if isinstance(S, fal_client.InProgress):
            return ProviderStatus("running")
        if isinstance(S, fal_client.Completed):
            return ProviderStatus("completed", getattr(S, "error", None))
        return ProviderStatus("running")

    async def Result(self, Endpoint: str, RequestId: str) -> dict:
        try:
            return await self.Client.result(Endpoint, RequestId)
        except Exception as E:
            raise _Wrap(E) from E

    async def DownloadTo(self, Url: str, Target, OnProgress=None) -> tuple[int, str]:
        """Stream a (possibly 250 MB) artifact straight to disk, hashing as it arrives. Never holds the
        file in memory. Returns (bytes, sha256). Writes Target + ".part" and renames when complete."""
        import hashlib
        from pathlib import Path
        Target = Path(Target)
        Target.parent.mkdir(parents=True, exist_ok=True)
        Part = Target.with_name(Target.name + ".part")
        Hash, Size = hashlib.sha256(), 0
        try:
            async with httpx.AsyncClient(timeout=600.0, follow_redirects=True) as Client:
                async with Client.stream("GET", Url) as Resp:
                    if Resp.status_code in _TransientHttp:
                        raise TransientProviderError(f"Download HTTP {Resp.status_code}")
                    if Resp.status_code != 200:
                        raise ProviderError(f"Download failed: HTTP {Resp.status_code}", "download_failed")
                    Total = int(Resp.headers.get("content-length") or 0) or None
                    with open(Part, "wb") as F:
                        async for Chunk in Resp.aiter_bytes(1 << 20):
                            Size += len(Chunk)
                            if Size > MaxDownloadBytes:
                                raise ProviderError("Downloaded artifact is too large", "download_failed")
                            F.write(Chunk)
                            Hash.update(Chunk)
                            if OnProgress:
                                OnProgress(Size, Total)
            Part.replace(Target)
            return Size, Hash.hexdigest()
        except (httpx.TransportError, httpx.TimeoutException) as E:
            raise TransientProviderError(str(E)) from E
        finally:
            if Part.exists():
                Part.unlink(missing_ok=True)

    async def Download(self, Url: str) -> bytes:
        try:
            async with httpx.AsyncClient(timeout=600.0, follow_redirects=True) as Client:
                async with Client.stream("GET", Url) as Resp:
                    if Resp.status_code in _TransientHttp:
                        raise TransientProviderError(f"Download HTTP {Resp.status_code}")
                    if Resp.status_code != 200:
                        raise ProviderError(f"Download failed: HTTP {Resp.status_code}", "download_failed")
                    Chunks, Size = [], 0
                    async for Chunk in Resp.aiter_bytes(65536):
                        Size += len(Chunk)
                        if Size > MaxDownloadBytes:
                            raise ProviderError("Downloaded artifact is too large", "download_failed")
                        Chunks.append(Chunk)
                    return b"".join(Chunks)
        except httpx.TransportError as E:
            raise TransientProviderError(str(E)) from E
