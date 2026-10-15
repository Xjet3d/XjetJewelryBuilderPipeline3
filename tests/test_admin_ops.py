"""Admin operations: the Needs Attention queue, the dashboard time range, unique design names,
searchable identifiers on sessions."""

import pytest

from p3.providers import endpoints
from tests.conftest import Harness, MakeLive

AdminKey = "ops-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Customer = {"first_name": "Dana", "last_name": "Levi", "email": "dana@atelier-customer.co.il", "phone": "+972 54 123 4567"}
Address = {"recipient": "Dana Levi", "line1": "12 Rothschild Blvd", "city": "Tel Aviv", "postal_code": "6688112", "country": "IL"}


@pytest.fixture
async def HX(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    yield Obj
    await Obj.Close()


async def _Order(H, Prompt="Aurora twist band"):
    Batch = await H.NewDesign(Prompt)
    Cand = Batch["candidates"][0]
    Cus = (await H.Client.post(f"/api/designs/{Batch['design_id']}/customize", json={"candidate_id": Cand["id"]})).json()
    await H.Idle()
    await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"ring_size": 7})
    await H.Client.post("/api/bag", json={"customization_id": Cus["id"]})
    R = await H.Client.post("/api/orders", json={"customer": Customer, "address": Address, "shipping_method": "standard",
                                                 "terms_accepted": True, "client_request_id": "c-" + Prompt})
    assert R.status_code == 200, R.text
    return Batch["design_id"], R.json()


async def test_needs_attention_lists_what_needs_a_human(HX, monkeypatch):
    H = HX
    from p3 import production3d
    Did, O = await _Order(H)
    monkeypatch.setattr(production3d, "MaxRoundness", -1.0)             # the 3D result gets flagged for review
    T = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={}, headers=Admin)).json()
    await H.Idle()
    Did2 = (await H.NewDesign("Signet ring"))["design_id"]
    Q = await H.Client.post("/api/quote-requests", json={"design_id": Did2, "candidate_id": (await H.Design(Did2))["batches"][0]["candidates"][0]["id"],
                                                         "material_id": "gold_14k_yellow", "ring_size": 9, "quantity": 1, "customer": Customer})
    assert Q.status_code == 200
    A = (await H.Client.get("/api/admin/attention", headers=Admin)).json()
    assert A["count"] == 1 and A["items"][0]["kind"] == "quote_request"        # mock sessions/orders are left out
    MakeLive(H)
    A = (await H.Client.get("/api/admin/attention", headers=Admin)).json()
    Kinds = {I["kind"] for I in A["items"]}
    assert {"3d_review", "order_new", "quote_request"} <= Kinds
    Review = next(I for I in A["items"] if I["kind"] == "3d_review")
    assert Review["severity"] == "warn" and Review["href"] == f"#/sessions/{Did}" and Review["ref"] == "R-1001"
    New = next(I for I in A["items"] if I["kind"] == "order_new")
    assert New["href"] == f"#/orders/{O['id']}" and New["ref"] == "ORD-10001" and New["customer"] == "Dana Levi"
    assert Did in A["sessions"]
    assert [I["severity"] for I in A["items"]] == sorted([I["severity"] for I in A["items"]], key=lambda S: {"error": 0, "warn": 1, "info": 2}[S])
    # A failed payment and an old pending payment are attention items; a paid, completed order is not
    H.Ctx.Db.Execute("UPDATE orders SET created_at = '2026-01-01T00:00:00.000+00:00'")
    A = (await H.Client.get("/api/admin/attention", headers=Admin)).json()
    assert "payment_pending" in A["by_kind"]
    await H.Client.post(f"/api/admin/orders/{O['id']}/payment", json={"status": "failed", "note": "card declined"}, headers=Admin)
    A = (await H.Client.get("/api/admin/attention", headers=Admin)).json()
    assert "payment_failed" in A["by_kind"] and "payment_pending" not in A["by_kind"]
    for S in ("payment_confirmed", "three_d_ready", "production", "qc", "shipped", "completed"):
        # production and later need complete 3D results; this test has none, so each step is a noted exception
        await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": S, "note": "test", "force": True}, headers=Admin)
    A = (await H.Client.get("/api/admin/attention", headers=Admin)).json()
    assert not {K for K in A["by_kind"] if K.startswith("payment") or K.startswith("order")}
    Dash = (await H.Client.get("/api/admin/dashboard", headers=Admin)).json()
    assert Dash["needs_attention"]["count"] == A["count"] and Dash["needs_attention"]["items"][0]["label"]


async def test_dashboard_time_range(HX):
    H = HX
    await _Order(H, "Old band")
    MakeLive(H)
    H.Ctx.Db.Execute("UPDATE designs SET created_at = '2026-01-01T00:00:00.000+00:00'")
    H.Ctx.Db.Execute("UPDATE orders SET created_at = '2026-01-01T00:00:00.000+00:00'")
    All = (await H.Client.get("/api/admin/dashboard", headers=Admin)).json()
    assert All["sessions"] == 1 and All["orders"]["orders"] == 1 and All["range"] == {"days": None, "since": None}
    Week = (await H.Client.get("/api/admin/dashboard?days=7", headers=Admin)).json()
    assert Week["sessions"] == 0 and Week["orders"]["orders"] == 0 and Week["range"]["days"] == 7 and Week["range"]["since"]
    assert Week["geometry"] == All["geometry"]                              # model statistics stay all-time
    assert (await H.Client.get("/api/admin/dashboard?days=0", headers=Admin)).json()["range"]["days"] is None


