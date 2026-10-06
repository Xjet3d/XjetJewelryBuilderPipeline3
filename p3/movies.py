"""Customize movie: one Minimax camera-controls video from the selected image.

  * generated only on Proceed (never for every candidate);
  * one movie per candidate: a live or ready movie of the candidate is reused whatever the movie configuration
    says now, so repeated Proceed clicks, returning to the same candidate and a configuration activated in the
    meantime never duplicate paid work (a new configuration applies to candidates that have no movie yet; the
    movies_one_live partial unique index also stops a race between two Proceed clicks);
  * a failed movie is retried on its own — the image batch is untouched;
  * movie state never gates pricing or the bag.
"""

import logging
import sqlite3

from p3 import assets
from p3.accounts import Principal, UsageMovie
from p3.context import Context, HttpError
from p3.db import NewId, Now
from p3.providers import endpoints
from p3 import products as Products
from p3.modelconfig import BuildRequest, ModelIdFor
from p3.runner import DownloadWithRetry, FailureFor, PollUntilDone

Logger = logging.getLogger("p3.movies")


class MovieService:
    def __init__(self, Ctx: Context):
        self.Ctx = Ctx

    def _Model(self, CandidateId: str) -> str:
        """The movie model of the candidate's product: rings keep minimax-camera, charms have their own."""
        return ModelIdFor(endpoints.Movie, Products.OfCandidate(self.Ctx.Db, CandidateId))

    def _Chosen(self, CandidateId: str) -> str | None:
        """The movie the Admin chose to show for the image (candidates.movie_id), when an image has several."""
        Row = self.Ctx.Db.One("SELECT movie_id FROM candidates WHERE id = ?", (CandidateId,))
        return Row["movie_id"] if Row else None

    def Latest(self, CandidateId: str) -> dict | None:
        """The candidate's movie: the one the Admin chose for it if that one is ready, else its newest ready one
        (whatever configuration made it — a ready movie stays shown while a new one is being made), else the one
        being made, else its latest failed one."""
        Rows = self.Ctx.Db.All("SELECT * FROM movies WHERE candidate_id = ? ORDER BY created_at DESC", (CandidateId,))
        Chosen = self._Chosen(CandidateId)
        Ready = [M for M in Rows if M["status"] == "ready"]
        Live = [M for M in Rows if M["status"] in ("queued", "running")]
        return (next((M for M in Ready if M["id"] == Chosen), None) or (Ready[0] if Ready else None)
                or (Live[0] if Live else None) or (Rows[0] if Rows else None))

    def _Live(self, CandidateId: str) -> dict | None:
        """The candidate's live or ready movie to reuse — the Admin's choice for it, else the newest — from any
        configuration version: it is never remade."""
        Rows = self.Ctx.Db.All("SELECT * FROM movies WHERE candidate_id = ? AND status IN ('queued','running','ready') "
                               "ORDER BY created_at DESC", (CandidateId,))
        Chosen = self._Chosen(CandidateId)
        return next((M for M in Rows if M["id"] == Chosen), Rows[0] if Rows else None)

    def Choose(self, CandidateId: str, MovieId: str) -> dict:
        """Admin: make one of the image's finished movies the one shown (Customize, the Admin session page)."""
        M = self.Ctx.Db.One("SELECT * FROM movies WHERE id = ? AND candidate_id = ?", (MovieId, CandidateId))
        if M is None:
            raise HttpError(404, "movie_not_found", "That movie does not belong to this image.")
        if M["status"] != "ready":
            raise HttpError(409, "movie_not_ready", "Only a finished movie can be the one shown.")
        self.Ctx.Db.Update("candidates", CandidateId, movie_id=MovieId)
        return self.ToJson(M)

    def Remake(self, CandidateId: str, By: str) -> dict:
        """Admin ("Make a new movie"): a new movie for a finished image with the movie configuration active now,
        whatever movies it already has. It becomes the one shown once it is ready (the current one stays until
        then, Latest); it is outside the one-live-movie rule and does not use the customer's allowance."""
        Db = self.Ctx.Db
        Cand = Db.One("SELECT * FROM candidates WHERE id = ? AND status = 'ready'", (CandidateId,))
        if Cand is None:
            raise HttpError(409, "no_ready_image", "Only a finished image can have a movie.")
        if Db.One("SELECT 1 AS x FROM movies WHERE candidate_id = ? AND status IN ('queued', 'running')", (CandidateId,)):
            raise HttpError(409, "movie_in_progress", "A movie is already being made for this image.")
        Version = self.Ctx.Models.Active(self._Model(CandidateId)).Id
        MovieId, T = NewId("mov"), Now()
        Db.Execute("INSERT INTO movies (id, candidate_id, config_version, endpoint, status, created_at, updated_at, made_by_admin) "
                   "VALUES (?,?,?,?,?,?,?,?)", (MovieId, CandidateId, Version, endpoints.Movie, "queued", T, T, By))
        Db.Update("candidates", CandidateId, movie_id=MovieId)
        self.Ctx.Runner.Spawn(f"movie:{MovieId}", self._Drive(MovieId))
        return self.ToJson(Db.One("SELECT * FROM movies WHERE id = ?", (MovieId,)))

    def _Payer(self, Movie: dict, Cand: dict) -> str:
        """Who is charged: the customer who requested the movie (shared gallery designs), else the owner."""
        if Movie.get("requested_by"):
            return Movie["requested_by"]
        return self.Ctx.Db.One("SELECT owner_account_id FROM designs WHERE id = ?", (Cand["design_id"],))["owner_account_id"]

    def Ensure(self, Who: Principal, CandidateId: str) -> dict:
        """Start the movie for a ready candidate, or return the live/ready one."""
        Db = self.Ctx.Db
        Version = self.Ctx.Models.Active(self._Model(CandidateId)).Id
        if self._Live(CandidateId):         # the candidate already has a movie (any configuration) → reuse, no new charge
            return self.ToJson(self.Latest(CandidateId))
        self.Ctx.Accounts.AuthorizeSpend(Who, UsageMovie, 1)
        MovieId = NewId("mov")
        T = Now()
        try:
            Db.Execute("INSERT INTO movies (id, candidate_id, config_version, endpoint, status, created_at, updated_at, "
                       "requested_by) VALUES (?,?,?,?,?,?,?,?)",
                       (MovieId, CandidateId, Version, endpoints.Movie, "queued", T, T, Who.AccountId))
        except sqlite3.IntegrityError:
            # Lost a race with a concurrent Proceed: reuse the winner.
            return self.ToJson(self._Live(CandidateId))
        self.Ctx.Runner.Spawn(f"movie:{MovieId}", self._Drive(MovieId))
        return self.ToJson(Db.One("SELECT * FROM movies WHERE id = ?", (MovieId,)))

    async def _Drive(self, MovieId: str) -> None:
        Ctx = self.Ctx
        Db = Ctx.Db
        S = Ctx.Settings
        Movie = Db.One("SELECT * FROM movies WHERE id = ?", (MovieId,))
        if Movie is None or Movie["status"] not in ("queued", "running"):
            return
        Cand = Db.One("SELECT c.*, b.design_id FROM candidates c JOIN batches b ON b.id = c.batch_id "
                      "WHERE c.id = ?", (Movie["candidate_id"],))
        try:
            RequestId = Movie["provider_request_id"]
            if not RequestId:
                ImagePath = assets.Resolve(S.AssetsDir, Cand["asset_path"])
                if not ImagePath.is_file():
                    from p3.providers.base import ProviderError
                    raise ProviderError("The selected image could not be loaded.", "reference_unavailable")
                ImageUrl = await Ctx.Provider.Upload(ImagePath.read_bytes(), assets.ImageContentType(Cand["asset_path"]))
                Version = Ctx.Models.Resolve(Movie["config_version"], Movie["endpoint"])
                Arguments = BuildRequest(Version.Model, Version.Params, {"image_url": ImageUrl})
                RequestId = await Ctx.Provider.Submit(Movie["endpoint"], Arguments)
                Db.Update("movies", MovieId, status="running", provider_request_id=RequestId)
                Ctx.Accounts.RecordUsage(self._Payer(Movie, Cand), UsageMovie, 1, MovieId,
                                         Provider=Ctx.Provider.Name, Endpoint=Movie["endpoint"])
            Result = await PollUntilDone(Ctx.Provider, Movie["endpoint"], RequestId,
                                         Ctx.Gen.Movie.RequestTimeoutS, S.PollIntervalS, S.MaxTransientPollErrors)
            Url = (Result.get("video") or {}).get("url")
            if not Url:
                raise assets.AssetError("Provider returned no video")
            Data = await DownloadWithRetry(Ctx.Provider, Url, S.MaxTransientPollErrors, S.PollIntervalS)
            assets.ValidateMp4(Data)
            RelPath = f"designs/{Cand['design_id']}/movies/{MovieId}.mp4"
            assets.WriteAtomic(S.AssetsDir, RelPath, Data)
            Db.Update("movies", MovieId, status="ready", asset_path=RelPath, error=None, error_code=None)
            # P2 rule: a finished 360° movie uses one generation (charged once, on success only) — the customer's,
            # never for a movie the Admin asked for.
            if not Movie["made_by_admin"]:
                Ctx.Accounts.CommitCharge(self._Payer(Movie, Cand), UsageMovie, MovieId)
        except Exception as E:
            Message, Code = FailureFor(E)
            Logger.warning("Movie %s failed (%s): %s", MovieId, Code, E)
            Db.Update("movies", MovieId, status="failed", error=Message, error_code=Code)

    def Reconcile(self) -> dict:
        Db = self.Ctx.Db
        Resumed = Interrupted = 0
        for M in Db.All("SELECT id, provider_request_id FROM movies WHERE status IN ('queued','running')"):
            if M["provider_request_id"] and not self.Ctx.Provider.Owns(M["provider_request_id"]):
                if self.Ctx.Provider.Name == "mock":
                    continue      # live request: resumes when live mode is active
                Db.Update("movies", M["id"], status="interrupted", error_code="interrupted",
                          error="This mock-mode movie cannot be resumed in live mode. You can retry.")
                Interrupted += 1
            elif M["provider_request_id"]:
                self.Ctx.Runner.Spawn(f"movie:{M['id']}", self._Drive(M["id"]))
                Resumed += 1
            else:
                Db.Update("movies", M["id"], status="interrupted", error_code="interrupted",
                          error="Movie generation was interrupted by a server restart. You can retry.")
                Interrupted += 1
        return {"resumed": Resumed, "interrupted": Interrupted}

    def ToJson(self, M: dict | None) -> dict | None:
        if M is None:
            return None
        return {"id": M["id"], "candidate_id": M["candidate_id"], "status": M["status"],
                "config_version": M["config_version"], "endpoint": M["endpoint"],
                "movie_url": self.Ctx.AssetUrl(M["asset_path"]) if M["status"] == "ready" else None,
                "error": M["error"], "error_code": M["error_code"], "by_admin": bool(M["made_by_admin"]),
                "retryable": M["status"] in ("failed", "interrupted")}
