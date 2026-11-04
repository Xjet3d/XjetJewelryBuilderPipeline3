"""Israeli pricing (B6): prices in shekels including VAT for Israeli customers; USD everywhere else, exactly as before.

OFF by default (Admin → Settings → Pricing & Materials → Israeli pricing). Off, nothing here changes a price, a page or an
order. On:
  * the visitor's currency: their own choice (the essential cookie p3_currency, set by the ₪/$ switch) first, then the
    country of their connection (Cloudflare's CF-IPCountry header; read per request, never stored) — Israel → ₪;
  * the delivery country at checkout is final: Israel → ₪ including VAT, any other country → USD without Israeli VAT;
  * a visitor from Israel who chooses $ sees US dollars including Israeli VAT, to the cent (owner, 4 Nov 2026): a price to
    read only — a delivery to Israel is still charged in ₪, one abroad in $ without VAT (UsdVatMarket);
  * every price list stays in USD; a ₪ price is computed from it: USD × the price rate (the Bank of Israel rate plus the
    buffer) × (1 + VAT), rounded UP to whole shekels ending in 9 (owner decisions D1, D5, D7 of 3 Nov 2026). Rule 1 (round
    before VAT, then add VAT) stays a setting;
  * the price rate moves only when the effective rate has moved by the threshold since the last change, or on the weekly
    day (D6) — never per page view. The Bank of Israel rate is fetched once a day (SDMX, then the PublicApi) and kept with
    its date and source (fx_rates); a manual rate overrides it until its end date.

Every settings save is a version (who, when, note) and can be restored, like the price lists. An order keeps the ₪ figures
it was placed at, with the rate, its date and source, the buffer, the VAT and the rule (orders.fx_json): a later change
never touches it.
"""

import asyncio
import csv
import io
import json
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from p3.db import Database, Dumps, Now

Logger = logging.getLogger("p3.ilspricing")

Schema = """
CREATE TABLE IF NOT EXISTS ils_pricing_settings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    settings_json TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    note          TEXT
);
CREATE TABLE IF NOT EXISTS fx_rates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rate_date   TEXT NOT NULL,
    rate        TEXT NOT NULL,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    UNIQUE (rate_date, source)
);
CREATE TABLE IF NOT EXISTS ils_price_rates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rate        TEXT NOT NULL,
    base_rate   TEXT NOT NULL,
    base_date   TEXT,
    source      TEXT NOT NULL,
    buffer_pct  TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    created_by  TEXT NOT NULL
);
"""

Ils, Usd = "ILS", "USD"
CookieName = "p3_currency"
try:
    Israel = ZoneInfo("Asia/Jerusalem")
except Exception:  # noqa: BLE001 — no time zone data (a Windows checkout without tzdata): Israel standard time is close enough
    Israel = timezone(timedelta(hours=2), "IST")
SdmxUrl = ("https://edge.boi.gov.il/FusionEdgeServer/sdmx/v2/data/dataflow/BOI.STATISTICS/EXR/1.0/RER_USD_ILS"
           "?lastNObservations=1&format=csv")
PublicApiUrl = "https://boi.org.il/PublicApi/GetExchangeRate?key=USD"
Endings = {"9": "ending in 9 (…339, …349)", "49_99": "ending in 49 or 99", "0": "whole tens (…340, …350)",
           "1": "whole shekels"}
Stages = {"after_vat": "Round the total including VAT (Rule 2)", "before_vat": "Round the price before VAT, then add VAT (Rule 1)"}
Weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# The owner's decisions of 3 Nov 2026 (B6 D1–D10); the switch itself starts OFF
Defaults = {
    "enabled": False,
    "rate_mode": "auto",                 # "auto": the Bank of Israel rate; "manual": manual_rate until manual_until
    "manual_rate": None, "manual_until": None, "manual_reason": "",
    "buffer_pct": 2.0,                   # D5
    "reprice_threshold_pct": 1.5,        # D6: shown prices move when the effective rate moved this much…
    "reprice_weekday": "sunday",         # …or once a week on this day ("" = never by date)
    "stage": "after_vat",                # D1: Rule 2
    "ending": "9",                       # D7
    "vat_pct": 18.0, "vat_next_pct": None, "vat_next_from": None,
    "fetch_hour": 16, "stale_business_days": 4,
    "countries": ["IL"],                 # delivery and connection countries priced in ₪
    "geo_header": "CF-IPCountry",
}


