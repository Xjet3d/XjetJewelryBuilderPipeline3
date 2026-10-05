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

The charm size definition lives here, in one place: the selected size is the height of the main charm
body in millimetres, excluding the standard attachment loop.
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

# ── the charm size: one central definition ───────────────────────────────────
CharmSizeUnit = "mm"
CharmSizeDefinition = {
    "measure": "body_height",
    "unit": CharmSizeUnit,
    "label": "Charm height",
    "text": "The height of the main charm body, excluding the standard attachment loop.",
}
DefaultCharmSizes = (15.0, 20.0, 25.0, 30.0)   # a starting list only — edited in Admin → Settings → Products
CharmSizeMin, CharmSizeMax, CharmSizesMax = 3.0, 100.0, 12


def CharmSizeLabel(Size) -> str:
    return f"{float(Size):g} {CharmSizeUnit}"


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

Defaults = {"charms_available": False, "charm_sizes": list(DefaultCharmSizes)}


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

    def State(self) -> dict:
        Rows = {R["key"]: R for R in self.Db.All("SELECT * FROM product_settings")}
        Log = self.Db.All("SELECT key, value_json, note, at, by FROM product_settings_log ORDER BY id DESC LIMIT 30")
        return {"charms_available": self.CharmsAvailable,
                "charms_available_changed": {K: Rows["charms_available"][K] for K in ("updated_at", "updated_by")}
                if "charms_available" in Rows else None,
                "charm_sizes": self.CharmSizes, "charm_size_definition": CharmSizeDefinition,
                "charm_size_limits": {"min": CharmSizeMin, "max": CharmSizeMax, "count": CharmSizesMax},
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
