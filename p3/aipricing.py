"""AI price list and per-job cost estimates (list prices, not invoices).

fal.ai bills each endpoint in its own unit. The price list stores, per endpoint, the unit price and
how a P3 job is converted into units:

  * nano-banana-pro (+ /edit):       per image (every P3 request is one image, 1K)
  * minimax/h3-max/camera-controls:  per second of video, by resolution × the movie's duration
  * hitem3d/hi3d/v3.0/image-to-3d:   per credit; credits = geometry (by resolution) + texture + PBR

Seeded from the fal.ai pricing API (GET https://api.fal.ai/v1/models/pricing) and the model pages
(2026-10-01). "Refresh from fal.ai" updates the unit prices from the pricing API; every value is
editable in Admin. Costs are always labelled as estimates: account discounts, promotions and
resolution surcharges not listed here are not reflected. Mock jobs cost $0.
"""

import json
import re
from datetime import datetime, timezone

import httpx

from p3.db import Database, Dumps, Now
from p3.providers import endpoints

PricingApi = "https://api.fal.ai/v1/models/pricing"
# The movie's earlier list: fal.ai's launch rates, which ended on 30 Sep 2026 (replaced on start-up where unedited)
ExpiredMovieRates = {"480P": 0.025, "768P": 0.04, "1080P": 0.08}

DefaultPriceList = {
    "currency": "USD",
    "source": "fal.ai pricing API + model pages, 2026-10-01; the movie: fal.ai model documentation, 2026-10-09",
    "endpoints": {
        endpoints.ImageGenerate: {"unit": "image", "per_image": 0.15,
                                  "note": "Per generated image; P3 requests one 1K image per request."},
        endpoints.ImageEdit: {"unit": "image", "per_image": 0.15,
                              "note": "Per generated image; P3 requests one 1K image per request."},
        endpoints.Movie: {"unit": "second", "per_second": {"480P": 0.03, "768P": 0.048, "1080P": 0.096},
                          "scheduled": {"from": "2026-10-15", "per_second": {"480P": 0.05, "768P": 0.08, "1080P": 0.16}},
                          "note": "Per second of video by resolution (fal.ai's documentation of this model, checked "
                                  "2026-10-09): promotional rates, 40% off, until 15 Oct 2026; then the scheduled list rates."},
        endpoints.Mesh: {"unit": "credit", "per_credit": 0.02,
                         "credits": {"geometry": {"2048quality": 90, "2048master": 440}, "texture": 10, "pbr": 5},
                         "note": "Credits = geometry (by resolution) + texture (if enabled) + PBR (if enabled); "
                                 "fal.ai: $2.10 (2048quality) / $9.10 (2048master) with texture and PBR."},
        "fal-ai/any-llm": {"unit": "request", "per_request": 0.001, "note": "The prompt check, where it is on (p3/promptcheck.py)."},
    },
}

Schema = """
CREATE TABLE IF NOT EXISTS ai_price_lists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    price_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);
"""


class PriceError(ValueError):
    pass


def _Num(V, What: str) -> float:
    if isinstance(V, bool) or not isinstance(V, (int, float)) or V < 0:
        raise PriceError(f"{What} must be a number ≥ 0.")
    return float(V)


def _ValidateEntry(Ep: str, P: dict) -> None:
    for K in ("per_image", "per_credit", "per_request"):
        if K in P:
            _Num(P[K], f"{Ep} {K}")
    if "per_second" in P:
        if not isinstance(P["per_second"], dict):
            raise PriceError(f"{Ep} per_second must map resolution → price.")
        for R, V in P["per_second"].items():
            _Num(V, f"{Ep} per_second[{R}]")
    if "credits" in P:
        C = P["credits"]
        _Num(C.get("texture", 0), f"{Ep} credits.texture")
        _Num(C.get("pbr", 0), f"{Ep} credits.pbr")
        for R, V in (C.get("geometry") or {}).items():
            _Num(V, f"{Ep} credits.geometry[{R}]")


def ValidatePriceList(Doc: dict) -> dict:
    if not isinstance(Doc, dict) or not isinstance(Doc.get("endpoints"), dict):
        raise PriceError("The price list must have an 'endpoints' object.")
    for Ep, P in Doc["endpoints"].items():
        if not isinstance(P, dict):
            raise PriceError(f"{Ep}: must be an object.")
        _ValidateEntry(Ep, P)
        if "scheduled" in P:                     # a dated change: these prices apply from that UTC day on
            Sc = P["scheduled"]
            if not isinstance(Sc, dict) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(Sc.get("from") or "")):
                raise PriceError(f"{Ep} scheduled must have a 'from' date (YYYY-MM-DD) and the prices that apply from it.")
            _ValidateEntry(f"{Ep} scheduled", Sc)
    return Doc


def Effective(P: dict, Day: str | None = None) -> dict:
    """An entry's prices on a UTC day: a scheduled change applies from its date (e.g. when a promotional rate ends)."""
    Sc = P.get("scheduled")
    Day = Day or datetime.now(timezone.utc).date().isoformat()
    if isinstance(Sc, dict) and Sc.get("from") and Day >= Sc["from"]:
        return {**{K: V for K, V in P.items() if K != "scheduled"}, **{K: V for K, V in Sc.items() if K != "from"}}
    return P


