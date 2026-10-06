"""Test harness: every test gets its own temporary data dir and a mock provider.

No test makes a network call or touches Pipeline 2.
"""

import json
from pathlib import Path

import httpx
import pytest

from p3.app import CreateApp
from p3.providers.mock import MockProvider
from p3.settings import ConfigDir, LoadSettings

ShippedProfile = ConfigDir / "pricing_profile.json"
DevProfile = ConfigDir / "pricing_profile.dev-example.json"


class Harness:
    def __init__(self, TmpPath: Path, Profile: Path = ShippedProfile, AllowUnapproved=False,
                 AdminKey=None, Provider=None, BasePath="", FalKey=None, Factories=None):
        self.TmpPath = TmpPath
        self.Settings = LoadSettings(DataDir=TmpPath / "var", Provider="mock", PricingProfilePath=Profile,
                                     AllowUnapprovedPricing=AllowUnapproved, AdminKey=AdminKey,
                                     PollIntervalS=0.005, MaxTransientPollErrors=5, MockLatencyS=0.0,
                                     BasePath=BasePath, FalKey=FalKey)
        self.Base = self.Settings.BasePath
        self.Provider = Provider or MockProvider(LatencyS=0.0, RenderVideo=False)
        self.App = CreateApp(self.Settings, None if Factories else self.Provider, ProviderFactories=Factories)
        self.Ctx = self.App.state.Ctx
        self.Svc = self.App.state.Services
        self.Token, self.Who = self.Ctx.Accounts.IssueToken("test")
        # With a base path every test request goes to http://p3.test/<Base>/..., exactly like production.
        self.Client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.App), base_url="http://p3.test" + self.Base,
                                        headers={"X-Access-Token": self.Token})

    async def Idle(self):
        await self.Ctx.Runner.WaitIdle(10)

    async def Close(self):
        await self.Ctx.Runner.Shutdown()
        await self.Client.aclose()

    # ── flow helpers ─────────────────────────────────────────────────────
    async def NewDesign(self, Prompt="A slim twisted band with a small leaf motif", **Form) -> dict:
        R = await self.Client.post("/api/designs", data={"prompt": Prompt, **Form})
        assert R.status_code == 200, R.text
        await self.Idle()
        return (await self.Client.get(f"/api/batches/{R.json()['id']}")).json()

    async def Design(self, DesignId) -> dict:
        R = await self.Client.get(f"/api/designs/{DesignId}")
        assert R.status_code == 200, R.text
        return R.json()

    async def Proceed(self, DesignId, CandidateId) -> dict:
        R = await self.Client.post(f"/api/designs/{DesignId}/customize", json={"candidate_id": CandidateId})
        assert R.status_code == 200, R.text
        return R.json()

    def AssetBytes(self, Url: str) -> bytes:
        assert Url.startswith(self.Base + "/assets/"), Url
        return (self.Settings.AssetsDir / Url.removeprefix(self.Base + "/assets/")).read_bytes()


@pytest.fixture
async def H(tmp_path):
    Obj = Harness(tmp_path)
    yield Obj
    await Obj.Close()


@pytest.fixture
async def HDevPricing(tmp_path):
    Obj = Harness(tmp_path, Profile=DevProfile, AllowUnapproved=True)
    yield Obj
    await Obj.Close()


@pytest.fixture
def WriteProfile(tmp_path):
    def _Write(Data: dict) -> Path:
        P = tmp_path / "profile.json"
        P.write_text(json.dumps(Data), encoding="utf-8")
        return P
    return _Write


def MakeLive(H) -> None:
    """Re-label everything this harness generated as live (fal) activity — the Admin's statistics
    deliberately leave mock-mode sessions out."""
    Db = H.Ctx.Db
    Db.Execute("UPDATE designs SET ai_mode = 'fal'")
    for T in ("candidates", "movies", "meshes"):
        Db.Execute(f"UPDATE {T} SET provider_request_id = 'falreq_' || id WHERE provider_request_id LIKE 'mockreq_%'")
    H.Ctx.Accounts.Db.Execute("UPDATE usage_events SET provider = 'fal', mode = 'live' WHERE provider = 'mock'")


LegacyCharmSizes = [15, 20, 25, 30]      # the sizes the charm tests of 2026-10-05 were written with (on offer today: 10 / 14 / 18)


async def OfferCharmSizes(H, AdminKey: str, Sizes=LegacyCharmSizes) -> None:
    """Put these charm sizes on offer (an Admin setting) so the older charm tests keep their 20 / 25 mm numbers."""
    R = await H.Client.put("/api/admin/products/charm-sizes", json={"sizes": Sizes, "names": {}, "default": None},
                           headers={"Authorization": f"Bearer {AdminKey}"})
    assert R.status_code == 200, R.text