async def test_gallery_master_names_are_distinctive(HX):
    H = HX
    A = await H.NewDesign("Aurora ring")
    B = await H.NewDesign("Aurora ring again")
    for Batch in (A, B):
        await H.Client.put(f"/api/designs/{Batch['design_id']}/selection", json={"candidate_id": Batch["candidates"][0]["id"]})
        assert (await H.Client.post("/api/admin/gallery", json={"design_id": Batch["design_id"]}, headers=Admin)).status_code == 200
    R = await H.Client.patch(f"/api/admin/designs/{B['design_id']}", json={"title": "Aurora Twist"}, headers=Admin)
    assert R.status_code == 200 and R.json()["title"] == "Aurora Twist"
    assert (await H.Client.patch(f"/api/admin/designs/{B['design_id']}", json={"title": "x"}, headers=Admin)).status_code == 400
    assert (await H.Client.patch(f"/api/admin/designs/{B['design_id']}", json={"title": "Aurora Twist"})).status_code == 403
    # The same name as another gallery master is refused unless forced, and flagged in the gallery list
    Dup = await H.Client.patch(f"/api/admin/designs/{A['design_id']}", json={"title": " aurora  twist "}, headers=Admin)
    assert Dup.status_code == 409 and Dup.json()["error"]["code"] == "duplicate_title" and "R-1002" in Dup.json()["error"]["message"]
    assert (await H.Client.patch(f"/api/admin/designs/{A['design_id']}", json={"title": "Aurora Twist", "force": True}, headers=Admin)).status_code == 200
    G = (await H.Client.get("/api/admin/gallery", headers=Admin)).json()["items"]
    assert all(X["duplicate_name"] for X in G) and all(X["title"] == "Aurora Twist" for X in G)
    await H.Client.patch(f"/api/admin/designs/{A['design_id']}", json={"title": "Aurora Halo"}, headers=Admin)
    G = (await H.Client.get("/api/admin/gallery", headers=Admin)).json()["items"]
    assert not any(X["duplicate_name"] for X in G)
    Public = (await H.Client.get("/api/gallery")).json()["items"]
    assert {X["title"] for X in Public} == {"Aurora Halo", "Aurora Twist"}
    S = (await H.Client.get(f"/api/admin/sessions/{A['design_id']}", headers=Admin)).json()
    assert S["session"]["title"] == "Aurora Halo" and any(E["kind"] == "admin_design_renamed" for E in S["timeline"])
    assert (await H.Client.patch("/api/admin/designs/dsg_nope", json={"title": "Nope"}, headers=Admin)).status_code == 404


