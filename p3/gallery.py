"""Inspiration Gallery — shared XJet master designs as an entry point into the design flow.

An admin adds a design to the gallery; its tile shows the design's selected image. The design stays
ONE shared XJet master design: "Make it yours" links the customer to it (gallery_uses) instead of
copying it. The customer's own selection lives on that link, their Customize choices in
customizations (per customer), their bag lines in bag_lines; the images, the 360° movies and the
Hi3D raw model belong to the master design and are reused by everyone. A refinement by a customer
forks into a design of their own (p3/images.py), leaving the master untouched.

Every use is its own journey in the Admin (session id = the use id), so a master design can be
opened to see every customer who used it, and all masters can be compared.
"""

import re
import unicodedata

from p3 import ringids as RingIds
from p3 import sessions as Sessions
from p3.accounts import Principal
from p3.context import Context, HttpError
from p3.db import NewId, Now


def ShareSlug(Title: str) -> str:
    """Link name from a design name: lower-case ASCII words joined by '-' ("Aurora Twist" → "aurora-twist",
    "Éternité" → "eternite"); at most 60 characters."""
    Ascii = unicodedata.normalize("NFKD", Title or "").encode("ascii", "ignore").decode("ascii").lower()
    return "-".join(re.findall(r"[a-z0-9]+", Ascii))[:60].strip("-")


