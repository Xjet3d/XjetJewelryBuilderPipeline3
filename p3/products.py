"""Product types — Rings and Charms, side by side, each with its own configuration and behaviour.

Every design carries one product type, set when the design is created and never changed afterwards
(a database trigger refuses any change). Everything that came before charms is a ring: the column
defaults to 'ring', so existing designs, sessions and orders are rings without being rewritten.
Refinements, variations, gallery journeys and split refinements inherit the product of the design
they come from.

Customer visibility is a separate switch (Admin → Settings → Products): "Charms available to
customers", OFF by default. While it is off the customer site is the ring-only site of before — no
product choice, no charm text, no charm tiles — and the server refuses charm requests from
customers. A browser signed in to the Admin sees charms anyway (a preview), so the whole charm system
can be built and tested internally before customers see it.

The charm size definition lives here, in one place (CharmSizeDefinition, Charm3DHeight): for now the
selected size is the charm's TOTAL height in millimetres, the attachment loop included — a 20 mm charm is
20 mm from its lowest point to the top of its loop, and its 3D model is scaled as a whole to that height.
"""

import json
from dataclasses import dataclass

from p3.context import Context, HttpError
from p3.db import Dumps, Now

Ring, Charm = "ring", "charm"
All = (Ring, Charm)
Default = Ring
Labels = {Ring: "Ring", Charm: "Charm"}
Plurals = {Ring: "Rings", Charm: "Charms"}
Prefixes = {Ring: "R", Charm: "C"}          # customer-facing IDs: R-1001 … and C-1001 … (separate sequences)

