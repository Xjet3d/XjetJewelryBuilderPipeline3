"""Material statuses (WP3): what customers may do with each metal, set per product in Admin → Settings → Pricing &
Materials ("Customer status" — the Ring table and the Charm table, separately):

  available  on sale at its fixed price: Customize prices it; Add to Bag, checkout and the order take it
  preview    Preview Only: seen in Customize (renders keep their gold look), never priced, bagged, ordered or quoted —
             "Order it in Sterling Silver or Stainless Steel"
  quote      Quote Only: no fixed price — a quote request, the Admin's offer and its approval (orders.QuoteOfferProblem)
  hidden     not offered; what was made in it still opens and renders

A status gates actions only: no metal leaves the catalog, the offered lists or the price books (C1), and a change is no
new price version — it is a product setting, logged with who, when and a note. Until the Admin sets one, a metal keeps
today's behaviour: the catalog's purchasable metals available, the others (solid gold) quote only — so the release
changes nothing for customers. An Admin session never relaxes a status; an order already placed is never checked
against one. No Terms text reads a status, so a status change never makes a Terms version.
"""

from p3 import charmprices as CharmPrices
from p3 import products as Products
from p3.context import HttpError

Available, Preview, Quote, Hidden = "available", "preview", "quote", "hidden"
All = (Available, Preview, Quote, Hidden)
Labels = {Available: "Available", Preview: "Preview Only", Quote: "Quote Only", Hidden: "Hidden"}
Key = "material_statuses"                      # product_settings: {"ring": {material id: status}, "charm": {…}}
# Where a status puts a metal for customers: the catalog's groups of before ("fashion" is sold at a fixed price,
# "luxury" is quoted) and the Preview only group; a hidden metal is in none
GroupIds = {Available: "fashion", Quote: "luxury", Preview: "preview"}
GroupOrder = (Available, Quote, Preview)
PreviewLabel = "Preview only"
# The order metals are named in a sentence ("Order it in Sterling Silver or Stainless Steel")
SentenceOrder = ("silver", "stainless_steel", "vermeil")


def Offered(Ctx, Product: str) -> list:
    """The metals a product is made in, in the catalog's order: every catalog metal for a ring; the charm's own list
    (charmprices.Offered) for a charm."""
    Cat = Ctx.Catalog
    return CharmPrices.Offered(Cat) if Product == Products.Charm else list(Cat.Materials.values())


def Default(Ctx, Mat) -> str:
    """Today's behaviour, before any Admin choice: a purchasable metal is available, any other quote only."""
    return Available if Ctx.Catalog.IsPurchasableGroup(Mat.Group) else Quote


def _Stored(Ctx) -> dict:
    Settings = getattr(Ctx, "Products", None)
    Value = Settings.Get(Key) if Settings is not None else {}
    return Value if isinstance(Value, dict) else {}


def Map(Ctx, Product: str, Stored: dict | None = None) -> dict:
    """{material id: status} for every metal the product is made in (the Admin's choice, else today's default)."""
    Own = ((_Stored(Ctx) if Stored is None else Stored).get(Product) or {})
    return {M.Id: Own.get(M.Id) if Own.get(M.Id) in All else Default(Ctx, M) for M in Offered(Ctx, Product)}


def Of(Ctx, Product: str, MaterialId: str) -> str:
    """One metal's status for a product; a metal the product is not made in is hidden."""
    return Map(Ctx, Product or Products.Ring).get(MaterialId, Hidden)


def GroupOf(Status: str) -> str | None:
    return GroupIds.get(Status)


def CatalogGroups(Ctx) -> tuple[list, list]:
    """The customer catalog's metal groups (a ring's statuses; a charm's metals carry their own): every metal under its
    status's group, in the catalog's order — with today's statuses exactly the groups of before. The hidden metals come
    apart: they are offered nowhere, but what was made in them still renders."""
    Cat = Ctx.Catalog
    Statuses = Map(Ctx, Products.Ring)
    Config = {G["id"]: G for G in Cat.Groups}
    Groups = []
    for S in GroupOrder:
        Gid = GroupIds[S]
        Mats = [{**M.ToJson(), "group": Gid} for M in Cat.Materials.values() if Statuses.get(M.Id) == S]
        if Mats:
            Groups.append({**(Config.get(Gid) or {"id": Gid, "label": PreviewLabel, "purchasable": False}), "materials": Mats})
    return Groups, [{**M.ToJson(), "group": None} for M in Cat.Materials.values() if Statuses.get(M.Id, Hidden) == Hidden]


