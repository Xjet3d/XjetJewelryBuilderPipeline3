"""Four-candidate image batches: initial generation, refinement, slot retry.

Semantics (spec sections 3, 4.2, 4.3, 7.1):
  * one batch = four separately identified candidates from ONE effective prompt;
  * fal nano-banana-pro caps num_images at 4, so each slot is its own
    single-image request with a distinct seed (enables per-slot retry/identity);
  * refinement uses the SELECTED candidate's image as the reference for all four
    slots with the same instruction (refinement images A-D each add their own variation directive) — never text-only as a fallback;
  * exact-duplicate outputs within a batch are retried a bounded number of times;
  * a slot still running at the 90-second limit is finished in the background: its request is asked for the image
    for up to 10 minutes more, never resubmitted (R-41); the free "Retry missing" asks fal for an earlier request's
    image before paying for a new one;
  * async results only ever write their own candidate row — never the design's
    selection — so late results cannot overwrite a different design.
"""

import logging
import secrets
from datetime import datetime, timezone

from p3 import assets
from p3 import products as Products
from p3 import morevariations as MoreVariations
from p3.accounts import Principal, UsageImage
from p3 import credits as Credits
from p3.context import Context, HttpError
from p3.db import NewId, Now
from p3.naming import NameForPrompt, NameForVariation
from p3.providers import endpoints
from p3.providers.base import ProviderError, TransientProviderError
from p3.modelconfig import BuildRequest, Render, SlotDirective, WithDirective
from p3.runner import DownloadWithRetry, FailureFor, JobTimeout, PollUntilDone

Logger = logging.getLogger("p3.images")

MinTextLength = 3
MaxTextLength = 2000

# A slot past images.request_timeout_s (90 s) is "Still finishing" (recovering_since): outside the queue for new requests,
# its request is asked again every RecoveryPollFactor poll intervals (10 s on the server) until images.recovery_window_s
# (10 minutes) has passed, and its image is attached when it arrives — never a new request (R-41, 2026-10-25).
RecoveryPollFactor = 5

# Failures whose request may still have produced an image (it ran out of time, fal could not be reached, the file could
# not be fetched): "Retry missing" asks fal for that image before anything new is paid for. Every other failure is known
# to have ended without a usable image.
OutcomeUnknown = {"timeout", "provider_unreachable", "download_failed", "internal_error"}


def ValidateText(Text: str, What: str) -> str:
    Text = (Text or "").strip()
    if len(Text) < MinTextLength:
        raise HttpError(400, "text_too_short", f"Please describe the {What} in a few more words.")
    if len(Text) > MaxTextLength:
        raise HttpError(400, "text_too_long", f"The {What} is too long (max {MaxTextLength} characters).")
    return Text


# What a customer is told about an option that could not be made: the app's own sentence for the kind of failure,
# never the provider's words (fal.ai's message, an HTTP status, an exception), which stay with the slot for the
# Admin's session page and the log.
CustomerFailureText = {
    "content_policy": "The image service declined this option under its safety rules.",
    "billing": "The design service is temporarily unavailable. Please try again later.",
    "reference_unavailable": "The reference image could not be loaded.",
    "duplicate_output": "This option came out identical to another one.",
    "spend_cap_reached": "AI generation is paused for today. Please try again tomorrow.",
}
GenericFailureText = "This option couldn't be generated."

# A refusal under the AI model's content rules (most often a picture of a real, identifiable person): the same
# description is refused again, so the app never offers it for another paid request — the customer changes it instead.
NotRetryable = {"content_policy"}
PolicyRetryText = ("The image service declined this option under its safety rules and would decline the same description "
                   "again. Please change your description.")


def CustomerError(Code: str | None) -> str:
    return CustomerFailureText.get(Code or "", GenericFailureText)


def _NewSeed() -> int:
    return secrets.randbelow(2**31 - 1) + 1


def BatchStatus(Candidates: list[dict], DesiredCount: int) -> str:
    States = [C["status"] for C in Candidates]
    if len(States) == DesiredCount and all(S == "ready" for S in States):
        return "complete"
    Ready = "ready" in States
    # A slot still finishing after the 90-second limit holds the batch only while nothing is ready: once an image is in,
    # the customer goes on with the ready ones while the late one is attached in the background
    if any(C["status"] in ("pending", "generating") and not (Ready and C.get("recovering_since")) for C in Candidates):
        return "generating" if any(S != "pending" for S in States) else "queued"
    if Ready:
        return "partial"
    return "failed"


