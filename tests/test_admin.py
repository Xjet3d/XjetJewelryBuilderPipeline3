"""P3 Admin: protection, B2C2 token management (Name/Email required, one account per email,
edit / deactivate / soft remove), and the per-user detail built only from recorded data."""

import re

import pytest

from p3.providers import endpoints
from tests.conftest import Harness, MakeLive

AdminKey = "admin-test-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}


@pytest.fixture
async def HA(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey, BasePath="/JewelryB2C3")
    yield Obj
    await Obj.Close()


async def _Create(H, Name="Dana Levi", Email="dana@example.com", Max=10):
    return await H.Client.post("/api/admin/users", json={"Name": Name, "Email": Email, "MaxGenerations": Max}, headers=Admin)


async def test_admin_requires_the_admin_key_and_is_separate_from_the_customer_site(HA):
    H = HA
    for Path in ("/api/admin/users", "/api/admin/session", f"/api/admin/users/{H.Who.AccountId}"):
        assert (await H.Client.get(Path)).status_code == 403                       # customer token is not enough
        assert (await H.Client.get(Path, headers={"Authorization": "Bearer wrong"})).status_code == 403
    assert (await H.Client.post("/api/admin/users", json={"Name": "x", "Email": "x@y.zz"})).status_code == 403
    Page = await H.Client.get("/admin/")
    assert Page.status_code == 200 and "XJet Admin" in Page.text and Page.headers["x-robots-tag"] == "noindex, nofollow"
    assert '"/JewelryB2C3/static/admin.js?v=' in Page.text
    Site = (await H.Client.get("/")).text
    assert "/admin" not in Site and "api/admin" not in (await H.Client.get("/static/app.js")).text


async def test_admin_sign_in_is_remembered_by_a_server_side_session_cookie(HA):
    """The key is entered once per browser; a server-side session (HttpOnly cookie) signs the admin in
    afterwards, survives restarts, and can be revoked on the server. The key is never stored client-side."""
    H = HA
    Js = (await H.Client.get("/static/admin.js")).text
    assert "p3_admin_key" not in Js and "KEY_STORE" not in Js                    # the key is never stored client-side
    assert (await H.Client.post("/api/admin/login", json={"key": "wrong"})).status_code == 403
    assert (await H.Client.get("/api/admin/session")).status_code == 403                    # nothing remembered yet
    R = await H.Client.post("/api/admin/login", json={"key": AdminKey})
    assert R.status_code == 200 and R.json()["remembered"] and R.json()["mode"] == "mock"
    C = R.headers["set-cookie"]
    assert "p3_admin_session=" in C and "HttpOnly" in C and "Path=/JewelryB2C3/" in C and "Max-Age=" in C and "SameSite=lax" in C.lower().replace("samesite=lax", "SameSite=lax")
    assert AdminKey not in C
    # The cookie alone (no Authorization header) authenticates every admin route — the client keeps the jar
    assert (await H.Client.get("/api/admin/session")).status_code == 200
    assert (await H.Client.get("/api/admin/users")).status_code == 200
    assert (await H.Client.get("/api/admin/session", headers={"Cookie": "p3_admin_session=forged"})).status_code == 403
    # Revoked on the server (e.g. a lost laptop) → that browser must sign in again
    H.Ctx.Db.Execute("UPDATE admin_sessions SET revoked_at = '2026-01-01T00:00:00+00:00'")
    assert (await H.Client.get("/api/admin/session")).status_code == 403
    assert (await H.Client.post("/api/admin/login", json={"key": AdminKey})).status_code == 200
    assert (await H.Client.get("/api/admin/session")).status_code == 200
    Rows = H.Ctx.Db.All("SELECT * FROM admin_sessions")
    assert len(Rows) == 2 and all(len(R["session_hash"]) == 64 for R in Rows) and Rows[1]["expires_at"] > Rows[1]["created_at"]
    # Sign out: the session is revoked and the cookie cleared
    assert (await H.Client.post("/api/admin/logout")).json()["ok"]
    assert (await H.Client.get("/api/admin/session")).status_code == 403
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM admin_sessions WHERE revoked_at IS NULL")["n"] == 0
    # "Sign out everywhere" revokes every remembered browser at once
    await H.Client.post("/api/admin/login", json={"key": AdminKey})
    Other = await H.Client.post("/api/admin/login", json={"key": AdminKey}, headers={"Cookie": ""})
    assert Other.status_code == 200 and H.Ctx.Db.One("SELECT COUNT(*) AS n FROM admin_sessions WHERE revoked_at IS NULL")["n"] == 2
    assert (await H.Client.post("/api/admin/logout-everywhere")).json()["revoked"] == 2
    assert (await H.Client.get("/api/admin/session")).status_code == 403


