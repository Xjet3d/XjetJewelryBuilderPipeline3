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

# The shared inbox the business confirmed on 2026-10-12. Unless configured otherwise it is the support address the site
# and the emails name (P3_SUPPORT_EMAIL), the Reply-To of every email (MAIL_REPLY_TO) and the recipient of the team's
# notifications about new orders and quote requests (P3_STAFF_NOTIFY_EMAILS; "none" switches them off, as on proto).
DefaultSupportEmail = "atelier@xjet3d.com"


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
    AdminBaseUrl: str = ""           # P3_ADMIN_BASE_URL: where Admin sign-in returns to, e.g. http://localhost:8340 (an SSH tunnel)
    LockMode: bool = False           # P3_LOCK_MODE: the live/mock switch is refused and var/runtime.json is ignored
    SigningSecret: str | None = None  # P3_SIGNING_SECRET: HMAC secret for signed download links (falls back to the admin key)
    MailMode: str = "outbox"         # P3_MAIL_MODE: outbox | smtp (read by p3.mail; here for validation)
    DailyAiSpendCapUsd: float | None = None   # P3_DAILY_AI_SPEND_CAP_USD: paid submissions stop when the day's estimate reaches it
    RateLimits: bool = True          # P3_RATE_LIMITS: per-IP / per-account abuse protection (tests switch it off)
    NoDevTools: bool = False         # P3_NO_DEV_TOOLS: hide the developer tools outside production too (staging)
    SupportEmail: str = DefaultSupportEmail   # P3_SUPPORT_EMAIL: the address the site and the emails name for questions
    AdminKeyFromAdmin: bool = False  # the Admin key was changed in Admin → Settings → Keys (p3/adminkey.py); never in production
    FalKeyFromAdmin: bool = False    # the key was saved in Admin → Settings → System (p3/falkey.py); never in production
    StaffNotifyEmails: tuple = ()    # P3_STAFF_NOTIFY_EMAILS: who is emailed about a new order or quote request (comma-separated;
                                     # unset = the support address, "none" = nobody)
    ReplyTo: str = ""                # MAIL_REPLY_TO: the Reply-To of outgoing mail (unset = the support address)
    # ── customer authentication and administrator identity (AJW-39, docs/AUTH-PROPOSAL-AJW-39.md) ──
    AuthMode: str = "dual"           # P3_AUTH_MODE: legacy (6-letter token only) | dual (sessions and token) | modern (sessions only)
    AdminIdentityEnforced: bool = False   # P3_ADMIN_IDENTITY_ENFORCED: the Admin opens only for enrolled Google/Apple identities
    GoogleClientId: str = ""         # P3_GOOGLE_CLIENT_ID / P3_GOOGLE_CLIENT_SECRET: the Google OAuth web client
    GoogleClientSecret: str = ""
    AppleClientId: str = ""          # P3_APPLE_CLIENT_ID (the Services ID), P3_APPLE_TEAM_ID, P3_APPLE_KEY_ID,
    AppleTeamId: str = ""            # P3_APPLE_PRIVATE_KEY (the .p8 text, "\n" allowed) or P3_APPLE_PRIVATE_KEY_PATH
    AppleKeyId: str = ""
    ApplePrivateKey: str = ""
    # ── the Atelier ↔ Fulfillment integration (docs/FULFILLMENT.md); off by default: nothing of it exists then ──
    FulfillmentIntegration: bool = False      # P3_FULFILLMENT_INTEGRATION: the bridge routes, the Admin action, the triggers
    FulfillmentServiceKey: str | None = None  # P3_FULFILLMENT_SERVICE_KEY: the credential Fulfillment presents (never the Admin key)
    # Customer notifications derived from Fulfillment's events (Batch 8): intents are always recorded; sending is OFF by
    # default and, when on, goes to the test recipient when one is set (never to customers until the owner activates it)
    FulfillmentCustomerNotifications: bool = False   # P3_FULFILLMENT_CUSTOMER_NOTIFICATIONS
    FulfillmentNotificationRecipient: str | None = None   # P3_FULFILLMENT_NOTIFICATION_RECIPIENT: every customer email goes here instead (testing / initial activation)
    FulfillmentNotificationWindowS: int = 120    # P3_FULFILLMENT_NOTIFICATION_WINDOW_S: parts of one order that reach a milestone together make one email
    # The milestones e-mailed once sending is on (the owner's decision of 2026-10-15: shipped and delivered); the others are
    # recorded as suppressed, never sent later. Known: review_approved, changes_required, cannot_produce, production_started, shipped, delivered
    FulfillmentNotificationMilestones: tuple = ("shipped", "delivered")   # P3_FULFILLMENT_NOTIFICATION_MILESTONES (comma-separated)
    # The commercial rule Atelier owns and advertises to Fulfillment (identity.payment_gate; docs/order-to-production-review.md
    # §J, K8): True (the default, the deployed systems) — an unpaid order's lines are not released and the live checks say
    # PAYMENT_PENDING; False — payment is shown, never gating: the Admin decides what enters Production (switched at activation)
    FulfillmentPaymentGate: bool = True          # P3_FULFILLMENT_PAYMENT_GATE
    FulfillmentUrl: str = ""                     # P3_FULFILLMENT_URL: the Fulfillment application's URL for the Admin's "Open in Fulfillment" links (e.g. https://tron/atelier-fulfillment)
    # The controlled phase (the owner's authorization of 2026-10-13, §6): while set, only these orders (ids or references,
    # comma-separated) can be sent to Production — enforced by the server, not by the page; empty: every eligible order
    FulfillmentOrderAllowlist: tuple = ()        # P3_FULFILLMENT_ORDER_ALLOWLIST

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


