"""Runtime settings for Pipeline 3, read from environment variables.

Nothing here points at Pipeline 2. All runtime data lives under P3_DATA_DIR
(default ./var inside this repository).
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

RepoRoot  = Path(__file__).resolve().parent.parent
ConfigDir = RepoRoot / "config"
WebDir    = RepoRoot / "web"


def _Bool(Name: str, Default: bool = False) -> bool:
    Raw = os.environ.get(Name)
    if Raw is None:
        return Default
    return Raw.strip().lower() in ("1", "true", "yes", "on")


def _LoadDotEnv(EnvPath: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing environment variables win."""
    if not EnvPath.is_file():
        return
    for Line in EnvPath.read_text(encoding="utf-8").splitlines():
        Line = Line.strip()
        if not Line or Line.startswith("#") or "=" not in Line:
            continue
        Key, Value = Line.split("=", 1)
        os.environ.setdefault(Key.strip(), Value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    DataDir: Path
    Provider: str                  # "mock" | "fal"
    FalKey: str | None
    AdminKey: str | None
    PricingProfilePath: Path
    AllowUnapprovedPricing: bool
    MockLatencyS: float
    PollIntervalS: float
    MaxTransientPollErrors: int
    AccountProvider: str = "local"   # P3_ACCOUNT_PROVIDER; see docs/ACCOUNTS.md
    BasePath: str = ""               # P3_BASE_PATH, e.g. "/JewelryB2C3"; "" = served at the root
    # ── production posture (docs/PRODUCTION-READINESS-HANDOFF.md) ──
    Env: str = "development"         # P3_ENV: development | production — production enforces ValidateProduction()
    PublicBaseUrl: str = ""          # P3_PUBLIC_BASE_URL: the customer site's origin for emailed links, share links, sitemap
    AdminHost: str = ""              # P3_ADMIN_HOST: the Admin page/API (and dev tools) answer only on this host
    LockMode: bool = False           # P3_LOCK_MODE: the live/mock switch is refused and var/runtime.json is ignored
    SigningSecret: str | None = None  # P3_SIGNING_SECRET: HMAC secret for signed download links (falls back to the admin key)
    MailMode: str = "outbox"         # P3_MAIL_MODE: outbox | smtp (read by p3.mail; here for validation)
    DailyAiSpendCapUsd: float | None = None   # P3_DAILY_AI_SPEND_CAP_USD: paid submissions stop when the day's estimate reaches it
    RateLimits: bool = True          # P3_RATE_LIMITS: per-IP / per-account abuse protection (tests switch it off)
    NoDevTools: bool = False         # P3_NO_DEV_TOOLS: hide the developer tools outside production too (staging)
    SupportEmail: str = ""           # P3_SUPPORT_EMAIL: the address the site and the emails name for questions (never invented)
    StaffNotifyEmails: tuple = ()    # P3_STAFF_NOTIFY_EMAILS: who is emailed about a new order or quote request (comma-separated)
    ReplyTo: str = ""                # MAIL_REPLY_TO: the Reply-To of outgoing mail; without it the mail says how to get in touch

    @property
    def Production(self) -> bool:
        return self.Env == "production"

    @property
    def DevTools(self) -> bool:
        """The developer page, /api/dev/*, the API docs, the showcase prototype and the #developer panel: never in production."""
        return not self.Production and not self.NoDevTools

    @property
    def DbPath(self) -> Path:
        return self.DataDir / "pipeline3.db"

    @property
    def AssetsDir(self) -> Path:
        """Customer-visible artifacts (candidate images, movies, references). Served at /assets."""
        return self.DataDir / "assets"

    @property
    def RuntimeStatePath(self) -> Path:
        """Developer-chosen runtime settings (AI mode) that override .env until changed again."""
        return self.DataDir / "runtime.json"

    @property
    def DevDir(self) -> Path:
        """Developer-only artifacts (meshes). Never served statically."""
        return self.DataDir / "dev"


def NormalizeBasePath(Raw: str | None) -> str:
    """"" or "/Segment[/Segment]" without a trailing slash (so "/JewelryB2C3/" -> "/JewelryB2C3")."""
    Value = (Raw or "").strip().rstrip("/")
    if not Value:
        return ""
    if not Value.startswith("/"):
        Value = "/" + Value
    if not re.fullmatch(r"(/[A-Za-z0-9._-]+)+", Value) or any(Seg in (".", "..") for Seg in Value.split("/")):
        raise ValueError(f"P3_BASE_PATH must look like /JewelryB2C3, got {Raw!r}")
    return Value


def LoadSettings(**Overrides) -> Settings:
    _LoadDotEnv(RepoRoot / ".env")
    ProfilePath = Path(os.environ.get("P3_PRICING_PROFILE", ConfigDir / "pricing_profile.json"))
    if not ProfilePath.is_absolute():
        ProfilePath = (RepoRoot / ProfilePath).resolve()
    Values = dict(
        DataDir=Path(os.environ.get("P3_DATA_DIR", RepoRoot / "var")).resolve(),
        Provider=os.environ.get("P3_PROVIDER", "mock").strip().lower(),
        FalKey=os.environ.get("FAL_KEY") or None,
        AdminKey=os.environ.get("P3_ADMIN_KEY") or None,
        PricingProfilePath=ProfilePath,
        AllowUnapprovedPricing=_Bool("P3_ALLOW_UNAPPROVED_PRICING"),
        MockLatencyS=float(os.environ.get("P3_MOCK_LATENCY_S", "1.5")),
        PollIntervalS=float(os.environ.get("P3_POLL_INTERVAL_S", "2.0")),
        MaxTransientPollErrors=int(os.environ.get("P3_MAX_TRANSIENT_POLL_ERRORS", "10")),
        AccountProvider=os.environ.get("P3_ACCOUNT_PROVIDER", "local").strip().lower(),
        BasePath=os.environ.get("P3_BASE_PATH", ""),
        Env=os.environ.get("P3_ENV", "development").strip().lower() or "development",
        PublicBaseUrl=os.environ.get("P3_PUBLIC_BASE_URL", "").strip().rstrip("/"),
        AdminHost=os.environ.get("P3_ADMIN_HOST", "").strip().lower(),
        LockMode=_Bool("P3_LOCK_MODE"),
        SigningSecret=os.environ.get("P3_SIGNING_SECRET") or None,
        MailMode=os.environ.get("P3_MAIL_MODE", "outbox").strip().lower(),
        DailyAiSpendCapUsd=float(os.environ["P3_DAILY_AI_SPEND_CAP_USD"]) if os.environ.get("P3_DAILY_AI_SPEND_CAP_USD") else None,
        RateLimits=_Bool("P3_RATE_LIMITS", True),
        NoDevTools=_Bool("P3_NO_DEV_TOOLS"),
        SupportEmail=os.environ.get("P3_SUPPORT_EMAIL", "").strip(),
        StaffNotifyEmails=tuple(A.strip() for A in os.environ.get("P3_STAFF_NOTIFY_EMAILS", "").split(",") if A.strip()),
        ReplyTo=os.environ.get("MAIL_REPLY_TO", "").strip(),
    )
    Values.update(Overrides)
    Values["BasePath"] = NormalizeBasePath(Values["BasePath"])
    S = Settings(**Values)
    if S.Env not in ("development", "production"):
        raise ValueError(f"P3_ENV must be 'development' or 'production', got {S.Env!r}")
    if S.Provider not in ("mock", "fal"):
        raise ValueError(f"P3_PROVIDER must be 'mock' or 'fal', got {S.Provider!r}")
    if S.Provider == "fal" and not S.FalKey:
        raise ValueError("P3_PROVIDER=fal requires FAL_KEY")
    if S.Production:
        S.LockMode = True                          # the developer switch never applies in production
        Problems = ValidateProduction(S)
        if Problems:
            raise ValueError("P3_ENV=production refuses to start with development defaults:\n  - " + "\n  - ".join(Problems))
    return S


def ValidateProduction(S: Settings) -> list[str]:
    """Everything a production start must have (docs/PRODUCTION-READINESS-HANDOFF.md, section E). An empty list
    means the configuration is acceptable; otherwise the service must not start."""
    P = []
    if S.Provider != "fal" or not S.FalKey:
        P.append("P3_PROVIDER must be fal with FAL_KEY set (no mock provider in production)")
    if not S.PublicBaseUrl.startswith("https://"):
        P.append("P3_PUBLIC_BASE_URL must be the customer site's https origin, e.g. https://atelier.example.com")
    if not S.AdminHost:
        P.append("P3_ADMIN_HOST must name the Admin host (the Admin and its API answer only there), e.g. admin.atelier.example.com")
    elif S.PublicBaseUrl and S.AdminHost == re.sub(r"^https?://", "", S.PublicBaseUrl).split("/")[0].lower():
        P.append("P3_ADMIN_HOST must differ from the public customer host")
    if S.BasePath:
        P.append("P3_BASE_PATH must be empty in production (the customer site is served at the root of its own host)")
    if not S.AdminKey or len(S.AdminKey) < 24:
        P.append("P3_ADMIN_KEY must be a random secret of at least 24 characters")
    if not S.SigningSecret or len(S.SigningSecret) < 24 or S.SigningSecret == S.AdminKey:
        P.append("P3_SIGNING_SECRET must be a separate random secret of at least 24 characters (signed download links)")
    if S.AllowUnapprovedPricing:
        P.append("P3_ALLOW_UNAPPROVED_PRICING must be false (customers only see approved prices)")
    if S.MailMode != "smtp":
        P.append("P3_MAIL_MODE must be smtp (the outbox mode sends nothing)")
    if S.DailyAiSpendCapUsd is None or S.DailyAiSpendCapUsd <= 0:
        P.append("P3_DAILY_AI_SPEND_CAP_USD must be set (the day's paid AI submissions stop at this estimate)")
    if "@" not in S.SupportEmail:
        P.append("P3_SUPPORT_EMAIL must be the confirmed support address the site and the emails name (none is invented)")
    try:
        if S.DataDir.is_relative_to(RepoRoot):
            P.append(f"P3_DATA_DIR must be outside the code checkout ({RepoRoot}); e.g. /srv/atelier/data")
    except (ValueError, OSError):
        pass
    return P
