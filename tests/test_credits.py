"""Credits (p3/credits.py): 1 per design image, 1 per refinement image, 1 per 360° movie, 0 per 3D model. Reserved
before the work starts (so parallel requests cannot overspend), charged when the result is delivered, released on
failure; never charged for the Admin's own work; a duplicate image is shown as a failed option and never re-requested
on its own; every submission is recorded with its estimated cost; the daily AI spend cap; the request limits."""

import pytest

from p3 import credits as Credits
from p3.providers import endpoints
from tests.conftest import Harness

AdminKey = "credits-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
QuotaMessage = "You have used all the credits on this account. Contact us to extend your allowance."


async def Status(H) -> dict:
    return (await H.Client.get("/api/token-status")).json()


async def test_a_design_reserves_four_credits_and_parallel_requests_cannot_overspend(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        assert (await Status(H))["tariff"] == {"image": 1, "refinement_image": 1, "movie": 1, "mesh": 0}
        H.Ctx.Accounts.SetQuota(H.Who.AccountId, MaxGenerations=5)
        H.Provider.LatencyS = 3.0                                           # the four images stay in flight for a while
        R = await H.Client.post("/api/designs", data={"prompt": "A slim band"})
        assert R.status_code == 200, R.text
        S = await Status(H)
        assert (S["used"], S["reserved"], S["remaining"]) == (0, 4, 1)      # reserved at once, charged when delivered
        R2 = await H.Client.post("/api/designs", data={"prompt": "Another band"})
        assert R2.status_code == 402 and R2.json()["error"] == {"code": "quota_exhausted", "message": QuotaMessage}
        H.Provider.LatencyS = 0.0
        await H.Idle()
        S = await Status(H)
        assert (S["used"], S["reserved"], S["remaining"]) == (4, 0, 1)
        Batch = (await H.Client.get(f"/api/batches/{R.json()['id']}")).json()
        assert Batch["status"] == "complete"
        # A refinement needs four more credits: refused with one left, and nothing is submitted
        Subs = len(H.Provider.Submissions)
        R3 = await H.Client.post(f"/api/designs/{Batch['design_id']}/batches",
                                 json={"parent_candidate_id": Batch["candidates"][0]["id"], "instruction": "A thinner band"})
        assert R3.status_code == 402 and len(H.Provider.Submissions) == Subs
        # The Admin's page shows the ledger per user with the credits, and the tariff can be edited there
        Users = (await H.Client.get("/api/admin/users", headers=Admin)).json()["users"]
        Me = next(U for U in Users if U["account_id"] == H.Who.AccountId)
        assert (Me["used"], Me["max"], Me["remaining"]) == (4, 5, 1)
        T = await H.Client.put("/api/admin/credits/tariff", json={"tariff": {"image": 2, "refinement_image": 1, "movie": 3, "mesh": 0}}, headers=Admin)
        assert T.status_code == 200 and T.json()["tariff"]["movie"] == 3 and (await Status(H))["tariff"]["image"] == 2
        assert (await H.Client.put("/api/admin/credits/tariff", json={"tariff": {"image": -1}}, headers=Admin)).status_code == 400
        assert (await H.Client.put("/api/admin/credits/tariff", json={"tariff": {"image": 1}})).status_code == 403
    finally:
        await H.Close()


async def test_failed_and_duplicate_images_are_not_charged_and_never_re_requested(tmp_path):
    H = Harness(tmp_path)
    try:
        H.Provider.Script(endpoints.ImageGenerate, "fail", "fail", "duplicate", "duplicate")
        Batch = await H.NewDesign("Band")
        ByStatus = sorted(C["status"] for C in Batch["candidates"])
        assert ByStatus == ["failed", "failed", "failed", "ready"] and Batch["status"] == "partial"
        assert [C["error_code"] for C in Batch["candidates"]].count("duplicate_output") == 1
        assert len(H.Provider.Submissions) == 4                              # no automatic (paid) re-request of anything
        S = await Status(H)
        assert (S["used"], S["reserved"], S["remaining"]) == (1, 0, 99)     # only the delivered image is charged
        # "Generate another option" for the three failed ones: a new reservation, charged as they arrive
        R = await H.Client.post(f"/api/batches/{Batch['id']}/retry-failed")
        assert R.status_code == 200, R.text
        await H.Idle()
        S = await Status(H)
        assert (S["used"], S["reserved"]) == (4, 0) and len(H.Provider.Submissions) == 7
    finally:
        await H.Close()


async def test_a_movie_is_charged_when_finished_released_when_it_fails_and_never_for_the_admin(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Batch = await H.NewDesign("Band")                                    # 4 credits
        Cand = Batch["candidates"][0]["id"]
        H.Provider.Script(endpoints.Movie, "fail")
        await H.Proceed(Batch["design_id"], Cand)
        await H.Idle()
        S = await Status(H)
        assert (S["used"], S["reserved"]) == (4, 0)                          # the failed movie released its credit
        assert (await H.Client.post(f"/api/candidates/{Cand}/movie")).status_code == 200    # the customer asks again
        await H.Idle()
        assert (await Status(H))["used"] == 5
        await H.Client.post(f"/api/candidates/{Cand}/movie")                 # the finished movie is reused: no charge
        await H.Idle()
        assert (await Status(H))["used"] == 5
        # The Admin's "Make a new movie": internal usage, recorded with its (mock: $0) cost, never the customer's credits
        R = await H.Client.post(f"/api/admin/candidates/{Cand}/movies", headers=Admin)
        assert R.status_code == 200, R.text
        await H.Idle()
        assert (await Status(H))["used"] == 5
        Rows = H.Ctx.Accounts.Db.All("SELECT internal, cost_usd, cost_source FROM usage_events WHERE kind = 'movie' ORDER BY id")
        assert [R["internal"] for R in Rows] == [0, 0, 1] and all(R["cost_usd"] == 0 and R["cost_source"] for R in Rows)
        # A 3D model is XJet's production cost: recorded as internal, never a credit; a second request reuses the live one
        First = H.Svc.Meshes.Create(Cand, None)
        assert H.Svc.Meshes.Create(Cand, {"face_count": 100000})["id"] == First["id"]
        await H.Idle()
        assert (await Status(H))["used"] == 5
        assert H.Ctx.Accounts.Db.One("SELECT internal FROM usage_events WHERE kind = 'mesh'")["internal"] == 1
        assert (await H.Client.get("/api/token-status")).json()["usage"] == {"image_requests": 4, "movie_requests": 2}
    finally:
        await H.Close()


async def test_the_daily_spend_cap_stops_paid_submissions(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey, DailyAiSpendCapUsd=0.5)
    try:
        H.Provider.Name = "fal"          # the mock is accounted as a live provider (list price $0.15 per image); nothing real is called
        Batch = await H.NewDesign("Band")
        Codes = [C["error_code"] for C in Batch["candidates"]]
        assert Codes.count("spend_cap_reached") == 1 and len(H.Provider.Submissions) == 3      # 3 × $0.15; a 4th would pass $0.50
        Stopped = next(C for C in Batch["candidates"] if C["error_code"] == "spend_cap_reached")
        assert Stopped["error"] == "AI generation is paused for today. Please try again tomorrow." and Stopped["retryable"]
        S = await Status(H)
        assert (S["used"], S["reserved"]) == (3, 0)
        Full = (await H.Client.get("/api/admin/health", headers=Admin)).json()
        assert Full["ai_spend_today_usd"] == pytest.approx(0.45) and Full["daily_ai_spend_cap_usd"] == 0.5
        Cr = (await H.Client.get("/api/admin/credits", headers=Admin)).json()
        assert Cr["spend_today_usd"] == pytest.approx(0.45) and Cr["daily_cap_usd"] == 0.5
    finally:
        await H.Close()


async def test_request_limits_answer_429_with_retry_after(tmp_path):
    H = Harness(tmp_path, RateLimits=True)
    try:
        H.Ctx.RateLimiter.Limits["auth:ip"] = (3, 900)
        for _ in range(3):
            assert (await H.Client.post("/api/register-token", json={"Token": "NOSUCH"})).status_code == 404
        R = await H.Client.post("/api/register-token", json={"Token": "NOSUCH"})
        assert R.status_code == 429 and R.json()["error"]["code"] == "rate_limited" and int(R.headers["retry-after"]) >= 1
        # Another address is not affected by this one's limit
        assert (await H.Client.post("/api/register-token", json={"Token": "NOSUCH"}, headers={"X-Forwarded-For": "203.0.113.9"})).status_code == 404
        H.Ctx.RateLimiter.Limits["start:account"] = (1, 3600)
        assert (await H.Client.post("/api/designs", data={"prompt": "Band"})).status_code == 200
        R = await H.Client.post("/api/designs", data={"prompt": "Band two"}, headers={"X-Forwarded-For": "203.0.113.9"})
        assert R.status_code == 429                                          # the account's limit, whatever the address
        H.Ctx.RateLimiter.Limits["register:email"] = (1, 3600)
        assert (await H.Client.post("/api/register", json={"Name": "Dana", "Email": "dana@example.com"})).status_code == 200
        assert (await H.Client.post("/api/register", json={"Name": "Dana", "Email": "DANA@example.com"})).status_code == 429
    finally:
        await H.Close()


async def test_reservations_are_rebuilt_from_the_job_tables_at_startup(tmp_path):
    H = Harness(tmp_path)
    try:
        H.Ctx.Accounts.Db.Execute("UPDATE accounts SET tokens_reserved = 7")             # a reservation stranded by a crash
        assert (await Status(H))["remaining"] == 93
        assert Credits.Rebuild(H.Ctx) == {}                                               # nothing is in flight
        assert (await Status(H))["remaining"] == 100
        H.Provider.LatencyS = 3.0
        await H.Client.post("/api/designs", data={"prompt": "Band"})
        assert Credits.Rebuild(H.Ctx) == {H.Who.AccountId: 4}                             # the four pending images
        H.Provider.LatencyS = 0.0
        await H.Idle()
    finally:
        await H.Close()
