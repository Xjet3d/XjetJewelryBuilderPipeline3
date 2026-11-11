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
# An Admin's preview makes a hidden product only on a test account (p3/products.py RequirePreviewAccount): a test that
# previews one passes AccountEmail=PreviewEmail, so the harness's account is a test account
PreviewEmail = "harness.preview@example.com"


class Harness:
    def __init__(self, TmpPath: Path, Profile: Path = ShippedProfile, AllowUnapproved=False,
                 AdminKey=None, Provider=None, BasePath="", FalKey=None, Factories=None, AccountEmail=None, **SettingsOverrides):
        self.TmpPath = TmpPath
        # The existing suite runs in the pre-AJW-39 sign-in mode ("legacy"); tests/test_auth*.py pass AuthMode="dual" / "modern".
        # Tests run without the per-IP / per-account throttles unless a test asks for them (RateLimits=True), and
        # without team notifications unless a test names recipients (StaffNotifyEmails=...)
        self.Settings = LoadSettings(DataDir=TmpPath / "var", Provider="mock", PricingProfilePath=Profile,
                                     AllowUnapprovedPricing=AllowUnapproved, AdminKey=AdminKey,
                                     PollIntervalS=0.005, MaxTransientPollErrors=5, MockLatencyS=0.0,
                                     BasePath=BasePath, FalKey=FalKey, **{"RateLimits": False, "StaffNotifyEmails": (), "AuthMode": "legacy", **SettingsOverrides})
        self.Base = self.Settings.BasePath
        self.Provider = Provider or MockProvider(LatencyS=0.0, RenderVideo=False)
        self.App = CreateApp(self.Settings, None if Factories else self.Provider, ProviderFactories=Factories)
        self.Ctx = self.App.state.Ctx
        self.Svc = self.App.state.Services
        # A generous allowance: the credits (4 per design) must not get in the way of tests that make several designs
        self.Token, self.Who = self.Ctx.Accounts.IssueToken("test", MaxGenerations=100, Email=AccountEmail)
        # With a base path every test request goes to http://p3.test/<Base>/..., exactly like production.
        self.Client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.App), base_url="http://p3.test" + self.Base,
                                        headers={"X-Access-Token": self.Token})

    async def Idle(self):
        # A safety net, not a pace: it returns as soon as the tasks finish. 60 s, because one wait can cover real
        # geometry work — each job is its own Python process, and measuring an oval bore takes 5–7 s on a Windows
        # laptop, so a 3D result's measure + preview + integrity came to 7.5–8.5 s and failed at the old 10 s
        # whenever the machine was a little busier (tests/test_stl_fix_reuse.py, 11 Nov 2026).
        await self.Ctx.Runner.WaitIdle(60)

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


def Accepted(H) -> dict:
    """The Terms box ticked, with the versions the site serves now: what every sign-up sends (2026-10-29)."""
    return {"accept_terms": True, **H.Ctx.Accounts.Consent.Current()}


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


LegacyCharmSizes = [14, 18, 22]      # on the 10-30 mm grid, 18 among them (5 Nov 2026): the charm tests' 15 / 20 / 25 mm


async def OfferCharmSizes(H, AdminKey: str, Sizes=LegacyCharmSizes) -> None:
    """Put these charm sizes on offer (an Admin setting): the older charm tests' 20 / 25 mm are 18 / 22 mm since 5 Nov 2026."""
    R = await H.Client.put("/api/admin/products/charm-sizes", json={"sizes": Sizes, "names": {}, "default": None},
                           headers={"Authorization": f"Bearer {AdminKey}"})
    assert R.status_code == 200, R.text
