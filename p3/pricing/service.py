"""Server-authoritative fixed-volume quote service (spec section 6).

Confirmed rules enforced here:
  * every fashion unit assumes 1.0 cm³;
  * the selected fashion material determines the price;
  * ring size never changes the quote (it is not an input);
  * Luxury has no numeric price (pricing_status=unavailable, unit_price=None);
  * a missing/invalid/zero price is "unavailable", never a fallback number.

What is NOT decided (and therefore configurable, not hard-coded): the numeric
profile. See docs/PRICING.md.
"""

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from p3.config import Catalog, ContentVersion
from p3.pricing import cpp

AssumedVolumeCm3 = 1.0


@dataclass
class Quote:
    material_id: str
    material_group: str
    pricing_status: str                  # "available" | "unavailable"
    unit_price: float | None
    currency: str
    assumed_volume_cm3: float
    pricing_version: str | None
    profile_approved: bool
    estimated_weight_g: float | None = None
    unavailable_reason: str | None = None
    notes: list = field(default_factory=list)

    @property
    def IsAvailable(self) -> bool:
        return self.pricing_status == "available"

    def ToJson(self) -> dict:
        return dict(self.__dict__)


class PricingProfileError(ValueError):
    pass


def UnpricedReason(Status: str) -> str | None:
    """Why a metal of this status has no price (p3/materialstatus.py), or None for an available metal. Quote Only keeps
    the reason gold always had."""
    return {"available": None, "quote": "luxury_pricing_unavailable", "preview": "material_preview_only"}.get(Status, "material_not_offered")