class IlsPricingError(ValueError):
    pass


def _Dec(V) -> Decimal:
    return Decimal(str(V))


def Q2(X: Decimal) -> Decimal:
    return X.quantize(Decimal("0.01"), ROUND_HALF_UP)


def RoundUp(X: Decimal, Ending: str) -> Decimal:
    """Up, always, to the ending: '9' → …9 per 10, '49_99' → …49 or …99, '0' → whole tens, '1' → whole shekels. An exact
    match stays (₪349.00 → ₪349)."""
    N = math.ceil(X)
    if Ending == "9":
        return Decimal(math.ceil((N - 9) / 10) * 10 + 9)
    if Ending == "49_99":
        return Decimal(math.ceil((N - 49) / 50) * 50 + 49)
    if Ending == "0":
        return Decimal(math.ceil(N / 10) * 10)
    return Decimal(N)


def PriceIls(UsdAmount, Rate: Decimal, VatPct: Decimal, Stage: str, Ending: str) -> dict:
    """One USD list price → the ₪ customer price {total, net, vat} (Decimal). Rate already includes the buffer."""
    Vat = VatPct / 100
    Converted = _Dec(UsdAmount) * Rate
    if Stage == "before_vat":
        Net = RoundUp(Converted, Ending)
        V = Q2(Net * Vat)
        return {"converted": Q2(Converted), "net": Net, "vat": V, "total": Net + V}
    Total = RoundUp(Converted * (1 + Vat), Ending)
    Net = Q2(Total / (1 + Vat))
    return {"converted": Q2(Converted), "net": Net, "vat": Total - Net, "total": Total}


def Validate(Doc: dict) -> dict:
    """A settings document from the Admin, checked and completed with the defaults."""
    if not isinstance(Doc, dict):
        raise IlsPricingError("The settings must be an object.")
    S = {**Defaults, **{K: V for K, V in Doc.items() if K in Defaults}}

    def Num(Name, Low, High, Optional=False):
        V = S[Name]
        if V in (None, "") and Optional:
            S[Name] = None
            return
        try:
            V = float(V)
        except (TypeError, ValueError):
            raise IlsPricingError(f"{Name}: a number is needed.") from None
        if not Low <= V <= High:
            raise IlsPricingError(f"{Name}: between {Low:g} and {High:g}.")
        S[Name] = V

    S["enabled"] = S["enabled"] is True
    if S["rate_mode"] not in ("auto", "manual"):
        raise IlsPricingError("rate_mode: auto or manual.")
    Num("manual_rate", 1, 10, Optional=True)
    if S["rate_mode"] == "manual" and S["manual_rate"] is None:
        raise IlsPricingError("A manual rate is needed in manual mode.")
    for Name in ("manual_until", "vat_next_from"):
        if S[Name] in (None, ""):
            S[Name] = None
            continue
        try:
            date.fromisoformat(str(S[Name]))
        except ValueError:
            raise IlsPricingError(f"{Name}: a date (YYYY-MM-DD).") from None
        S[Name] = str(S[Name])
    S["manual_reason"] = str(S["manual_reason"] or "")[:200]
    Num("buffer_pct", 0, 10)
    Num("reprice_threshold_pct", 0, 20)
    if S["reprice_weekday"] not in ("",) + Weekdays:
        raise IlsPricingError("reprice_weekday: a day of the week, or empty.")
    if S["stage"] not in Stages:
        raise IlsPricingError("stage: after_vat or before_vat.")
    if S["ending"] not in Endings:
        raise IlsPricingError("ending: 9, 49_99, 0 or 1.")
    Num("vat_pct", 0, 50)
    Num("vat_next_pct", 0, 50, Optional=True)
    if (S["vat_next_pct"] is None) != (S["vat_next_from"] is None):
        raise IlsPricingError("A scheduled VAT rate needs both its rate and its date.")
    Num("fetch_hour", 0, 23)
    S["fetch_hour"] = int(S["fetch_hour"])
    Num("stale_business_days", 1, 30)
    S["stale_business_days"] = int(S["stale_business_days"])
    Codes = S["countries"] if isinstance(S["countries"], list) else str(S["countries"]).replace(",", " ").split()
    S["countries"] = sorted({str(C).strip().upper() for C in Codes if len(str(C).strip()) == 2}) or ["IL"]
    S["geo_header"] = str(S["geo_header"] or "CF-IPCountry").strip()[:60]
    return S