PageGroups_ = ("fashion", "luxury", "preview")


def PageGroups(Ctx, Visible: tuple) -> dict:
    """The Materials page's metals by status for the products on offer (both, when none is): a metal available for any
    of them is listed as available ("fashion"), else as quote only ("luxury"), else as preview only ("preview"); a
    hidden one nowhere — in the catalog's order, by the same rule as the page itself (web/app.js pageMetals)."""
    Prods = tuple(Visible) or Products.All
    Maps = {P: Map(Ctx, P) for P in Prods}
    Out = {G: [] for G in PageGroups_}
    for M in Ctx.Catalog.Materials.values():
        Groups = [GroupOf(Maps[P][M.Id]) for P in Prods if GroupOf(Maps[P].get(M.Id, Hidden))]
        if Groups:
            Out[min(Groups, key=PageGroups_.index)].append(M)
    return Out


def PageStatuses(Ctx, Visible: tuple, MaterialId: str) -> dict:
    """{product: status} of one metal for the products on offer (both, when none is): where they differ the page says so."""
    return {P: Of(Ctx, P, MaterialId) for P in (tuple(Visible) or Products.All)}


def Names(Labels_: list[str]) -> str:
    return (", ".join(Labels_[:-1]) + " or " + Labels_[-1]) if len(Labels_) > 1 else (Labels_[0] if Labels_ else "")


def OrderIn(Ctx, Product: str) -> str:
    """"Order it in Sterling Silver or Stainless Steel": the metals the product can be ordered in now."""
    Statuses = Map(Ctx, Product)
    Ids = [M for M in SentenceOrder if Statuses.get(M) == Available] + \
          [M for M, S in Statuses.items() if S == Available and M not in SentenceOrder]
    Labels_ = [CharmPrices.MaterialLabel(Ctx, Product, M) for M in Ids]
    return f"Order it in {Names(Labels_)}." if Labels_ else ""


def PreviewText(Ctx, Product: str) -> str:
    """What a customer reads for a Preview Only metal: no price, no bag, no quote — and what to order instead."""
    Order = OrderIn(Ctx, Product)
    return "Preview only — not available to order." + (f" {Order}" if Order else "")


def HiddenText() -> str:
    return "This metal is no longer offered."


# ── the Admin: the Customer status column (Admin → Settings → Pricing & Materials) ─────────────────────────────────────
def _Missing(Ctx, Product: str, Mat) -> list[str]:
    """What a metal lacks to be available: a fixed price (a charm: for every size customers can choose), its density and its cost."""
    Ring = Ctx.MaterialPrices.Row(Mat.Id) if getattr(Ctx, "MaterialPrices", None) else {}
    Out = []
    if Product == Products.Charm:
        Row = Ctx.CharmPrices.Row(Mat.Id)
        if Mat.Id not in CharmPrices.FixedPriceMaterials:
            return ["a fixed charm price (gold charms are quoted individually)"]
        Fixed = Row.get("fixed_prices") or {}
        Sizes = [S for S in Ctx.Products.CharmSizesEnabled if not Fixed.get(CharmPrices.SizeKey(S))]   # the sizes customers can choose
        if Sizes:
            Out.append("a fixed price for " + ", ".join(f"{S:g} mm" for S in Sizes))
        Cost = Row.get("cost_per_g")
    else:
        if not Ring.get("fixed_price"):
            Out.append("a fixed price")
        Cost = Ring.get("cost_per_g")
    if not Ring.get("density_g_cm3"):
        Out.append("its density")
    if not Cost:
        Out.append("its cost per gram")
    return Out