async def test_journey_shows_the_image_the_customer_gave_at_each_step(HX):
    from p3.providers.mock import _RingImage
    H = HX
    R = await H.Client.post("/api/designs", data={"prompt": "Take inspiration from the attached ring", "rights_confirmed": "true"},
                            files={"reference": ("inspiration.png", _RingImage(7, "ref"), "image/png")})
    assert R.status_code == 200, R.text
    await H.Idle()
    Did = R.json()["design_id"]
    B = (await H.Client.get(f"/api/batches/{R.json()['id']}")).json()
    await H.Client.post(f"/api/designs/{Did}/batches", json={"parent_candidate_id": B["candidates"][1]["id"], "instruction": "thinner"})
    await H.Idle()
    T = (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()["timeline"]
    Started = next(E for E in T if E["kind"] == "started")
    Gen = next(E for E in T if E["kind"] == "generate_requested")
    Ref = next(E for E in T if E["kind"] == "refine_requested")
    for E in (Started, Gen):                                     # the uploaded (or pasted) reference, downloadable under a telling name
        assert E["reference_kind"] == "upload" and E["reference_url"].endswith(".png") and "/references/" in E["reference_url"]
        assert E["download_name"].endswith("_R-1001_reference.png") and E["reference_label"] == "Customer's reference image"
    assert (await H.Client.get(Started["reference_url"])).status_code == 200
    assert Ref["reference_kind"] == "option" and Ref["reference_label"] == "Refined from R-1001-B" and Ref["download_name"].endswith("_R-1001-B.png")
    assert all("reference_url" not in E for E in T if E["kind"] not in ("started", "generate_requested", "refine_requested"))
    # a design typed without an image has no reference fields at all (no empty placeholder)
    Plain = await H.NewDesign("a plain band")
    T2 = (await H.Client.get(f"/api/admin/sessions/{Plain['design_id']}", headers=Admin)).json()["timeline"]
    assert all("reference_url" not in E for E in T2)


async def test_design_names_are_never_shared_and_variations_follow_their_master(HX):
    from p3.providers import endpoints
    H = HX
    # The same prompt twice: another word combination, never two rings called the same and no number
    A = await H.NewDesign("simple delicate twisted band")
    B = await H.NewDesign("simple delicate twisted band")
    Ta, Tb = (await H.Design(A["design_id"]))["title"], (await H.Design(B["design_id"]))["title"]
    assert Ta == "Fil Twist" and Tb == "Fil Spiral"
    assert (await H.Design((await H.NewDesign("another delicate twisted band"))["design_id"]))["title"] == "Fil Helix"
    # A customer's refinement of a gallery master keeps the lineage ("Fil …"), never the master's own name
    await H.Client.put(f"/api/designs/{A['design_id']}/selection", json={"candidate_id": A["candidates"][0]["id"]})
    await H.Client.post("/api/admin/gallery", json={"design_id": A["design_id"]}, headers=Admin)
    Cust = {"X-Access-Token": H.Ctx.Accounts.IssueToken("customer")[0]}
    Item = (await H.Client.get("/api/gallery")).json()["items"][0]
    await H.Client.post(f"/api/gallery/{Item['id']}/start", json={}, headers=Cust)
    Fork = (await H.Client.post(f"/api/designs/{A['design_id']}/batches", json={"parent_candidate_id": A["candidates"][0]["id"],
                                                                                "instruction": "make it a lattice"}, headers=Cust)).json()
    await H.Idle()
    assert (await H.Client.get(f"/api/designs/{Fork['design_id']}", headers=Cust)).json()["title"] == "Fil Lattice"
    Fork2 = (await H.Client.post(f"/api/designs/{A['design_id']}/batches", json={"parent_candidate_id": A["candidates"][1]["id"],
                                                                                 "instruction": "wider"}, headers=Cust)).json()
    await H.Idle()
    assert (await H.Client.get(f"/api/designs/{Fork2['design_id']}", headers=Cust)).json()["title"] == "Fil Wide"
    # The Rename form offers local suggestions (free names, lineage kept for a variation) — no AI call
    N = (await H.Client.get(f"/api/admin/designs/{Fork['design_id']}/names", headers=Admin)).json()
    assert N["lineage"] == "Fil Twist" and N["suggestions"][:2] == ["Fil Mesh", "Fil Filigree"]
    N = (await H.Client.get(f"/api/admin/designs/{A['design_id']}/names", headers=Admin)).json()
    assert N["lineage"] is None and "Fil Rope" in N["suggestions"] and "Fil Twist" not in N["suggestions"]
    # Renaming the master renames the variations that share its family word: "Fil Lattice" → "Aurora Lattice"
    R = (await H.Client.patch(f"/api/admin/designs/{A['design_id']}", json={"title": "Aurora Twist"}, headers=Admin)).json()
    assert {V["title"] for V in R["variations"]} == {"Aurora Lattice", "Aurora Wide"}
    assert (await H.Client.get(f"/api/designs/{Fork['design_id']}", headers=Cust)).json()["title"] == "Aurora Lattice"
    # A variation renamed by hand keeps its own name when the master is renamed again
    await H.Client.patch(f"/api/admin/designs/{Fork2['design_id']}", json={"title": "Petite"}, headers=Admin)
    R = (await H.Client.patch(f"/api/admin/designs/{A['design_id']}", json={"title": "Vesper Twist"}, headers=Admin)).json()
    assert [V["title"] for V in R["variations"]] == ["Vesper Lattice"]
    assert (await H.Client.get(f"/api/designs/{Fork2['design_id']}", headers=Cust)).json()["title"] == "Petite"
    # The master has a 3D model → the variation (new images, a different design) still gets its own first model
    # normally: Generate 3D is open, the source model is only noted (until 2026-10-06 a typed phrase was required)
    T = (await H.Client.post(f"/api/admin/sessions/{A['design_id']}/3d", json={}, headers=Admin)).json()
    await H.Idle()
    S = (await H.Client.get(f"/api/admin/sessions/{Fork['design_id']}", headers=Admin)).json()
    assert S["three_d_defaults"]["existing_model"] is None
    assert S["three_d_defaults"]["source_model"]["design_id"] == A["design_id"] and S["three_d_defaults"]["source_model"]["ring_id"] == "R-1001-A"
    Subs = len(H.Provider.SubmissionsFor(endpoints.Mesh))
    R = await H.Client.post(f"/api/admin/sessions/{Fork['design_id']}/3d", json={}, headers=Admin)
    assert R.status_code == 200 and len(H.Provider.SubmissionsFor(endpoints.Mesh)) == Subs + 1
    await H.Idle()
    S = (await H.Client.get(f"/api/admin/sessions/{Fork['design_id']}", headers=Admin)).json()
    assert S["three_d_defaults"]["existing_model"] and S["three_d_defaults"]["source_model"] is None
    assert not [E for E in S["timeline"] if E["kind"] == "admin_3d_new_model_override"]
    # A second model of the SAME design still needs the typed confirmation
    Other = next(O for O in S["three_d_defaults"]["options"] if O["id"] != S["three_d_defaults"]["existing_model"]["candidate_id"])
    R = await H.Client.post(f"/api/admin/sessions/{Fork['design_id']}/3d", json={"candidate_id": Other["id"]}, headers=Admin)
    assert R.status_code == 409 and R.json()["error"]["code"] == "hi3d_model_exists"


async def test_sessions_carry_every_searchable_identifier(HX):
    H = HX
    Did, O = await _Order(H, "Aurora twist band")
    Rows = (await H.Client.get("/api/admin/sessions?include_mock=true", headers=Admin)).json()["sessions"]
    X = next(R for R in Rows if R["session_id"] == Did)
    assert X["ring_id"] == "R-1001" and X["selected_ring_id"] == "R-1001-A"
    assert X["option_ring_ids"] == ["R-1001-A", "R-1001-B", "R-1001-C", "R-1001-D"]
    assert X["order_refs"] == ["ORD-10001"] and X["ordered"] and X["legacy_copy"] is False


async def test_a_flagged_3d_result_can_be_accepted_for_production_as_measured(HX, monkeypatch):
    """A result that needs production review (here: a bore that is not quite round) is the Admin's decision: accepting
    it makes it complete everywhere — the result, the session list, the order line — while its reasons stay on it,
    with who accepted it, when and why."""
    H = HX
    from p3 import production3d
    Did, O = await _Order(H)
    monkeypatch.setattr(production3d, "MaxRoundness", -1.0)             # every bore is "not round": flagged for review
    T = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={}, headers=Admin)).json()
    await H.Idle()
    S = (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()
    R3 = S["three_d"][0]
    assert R3["production_state"] == "review_required" and R3["review"][0]["code"] == "bore_not_round"
    assert R3["accepted"] is None and not R3["review_stale"] and S["session"]["three_d_state"] == "review_required"
    R = await H.Client.post(f"/api/admin/3d/{R3['id']}/accept", json={"note": "fits the customer's finger"}, headers=Admin)
    assert R.status_code == 200, R.text
    A = R.json()
    assert A["production_state"] == "complete" and A["status"] == "needs_review"              # accepted, not re-measured
    assert A["accepted"]["by"] == "developer-key" and A["accepted"]["note"] == "fits the customer's finger" and A["accepted"]["at"]
    assert A["review"][0]["code"] == "bore_not_round"                                           # the reasons stay
    S = (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()
    assert S["session"]["three_d_state"] == "complete" and not S["session"]["three_d_review"]
    Ev = next(E for E in S["timeline"] if E["kind"] == "admin_3d_accepted")
    assert Ev["data"]["note"] == "fits the customer's finger" and "not round" in Ev["data"]["reasons"]
    L = (await H.Client.get("/api/admin/sessions?include_mock=true", headers=Admin)).json()["sessions"]
    assert next(X for X in L if X["design_id"] == Did)["three_d_state"] == "complete"
    Od = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()
    assert Od["lines"][0]["three_d_state"] == "complete"
    # Accepting twice, or a result that needs no review, is refused
    assert (await H.Client.post(f"/api/admin/3d/{R3['id']}/accept", json={}, headers=Admin)).status_code == 409
    monkeypatch.setattr(production3d, "MaxRoundness", 0.04)
    T2 = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 8}, headers=Admin)).json()
    await H.Idle()
    assert (await H.Client.post(f"/api/admin/3d/{T2['id']}/accept", json={}, headers=Admin)).status_code == 409


