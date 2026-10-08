"""Wiring of the shared services used by every module (no globals)."""

import asyncio
from dataclasses import dataclass, field

from p3.accounts import AccountProvider
from p3.config import Catalog, GenerationConfig
from p3.db import Database
from p3.pricing.service import PricingService
from p3.providers.base import Provider
from p3.runner import TaskRunner
from p3.settings import Settings


class HttpError(Exception):
    """Service-level error carrying an HTTP status and a stable error code."""

    def __init__(self, Status: int, Code: str, Message: str, Details: dict | None = None):
        super().__init__(Message)
        self.Status = Status
        self.Code = Code
        self.Message = Message
        self.Details = Details or {}                  # extra fields of the error JSON (never "code" or "message")


@dataclass
class Context:
    Settings: Settings
    Db: Database
    Provider: Provider
    Gen: GenerationConfig
    Catalog: Catalog
    Pricing: PricingService
    Accounts: AccountProvider
    Models: "ModelConfigStore" = None             # versioned AI prompts & parameters (source of truth)
    MaterialPrices: "MaterialPriceBook" = None    # versioned density / price $/g / cost $/g / fixed price
    Products: "ProductSettings" = None            # rings and charms: customer availability of charms, charm sizes
    CharmPrices: "CharmPriceBook" = None          # charm pricing: its own versioned table, never the ring prices
    AiPrices: "PriceBook" = None                  # fal.ai list prices: the estimated cost recorded with every submission
    RateLimiter: "RateLimiter" = None             # per-IP / per-account request limits (p3/ratelimit.py)
    SpendInFlight: float = 0.0                    # estimated cost of the paid submissions between cap check and record
    Runner: TaskRunner = field(default_factory=TaskRunner)
    ImageSemaphore: asyncio.Semaphore | None = None
    _Locks: dict = field(default_factory=dict)

    def Lock(self, Key: str) -> asyncio.Lock:
        if Key not in self._Locks:
            self._Locks[Key] = asyncio.Lock()
        return self._Locks[Key]

    def Semaphore(self) -> asyncio.Semaphore:
        if self.ImageSemaphore is None:
            self.ImageSemaphore = asyncio.Semaphore(self.Gen.Images.MaxConcurrentRequests)
        return self.ImageSemaphore

    def AssetUrl(self, RelPath: str | None) -> str | None:
        # Always under the base path, so a deployment behind /JewelryB2C3 never emits root URLs.
        return f"{self.Settings.BasePath}/assets/{RelPath}" if RelPath else None