class PricingService:
    def __init__(self, CatalogObj: Catalog, ProfilePath: Path, AllowUnapproved: bool):
        self.Catalog = CatalogObj
        self.ProfilePath = ProfilePath
        self.AllowUnapproved = AllowUnapproved
        self.Profile = None
        self.ProfileError = None
        self.Book = None                         # MaterialPriceBook: fixed price per material (Admin table)
        self.Status = None                       # material id → its ring status (p3/materialstatus.py); None: the group rule
        try:
            self.Profile = self._LoadProfile(ProfilePath)
        except (OSError, ValueError) as E:
            # An invalid profile makes fashion pricing unavailable; it must not crash the app.
            self.ProfileError = str(E)

    @staticmethod
    def _LoadProfile(ProfilePath: Path) -> dict:
        Raw = json.loads(Path(ProfilePath).read_text(encoding="utf-8"))
        if not isinstance(Raw.get("version"), str) or not Raw["version"]:
            raise PricingProfileError("Pricing profile needs a non-empty 'version'")
        if float(Raw.get("assumed_volume_cm3", -1)) != AssumedVolumeCm3:
            raise PricingProfileError("Pricing profile must declare assumed_volume_cm3 = 1.0")
        if not isinstance(Raw.get("materials"), dict):
            raise PricingProfileError("Pricing profile needs a 'materials' object")
        Raw.setdefault("currency", "USD")
        Raw["approved"] = Raw.get("approved") is True
        return Raw

    @property
    def ProfileVersion(self) -> str | None:
        if not self.Profile:
            return None
        return f"{self.Profile['version']}+{ContentVersion('p', self.Profile)}"

    def _Unavailable(self, Mat, Reason: str, Notes=None) -> Quote:
        return Quote(material_id=Mat.Id, material_group=Mat.Group, pricing_status="unavailable",
                     unit_price=None, currency=(self.Profile or {}).get("currency", "USD"),
                     assumed_volume_cm3=AssumedVolumeCm3, pricing_version=self.ProfileVersion,
                     profile_approved=bool(self.Profile and self.Profile["approved"]),
                     unavailable_reason=Reason, notes=list(Notes or []))

    def QuoteFor(self, MaterialId: str) -> Quote:
        """Quote one unit of `MaterialId`. Ring size is deliberately not a parameter."""
        Mat = self.Catalog.Get(MaterialId)
        if Mat is None:
            raise KeyError(MaterialId)
        Reason = UnpricedReason(self.Status(Mat.Id) if self.Status else
                                ("available" if self.Catalog.IsPurchasableGroup(Mat.Group) else "quote"))
        if Reason:                                   # a price only for an available metal (WP3)
            return self._Unavailable(Mat, Reason)
        Fixed = self._BookPrice(Mat)
        if Fixed is not None:
            return Fixed
        if self.Profile is None:
            return self._Unavailable(Mat, "pricing_profile_invalid",
                                     [self.ProfileError] if self.ProfileError else None)
        if not self.Profile["approved"] and not self.AllowUnapproved:
            return self._Unavailable(Mat, "pricing_profile_unapproved")

        Entry = self.Profile["materials"].get(MaterialId) or {}
        Method = Entry.get("method", "unconfigured")
        Notes = []
        if not self.Profile["approved"]:
            Notes.append("Unapproved development pricing profile — not a real price.")
        try:
            if Method == "fixed":
                UnitPrice = float(Entry["unit_price"])
                if Entry.get("source"):
                    Notes.append(f"Fixed price source: {Entry['source']}")
            elif Method == "cpp_reference":
                UnitPrice = self._CppPrice(Mat, Entry, Notes)
            else:
                return self._Unavailable(Mat, "pricing_not_configured")
        except (KeyError, TypeError, ValueError) as E:
            return self._Unavailable(Mat, "pricing_profile_invalid", [str(E)])

        if Mat.RequiresPlating:
            Plating = Entry.get("plating_cost_usd")
            if Plating is None:
                if self.Profile["approved"]:
                    # An approved profile must state vermeil plating explicitly (0 is allowed).
                    return self._Unavailable(Mat, "plating_cost_not_configured")
                Notes.append("Gold plating is not costed in this estimate.")
            else:
                UnitPrice += float(Plating)

        if not math.isfinite(UnitPrice) or UnitPrice <= 0:
            # A zero/negative/placeholder price never unlocks purchasing.
            return self._Unavailable(Mat, "pricing_profile_invalid", ["Non-positive unit price"])

        return Quote(material_id=Mat.Id, material_group=Mat.Group, pricing_status="available",
                     unit_price=round(UnitPrice, 2), currency=self.Profile["currency"],
                     assumed_volume_cm3=AssumedVolumeCm3, pricing_version=self.ProfileVersion,
                     profile_approved=self.Profile["approved"],
                     estimated_weight_g=round(AssumedVolumeCm3 * Mat.DensityGCm3, 2),
                     notes=Notes)

    def _BookPrice(self, Mat) -> Quote | None:
        """The fixed price set in Admin → Material pricing (an admin decision, so it counts as approved)."""
        if self.Book is None:
            return None
        Doc = self.Book.Current()
        Fixed = (Doc["materials"].get(Mat.Id) or {}).get("fixed_price")
        if not Fixed:
            return None
        return Quote(material_id=Mat.Id, material_group=Mat.Group, pricing_status="available",
                     unit_price=round(float(Fixed), 2), currency=Doc.get("currency", "USD"),
                     assumed_volume_cm3=AssumedVolumeCm3, pricing_version=Doc["version"], profile_approved=True,
                     estimated_weight_g=round(AssumedVolumeCm3 * Mat.DensityGCm3, 2),
                     notes=["Fixed price from Admin → Material pricing."])

    def _CppPrice(self, Mat, Entry: dict, Notes: list) -> float:
        Defaults = self.Profile.get("defaults", {})
        Dims = Entry.get("reference_dims_mm") or Defaults.get("reference_dims_mm")
        if not Dims:
            raise ValueError("cpp_reference requires reference_dims_mm (no default is assumed)")
        Shop = Entry.get("cpp_shop_parameters") or Defaults.get("cpp_shop_parameters")
        Result = cpp.CalculateCost(Mat.CppMetal, float(Dims["x"]), float(Dims["y"]), float(Dims["z"]),
                                   AssumedVolumeCm3, Shop)
        Notes.append(f"CPP cost at 1 cm³ with reference box {Dims['x']}×{Dims['y']}×{Dims['z']} mm "
                     f"({cpp.CppDbVersion()}).")
        return Result["price_usd"]