# ── the charm size: one central definition (the only place to change it) ──────
# Decision of 2026-10-05: a charm's size is its total height, the loop included. Customize, the bag, checkout,
# orders, emails, prices (per size), the 3D scaling and the STL all follow this. There is no loop detection or
# loop measurement: should the definition change later (e.g. the body height with a standard loop attached
# separately), change it here and in Charm3DHeight.
CharmSizeUnit = "mm"
CharmSizeDefinition = {
    "measure": "total_height",
    "includes_loop": True,
    "unit": CharmSizeUnit,
    "label": "Charm height",
    "short": "total height incl. loop",
    "text": "The total height of the charm, including the attachment loop at the top.",
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


def CharmSizeOptions(Sizes, Names: dict, Recommended) -> list[dict]:
    """The size choices in order: size, name (or None), recommended, and the choice label."""
    return [{"size": float(S), "name": Names.get(SizeKey(S)) or None, "recommended": float(S) == float(Recommended),
             "label": CharmSizeChoiceLabel(S, Names.get(SizeKey(S)), float(S) == float(Recommended))}
            for S in sorted(float(V) for V in Sizes)]


def SizeLabel(Product: str, RingSize=None, CharmSize=None) -> str | None:
    """'US 7' for a ring, '20 mm' for a charm (None when no size was chosen)."""
    if Product == Charm:
        return CharmSizeLabel(CharmSize) if CharmSize is not None else None
    return f"US {float(RingSize):g}" if RingSize is not None else None


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

Defaults = {"charms_available": False, "charm_sizes": list(DefaultCharmSizes),
            "charm_size_names": dict(DefaultCharmSizeNames), "charm_default_size": DefaultCharmRecommendedSize}


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


def ValidateCharmSizes(Raw) -> list[float]:
    if not isinstance(Raw, (list, tuple)) or not Raw:
        raise HttpError(400, "invalid_charm_sizes", "Give at least one charm size.")
    Out = []
    for V in Raw:
        try:
            F = float(V)
        except (TypeError, ValueError):
            raise HttpError(400, "invalid_charm_sizes", f"Charm sizes must be numbers in millimetres ({V!r} is not).")
        if not CharmSizeMin <= F <= CharmSizeMax:
            raise HttpError(400, "invalid_charm_sizes",
                            f"Charm sizes must be between {CharmSizeMin:g} and {CharmSizeMax:g} mm ({F:g} is not).")
        Out.append(round(F, 2))
    if len(set(Out)) != len(Out):
        raise HttpError(400, "invalid_charm_sizes", "Each charm size can be listed only once.")
    if len(Out) > CharmSizesMax:
        raise HttpError(400, "invalid_charm_sizes", f"At most {CharmSizesMax} charm sizes.")
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

    @property
    def CharmsAvailable(self) -> bool:
        return self.Get("charms_available") is True

    @property
    def CharmSizes(self) -> list[float]:
        return [float(V) for V in self.Get("charm_sizes")]

    def IsValidCharmSize(self, Size) -> bool:
        try:
            return float(Size) in self.CharmSizes
        except (TypeError, ValueError):
            return False

    @property
    def CharmSizeNames(self) -> dict:
        """{"14": "Classic"} for the sizes on offer that have a name."""
        Keys = {SizeKey(S) for S in self.CharmSizes}
        return {K: V for K, V in (self.Get("charm_size_names") or {}).items() if K in Keys and V}

    @property
    def CharmDefaultSize(self) -> float:
        """The recommended size (products.CharmDefaultSize: the configured one when on offer, else the middle)."""
        return CharmDefaultSize(self.CharmSizes, self.Get("charm_default_size"))

    def CharmSizeOptions(self) -> list[dict]:
        """The size choices: size, name, recommended and the choice label ('14 mm — Classic · Recommended')."""
        return CharmSizeOptions(self.CharmSizes, self.CharmSizeNames, self.CharmDefaultSize)

    def State(self) -> dict:
        Rows = {R["key"]: R for R in self.Db.All("SELECT * FROM product_settings")}
        Log = self.Db.All("SELECT key, value_json, note, at, by FROM product_settings_log ORDER BY id DESC LIMIT 30")
        return {"charms_available": self.CharmsAvailable,
                "charms_available_changed": {K: Rows["charms_available"][K] for K in ("updated_at", "updated_by")}
                if "charms_available" in Rows else None,
                "charm_sizes": self.CharmSizes, "charm_size_names": self.CharmSizeNames,
                "charm_default_size": self.CharmDefaultSize, "charm_size_options": self.CharmSizeOptions(),
                "charm_size_definition": CharmSizeDefinition,
                "charm_size_limits": {"min": CharmSizeMin, "max": CharmSizeMax, "count": CharmSizesMax, "name": CharmSizeNameMax},
                "log": [{**L, "value": json.loads(L.pop("value_json"))} for L in Log]}


# ── who sees charms ───────────────────────────────────────────────────────────
def AdminPreview(Ctx: Context, Request) -> bool:
    """A browser signed in to the Admin (its session cookie) previews charms on the customer site."""
    if Request is None:
        return False
    from p3 import adminauth
    Token = Request.cookies.get(adminauth.CookieName) if hasattr(Request, "cookies") else None
    try:
        return bool(Token and Ctx.Settings.AdminKey and adminauth.Validate(Ctx, Token))
    except Exception:  # noqa: BLE001 — never let a preview check break the customer site
        return False


def CharmsVisible(Ctx: Context, Request=None) -> bool:
    """Charms on the customer site: switched on for customers, or previewed by a signed-in admin."""
    Settings = getattr(Ctx, "Products", None)
    if Settings is not None and Settings.CharmsAvailable:
        return True
    return AdminPreview(Ctx, Request)


def VisibleProducts(Ctx: Context, Request=None) -> tuple:
    return All if CharmsVisible(Ctx, Request) else (Ring,)


def RequireVisible(Ctx: Context, Product: str, Request=None) -> str:
    """A customer request for a product they cannot see is refused as if the product did not exist."""
    if Product == Charm and not CharmsVisible(Ctx, Request):
        raise HttpError(400, "unknown_product", "Unknown product.")
    return Product
