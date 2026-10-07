"""Credits — what a paid action costs the customer, decided in one place (docs/PRODUCTION-READINESS-HANDOFF.md).

A credit is a customer-triggered generation ACTION, not an output file (Admin → Settings → Products → Credits;
DefaultTariff below): 1 credit per design request (whatever it generates — normally four images), 1 per refinement
request (its four images), 1 per explicitly requested additional option ("Generate another option"), 1 per 360°
movie, 0 for a 3D model (Hi3D is XJet's production cost, never the customer's). Selecting an option, changing the
material or the size and reusing an existing movie cost nothing. Ten credits are, for example, four design requests,
two refinements and four movies.

A credit is RESERVED before the work is created — atomically against the allowance, so parallel requests can never
overspend it — and CHARGED when the action delivers (its first ready image; a finished movie). An action whose every
image fails, times out or comes out a duplicate releases its reservation: the customer pays for what they get.
Reservations are rebuilt from the job tables at every start, so a crash cannot strand them. Work the Admin asks for
("Make a new movie", 3D models) is recorded as internal usage and never charged to a customer.

Every provider submission is recorded as usage with its estimated list-price cost (p3/aipricing.py); the day's
estimated live spend — the recorded events plus the submissions in flight — is checked against
P3_DAILY_AI_SPEND_CAP_USD before each paid submission.
"""

from collections import defaultdict
from datetime import datetime, timezone

from p3.accounts import Principal
from p3.context import Context, HttpError
from p3.db import NewId
from p3.providers.base import ProviderError

Design, Refinement, Option, Movie, Mesh = "design", "refinement", "option", "movie", "mesh"
Kinds = (Design, Refinement, Option, Movie, Mesh)
Labels = {Design: "design request (four images)", Refinement: "refinement request (four images)",
          Option: "additional option requested", Movie: "360° movie", Mesh: "3D model"}
DefaultTariff = {Design: 1, Refinement: 1, Option: 1, Movie: 1, Mesh: 0}
TariffKey = "credit_tariff"              # stored with the product settings (Admin-edited, logged)
TariffMax = 100
SpendCapCode = "spend_cap_reached"
SpendCapCustomerText = "AI generation is paused for today. Please try again tomorrow."
CostSource = "estimate: fal.ai list price (Admin → Pricing → AI prices)"


def Tariff(Ctx: Context) -> dict:
    """The credits one piece of work of each kind costs (the stored tariff over the defaults)."""
    Settings_ = getattr(Ctx, "Products", None)
    Stored = (Settings_.Get(TariffKey) if Settings_ is not None else None) or {}
    return {K: int(Stored.get(K, DefaultTariff[K])) for K in Kinds}


def ValidateTariff(Raw) -> dict:
    if not isinstance(Raw, dict):
        raise HttpError(400, "invalid_tariff", "The tariff is given per kind of work.")
    Out = {}
    for K in Kinds:
        V = Raw.get(K, DefaultTariff[K])
        if isinstance(V, bool) or not isinstance(V, int) or not 0 <= V <= TariffMax:
            raise HttpError(400, "invalid_tariff",
                            f"The credits per {Labels[K]} must be a whole number from 0 to {TariffMax}.")
        Out[K] = V
    return Out


def KindOfBatch(BatchKind: str | None) -> str:
    """A refinement batch is a refinement request; every other batch is a design request."""
    return Refinement if BatchKind == "refine" else Design


def ActionId() -> str:
    """The credit reference of an explicitly requested additional option (a retry click): its own action."""
    return NewId("act")


def KindOfCandidate(Cand: dict, Batch: dict) -> str:
    """What the slot's credit was reserved for: the batch's request, or an additional option asked for later."""
    Ref = Cand.get("credit_ref") or Batch["id"]
    return Option if Ref != Batch["id"] else KindOfBatch(Batch["kind"])


def SettleAction(Ctx: Context, AccountId: str | None, Kind: str, CreditRef: str) -> None:
    """A slot of the action finished: the action is charged once it has delivered an image (whatever the other
    slots do later) and released once every slot has finished without one. Settle() is idempotent per reference."""
    Rows = Ctx.Db.All("SELECT status FROM candidates WHERE credit_ref = ?", (CreditRef,))
    Statuses = [R["status"] for R in Rows]
    if "ready" in Statuses:
        Settle(Ctx, AccountId, Kind, CreditRef, True)
    elif Statuses and all(St in ("ready", "failed") for St in Statuses):
        Settle(Ctx, AccountId, Kind, CreditRef, False)


def Cost(Ctx: Context, Kind: str, Count: int = 1) -> int:
    return Tariff(Ctx)[Kind] * int(Count)


def Reserve(Ctx: Context, Who: Principal, Kind: str, Count: int = 1) -> int:
    """Reserve the credits for Count pieces of work of this kind, or raise InsufficientCredits. Returns the units."""
    Units = Cost(Ctx, Kind, Count)
    if Units > 0:
        Ctx.Accounts.AuthorizeSpend(Who, Kind, Units)
    return Units


