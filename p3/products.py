"""Product types — Rings and Charms, side by side, each with its own configuration and behaviour.

Every design carries one product type, set when the design is created and never changed afterwards
(a database trigger refuses any change). Everything that came before charms is a ring: the column
defaults to 'ring', so existing designs, sessions and orders are rings without being rewritten.
Refinements, variations, gallery journeys and split refinements inherit the product of the design
they come from.

Customer availability is a separate, independent switch per product (Admin → Settings → Products:
"Rings available to customers", "Charms available to customers" — rings ON and charms OFF by default;
a future product joins the same list). A product that is OFF is not offered on the Design screen, not
in the Gallery's listing or its filters, not in the showcase, the site's wording does not mention it,
and the server refuses every production action on a design of it — starting, Make it yours, refining,
another option, Customize, a movie, the bag, a quote, an order ("Currently unavailable for customization
or ordering"). Once a product has been on offer (launched), OFF only pauses it: its gallery designs stay
viewable — their pages and old links, Favorites, Share and the sitemap — read-only (the owner's policy of
2026-10-15). A product never yet on offer stays hidden from customers. While rings are the only product
on, the customer site is the ring-only site of before. The Admin configures every product regardless,
and a browser signed in to the Admin sees the hidden products on the customer site (a preview), so a
product can be built and tested internally before customers see it. A preview is only for testing: a hidden
product is designed, bagged, ordered and quoted only on a test account (an address at a reserved test domain,
p3/testrecords.py), never on a customer's own, so it never places a real order or spends a customer's credits.

The charm size definition lives here, in one place (CharmSizeDefinition, Charm3DHeight): for now the
selected size is the charm's TOTAL height in millimetres, the attachment loop included — a 20 mm charm is
20 mm from its lowest point to the top of its loop, and its 3D model is scaled as a whole to that height.
"""

import json
from dataclasses import dataclass

from p3.context import Context, HttpError
from p3.db import Dumps, Now

Ring, Charm = "ring", "charm"
All = (Charm, Ring)                 # the order customers meet them in everywhere: charms first, rings one tap away (WP4)
Default = Ring                      # the product of a record that names none: everything made before charms is a ring
# The Terms of Service name the products in their first order, rings then charms (M7): their wording, and so the
# version a customer accepted (p3/accounts/consent.py hashes it), stays the same
TermsOrder = (Ring, Charm)
Labels = {Ring: "Ring", Charm: "Charm"}
Plurals = {Ring: "Rings", Charm: "Charms"}
Prefixes = {Ring: "R", Charm: "C"}          # customer-facing IDs: R-1001 … and C-1001 … (separate sequences)
AvailabilityKeys = {P: f"{Plurals[P].lower()}_available" for P in All}   # rings_available, charms_available (product_settings)
MovieKeys = {P: f"{Plurals[P].lower()}_movie" for P in All}               # rings_movie, charms_movie: the 360° movie switch per product
# What a customer reads where a design of a product switched OFF can be seen but not made (RequireOffered)
UnavailableText = "Currently unavailable for customization or ordering."
UnavailableError = "This design is currently unavailable for customization or ordering."
# What an Admin's browser reads when it previews a hidden product on an account that is not a test account
PreviewOnlyError = "Preview only: use a test account to try this product."
# The header a browser sends while an Admin has opened the site in preview (?preview=on, this tab only)
PreviewHeader = "X-P3-Preview"


def DefaultProduct(Visible, Previewed=()) -> str:
    """The product a design is when the request names none, and the one Design opens on: the first product on offer —
    a charm while charms are on offer (WP4); one an Admin's browser only previews comes after those customers can design
    — else a ring (the request is then refused, the product being invisible)."""
    On = [P for P in All if P in Visible]
    return next((P for P in On if P not in Previewed), On[0] if On else Ring)

# ── the charm size: one central definition (the only place to change it) ──────
# Decision of 2026-10-05: a charm's size is its total height, the loop included. Customize, the bag, checkout,
# orders, emails, prices (per size), the 3D scaling and the STL all follow this. There is no loop detection or
# loop measurement: should the definition change later (e.g. the body height with a standard loop attached
# separately), change it here and in Charm3DHeight.
CharmSizeUnit = "mm"
# Customers read "attachment eyelet" (Customize's About Charm Size, the FAQ, Contact: owner, 12 Nov 2026); the definition
# itself is unchanged — the total height, the loop at the top included. "short" names the exported STL's header and file:
# production files stay exactly as they were.
CharmSizeDefinition = {
    "measure": "total_height",
    "includes_loop": True,
    "unit": CharmSizeUnit,
    "label": "Charm height",
    "short": "total height incl. loop",
    "text": "The total height of the charm, including the attachment eyelet at the top.",
}


