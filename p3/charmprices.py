"""Charm pricing — completely separate from ring pricing (Admin → Settings → Pricing & Materials → Charm).

A charm's customer price depends on its material AND its size. Per material a charm is made in:
  level_prices   the price the website shows, per level of size (5 Nov 2026): "1" the smallest size on offer, "2" the
                 middle, "3" the biggest — rising with the size. Empty = not sold at that size yet: the website shows
                 "Price unavailable". Never a ring price, never a calculated number. The documents saved before kept
                 a price per size in mm (fixed_prices, "14" → 69.0): read by the rank of their sizes. Every answer also
                 gives fixed_prices per size on offer, derived from the levels.
  price_per_g    3D calculated price = weight × price $/g      (Admin only)
  cost_per_g     production cost     = weight × cost $/g       (Admin only)
Weight = 3D volume × the material's sintered density: a property of the material, the same for a ring and a
charm (Admin → Pricing · materials). Every price and cost here is the charm's own.

The materials: Stainless Steel, Sterling Silver and 14K Gold over Stainless Steel at a fixed price per size; gold through
the quote flow, exactly like a gold ring (no fixed price). Every save is a new version ("charms-v<N>": who,
when, note). The table starts empty — no charm price is invented — so every charm is "Price unavailable"
until an Admin sets its prices.
"""

import json
import math

from p3 import products as Products
from p3.db import Database, Dumps, Now
from p3.pricing.service import Quote, UnpricedReason

Schema = """
CREATE TABLE IF NOT EXISTS charm_price_lists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    price_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    note        TEXT
);
"""
FixedPriceMaterials = ("stainless_steel", "silver", "vermeil")
# How a charm's material reads to the customer (rings keep the catalog's labels)
Labels = {"stainless_steel": "Stainless Steel", "silver": "Sterling Silver", "vermeil": "14K Gold over Stainless Steel"}


class CharmPriceError(ValueError):
    pass


def SizeKey(Size) -> str:
    """The table's key for a size in mm: "20", "22.5"."""
    return f"{float(Size):g}"


def Offered(Catalog) -> list:
    """The catalog materials a charm is made in: the three at a fixed price, then gold (quote flow)."""
    Out = [Catalog.Get(M) for M in FixedPriceMaterials if Catalog.Get(M)]
    return Out + [M for M in Catalog.Materials.values() if not Catalog.IsPurchasableGroup(M.Group)]


def IsOffered(Catalog, MaterialId: str) -> bool:
    return any(M.Id == MaterialId for M in Offered(Catalog))


def Label(Catalog, MaterialId: str) -> str:
    Mat = Catalog.Get(MaterialId)
    return Labels.get(MaterialId) or (Mat.Label if Mat else MaterialId)


def _EmptyRow() -> dict:
    return {"price_per_g": None, "cost_per_g": None, "level_prices": {}}


def _LegacyLevels(Fixed: dict, Sizes) -> dict:
    """A price per size in mm (a document saved before the levels) as a price per level: by the level of each size
    on offer when all its sizes are on offer, else by the rank of its own sizes (the smallest at the low price)."""
    Priced = sorted((float(K), V) for K, V in (Fixed or {}).items() if V not in (None, ""))
    On = [float(S) for S in Sizes]
    if Priced and all(S in On for S, _ in Priced):
        return {Products.CharmLevelOf(S, On): V for S, V in Priced}
    return {L: V for L, (_, V) in zip(Products.CharmLevels, Priced)}


def _Levels(Row: dict, Sizes) -> dict:
    """A row's prices per level, from a document of either kind."""
    if isinstance(Row.get("level_prices"), dict):
        return Row["level_prices"]
    return _LegacyLevels(Row.get("fixed_prices") or {}, Sizes)