async def test_the_3d_viewer_never_shows_another_sessions_model(HA):
    """The session page's 3D viewer shares one WebGL canvas across sessions, and a canvas keeps its last frame:
    clearing the viewer takes the canvas off the page (a session without a 3D model showed the previous session's
    model, frozen), and a model is started only for the session that is still open."""
    Js = (await HA.Client.get("/static/admin.js")).text
    Clear = Js[Js.index("    clear3d() {"):Js.index("    glRenderer() {")]
    assert "if (GL.renderer?.domElement.parentNode) GL.renderer.domElement.remove();" in Clear
    Show = Js[Js.index("    async show3d(t) {"):Js.index("    parsePreview(buf) {")]
    assert Show.index("this.clear3d();") < Show.index("el.appendChild(renderer.domElement)")      # back once drawn
    assert "if (this.viewer3d.id !== t.id) return;" in Show                       # another session or model meanwhile
    assert "setTimeout(() => { if (this.sd?.session.session_id === sid) this.show3d(latest); }, 50);" in Js


async def test_admin_disabled_without_a_key(tmp_path):
    H = Harness(tmp_path)
    try:
        assert (await H.Client.get("/api/admin/users", headers=Admin)).status_code == 503
    finally:
        await H.Close()


async def test_create_requires_name_and_email_and_rejects_duplicates(HA):
    H = HA
    for Name, Email, Message in (("", "a@b.co", "Name is required."), ("Dana", "", "Please enter a valid email address."),
                                 ("Dana", "not-an-email", "Please enter a valid email address.")):
        R = await _Create(H, Name, Email)
        assert R.status_code == 400 and R.json()["error"]["message"] == Message
    assert (await _Create(H, Max=0)).status_code == 400
    R = await _Create(H)
    assert R.status_code == 200
    U = R.json()
    assert re.fullmatch(r"[A-Z]{6}", U["token"]) and U["token"] == U["token"].upper()
    assert (U["name"], U["email"], U["used"], U["max"], U["status"], U["source"]) == \
           ("Dana Levi", "dana@example.com", 0, 10, "unused", "admin")
    Dup = await _Create(H, "Other", "DANA@example.com")
    assert Dup.status_code == 409 and Dup.json()["error"]["code"] == "duplicate_email"
    assert U["account_id"] in Dup.json()["error"]["message"]
    # The new token works for the customer straight away (admin tokens are active, as in B2C2).
    R = await H.Client.post("/api/register-token", json={"Token": U["token"].lower()}, headers={"X-Access-Token": ""})
    assert R.json()["ok"] and R.json()["name"] == "Dana Levi"


async def test_table_edit_deactivate_activate_and_soft_remove(HA):
    H = HA
    U = (await _Create(H)).json()
    Uid, Token = U["account_id"], U["token"]
    Users = (await H.Client.get("/api/admin/users", headers=Admin)).json()["users"]
    Row = next(X for X in Users if X["account_id"] == Uid)
    assert Row["token"] == Token and Row["token_complete"]                      # full token shown, as B2C2
    # Edit: name, email, max, reset usage — Name/Email still required, duplicates still rejected.
    H.Ctx.Accounts.Db.Execute("UPDATE accounts SET generations_used = 3 WHERE account_id = ?", (Uid,))
    R = await H.Client.patch(f"/api/admin/users/{Uid}", json={"Name": "Dana L.", "Email": "dana.l@example.com",
                                                               "MaxGenerations": 20, "ResetUsage": True}, headers=Admin)
    assert (R.json()["name"], R.json()["email"], R.json()["max"], R.json()["used"]) == ("Dana L.", "dana.l@example.com", 20, 0)
    assert (await H.Client.patch(f"/api/admin/users/{Uid}", json={"Name": "", "Email": "x@y.co", "MaxGenerations": 5},
                                 headers=Admin)).status_code == 400
    # Deactivate → the customer is refused with B2C2's message; Activate → works again.
    assert (await H.Client.post(f"/api/admin/users/{Uid}/deactivate", headers=Admin)).json()["status"] == "inactive"
    R = await H.Client.post("/api/register-token", json={"Token": Token})
    assert R.status_code == 403 and R.json()["error"]["message"] == "This token has been deactivated. Contact XJet."
    assert (await H.Client.post(f"/api/admin/users/{Uid}/activate", headers=Admin)).json()["status"] == "unused"
    assert (await H.Client.post("/api/register-token", json={"Token": Token})).json()["ok"]
    # Soft remove: token revoked, hidden from the list, kept with history; restorable.
    assert (await H.Client.post(f"/api/admin/users/{Uid}/remove", headers=Admin)).json()["status"] == "removed"
    assert (await H.Client.post("/api/register-token", json={"Token": Token})).status_code == 403
    Ids = [X["account_id"] for X in (await H.Client.get("/api/admin/users", headers=Admin)).json()["users"]]
    assert Uid not in Ids
    All = (await H.Client.get("/api/admin/users?include_removed=true", headers=Admin)).json()["users"]
    assert next(X for X in All if X["account_id"] == Uid)["status"] == "removed"
    assert (await _Create(H, "New Dana", "dana.l@example.com")).status_code == 200      # email freed by removal
    assert (await H.Client.post(f"/api/admin/users/{Uid}/restore", headers=Admin)).status_code == 409
    assert (await H.Client.post("/api/admin/users/p3local:acct_nope/remove", headers=Admin)).status_code == 404


