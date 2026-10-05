"""Customize movie: one Minimax camera-controls video from the selected image.

  * generated only on Proceed (never for every candidate);
  * keyed by (candidate, movie config_version): a live or ready movie is reused,
    so repeated Proceed clicks and returning to the same candidate never
    duplicate paid work (enforced by the movies_one_live partial unique index);
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

    def Latest(self, CandidateId: str) -> dict | None:
        """Most relevant movie for the candidate under the current config (live first, then latest)."""
        Same = self.Ctx.Models.Equivalent(self._Model(CandidateId))    # versions with the active settings
        Q = ",".join("?" * len(Same))
        Row = self.Ctx.Db.One(
            f"SELECT * FROM movies WHERE candidate_id = ? AND config_version IN ({Q}) "
            "ORDER BY CASE WHEN status IN ('queued','running','ready') THEN 0 ELSE 1 END, created_at DESC LIMIT 1",
            (CandidateId, *Same))
        return Row

    def _Live(self, CandidateId: str) -> dict | None:
        Same = self.Ctx.Models.Equivalent(self._Model(CandidateId))
        Q = ",".join("?" * len(Same))
        return self.Ctx.Db.One(f"SELECT * FROM movies WHERE candidate_id = ? AND config_version IN ({Q}) "
                               "AND status IN ('queued','running','ready') ORDER BY created_at DESC LIMIT 1",
                               (CandidateId, *Same))

    def _Payer(self, Movie: dict, Cand: dict) -> str:
        """Who is charged: the customer who requested the movie (shared gallery designs), else the owner."""
        if Movie.get("requested_by"):
            return Movie["requested_by"]
        return self.Ctx.Db.One("SELECT owner_account_id FROM designs WHERE id = ?", (Cand["design_id"],))["owner_account_id"]

    def Ensure(self, Who: Principal, CandidateId: str) -> dict:
        """Start the movie for a ready candidate, or return the live/ready one."""
        Db = self.Ctx.Db
        Version = self.Ctx.Models.Active(self._Model(CandidateId)).Id
        Live = self._Live(CandidateId)      # same settings (any equivalent version) → reuse, no new charge
        if Live:
            return self.ToJson(Live)
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
            # P2 rule: a finished 360° movie uses one generation (charged once, on success only).
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
                "error": M["error"], "error_code": M["error_code"],
                "retryable": M["status"] in ("failed", "interrupted")}
