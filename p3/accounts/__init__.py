"""Accounts, access tokens and credits — the ONLY integration point with identity.

Pipeline 3 never reads token or account storage directly: every route resolves
the caller through an AccountProvider into a Principal, and every paid action
asks the provider before spending and reports usage afterwards. Application
data (designs, bag lines, …) stores only Principal.AccountId.

Today the provider is LocalAccountProvider (P3's own accounts.db). A future
shared Users/Auth/Tokens service used by Pipeline 2 and Pipeline 3 is added by
implementing this same Protocol (see docs/ACCOUNTS.md) — no other module changes.
"""

from dataclasses import dataclass, field
from typing import Protocol


class AuthError(Exception):
    """The token is missing, unknown or deactivated."""

    def __init__(self, Code: str, Message: str):
        super().__init__(Message)
        self.Code = Code
        self.Message = Message


class InsufficientCredits(Exception):
    """The account may not spend the requested units (quota / balance policy)."""

    def __init__(self, Message: str = "You have used all the credits on this account. Contact us to extend your allowance."):
        super().__init__(Message)
        self.Message = Message


class DuplicateEmail(Exception):
    """An admin tried to create (or rename to) an email that already has an account."""

    def __init__(self, AccountId: str):
        super().__init__("This email already has an account.")
        self.AccountId = AccountId


class RemovedAccount(Exception):
    """An admin tried to create a customer with the email of a removed account: that account is restored instead of
    a second one being made (its history, credits and activity stay with it)."""

    def __init__(self, AccountId: str, Name: str = "", RemovedAt: str | None = None):
        super().__init__("This email belongs to a removed account.")
        self.AccountId, self.Name, self.RemovedAt = AccountId, Name, RemovedAt


class AccountNotFound(Exception):
    def __init__(self, AccountId: str):
        super().__init__(f"Unknown account: {AccountId}")
        self.AccountId = AccountId


@dataclass(frozen=True)
class Principal:
    """The authenticated caller.

    AccountId is a namespaced, provider-issued identifier ("<issuer>:<id>", e.g.
    "p3local:acct_…"). It is what application tables store, so they can later be
    re-pointed at a shared user system by mapping IDs, not by rewriting tokens.
    """
    AccountId: str
    DisplayName: str
    Issuer: str
    Attributes: dict = field(default_factory=dict)


# Usage kinds P3 reports. Units are provider requests (an image batch of four is 4 "image").
UsageImage = "image"
UsageMovie = "movie"
UsageMesh = "mesh"
UsagePromptCheck = "prompt_check"      # one LLM request before the images (XJet's cost)


class AccountProvider(Protocol):
    Issuer: str

    def Authenticate(self, Token: str | None) -> Principal:
        """Resolve an access token to a Principal or raise AuthError."""

    def AuthorizeSpend(self, Who: Principal, Kind: str, Units: int) -> None:
        """Reserve Units credits for paid work about to start, atomically against the allowance (what is used plus
        what is reserved plus Units must fit), or raise InsufficientCredits. p3/credits.py decides the units."""

    def Settle(self, AccountId: str, Kind: str, RefId: str, Charged: bool, Units: int = 1) -> bool:
        """The reserved credits of one piece of work: charged (a delivered result; once per RefId) or released."""

    def Release(self, AccountId: str, Units: int) -> None:
        """Give reserved credits back (the work was never created)."""

    def RebuildReservations(self, Reserved: dict) -> None:
        """Set every account's reservation from {account_id: units} (the job tables at startup); others to 0."""

    def RecordUsage(self, AccountId: str, Kind: str, Units: int, RefId: str,
                    Provider: str | None = None, Endpoint: str | None = None, CostUsd: float | None = None,
                    CostSource: str | None = None, Internal: bool = False) -> None:
        """Record units actually submitted to a provider (called once per submission), with the provider, endpoint
        and estimated cost so cost per user can be reported; Internal = XJet's own work, never charged."""

    def SpendSince(self, DayIso: str) -> float:
        """The estimated cost of the live submissions recorded since this UTC day (YYYY-MM-DD)."""

    def RecordSignIn(self, AccountId: str, Method: str) -> None:
        """Record a customer sign-in (token entry or verification link)."""

    def CommitCharge(self, AccountId: str, Kind: str, RefId: str) -> bool:
        """Charge the account's allowance for a FINISHED result (P2: one generation per 360° movie)."""

    def StartEmailRegistration(self, Name: str, Email: str) -> dict:
        """Email sign-in / registration (P2 /api/register semantics, for every account with that email):
        already_registered (+ token to mail) | verification_sent / verification_resent (+ secret) | inactive | removed."""

    def VerifyEmail(self, Secret: str) -> dict:
        """Consume a verification link (P2 /verify): verified | already | expired | invalid | removed."""

    def UsageSummary(self, AccountId: str) -> dict:
        """{kind: units} for display."""

    def Profile(self, Who: Principal) -> dict:
        """Customer-visible account summary (label, usage, balance if any)."""

    # Administration — implemented by providers that own their token storage.
    def IssueToken(self, Label: str, DisplayName: str | None = None, Email: str | None = None) -> tuple[str, Principal]:
        ...

    def DeactivateToken(self, Token: str) -> bool:
        ...

    def ListAccounts(self) -> list[dict]:
        ...


def BuildProvider(Kind: str, DataDir) -> AccountProvider:
    """Factory keyed by P3_ACCOUNT_PROVIDER. Only "local" exists today."""
    if Kind == "local":
        from p3.accounts.local import LocalAccountProvider
        return LocalAccountProvider(DataDir / "accounts.db")
    raise ValueError(f"Unknown account provider {Kind!r} (supported: local)")
