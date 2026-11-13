"""Customize (selection → material/size/quantity → quote) and the bag.

The bag is a server-side, per-account list of quote snapshots; checkout (p3/orders.py) turns it into
an order at today's fixed prices. Purchase rules are enforced here, not just in the browser (spec 4.6):
  * only a metal Available for the product is priced and purchasable (p3/materialstatus.py, WP3): a Quote Only metal
    asks for a quote, a Preview Only one is seen only, a hidden one is offered no more;
  * an available metal is purchasable only with a valid server quote and a chosen standard size;
  * ring size and quantity never change the unit price.
Customize always opens on Stainless Steel (a new charm at its default size); entered again on a saved customization it
shows the same, the saved record untouched until the customer changes something or adds to the bag (WP3).
"""

import json

from p3 import charmprices as CharmPrices
from p3 import materialstatus as MaterialStatus
from p3 import products as Products
from p3.pricing.service import PublicPromotion
from p3 import ringids as RingIds
from p3 import sessions as Sessions
from p3.accounts import InsufficientCredits, Principal
from p3.context import Context, HttpError
from p3.db import Dumps, NewId, Now
from p3.images import ImageService
from p3.movies import MovieService

MaxQuantity = 10


# Customize opens on US 10 (product-owner decision 2026-10-01); the customer can change it.
DefaultRingSize = 10.0
# Why a metal that is not Available can't go to the bag (Quote Only keeps the reason gold always had)
BlockedReasons = {MaterialStatus.Quote: "luxury_preview_only", MaterialStatus.Preview: "preview_only",
                  MaterialStatus.Hidden: "material_not_offered"}


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
        """Lock in the selected candidate for Customize and start (or reuse) its movie — not while a refinement of the
        design is being made (R-18)."""
        D = self.Images.RequireDesign(Who, DesignId)
        self.Images.RequireReadyCandidate(DesignId, CandidateId)
        if self.Images.RefinementRunning(DesignId):
            raise HttpError(409, "refinement_running", "Wait for the refinement to finish.")
        Db = self.Ctx.Db
        Charm = (D.get("product_type") or Products.Ring) == Products.Charm
        if self._Selected(Who, D) != CandidateId:              # the saved design is written only when its pick changes
            self._SetSelection(Who, D, CandidateId)
        Existing = Db.One("SELECT * FROM customizations WHERE owner_account_id = ? AND design_id = ? AND candidate_id = ?",
                          (Who.AccountId, DesignId, CandidateId))
        # Customize opens on Stainless Steel (WP3); a charm on its default size (14 mm), so its price shows at once
        Metal, CharmSize = self.Ctx.Catalog.DefaultMaterialId, self.Ctx.Products.CharmDefaultSize if Charm else None
        if Existing is None:
            T = Now()
            # A ring opens on the size the customer already chose for this design (on another of its options: R-21),
            # otherwise on US 10
            Known = None if Charm else Db.One("SELECT ring_size FROM customizations WHERE owner_account_id = ? AND design_id = ? "
                                              "AND size_confirmed_at IS NOT NULL AND ring_size IS NOT NULL "
                                              "ORDER BY size_confirmed_at DESC LIMIT 1", (Who.AccountId, DesignId))
            Db.Execute("INSERT OR IGNORE INTO customizations (id, owner_account_id, design_id, candidate_id, material_id, "
                       "ring_size, charm_size, quantity, created_at, updated_at, size_confirmed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (NewId("cus"), Who.AccountId, DesignId, CandidateId, Metal,
                        None if Charm else (Known["ring_size"] if Known else DefaultRingSize), CharmSize, 1, T, T, T if Known else None))
        else:
            # Entered again: it shows the same; the saved choices stay exactly as they are until the customer acts
            Size = (Existing["charm_size"] if Existing["charm_size"] is not None else CharmSize) if Charm else None
            if Existing["material_id"] != Metal or (Charm and Existing["charm_size"] is None):
                Db.Execute("INSERT INTO customize_entries (customization_id, owner_account_id, material_id, charm_size, created_at) "
                           "VALUES (?,?,?,?,?) ON CONFLICT(customization_id) DO UPDATE SET material_id = excluded.material_id, "
                           "charm_size = excluded.charm_size, created_at = excluded.created_at",
                           (Existing["id"], Who.AccountId, Metal, Size, Now()))
            else:
                Db.Execute("DELETE FROM customize_entries WHERE customization_id = ?", (Existing["id"],))
        if Products.MovieAvailable(self.Ctx, D.get("product_type") or Products.Ring):
            try:
                self.Movies.Ensure(Who, CandidateId)            # the product's movie switch is ON: start or reuse its movie
            except InsufficientCredits:
                # B5 (11 Nov 2026): no credit for a new movie — Customize opens on the still images and the piece can be
                # ordered; nothing is started or charged (an existing movie is reused above, for free)
                pass
        Row = Db.One("SELECT * FROM customizations WHERE owner_account_id = ? AND design_id = ? AND candidate_id = ?",
                     (Who.AccountId, DesignId, CandidateId))
        View = self._View(Row)
        Size = {"product_type": Products.Charm, "charm_size": View["charm_size"]} if Charm else {"ring_size": View["ring_size"]}
        Saved = {"saved_material_id": Row["material_id"]} if Row["material_id"] != View["material_id"] else {}
        Sessions.Record(self.Ctx, Who.AccountId, "customize_opened", DesignId, candidate_id=CandidateId,
                        material_id=View["material_id"], **Size, **Saved, defaults=Existing is None,
                        **Sessions.QuoteSnapshot(self.Ctx, View["material_id"], *((Products.Charm, View["charm_size"]) if Charm else ())))
        return self.ToJson(Row)

    def _Selected(self, Who: Principal, D: dict) -> str | None:
        """The design's pick as saved: the owner's on the design, a gallery customer's on their use row."""
        if D["owner_account_id"] == Who.AccountId:
            return D.get("selected_candidate_id")
        U = self.Ctx.Db.One("SELECT selected_candidate_id FROM gallery_uses WHERE design_id = ? AND owner_account_id = ?",
                            (D["id"], Who.AccountId))
        return U["selected_candidate_id"] if U else None

    def _Entry(self, CustomizationId: str) -> dict | None:
        return self.Ctx.Db.One("SELECT material_id, charm_size FROM customize_entries WHERE customization_id = ?", (CustomizationId,))

    def _View(self, Row: dict) -> dict:
        """What Customize shows for a customization: its saved choices — or, entered again and not acted on since, the
        metal Customize opens on (and a charm's default size), the saved record untouched (WP3). While customers have a
        single charm size (Admin → Charm sizes), a charm saved in a size no longer shown shows that size instead — there
        is no size choice to wait for; adding it to the bag saves it. With two or more, it asks for another size as before."""
        E = self._Entry(Row["id"])
        V = Row if E is None else             {**Row, "material_id": E["material_id"], **({"charm_size": E["charm_size"]} if E["charm_size"] is not None else {})}
        On = self.Ctx.Products.CharmSizesEnabled
        if V.get("charm_size") is not None and len(On) == 1 and float(V["charm_size"]) != On[0]:
            V = {**V, "charm_size": On[0]}
        return V

    def Update(self, Who: Principal, CustomizationId: str, Changes: dict) -> dict:
        Row = self._Owned(Who, CustomizationId)
        Charm = Products.Of(self.Ctx.Db, Row["design_id"]) == Products.Charm
        Fields = {}
        if "material_id" in Changes:
            if self.Ctx.Catalog.Get(Changes["material_id"]) is None:
                raise HttpError(400, "unknown_material", "Unknown material.")
            if Charm and not CharmPrices.IsOffered(self.Ctx.Catalog, Changes["material_id"]):
                raise HttpError(400, "material_not_offered", "This material is not offered for charms.")
            if MaterialStatus.Of(self.Ctx, Products.Charm if Charm else Products.Ring, Changes["material_id"]) == MaterialStatus.Hidden:
                raise HttpError(400, "material_not_offered", MaterialStatus.HiddenText())
            Fields["material_id"] = Changes["material_id"]
        if "ring_size" in Changes and Charm:
            raise HttpError(400, "invalid_size", "A charm has a charm size in mm, not a ring size.")
        if "charm_size" in Changes:
            if not Charm:
                raise HttpError(400, "invalid_size", "A ring has a ring size, not a charm size.")
            Size = Changes["charm_size"]
            if Size is not None and not self.Ctx.Products.IsValidCharmSize(Size):
                raise HttpError(400, "invalid_charm_size", "Please choose one of the charm sizes.")
            Fields["charm_size"] = None if Size is None else float(Size)
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
        Entry = self._Entry(Row["id"])
        if Entry:      # the first change after Customize opened again keeps what it showed: its metal (and a charm's size)
            Fields.setdefault("material_id", Entry["material_id"])
            if Charm and Entry["charm_size"] is not None:
                Fields.setdefault("charm_size", Entry["charm_size"])
        if Fields:
            # A ring size the customer picks, the suggested one included, is their confirmed size (R-21)
            Confirmed = {"size_confirmed_at": Now() if Fields["ring_size"] is not None else None} if "ring_size" in Fields else {}
            self.Ctx.Db.Update("customizations", CustomizationId, **Fields, **Confirmed)
            Priced = (Products.Charm, Fields.get("charm_size", Row["charm_size"])) if Charm else ()
            Sessions.Record(self.Ctx, Who.AccountId, "customization_changed", Row["design_id"],
                            customization_id=Row["id"], **Fields, **({"product_type": Products.Charm} if Charm else {}),
                            **Sessions.QuoteSnapshot(self.Ctx, Fields.get("material_id", Row["material_id"]), *Priced))
        if Entry:                                       # what it showed is now the saved choice: the entry ends
            self.Ctx.Db.Execute("DELETE FROM customize_entries WHERE customization_id = ?", (Row["id"],))
        return self.ToJson(self.Ctx.Db.One("SELECT * FROM customizations WHERE id = ?", (Row["id"],)))

    def Get(self, Who: Principal, CustomizationId: str, Offered: tuple | None = None) -> dict:
        return self.ToJson(self._Owned(Who, CustomizationId), Offered)

    def _Owned(self, Who: Principal, CustomizationId: str) -> dict:
        Row = self.Ctx.Db.One("SELECT * FROM customizations WHERE id = ? AND owner_account_id = ?",
                              (CustomizationId, Who.AccountId))
        if Row is None:
            raise HttpError(404, "customization_not_found", "Customization not found.")
        return Row

    def Purchasability(self, Row: dict, Quote, Product: str = Products.Ring) -> tuple[bool, str | None]:
        Status = MaterialStatus.Of(self.Ctx, Product, Row["material_id"])
        if Status != MaterialStatus.Available:                 # only an available metal goes to the bag (WP3)
            return False, BlockedReasons[Status]
        if Product == Products.Charm:
            # A charm's price depends on its size, so the size comes first
            if Row.get("charm_size") is None:
                return False, "charm_size_required"
            if Quote.unavailable_reason == "charm_size_not_offered":
                return False, "charm_size_not_offered"
            return (True, None) if Quote.IsAvailable else (False, "price_unavailable")
        if not Quote.IsAvailable:
            return False, "price_unavailable"
        if Row["ring_size"] is None:
            return False, "ring_size_required"
        return True, None

    def ToJson(self, Row: dict, Offered: tuple | None = None) -> dict:
        """Offered: the products this caller may order — a customization of any other (its product switched OFF) says
        it can't go to the bag (design_unavailable), never "can add" (WP2). None: not checked (internal callers). What it
        shows is what Customize shows (_View: entered again, the default metal; the saved record untouched)."""
        Row = self._View(Row)
        Cand = self.Ctx.Db.One("SELECT * FROM candidates WHERE id = ?", (Row["candidate_id"],))
        Product = Products.Of(self.Ctx.Db, Row["design_id"])
        Quote = CharmPrices.QuoteFor(self.Ctx, Product, Row["material_id"], Row.get("charm_size"))
        CanAdd, Reason = self.Purchasability(Row, Quote, Product)
        if Offered is not None and Product not in Offered:
            CanAdd, Reason = False, "design_unavailable"
        MovieOn = Products.MovieAvailable(self.Ctx, Product)       # OFF: the still image only, whatever movies exist
        Out = {
            "id": Row["id"], "design_id": Row["design_id"], "candidate_id": Row["candidate_id"],
            # its item ID (C-1062-C / R-1042-B), under the name in Customize as in the bag (Boutique, 13 Nov 2026)
            "ring_id": RingIds.CandidateRefs(self.Ctx.Db, [Row["design_id"]]).get(Row["candidate_id"]),
            "image_url": self.Ctx.AssetUrl(Cand["asset_path"]),
            "material_id": Row["material_id"], "ring_size": Row["ring_size"], "quantity": Row["quantity"],
            "size_confirmed": Row.get("charm_size") is not None if Product == Products.Charm else Row.get("size_confirmed_at") is not None,
            "quote": Quote.ToJson(),
            "line_total": round(Quote.unit_price * Row["quantity"], 2) if Quote.IsAvailable else None,
            "can_add_to_bag": CanAdd, "add_to_bag_blocked_reason": Reason,
            "movie": self.Movies.ToJson(self.Movies.Latest(Row["candidate_id"])) if MovieOn else None,
            "movie_available": MovieOn,
        }
        if Product == Products.Charm:
            Out.update(self._CharmJson(Row))
        return Out

    def _CharmJson(self, Row: dict) -> dict:
        """What Customize shows for a charm: its sizes (each with today's price in the chosen material), what a
        size measures, and the materials a charm is made in — grouped by the charm's own statuses (a hidden one left out)."""
        Cat = self.Ctx.Catalog
        Statuses = MaterialStatus.Map(self.Ctx, Products.Charm)
        Sizes = []
        for O in self.Ctx.Products.CharmSizeOptions():       # '14 mm — Classic · Recommended', each with its price
            Q = self.Ctx.CharmPrices.QuoteFor(Row["material_id"], O["size"])
            Sizes.append({**O, "unit_price": Q.unit_price, "currency": Q.currency, "pricing_status": Q.pricing_status,
                          **({"promotion": PublicPromotion(Q.promotion)} if Q.promotion else {})})
        return {"product_type": Products.Charm, "charm_size": Row.get("charm_size"),
                "size_label": Products.SizeLabel(Products.Charm, None, Row.get("charm_size")),
                "recommended_size": self.Ctx.Products.CharmDefaultSize,     # suggested until the customer picks a size
                "material_label": CharmPrices.Label(Cat, Row["material_id"]),
                "charm_sizes": Sizes, "size_definition": Products.CharmSizeDefinition,
                "materials": [{"id": M.Id, "label": CharmPrices.Label(Cat, M.Id), "group": MaterialStatus.GroupOf(Statuses[M.Id]),
                               "purchasable": Statuses[M.Id] == MaterialStatus.Available}
                              for M in CharmPrices.Offered(Cat) if Statuses[M.Id] != MaterialStatus.Hidden]}

    # ── bag ──────────────────────────────────────────────────────────────
    def AddToBag(self, Who: Principal, CustomizationId: str, Visible: tuple | None = None, RequestId: str | None = None) -> dict:
        """Add what Customize shows, as a shop's cart does (owner, 13 Nov 2026): every click adds its quantity; the same
        piece (design option, product, metal, size) at the same price is ONE line whose quantity grows (1 + 1 + 1 = 3;
        2 + 2 = 4); a different metal, size or price is a line of its own. A line holds at most MaxQuantity: a click
        that would go past it adds what still fits and says so; one with no room left is refused with the reason.
        RequestId: the click's own id — the same request sent again (a retry) adds nothing more. The answer is the bag,
        plus "added": {line_id, quantity, line_quantity, limited} for the confirmation."""
        Row = self._Owned(Who, CustomizationId)
        Shown = self._View(Row)                     # a charm's size no longer shown: the single size Customize shows
        Fix = {"charm_size": Shown["charm_size"]} if Shown.get("charm_size") != Row.get("charm_size") else {}
        if self._Entry(Row["id"]) or Fix:           # adding what Customize shows: it becomes the saved choice first
            self.Update(Who, CustomizationId, Fix)
            Row = self._Owned(Who, CustomizationId)
        Product = Products.Of(self.Ctx.Db, Row["design_id"])          # the line is a snapshot of the design's product
        Quote = CharmPrices.QuoteFor(self.Ctx, Product, Row["material_id"], Row.get("charm_size"))
        CanAdd, Reason = self.Purchasability(Row, Quote, Product)
        # Refused first: the same line already in the bag (bagged while it could be) is no reason to answer "added" (WP3)
        if not CanAdd:
            Messages = {"luxury_preview_only": "Luxury materials are preview-only for now.",
                        "preview_only": MaterialStatus.PreviewText(self.Ctx, Product),
                        "material_not_offered": MaterialStatus.HiddenText(),
                        "price_unavailable": "Price unavailable — this item cannot be added to the bag.",
                        "ring_size_required": "Please choose a ring size.",
                        "charm_size_required": "Please choose a charm size.",
                        "charm_size_not_offered": "This charm size is no longer offered — please choose another size."}
            raise HttpError(409, Reason, Messages[Reason])
        RingSize = Row["ring_size"] if Product == Products.Ring else None
        CharmSize = Row.get("charm_size") if Product == Products.Charm else None
        Want = max(1, min(MaxQuantity, int(Row["quantity"] or 1)))
        ReqId = str(RequestId or "").strip()[:80] or None
        # One transaction: the click's id, the matching line and its new quantity — two clicks at once both count, a
        # request sent twice counts once
        Repeated = False
        with self.Ctx.Db.Transaction() as Conn:
            if ReqId and Conn.execute("INSERT OR IGNORE INTO bag_add_requests (owner_account_id, request_id, created_at) VALUES (?,?,?)",
                                      (Who.AccountId, ReqId, Now())).rowcount == 0:
                Repeated = True
            else:
                Add, LineId, LineQty = self._AddLine(Conn, Who, Row, Product, Quote, RingSize, CharmSize, Want)
        if Repeated:
            return {**self.Bag(Who, Visible), "added": None, "repeated_request": True}
        Size = {"product_type": Products.Charm, "charm_size": CharmSize} if Product == Products.Charm else {"ring_size": RingSize}
        Sessions.Record(self.Ctx, Who.AccountId, "bag_added", Row["design_id"], line_id=LineId,
                        material_id=Row["material_id"], **Size, quantity=Add, line_quantity=LineQty,
                        unit_price=Quote.unit_price, currency=Quote.currency, pricing_version=Quote.pricing_version)
        return {**self.Bag(Who, Visible), "added": {"line_id": LineId, "quantity": Add, "line_quantity": LineQty,
                                                     "limited": Add < Want, "max_quantity": MaxQuantity}}

    def _AddLine(self, Conn, Who: Principal, Row: dict, Product: str, Quote, RingSize, CharmSize, Want: int) -> tuple:
        """Inside the add's transaction: grow the matching line (same option, product, metal, size and price) by Want, up to
        MaxQuantity, or start a new line. Returns (added, line id, the line's quantity)."""
        Line = Conn.execute(
            "SELECT id, quantity FROM bag_lines WHERE owner_account_id = ? AND design_id = ? AND candidate_id = ? "
            "AND product_type = ? AND material_id = ? AND COALESCE(ring_size, -1) = COALESCE(?, -1) "
            "AND COALESCE(charm_size, -1) = COALESCE(?, -1) AND unit_price = ? AND currency = ? ORDER BY created_at LIMIT 1",
            (Who.AccountId, Row["design_id"], Row["candidate_id"], Product, Row["material_id"], RingSize, CharmSize,
             Quote.unit_price, Quote.currency)).fetchone()
        if Line:
            Room = MaxQuantity - Line["quantity"]
            if Room <= 0:
                raise HttpError(409, "bag_quantity_limit",
                                f"Your bag already has {MaxQuantity} of this piece — the most for one piece. "
                                "For more, contact us.")
            Add, LineId = min(Want, Room), Line["id"]
            Conn.execute("UPDATE bag_lines SET quantity = quantity + ?, customization_id = ?, pricing_version = ?, quote_json = ? "
                         "WHERE id = ?", (Add, Row["id"], Quote.pricing_version, Dumps(Quote.ToJson()), LineId))
            LineQty = Line["quantity"] + Add
        else:
            Add, LineId, LineQty = Want, NewId("bag"), Want
            Conn.execute(
                "INSERT INTO bag_lines (id, owner_account_id, design_id, candidate_id, customization_id, product_type, material_id, "
                "ring_size, charm_size, quantity, unit_price, currency, pricing_version, quote_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (LineId, Who.AccountId, Row["design_id"], Row["candidate_id"], Row["id"], Product, Row["material_id"],
                 RingSize, CharmSize, Want, Quote.unit_price, Quote.currency, Quote.pricing_version, Dumps(Quote.ToJson()), Now()))
        return Add, LineId, LineQty

    def SetBagQuantity(self, Who: Principal, LineId: str, Quantity, Visible: tuple | None = None) -> dict:
        """A bag line's quantity, changed in the bag (R-20): 1 to MaxQuantity at the line's own price; its total and the
        bag's follow."""
        if not isinstance(Quantity, int) or isinstance(Quantity, bool) or not 1 <= Quantity <= MaxQuantity:
            raise HttpError(400, "invalid_quantity", f"Quantity must be between 1 and {MaxQuantity}.")
        Line = self.Ctx.Db.One("SELECT design_id, product_type, quantity FROM bag_lines WHERE id = ? AND owner_account_id = ?",
                               (LineId, Who.AccountId))
        if Line is None:
            raise HttpError(404, "bag_line_not_found", "Bag line not found.")
        if Visible is not None and (Line.get("product_type") or Products.Ring) not in Visible:   # its product is OFF: locked
            raise HttpError(409, "design_unavailable", "This piece is currently unavailable for customization or ordering — "
                                                       "its quantity can't change. You can remove it from your bag.")
        if Line["quantity"] != Quantity:
            self.Ctx.Db.Execute("UPDATE bag_lines SET quantity = ? WHERE id = ? AND owner_account_id = ?", (Quantity, LineId, Who.AccountId))
            Sessions.Record(self.Ctx, Who.AccountId, "bag_quantity_changed", Line["design_id"], line_id=LineId, quantity=Quantity)
        return self.Bag(Who, Visible)

    def RemoveFromBag(self, Who: Principal, LineId: str, Visible: tuple | None = None) -> dict:
        Line = self.Ctx.Db.One("SELECT design_id FROM bag_lines WHERE id = ? AND owner_account_id = ?", (LineId, Who.AccountId))
        if Line:
            Sessions.Record(self.Ctx, Who.AccountId, "bag_removed", Line["design_id"], line_id=LineId)
        if self.Ctx.Db.Execute("DELETE FROM bag_lines WHERE id = ? AND owner_account_id = ?", (LineId, Who.AccountId)) == 0:
            raise HttpError(404, "bag_line_not_found", "Bag line not found.")
        return self.Bag(Who, Visible)

    def Bag(self, Who: Principal, Visible: tuple | None = None) -> dict:
        """The bag at today's prices. Visible: the products on offer to this browser — a line of a product switched OFF
        stays in the bag, marked unavailable, and is not orderable (None: every product, for internal callers). A line
        that can't be ordered is left out of the total and never blocks the others (WP2): the rest can be ordered."""
        Lines = []
        Paused = False
        Totals: dict[str, float] = {}
        Current: dict[tuple, object] = {}           # (product, material, charm size) → today's quote (version, availability)
        Rows = self.Ctx.Db.All("SELECT b.*, c.asset_path, d.title FROM bag_lines b "
                               "JOIN candidates c ON c.id = b.candidate_id JOIN designs d ON d.id = b.design_id "
                               "WHERE b.owner_account_id = ? ORDER BY b.created_at", (Who.AccountId,))
        Refs = RingIds.CandidateRefs(self.Ctx.Db, list({L["design_id"] for L in Rows}))
        Count = 0                                   # the lines that can be ordered now
        Statuses = {P: MaterialStatus.Map(self.Ctx, P) for P in Products.All}     # each product's metal statuses (WP3)
        for L in Rows:
            Product = L.get("product_type") or Products.Ring
            Status = Statuses[Product].get(L["material_id"], MaterialStatus.Hidden)
            Key = (Product, L["material_id"], L.get("charm_size") if Product == Products.Charm else None)
            if Key not in Current:
                Current[Key] = CharmPrices.QuoteFor(self.Ctx, Product, L["material_id"], L.get("charm_size"))
            Q = Current[Key]
            # Orderable: an available metal with a price today and a standard size (checked again at checkout); a line in
            # a Preview Only, Quote Only or hidden metal stays in the bag, left out (WP2's exclusion, WP3)
            Off = Visible is not None and Product not in Visible        # its product is switched OFF: kept, not orderable
            Paused = Paused or Off
            if Off:
                LineOk = False
            elif Product == Products.Charm:
                LineOk = Status == MaterialStatus.Available and Q.IsAvailable \
                    and L["charm_size"] is not None and self.Ctx.Products.IsValidCharmSize(L["charm_size"])
            else:
                LineOk = Status == MaterialStatus.Available and Q.IsAvailable \
                    and L["ring_size"] is not None and self.Ctx.Catalog.IsValidSize(L["ring_size"])
            Total = round(L["unit_price"] * L["quantity"], 2)
            if LineOk:                                  # only what can be ordered counts in the total
                Count += 1
                Totals[L["currency"]] = round(Totals.get(L["currency"], 0) + Total, 2)
            Lines.append({"id": L["id"], "design_id": L["design_id"], "title": L["title"], "ring_id": Refs.get(L["candidate_id"]),
                          "candidate_id": L["candidate_id"], "image_url": self.Ctx.AssetUrl(L["asset_path"]),
                          "material_id": L["material_id"],
                          "material_label": CharmPrices.MaterialLabel(self.Ctx, Product, L["material_id"]),
                          "ring_size": L["ring_size"], "product_type": Product,
                          "charm_size": L.get("charm_size"),
                          "size_label": Products.SizeLabel(L.get("product_type") or Products.Ring, L["ring_size"], L.get("charm_size")),
                          "quantity": L["quantity"], "unit_price": L["unit_price"],
                          "currency": L["currency"], "line_total": Total, "pricing_version": L["pricing_version"],
                          "price_is_stale": L["pricing_version"] != Q.pricing_version,
                          "current_unit_price": Q.unit_price if Q.IsAvailable else None, "orderable": LineOk,
                          # today's promotion on it (its price above is what it was added at; checkout charges today's)
                          **({"promotion": PublicPromotion(Q.promotion)} if Q.IsAvailable and Q.promotion else {}),
                          **({"unavailable": True} if Off else {}),
                          # a charm in a size customers can no longer choose: the bag says so (the line keeps its size)
                          **({"size_not_offered": True} if not Off and Product == Products.Charm and L.get("charm_size") is not None
                             and not self.Ctx.Products.IsValidCharmSize(L["charm_size"]) else {}),
                          # the metal's status when it is not Available ("preview": the bag says Preview only)
                          **({"material_status": Status} if Status != MaterialStatus.Available else {}),
                          "quote": json.loads(L["quote_json"])})
        Left = len(Lines) - Count
        Note = None
        if Left:
            Note = (("One line is" if Left == 1 else f"{Left} lines are") + " "
                    + ("currently unavailable for customization or ordering" if Paused else "not orderable yet — see the line for the reason")
                    + "." + ((" It stays" if Left == 1 else " They stay") + " in your bag, left out of the total; the rest can be ordered."
                             if Count else ""))
        return {"lines": Lines, "totals": Totals, "checkout_available": Count > 0, "checkout_note": Note}