def _WithSizes(Row: dict, Sizes) -> dict:
    """A row with its level prices and, derived from them, its price per size on offer (fixed_prices)."""
    Levels = _Levels(Row, Sizes)
    Fixed = {SizeKey(S): Levels[L] for S in Sizes for L in (Products.CharmLevelOf(S, Sizes),) if L and Levels.get(L)}
    return {**{K: V for K, V in Row.items() if K not in ("fixed_prices", "level_prices")}, "level_prices": Levels,
            "fixed_prices": Fixed}


def _Number(Name: str, Value):
    if Value in (None, ""):
        return None
    try:
        V = float(Value)
    except (TypeError, ValueError):
        raise CharmPriceError(f"{Name} must be a number or empty.")
    if not math.isfinite(V) or V <= 0:
        raise CharmPriceError(f"{Name} must be greater than 0 (leave it empty if unknown).")
    return V


def _SizeLevels(Name: str, Fixed, Sizes) -> dict:
    """Prices given per size in mm (the Admin's columns): each size on offer is its level; a price for a size not
    on offer is refused (a size not on offer has no price)."""
    if not isinstance(Fixed, dict):
        raise CharmPriceError(f"{Name}: fixed prices are given per size.")
    Out = {}
    for Size, Price in Fixed.items():
        try:
            S = float(Size)
        except (TypeError, ValueError):
            raise CharmPriceError(f"{Name}: {Size!r} is not a size in mm.")
        L = Products.CharmLevelOf(S, Sizes) if math.isfinite(S) else None
        if L is None:
            if Price in (None, ""):
                continue
            raise CharmPriceError(f"{Name}: {S:g} mm is not a size on offer ("
                                  + ", ".join(f"{float(X):g}" for X in sorted(Sizes)) + " mm); prices follow the sizes on offer.")
        Out[L] = Price
    return Out


def Validate(Doc: dict, Catalog, Sizes=Products.DefaultCharmSizes) -> dict:
    """A price table to save: per material its price and cost per gram and its three level prices (level_prices, or
    fixed_prices per size on offer), each above the one before — the biggest size at the highest price."""
    Mats = Doc.get("materials")
    if not isinstance(Mats, dict):
        raise CharmPriceError("The table needs a 'materials' object.")
    Out = {}
    for Mid, Row in Mats.items():
        if not IsOffered(Catalog, Mid):
            raise CharmPriceError(f"{Mid} is not a charm material.")
        Name = Label(Catalog, Mid)
        Row = Row or {}
        Clean = {"price_per_g": _Number(f"{Name}: price $/g", Row.get("price_per_g")),
                 "cost_per_g": _Number(f"{Name}: cost $/g", Row.get("cost_per_g")), "level_prices": {}}
        Levels = Row.get("level_prices")
        if Levels is None:
            Levels = _SizeLevels(Name, Row.get("fixed_prices") or {}, Sizes)
        if not isinstance(Levels, dict):
            raise CharmPriceError(f"{Name}: prices are given per level (1 small, 2 medium, 3 large).")
        for L, Price in Levels.items():
            if str(L) not in Products.CharmLevels:
                raise CharmPriceError(f"{Name}: {L!r} is not a price level (1 small, 2 medium, 3 large).")
            P = _Number(f"{Name}: the {Products.CharmLevelLabels[str(L)]} size's price", Price)
            if P is None:
                continue
            if Mid not in FixedPriceMaterials:
                raise CharmPriceError(f"{Name}: gold charms are quoted individually and have no fixed price.")
            Clean["level_prices"][str(L)] = P
        Clean["level_prices"] = {L: Clean["level_prices"][L] for L in Products.CharmLevels if L in Clean["level_prices"]}
        Set = list(Clean["level_prices"].values())
        if any(B <= A for A, B in zip(Set, Set[1:])):
            raise CharmPriceError(f"{Name}: the prices must rise with the size — small below medium below large.")
        Out[Mid] = Clean
    # Every charm material has a row: one left out of the table has no prices (shown as unavailable)
    Out = {M.Id: Out.get(M.Id) or _EmptyRow() for M in Offered(Catalog)}
    return {"currency": Doc.get("currency") or "USD", "materials": Out}


