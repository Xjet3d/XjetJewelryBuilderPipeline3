"""Saved designs: list and full state (for reload recovery)."""

from p3 import ringids as RingIds
from p3 import sessions as Sessions
from p3.accounts import Principal
from p3.context import Context, HttpError
from p3.db import Now
from p3.customize import CustomizeService
from p3.images import ImageService


class DesignService:
    def __init__(self, Ctx: Context, Images: ImageService, Customize: CustomizeService):
        self.Ctx = Ctx
        self.Images = Images
        self.Customize = Customize

    def _Thumb(self, DesignId: str, SelectedId: str | None) -> str | None:
        if SelectedId:
            C = self.Ctx.Db.One("SELECT asset_path FROM candidates WHERE id = ?", (SelectedId,))
            if C and C["asset_path"]:
                return self.Ctx.AssetUrl(C["asset_path"])
        C = self.Ctx.Db.One("SELECT c.asset_path FROM candidates c JOIN batches b ON b.id = c.batch_id "
                            "WHERE b.design_id = ? AND c.status = 'ready' ORDER BY b.created_at DESC, c.slot LIMIT 1", (DesignId,))
        return self.Ctx.AssetUrl(C["asset_path"]) if C else None

    def List(self, Who: Principal, Visible: tuple = ("ring", "charm")) -> list[dict]:
        """The customer's own designs and the shared gallery designs they started, latest activity first — of the
        products this browser can see (a charm stays out of a ring-only site)."""
        Out = []
        for D in self.Ctx.Db.All("SELECT * FROM designs WHERE owner_account_id = ? AND removed_at IS NULL "
                                 "ORDER BY updated_at DESC LIMIT 100", (Who.AccountId,)):
            Out.append({"id": D["id"], "title": D["title"], "thumbnail_url": self._Thumb(D["id"], D["selected_candidate_id"]),
                        "updated_at": D["updated_at"], "created_at": D["created_at"], "shared": False,
                        "origin": "gallery" if D.get("source_design_id") else "prompt",
                        "product_type": D.get("product_type") or "ring"})
        for U in self.Ctx.Db.All("SELECT u.*, d.title, d.product_type FROM gallery_uses u JOIN designs d ON d.id = u.design_id "
                                 "WHERE u.owner_account_id = ? AND u.removed_at IS NULL ORDER BY u.last_active_at DESC LIMIT 100",
                                 (Who.AccountId,)):
            Out.append({"id": U["design_id"], "title": U["title"],
                        "thumbnail_url": self._Thumb(U["design_id"], U["selected_candidate_id"] or U["source_candidate_id"]),
                        "updated_at": U["last_active_at"], "created_at": U["started_at"], "shared": True, "origin": "gallery",
                        "product_type": U.get("product_type") or "ring"})
        Out = [X for X in Out if X["product_type"] in Visible]
        Out.sort(key=lambda X: X["updated_at"] or "", reverse=True)
        return Out[:100]

    def Remove(self, Who: Principal, DesignId: str) -> dict:
        """Remove from My Designs: a design of the customer's own is hidden (soft), a shared gallery design
        is unlinked. The Admin keeps the journey and its statistics; the bag is not affected."""
        Db = self.Ctx.Db
        D = Db.One("SELECT * FROM designs WHERE id = ?", (DesignId,))
        T = Now()
        if D and D["owner_account_id"] == Who.AccountId and not D.get("removed_at"):
            Db.Execute("UPDATE designs SET removed_at = ? WHERE id = ?", (T, DesignId))
            Sessions.Record(self.Ctx, Who.AccountId, "design_removed", DesignId)
            return {"removed": True, "shared": False}
        Use = Db.One("SELECT id FROM gallery_uses WHERE design_id = ? AND owner_account_id = ? AND removed_at IS NULL",
                     (DesignId, Who.AccountId)) if D else None
        if Use:
            Db.Execute("UPDATE gallery_uses SET removed_at = ? WHERE id = ?", (T, Use["id"]))
            Sessions.Record(self.Ctx, Who.AccountId, "design_removed", DesignId, shared=True, use_id=Use["id"])
            return {"removed": True, "shared": True}
        raise HttpError(404, "design_not_found", "Design not found.")

    def Get(self, Who: Principal, DesignId: str, Visible: tuple = ("ring", "charm")) -> dict:
        D = self.Images.RequireDesign(Who, DesignId, Visible)
        Batches = [self.Images.GetBatch(B["id"]) for B in self.Ctx.Db.All(
            "SELECT id FROM batches WHERE design_id = ? ORDER BY created_at", (DesignId,))]
        # A shared XJet master design: the customer's own selection and choices, the images shared.
        Use = None if D["owner_account_id"] == Who.AccountId else self.Ctx.Db.One(
            "SELECT * FROM gallery_uses WHERE design_id = ? AND owner_account_id = ? AND removed_at IS NULL", (DesignId, Who.AccountId))
        Selected = (Use["selected_candidate_id"] or Use["source_candidate_id"]) if Use else D["selected_candidate_id"]
        Customization = None
        if Selected:
            Row = self.Ctx.Db.One("SELECT * FROM customizations WHERE owner_account_id = ? AND design_id = ? AND candidate_id = ?",
                                  (Who.AccountId, DesignId, Selected))
            Customization = self.Customize.ToJson(Row) if Row else None
        SourceCandidate = Use["source_candidate_id"] if Use else D.get("source_candidate_id")
        # Derived from another design: a gallery journey when that design belongs to someone else (a customer's
        # fork of a master); the owner's own refinement of their own master is a design of their own
        Source = self.Ctx.Db.One("SELECT owner_account_id FROM designs WHERE id = ?", (D["source_design_id"],)) if D.get("source_design_id") else None
        FromGallery = bool(Use) or bool(D.get("source_design_id") and (Source is None or Source["owner_account_id"] != D["owner_account_id"]))
        # Privacy: a shared master design shows its images and its name only — never its prompt, its reference image
        # or the words of its refinements; a design forked from someone else's master keeps the master's prompt out of
        # sight too (the customer's own refinement words are theirs to see).
        if Use:
            for B in Batches:
                B["user_text"] = None
                B["reference_url"] = None
        return {"id": D["id"], "title": D["title"], "prompt": None if FromGallery else D["prompt"],
                "product_type": D.get("product_type") or "ring",
                "selected_candidate_id": Selected, "created_at": Use["started_at"] if Use else D["created_at"],
                "updated_at": Use["last_active_at"] if Use else D["updated_at"], "batches": Batches,
                "customization": Customization, "shared": bool(Use),
                "origin": "gallery" if FromGallery else "prompt",
                "source_ring_id": RingIds.CandidateRef(self.Ctx.Db, SourceCandidate) if SourceCandidate else None,
                # Set once the design has a movie, 3D, order or gallery tile: a refinement is then saved as a new design
                "refinement_creates_new_design": "shared" if Use else self.Images.CommittedReason(DesignId)}