def Problem(Ctx, Product: str, MaterialId: str, Status: str) -> str | None:
    """Why this metal can't take this status for this product, or None."""
    Mat = Ctx.Catalog.Get(MaterialId or "")
    if Status not in All:
        return f"Unknown status: {Status}."
    if Mat is None or MaterialId not in Map(Ctx, Product):
        return f"{MaterialId} is not a {Products.Labels[Product].lower()} material."
    Label = CharmPrices.MaterialLabel(Ctx, Product, Mat.Id)
    if MaterialId == Ctx.Catalog.DefaultMaterialId and Status != Available:
        return f"{Label} is the metal Customize opens on: it stays Available."
    if Status == Available:
        Missing = _Missing(Ctx, Product, Mat)
        if Missing:
            return f"{Label} can be Available only with " + " and ".join(Missing) + " set."
    return None


def Affected(Ctx, Product: str, MaterialId: str) -> dict:
    """The customer records in this metal for this product that a status change touches (counts; open quote requests
    by reference). Placed orders are never checked against a status: they are not counted."""
    Db = Ctx.Db
    Kind = "COALESCE(d.product_type, 'ring') = ?"
    Saved = Db.One(f"SELECT COUNT(*) AS n FROM customizations c JOIN designs d ON d.id = c.design_id WHERE c.material_id = ? AND {Kind}",
                   (MaterialId, Product))["n"]
    Bag = Db.One("SELECT COUNT(*) AS n, COUNT(DISTINCT owner_account_id) AS bags FROM bag_lines "
                 "WHERE material_id = ? AND COALESCE(product_type, 'ring') = ?", (MaterialId, Product))
    Open = Db.All("SELECT request_no, status, offer_json FROM quote_requests WHERE material_id = ? AND COALESCE(product_type, 'ring') = ? "
                  "AND status IN ('new', 'quoted', 'answered') AND deleted_at IS NULL ORDER BY request_no", (MaterialId, Product))
    return {"customizations": Saved, "bag_lines": Bag["n"], "bags": Bag["bags"],
            "open_quotes": [{"ref": f"Q-{R['request_no']}" if R["request_no"] else None, "status": R["status"],
                             "offer": bool(R["offer_json"])} for R in Open]}


def AdminRows(Ctx, Product: str) -> list[dict]:
    """The Customer status column of one product's pricing table: each metal's status and, per status, why it can't be
    chosen now (None when it can)."""
    Statuses = Map(Ctx, Product)
    return [{"id": M.Id, "status": Statuses[M.Id], "default": Default(Ctx, M),
             "blocked": {S: Problem(Ctx, Product, M.Id, S) for S in All}} for M in Offered(Ctx, Product)]


def Admin(Ctx) -> dict:
    return {"statuses": [{"id": S, "label": Labels[S]} for S in All], "default_material_id": Ctx.Catalog.DefaultMaterialId,
            "products": {P: AdminRows(Ctx, P) for P in Products.All},
            "log": [L for L in Ctx.Products.State()["log"] if L["key"] == Key][:10]}


def Save(Ctx, Changes: list, By: str, Note: str = "", DryRun: bool = False) -> dict:
    """Set several metals' statuses at once, for one product or both (activation is one Admin change). Every change is
    checked first (Problem); DryRun answers what would change and the records it touches, and saves nothing."""
    if not isinstance(Changes, list) or not Changes:
        raise HttpError(400, "invalid_statuses", "Name at least one metal and its new status.")
    Stored = {P: dict(V) for P, V in _Stored(Ctx).items() if P in Products.All and isinstance(V, dict)}
    Plan = []
    for C in Changes:
        Product, Mid, Status = Products.Normalize((C or {}).get("product")), str((C or {}).get("material_id") or ""), str((C or {}).get("status") or "")
        Why = Problem(Ctx, Product, Mid, Status)
        if Why:
            raise HttpError(400, "invalid_status", Why)
        Was = Of(Ctx, Product, Mid)
        if Was != Status:
            Plan.append({"product": Product, "material_id": Mid, "label": CharmPrices.MaterialLabel(Ctx, Product, Mid),
                         "from": Was, "to": Status, "affected": Affected(Ctx, Product, Mid)})
            Stored.setdefault(Product, {})[Mid] = Status
    if Plan and not DryRun:
        Ctx.Products.Set(Key, {P: Map(Ctx, P, Stored) for P in Products.All}, By, Note or "Customer statuses changed in Admin")
    return {"changes": Plan, "saved": bool(Plan) and not DryRun, **Admin(Ctx)}