async def test_user_detail_counts_only_recorded_activity(HA):
    H = HA
    Batch = await H.NewDesign("Signet ring with a hexagon face")
    DesignId, Selected = Batch["design_id"], Batch["candidates"][1]
    await H.Client.put(f"/api/designs/{DesignId}/selection", json={"candidate_id": Selected["id"]})
    R = await H.Client.post(f"/api/designs/{DesignId}/batches",
                            json={"parent_candidate_id": Selected["id"], "instruction": "Add milgrain edges"})
    assert R.status_code == 200
    await H.Idle()
    await H.Proceed(DesignId, Selected["id"])                                  # 360° movie (charged on success)
    await H.Idle()
    H.Provider.Script(endpoints.Movie, "fail")
    Other = Batch["candidates"][2]
    await H.Proceed(DesignId, Other["id"])                                     # a failed movie (not charged)
    await H.Idle()
    await H.Client.post("/api/register-token", json={"Token": H.Token})        # a sign-in
    Mock = (await H.Client.get(f"/api/admin/users/{H.Who.AccountId}", headers=Admin)).json()
    assert Mock["totals"]["designs"] == 0 and Mock["usage_ledger"] == [] and Mock["sessions"] == []   # mock is not counted
    MakeLive(H)

    D = (await H.Client.get(f"/api/admin/users/{H.Who.AccountId}", headers=Admin)).json()
    T = D["totals"]
    assert (T["designs"], T["design_batches"], T["refinements"]) == (1, 1, 1)
    assert T["images"]["total"] == 4 and T["images"]["by_status"] == {"ready": 4}
    assert T["refinement_images"]["total"] == 4
    assert T["movies"]["total"] == 2 and T["movies"]["by_status"] == {"ready": 1, "failed": 1}
    assert T["meshes"]["total"] == 0 and T["bag_lines"] == 0
    assert (T["jobs_succeeded"], T["jobs_failed"]) == (9, 1)
    assert (T["generations_used"], T["generations_max"], T["sign_ins"]) == (1, 10, 1)
    assert T["images"]["by_provider"] == {"fal": 4}
    assert D["user"]["last_sign_in_at"] and D["user"]["last_activity_at"]

    # Provider ledger: every submission carries provider + endpoint; cost stays empty (not configured).
    Requests = {}
    for Row in D["usage_ledger"]:
        Requests[(Row["kind"], Row["provider"])] = Requests.get((Row["kind"], Row["provider"]), 0) + Row["requests"]
    assert Requests == {("image", "fal"): 8, ("movie", "fal"): 2}
    assert len([Row for Row in D["usage_ledger"] if Row["kind"] == "image"]) == 2     # design + refine endpoints
    assert all(R["endpoint"] != "unknown" and R["cost_usd"] is None for R in D["usage_ledger"])
    assert D["cost_reporting"] == "not_configured"

    G = D["designs"][0]
    Chosen = [C for B in G["batches"] for C in B["candidates"] if C["selected"]]
    assert G["title"] and len(Chosen) == 1 and G["thumbnail_url"] == Chosen[0]["image_url"]   # the current selection
    assert [B["kind"] for B in G["batches"]] == ["initial", "refine"] and len(G["batches"][0]["candidates"]) == 4
    Kinds = [E["kind"] for E in D["timeline"]]
    assert {"design_created", "refinement", "movie", "sign_in"} <= set(Kinds)
    assert D["daily"] and D["daily"][-1]["images"] == 4 and D["daily"][-1]["refinement_images"] == 4


async def test_link_sign_in_is_recorded(HA):
    H = HA
    R = await H.Client.post("/api/register-token", json={"Token": H.Token, "Via": "link"}, headers={"X-Access-Token": ""})
    assert R.json()["ok"]
    Ev = H.Ctx.Accounts.AdminActivity(H.Who.AccountId)["events"]
    assert [(E["kind"], E["detail"]) for E in Ev] == [("sign_in", "link")]


def test_earlier_usage_is_backfilled_with_provider_and_endpoint(tmp_path):
    from p3.usage import BackfillUsageAnnotations
    import asyncio
    H = Harness(tmp_path)
    try:
        asyncio.run(H.NewDesign("Plain band"))
        H.Ctx.Accounts.Db.Execute("UPDATE usage_events SET provider = NULL, endpoint = NULL, mode = NULL")
        assert BackfillUsageAnnotations(H.Ctx) == 4
        Rows = H.Ctx.Accounts.AdminActivity(H.Who.AccountId)["usage"]
        assert {(R["provider"], R["mode"]) for R in Rows} == {("mock", "mock")} and all(R["endpoint"] for R in Rows)
        assert BackfillUsageAnnotations(H.Ctx) == 0
    finally:
        asyncio.run(H.Close())