@dataclass
class Market:
    """How one answer prices: in ₪ at this rate, VAT and rule. Absent (None): USD, as before."""
    rate: Decimal                      # the price rate: the base rate with the buffer
    vat_pct: Decimal
    stage: str
    ending: str
    info: dict                         # the rate's base, date, source, buffer and when it was set (kept with an order)
    currency: str = Ils

    def Unit(self, UsdAmount) -> float | None:
        if UsdAmount is None:
            return None
        return float(PriceIls(UsdAmount, self.rate, self.vat_pct, self.stage, self.ending)["total"])

    def Plain(self, UsdAmount) -> float:
        """An amount that is not a price (a fixed promo discount): converted with VAT, to the agora, never rounded up."""
        return float(Q2(_Dec(UsdAmount) * self.rate * (1 + self.vat_pct / 100)))

    def VatOf(self, Total) -> float:
        """The VAT inside a total including VAT (the tax invoice's split)."""
        T = _Dec(Total)
        return float(T - Q2(T / (1 + self.vat_pct / 100)))

    def Record(self) -> dict:
        return {"currency": Ils, **self.info, "rate": str(self.rate), "vat_pct": float(self.vat_pct), "stage": self.stage,
                "ending": self.ending}


@dataclass
class UsdVatMarket(Market):
    """US dollars including Israeli VAT, to the cent ($49 → $57.82): what a visitor from Israel sees after choosing $ on
    the ₪/$ switch (owner, 4 Nov 2026). A price to read: the delivery country decides what is charged (DeliveryCurrency)."""
    currency: str = Usd

    def Unit(self, UsdAmount) -> float | None:
        if UsdAmount is None:
            return None
        return float(Q2(_Dec(UsdAmount) * (1 + self.vat_pct / 100)))

    def Plain(self, UsdAmount) -> float:
        return float(Q2(_Dec(UsdAmount) * (1 + self.vat_pct / 100)))

    def Record(self) -> dict:
        return {"currency": Usd, "vat_pct": float(self.vat_pct), "display_only": True}