def Release(Ctx: Context, AccountId: str | None, Units: int) -> None:
    """The work was never created (an error after the reservation): give the credits back."""
    if Units > 0 and AccountId:
        Ctx.Accounts.Release(AccountId, Units)


def Settle(Ctx: Context, AccountId: str | None, Kind: str, RefId: str, Charged: bool, Count: int = 1) -> None:
    """The work is over: charge the reserved credits (a delivered result) or release them (a failure). Charging is
    once per RefId, so a resumed or repeated settlement never charges twice."""
    Units = Cost(Ctx, Kind, Count)
    if Units > 0 and AccountId:
        Ctx.Accounts.Settle(AccountId, Kind, RefId, Charged, Units)


def Rebuild(Ctx: Context) -> dict:
    """Reservations from the job tables (after the startup reconciliation): every pending or generating customer
    image and every queued or running customer movie holds its credits; nothing else does."""
    Reserved: dict[str, int] = defaultdict(int)
    Db = Ctx.Db
    for R in Db.All("SELECT DISTINCT COALESCE(c.credit_ref, b.id) AS ref, b.id AS batch_id, b.kind, d.owner_account_id AS acct "
                    "FROM candidates c JOIN batches b ON b.id = c.batch_id JOIN designs d ON d.id = b.design_id "
                    "WHERE c.status IN ('pending', 'generating')"):
        Reserved[R["acct"]] += Cost(Ctx, Option if R["ref"] != R["batch_id"] else KindOfBatch(R["kind"]))
    for R in Db.All("SELECT COALESCE(m.requested_by, d.owner_account_id) AS acct FROM movies m "
                    "JOIN candidates c ON c.id = m.candidate_id JOIN batches b ON b.id = c.batch_id "
                    "JOIN designs d ON d.id = b.design_id "
                    "WHERE m.status IN ('queued', 'running') AND m.made_by_admin IS NULL"):
        Reserved[R["acct"]] += Cost(Ctx, Movie)
    Out = {K: V for K, V in Reserved.items() if V}
    Ctx.Accounts.RebuildReservations(Out)
    return Out


# ── usage and the day's spend ────────────────────────────────────────────────
def Today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def Estimate(Ctx: Context, Endpoint: str, Params: dict | None) -> float | None:
    """The estimated list-price cost of one request (None when no price is known)."""
    Book = getattr(Ctx, "AiPrices", None)
    if Book is None:
        return None
    try:
        return Book.Estimate(Endpoint, Ctx.Provider.Name, Params or {}).get("cost")
    except Exception:  # noqa: BLE001 — a price list problem never stops a generation
        return None


def SpendToday(Ctx: Context) -> float:
    """The day's estimated live spend: recorded submissions plus the ones in flight (UTC day)."""
    return float(Ctx.Accounts.SpendSince(Today())) + float(getattr(Ctx, "SpendInFlight", 0.0) or 0.0)


def CheckSpendCap(Ctx: Context, Endpoint: str, Params: dict | None) -> float:
    """Before a paid submission: the day's estimated spend plus this request must stay within the cap. The request's
    estimate joins the in-flight tally (so concurrent submissions count each other) until SubmitDone()."""
    Cap = Ctx.Settings.DailyAiSpendCapUsd
    if not Cap or Ctx.Provider.Name == "mock":
        return 0.0
    Est = Estimate(Ctx, Endpoint, Params) or 0.0
    if SpendToday(Ctx) + Est > Cap:
        raise ProviderError(f"The daily AI spend cap (${Cap:,.2f}) is reached: no more paid requests are submitted today.",
                            SpendCapCode)
    Ctx.SpendInFlight = float(getattr(Ctx, "SpendInFlight", 0.0) or 0.0) + Est
    return Est


def SubmitDone(Ctx: Context, Est: float) -> None:
    """The submission is recorded (or failed): its estimate leaves the in-flight tally."""
    if Est:
        Ctx.SpendInFlight = max(0.0, float(getattr(Ctx, "SpendInFlight", 0.0) or 0.0) - Est)


def RecordUsage(Ctx: Context, AccountId: str | None, Kind: str, RefId: str, Endpoint: str, Params: dict | None = None,
                Internal: bool = False) -> None:
    """One provider submission: the usage event with its estimated list-price cost. Internal = XJet's own work (the
    Admin's movies, 3D models), attributed to the account for reporting but never charged to it."""
    if not AccountId:
        return
    Ctx.Accounts.RecordUsage(AccountId, Kind, 1, RefId, Provider=Ctx.Provider.Name, Endpoint=Endpoint,
                             CostUsd=Estimate(Ctx, Endpoint, Params), CostSource=CostSource, Internal=Internal)


def Status(Ctx: Context) -> dict:
    """For the Admin: the tariff, the defaults and the day's spend against the cap."""
    return {"tariff": Tariff(Ctx), "default_tariff": dict(DefaultTariff), "kinds": [{"id": K, "label": Labels[K]} for K in Kinds],
            "spend_today_usd": round(SpendToday(Ctx), 4), "daily_cap_usd": Ctx.Settings.DailyAiSpendCapUsd,
            "provider": Ctx.Provider.Name, "cost_source": CostSource}
