"""In-process mock provider for tests and local UI work. Makes no network calls.

Outcomes can be scripted per endpoint (FIFO) to exercise failure paths:
  "ok"         — normal result
  "fail"       — request completes with a provider error
  "duplicate"  — image result byte-identical to every other "duplicate" result
  "submit_transient" — Submit raises TransientProviderError once
  "status_transient:N" — the first N status reads raise TransientProviderError
  "hang"       — never completes (for timeout tests)
Every submitted request is recorded in .Submissions so tests can assert exactly
which (paid, in live mode) calls would have been made.
"""

import asyncio
import hashlib
import io
import shutil
import subprocess
import tempfile
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

from PIL import Image, ImageDraw

from p3.providers import endpoints
from p3.providers.base import ProviderError, ProviderStatus, TransientProviderError


def _RingImage(Seed: int, Label: str, Size: int = 512) -> bytes:
    H = hashlib.sha256(f"{Seed}:{Label}".encode()).digest()
    Img = Image.new("RGB", (Size, Size), (255, 255, 255))
    D = ImageDraw.Draw(Img)
    Metal = (150 + H[0] % 100, 130 + H[1] % 110, 90 + H[2] % 120)
    Cx, Cy = Size // 2, Size // 2 + 20
    Outer = 150 + H[3] % 40
    Band = 22 + H[4] % 26
    D.ellipse([Cx - Outer, Cy - Outer, Cx + Outer, Cy + Outer], fill=Metal)
    D.ellipse([Cx - Outer + Band, Cy - Outer + Band, Cx + Outer - Band, Cy + Outer - Band], fill=(255, 255, 255))
    Top = 30 + H[5] % 30
    Shape = H[6] % 3
    Box = [Cx - Top, Cy - Outer - Top, Cx + Top, Cy - Outer + Top]
    if Shape == 0:
        D.ellipse(Box, fill=tuple(max(0, C - 40) for C in Metal))
    elif Shape == 1:
        D.rectangle(Box, fill=tuple(max(0, C - 40) for C in Metal))
    else:
        D.polygon([(Cx, Box[1]), (Box[2], Cy - Outer), (Cx, Box[3]), (Box[0], Cy - Outer)],
                  fill=tuple(max(0, C - 40) for C in Metal))
    D.text((12, Size - 24), f"MOCK seed {Seed}", fill=(170, 170, 170))
    Buf = io.BytesIO()
    Img.save(Buf, format="PNG")
    return Buf.getvalue()


