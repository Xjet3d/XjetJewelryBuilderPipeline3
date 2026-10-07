"""Credits (p3/credits.py): a credit is a customer-triggered action, not an output file — 1 per design request (its
four images), 1 per refinement request, 1 per explicitly requested additional option, 1 per 360° movie, 0 per 3D
model; selecting, materials, sizes and a reused movie are free. Reserved before the action starts (so parallel
requests cannot overspend), charged once it delivers, released when every image of it fails; never charged for the
Admin's own work; a duplicate image is a failed option, never re-requested on its own; every submission is recorded
with its estimated cost; the daily AI spend cap; the request limits."""

import pytest

from p3 import credits as Credits
from p3.providers import endpoints
from tests.conftest import Harness

AdminKey = "credits-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
QuotaMessage = "You have used all the credits on this account. Contact us to extend your allowance."


async def Status(H) -> dict:
    return (await H.Client.get("/api/token-status")).json()


async def test_a_design_request_is_one_credit_and_parallel_requests_cannot_overspend(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        assert (await Status(H))["tariff"] == {"design": 1, "refinement": 1, "option": 1, "movie": 1, "mesh": 0}
        H.Ctx.Accounts.SetQuota(H.Who.AccountId, MaxGenerations=2)
        H.Provider.LatencyS = 3.0                                           # the images stay in flight for a while
        R1 = await H.Client.post("/api/designs", data={"prompt": "A slim band"})
        assert R1.status_code == 200, R1.text
        S = await Status(H)
        assert (S["used"], S["reserved"], S["remaining"]) == (0, 1, 1)      # one request = one credit, reserved at once
        R2 = await H.Client.post("/api/designs", data={"prompt": "Another band"})
        assert R2.status_code == 200, R2.text
        R3 = await H.Client.post("/api/designs", data={"prompt": "A third band"})
        assert R3.status_code == 402 and R3.json()["error"] == {"code": "quota_exhausted", "message": QuotaMessage}
        H.Provider.LatencyS = 0.0
        await H.Idle()
        S = await Status(H)
        assert (S["used"], S["reserved"], S["remaining"]) == (2, 0, 0)      # two requests delivered: two credits, not eight
        Batch = (await H.Client.get(f"/api/batches/{R1.json()['id']}")).json()
        assert Batch["status"] == "complete" and len([C for C in Batch["candidates"] if C["status"] == "ready"]) == 4
        # A refinement is one more credit: refused at zero, and nothing is submitted
        Subs = len(H.Provider.Submissions)
        R4 = await H.Client.post(f"/api/designs/{Batch['design_id']}/batches",
                                 json={"parent_candidate_id": Batch["candidates"][0]["id"], "instruction": "A thinner band"})
        assert R4.status_code == 402 and len(H.Provider.Submissions) == Subs
        # The Admin's user row shows the credits; the tariff is editable there (and only there)
        Users = (await H.Client.get("/api/admin/users", headers=Admin)).json()["users"]
        Me = next(U for U in Users if U["account_id"] == H.Who.AccountId)
        assert (Me["used"], Me["max"], Me["remaining"]) == (2, 2, 0)
        T = await H.Client.put("/api/admin/credits/tariff", json={"tariff": {"design": 2, "refinement": 1, "option": 1, "movie": 3, "mesh": 0}}, headers=Admin)
        assert T.status_code == 200 and T.json()["tariff"]["movie"] == 3 and (await Status(H))["tariff"]["design"] == 2
        assert (await H.Client.put("/api/admin/credits/tariff", json={"tariff": {"design": -1}}, headers=Admin)).status_code == 400
        assert (await H.Client.put("/api/admin/credits/tariff", json={"tariff": {"design": 1}})).status_code == 403
    finally:
        await H.Close()


async def test_a_request_is_charged_once_whatever_its_images_do_and_released_when_all_fail(tmp_path):
    H = Harness(tmp_path)
    try:
        H.Provider.Script(endpoints.ImageGenerate, "fail", "fail", "duplicate", "duplicate")
        Batch = await H.NewDesign("Band")
        assert sorted(C["status"] for C in Batch["candidates"]) == ["failed", "failed", "failed", "ready"] and Batch["status"] == "partial"
        assert [C["error_code"] for C in Batch["candidates"]].count("duplicate_output") == 1
        assert len(H.Provider.Submissions) == 4                              # no automatic (paid) re-request of anything
        S = await Status(H)
        assert (S["used"], S["reserved"], S["remaining"]) == (1, 0, 99)     # the request delivered an image: one credit
        # "Generate another option" for the three failed slots: one click, one credit, however many slots
        H.Provider.LatencyS = 3.0                                             # hold the retried slots in flight
        R = await H.Client.post(f"/api/batches/{Batch['id']}/retry-failed")
        assert R.status_code == 200, R.text
        S = await Status(H)
        assert (S["used"], S["reserved"]) == (1, 1)
        H.Provider.LatencyS = 0.0
        await H.Idle()
        S = await Status(H)
        assert (S["used"], S["reserved"]) == (2, 0) and len(H.Provider.Submissions) == 7
        # A single option retried on its own is one credit too
        H.Provider.Script(endpoints.ImageGenerate, "fail")
        B2 = await H.NewDesign("Band two")
        Failed = next(C for C in B2["candidates"] if C["status"] == "failed")
        assert (await H.Client.post(f"/api/candidates/{Failed['id']}/retry")).status_code == 200
        await H.Idle()
        assert (await Status(H))["used"] == 4                                 # request + option
        # A request whose every image fails is released, not charged
        H.Provider.Script(endpoints.ImageGenerate, "fail", "fail", "fail", "fail")
        B3 = await H.NewDesign("Band three")
        assert all(C["status"] == "failed" for C in B3["candidates"])
        S = await Status(H)
        assert (S["used"], S["reserved"], S["remaining"]) == (4, 0, 96)
    finally:
        await H.Close()


async def test_a_movie_is_charged_when_finished_released_when_it_fails_and_never_for_the_admin(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Batch = await H.NewDesign("Band")                                    # 1 credit
        Cand = Batch["candidates"][0]["id"]
        H.Provider.Script(endpoints.Movie, "fail")
        Cus = await H.Proceed(Batch["design_id"], Cand)
        await H.Idle()
        S = await Status(H)
        assert (S["used"], S["reserved"]) == (1, 0)                          # the failed movie released its credit
        assert (await H.Client.post(f"/api/candidates/{Cand}/movie")).status_code == 200    # the customer asks again
        await H.Idle()
        assert (await Status(H))["used"] == 2
        await H.Client.post(f"/api/candidates/{Cand}/movie")                 # the finished movie is reused: no charge
        await H.Idle()
        await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"ring_size": 7, "material_id": "vermeil"})   # free
        await H.Client.put(f"/api/designs/{Batch['design_id']}/selection", json={"candidate_id": Batch["candidates"][1]["id"]})
        assert (await Status(H))["used"] == 2
        # The Admin's "Make a new movie": internal usage, recorded with its (mock: $0) cost, never the customer's credits
        R = await H.Client.post(f"/api/admin/candidates/{Cand}/movies", headers=Admin)
        assert R.status_code == 200, R.text
        await H.Idle()
        assert (await Status(H))["used"] == 2
        Rows = H.Ctx.Accounts.Db.All("SELECT internal, cost_usd, cost_source FROM usage_events WHERE kind = 'movie' ORDER BY id")
        assert [R["internal"] for R in Rows] == [0, 0, 1] and all(R["cost_usd"] == 0 and R["cost_source"] for R in Rows)
        # A 3D model is XJet's production cost: recorded as internal, never a credit; a second request reuses the live one
        First = H.Svc.Meshes.Create(Cand, None)
        assert H.Svc.Meshes.Create(Cand, {"face_count": 100000})["id"] == First["id"]
        await H.Idle()
        assert (await Status(H))["used"] == 2
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
        assert (S["used"], S["reserved"]) == (1, 0)                          # the request delivered three images: one credit
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
        assert Credits.Rebuild(H.Ctx) == {H.Who.AccountId: 1}                             # one pending request: one credit
        H.Provider.LatencyS = 0.0
        await H.Idle()
    finally:
        await H.Close()