def Charm3DHeight(SizeMm) -> float:
    """The overall height a charm's 3D model is scaled to for a chosen size: the size itself, while the size is
    the charm's total height (the loop included)."""
    if CharmSizeDefinition["measure"] == "total_height":
        return float(SizeMm)
    raise ValueError(f"No 3D scaling is defined for the charm size measure {CharmSizeDefinition['measure']!r}.")
# The sizes on offer (2026-10-06): 10 mm Delicate · 14 mm Classic (recommended) · 18 mm Bold — a starting list
# only, edited in Admin → Settings → Products (sizes, their names and which one is recommended)
DefaultCharmSizes = (10.0, 14.0, 18.0)
DefaultCharmSizeNames = {"10": "Delicate", "14": "Classic", "18": "Bold"}
DefaultCharmRecommendedSize = 14.0
CharmSizeMin, CharmSizeMax, CharmSizesMax, CharmSizeNameMax = 3.0, 100.0, 12, 30
# The owner's rule of 4–5 Nov 2026: the sizes come from a 4 mm grid; exactly three are on offer at a time, 18 mm always
# among them. They are priced by their rank, at three levels per metal — the smallest at the low price, the middle at
# the medium and the biggest at the high one — and each level keeps its name (Delicate · Classic · Bold), whatever
# sizes it is given. A 3D can be made at any size of the grid (Admin), offered or not.
CharmSizeGrid = (10.0, 14.0, 18.0, 22.0, 26.0, 30.0)
CharmRequiredSize = 18.0
CharmSizesOnOffer = 3
CharmLevels = ("1", "2", "3")                                   # small · medium · large
CharmLevelLabels = {"1": "small", "2": "medium", "3": "large"}
DefaultCharmLevelNames = {"1": "Delicate", "2": "Classic", "3": "Bold"}


def CharmLevelOf(Size, Sizes) -> str | None:
    """The price level of a size among the sizes on offer ("1" for the smallest … "3" for the biggest); None when the
    size is not on offer."""
    try:
        S = sorted(float(V) for V in Sizes)
        return CharmLevels[S.index(float(Size))] if len(S) <= len(CharmLevels) else None
    except (TypeError, ValueError):
        return None


def SizeKey(Size) -> str:
    """The key of a size in mm in the names map and the price table: "14", "22.5"."""
    return f"{float(Size):g}"