def _ApplePrivateKey() -> str:
    """The Sign in with Apple private key (.p8): inline in P3_APPLE_PRIVATE_KEY, or read from P3_APPLE_PRIVATE_KEY_PATH."""
    Inline = os.environ.get("P3_APPLE_PRIVATE_KEY", "").strip().replace("\\n", "\n")
    if Inline:
        return Inline
    Path_ = os.environ.get("P3_APPLE_PRIVATE_KEY_PATH", "").strip()
    return Path(Path_).read_text(encoding="utf-8").strip() if Path_ and Path(Path_).is_file() else ""


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


def StaffRecipients(Raw: str, Support: str) -> tuple:
    """P3_STAFF_NOTIFY_EMAILS: a comma-separated list; unset means the support address; "none" (or "off") means nobody."""
    Raw = (Raw or "").strip()
    if Raw.lower() in ("none", "off"):
        return ()
    return tuple(A.strip() for A in Raw.split(",") if A.strip()) or ((Support,) if Support else ())


def LoadSettings(**Overrides) -> Settings:
    _LoadDotEnv(RepoRoot / ".env")
    Support = os.environ.get("P3_SUPPORT_EMAIL", "").strip() or DefaultSupportEmail
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
        AdminBaseUrl=os.environ.get("P3_ADMIN_BASE_URL", "").strip().rstrip("/"),
        LockMode=_Bool("P3_LOCK_MODE"),
        SigningSecret=os.environ.get("P3_SIGNING_SECRET") or None,
        MailMode=os.environ.get("P3_MAIL_MODE", "outbox").strip().lower(),
        DailyAiSpendCapUsd=float(os.environ["P3_DAILY_AI_SPEND_CAP_USD"]) if os.environ.get("P3_DAILY_AI_SPEND_CAP_USD") else None,
        RateLimits=_Bool("P3_RATE_LIMITS", True),
        NoDevTools=_Bool("P3_NO_DEV_TOOLS"),
        SupportEmail=Support,
        StaffNotifyEmails=StaffRecipients(os.environ.get("P3_STAFF_NOTIFY_EMAILS", ""), Support),
        ReplyTo=os.environ.get("MAIL_REPLY_TO", "").strip() or Support,
        AuthMode=os.environ.get("P3_AUTH_MODE", "dual").strip().lower(),
        AdminIdentityEnforced=_Bool("P3_ADMIN_IDENTITY_ENFORCED"),
        GoogleClientId=os.environ.get("P3_GOOGLE_CLIENT_ID", "").strip(),
        GoogleClientSecret=os.environ.get("P3_GOOGLE_CLIENT_SECRET", "").strip(),
        AppleClientId=os.environ.get("P3_APPLE_CLIENT_ID", "").strip(),
        AppleTeamId=os.environ.get("P3_APPLE_TEAM_ID", "").strip(),
        AppleKeyId=os.environ.get("P3_APPLE_KEY_ID", "").strip(),
        ApplePrivateKey=_ApplePrivateKey(),
        FulfillmentIntegration=_Bool("P3_FULFILLMENT_INTEGRATION"),
        FulfillmentServiceKey=os.environ.get("P3_FULFILLMENT_SERVICE_KEY") or None,
        FulfillmentCustomerNotifications=_Bool("P3_FULFILLMENT_CUSTOMER_NOTIFICATIONS"),
        FulfillmentNotificationRecipient=os.environ.get("P3_FULFILLMENT_NOTIFICATION_RECIPIENT", "").strip() or None,
        FulfillmentNotificationWindowS=int(os.environ.get("P3_FULFILLMENT_NOTIFICATION_WINDOW_S") or 120),
        FulfillmentNotificationMilestones=tuple(X.strip() for X in (os.environ.get("P3_FULFILLMENT_NOTIFICATION_MILESTONES") or "shipped,delivered").split(",") if X.strip()),
        FulfillmentPaymentGate=_Bool("P3_FULFILLMENT_PAYMENT_GATE", True),
        FulfillmentUrl=os.environ.get("P3_FULFILLMENT_URL", "").strip().rstrip("/"),
        FulfillmentOrderAllowlist=tuple(X.strip() for X in os.environ.get("P3_FULFILLMENT_ORDER_ALLOWLIST", "").split(",") if X.strip()),
    )
    Values.update(Overrides)
    Values["BasePath"] = NormalizeBasePath(Values["BasePath"])
    S = Settings(**Values)
    if not S.Production:                           # a key saved in the Admin wins over FAL_KEY (never in production)
        from p3 import adminkey, falkey
        Saved = falkey.ReadSaved(S.DataDir)
        if Saved:
            S.FalKey, S.FalKeyFromAdmin = Saved, True
        SavedAdmin = adminkey.ReadSaved(S.DataDir)
        if SavedAdmin:
            S.AdminKey, S.AdminKeyFromAdmin = SavedAdmin, True
    if S.Env not in ("development", "production"):
        raise ValueError(f"P3_ENV must be 'development' or 'production', got {S.Env!r}")
    if S.AuthMode not in ("legacy", "dual", "modern"):
        raise ValueError(f"P3_AUTH_MODE must be legacy, dual or modern, got {S.AuthMode!r}")
    if S.Provider not in ("mock", "fal"):
        raise ValueError(f"P3_PROVIDER must be 'mock' or 'fal', got {S.Provider!r}")
    if S.Provider == "fal" and not S.FalKey:
        raise ValueError("P3_PROVIDER=fal requires FAL_KEY")
    if S.Production:
        S.LockMode = True                          # the developer switch never applies in production
        Problems = ValidateProduction(S)
        if Problems:
            raise ValueError("P3_ENV=production refuses to start with development defaults:\n  - " + "\n  - ".join(Problems))
    Problems = ValidateFulfillment(S)
    if Problems:
        raise ValueError("P3_FULFILLMENT_INTEGRATION=on refuses to start:\n  - " + "\n  - ".join(Problems))
    return S


