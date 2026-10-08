"""AI provider mode (mock / live): startup choice, developer switch, persistence.

Precedence at startup:
  1. var/runtime.json "ai_mode", written by the developer switch (survives restarts);
  2. otherwise P3_PROVIDER from .env ("mock" | "fal").
Live mode is only possible when FAL_KEY is configured; a saved "live" choice
without a key falls back to mock, and says so.

Switching is refused while generations are running, so a job never ends up
being polled by a different provider than the one that accepted it.
"""

import json
import logging
from pathlib import Path

from p3.context import Context, HttpError

Logger = logging.getLogger("p3.modes")

Mock, Live = "mock", "live"
LiveConfirmation = "I understand live mode makes billed API calls"


def _ReadState(StatePath: Path) -> dict:
    try:
        return json.loads(StatePath.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def DefaultFactories(S) -> dict:
    def MakeMock():
        from p3.providers.mock import MockProvider
        return MockProvider(LatencyS=S.MockLatencyS)

    def MakeLive():
        from p3.providers.fal import FalProvider
        return FalProvider(S.FalKey)
    return {Mock: MakeMock, Live: MakeLive}


def ResolveStartupMode(S) -> tuple[str, str]:
    """(mode, source) for a fresh start. With P3_LOCK_MODE (always in production) the configuration decides alone:
    the developer switch's saved choice is ignored and there is no fallback."""
    if S.LockMode:
        return (Live if S.Provider == "fal" else Mock), "configuration (locked: P3_PROVIDER)"
    Saved = _ReadState(S.RuntimeStatePath).get("ai_mode")
    if Saved in (Mock, Live):
        if Saved == Live and not S.FalKey:
            Logger.warning("Saved AI mode is live but FAL_KEY is not set; starting in mock mode.")
            return Mock, "developer switch (live unavailable: no FAL_KEY)"
        return Saved, "developer switch"
    return (Live if S.Provider == "fal" else Mock), "configuration (.env P3_PROVIDER)"


class ModeManager:
    def __init__(self, Ctx: Context, Mode: str, Source: str, Factories: dict):
        self.Ctx = Ctx
        self.Mode = Mode
        self.Source = Source
        self.Factories = Factories

    @property
    def LiveAvailable(self) -> bool:
        return bool(self.Ctx.Settings.FalKey)

    def ActiveJobs(self) -> int:
        Db = self.Ctx.Db
        return (Db.One("SELECT COUNT(*) AS n FROM candidates WHERE status IN ('pending','generating')")["n"]
                + Db.One("SELECT COUNT(*) AS n FROM movies WHERE status IN ('queued','running')")["n"]
                + Db.One("SELECT COUNT(*) AS n FROM meshes WHERE status IN ('queued','running')")["n"])

    def Status(self) -> dict:
        return {"mode": self.Mode, "source": self.Source, "provider": self.Ctx.Provider.Name,
                "live_available": self.LiveAvailable, "active_jobs": self.ActiveJobs(),
                "locked": bool(self.Ctx.Settings.LockMode),
                "live_confirmation": LiveConfirmation}

    def Switch(self, Target: str, Confirmation: str | None) -> dict:
        if Target not in (Mock, Live):
            raise HttpError(400, "invalid_mode", "Mode must be 'mock' or 'live'.")
        if self.Ctx.Settings.LockMode:
            raise HttpError(409, "mode_locked", "The AI mode is fixed by the server configuration (P3_LOCK_MODE); "
                                                "it cannot be switched at runtime.")
        if Target == self.Mode:
            return self.Status()
        if Target == Live:
            if not self.LiveAvailable:
                raise HttpError(409, "live_unavailable", "Live mode needs FAL_KEY in the server configuration.")
            if (Confirmation or "").strip() != LiveConfirmation:
                raise HttpError(400, "live_confirmation_required",
                                f'Switching to live mode requires the confirmation text: "{LiveConfirmation}".')
        if self.ActiveJobs():
            raise HttpError(409, "jobs_running", "Wait until the running generations finish before switching AI mode.")
        self.Ctx.Provider = self.Factories[Target]()
        self.Mode = Target
        self.Source = "developer switch"
        Path_ = self.Ctx.Settings.RuntimeStatePath
        State = _ReadState(Path_)
        State["ai_mode"] = Target
        Path_.write_text(json.dumps(State, indent=2), encoding="utf-8")
        Logger.warning("AI mode switched to %s by developer", Target.upper())
        return self.Status()