def _CharmImage(Seed: int, Label: str, Size: int = 512) -> bytes:
    """A placeholder charm: a body (disc, drop, heart-like or shield) under one plain round loop at the top center."""
    H = hashlib.sha256(f"charm:{Seed}:{Label}".encode()).digest()
    Img = Image.new("RGB", (Size, Size), (255, 255, 255))
    D = ImageDraw.Draw(Img)
    Metal = (150 + H[0] % 100, 130 + H[1] % 110, 90 + H[2] % 120)
    Dark = tuple(max(0, C - 45) for C in Metal)
    Cx, Top = Size // 2, 96
    R = 26                                                     # the loop: always the same plain ring
    D.ellipse([Cx - R, Top - R, Cx + R, Top + R], outline=Metal, width=11)
    W, Hh = 112 + H[3] % 40, 150 + H[4] % 50                    # the body below it
    Y0 = Top + R - 4
    Shape = H[5] % 4
    if Shape == 0:
        D.ellipse([Cx - W, Y0, Cx + W, Y0 + 2 * W], fill=Metal)
    elif Shape == 1:
        D.polygon([(Cx, Y0), (Cx + W, Y0 + Hh * 0.55), (Cx, Y0 + Hh + 60), (Cx - W, Y0 + Hh * 0.55)], fill=Metal)
    elif Shape == 2:
        D.ellipse([Cx - W, Y0, Cx, Y0 + W], fill=Metal)
        D.ellipse([Cx, Y0, Cx + W, Y0 + W], fill=Metal)
        D.polygon([(Cx - W, Y0 + W // 2), (Cx + W, Y0 + W // 2), (Cx, Y0 + Hh + 40)], fill=Metal)
    else:
        D.polygon([(Cx - W, Y0), (Cx + W, Y0), (Cx + W, Y0 + Hh * 0.6), (Cx, Y0 + Hh + 30), (Cx - W, Y0 + Hh * 0.6)], fill=Metal)
    D.ellipse([Cx - 22, Y0 + 70, Cx + 22, Y0 + 114], fill=Dark)   # a relief, so the four options differ
    D.text((12, Size - 24), f"MOCK charm seed {Seed}", fill=(170, 170, 170))
    Buf = io.BytesIO()
    Img.save(Buf, format="PNG")
    return Buf.getvalue()


def _IsCharmRequest(Args: dict) -> bool:
    """A charm configuration says so in its system prompt (the ring prompt never calls the piece a jewelry charm)."""
    return "jewelry charm" in str(Args.get("system_prompt") or "").lower()


_DuplicateImage = None


_BundledMovie = Path(__file__).with_name("mock_assets") / "mock_movie.mp4"


def _FakeMp4() -> bytes:
    return b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64


def _RenderMp4(ImageBytes: bytes) -> bytes | None:
    """Render a short slow-pan clip from the input image with ffmpeg, when available."""
    Ffmpeg = shutil.which("ffmpeg")
    if not Ffmpeg:
        return None
    with tempfile.TemporaryDirectory() as Tmp:
        Src = Path(Tmp) / "in.png"; Out = Path(Tmp) / "out.mp4"
        Src.write_bytes(ImageBytes)
        Cmd = [Ffmpeg, "-y", "-loop", "1", "-i", str(Src), "-t", "4", "-r", "24",
               "-vf", "scale=512:512,zoompan=z='1+0.1*sin(2*PI*on/96)':d=1:s=512x512,format=yuv420p",
               "-c:v", "libx264", "-an", str(Out)]
        R = subprocess.run(Cmd, capture_output=True, timeout=60)
        return Out.read_bytes() if R.returncode == 0 and Out.exists() else None


def _MeshBytes(Format: str) -> bytes:
    import trimesh
    Mesh = trimesh.creation.torus(major_radius=9.0, minor_radius=1.5)
    return Mesh.export(file_type=Format)


class MockProvider:
    Name = "mock"

    def __init__(self, LatencyS: float = 0.0, RenderVideo: bool = True):
        self.LatencyS = LatencyS
        self.RenderVideo = RenderVideo
        self.Scripts = defaultdict(deque)       # endpoint -> deque of outcomes
        self.Submissions = []                   # (endpoint, arguments, request_id)
        self.Uploads = {}                       # url -> bytes
        self.Files = {}                         # url -> bytes (results)
        self.Requests = {}                      # request_id -> dict
        self.FailUploads = False

    # ── scripting helpers ────────────────────────────────────────────────
    def Script(self, Endpoint: str, *Outcomes: str) -> None:
        self.Scripts[Endpoint].extend(Outcomes)

    def SubmissionsFor(self, Endpoint: str) -> list:
        return [S for S in self.Submissions if S[0] == Endpoint]

    # ── provider interface ───────────────────────────────────────────────
    def Owns(self, RequestId: str) -> bool:
        return RequestId.startswith("mockreq_")

    async def Upload(self, Data: bytes, ContentType: str) -> str:
        if self.FailUploads:
            raise ProviderError("Mock upload failure", "upload_failed")
        Url = f"mock://upload/{uuid.uuid4().hex}"
        self.Uploads[Url] = Data
        return Url

    async def Submit(self, Endpoint: str, Arguments: dict) -> str:
        Queue = self.Scripts[Endpoint]
        Outcome = Queue.popleft() if Queue else "ok"
        if Outcome == "submit_transient":
            raise TransientProviderError("Mock transient submit failure")
        RequestId = f"mockreq_{uuid.uuid4().hex[:12]}"
        TransientReads = 0
        if Outcome.startswith("status_transient:"):
            TransientReads = int(Outcome.split(":", 1)[1]); Outcome = "ok"
        self.Requests[RequestId] = {"endpoint": Endpoint, "arguments": dict(Arguments),
                                    "outcome": Outcome, "started": time.monotonic(),
                                    "transient_reads": TransientReads}
        self.Submissions.append((Endpoint, dict(Arguments), RequestId))
        return RequestId

    async def Status(self, Endpoint: str, RequestId: str) -> ProviderStatus:
        Req = self.Requests.get(RequestId)
        if Req is None:
            raise ProviderError("Unknown mock request", "provider_error")
        if Req["transient_reads"] > 0:
            Req["transient_reads"] -= 1
            raise TransientProviderError("Mock transient status failure")
        if Req["outcome"] == "hang" or time.monotonic() - Req["started"] < self.LatencyS:
            return ProviderStatus("running")
        if Req["outcome"] == "fail":
            return ProviderStatus("completed", "Mock provider failure")
        return ProviderStatus("completed")

    async def Result(self, Endpoint: str, RequestId: str) -> dict:
        global _DuplicateImage
        Req = self.Requests[RequestId]
        if Req["outcome"] == "fail":
            raise ProviderError("Mock provider failure")
        Args = Req["arguments"]
        Url = f"mock://result/{RequestId}"
        if Endpoint in (endpoints.ImageGenerate, endpoints.ImageEdit):
            if Req["outcome"] == "duplicate":
                if _DuplicateImage is None:
                    _DuplicateImage = _RingImage(0, "duplicate")
                self.Files[Url] = _DuplicateImage
            else:
                Draw = _CharmImage if _IsCharmRequest(Args) else _RingImage
                self.Files[Url] = Draw(int(Args.get("seed", 0)), Args["prompt"][:40])
            return {"images": [{"url": Url, "content_type": "image/png"}], "description": "mock"}
        if Endpoint == endpoints.Movie:
            Data = None
            if self.RenderVideo:
                Source = self.Uploads.get(Args["image_url"])
                if Source:
                    Data = await asyncio.get_running_loop().run_in_executor(None, _RenderMp4, Source)
            if Data is None and self.RenderVideo and _BundledMovie.is_file():
                Data = _BundledMovie.read_bytes()      # servers without ffmpeg still get a playable clip
            self.Files[Url] = Data or _FakeMp4()
            return {"video": {"url": Url, "content_type": "video/mp4"}}
        if Endpoint == endpoints.Mesh:
            Fmt = Args.get("export_format", "glb")
            self.Files[Url] = _MeshBytes(Fmt)
            return {"model_mesh": {"url": Url, "file_name": f"mesh.{Fmt}"}}
        raise ProviderError(f"Mock has no handler for {Endpoint}")

    async def Download(self, Url: str) -> bytes:
        if Url in self.Files:
            return self.Files[Url]
        raise ProviderError("Mock download not found", "download_failed")

    async def DownloadTo(self, Url: str, Target, OnProgress=None) -> tuple[int, str]:
        import hashlib
        from pathlib import Path
        Data = await self.Download(Url)
        Target = Path(Target)
        Target.parent.mkdir(parents=True, exist_ok=True)
        Target.write_bytes(Data)
        if OnProgress:
            OnProgress(len(Data), len(Data))
        return len(Data), hashlib.sha256(Data).hexdigest()