class IlsPricing:
    def __init__(self, Db: Database, Fetcher=None):
        self.Db = Db
        self.Fetcher = Fetcher or FetchBankOfIsrael      # replaceable (tests): () → (date, rate, source)
        self.HeaderSeenAt = None                        # when a request last carried the country header (Admin status)
        with Db.Connect() as Conn:
            Conn.executescript(Schema)
        if not Db.One("SELECT id FROM ils_pricing_settings LIMIT 1"):
            self.Db.Execute("INSERT INTO ils_pricing_settings (settings_json, created_at, created_by, note) VALUES (?,?,?,?)",
                            (Dumps(Validate({})), Now(), "seed", "Israeli pricing: OFF; the owner's decisions of 3 Nov 2026 as defaults"))

    # ── settings ─────────────────────────────────────────────────────────
    def Settings(self) -> dict:
        R = self.Db.One("SELECT * FROM ils_pricing_settings ORDER BY id DESC LIMIT 1")
        return {**Validate(json.loads(R["settings_json"])), "version": f"ils-v{R['id']}", "updated_at": R["created_at"],
                "updated_by": R["created_by"], "update_note": R["note"]}

    def Enabled(self) -> bool:
        return self.Settings()["enabled"]

    def History(self, Limit: int = 20) -> list[dict]:
        return self.Db.All("SELECT id, created_at, created_by, note, settings_json FROM ils_pricing_settings ORDER BY id DESC LIMIT ?",
                           (Limit,))

    def Save(self, Doc: dict, By: str, Note: str = "") -> dict:
        Clean = Validate(Doc)
        self.Db.Execute("INSERT INTO ils_pricing_settings (settings_json, created_at, created_by, note) VALUES (?,?,?,?)",
                        (Dumps(Clean), Now(), By, Note or "Edited in Admin"))
        if not self.PriceRate() or self._BufferChanged(Clean):
            self.Reprice(By, "settings saved", Force=True)
        return self.Settings()

    def Restore(self, VersionId: int, By: str) -> dict:
        R = self.Db.One("SELECT settings_json FROM ils_pricing_settings WHERE id = ?", (VersionId,))
        if not R:
            raise IlsPricingError("No such version.")
        return self.Save(json.loads(R["settings_json"]), By, f"Restored version ils-v{VersionId}")

    def _BufferChanged(self, S: dict) -> bool:
        P = self.PriceRate()
        return bool(P) and _Dec(P["buffer_pct"]) != _Dec(S["buffer_pct"])

    # ── rates ────────────────────────────────────────────────────────────
    def LatestRate(self) -> dict | None:
        return self.Db.One("SELECT * FROM fx_rates ORDER BY rate_date DESC, id DESC LIMIT 1")

    def Rates(self, Limit: int = 30) -> list[dict]:
        return self.Db.All("SELECT * FROM fx_rates ORDER BY rate_date DESC, id DESC LIMIT ?", (Limit,))

    def Fetch(self) -> dict:
        """The latest Bank of Israel rate, stored once per date and source. A failure is reported, never invented."""
        try:
            Day, Rate, Source = self.Fetcher()
        except Exception as E:  # noqa: BLE001 — both official sources down: prices keep the last good rate
            Logger.warning("Bank of Israel rate: not fetched (%s)", E)
            return {"ok": False, "error": str(E)[:200]}
        self.Db.Execute("INSERT OR IGNORE INTO fx_rates (rate_date, rate, source, fetched_at) VALUES (?,?,?,?)",
                        (Day, str(Rate), Source, Now()))
        return {"ok": True, "rate_date": Day, "rate": str(Rate), "source": Source}

    def BaseRate(self, Settings: dict | None = None, Today: date | None = None) -> dict | None:
        """The rate prices start from: the manual rate while it applies, else the latest Bank of Israel rate stored."""
        S = Settings or self.Settings()
        Today = Today or datetime.now(Israel).date()
        if S["rate_mode"] == "manual" and S["manual_rate"] and (not S["manual_until"] or Today <= date.fromisoformat(S["manual_until"])):
            return {"rate": _Dec(S["manual_rate"]), "date": None, "source": "manual"}
        R = self.LatestRate()
        return {"rate": _Dec(R["rate"]), "date": R["rate_date"], "source": R["source"]} if R else None

    def EffectiveRate(self, Settings: dict | None = None) -> dict | None:
        S = Settings or self.Settings()
        B = self.BaseRate(S)
        if not B:
            return None
        return {**B, "buffer_pct": S["buffer_pct"], "effective": (B["rate"] * (1 + _Dec(S["buffer_pct"]) / 100)).quantize(Decimal("0.0001"), ROUND_HALF_UP)}

    def PriceRate(self) -> dict | None:
        return self.Db.One("SELECT * FROM ils_price_rates ORDER BY id DESC LIMIT 1")

    def PriceRates(self, Limit: int = 20) -> list[dict]:
        return self.Db.All("SELECT * FROM ils_price_rates ORDER BY id DESC LIMIT ?", (Limit,))

    def Reprice(self, By: str = "system", Reason: str = "", Force: bool = False, Now_: datetime | None = None) -> dict | None:
        """Moves the price rate to the effective rate when it has moved by the threshold since the last change, on the
        weekly day (once that day), or when forced (the Admin's "Apply now", a buffer change). Returns the new row or None."""
        S = self.Settings()
        E = self.EffectiveRate(S)
        if not E:
            return None
        P = self.PriceRate()
        When = Now_ or datetime.now(Israel)
        Why = Reason or "forced"
        if P and not Force:
            Old = _Dec(P["rate"])
            Moved = abs(E["effective"] - Old) / Old * 100
            Last = datetime.fromisoformat(P["created_at"].replace("Z", "+00:00")).astimezone(Israel)
            Weekly = S["reprice_weekday"] and Weekdays[When.weekday()] == S["reprice_weekday"] and Last.date() < When.date()
            if E["effective"] == Old:
                return None
            if Moved >= _Dec(S["reprice_threshold_pct"]):
                Why = f"the rate moved {Moved.quantize(Decimal('0.01'))}%"
            elif Weekly:
                Why = "weekly update"
            else:
                return None
        self.Db.Execute("INSERT INTO ils_price_rates (rate, base_rate, base_date, source, buffer_pct, reason, created_at, created_by) "
                        "VALUES (?,?,?,?,?,?,?,?)", (str(E["effective"]), str(E["rate"]), E["date"], E["source"],
                                                    str(S["buffer_pct"]), Why, Now(), By))
        return self.PriceRate()

    def VatPct(self, S: dict, Today: date | None = None) -> Decimal:
        Today = Today or datetime.now(Israel).date()
        if S["vat_next_pct"] is not None and Today >= date.fromisoformat(S["vat_next_from"]):
            return _Dec(S["vat_next_pct"])
        return _Dec(S["vat_pct"])

    def Status(self) -> dict:
        """What the Admin card shows: the base, effective and price rates, staleness and the header's arrival."""
        S = self.Settings()
        R = self.LatestRate()
        E = self.EffectiveRate(S)
        P = self.PriceRate()
        Stale = False
        if R:
            Stale = BusinessDaysSince(date.fromisoformat(R["rate_date"]), datetime.now(Israel).date()) > S["stale_business_days"]
        return {"latest_rate": R, "stale": Stale and S["rate_mode"] == "auto",
                "effective_rate": {**E, "rate": str(E["rate"]), "effective": str(E["effective"])} if E else None,
                "price_rate": P, "vat_pct_today": float(self.VatPct(S)), "header_seen_at": self.HeaderSeenAt,
                "ready": bool(P)}

    # ── markets ──────────────────────────────────────────────────────────
    def MarketFor(self, Currency: str) -> Market | None:
        """₪ for ILS while the switch is on and a price rate exists; None (USD) otherwise."""
        if Currency != Ils:
            return None
        S = self.Settings()
        P = self.PriceRate()
        if not S["enabled"] or not P:
            return None
        return Market(rate=_Dec(P["rate"]), vat_pct=self.VatPct(S), stage=S["stage"], ending=S["ending"],
                      info={"base_rate": P["base_rate"], "base_date": P["base_date"], "source": P["source"],
                            "buffer_pct": float(P["buffer_pct"]), "price_rate_id": P["id"], "price_rate_set_at": P["created_at"],
                            "settings_version": S["version"]})

    def VisitorCurrency(self, Headers, Cookies) -> str:
        """The visitor's own choice (the ₪/$ switch), else their connection's country. Off: always USD."""
        S = self.Settings()
        if not S["enabled"]:
            return Usd
        Chosen = str(Cookies.get(CookieName) or "").upper()
        if Chosen in (Ils, Usd):
            return Chosen
        Country = str(Headers.get(S["geo_header"]) or "").strip().upper()
        if Country:
            self.HeaderSeenAt = Now()
        return Ils if Country in S["countries"] else Usd

    def VisitorMarket(self, Headers, Cookies) -> Market | None:
        """How prices read before a delivery country is known: ₪ including VAT for the ₪ choice; for a visitor from Israel
        who chose $, dollars including Israeli VAT; anyone else, plain USD (None). Off: None."""
        Cur = self.VisitorCurrency(Headers, Cookies)
        if Cur == Ils:
            return self.MarketFor(Ils)
        S = self.Settings()
        if not S["enabled"]:
            return None
        if str(Headers.get(S["geo_header"]) or "").strip().upper() not in S["countries"]:
            return None
        return UsdVatMarket(rate=Decimal(1), vat_pct=self.VatPct(S), stage="exact", ending="", info={})

    def DeliveryCurrency(self, Country: str | None) -> str | None:
        """The delivery country decides at checkout (None: no country yet, the visitor's currency stands). Off: USD."""
        S = self.Settings()
        if not S["enabled"]:
            return Usd
        if not Country:
            return None
        return Ils if str(Country).strip().upper() in S["countries"] else Usd

    # ── the Admin's simulator ────────────────────────────────────────────
    def Simulate(self, Items: list[dict], Proposed: dict | None = None) -> dict:
        """Every list price under the current settings and, when given, the proposed ones, side by side (Decimal)."""
        Cur = self.Settings()

        def Under(S):
            E = self.EffectiveRate(S)
            if not E:
                return None, None
            Rate = E["effective"] if S is not Cur or not self.PriceRate() else _Dec(self.PriceRate()["rate"])
            return Rate, self.VatPct(S)

        Rows = []
        A = Under(Cur)
        B = Under(Validate({**Cur, **Proposed})) if Proposed else (None, None)
        Pv = Validate({**Cur, **Proposed}) if Proposed else None
        for It in Items:
            Row = {**It}
            if A[0] is not None and It.get("usd") is not None:
                P = PriceIls(It["usd"], A[0], A[1], Cur["stage"], Cur["ending"])
                Row["current"] = {K: str(V) for K, V in P.items()}
            if Pv and B[0] is not None and It.get("usd") is not None:
                P = PriceIls(It["usd"], B[0], B[1], Pv["stage"], Pv["ending"])
                Row["proposed"] = {K: str(V) for K, V in P.items()}
                if "current" in Row:
                    Row["difference"] = str(_Dec(Row["proposed"]["total"]) - _Dec(Row["current"]["total"]))
            Rows.append(Row)
        return {"rows": Rows, "current_rate": str(A[0]) if A[0] is not None else None,
                "proposed_rate": str(B[0]) if B[0] is not None else None}

    # ── the daily fetch ──────────────────────────────────────────────────
    async def Loop(self, Interval: float = 1800.0) -> None:
        """While the switch is on: fetch the day's rate once it is published (Monday–Friday after the fetch hour, Israel
        time; again every interval until one is stored for today), then let the price rate follow the rules."""
        while True:
            try:
                await asyncio.sleep(Interval)
                S = self.Settings()
                if not S["enabled"]:
                    continue
                Here = datetime.now(Israel)
                R = self.LatestRate()
                if Here.weekday() < 5 and Here.hour >= S["fetch_hour"] and (not R or R["rate_date"] < Here.date().isoformat()):
                    await asyncio.to_thread(self.Fetch)
                self.Reprice("system")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the loop never stops prices; the Admin sees the stale rate
                Logger.exception("Israeli pricing: daily step failed")


