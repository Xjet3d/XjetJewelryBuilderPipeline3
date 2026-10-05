"""Charm pricing — completely separate from ring pricing (Admin → Settings → Pricing & Materials → Charm).

A charm's customer price depends on its material AND its size. Per material a charm is made in:
  fixed_prices   the price the website shows, per charm size in mm ("20" → 145.0). Empty = not sold at that
                 size yet: the website shows "Price unavailable". Never a ring price, never a calculated number.
  price_per_g    3D calculated price = weight × price $/g      (Admin only)
  cost_per_g     production cost     = weight × cost $/g       (Admin only)
Weight = 3D volume × the material's sintered density: a property of the material, the same for a ring and a
charm (Admin → Pricing · materials). Every price and cost here is the charm's own.

The materials: Stainless Steel, Sterling Silver and 14K Gold Vermeil at a fixed price per size; gold through
the quote flow, exactly like a gold ring (no fixed price). Every save is a new version ("charms-v<N>": who,
when, note). The table starts empty — no charm price is invented — so every charm is "Price unavailable"
until an Admin sets its prices.
"""

import json
import math

from p3 import products as Products
from p3.db import Database, Dumps, Now
from p3.pricing.service import Quote

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
Labels = {"stainless_steel": "Stainless Steel", "silver": "Sterling Silver", "vermeil": "14K Gold Vermeil"}


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
    return {"price_per_g": None, "cost_per_g": None, "fixed_prices": {}}


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


def Validate(Doc: dict, Catalog) -> dict:
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
                 "cost_per_g": _Number(f"{Name}: cost $/g", Row.get("cost_per_g")), "fixed_prices": {}}
        Fixed = Row.get("fixed_prices") or {}
        if not isinstance(Fixed, dict):
            raise CharmPriceError(f"{Name}: fixed prices are given per size.")
        for Size, Price in Fixed.items():
            try:
                S = float(Size)
            except (TypeError, ValueError):
                raise CharmPriceError(f"{Name}: {Size!r} is not a size in mm.")
            if not math.isfinite(S) or not Products.CharmSizeMin <= S <= Products.CharmSizeMax:
                raise CharmPriceError(f"{Name}: sizes are between {Products.CharmSizeMin:g} and {Products.CharmSizeMax:g} mm.")
            P = _Number(f"{Name}: the {SizeKey(S)} mm price", Price)
            if P is None:
                continue
            if Mid not in FixedPriceMaterials:
                raise CharmPriceError(f"{Name}: gold charms are quoted individually and have no fixed price.")
            Clean["fixed_prices"][SizeKey(S)] = P
        Out[Mid] = Clean
    # Every charm material has a row: one left out of the table has no prices (shown as unavailable)
    Out = {M.Id: Out.get(M.Id) or _EmptyRow() for M in Offered(Catalog)}
    return {"currency": Doc.get("currency") or "USD", "materials": Out}


class CharmPriceBook:
    def __init__(self, Db: Database, Catalog, Settings):
        self.Db = Db
        self.Catalog = Catalog
        self.Settings = Settings                 # products.ProductSettings: the charm sizes on offer
        self.OnSave = []                         # callbacks after a new version (e.g. price charm 3D results that had no price)
        with Db.Connect() as Conn:
            Conn.executescript(Schema)
        if not Db.One("SELECT id FROM charm_price_lists LIMIT 1"):
            self.Save({"materials": {M.Id: _EmptyRow() for M in Offered(Catalog)}}, "seed",
                      "Initial charm pricing table: empty, no charm price is invented")

    def Current(self) -> dict:
        R = self.Db.One("SELECT * FROM charm_price_lists ORDER BY id DESC LIMIT 1")
        return {**json.loads(R["price_json"]), "version": f"charms-v{R['id']}", "updated_at": R["created_at"],
                "updated_by": R["created_by"], "update_note": R["note"]}

    def History(self, Limit: int = 20) -> list[dict]:
        return self.Db.All("SELECT id, created_at, created_by, note FROM charm_price_lists ORDER BY id DESC LIMIT ?", (Limit,))

    def Save(self, Doc: dict, By: str, Note: str = "") -> dict:
        Clean = Validate(Doc, self.Catalog)
        self.Db.Execute("INSERT INTO charm_price_lists (price_json, created_at, created_by, note) VALUES (?,?,?,?)",
                        (Dumps(Clean), Now(), By, Note))
        for Fn in self.OnSave:
            Fn()
        return self.Current()

    def Row(self, MaterialId: str) -> dict:
        return self.Current()["materials"].get(MaterialId) or _EmptyRow()

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
        if not self.Catalog.IsPurchasableGroup(Mat.Group):
            return Unavailable("luxury_pricing_unavailable")
        if Size is None:
            return Unavailable("charm_size_required")
        if not self.Settings.IsValidCharmSize(Size):
            return Unavailable("charm_size_not_offered")
        Price = (Doc["materials"].get(Mat.Id) or {}).get("fixed_prices", {}).get(SizeKey(Size))
        if not Price:
            return Unavailable("charm_price_not_set")
        return Quote(material_id=Mat.Id, material_group=Mat.Group, pricing_status="available",
                     unit_price=round(float(Price), 2), currency=Doc["currency"], assumed_volume_cm3=None,
                     pricing_version=Doc["version"], profile_approved=True,
                     notes=[f"Fixed charm price for {SizeKey(Size)} mm from Admin → Pricing · Charm."])

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
        """The table as the Admin edits it: one row per charm material, a fixed price per size on offer (and any
        other size that still has a price), price and cost per gram."""
        Doc = self.Current()
        Sizes = [float(S) for S in self.Settings.CharmSizes]
        Priced = {float(S) for R in Doc["materials"].values() for S in (R.get("fixed_prices") or {})}
        Rows = [{"id": M.Id, "label": Label(self.Catalog, M.Id), "group": M.Group, "fixed_price_allowed": M.Id in FixedPriceMaterials,
                 "density_g_cm3": M.DensityGCm3, **_EmptyRow(), **(Doc["materials"].get(M.Id) or {})} for M in Offered(self.Catalog)]
        return {**Doc, "history": self.History(), "rows": Rows, "sizes": Sizes,
                "other_priced_sizes": sorted(Priced - set(Sizes)), "size_definition": Products.CharmSizeDefinition}


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
