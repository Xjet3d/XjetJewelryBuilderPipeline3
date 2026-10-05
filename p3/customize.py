"""Customize (selection → material/size/quantity → quote) and the bag.

The bag is a server-side, per-account list of quote snapshots; checkout (p3/orders.py) turns it into
an order at today's fixed prices. Purchase rules are enforced here, not just in the browser (spec 4.6):
  * Luxury → never purchasable, never priced;
  * Fashion → purchasable only with a valid server quote and a chosen standard size;
  * ring size and quantity never change the unit price.
"""

import json

from p3 import products as Products
from p3 import ringids as RingIds
from p3 import sessions as Sessions
from p3.accounts import Principal
from p3.context import Context, HttpError
from p3.db import Dumps, NewId, Now
from p3.images import ImageService
from p3.movies import MovieService

MaxQuantity = 10


# Customize opens on US 10 (product-owner decision 2026-10-01); the customer can change it.
DefaultRingSize = 10.0


class CustomizeService:
    def __init__(self, Ctx: Context, Images: ImageService, Movies: MovieService):
        self.Ctx = Ctx
        self.Images = Images
        self.Movies = Movies

    # ── selection ────────────────────────────────────────────────────────
    def _SetSelection(self, Who: Principal, D: dict, CandidateId: str | None) -> None:
        """The owner's selection lives on the design; a gallery customer's selection on their use row."""
        if D["owner_account_id"] == Who.AccountId:
            self.Ctx.Db.Update("designs", D["id"], selected_candidate_id=CandidateId)
        else:
            self.Ctx.Db.Execute("UPDATE gallery_uses SET selected_candidate_id = ?, last_active_at = ? "
                                "WHERE design_id = ? AND owner_account_id = ?", (CandidateId, Now(), D["id"], Who.AccountId))

    def Select(self, Who: Principal, DesignId: str, CandidateId: str | None) -> dict:
        D = self.Images.RequireDesign(Who, DesignId)
        if CandidateId is not None:
            self.Images.RequireReadyCandidate(DesignId, CandidateId)
        self._SetSelection(Who, D, CandidateId)
        Sessions.Record(self.Ctx, Who.AccountId, "option_selected", DesignId, candidate_id=CandidateId)
        return {"design_id": DesignId, "selected_candidate_id": CandidateId}

    # ── proceed ──────────────────────────────────────────────────────────
    def Proceed(self, Who: Principal, DesignId: str, CandidateId: str) -> dict:
        """Lock in the selected candidate for Customize and start (or reuse) its movie."""
        D = self.Images.RequireDesign(Who, DesignId)
        self.Images.RequireReadyCandidate(DesignId, CandidateId)
        Db = self.Ctx.Db
        self._SetSelection(Who, D, CandidateId)
        Existing = Db.One("SELECT * FROM customizations WHERE owner_account_id = ? AND design_id = ? AND candidate_id = ?",
                          (Who.AccountId, DesignId, CandidateId))
        if Existing is None:
            T = Now()
            Db.Execute("INSERT OR IGNORE INTO customizations (id, owner_account_id, design_id, candidate_id, material_id, "
                       "ring_size, quantity, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (NewId("cus"), Who.AccountId, DesignId, CandidateId, self.Ctx.Catalog.DefaultMaterialId,
                        DefaultRingSize, 1, T, T))
        self.Movies.Ensure(Who, CandidateId)
        Row = Db.One("SELECT * FROM customizations WHERE owner_account_id = ? AND design_id = ? AND candidate_id = ?",
                     (Who.AccountId, DesignId, CandidateId))
        Sessions.Record(self.Ctx, Who.AccountId, "customize_opened", DesignId, candidate_id=CandidateId,
                        material_id=Row["material_id"], ring_size=Row["ring_size"], defaults=Existing is None,
                        **Sessions.QuoteSnapshot(self.Ctx, Row["material_id"]))
        return self.ToJson(Row)

    def Update(self, Who: Principal, CustomizationId: str, Changes: dict) -> dict:
        Row = self._Owned(Who, CustomizationId)
        Fields = {}
        if "material_id" in Changes:
            if self.Ctx.Catalog.Get(Changes["material_id"]) is None:
                raise HttpError(400, "unknown_material", "Unknown material.")
            Fields["material_id"] = Changes["material_id"]
        if "ring_size" in Changes:
            Size = Changes["ring_size"]
            if Size is not None and not self.Ctx.Catalog.IsValidSize(Size):
                raise HttpError(400, "invalid_ring_size", "Please choose a standard ring size.")
            Fields["ring_size"] = None if Size is None else float(Size)
        if "quantity" in Changes:
            Qty = Changes["quantity"]
            if not isinstance(Qty, int) or isinstance(Qty, bool) or not 1 <= Qty <= MaxQuantity:
                raise HttpError(400, "invalid_quantity", f"Quantity must be between 1 and {MaxQuantity}.")
            Fields["quantity"] = Qty
        if Fields:
            self.Ctx.Db.Update("customizations", CustomizationId, **Fields)
            Sessions.Record(self.Ctx, Who.AccountId, "customization_changed", Row["design_id"],
                            customization_id=Row["id"], **Fields,
                            **Sessions.QuoteSnapshot(self.Ctx, Fields.get("material_id", Row["material_id"])))
        return self.ToJson(self.Ctx.Db.One("SELECT * FROM customizations WHERE id = ?", (Row["id"],)))

    def Get(self, Who: Principal, CustomizationId: str) -> dict:
        return self.ToJson(self._Owned(Who, CustomizationId))

    def _Owned(self, Who: Principal, CustomizationId: str) -> dict:
        Row = self.Ctx.Db.One("SELECT * FROM customizations WHERE id = ? AND owner_account_id = ?",
                              (CustomizationId, Who.AccountId))
        if Row is None:
            raise HttpError(404, "customization_not_found", "Customization not found.")
        return Row

    def Purchasability(self, Row: dict, Quote) -> tuple[bool, str | None]:
        Mat = self.Ctx.Catalog.Get(Row["material_id"])
        if Mat is None or not self.Ctx.Catalog.IsPurchasableGroup(Mat.Group):
            return False, "luxury_preview_only"
        if not Quote.IsAvailable:
            return False, "price_unavailable"
        if Row["ring_size"] is None:
            return False, "ring_size_required"
        return True, None

    def ToJson(self, Row: dict) -> dict:
        Cand = self.Ctx.Db.One("SELECT * FROM candidates WHERE id = ?", (Row["candidate_id"],))
        Quote = self.Ctx.Pricing.QuoteFor(Row["material_id"])
        CanAdd, Reason = self.Purchasability(Row, Quote)
        return {
            "id": Row["id"], "design_id": Row["design_id"], "candidate_id": Row["candidate_id"],
            "image_url": self.Ctx.AssetUrl(Cand["asset_path"]),
            "material_id": Row["material_id"], "ring_size": Row["ring_size"], "quantity": Row["quantity"],
            "quote": Quote.ToJson(),
            "line_total": round(Quote.unit_price * Row["quantity"], 2) if Quote.IsAvailable else None,
            "can_add_to_bag": CanAdd, "add_to_bag_blocked_reason": Reason,
            "movie": self.Movies.ToJson(self.Movies.Latest(Row["candidate_id"])),
        }

    # ── bag ──────────────────────────────────────────────────────────────
    def AddToBag(self, Who: Principal, CustomizationId: str) -> dict:
        Row = self._Owned(Who, CustomizationId)
        Quote = self.Ctx.Pricing.QuoteFor(Row["material_id"])
        CanAdd, Reason = self.Purchasability(Row, Quote)
        if not CanAdd:
            Messages = {"luxury_preview_only": "Luxury materials are preview-only for now.",
                        "price_unavailable": "Price unavailable — this item cannot be added to the bag.",
                        "ring_size_required": "Please choose a ring size."}
            raise HttpError(409, Reason, Messages[Reason])
        LineId = NewId("bag")
        Product = Products.Of(self.Ctx.Db, Row["design_id"])          # the line is a snapshot of the design's product
        self.Ctx.Db.Execute(
            "INSERT INTO bag_lines (id, owner_account_id, design_id, candidate_id, customization_id, product_type, material_id, "
            "ring_size, charm_size, quantity, unit_price, currency, pricing_version, quote_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (LineId, Who.AccountId, Row["design_id"], Row["candidate_id"], Row["id"], Product, Row["material_id"],
             Row["ring_size"] if Product == Products.Ring else None, Row.get("charm_size") if Product == Products.Charm else None,
             Row["quantity"], Quote.unit_price, Quote.currency, Quote.pricing_version, Dumps(Quote.ToJson()), Now()))
        Sessions.Record(self.Ctx, Who.AccountId, "bag_added", Row["design_id"], line_id=LineId,
                        material_id=Row["material_id"], ring_size=Row["ring_size"], quantity=Row["quantity"],
                        unit_price=Quote.unit_price, currency=Quote.currency, pricing_version=Quote.pricing_version)
        return self.Bag(Who)

    def RemoveFromBag(self, Who: Principal, LineId: str) -> dict:
        Line = self.Ctx.Db.One("SELECT design_id FROM bag_lines WHERE id = ? AND owner_account_id = ?", (LineId, Who.AccountId))
        if Line:
            Sessions.Record(self.Ctx, Who.AccountId, "bag_removed", Line["design_id"], line_id=LineId)
        if self.Ctx.Db.Execute("DELETE FROM bag_lines WHERE id = ? AND owner_account_id = ?", (LineId, Who.AccountId)) == 0:
            raise HttpError(404, "bag_line_not_found", "Bag line not found.")
        return self.Bag(Who)

    def Bag(self, Who: Principal) -> dict:
        Lines = []
        Totals: dict[str, float] = {}
        Current: dict[str, object] = {}             # material → today's quote (version, availability)
        Rows = self.Ctx.Db.All("SELECT b.*, c.asset_path, d.title FROM bag_lines b "
                               "JOIN candidates c ON c.id = b.candidate_id JOIN designs d ON d.id = b.design_id "
                               "WHERE b.owner_account_id = ? ORDER BY b.created_at", (Who.AccountId,))
        Refs = RingIds.CandidateRefs(self.Ctx.Db, list({L["design_id"] for L in Rows}))
        Orderable = True
        for L in Rows:
            Mat = self.Ctx.Catalog.Get(L["material_id"])
            if L["material_id"] not in Current:
                Current[L["material_id"]] = self.Ctx.Pricing.QuoteFor(L["material_id"])
            Q = Current[L["material_id"]]
            # Orderable: a purchasable material with a price today and a standard size (checked again at checkout)
            LineOk = Mat is not None and self.Ctx.Catalog.IsPurchasableGroup(Mat.Group) and Q.IsAvailable \
                and L["ring_size"] is not None and self.Ctx.Catalog.IsValidSize(L["ring_size"])
            Orderable = Orderable and LineOk
            Total = round(L["unit_price"] * L["quantity"], 2)
            Totals[L["currency"]] = round(Totals.get(L["currency"], 0) + Total, 2)
            Lines.append({"id": L["id"], "design_id": L["design_id"], "title": L["title"], "ring_id": Refs.get(L["candidate_id"]),
                          "candidate_id": L["candidate_id"], "image_url": self.Ctx.AssetUrl(L["asset_path"]),
                          "material_id": L["material_id"], "material_label": Mat.Label if Mat else L["material_id"],
                          "ring_size": L["ring_size"], "product_type": L.get("product_type") or Products.Ring,
                          "charm_size": L.get("charm_size"),
                          "size_label": Products.SizeLabel(L.get("product_type") or Products.Ring, L["ring_size"], L.get("charm_size")),
                          "quantity": L["quantity"], "unit_price": L["unit_price"],
                          "currency": L["currency"], "line_total": Total, "pricing_version": L["pricing_version"],
                          "price_is_stale": L["pricing_version"] != Q.pricing_version,
                          "current_unit_price": Q.unit_price if Q.IsAvailable else None, "orderable": LineOk,
                          "quote": json.loads(L["quote_json"])})
        return {"lines": Lines, "totals": Totals, "checkout_available": bool(Lines) and Orderable,
                "checkout_note": None if Orderable else "One of the lines cannot be ordered yet — see the line for the reason."}