def BusinessDaysSince(Start: date, End: date) -> int:
    """Israeli business days (Sunday–Thursday) after Start up to End."""
    N, D = 0, Start
    while D < End:
        D += timedelta(days=1)
        if D.weekday() not in (4, 5):        # Friday, Saturday
            N += 1
    return N


def FetchBankOfIsrael(Timeout: float = 15.0) -> tuple[str, Decimal, str]:
    """The representative USD/ILS rate: SDMX first, the PublicApi second. Raises when both fail."""
    import httpx
    Errors = []
    try:
        R = httpx.get(SdmxUrl, timeout=Timeout)
        R.raise_for_status()
        Row = next(csv.DictReader(io.StringIO(R.text)))
        Rate = _Dec(Row["OBS_VALUE"])
        if not Decimal("1") < Rate < Decimal("10"):
            raise ValueError(f"implausible rate {Rate}")
        return date.fromisoformat(Row["TIME_PERIOD"]).isoformat(), Rate, "boi_sdmx"
    except Exception as E:  # noqa: BLE001
        Errors.append(f"SDMX: {E}")
    try:
        R = httpx.get(PublicApiUrl, timeout=Timeout)
        R.raise_for_status()
        J = R.json()
        Rate = _Dec(J["currentExchangeRate"])
        if not Decimal("1") < Rate < Decimal("10"):
            raise ValueError(f"implausible rate {Rate}")
        Day = datetime.fromisoformat(J["lastUpdate"][:19]).replace(tzinfo=timezone.utc).astimezone(Israel).date()
        return Day.isoformat(), Rate, "boi_publicapi"
    except Exception as E:  # noqa: BLE001
        Errors.append(f"PublicApi: {E}")
    raise RuntimeError("; ".join(Errors))