class GalleryService:
    def __init__(self, Ctx: Context):
        self.Ctx = Ctx

    # ── reading ──────────────────────────────────────────────────────────
    def _Rows(self) -> list[dict]:
        return self.Ctx.Db.All(
            "SELECT g.*, d.title, d.prompt, d.ring_no, d.charm_no, d.product_type, d.owner_account_id, d.share_slug, c.asset_path, "
            "c.status AS candidate_status "
            "FROM gallery_items g JOIN designs d ON d.id = g.design_id JOIN candidates c ON c.id = g.candidate_id "
            "ORDER BY g.position, g.created_at")

    def List(self, Visible: tuple = ("ring",), WithProduct: bool = False) -> list[dict]:
        """Public tiles: the title, the image and the master design's id (what a favorite refers to) — nothing
        about the design's owner. Only the products the customer can see (Visible); the product of each tile is
        given only when the customer can see more than rings (WithProduct), so the ring-only site is unchanged."""
        Out = []
        for R in self._Rows():
            if R["candidate_status"] != "ready" or not R["asset_path"] or (R["product_type"] or "ring") not in Visible:
                continue
            Tile = {"id": R["id"], "design_id": R["design_id"], "title": R["title"], "image_url": self.Ctx.AssetUrl(R["asset_path"])}
            if WithProduct:
                Tile["product_type"] = R["product_type"] or "ring"
            Out.append(Tile)
        return Out

    # ── sharing: a clean customer link by design name ────────────────────
    def ShareFor(self, ItemId: str, Visible: tuple = ("ring", "charm")) -> dict | None:
        """The share details of a gallery design. Its link name comes from the design name the first time it is
        shared ("Aurora Twist" → aurora-twist, unique) and then never changes, so every link that was ever
        sent keeps working, whatever the design is called later."""
        R = self.Ctx.Db.One("SELECT g.id, g.design_id, d.title, d.share_slug, d.product_type, c.asset_path, c.status AS candidate_status "
                            "FROM gallery_items g JOIN designs d ON d.id = g.design_id JOIN candidates c ON c.id = g.candidate_id "
                            "WHERE g.id = ?", (ItemId,))
        if R is None or R["candidate_status"] != "ready" or not R["asset_path"] or (R["product_type"] or "ring") not in Visible:
            return None
        Slug = R["share_slug"] or self._AssignSlug(R["design_id"], R["title"])
        return {"item_id": R["id"], "design_id": R["design_id"], "slug": Slug, "title": R["title"],
                "image_url": self.Ctx.AssetUrl(R["asset_path"]), "product_type": R["product_type"] or "ring"}

    def _AssignSlug(self, DesignId: str, Title: str) -> str:
        Db = self.Ctx.Db
        Base = ShareSlug(Title) or "design"
        Taken = {R["share_slug"] for R in Db.All("SELECT share_slug FROM designs WHERE share_slug IS NOT NULL")}
        Slug, N = Base, 2
        while Slug in Taken:
            Slug, N = f"{Base}-{N}", N + 1
        Db.Execute("UPDATE designs SET share_slug = ? WHERE id = ? AND share_slug IS NULL", (Slug, DesignId))
        return Db.One("SELECT share_slug FROM designs WHERE id = ?", (DesignId,))["share_slug"]

    def Resolve(self, Slug: str, Visible: tuple = ("ring", "charm")) -> dict | None:
        """The gallery design behind a share link: by its assigned link name first, else by its current name."""
        S = (Slug or "").strip().lower()
        if not S:
            return None
        Rows = [R for R in self._Rows() if R["candidate_status"] == "ready" and R["asset_path"] and (R["product_type"] or "ring") in Visible]
        Hit = next((R for R in Rows if (R["share_slug"] or "").lower() == S), None) \
            or next((R for R in Rows if ShareSlug(R["title"]) == S), None)
        if Hit is None:
            return None
        return {"item_id": Hit["id"], "design_id": Hit["design_id"], "slug": Hit["share_slug"] or ShareSlug(Hit["title"]),
                "title": Hit["title"], "image_url": self.Ctx.AssetUrl(Hit["asset_path"]), "product_type": Hit["product_type"] or "ring"}

    # ── ♥ favorites: a saved reference to a master design, never a copy ──
    def Favorites(self, Who: Principal, Visible: tuple = ("ring",), WithProduct: bool = False) -> list[dict]:
        """The customer's favorites that are in the gallery now, newest first (public tile data only)."""
        Saved = self.Ctx.Db.All("SELECT design_id, created_at FROM gallery_favorites WHERE owner_account_id = ? "
                                "ORDER BY created_at DESC, rowid DESC", (Who.AccountId,))
        Order = {F["design_id"]: I for I, F in enumerate(Saved)}
        return sorted([T for T in self.List(Visible, WithProduct) if T["design_id"] in Order], key=lambda T: Order[T["design_id"]])

    def Favorite(self, Who: Principal, DesignId: str, Visible: tuple = ("ring",), WithProduct: bool = False) -> list[dict]:
        if not any(T["design_id"] == DesignId for T in self.List(Visible)):
            raise HttpError(404, "not_in_gallery", "This design is not in the Inspiration Gallery.")
        self.Ctx.Db.Execute("INSERT OR IGNORE INTO gallery_favorites (owner_account_id, design_id, created_at) VALUES (?,?,?)",
                            (Who.AccountId, DesignId, Now()))
        return self.Favorites(Who, Visible, WithProduct)

    def Unfavorite(self, Who: Principal, DesignId: str, Visible: tuple = ("ring",), WithProduct: bool = False) -> list[dict]:
        self.Ctx.Db.Execute("DELETE FROM gallery_favorites WHERE owner_account_id = ? AND design_id = ?", (Who.AccountId, DesignId))
        return self.Favorites(Who, Visible, WithProduct)

    def Stats(self, DesignIds: list[str]) -> dict[str, dict]:
        """Usage of master designs: who selected them and how far they went (every journey counts — a
        customer choosing a design is real usage whatever mode generated the design's images)."""
        if not DesignIds:
            return {}
        Db = self.Ctx.Db
        Q = ",".join("?" * len(DesignIds))
        Out = {D: {"users": 0, "sessions": 0, "customize": 0, "bag": 0, "checkout": 0, "orders": 0, "three_d": 0, "forks": 0,
                   "favorites": 0, "last_used_at": None} for D in DesignIds}
        Users: dict[str, set] = {D: set() for D in DesignIds}
        for U in Db.All(f"SELECT u.* FROM gallery_uses u WHERE u.design_id IN ({Q})", DesignIds):
            S = Out[U["design_id"]]
            S["sessions"] += 1
            Users[U["design_id"]].add(U["owner_account_id"])
            S["last_used_at"] = max(S["last_used_at"] or "", U["last_active_at"])
            Args = (U["design_id"], U["owner_account_id"])
            if Db.One("SELECT 1 AS x FROM customizations WHERE design_id = ? AND owner_account_id = ?", Args):
                S["customize"] += 1
            if Db.One("SELECT 1 AS x FROM bag_lines WHERE design_id = ? AND owner_account_id = ?", Args):
                S["bag"] += 1
            if Db.One("SELECT 1 AS x FROM session_events WHERE design_id = ? AND owner_account_id = ? AND kind = 'checkout_clicked'", Args):
                S["checkout"] += 1
            if Db.One("SELECT 1 AS x FROM order_lines l JOIN orders o ON o.id = l.order_id WHERE l.design_id = ? AND o.owner_account_id = ? "
                      "AND o.status != 'cancelled'", Args):
                S["orders"] += 1
        for D in DesignIds:
            Out[D]["users"] = len(Users[D])
            Out[D]["three_d"] = int(Db.One("SELECT COUNT(*) AS n FROM session_3d WHERE design_id = ?", (D,))["n"])
            Out[D]["forks"] = int(Db.One("SELECT COUNT(*) AS n FROM designs WHERE source_design_id = ?", (D,))["n"])
            Out[D]["favorites"] = int(Db.One("SELECT COUNT(*) AS n FROM gallery_favorites WHERE design_id = ?", (D,))["n"])
        return Out

    def AdminList(self) -> list[dict]:
        """Gallery tiles in display order, with usage statistics; then master designs no longer in the
        gallery that customers still use (position None)."""
        Rows = self._Rows()
        InGallery = {R["design_id"] for R in Rows}
        Former = self.Ctx.Db.All("SELECT DISTINCT d.id AS design_id, d.title, d.prompt, d.ring_no, d.charm_no, d.product_type, "
                                 "d.owner_account_id "
                                 "FROM gallery_uses u JOIN designs d ON d.id = u.design_id "
                                 + (f"WHERE d.id NOT IN ({','.join('?' * len(InGallery))})" if InGallery else ""), list(InGallery))
        Ids = [R["design_id"] for R in Rows] + [F["design_id"] for F in Former]
        Refs = RingIds.CandidateRefs(self.Ctx.Db, Ids)
        Stats = self.Stats(Ids)
        Names: dict[str, int] = {}
        for R in Rows:
            Names[R["title"].strip().lower()] = Names.get(R["title"].strip().lower(), 0) + 1
        Out = [{"id": R["id"], "design_id": R["design_id"], "candidate_id": R["candidate_id"],
                "ring_id": Refs.get(R["candidate_id"]), "design_ring_id": RingIds.Ref(R), "product_type": R["product_type"] or "ring",
                "title": R["title"], "prompt": R["prompt"], "image_url": self.Ctx.AssetUrl(R["asset_path"]),
                # Every master design should have a distinctive name; a shared one is flagged for renaming
                "duplicate_name": Names.get(R["title"].strip().lower(), 0) > 1,
                "ready": R["candidate_status"] == "ready" and bool(R["asset_path"]), "in_gallery": True,
                "position": R["position"], "created_at": R["created_at"], "created_by": R["created_by"],
                **Stats[R["design_id"]]} for R in Rows]
        for F in Former:
            Sel = self.Ctx.Db.One("SELECT selected_candidate_id FROM designs WHERE id = ?", (F["design_id"],))
            C = self.Ctx.Db.One("SELECT asset_path FROM candidates WHERE id = ?", (Sel["selected_candidate_id"],)) \
                if Sel and Sel["selected_candidate_id"] else None
            Out.append({"id": None, "design_id": F["design_id"], "candidate_id": Sel["selected_candidate_id"] if Sel else None,
                        "ring_id": Refs.get(Sel["selected_candidate_id"]) if Sel else None,
                        "design_ring_id": RingIds.Ref(F), "product_type": F["product_type"] or "ring",
                        "title": F["title"], "prompt": F["prompt"],
                        "image_url": self.Ctx.AssetUrl(C["asset_path"]) if C else None, "ready": bool(C),
                        "in_gallery": False, "position": None, "created_at": None, "created_by": None,
                        **Stats[F["design_id"]]})
        return Out

    def ForDesign(self, DesignId: str) -> dict | None:
        return next((I for I in self.AdminList() if I["design_id"] == DesignId and I["in_gallery"]), None)

    def Usage(self, DesignId: str) -> list[dict]:
        """Every customer journey on a master design, most recent activity first."""
        Uses = self.Ctx.Db.All("SELECT id FROM gallery_uses WHERE design_id = ? ORDER BY last_active_at DESC", (DesignId,))
        if not Uses:
            return []
        Rows = Sessions.Summaries(self.Ctx, SessionIds=[U["id"] for U in Uses])
        Rows.sort(key=lambda X: X["last_activity_at"] or X["started_at"] or "", reverse=True)
        return Rows

    def UseFor(self, Who: Principal, DesignId: str) -> dict | None:
        return self.Ctx.Db.One("SELECT * FROM gallery_uses WHERE design_id = ? AND owner_account_id = ?",
                               (DesignId, Who.AccountId))

    # ── curation (admin) ─────────────────────────────────────────────────
    def Add(self, DesignId: str, CandidateId: str | None, By: str) -> dict:
        """Add a design (its selected image, or the given option) to the gallery, or change its image."""
        Db = self.Ctx.Db
        D = Db.One("SELECT * FROM designs WHERE id = ?", (DesignId,))
        if D is None:
            raise HttpError(404, "design_not_found", "Design not found.")
        Cid = CandidateId or D["selected_candidate_id"]
        if not Cid:
            raise HttpError(409, "no_selected_image", "This design has no selected image. Choose an option to show first.")
        C = Db.One("SELECT c.* FROM candidates c JOIN batches b ON b.id = c.batch_id WHERE c.id = ? AND b.design_id = ?",
                   (Cid, DesignId))
        if C is None or C["status"] != "ready" or not C["asset_path"]:
            raise HttpError(409, "image_not_ready", "Only a ready image can be shown in the gallery.")
        T = Now()
        Existing = Db.One("SELECT id FROM gallery_items WHERE design_id = ?", (DesignId,))
        if Existing:
            Db.Execute("UPDATE gallery_items SET candidate_id = ?, created_at = ?, created_by = ? WHERE id = ?",
                       (Cid, T, By, Existing["id"]))
            return self._Item(Existing["id"])
        Position = int(Db.One("SELECT COALESCE(MAX(position), 0) AS p FROM gallery_items")["p"]) + 1
        Id = NewId("gal")
        Db.Execute("INSERT INTO gallery_items (id, design_id, candidate_id, position, created_at, created_by) "
                   "VALUES (?,?,?,?,?,?)", (Id, DesignId, Cid, Position, T, By))
        return self._Item(Id)

    def _Item(self, ItemId: str) -> dict:
        I = next((X for X in self.AdminList() if X["id"] == ItemId), None)
        if I is None:
            raise HttpError(404, "gallery_item_not_found", "Gallery item not found.")
        return I

    def Remove(self, ItemId: str) -> None:
        """Take the tile off the site. Customers who started from it keep their link to the master design."""
        self._Item(ItemId)
        self.Ctx.Db.Execute("DELETE FROM gallery_items WHERE id = ?", (ItemId,))
        self._Renumber()

    def Move(self, ItemId: str, Direction: str) -> None:
        Ids = [X["id"] for X in self.AdminList() if X["in_gallery"]]
        if ItemId not in Ids:
            raise HttpError(404, "gallery_item_not_found", "Gallery item not found.")
        I = Ids.index(ItemId)
        J = I - 1 if Direction == "up" else I + 1
        if 0 <= J < len(Ids):
            Ids[I], Ids[J] = Ids[J], Ids[I]
        self._Renumber(Ids)

    def _Renumber(self, Ids: list[str] | None = None) -> None:
        Ids = Ids or [X["id"] for X in self.AdminList() if X["in_gallery"]]
        with self.Ctx.Db.Transaction() as Conn:
            for N, Id in enumerate(Ids, start=1):
                Conn.execute("UPDATE gallery_items SET position = ? WHERE id = ?", (N, Id))

    # ── "Make it yours" (customer) ───────────────────────────────────────
    def Start(self, Who: Principal, ItemId: str, ClientRequestId: str | None = None, Visible: tuple = ("ring", "charm")) -> str:
        """Link the customer to the shared master design (or bring their existing link back to the
        top of My Designs). Nothing is copied, generated or charged. Returns the master design id."""
        Db = self.Ctx.Db
        R = Db.One("SELECT g.*, d.owner_account_id, d.product_type, c.status AS candidate_status FROM gallery_items g "
                   "JOIN designs d ON d.id = g.design_id JOIN candidates c ON c.id = g.candidate_id WHERE g.id = ?", (ItemId,))
        if R is None or R["candidate_status"] != "ready" or (R["product_type"] or "ring") not in Visible:
            raise HttpError(404, "gallery_item_not_found", "This gallery design is no longer available.")
        T = Now()
        if R["owner_account_id"] == Who.AccountId:                   # XJet opening its own design
            Removed = Db.One("SELECT removed_at FROM designs WHERE id = ?", (R["design_id"],))["removed_at"]
            Db.Execute("UPDATE designs SET updated_at = ?, removed_at = NULL WHERE id = ?", (T, R["design_id"]))
            if Removed:                                             # it was taken off My Designs: back it comes
                Sessions.Record(self.Ctx, Who.AccountId, "design_restored", R["design_id"], gallery_item_id=ItemId)
            return R["design_id"]
        Use = self.UseFor(Who, R["design_id"])
        if Use:                                                     # also restores a removed link
            Db.Execute("UPDATE gallery_uses SET last_active_at = ?, removed_at = NULL WHERE id = ?", (T, Use["id"]))
            Sessions.Record(self.Ctx, Who.AccountId, "gallery_reopened", R["design_id"], gallery_item_id=ItemId,
                            use_id=Use["id"])
            return R["design_id"]
        Uid = NewId("use")
        Db.Execute("INSERT INTO gallery_uses (id, design_id, owner_account_id, gallery_item_id, source_candidate_id, "
                   "selected_candidate_id, started_at, last_active_at) VALUES (?,?,?,?,?,?,?,?)",
                   (Uid, R["design_id"], Who.AccountId, ItemId, R["candidate_id"], R["candidate_id"], T, T))
        Sessions.Record(self.Ctx, Who.AccountId, "gallery_started", R["design_id"], gallery_item_id=ItemId, use_id=Uid,
                        source_ring_id=RingIds.CandidateRef(Db, R["candidate_id"]))
        return R["design_id"]