class CharmPriceBook:
    def __init__(self, Db: Database, Catalog, Settings):
        self.Db = Db
        self.Catalog = Catalog
        self.Settings = Settings                 # products.ProductSettings: the charm sizes on offer
        self.Status = None                       # material id → its charm status (p3/materialstatus.py); None: the group rule
        self.OnSave = []                         # callbacks after a new version (e.g. price charm 3D results that had no price)
        with Db.Connect() as Conn:
            Conn.executescript(Schema)
        Last = Db.One("SELECT price_json FROM charm_price_lists ORDER BY id DESC LIMIT 1")
        if not Last:
            self.Save({"materials": {M.Id: _EmptyRow() for M in Offered(Catalog)}}, "seed",
                      "Initial charm pricing table: empty, no charm price is invented")
        elif any("level_prices" not in R for R in json.loads(Last["price_json"]).get("materials", {}).values()):
            # A table saved per size in mm (before 5 Nov 2026): the same prices again, per level, as a new version
            Doc = json.loads(Last["price_json"])
            Sizes = self.Settings.CharmSizes
            try:
                self.Save({**Doc, "materials": {M: {**R, "level_prices": _Levels(R, Sizes)} for M, R in Doc["materials"].items()}},
                          "migration", "Charm prices per level of size (small, medium, large), carried over from the prices "
                          "per size (" + ", ".join(f"{S:g}" for S in Sizes) + " mm)")
            except CharmPriceError:          # a table the new rules refuse (prices not rising) stays as it is, read by rank
                pass

    def Current(self) -> dict:
        """The table in force, each row with its level prices and its price per size on offer (fixed_prices)."""
        R = self.Db.One("SELECT * FROM charm_price_lists ORDER BY id DESC LIMIT 1")
        Doc = json.loads(R["price_json"])
        Sizes = self.Settings.CharmSizes
        return {**Doc, "materials": {M: _WithSizes(Row, Sizes) for M, Row in (Doc.get("materials") or {}).items()},
                "version": f"charms-v{R['id']}", "updated_at": R["created_at"], "updated_by": R["created_by"],
                "update_note": R["note"]}

    def History(self, Limit: int = 20) -> list[dict]:
        return self.Db.All("SELECT id, created_at, created_by, note FROM charm_price_lists ORDER BY id DESC LIMIT ?", (Limit,))

    def Save(self, Doc: dict, By: str, Note: str = "") -> dict:
        Clean = Validate(Doc, self.Catalog, self.Settings.CharmSizes)
        self.Db.Execute("INSERT INTO charm_price_lists (price_json, created_at, created_by, note) VALUES (?,?,?,?)",
                        (Dumps(Clean), Now(), By, Note))
        for Fn in self.OnSave:
            Fn()
        return self.Current()

    def Row(self, MaterialId: str) -> dict:
        return self.Current()["materials"].get(MaterialId) or _WithSizes(_EmptyRow(), self.Settings.CharmSizes)

    def QuoteFor(self, MaterialId: str, Size) -> Quote:
        """One charm of this material and size at its fixed price — or why there is none. Never a ring price."""
        Mat = self.Catalog.Get(MaterialId)
        if Mat is None:
            raise KeyError(MaterialId)
        Doc = self.Current()

        def Unavailable(Reason: str) -> Quote:
            return Quote(material_id=Mat.Id, material_group=Mat.Group, pricing_status="unavailable", unit_price=None,
                         currency=Doc["currency"], assumed_volume_cm3=None, pricing_version=Doc["version"],
                         profile_approved=True, unavailable_reason=Reason)
        if not IsOffered(self.Catalog, Mat.Id):
            return Unavailable("material_not_offered")
        Reason = UnpricedReason(self.Status(Mat.Id) if self.Status else
                                ("available" if self.Catalog.IsPurchasableGroup(Mat.Group) else "quote"))
        if Reason:                                   # a price only for an available metal (WP3)
            return Unavailable(Reason)
        if Size is None:
            return Unavailable("charm_size_required")
        Level = self.Settings.CharmLevel(Size)
        if Level is None:
            return Unavailable("charm_size_not_offered")
        Price = (Doc["materials"].get(Mat.Id) or {}).get("level_prices", {}).get(Level)
        if not Price:
            return Unavailable("charm_price_not_set")
        return Quote(material_id=Mat.Id, material_group=Mat.Group, pricing_status="available",
                     unit_price=round(float(Price), 2), currency=Doc["currency"], assumed_volume_cm3=None,
                     pricing_version=Doc["version"], profile_approved=True,
                     notes=[f"Fixed charm price for {SizeKey(Size)} mm (the {Products.CharmLevelLabels[Level]} size) "
                            "from Admin → Pricing · Charm."])

    def Price3D(self, MaterialId: str, WeightG: float | None) -> dict:
        """A charm's production cost and 3D calculated price from its weight — charm $/g only, or why not."""
        R = self.Row(MaterialId)
        if WeightG is None:
            return {"status": "needs_review", "reason": "weight unavailable (volume not reliable)"}
        Cost = round(WeightG * R["cost_per_g"], 2) if R.get("cost_per_g") else None
        Price = round(WeightG * R["price_per_g"], 2) if R.get("price_per_g") else None
        Missing = [N for N, V in (("cost $/g", Cost), ("price $/g", Price)) if V is None]
        return {"status": "calculated" if not Missing else "cost_model_not_configured",
                "production_cost": Cost, "calculated_price": Price,
                "reason": f"charm {' and '.join(Missing)} not set for this material" if Missing else None,
                "breakdown": {"weight_g": WeightG, "cost_per_g": R.get("cost_per_g"), "price_per_g": R.get("price_per_g"),
                              "pricing": "charm"}}

    def AdminTable(self) -> dict:
        """The table as the Admin edits it: one row per charm material, its three level prices (as columns: the sizes on
        offer, smallest first — small, medium, large), price and cost per gram."""
        Doc = self.Current()
        Sizes = self.Settings.CharmSizes
        Rows = [{"id": M.Id, "label": Label(self.Catalog, M.Id), "group": M.Group, "fixed_price_allowed": M.Id in FixedPriceMaterials,
                 "density_g_cm3": M.DensityGCm3, **_WithSizes({**_EmptyRow(), **(Doc["materials"].get(M.Id) or {})}, Sizes)}
                for M in Offered(self.Catalog)]
        Names = self.Settings.CharmLevelNames
        Levels = [{"level": L, "label": Products.CharmLevelLabels[L], "size": S, "name": Names.get(L) or None}
                  for S in Sizes for L in (Products.CharmLevelOf(S, Sizes),) if L]
        return {**Doc, "history": self.History(), "rows": Rows, "sizes": Sizes, "levels": Levels,
                "size_names": self.Settings.CharmSizeNames, "default_size": self.Settings.CharmDefaultSize,
                "other_priced_sizes": [], "size_definition": Products.CharmSizeDefinition}


# ── one entry point for both products ───────────────────────────────────────────
def QuoteFor(Ctx, Product: str, MaterialId: str, CharmSize=None) -> Quote:
    """The price of one unit of a design's product: a ring from the ring pricing exactly as before, a charm from
    the charm price book only (never the other way round)."""
    if Product == Products.Charm:
        return Ctx.CharmPrices.QuoteFor(MaterialId, CharmSize)
    return Ctx.Pricing.QuoteFor(MaterialId)


def MaterialLabel(Ctx, Product: str, MaterialId: str) -> str:
    if Product == Products.Charm:
        return Label(Ctx.Catalog, MaterialId)
    Mat = Ctx.Catalog.Get(MaterialId)
    return Mat.Label if Mat else MaterialId