class ImageService:
    def __init__(self, Ctx: Context):
        self.Ctx = Ctx

    # ── creation ─────────────────────────────────────────────────────────
    def CreateInitial(self, Who: Principal, Prompt: str, ReferencePng: bytes | None,
                      ClientRequestId: str | None, Product: str = Products.Ring) -> dict:
        """Create a design of one product (ring or charm — fixed for good) and its first four-candidate batch.
        Idempotent per ClientRequestId."""
        Db = self.Ctx.Db
        Prompt = ValidateText(Prompt, "design")
        Product = Products.Normalize(Product)
        self.RequireConfigured(Product)
        if ClientRequestId:
            Existing = Db.One("SELECT id FROM designs WHERE owner_account_id = ? AND client_request_id = ?",
                              (Who.AccountId, ClientRequestId))
            if Existing:
                return self._FirstBatch(Existing["id"])
        Units = Credits.Reserve(self.Ctx, Who, Credits.Design, 1)              # one credit for the request, whatever it generates
        try:
            DesignId = NewId("dsg")
            BatchId = NewId("bat")
            RefPath = None
            if ReferencePng is not None:
                RefPath = f"designs/{DesignId}/references/{BatchId}.png"
                assets.WriteAtomic(self.Ctx.Settings.AssetsDir, RefPath, ReferencePng)
            Title = NameForPrompt(Db, Prompt, Product)              # "Fil Twist": local rules, never another design's name
            with Db.Transaction() as Conn:
                T = Now()
                Conn.execute("INSERT INTO designs (id, owner_account_id, title, prompt, client_request_id, created_at, updated_at, "
                             "ai_mode, product_type) VALUES (?,?,?,?,?,?,?,?,?)",
                             (DesignId, Who.AccountId, Title, Prompt, ClientRequestId, T, T, self.Ctx.Provider.Name, Product))
                self._InsertBatch(Conn, BatchId, DesignId, "initial", None, Prompt, RefPath, None)
            self._StartBatch(BatchId)
        except Exception:
            Credits.Release(self.Ctx, Who.AccountId, Units)
            raise
        return self.GetBatch(BatchId)

    def CreateRefinement(self, Who: Principal, DesignId: str, ParentCandidateId: str, Instruction: str,
                         ClientRequestId: str | None, Visible: tuple = ("ring", "charm")) -> dict:
        Db = self.Ctx.Db
        Instruction = ValidateText(Instruction, "refinement")
        D = self.RequireDesign(Who, DesignId, Visible)
        # A customer refining a shared gallery design gets their own design (a fork): the XJet master
        # design is never changed, and the four new images are theirs alone. The owner's own design is
        # treated the same way once it has become a master (a 360° movie, a 3D request, an order or a
        # gallery tile): the refinement is a new design, so what was made or sold stays exactly as it was.
        Shared = D["owner_account_id"] != Who.AccountId
        Committed = None if Shared else self.CommittedReason(DesignId)
        Fork = Shared or bool(Committed)
        if ClientRequestId:
            Existing = Db.One("SELECT id FROM designs WHERE owner_account_id = ? AND client_request_id = ?",
                              (Who.AccountId, ClientRequestId)) if Fork else None
            if Existing:
                return self._FirstBatch(Existing["id"])
            Existing = Db.One("SELECT id FROM batches WHERE design_id = ? AND client_request_id = ?",
                              (DesignId, ClientRequestId))
            if Existing:
                return self.GetBatch(Existing["id"])
        Parent = self.RequireReadyCandidate(DesignId, ParentCandidateId)
        # The exact selected image must be available; no silent text-only fallback.
        try:
            SourcePath = assets.Resolve(self.Ctx.Settings.AssetsDir, Parent["asset_path"])
        except assets.AssetError:
            SourcePath = None
        if SourcePath is None or not SourcePath.is_file():
            raise HttpError(409, "reference_unavailable",
                            "The selected image could not be loaded for refinement. "
                            "Please pick another image or try again.")
        # "Make more variations" and the like (R-50 follow-up): four variations of the selected design from the fixed
        # instruction; the customer's own words stay the batch's text (the studio's bubble, the Admin's journey)
        PromptText = MoreVariations.Instruction(D.get("product_type") or Products.Ring) if MoreVariations.IsMoreVariations(Instruction) else None
        Units = Credits.Reserve(self.Ctx, Who, Credits.Refinement, 1)          # one credit for the refinement request
        try:
            BatchId = NewId("bat")
            Target = NewId("dsg") if Fork else DesignId
            # The fork keeps its lineage in its name ("Fil Twist" -> "Fil Lattice"), never the master's own name
            ForkTitle = NameForVariation(Db, D["title"], Instruction, D["prompt"], D.get("product_type") or Products.Ring) if Fork else None
            with Db.Transaction() as Conn:
                T = Now()
                if Fork:
                    # A fork is always the same product as the design it was refined from
                    Conn.execute("INSERT INTO designs (id, owner_account_id, title, prompt, client_request_id, created_at, "
                                 "updated_at, ai_mode, source_design_id, source_candidate_id, product_type) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                 (Target, Who.AccountId, ForkTitle, D["prompt"], ClientRequestId, T, T,
                                  self.Ctx.Provider.Name, DesignId, ParentCandidateId, D.get("product_type") or Products.Ring))
                self._InsertBatch(Conn, BatchId, Target, "refine", ParentCandidateId, Instruction,
                                  Parent["asset_path"], None if Fork else ClientRequestId, PromptText)
                Conn.execute("UPDATE designs SET updated_at = ? WHERE id = ?", (T, Target))
                if Fork:
                    Conn.execute("UPDATE gallery_uses SET last_active_at = ? WHERE design_id = ? AND owner_account_id = ?",
                                 (T, DesignId, Who.AccountId))
            if Fork:
                from p3 import ringids as RingIds
                from p3 import sessions as Sessions
                if Shared:
                    Sessions.Record(self.Ctx, Who.AccountId, "gallery_refined", Target, source_design_id=DesignId,
                                    source_ring_id=RingIds.CandidateRef(Db, ParentCandidateId), parent_candidate_id=ParentCandidateId)
                else:
                    Sessions.Record(self.Ctx, Who.AccountId, "design_forked", Target, source_design_id=DesignId,
                                    source_ring_id=RingIds.CandidateRef(Db, ParentCandidateId), parent_candidate_id=ParentCandidateId,
                                    reason=Committed)
            self._StartBatch(BatchId)
        except Exception:
            Credits.Release(self.Ctx, Who.AccountId, Units)
            raise
        return self.GetBatch(BatchId)

    def RequireConfigured(self, Product: str) -> None:
        """A design can only be made for a product the AI configuration supports (charms need their own models)."""
        if not self.Ctx.Models.Supports(Product):
            raise HttpError(409, "product_unavailable", "This product cannot be designed yet.")

    def CommittedReason(self, DesignId: str) -> str | None:
        """Why a design is a master that a refinement must not change any more: it sits in the gallery, has
        an order or quote request, a 3D request, or a 360° movie (made or being made). None = still a draft
        of images only, which a refinement may extend in place."""
        Db = self.Ctx.Db
        if Db.One("SELECT 1 AS x FROM gallery_items WHERE design_id = ?", (DesignId,)):
            return "gallery"
        if Db.One("SELECT 1 AS x FROM order_lines WHERE design_id = ?", (DesignId,)):
            return "order"
        if Db.One("SELECT 1 AS x FROM quote_requests WHERE design_id = ?", (DesignId,)):
            return "quote"
        if Db.One("SELECT 1 AS x FROM session_3d WHERE design_id = ?", (DesignId,)):
            return "3d"
        if Db.One("SELECT 1 AS x FROM movies m JOIN candidates c ON c.id = m.candidate_id JOIN batches b ON b.id = c.batch_id "
                  "WHERE b.design_id = ? AND m.status IN ('queued', 'running', 'ready')", (DesignId,)):
            return "movie"
        return None

    def _InsertBatch(self, Conn, BatchId, DesignId, Kind, ParentId, UserText, RefPath, ClientRequestId, PromptText=None):
        """PromptText: what the prompt carries in place of the customer's words (the "more variations" instruction)."""
        Img = self.Ctx.Gen.Images
        Endpoint = endpoints.ImageEdit if RefPath else endpoints.ImageGenerate
        # The active admin configuration for this endpoint; its version is recorded on the batch so
        # all four requests use it even if a newer version is activated before they are submitted.
        DesignPrompt, Product = Conn.execute("SELECT prompt, product_type FROM designs WHERE id = ?", (DesignId,)).fetchone()
        # A charm never uses a ring configuration. A New Design from an uploaded photo has its own (R-51): the photo is
        # inspiration for a new piece, never "the existing ring" of the refinement instructions.
        Version = (self.Ctx.Models.ActivePhoto(Product or Products.Ring) if Kind == "initial" and RefPath
                   else self.Ctx.Models.ActiveFor(Endpoint, Product or Products.Ring))
        Effective = Render(Version.Params["prompt"], {"user_text": PromptText or UserText, "design_prompt": DesignPrompt})
        T = Now()
        Conn.execute(
            "INSERT INTO batches (id, design_id, kind, parent_candidate_id, user_text, effective_prompt, "
            "endpoint, reference_asset, desired_count, config_version, client_request_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (BatchId, DesignId, Kind, ParentId, UserText, Effective, Endpoint, RefPath,
             Img.CandidatesPerBatch, Version.Id, ClientRequestId, T))
        Seeds = set()
        while len(Seeds) < Img.CandidatesPerBatch:
            Seeds.add(_NewSeed())
        for Slot, Seed in enumerate(sorted(Seeds)):
            Conn.execute("INSERT INTO candidates (id, batch_id, slot, status, seed, created_at, updated_at, credit_ref) "
                         "VALUES (?,?,?,?,?,?,?,?)", (NewId("cand"), BatchId, Slot, "pending", Seed, T, T, BatchId))

    def _StartBatch(self, BatchId: str) -> None:
        for C in self.Ctx.Db.All("SELECT id FROM candidates WHERE batch_id = ? AND status = 'pending'", (BatchId,)):
            self.Ctx.Runner.Spawn(f"cand:{C['id']}", self._Drive(C["id"]))

    # ── retry ────────────────────────────────────────────────────────────
    def RetryCandidate(self, Who: Principal, CandidateId: str) -> dict:
        """"Generate another option": a new image the customer asks for in place of a failed one — 1 credit, a new
        request with a new seed."""
        Cand, Batch = self._OwnedCandidate(Who, CandidateId)
        if Cand["status"] != "failed":
            raise HttpError(409, "not_failed", "Only a failed image can be retried.")
        if Cand["error_code"] in NotRetryable:                                 # refused again: nothing is reserved or paid
            raise HttpError(409, "not_retryable", PolicyRetryText)
        Credits.Reserve(self.Ctx, Who, Credits.Option, 1)                      # "Generate another option": one credit per click
        self._RecordRetry(Who, Batch, [Cand])
        self._ResetForRetry(CandidateId, Credits.ActionId())
        self.Ctx.Runner.Spawn(f"cand:{CandidateId}", self._Drive(CandidateId))
        return self.GetBatch(Batch["id"])

    async def RetryFailed(self, Who: Principal, BatchId: str) -> dict:
        """"Retry missing" (and "Try again"): free (T-10, 2026-10-25) — the request's own credit covers its missing
        options, so nothing is reserved, and a request whose credit was restored is never charged again. An option
        whose request may still have produced an image (OutcomeUnknown) gets that image: fal is asked for it, and a
        request still running is waited for ("Still finishing"); only an option whose request ended without an image
        gets a new one. A content refusal is never asked again."""
        Db = self.Ctx.Db
        Batch = self._OwnedBatch(Who, BatchId)
        Failed = Db.All("SELECT * FROM candidates WHERE batch_id = ? AND status = 'failed'", (BatchId,))
        Retry = [C for C in Failed if C["error_code"] not in NotRetryable]     # a content refusal is never asked again
        if Failed and not Retry:
            raise HttpError(409, "not_retryable", PolicyRetryText)
        if Retry:
            self._RecordRetry(Who, Batch, Retry, Free=True)
        for C in Retry:
            Earlier = await self._EarlierRequest(Batch, C) if C["error_code"] in OutcomeUnknown else None
            if Earlier:                     # its image is there, or still coming: the same request, nothing new is paid for
                Db.Update("candidates", C["id"], status="generating", error=None, error_code=None,
                          recovering_since=Now() if Earlier == "running" else None)
            else:
                self._ResetForRetry(C["id"], C["credit_ref"] or Batch["id"])
            self.Ctx.Runner.Spawn(f"cand:{C['id']}", self._Drive(C["id"]))
        return self.GetBatch(Batch["id"])

    async def _EarlierRequest(self, Batch: dict, Cand: dict) -> str | None:
        """What became of the slot's earlier request: "completed" (an image to fetch), "running" (still coming, or fal
        cannot be reached just now — waited for rather than paid for again) or None (it ended without an image)."""
        RequestId = Cand["provider_request_id"]
        if not RequestId or not self.Ctx.Provider.Owns(RequestId):
            return None
        try:
            St = await self.Ctx.Provider.Status(Batch["endpoint"], RequestId)
        except TransientProviderError:
            return "running"
        except ProviderError:
            return None
        if St.State == "completed":
            return None if St.Error else "completed"
        return "running"

    def _RecordRetry(self, Who: Principal, Batch: dict, Cands: list[dict], Free: bool = False) -> None:
        """The customer's "Generate another option" (1 credit) or "Retry missing" / "Try again" (free) in the session's
        Journey: which options were asked for again and why each had failed, so every second request shows on the
        Admin's session page."""
        from p3 import ringids as RingIds, sessions as Sessions
        Refs = RingIds.CandidateRefs(self.Ctx.Db, [Batch["design_id"]])
        Sessions.Record(self.Ctx, Who.AccountId, "option_requested", Batch["design_id"],
                        options=[Refs.get(C["id"]) or f"option {C['slot'] + 1}" for C in Cands],
                        after=sorted({C["error_code"] or "failed" for C in Cands}), **({"free": True} if Free else {}))

    def _ResetForRetry(self, CandidateId: str, CreditRef: str) -> None:
        self.Ctx.Db.Update("candidates", CandidateId, status="pending", provider_request_id=None, recovering_since=None,
                           error=None, error_code=None, duplicate_retries=0, seed=_NewSeed(), credit_ref=CreditRef)

    # ── background driver ────────────────────────────────────────────────
    async def _Drive(self, CandidateId: str) -> None:
        """One slot: submit once and wait up to images.request_timeout_s (90 s) from the submission for its image. A
        slot still running then is "Still finishing" (recovering_since): outside the queue for new requests, the same
        request is asked again until images.recovery_window_s (10 minutes) has passed, and its image is attached when
        it arrives; only then does the slot fail (R-41). A failed slot is never re-requested by the app on its own: the
        customer's "Retry missing" (free) and "Generate another option" (1 credit) are the only second requests, so
        nothing is paid for twice unseen. The request's credit, reserved when it was created, is settled once every
        one of its slots has finished (credits.SettleAction)."""
        Db = self.Ctx.Db
        async with self.Ctx.Semaphore():
            Cand = Db.One("SELECT * FROM candidates WHERE id = ?", (CandidateId,))
            if Cand is None or Cand["status"] in ("ready", "failed"):
                return
            Batch = Db.One("SELECT * FROM batches WHERE id = ?", (Cand["batch_id"],))
            Owner = Db.One("SELECT owner_account_id FROM designs WHERE id = ?", (Batch["design_id"],))["owner_account_id"]
            # A slot that was finishing when the server restarted, or that "Retry missing" found still running, goes
            # straight back to its recovery
            Late = Cand["provider_request_id"] if Cand["recovering_since"] else await self._Generate(Batch, Cand, Owner)
        if Late:
            await self._Recover(Batch, Cand, Owner, Late)
        self._Settle(Batch, CandidateId, Owner)

    async def _Generate(self, Batch: dict, Cand: dict, Owner: str) -> str | None:
        """Submit (unless the slot has its request already) and wait up to the 90-second limit. Returns the request id
        when the limit passed with the request still running; None once the slot is ready or failed."""
        Ctx, Db, S = self.Ctx, self.Ctx.Db, self.Ctx.Settings
        RequestId = Cand["provider_request_id"]
        try:
            if not RequestId:
                Arguments = await self._Arguments(Batch, Cand)
                Est = Credits.CheckSpendCap(Ctx, Batch["endpoint"], None)       # the day's cap, before anything is paid
                try:
                    RequestId = await Ctx.Provider.Submit(Batch["endpoint"], Arguments)
                    Db.Update("candidates", Cand["id"], status="generating",
                              provider_request_id=RequestId, attempts=Cand["attempts"] + 1)
                    Credits.RecordUsage(Ctx, Owner, UsageImage, Cand["id"], Batch["endpoint"])
                finally:
                    Credits.SubmitDone(Ctx, Est)
            Result = await PollUntilDone(Ctx.Provider, Batch["endpoint"], RequestId,
                                         Ctx.Gen.Images.RequestTimeoutS, S.PollIntervalS,
                                         S.MaxTransientPollErrors)
            await self._Deliver(Batch, Cand, Result)
        except JobTimeout:
            return RequestId
        except Exception as E:
            self._Failed(Cand["id"], E)
        return None

    async def _Recover(self, Batch: dict, Cand: dict, Owner: str, RequestId: str) -> None:
        """Past the 90-second limit: the same request is asked every RecoveryPollFactor poll intervals until the
        recovery window, counted from recovering_since, has passed (one last look after a restart) — never a new
        request. Its image is attached when it arrives, and the Admin's journey shows it as recovered late."""
        Ctx, Db, S = self.Ctx, self.Ctx.Db, self.Ctx.Settings
        Since = Cand["recovering_since"]
        if not Since:
            Since = Now()
            Db.Update("candidates", Cand["id"], recovering_since=Since)
        Elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(Since)).total_seconds()
        Interval = S.PollIntervalS * RecoveryPollFactor
        try:
            Result = await PollUntilDone(Ctx.Provider, Batch["endpoint"], RequestId,
                                         max(Ctx.Gen.Images.RecoveryWindowS - Elapsed, Interval / 2), Interval,
                                         S.MaxTransientPollErrors)
            await self._Deliver(Batch, Cand, Result)
        except Exception as E:
            self._Failed(Cand["id"], E)
            return
        if Db.One("SELECT status FROM candidates WHERE id = ?", (Cand["id"],))["status"] == "ready":
            from p3 import ringids as RingIds, sessions as Sessions
            Ref = RingIds.CandidateRefs(Db, [Batch["design_id"]]).get(Cand["id"]) or f"option {Cand['slot'] + 1}"
            Sessions.Record(Ctx, Owner, "option_recovered", Batch["design_id"], options=[Ref])

    async def _Deliver(self, Batch: dict, Cand: dict, Result: dict) -> None:
        S = self.Ctx.Settings
        Images = Result.get("images") or []
        if not Images or not Images[0].get("url"):
            raise assets.AssetError("Provider returned no image")
        Data = await DownloadWithRetry(self.Ctx.Provider, Images[0]["url"], S.MaxTransientPollErrors, S.PollIntervalS)
        Ext = assets.ValidateImage(Data)
        await self._StoreUnlessDuplicate(Batch, Cand, Data, Ext)

    def _Failed(self, CandidateId: str, E: Exception) -> None:
        # The provider's own words stay here (the Admin's session page and the log); the customer gets
        # CustomerError(Code). A slot that ran out of time keeps its request id: "Retry missing" asks fal for it first.
        Message, Code = FailureFor(E)
        Logger.warning("Candidate %s failed (%s): %s", CandidateId, Code, E)
        self.Ctx.Db.Update("candidates", CandidateId, status="failed", error=Message, error_code=Code)

    def _Settle(self, Batch: dict, CandidateId: str, Owner: str) -> None:
        """The slot is over: its request's credit is settled once every slot of the request is (credits.SettleAction)."""
        Cand = self.Ctx.Db.One("SELECT * FROM candidates WHERE id = ?", (CandidateId,))
        Credits.SettleAction(self.Ctx, Owner, Credits.KindOfCandidate(Cand, Batch), Cand.get("credit_ref") or Batch["id"])

    async def _Arguments(self, Batch: dict, Cand: dict) -> dict:
        """Provider arguments from the configuration version recorded on the batch (the prompt was
        rendered from its template when the batch was created)."""
        Version = self.Ctx.Models.Resolve(Batch["config_version"], Batch["endpoint"])
        Runtime = {"seed": Cand["seed"]}
        if Batch["kind"] == "refine":
            Runtime["slot"] = Cand["slot"]            # refinement images A–D each get their own variation directive
        if Batch["reference_asset"]:
            Runtime["image_url"] = await self._ReferenceUrl(Batch)
        ModelId = Version.Model                      # the model of the recorded version (a ring or a charm model)
        Params = {K: V for K, V in Version.Params.items() if K != "prompt"}
        Args = BuildRequest(ModelId, Params, Runtime)
        # The shared prompt rendered when the batch was created, plus this image's directive (if configured)
        Args["prompt"] = WithDirective(Batch["effective_prompt"], SlotDirective(ModelId, Version.Params, Runtime.get("slot")))
        return Args

    async def _ReferenceUrl(self, Batch: dict) -> str:
        """Upload the batch reference once; all slots share the same uploaded image."""
        async with self.Ctx.Lock(f"ref:{Batch['id']}"):
            Row = self.Ctx.Db.One("SELECT reference_upload_url FROM batches WHERE id = ?", (Batch["id"],))
            if Row and Row["reference_upload_url"]:
                return Row["reference_upload_url"]
            try:
                Path_ = assets.Resolve(self.Ctx.Settings.AssetsDir, Batch["reference_asset"])
                Data = Path_.read_bytes()
            except (OSError, assets.AssetError) as E:
                from p3.providers.base import ProviderError
                raise ProviderError("The reference image could not be loaded.", "reference_unavailable") from E
            Url = await self.Ctx.Provider.Upload(Data, assets.ImageContentType(Batch["reference_asset"]))
            self.Ctx.Db.Execute("UPDATE batches SET reference_upload_url = ? WHERE id = ?", (Url, Batch["id"]))
            return Url

    async def _StoreUnlessDuplicate(self, Batch: dict, Cand: dict, Data: bytes, Ext: str) -> bool:
        """Store a ready image. An output identical to another option of the batch is a failed option
        (duplicate_output, retryable): it is never re-requested by the app — that would be another paid image
        nobody asked for — the customer's "Generate another option" is the only second request."""
        Db = self.Ctx.Db
        Digest = assets.Sha256(Data)
        async with self.Ctx.Lock(f"batch:{Batch['id']}"):
            Dup = Db.One("SELECT id FROM candidates WHERE batch_id = ? AND id != ? AND status = 'ready' "
                         "AND content_sha256 = ?", (Batch["id"], Cand["id"], Digest))
            if Dup:
                Db.Update("candidates", Cand["id"], status="failed", error_code="duplicate_output",
                          error="This image came back identical to another option. You can retry it.")
                return True
            RelPath = f"designs/{Batch['design_id']}/candidates/{Cand['id']}.{Ext}"
            assets.WriteAtomic(self.Ctx.Settings.AssetsDir, RelPath, Data)
            Db.Update("candidates", Cand["id"], status="ready", asset_path=RelPath,
                      content_sha256=Digest, error=None, error_code=None)
            return True

    # ── restart reconciliation ───────────────────────────────────────────
    def Reconcile(self) -> dict:
        """Resume polling for submitted requests (a slot that was still finishing goes back to its recovery, for what
        is left of its window); mark never-confirmed ones interrupted.

        A candidate without a provider request id may or may not have been
        submitted before the crash, so it is NOT resubmitted automatically
        (that could double-charge); the user can retry it explicitly.
        """
        Db = self.Ctx.Db
        Resumed = Interrupted = 0
        for C in Db.All("SELECT c.id, c.provider_request_id FROM candidates c "
                        "WHERE c.status IN ('pending', 'generating')"):
            if C["provider_request_id"] and not self.Ctx.Provider.Owns(C["provider_request_id"]):
                if self.Ctx.Provider.Name == "mock":
                    Logger.warning("Candidate %s has a live request; it resumes when live mode is active", C["id"])
                    continue
                Db.Update("candidates", C["id"], status="failed", error_code="interrupted",
                          error="This mock-mode generation cannot be resumed in live mode. You can retry.")
                Interrupted += 1
            elif C["provider_request_id"]:
                self.Ctx.Runner.Spawn(f"cand:{C['id']}", self._Drive(C["id"]))
                Resumed += 1
            else:
                Db.Update("candidates", C["id"], status="failed", error_code="interrupted",
                          error="Generation was interrupted by a server restart. You can retry.")
                Interrupted += 1
        return {"resumed": Resumed, "interrupted": Interrupted}

    # ── reads / ownership ────────────────────────────────────────────────
    def RequireDesign(self, Who: Principal, DesignId: str, Visible: tuple = ("ring", "charm")) -> dict:
        """The customer's own design, or a shared XJet master design they started from the gallery (of a product
        this browser can see: a charm is not found on a ring-only site)."""
        D = self.Ctx.Db.One("SELECT * FROM designs WHERE id = ?", (DesignId,))
        if D is not None and (D.get("product_type") or "ring") not in Visible:
            D = None
        Own = D is not None and D["owner_account_id"] == Who.AccountId and not D.get("removed_at")
        Linked = D is not None and not Own and self.Ctx.Db.One(
            "SELECT 1 AS x FROM gallery_uses WHERE design_id = ? AND owner_account_id = ? AND removed_at IS NULL",
            (DesignId, Who.AccountId))
        if not (Own or Linked):
            raise HttpError(404, "design_not_found", "Design not found.")
        return D

    def RequireReadyCandidate(self, DesignId: str, CandidateId: str) -> dict:
        C = self.Ctx.Db.One("SELECT c.* FROM candidates c JOIN batches b ON b.id = c.batch_id "
                            "WHERE c.id = ? AND b.design_id = ?", (CandidateId, DesignId))
        if C is None:
            raise HttpError(404, "candidate_not_found", "That image does not belong to this design.")
        if C["status"] != "ready":
            raise HttpError(409, "candidate_not_ready", "That image is not ready yet.")
        return C

    def _OwnedBatch(self, Who: Principal, BatchId: str) -> dict:
        B = self.Ctx.Db.One("SELECT b.* FROM batches b JOIN designs d ON d.id = b.design_id "
                            "WHERE b.id = ? AND d.owner_account_id = ?", (BatchId, Who.AccountId))
        if B is None:
            raise HttpError(404, "batch_not_found", "Batch not found.")
        return B

    def _OwnedCandidate(self, Who: Principal, CandidateId: str) -> tuple[dict, dict]:
        C = self.Ctx.Db.One("SELECT * FROM candidates WHERE id = ?", (CandidateId,))
        if C is None:
            raise HttpError(404, "candidate_not_found", "Image not found.")
        return C, self._OwnedBatch(Who, C["batch_id"])

    def _FirstBatch(self, DesignId: str) -> dict:
        B = self.Ctx.Db.One("SELECT id FROM batches WHERE design_id = ? ORDER BY created_at LIMIT 1", (DesignId,))
        return self.GetBatch(B["id"])

    def CandidateJson(self, C: dict, MovieCredits: int = 0) -> dict:
        """The customer's view of a slot. `error` is the app's own sentence for the failure (CustomerError); the
        provider's text and the request id are for the Admin only (sessions.AccountActivity). `finishing`: past the
        90-second limit and still coming; `movie_credits`: what Customize would charge for this image's 360° movie."""
        return {"id": C["id"], "batch_id": C["batch_id"], "slot": C["slot"], "status": C["status"],
                "finishing": C["status"] == "generating" and bool(C.get("recovering_since")),
                "image_url": self.Ctx.AssetUrl(C["asset_path"]) if C["status"] == "ready" else None,
                "error": CustomerError(C["error_code"]) if C["status"] == "failed" else None, "error_code": C["error_code"],
                "retryable": C["status"] == "failed" and C["error_code"] not in NotRetryable, "movie_credits": MovieCredits}

    def _MovieCredits(self, B: dict, Cands: list[dict]) -> dict:
        """The credits opening Customize costs for each ready image (R-07): its 360° movie, unless the product's movies
        are off or the image has one made or being made already, which is reused for free (movies.Ensure)."""
        Ready = [C["id"] for C in Cands if C["status"] == "ready"]
        if not Ready or not Products.MovieAvailable(self.Ctx, Products.Of(self.Ctx.Db, B["design_id"])):
            return {}
        Have = {R["candidate_id"] for R in self.Ctx.Db.All(
            "SELECT DISTINCT m.candidate_id FROM movies m JOIN candidates c ON c.id = m.candidate_id "
            "WHERE c.batch_id = ? AND m.status IN ('queued', 'running', 'ready')", (B["id"],))}
        Cost = Credits.Cost(self.Ctx, Credits.Movie)
        return {Id: Cost for Id in Ready if Id not in Have}

    def GetBatch(self, BatchId: str) -> dict:
        B = self.Ctx.Db.One("SELECT * FROM batches WHERE id = ?", (BatchId,))
        Cands = self.Ctx.Db.All("SELECT * FROM candidates WHERE batch_id = ? ORDER BY slot", (BatchId,))
        Movie = self._MovieCredits(B, Cands)
        return {"id": B["id"], "design_id": B["design_id"], "kind": B["kind"],
                "parent_candidate_id": B["parent_candidate_id"], "user_text": B["user_text"],
                "reference_url": self.Ctx.AssetUrl(B["reference_asset"]),
                "desired_count": B["desired_count"], "config_version": B["config_version"],
                "status": BatchStatus(Cands, B["desired_count"]), "created_at": B["created_at"],
                "candidates": [self.CandidateJson(C, Movie.get(C["id"], 0)) for C in Cands]}

    def RefinementRunning(self, DesignId: str) -> bool:
        """A refinement of this design is still being made (the studio shows its waiting screen): Customize waits,
        or a paid movie would start for an option the customer is about to replace (R-18)."""
        Db = self.Ctx.Db
        for B in Db.All("SELECT id, desired_count FROM batches WHERE design_id = ? AND kind = 'refine' AND id IN "
                        "(SELECT batch_id FROM candidates WHERE status IN ('pending', 'generating'))", (DesignId,)):
            Cands = Db.All("SELECT status, recovering_since FROM candidates WHERE batch_id = ?", (B["id"],))
            if BatchStatus(Cands, B["desired_count"]) in ("queued", "generating"):
                return True
        return False