def CharmDefaultSize(Sizes, Preferred=None) -> float:
    """The recommended size — the one a charm's 3D is made in when the customer chose none, and the one Customize
    suggests: the configured size when it is on offer, else the middle size on offer (14 mm of 10–18). The Admin can
    always choose another for a 3D."""
    S = sorted(float(V) for V in Sizes) or list(DefaultCharmSizes)
    if Preferred is not None and float(Preferred) in S:
        return float(Preferred)
    return S[(len(S) - 1) // 2]


def CharmSizeLabel(Size) -> str:
    """'14 mm': the size itself — bag and order lines, emails, Admin results."""
    return f"{float(Size):g} {CharmSizeUnit}"


def CharmSizeChoiceLabel(Size, Name: str | None = None, Recommended: bool = False) -> str:
    """What a size choice says: '14 mm — Classic · Recommended', '10 mm — Delicate', '22 mm' (a size without a name)."""
    L = CharmSizeLabel(Size) + (f" — {Name}" if Name else "")
    return L + " · Recommended" if Recommended else L


def CharmSizeOptions(Sizes, Names: dict, Recommended, Enabled=None, Only: bool = True) -> list[dict]:
    """The size choices in order: size, name (or None), recommended, the choice label, its price level — by its rank among
    every size on offer (Sizes), so switching a size off for customers never moves another size's price — and whether
    customers can choose it (Enabled: those sizes; None: all). Only: list just the enabled ones (what customers see)."""
    On = {float(V) for V in (Sizes if Enabled is None else Enabled)}
    return [{"size": float(S), "name": Names.get(SizeKey(S)) or None, "recommended": float(S) == float(Recommended),
             "label": CharmSizeChoiceLabel(S, Names.get(SizeKey(S)), float(S) == float(Recommended)),
             "level": CharmLevelOf(S, Sizes), "level_label": CharmLevelLabels.get(CharmLevelOf(S, Sizes) or ""),
             "enabled": float(S) in On}
            for S in sorted(float(V) for V in Sizes) if float(S) in On or not Only]


def SizeLabel(Product: str, RingSize=None, CharmSize=None) -> str | None:
    """'US 7' for a ring, '20 mm' for a charm (None when no size was chosen)."""
    if Product == Charm:
        return CharmSizeLabel(CharmSize) if CharmSize is not None else None
    return f"US {float(RingSize):g}" if RingSize is not None else None


# Owner's decision (audit action plan, 14 Nov 2026, Task 3): no purchase limit per piece — the former 10 is gone from
# Customize, the bag, checkout and quote requests. MaxQuantity is only a technical guard against nonsense values (a
# script, extra digits typed by mistake); no customer meets it in practice.
MaxQuantity = 9999


def CheckQuantity(Qty, Code: str = "invalid_quantity") -> int:
    """A whole number of pieces, at least 1 (and not past the technical guard). Returns it."""
    if not isinstance(Qty, int) or isinstance(Qty, bool) or Qty < 1:
        raise HttpError(400, Code, "The quantity must be a whole number of at least 1.")
    if Qty > MaxQuantity:
        raise HttpError(400, Code, f"For more than {MaxQuantity:,} of one piece, please contact us.")
    return Qty


def Normalize(Value, Default_: str = Default) -> str:
    """'ring' / 'charm' (case-insensitive); missing → the default. Anything else is refused."""
    if Value is None or (isinstance(Value, str) and not Value.strip()):
        return Default_
    V = str(Value).strip().lower()
    if V not in All:
        raise HttpError(400, "unknown_product", "Unknown product.")
    return V


def Of(Db, DesignId: str) -> str:
    """The product of a design ('ring' for anything not found, as before charms existed)."""
    R = Db.One("SELECT product_type FROM designs WHERE id = ?", (DesignId,))
    return (R or {}).get("product_type") or Ring


def OfCandidate(Db, CandidateId: str) -> str:
    R = Db.One("SELECT d.product_type FROM candidates c JOIN batches b ON b.id = c.batch_id JOIN designs d ON d.id = b.design_id "
               "WHERE c.id = ?", (CandidateId,))
    return (R or {}).get("product_type") or Ring


def OfGalleryItem(Db, ItemId: str) -> str:
    R = Db.One("SELECT d.product_type FROM gallery_items g JOIN designs d ON d.id = g.design_id WHERE g.id = ?", (ItemId,))
    return (R or {}).get("product_type") or Ring


# ── settings (Admin): customer availability and the charm size list ──────────
Schema = """
CREATE TABLE IF NOT EXISTS product_settings (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_settings_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL,
    value_json  TEXT NOT NULL,
    note        TEXT,
    at          TEXT NOT NULL,
    by          TEXT NOT NULL
);
"""

Defaults = {"credit_tariff": {"design": 1, "refinement": 1, "option": 1, "movie": 1, "mesh": 0},   # p3/credits.py decides
            "starting_credits": None,                                                # p3/credits.py: none = the default (10)
            "credit_request_default": None,                                          # p3/accounts/creditrequests.py: none = 5
            "rings_available": True, "charms_available": False, "rings_movie": True, "charms_movie": True,
            "charm_sizes": list(DefaultCharmSizes),
            "charm_sizes_enabled": None,                                             # the sizes customers choose from: none = all
            "charm_size_names": dict(DefaultCharmSizeNames), "charm_default_size": DefaultCharmRecommendedSize,
            "charm_level_names": dict(DefaultCharmLevelNames),                       # by level since 5 Nov 2026
            "prompt_check": {"ring": False, "charm": False},                          # p3/promptcheck.py: off
            "site_indexing": False,                                                  # p3/sitepages.py: search engines kept out
            "material_statuses": {},                                                 # p3/materialstatus.py: none = today's behaviour
            "google_site_verification": "",                                          # Search Console's meta tag (public, not a secret)
            "analytics": {"enabled": False, "host": "", "providers": {}},  # p3/analytics.py: off, no tool, nothing loads
            "ai_spend_warning": {"threshold_usd": None, "to": ""},                  # p3/spendwarning.py: off
            "ai_daily_spend_cap": {"cap_usd": None},                               # p3/credits.py: the server's cap
            "ai_spend_warning_sent": {},
            "promo_banner": None}                                                    # p3/promobanner.py: off


def ValidateCharmSizeNames(Raw, Sizes) -> dict:
    """The names of the sizes on offer ({"14": "Classic"}): short texts; a name for a size not on offer is dropped."""
    if Raw is None:
        return {}
    if not isinstance(Raw, dict):
        raise HttpError(400, "invalid_charm_size_names", "Charm size names are given per size.")
    Keys = {SizeKey(S) for S in Sizes}
    Out = {}
    for K, V in Raw.items():
        try:
            Key = SizeKey(K)
        except (TypeError, ValueError):
            raise HttpError(400, "invalid_charm_size_names", f"{K!r} is not a size in mm.")
        if V is None or not str(V).strip():
            continue
        if not isinstance(V, str) or len(V.strip()) > CharmSizeNameMax:
            raise HttpError(400, "invalid_charm_size_names", f"A size name is a short text (at most {CharmSizeNameMax} characters).")
        if Key in Keys:
            Out[Key] = V.strip()
    return {K: Out[K] for K in sorted(Out, key=float)}


def ValidateCharmLevelNames(Raw) -> dict:
    """The levels' names ({"1": "Delicate", "2": "Classic", "3": "Bold"}): short texts; an empty one is no name."""
    if not isinstance(Raw, dict):
        raise HttpError(400, "invalid_charm_size_names", "Size names are given per level (1 small, 2 medium, 3 large).")
    Out = {}
    for K, V in Raw.items():
        if str(K) not in CharmLevels:
            raise HttpError(400, "invalid_charm_size_names", f"{K!r} is not a level (1 small, 2 medium, 3 large).")
        if V is None or not str(V).strip():
            continue
        if not isinstance(V, str) or len(V.strip()) > CharmSizeNameMax:
            raise HttpError(400, "invalid_charm_size_names", f"A size name is a short text (at most {CharmSizeNameMax} characters).")
        Out[str(K)] = V.strip()
    return {K: Out[K] for K in CharmLevels if K in Out}


def CharmLevelNamesOf(SizeNames: dict, Sizes) -> dict:
    """{"1": "Delicate", …}: the names of the sizes on offer ({"18": "Bold"}) as the names of their levels."""
    return {L: SizeNames[SizeKey(S)] for S in sorted(float(V) for V in Sizes)
            for L in (CharmLevelOf(S, Sizes),) if L and SizeNames.get(SizeKey(S))}


def ValidateCharmDefaultSize(Raw, Sizes) -> float | None:
    """The recommended size, one of the sizes on offer (None = not given)."""
    if Raw is None or Raw == "":
        return None
    try:
        F = round(float(Raw), 2)
    except (TypeError, ValueError):
        raise HttpError(400, "invalid_charm_default_size", "The recommended size must be one of the sizes on offer.")
    if F not in [float(S) for S in Sizes]:
        raise HttpError(400, "invalid_charm_default_size", "The recommended size must be one of the sizes on offer.")
    return F


def ValidateCharmSizesEnabled(Raw, Sizes) -> list[float]:
    """The sizes customers can choose (Admin → Charm sizes, "Shown to customers"): at least one, each one of the sizes on
    offer, smallest first. One alone: Customize shows that size without a choice; two or more: the size choice."""
    On = [float(S) for S in Sizes]
    if not isinstance(Raw, (list, tuple)) or not Raw:
        raise HttpError(400, "invalid_charm_sizes_enabled", "Show at least one charm size to customers.")
    Out = []
    for V in Raw:
        try:
            F = round(float(V), 2)
        except (TypeError, ValueError):
            raise HttpError(400, "invalid_charm_sizes_enabled", f"Charm sizes must be numbers in millimetres ({V!r} is not).")
        if F not in On:
            raise HttpError(400, "invalid_charm_sizes_enabled",
                            f"{F:g} mm is not one of the sizes on offer ({', '.join(f'{S:g}' for S in On)} mm).")
        if F not in Out:
            Out.append(F)
    return sorted(Out)


def ValidateCharmSizes(Raw) -> list[float]:
    """The sizes on offer: exactly three of the grid, 18 mm among them, in ascending order whatever order they came in."""
    Grid = ", ".join(f"{S:g}" for S in CharmSizeGrid)
    Rule = (f"Choose exactly {CharmSizesOnOffer} charm sizes from {Grid} mm, {CharmRequiredSize:g} mm always among them "
            f"(for example 18, 22, 26).")
    if not isinstance(Raw, (list, tuple)) or not Raw:
        raise HttpError(400, "invalid_charm_sizes", Rule)
    Out = []
    for V in Raw:
        try:
            F = round(float(V), 2)
        except (TypeError, ValueError):
            raise HttpError(400, "invalid_charm_sizes", f"Charm sizes must be numbers in millimetres ({V!r} is not).")
        if F not in CharmSizeGrid:
            raise HttpError(400, "invalid_charm_sizes", f"{F:g} mm is not one of the charm sizes ({Grid} mm).")
        Out.append(F)
    if len(set(Out)) != len(Out):
        raise HttpError(400, "invalid_charm_sizes", "Each charm size can be listed only once.")
    if len(Out) != CharmSizesOnOffer or CharmRequiredSize not in Out:
        raise HttpError(400, "invalid_charm_sizes", Rule)
    return sorted(Out)


@dataclass
class ProductSettings:
    Db: object

    def __post_init__(self):
        with self.Db.Connect() as Conn:
            Conn.executescript(Schema)

    def Get(self, Key: str):
        R = self.Db.One("SELECT value_json FROM product_settings WHERE key = ?", (Key,))
        return json.loads(R["value_json"]) if R else Defaults[Key]

    def Set(self, Key: str, Value, By: str, Note: str = "") -> None:
        T = Now()
        with self.Db.Transaction() as Conn:
            Conn.execute("INSERT INTO product_settings (key, value_json, updated_at, updated_by) VALUES (?,?,?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at, "
                         "updated_by = excluded.updated_by", (Key, Dumps(Value), T, By))
            Conn.execute("INSERT INTO product_settings_log (key, value_json, note, at, by) VALUES (?,?,?,?,?)",
                         (Key, Dumps(Value), (Note or "")[:300], T, By))

    def Available(self, Product: str) -> bool:
        """Customer availability of one product (Admin → Settings → Products)."""
        return self.Get(AvailabilityKeys[Product]) is True

    def AvailableProducts(self) -> tuple:
        """The products customers may design and order, in the canonical order (charms, then rings)."""
        return tuple(P for P in All if self.Available(P))

    def Launched(self, Product: str) -> bool:
        """The product has been on offer to customers: ON now, ON by default (rings), or switched ON at some time (the
        settings log). OFF after that, it is paused — its gallery designs stay viewable, read-only; a product never yet
        on offer stays hidden from customers."""
        Key = AvailabilityKeys[Product]
        if self.Available(Product) or Defaults[Key] is True:
            return True
        return self.Db.One("SELECT 1 AS x FROM product_settings_log WHERE key = ? AND value_json = ? LIMIT 1",
                           (Key, Dumps(True))) is not None

    def SetAvailable(self, Product: str, On: bool, By: str, Note: str = "") -> None:
        self.Set(AvailabilityKeys[Product], bool(On), By, Note)

    def MovieOn(self, Product: str) -> bool:
        """The 360° movie switch of a product (Admin → Settings → Products): OFF = no movie is made or shown in that
        product's flow; existing movies stay (hidden) and are shown again once the switch is ON."""
        return self.Get(MovieKeys[Product]) is not False

    def SetMovieOn(self, Product: str, On: bool, By: str, Note: str = "") -> None:
        self.Set(MovieKeys[Product], bool(On), By, Note)

    def ChangedAt(self, Key: str) -> dict | None:
        R = self.Db.One("SELECT updated_at, updated_by FROM product_settings WHERE key = ?", (Key,))
        return {"updated_at": R["updated_at"], "updated_by": R["updated_by"]} if R else None

    @property
    def CharmsAvailable(self) -> bool:
        return self.Available(Charm)

    @property
    def CharmSizes(self) -> list[float]:
        """The sizes on offer — the three priced sizes (their ranks are the price levels) — smallest first (stored sorted;
        sorted again for a list saved before the rule). Customers choose from CharmSizesEnabled."""
        return sorted(float(V) for V in self.Get("charm_sizes"))

    @property
    def CharmSizesEnabled(self) -> list[float]:
        """The sizes customers can choose (Admin → Charm sizes, "Shown to customers"), smallest first: the sizes on offer
        that are switched on — all of them while none is stored, or none of the stored ones is still on offer. Never a
        fixed list: Customize, the bag, checkout, quotes and the site's wording all follow it."""
        Raw = self.Get("charm_sizes_enabled")
        On = {float(V) for V in Raw} if isinstance(Raw, list) else set()
        return [S for S in self.CharmSizes if S in On] or self.CharmSizes

    def IsValidCharmSize(self, Size) -> bool:
        """A size a customer can choose now (a bag line, an order or a quote in another size asks for one of these)."""
        try:
            return float(Size) in self.CharmSizesEnabled
        except (TypeError, ValueError):
            return False

    def CharmLevel(self, Size) -> str | None:
        """The price level of a size on offer ("1" small · "2" medium · "3" large); None when it is not on offer."""
        return CharmLevelOf(Size, self.CharmSizes)

    def _Stored(self, Key: str) -> bool:
        return self.Db.One("SELECT 1 AS x FROM product_settings WHERE key = ?", (Key,)) is not None

    @property
    def CharmLevelNames(self) -> dict:
        """{"1": "Delicate", "2": "Classic", "3": "Bold"}: each level's name. A site that named its sizes before the levels
        (charm_size_names) keeps those names, read by the rank of its sizes."""
        if self._Stored("charm_level_names") or not self._Stored("charm_size_names"):
            return {K: V for K, V in (self.Get("charm_level_names") or {}).items() if K in CharmLevels and V}
        return CharmLevelNamesOf(self.Get("charm_size_names") or {}, self.CharmSizes)

    @property
    def CharmSizeNames(self) -> dict:
        """{"14": "Classic"} for the sizes on offer that have a name: their levels' names."""
        Names = self.CharmLevelNames
        return {SizeKey(S): Names[L] for S in self.CharmSizes for L in (self.CharmLevel(S),) if L and Names.get(L)}

    @property
    def CharmDefaultSize(self) -> float:
        """The recommended size (products.CharmDefaultSize) among the sizes customers can choose: the configured one when it
        is one of them, else their middle one — the only one when a single size is shown."""
        return CharmDefaultSize(self.CharmSizesEnabled, self.Get("charm_default_size"))

    def CharmSizeOptions(self, Every: bool = False) -> list[dict]:
        """The size choices customers have: size, name, recommended, the choice label ('14 mm — Classic · Recommended') and
        the price level. Every: all the sizes on offer, each saying whether customers can choose it (the Admin)."""
        return CharmSizeOptions(self.CharmSizes, self.CharmSizeNames, self.CharmDefaultSize, self.CharmSizesEnabled, Only=not Every)

    def State(self) -> dict:
        Log = self.Db.All("SELECT key, value_json, note, at, by FROM product_settings_log ORDER BY id DESC LIMIT 30")
        return {"availability": {P: self.Available(P) for P in All}, "launched": {P: self.Launched(P) for P in All},
                "availability_changed": {P: self.ChangedAt(AvailabilityKeys[P]) for P in All},
                "movies": {P: self.MovieOn(P) for P in All},
                "movies_changed": {P: self.ChangedAt(MovieKeys[P]) for P in All},
                "charms_available": self.CharmsAvailable,                       # the older names, still answered
                "charms_available_changed": self.ChangedAt(AvailabilityKeys[Charm]),
                "charm_sizes": self.CharmSizes, "charm_size_names": self.CharmSizeNames,
                "charm_level_names": self.CharmLevelNames, "charm_level_labels": CharmLevelLabels,
                "charm_default_size": self.CharmDefaultSize, "charm_size_options": self.CharmSizeOptions(Every=True),
                "charm_sizes_enabled": self.CharmSizesEnabled,
                "charm_size_definition": CharmSizeDefinition,
                "charm_size_grid": list(CharmSizeGrid), "charm_required_size": CharmRequiredSize,
                "charm_sizes_on_offer": CharmSizesOnOffer,
                "charm_size_limits": {"min": CharmSizeMin, "max": CharmSizeMax, "count": CharmSizesMax, "name": CharmSizeNameMax},
                "log": [{**L, "value": json.loads(L.pop("value_json"))} for L in Log]}


# ── who sees charms ───────────────────────────────────────────────────────────
def AdminPreview(Ctx: Context, Request) -> bool:
    """A browser signed in to the Admin that opened the site in preview on purpose (?preview=on: the PreviewHeader on its
    requests) sees the products hidden from customers. Without it an Admin's browser sees exactly what customers see
    (owner, 5 Nov 2026: no ring choice or ring action while Rings are OFF)."""
    if Request is None or not hasattr(Request, "headers") or Request.headers.get(PreviewHeader) != "1":
        return False
    from p3 import adminauth
    try:
        return bool(hasattr(Request, "headers") and any(adminauth.Validate(Ctx, T) for T in adminauth.Tokens(Request)))
    except Exception:  # noqa: BLE001 — never let a preview check break the customer site
        return False


def _Available(Ctx: Context) -> tuple:
    Settings = getattr(Ctx, "Products", None)
    return Settings.AvailableProducts() if Settings is not None else (Ring,)


def VisibleProducts(Ctx: Context, Request=None) -> tuple:
    """The products this browser may see: the ones available to customers — every product for a browser signed in
    to the Admin (a preview of what is hidden)."""
    Available = _Available(Ctx)
    if Available == All:
        return All
    return All if AdminPreview(Ctx, Request) else Available


def PreviewedProducts(Ctx: Context, Request=None) -> tuple:
    """The products this browser sees only because it is signed in to the Admin (hidden from customers)."""
    Available = _Available(Ctx)
    return tuple(P for P in VisibleProducts(Ctx, Request) if P not in Available)


def PausedProducts(Ctx: Context, Request=None) -> tuple:
    """The products switched OFF after their launch that this browser does not otherwise see: their gallery designs stay
    viewable — pages, old links, Favorites, Share, the sitemap — read-only."""
    Settings = getattr(Ctx, "Products", None)
    if Settings is None:
        return ()
    Visible = VisibleProducts(Ctx, Request)
    return tuple(P for P in All if P not in Visible and Settings.Launched(P))


def RequireOffered(Product: str, Visible: tuple) -> str:
    """A production action on a design of a product not on offer is refused; the design itself stays viewable."""
    if (Product or Ring) not in Visible:
        raise HttpError(409, "design_unavailable", UnavailableError)
    return Product


def TestAccount(Ctx: Context, Who) -> bool:
    """The caller's account is a test account: its address is at a domain reserved for tests (p3/testrecords.py)."""
    from p3.testrecords import ReservedEmail
    return Who is not None and ReservedEmail(Ctx.Accounts.Profile(Who).get("email"))


def RequirePreviewAccount(Ctx: Context, Product: str, Request=None, Who=None) -> str:
    """A product this browser sees only as an Admin's preview (hidden from customers) is designed, bagged, ordered and
    quoted only on a test account — never on a customer's own, so a preview never places a real order or spends a
    customer's credits (409 preview_test_account_only)."""
    if (Product or Ring) in PreviewedProducts(Ctx, Request) and not TestAccount(Ctx, Who):
        raise HttpError(409, "preview_test_account_only", PreviewOnlyError)
    return Product


def RequireOnOffer(Ctx: Context, Product: str, Request=None, Who=None) -> str:
    """RequireOffered for this browser: the products on offer to it (an Admin's browser previews the hidden ones, on a
    test account only: RequirePreviewAccount)."""
    RequireOffered(Product, VisibleProducts(Ctx, Request))
    return RequirePreviewAccount(Ctx, Product, Request, Who)


def OfferedTo(Ctx: Context, Request=None, Who=None) -> tuple:
    """The products this caller may bag, order and ask a quote for: the ones this browser sees, less those it sees only
    as an Admin's preview unless the account is a test account (RequirePreviewAccount)."""
    Visible, Available = VisibleProducts(Ctx, Request), _Available(Ctx)
    if Visible == Available or TestAccount(Ctx, Who):
        return Visible
    return tuple(P for P in Visible if P in Available)


def CharmsVisible(Ctx: Context, Request=None) -> bool:
    """Charms on the customer site: switched on for customers, or previewed by a signed-in admin."""
    return Charm in VisibleProducts(Ctx, Request)


def MovieAvailable(Ctx: Context, Product: str) -> bool:
    """Whether a 360° movie is made and shown for this product (its Admin switch; ON when nothing is configured)."""
    Settings = getattr(Ctx, "Products", None)
    return Settings.MovieOn(Product) if Settings is not None else True


def RequireVisible(Ctx: Context, Product: str, Request=None) -> str:
    """A customer request for a product they cannot see is refused as if the product did not exist."""
    if Product not in VisibleProducts(Ctx, Request):
        raise HttpError(400, "unknown_product", "Unknown product.")
    return Product