async def test_a_flag_from_an_earlier_measurement_is_shown_as_stale_and_cleared_by_re_measuring(HX, monkeypatch):
    """A result flagged by an earlier measurement whose reasons the current one no longer shows (Aurora Curve on proto:
    flagged by the first method, never measured with the current one) says so, with its stored reason; a local
    re-measure (no Hi3D call) clears it."""
    H = HX
    from p3 import production3d
    Did = (await H.NewDesign("Plain band"))["design_id"]
    monkeypatch.setattr(production3d, "MaxRoundness", -1.0)
    await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={}, headers=Admin)
    await H.Idle()
    monkeypatch.setattr(production3d, "MaxRoundness", 0.04)             # today's rule: the measurement shows no reason
    R3 = (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()["three_d"][0]
    assert R3["status"] == "needs_review" and R3["review"] == [] and R3["review_stale"] and R3["production_state"] == "review_required"
    assert "not round" in R3["error"]
    Subs = len(H.Provider.SubmissionsFor(endpoints.Mesh))
    R = await H.Client.post(f"/api/admin/3d/{R3['id']}/retry", headers=Admin)
    assert R.status_code == 200 and R.json()["retried"] == "geometry"
    await H.Idle()
    R3 = (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()["three_d"][0]
    assert R3["status"] == "measured" and R3["production_state"] == "complete" and not R3["review_stale"]
    assert len(H.Provider.SubmissionsFor(endpoints.Mesh)) == Subs                                 # no new Hi3D request


async def test_session_summaries_say_which_designs_are_in_the_gallery(HX):
    H = HX
    A = await H.NewDesign("A slim band for the gallery")
    await H.Client.put(f"/api/designs/{A['design_id']}/selection", json={"candidate_id": A["candidates"][0]["id"]})
    B = await H.NewDesign("A plain band of its own")
    assert (await H.Client.post("/api/admin/gallery", json={"design_id": A["design_id"]}, headers=Admin)).status_code == 200
    L = {X["design_id"]: X for X in (await H.Client.get("/api/admin/sessions?include_mock=true", headers=Admin)).json()["sessions"]}
    assert L[A["design_id"]]["in_gallery"] is True and L[B["design_id"]]["in_gallery"] is False
    Js = (await H.Client.get("/static/admin.js")).text
    assert "galleryMatches(x)" in Js and "this.galleryMatches(x) &&" in Js
    assert 'x-model="sGallery"' in (await H.Client.get("/admin/")).text


async def test_the_admin_can_make_a_new_movie_for_an_image(HX):
    """"Make a new movie": a new paid movie with the movie configuration active now, whatever movies the image has.
    The customer keeps seeing the ready one until the new one is ready, which then becomes the one shown; the
    customer's allowance is not used (the request is still recorded as usage, for the cost view)."""
    H = HX
    Batch = await H.NewDesign("Band")
    Did, Cand = Batch["design_id"], Batch["candidates"][0]
    Cus = (await H.Client.post(f"/api/designs/{Did}/customize", json={"candidate_id": Cand["id"]})).json()
    await H.Idle()
    First = (await H.Client.get(f"/api/customizations/{Cus['id']}")).json()["movie"]
    assert First["status"] == "ready" and not First["by_admin"] and len(H.Provider.SubmissionsFor(endpoints.Movie)) == 1
    Used = lambda: H.Ctx.Accounts.Db.One("SELECT generations_used FROM accounts WHERE account_id = ?", (H.Who.AccountId,))["generations_used"]
    U0 = Used()
    H.Provider.LatencyS = 1.0                                           # the new movie takes a moment
    R = await H.Client.post(f"/api/admin/candidates/{Cand['id']}/movies", headers=Admin)
    assert R.status_code == 200, R.text
    New = R.json()
    assert New["by_admin"] and New["status"] in ("queued", "running") and New["id"] != First["id"]
    # While it is being made the customer still sees the ready one; a second request is refused
    assert (await H.Client.get(f"/api/customizations/{Cus['id']}")).json()["movie"]["id"] == First["id"]
    assert (await H.Client.post(f"/api/admin/candidates/{Cand['id']}/movies", headers=Admin)).status_code == 409
    await H.Idle()
    H.Provider.LatencyS = 0.0
    Shown = (await H.Client.get(f"/api/customizations/{Cus['id']}")).json()["movie"]
    assert Shown["id"] == New["id"] and Shown["status"] == "ready" and Shown["config_version"] == First["config_version"]   # same settings: allowed
    assert len(H.Provider.SubmissionsFor(endpoints.Movie)) == 2 and Used() == U0                   # not charged to the customer
    S = (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()
    assert S["artifacts"]["movie_url"].endswith(New["id"] + ".mp4")
    assert any(E["kind"] == "admin_movie_requested" for E in S["timeline"])
    assert sum(1 for U in H.Ctx.Accounts.AdminActivity(H.Who.AccountId)["usage"] if U["kind"] == "movie") == 2
    # Proceeding again reuses it: still two movies, nothing new
    await H.Client.post(f"/api/designs/{Did}/customize", json={"candidate_id": Cand["id"]})
    await H.Idle()
    assert len(H.Provider.SubmissionsFor(endpoints.Movie)) == 2


async def _OvalResult(H, monkeypatch, Shape):
    """A design whose model's bore is the given shape (mock Hi3D), measured at US 7: its one result."""
    from p3.providers import mock
    monkeypatch.setattr(mock, "_MeshBytes", Shape)
    Did = (await H.NewDesign("A band with a bore that is not round"))["design_id"]
    await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 7}, headers=Admin)
    await H.Idle()
    return Did, (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()["three_d"][0]


async def _Result(H, Did: str, Sid: str) -> dict:
    return next(X for X in (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()["three_d"] if X["id"] == Sid)


def _Torus(Lobes: float = 0.0, Oval: float = 1.0):
    import numpy as np
    import trimesh

    def Shape(Format):
        M = trimesh.creation.torus(major_radius=9.0, minor_radius=1.5, major_sections=192, minor_sections=64)
        if Lobes:
            V = M.vertices.copy()
            V[:, :2] *= (1 + Lobes * np.cos(3 * np.arctan2(V[:, 1], V[:, 0])))[:, None]
            M.vertices = V
        if Oval != 1.0:
            M.apply_scale([Oval, 1.0, 1.0])
        return M.export(file_type=Format)
    return Shape


async def test_fixbore_proposes_a_corrected_stl_and_an_accepted_one_is_a_new_revision(HX, monkeypatch):
    """The manual FixBore (the owner's decision of 2026-10-15): a corrected STL is proposed for review — the model
    scaled along the bore's two axes so the bore is a circle of the size's inner diameter — and the result it is
    proposed for never changes. The Admin sees the result's numbers beside the corrected ones; accepting saves the very
    file reviewed (its hash) as a new revision: a result of its own, complete, its STL stored. Local work, no Hi3D call."""
    import hashlib
    H = HX
    Did, R3 = await _OvalResult(H, monkeypatch, _Torus(Oval=1.15))
    assert R3["production_state"] == "review_required" and [r["code"] for r in R3["review"]] == ["bore_not_round"]
    assert R3["bore_fix"] is None and R3["derived_from"] is None and R3["bore_expected_after"] < 0.01
    Tg, P0 = R3["target_inner_diameter_mm"], R3["geometry"]["production"]
    Subs = len(H.Provider.SubmissionsFor(endpoints.Mesh))
    R = await H.Client.post(f"/api/admin/3d/{R3['id']}/fix-bore", headers=Admin)
    assert R.status_code == 200 and R.json()["bore_fix"]["status"] == "computing" and R.json()["status"] == "needs_review", R.text
    assert (await H.Client.post(f"/api/admin/3d/{R3['id']}/fix-bore", headers=Admin)).json()["error"]["code"] in ("proposal_open", "busy")
    await H.Idle()
    F = await _Result(H, Did, R3["id"])
    # The result is exactly as it was: its status, its reason, its numbers, its uniform STL made on demand
    assert (F["status"], F["production_state"], [r["code"] for r in F["review"]]) == ("needs_review", "review_required", ["bore_not_round"])
    assert F["geometry"]["production"]["volume_mm3"] == pytest.approx(P0["volume_mm3"]) and F["bore_correction"] is None and F["scaled_stl"] == "on_demand"
    X = F["bore_fix"]
    assert (X["status"], X["error"], X["revision_id"]) == ("proposed", None, None)
    B, A = X["before"], X["after"]
    # Before: the gauge is the size, the oval 15 % wider the other way; after: round at the size, less metal
    assert B["gauge_mm"] == pytest.approx(Tg, rel=0.005) and B["bore_max_mm"] == pytest.approx(Tg * 17.25 / 15.0, rel=0.02) and B["roundness"] > 0.04
    assert A["gauge_mm"] == pytest.approx(Tg, rel=0.01) and A["roundness"] < 0.01 and A["bore_max_mm"] < B["bore_max_mm"] and A["warnings"] == []
    assert 0 < A["volume_mm3"] < B["volume_mm3"] and 0 < A["weight_g"] < B["weight_g"]
    assert X["scales"]["scale_major"] < 1 < X["scales"]["scale_minor"]                 # against the uniform scaling: the long way shrinks
    Stl = H.Ctx.Settings.DevDir / "bore_fixes" / f"{X['id']}.stl"
    assert Stl.is_file() and hashlib.sha256(Stl.read_bytes()).hexdigest() == X["file_sha256"] and Stl.stat().st_size == X["file_bytes"]
    assert len(H.Provider.SubmissionsFor(endpoints.Mesh)) == Subs                        # no Hi3D call
    # Accepted: a new revision — complete, its STL the reviewed file, stored; the original unchanged
    R = await H.Client.post(f"/api/admin/bore-fixes/{X['id']}/accept", json={"note": "oval bore of the AI model, made round"}, headers=Admin)
    assert R.status_code == 200, R.text
    Rev, Orig = R.json()["revision"], R.json()["original"]
    assert Rev["id"] != R3["id"] and (Rev["status"], Rev["production_state"], Rev["review"]) == ("measured", "complete", [])
    assert Rev["scaled_stl"] == "stored" and Rev["derived_from"]["session_3d_id"] == R3["id"] and Rev["derived_from"]["file_sha256"] == X["file_sha256"]
    assert (Rev["production_size"], Rev["material_id"], Rev["mesh_id"]) == (R3["production_size"], R3["material_id"], R3["mesh_id"])
    assert Rev["geometry"]["production"]["inner_diameter_mm"] == pytest.approx(A["gauge_mm"]) and Rev["price"]["weight_g"] == pytest.approx(A["weight_g"], rel=0.01)
    assert (Orig["status"], Orig["bore_fix"]["status"], Orig["bore_fix"]["revision_id"]) == ("needs_review", "accepted", Rev["id"])
    assert Orig["geometry"]["production"]["volume_mm3"] == pytest.approx(P0["volume_mm3"]) and Orig["bore_fix"]["decision_note"] == "oval bore of the AI model, made round"
    assert (await H.Client.post(f"/api/admin/bore-fixes/{X['id']}/accept", headers=Admin)).json()["error"]["code"] == "not_proposed"       # once
    # A revision stays as accepted: no re-measure in place, no correction of a correction; the original is not proposed again
    assert not Rev["live"]["can_retry"] and (await H.Client.post(f"/api/admin/3d/{Rev['id']}/retry", headers=Admin)).json()["error"]["code"] == "revision_fixed"
    assert (await H.Client.post(f"/api/admin/3d/{Rev['id']}/fix-bore", headers=Admin)).json()["error"]["code"] == "already_a_revision"
    assert (await H.Client.post(f"/api/admin/3d/{R3['id']}/fix-bore", headers=Admin)).json()["error"]["code"] == "already_corrected"
    # The revision's STL is the stored file itself, byte for byte; the original still exports its uniform scaling
    assert (await H.Client.post(f"/api/admin/3d/{Rev['id']}/export", headers=Admin)).json()["job_id"] == "stored"
    E = (await H.Client.get(f"/api/admin/3d/{Rev['id']}/export/stored", headers=Admin)).json()
    File = await H.Client.get(E["url"].removeprefix(H.Ctx.Settings.BasePath))
    assert File.status_code == 200 and hashlib.sha256(File.content).hexdigest() == X["file_sha256"]
    assert File.content[:80].rstrip() == b"XJet P3 scaled ring, bore made round"
    E2 = (await H.Client.post(f"/api/admin/3d/{R3['id']}/export", headers=Admin)).json()
    await H.Idle()
    E2 = (await H.Client.get(f"/api/admin/3d/{R3['id']}/export/{E2['job_id']}", headers=Admin)).json()
    File2 = await H.Client.get(E2["url"].removeprefix(H.Ctx.Settings.BasePath))
    assert File2.status_code == 200 and File2.content[:80].rstrip() == b"XJet P3 scaled ring"
    Kinds = [Y["kind"] for Y in (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()["timeline"]]
    assert {"admin_3d_bore_fix_proposed", "admin_3d_bore_fix_computed", "admin_3d_bore_fix_accepted"} <= set(Kinds)


async def test_a_rejected_correction_is_deleted_and_the_result_stays_as_it_was(HX, monkeypatch):
    H = HX
    Did, R3 = await _OvalResult(H, monkeypatch, _Torus(Oval=1.15))
    assert (await H.Client.post(f"/api/admin/3d/{R3['id']}/fix-bore", headers=Admin)).status_code == 200
    await H.Idle()
    X = (await _Result(H, Did, R3["id"]))["bore_fix"]
    assert X["status"] == "proposed"
    R = await H.Client.post(f"/api/admin/bore-fixes/{X['id']}/reject", json={"note": "the design stays as generated"}, headers=Admin)
    assert R.status_code == 200 and R.json()["bore_fix"]["status"] == "rejected", R.text
    assert not (H.Ctx.Settings.DevDir / "bore_fixes" / f"{X['id']}.stl").exists()
    F = await _Result(H, Did, R3["id"])
    assert (F["status"], [r["code"] for r in F["review"]], F["bore_fix"]["decision_note"]) == ("needs_review", ["bore_not_round"], "the design stays as generated")
    assert F["geometry"]["production"]["volume_mm3"] == pytest.approx(R3["geometry"]["production"]["volume_mm3"])
    assert len((await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()["three_d"]) == 1      # no revision
    assert (await H.Client.post(f"/api/admin/bore-fixes/{X['id']}/accept", headers=Admin)).json()["error"]["code"] == "not_proposed"
    assert (await H.Client.post(f"/api/admin/3d/{R3['id']}/fix-bore", headers=Admin)).status_code == 200            # proposed again later


async def test_a_fixbore_that_cannot_make_the_bore_round_is_not_proposed_and_changes_nothing(HX, monkeypatch):
    """A bore that is not an ellipse (three-lobed) cannot be made round by scaling: the attempt is recorded as failed
    with the reason, its file deleted, and the result stays exactly as measured."""
    H = HX
    Did, R3 = await _OvalResult(H, monkeypatch, _Torus(Lobes=0.08))
    assert [r["code"] for r in R3["review"]] == ["bore_not_round"]
    assert (await H.Client.post(f"/api/admin/3d/{R3['id']}/fix-bore", headers=Admin)).status_code == 200
    await H.Idle()
    F = await _Result(H, Did, R3["id"])
    assert (F["status"], [r["code"] for r in F["review"]], F["bore_correction"]) == ("needs_review", ["bore_not_round"], None)
    X = F["bore_fix"]
    assert X["status"] == "failed" and "not an ellipse" in X["error"] and X["after"]["roundness"] > 0.04 and X["file_sha256"] is None
    assert not (H.Ctx.Settings.DevDir / "bore_fixes" / f"{X['id']}.stl").exists()
    assert F["geometry"]["production"]["volume_mm3"] == pytest.approx(R3["geometry"]["production"]["volume_mm3"]) and F["scaled_stl"] == "on_demand"
    assert (await H.Client.post(f"/api/admin/bore-fixes/{X['id']}/accept", headers=Admin)).json()["error"]["code"] == "not_proposed"
    Ev = next(Y for Y in (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()["timeline"] if Y["kind"] == "admin_3d_bore_fix_computed")
    assert Ev["data"]["status"] == "failed"


async def test_a_correction_that_is_rounder_but_not_round_is_proposed_with_a_note_and_its_revision_stays_for_review(HX, monkeypatch):
    """An oval AND lobed bore: scaling along its axes removes the oval part but not the lobes — rounder, not round. It
    is proposed with that note; accepted, the revision is flagged for review like any other result (the Admin then
    accepts it as measured, or not). The original is unchanged."""
    from p3.geometry import UsSizeToInnerDiameterMm
    H = HX
    Did, R3 = await _OvalResult(H, monkeypatch, _Torus(Lobes=0.08, Oval=1.15))
    assert [r["code"] for r in R3["review"]] == ["bore_not_round"] and R3["bore_expected_after"] > 0.04
    Before = R3["geometry"]["raw"]["checks"]["roundness"]
    assert (await H.Client.post(f"/api/admin/3d/{R3['id']}/fix-bore", headers=Admin)).status_code == 200
    await H.Idle()
    X = (await _Result(H, Did, R3["id"]))["bore_fix"]
    assert X["status"] == "proposed" and 0.04 < X["after"]["roundness"] < 0.9 * Before and X["after"]["roundness_before"] == pytest.approx(Before)
    assert any("still not round" in W for W in X["after"]["warnings"])
    R = await H.Client.post(f"/api/admin/bore-fixes/{X['id']}/accept", json={}, headers=Admin)
    assert R.status_code == 200, R.text
    Rev = R.json()["revision"]
    assert (Rev["status"], Rev["production_state"]) == ("needs_review", "review_required") and "still not round" in Rev["error"]
    assert [r["code"] for r in Rev["review"]] == ["bore_still_not_round"] and not Rev["live"]["can_retry"]
    assert Rev["geometry"]["production"]["inner_diameter_mm"] == pytest.approx(UsSizeToInnerDiameterMm(7), rel=0.01)
    assert (await H.Client.post(f"/api/admin/3d/{Rev['id']}/accept", json={"note": "lobes of the twist"}, headers=Admin)).json()["production_state"] == "complete"
    assert R.json()["original"]["status"] == "needs_review"


async def test_a_result_measured_by_an_older_method_can_be_measured_again_in_place(HX):
    """A complete result whose numbers are older than the current method (a new measurement version, or another result
    on the same model had it measured again) offers "Re-measure": the retry finalizes it again in place — the same
    result, current numbers. A current result does not offer it."""
    H = HX
    Did = (await H.NewDesign("A plain band for the re-measure"))["design_id"]
    await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 7}, headers=Admin)
    await H.Idle()
    R3 = (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()["three_d"][0]
    assert R3["status"] == "measured" and not R3["live"]["can_retry"]
    assert (await H.Client.post(f"/api/admin/3d/{R3['id']}/retry", headers=Admin)).status_code == 409
    # The result's numbers fall behind the method (as after a new measurement version): re-measure is offered and works
    H.Ctx.Db.Execute("UPDATE geometry_results SET method_version = 'ring-measure-once-v0' WHERE session_3d_id = ?", (R3["id"],))
    S = (await H.Client.get(f"/api/admin/3d/{R3['id']}/status", headers=Admin)).json()
    assert S["can_retry"] and S["retry_is_local"]
    R = await H.Client.post(f"/api/admin/3d/{R3['id']}/retry", headers=Admin)
    assert R.status_code == 200 and R.json()["retried"] == "geometry"
    await H.Idle()
    F = next(X for X in (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()["three_d"] if X["id"] == R3["id"])
    assert F["status"] == "measured" and F["geometry"]["production"]["method_version"] == R3["geometry"]["production"]["method_version"]
    assert F["geometry"]["production"]["inner_diameter_mm"] == pytest.approx(R3["geometry"]["production"]["inner_diameter_mm"])
    assert not F["live"]["can_retry"]
    assert len((await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()["three_d"]) == 1     # in place, no new result


async def test_an_export_made_before_a_re_measure_is_not_served_again(HX):
    """The scaled STL export is reused while it lasts — but only for the same measurement method: after a re-measure
    with a newer method (the scaling may differ) a download gets a fresh export."""
    import json
    H = HX
    Did = (await H.NewDesign("A plain band for the export cache"))["design_id"]
    await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 7}, headers=Admin)
    await H.Idle()
    R3 = (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()["three_d"][0]
    # The model as if measured by an older method, and an export made then
    Db = H.Ctx.Db
    Raw = Db.One("SELECT mesh_id, measurement_json FROM raw_geometry WHERE mesh_id = ?", (R3["mesh_id"],))
    Old = {**json.loads(Raw["measurement_json"]), "method_version": "ring-measure-once-v0"}
    Db.Execute("UPDATE raw_geometry SET measurement_json = ?, method_version = 'ring-measure-once-v0' WHERE mesh_id = ?", (json.dumps(Old), Raw["mesh_id"]))
    Db.Execute("UPDATE geometry_results SET method_version = 'ring-measure-once-v0' WHERE session_3d_id = ?", (R3["id"],))
    A = (await H.Client.post(f"/api/admin/3d/{R3['id']}/export", headers=Admin)).json()
    await H.Idle()
    assert (await H.Client.post(f"/api/admin/3d/{R3['id']}/export", headers=Admin)).json()["job_id"] == A["job_id"]   # reused
    # Re-measured with the current method: the next download is a fresh export, the old one is not served again
    assert (await H.Client.post(f"/api/admin/3d/{R3['id']}/retry", headers=Admin)).status_code == 200
    await H.Idle()
    F = next(X for X in (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()["three_d"] if X["id"] == R3["id"])
    assert F["geometry"]["production"]["method_version"] != "ring-measure-once-v0"
    B = (await H.Client.post(f"/api/admin/3d/{R3['id']}/export", headers=Admin)).json()
    assert B["job_id"] != A["job_id"]
    await H.Idle()
    assert (await H.Client.get(f"/api/admin/3d/{R3['id']}/export/{B['job_id']}", headers=Admin)).json()["status"] == "done"
    assert (await H.Client.post(f"/api/admin/3d/{R3['id']}/export", headers=Admin)).json()["job_id"] == B["job_id"]   # now this one