# The customer milestones Fulfillment's events can mark (p3/fulfillment_notifications.py, spec §24.2)
NotificationMilestones = ("review_approved", "changes_required", "cannot_produce", "production_started", "shipped", "delivered")


def ValidateFulfillment(S: Settings) -> list[str]:
    """With the integration on, the service credential must exist, be long enough and be its own secret."""
    if not S.FulfillmentIntegration:
        return []
    P = []
    Min = 24 if S.Production else 16
    Key = S.FulfillmentServiceKey or ""
    if len(Key) < Min or any(C.isspace() for C in Key):
        P.append(f"P3_FULFILLMENT_SERVICE_KEY must be a random secret of at least {Min} characters (no spaces)")
    if Key and Key in (S.AdminKey, S.SigningSecret):
        P.append("P3_FULFILLMENT_SERVICE_KEY must differ from P3_ADMIN_KEY and P3_SIGNING_SECRET")
    R = S.FulfillmentNotificationRecipient or ""
    if R and ("@" not in R or any(C.isspace() for C in R)):
        P.append("P3_FULFILLMENT_NOTIFICATION_RECIPIENT must be one email address (the test recipient of every Fulfillment customer notification)")
    if S.FulfillmentCustomerNotifications and S.Production and not R:
        P.append("P3_FULFILLMENT_CUSTOMER_NOTIFICATIONS=true in production needs the owner's explicit activation: set P3_FULFILLMENT_NOTIFICATION_RECIPIENT "
                 "for the test phase, or remove this check with the approved deployment change")
    if S.FulfillmentNotificationWindowS < 0 or S.FulfillmentNotificationWindowS > 3600:
        P.append("P3_FULFILLMENT_NOTIFICATION_WINDOW_S is between 0 and 3600 seconds")
    Unknown = [M for M in S.FulfillmentNotificationMilestones if M not in NotificationMilestones]
    if Unknown:
        P.append(f"P3_FULFILLMENT_NOTIFICATION_MILESTONES names unknown milestones: {', '.join(Unknown)} (known: {', '.join(NotificationMilestones)})")
    return P


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
    if S.AuthMode == "legacy":
        P.append("P3_AUTH_MODE must be dual or modern (legacy is the pre-AJW-39 six-letter-token sign-in)")
    if S.AuthMode == "modern" and not S.AdminIdentityEnforced:
        P.append("P3_AUTH_MODE=modern requires P3_ADMIN_IDENTITY_ENFORCED=true (the Admin must not stay behind the shared key)")
    if "@" not in S.SupportEmail:
        P.append("P3_SUPPORT_EMAIL must be an email address (unset, the site uses the shared inbox " + DefaultSupportEmail + ")")
    try:
        if S.DataDir.is_relative_to(RepoRoot):
            P.append(f"P3_DATA_DIR must be outside the code checkout ({RepoRoot}); e.g. /srv/atelier/data")
    except (ValueError, OSError):
        pass
    return P