# ── answers in ₪: the USD figures a service computed, converted at the edge (M None: unchanged) ──
def LocalizeQuote(Q: dict, M: Market | None) -> dict:
    """A price quote ({unit_price, currency}) including VAT (₪, or $ for a visitor from Israel); the USD list price it
    came from rides along."""
    if not M or not isinstance(Q, dict) or Q.get("currency") != Usd:
        return Q
    return {**Q, "unit_price": M.Unit(Q.get("unit_price")), "currency": M.currency, "usd_unit_price": Q.get("unit_price"),
            "incl_vat": True}


def LocalizeCustomization(Out: dict, M: Market | None) -> dict:
    """What Customize shows: its quote, line total and, for a charm, each size's price."""
    if not M or not isinstance(Out, dict) or "quote" not in Out:
        return Out
    Q = LocalizeQuote(Out["quote"], M)
    Out = {**Out, "quote": Q}
    if Out.get("line_total") is not None and Q.get("unit_price") is not None:
        Out["line_total"] = round(Q["unit_price"] * Out["quantity"], 2)
    if Out.get("charm_sizes"):
        Out["charm_sizes"] = [{**S, "unit_price": M.Unit(S.get("unit_price")), "currency": M.currency} for S in Out["charm_sizes"]]
    return Out


def LocalizeBag(B: dict, M: Market | None) -> dict:
    """The bag: each line's price and total, and the total of what can be ordered."""
    if not M or not isinstance(B, dict) or "lines" not in B:
        return B
    Lines, Totals = [], {}
    for L in B["lines"]:
        if L.get("currency") == Usd:
            U = M.Unit(L["unit_price"])
            L = {**L, "unit_price": U, "currency": M.currency, "line_total": round(U * L["quantity"], 2),
                 "current_unit_price": M.Unit(L.get("current_unit_price"))}
        if L.get("orderable"):
            Totals[L["currency"]] = round(Totals.get(L["currency"], 0) + L["line_total"], 2)
        Lines.append(L)
    return {**B, "lines": Lines, "totals": Totals, "incl_vat": True}


def LocalizeCatalog(Out: dict, M: Market | None) -> dict:
    """The catalog's charm price table (the Materials page)."""
    Charm = ((Out.get("products") or {}).get("charm") or {})
    if M and Charm.get("materials"):
        for Mat in Charm["materials"]:
            Mat["prices"] = [{**P, "price": M.Unit(P["price"]), "currency": M.currency} if P.get("currency") == Usd else P
                             for P in Mat["prices"]]
    return Out