class PriceBook:
    def __init__(self, Db: Database):
        self.Db = Db
        with Db.Connect() as Conn:
            Conn.executescript(Schema)
        if not Db.One("SELECT id FROM ai_price_lists LIMIT 1"):
            self.Save(DefaultPriceList, "seed", "Initial list prices (fal.ai, 2026-10-01)")
        self._UpgradeExpiredMovieRates()

    def _UpgradeExpiredMovieRates(self) -> None:
        """A site still listing the movie at fal.ai's launch rates (expired 30 Sep 2026), unedited, moves to the official
        rates of the 2026-10-09 review — with the list rates scheduled from 15 Oct 2026 (docs/MOVIE-PRICING-2026-10-09.md).
        A list edited by hand is left as it is."""
        Cur = self.Current()
        E = Cur["endpoints"].get(endpoints.Movie)
        if not E or E.get("per_second") != ExpiredMovieRates or "scheduled" in E:
            return
        Doc = {K: V for K, V in Cur.items() if K not in ("version", "updated_at", "updated_by", "update_note")}
        Doc["endpoints"] = {**Doc["endpoints"], endpoints.Movie: json.loads(json.dumps(DefaultPriceList["endpoints"][endpoints.Movie]))}
        self.Save(Doc, "update", "Movie: fal.ai's official rates (2026-10-09 review) replace the expired launch rates; "
                                 "the list rates apply from 15 Oct 2026")

    def Current(self) -> dict:
        R = self.Db.One("SELECT * FROM ai_price_lists ORDER BY id DESC LIMIT 1")
        return {**json.loads(R["price_json"]), "version": R["id"], "updated_at": R["created_at"],
                "updated_by": R["created_by"], "update_note": R["note"]}

    def Save(self, Doc: dict, By: str, Note: str = "") -> dict:
        Doc = {K: V for K, V in Doc.items() if K not in ("version", "updated_at", "updated_by", "update_note")}
        ValidatePriceList(Doc)
        self.Db.Execute("INSERT INTO ai_price_lists (price_json, created_at, created_by, note) VALUES (?,?,?,?)",
                        (Dumps(Doc), Now(), By, Note))
        return self.Current()

    def RefreshFromFal(self, FalKey: str | None, By: str) -> dict:
        """Update unit prices from the fal.ai pricing API (read-only, free). Keeps the conversion
        rules (resolution ratios, credit counts) and scales per-resolution video prices."""
        if not FalKey:
            raise PriceError("No fal.ai key is configured on this server.")
        Doc = {K: V for K, V in self.Current().items() if K not in ("version", "updated_at", "updated_by", "update_note")}
        Ids = list(Doc["endpoints"])
        R = httpx.get(PricingApi, params=[("endpoint_id", I) for I in Ids],
                      headers={"Authorization": f"Key {FalKey}"}, timeout=30)
        if R.status_code != 200:
            raise PriceError(f"fal.ai pricing API returned HTTP {R.status_code}.")
        Changes = []
        for P in R.json().get("prices", []):
            E = Doc["endpoints"].get(P["endpoint_id"])
            if E is None:
                continue
            New = float(P["unit_price"])
            if "per_image" in E and E["per_image"] != New:
                Changes.append(f"{P['endpoint_id']}: {E['per_image']} → {New} per image"); E["per_image"] = New
            elif "per_credit" in E and E["per_credit"] != New:
                Changes.append(f"{P['endpoint_id']}: {E['per_credit']} → {New} per credit"); E["per_credit"] = New
            elif "per_request" in E and E["per_request"] != New:
                Changes.append(f"{P['endpoint_id']}: {E['per_request']} → {New} per request"); E["per_request"] = New
            elif "per_second" in E:
                Base = E["per_second"].get("480P")
                if Base and Base != New:                  # the API reports the base (480P) rate
                    Ratio = New / Base
                    E["per_second"] = {K: round(V * Ratio, 4) for K, V in E["per_second"].items()}
                    Changes.append(f"{P['endpoint_id']}: 480P {Base} → {New} per second (other resolutions scaled)")
        Doc["source"] = f"fal.ai pricing API, {Now()[:10]}"
        Saved = self.Save(Doc, By, "Refreshed from fal.ai: " + ("; ".join(Changes) if Changes else "no changes"))
        return {**Saved, "changes": Changes}

    # ── estimates ─────────────────────────────────────────────────────────
    def Estimate(self, Endpoint: str, Provider: str | None, Params: dict | None) -> dict:
        """Estimated list-price cost of ONE provider request with the given request parameters."""
        if Provider == "mock":
            return {"cost": 0.0, "basis": "mock (simulated, free)"}
        if Provider is None:
            return {"cost": 0.0, "basis": "not submitted"}
        P = self.Current()["endpoints"].get(Endpoint)
        Params = Params or {}
        if P is None:
            return {"cost": None, "basis": "no price for this endpoint"}
        P = Effective(P)
        if "per_image" in P:
            return {"cost": P["per_image"], "basis": f"1 image × ${P['per_image']}"}
        if "per_second" in P:
            Res = Params.get("resolution", "480P")
            Dur = Params.get("duration", 5)
            Rate = P["per_second"].get(Res)
            if Rate is None:
                return {"cost": None, "basis": f"no per-second price for {Res}"}
            return {"cost": round(Rate * Dur, 4), "basis": f"{Dur} s × ${Rate}/s ({Res})"}
        if "per_credit" in P:
            C = P.get("credits", {})
            Res = Params.get("resolution", "2048quality")
            Geo = (C.get("geometry") or {}).get(Res)
            if Geo is None:
                return {"cost": None, "basis": f"no credit count for {Res}"}
            Credits = Geo + (C.get("texture", 0) if Params.get("enable_texture", True) else 0) \
                + (C.get("pbr", 0) if Params.get("enable_pbr", True) else 0)
            return {"cost": round(Credits * P["per_credit"], 4), "basis": f"{Credits} credits × ${P['per_credit']}"}
        if "per_request" in P:
            return {"cost": P["per_request"], "basis": f"1 request × ${P['per_request']}"}
        return {"cost": None, "basis": "unknown unit"}
