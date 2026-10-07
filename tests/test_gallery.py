"""Inspiration Gallery: shared XJet master designs. "Make it yours" links a customer to the design —
it is never copied; their own selection, choices, bag and refinements are theirs."""

import pytest

from p3.providers import endpoints
from tests.conftest import Harness

AdminKey = "gallery-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}


@pytest.fixture
async def HG(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    yield Obj
    await Obj.Close()


async def _Curated(H):
    """An XJet design (the harness user) with a chosen image and its 360° movie, added to the gallery."""
    Batch = await H.NewDesign("Twisted bands joined by a small knot")
    Did, Cand = Batch["design_id"], Batch["candidates"][2]
    await H.Proceed(Did, Cand["id"])                                      # XJet made the 360° movie already
    await H.Idle()
    R = await H.Client.post("/api/admin/gallery", json={"design_id": Did}, headers=Admin)
    assert R.status_code == 200, R.text
    return Did, Cand, R.json()


async def test_gallery_lists_curated_designs_publicly_and_is_curated_by_the_admin(HG):
    H = HG
    assert (await H.Client.get("/api/gallery", headers={"X-Access-Token": "NOPE00"})).json()["items"] == []
    Did, Cand, Item = await _Curated(H)
    Items = (await H.Client.get("/api/gallery", headers={"X-Access-Token": "NOPE00"})).json()["items"]   # no sign-in needed
    assert [I["id"] for I in Items] == [Item["id"]] and Items[0]["image_url"] == Cand["image_url"]
    assert set(Items[0]) == {"id", "design_id", "title", "image_url"}        # nothing about the owner leaks
    assert (await H.Client.post("/api/admin/gallery", json={"design_id": Did})).status_code == 403   # customers can't curate
    # Admin: list with statistics, reorder, remove
    B2 = await H.NewDesign("Plain polished band")
    await H.Client.put(f"/api/designs/{B2['design_id']}/selection", json={"candidate_id": B2["candidates"][0]["id"]})
    I2 = (await H.Client.post("/api/admin/gallery", json={"design_id": B2["design_id"]}, headers=Admin)).json()
    L = (await H.Client.get("/api/admin/gallery", headers=Admin)).json()["items"]
    assert [X["id"] for X in L] == [Item["id"], I2["id"]] and [X["position"] for X in L] == [1, 2]
    assert L[0]["ring_id"] == "R-1001-C" and L[0]["ready"] and L[0]["in_gallery"]
    assert (L[0]["sessions"], L[0]["users"], L[0]["customize"], L[0]["bag"], L[0]["checkout"], L[0]["three_d"]) == (0, 0, 0, 0, 0, 0)
    L = (await H.Client.post(f"/api/admin/gallery/{I2['id']}/move", json={"direction": "up"}, headers=Admin)).json()["items"]
    assert [X["id"] for X in L] == [I2["id"], Item["id"]]
    L = (await H.Client.delete(f"/api/admin/gallery/{I2['id']}", headers=Admin)).json()["items"]
    assert [X["id"] for X in L] == [Item["id"]] and L[0]["position"] == 1
    # Changing the image of a design already in the gallery keeps its place
    Other = (await H.Design(Did))["batches"][0]["candidates"][0]
    R = (await H.Client.post("/api/admin/gallery", json={"design_id": Did, "candidate_id": Other["id"]}, headers=Admin)).json()
    assert R["id"] == Item["id"] and R["candidate_id"] == Other["id"] and R["ring_id"] == "R-1001-A"
    # A design without a chosen image cannot be added
    B3 = await H.NewDesign("Signet ring")
    assert (await H.Client.post("/api/admin/gallery", json={"design_id": B3["design_id"]}, headers=Admin)).status_code == 409
    assert (await H.Client.post("/api/admin/gallery", json={"design_id": "dsg_nope"}, headers=Admin)).status_code == 404


async def test_make_it_yours_links_the_customer_to_the_shared_master_design(HG):
    H = HG
    Did, Cand, Item = await _Curated(H)
    Subs, Designs = len(H.Provider.Submissions), H.Ctx.Db.One("SELECT COUNT(*) AS n FROM designs")["n"]
    Token, _ = H.Ctx.Accounts.IssueToken("customer")
    Cust = {"X-Access-Token": Token}
    assert (await H.Client.post(f"/api/gallery/{Item['id']}/start", json={}, headers={"X-Access-Token": "NOPE00"})).status_code == 401
    # The owner removed their own gallery design from My Designs: Make it yours brings it back instead of "Design not found"
    assert (await H.Client.delete(f"/api/designs/{Did}")).status_code == 200
    assert Did not in {X["id"] for X in (await H.Client.get("/api/designs")).json()["designs"]}
    Back = await H.Client.post(f"/api/gallery/{Item['id']}/start", json={})
    assert Back.status_code == 200 and Back.json()["id"] == Did, Back.text
    assert Did in {X["id"] for X in (await H.Client.get("/api/designs")).json()["designs"]}
    assert H.Ctx.Db.One("SELECT removed_at FROM designs WHERE id = ?", (Did,))["removed_at"] is None
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM session_events WHERE design_id = ? AND kind = 'design_restored'", (Did,))["n"] == 1
    R = await H.Client.post(f"/api/gallery/{Item['id']}/start", json={"client_request_id": "tap-1"}, headers=Cust)
    # The customer's steps on the shared design are recorded as theirs (design_opened used to be refused with a 404)
    assert (await H.Client.post("/api/events", json={"kind": "design_opened", "design_id": Did}, headers=Cust)).json()["ok"]
    assert (await H.Client.post("/api/events", json={"kind": "design_opened", "design_id": Did}, headers={"X-Access-Token": H.Ctx.Accounts.IssueToken("stranger")[0]})).status_code == 404
    assert R.status_code == 200, R.text
    D = R.json()
    # The same shared design — not a copy — with the gallery image selected for this customer
    assert D["id"] == Did and D["shared"] and D["origin"] == "gallery" and D["source_ring_id"] == "R-1001-C"
    assert D["selected_candidate_id"] == Cand["id"] and D["customization"] is None
    Master = await H.Design(Did)
    assert [C["image_url"] for C in D["batches"][0]["candidates"]] == [C["image_url"] for C in Master["batches"][0]["candidates"]]
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM designs")["n"] == Designs and len(H.Provider.Submissions) == Subs
    # The customer's own selection; XJet's view of its design is untouched
    First = D["batches"][0]["candidates"][0]
    await H.Client.put(f"/api/designs/{Did}/selection", json={"candidate_id": First["id"]}, headers=Cust)
    assert (await H.Client.get(f"/api/designs/{Did}", headers=Cust)).json()["selected_candidate_id"] == First["id"]
    assert (await H.Design(Did))["selected_candidate_id"] == Cand["id"]
    # Customize on the gallery image: XJet's movie is shown at once (no new movie), choices are the customer's own
    MovieSubs = len(H.Provider.SubmissionsFor(endpoints.Movie))
    Cus = (await H.Client.post(f"/api/designs/{Did}/customize", json={"candidate_id": Cand["id"]}, headers=Cust)).json()
    await H.Idle()
    assert Cus["movie"]["status"] == "ready" and len(H.Provider.SubmissionsFor(endpoints.Movie)) == MovieSubs
    assert Cus["design_id"] == Did and Cus["ring_size"] == 10 and Cus["material_id"] == H.Ctx.Catalog.DefaultMaterialId
    Cus = (await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"ring_size": 7, "material_id": "vermeil"}, headers=Cust)).json()
    Own = (await H.Design(Did))["customization"]
    assert (Own["ring_size"], Own["material_id"]) == (10, H.Ctx.Catalog.DefaultMaterialId)   # XJet's own choices
    assert (await H.Client.get(f"/api/customizations/{Cus['id']}")).status_code == 404        # not XJet's to read
    Bag = (await H.Client.post("/api/bag", json={"customization_id": Cus["id"]}, headers=Cust)).json()
    assert Bag["lines"][0]["design_id"] == Did and Bag["lines"][0]["ring_size"] == 7 and not Bag["lines"][0]["price_is_stale"]
    assert (await H.Client.get("/api/bag")).json()["lines"] == []                            # XJet's bag is empty
    # Starting it again: the same link, moved to the top of My Designs — never a second entry
    Own2 = await H.Client.post("/api/designs", data={"prompt": "My own plain band"}, headers=Cust)
    await H.Idle()
    Mine = (await H.Client.get("/api/designs", headers=Cust)).json()["designs"]
    assert [X["id"] for X in Mine] == [Own2.json()["design_id"], Did] and Mine[1]["shared"] and not Mine[0]["shared"]
    assert (await H.Client.post(f"/api/gallery/{Item['id']}/start", json={"client_request_id": "tap-2"}, headers=Cust)).json()["id"] == Did
    Mine = (await H.Client.get("/api/designs", headers=Cust)).json()["designs"]
    assert [X["id"] for X in Mine] == [Did, Own2.json()["design_id"]]
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM gallery_uses WHERE design_id = ?", (Did,))["n"] == 1
    assert (await H.Client.get(f"/api/designs/{Did}", headers=Cust)).json()["selected_candidate_id"] == Cand["id"]   # kept
    # A customer who never started from it cannot see the master design
    Stranger = {"X-Access-Token": H.Ctx.Accounts.IssueToken("stranger")[0]}
    assert (await H.Client.get(f"/api/designs/{Did}", headers=Stranger)).status_code == 404
    # A movie the customer asks for on another option is charged to the customer, not to XJet
    XjetUsed = (await H.Client.get("/api/session")).json()["used"]
    Sib = D["batches"][0]["candidates"][1]
    Cus3 = (await H.Client.post(f"/api/designs/{Did}/customize", json={"candidate_id": Sib["id"]}, headers=Cust)).json()
    await H.Idle()
    assert (await H.Client.get(f"/api/customizations/{Cus3['id']}", headers=Cust)).json()["movie"]["status"] == "ready"
    assert (await H.Client.get("/api/session", headers=Cust)).json()["used"] == 2     # their own design request and this movie
    assert (await H.Client.get("/api/session")).json()["used"] == XjetUsed
    # A refinement forks into a design of the customer's own; the master is never changed
    Fork = (await H.Client.post(f"/api/designs/{Did}/batches", json={"parent_candidate_id": Cand["id"], "instruction": "thinner band",
                                                                      "client_request_id": "ref-1"}, headers=Cust)).json()
    await H.Idle()
    assert Fork["design_id"] != Did and Fork["kind"] == "refine" and Fork["parent_candidate_id"] == Cand["id"]
    F = (await H.Client.get(f"/api/designs/{Fork['design_id']}", headers=Cust)).json()
    assert F["origin"] == "gallery" and F["source_ring_id"] == "R-1001-C" and len(F["batches"]) == 1
    assert F["batches"][0]["status"] == "complete" and not F["shared"]
    assert len((await H.Design(Did))["batches"]) == 1
    assert (await H.Client.post(f"/api/designs/{Did}/batches", json={"parent_candidate_id": Cand["id"], "instruction": "thinner band",
                                                                      "client_request_id": "ref-1"}, headers=Cust)).json()["design_id"] == Fork["design_id"]
    # Admin: statistics per master design, the customer list, and the customer's journey as a session
    G = (await H.Client.get("/api/admin/gallery", headers=Admin)).json()["items"][0]
    assert (G["sessions"], G["users"], G["customize"], G["bag"], G["checkout"], G["forks"]) == (1, 1, 1, 1, 0, 1)
    Uses = (await H.Client.get(f"/api/admin/gallery/usage/{Did}", headers=Admin)).json()["uses"]
    assert len(Uses) == 1 and Uses[0]["session_id"].startswith("use_") and Uses[0]["add_to_bag"] and Uses[0]["ring_size"] == 7
    assert Uses[0]["material_id"] == "vermeil" and Uses[0]["stage_reached"] == "bag" and Uses[0]["account_id"] != H.Who.AccountId
    S = (await H.Client.get(f"/api/admin/sessions/{Uses[0]['session_id']}", headers=Admin)).json()
    assert S["session"]["shared"] and S["session"]["origin"] == "gallery" and S["session"]["source_ring_id"] == "R-1001-C"
    assert S["design"]["shared"] and S["design"]["id"] == Did
    assert S["three_d_defaults"]["customer_size"] == 7 and S["three_d_defaults"]["material_id"] == "vermeil"
    Kinds = [E["kind"] for E in S["timeline"]]
    assert "gallery_started" in Kinds and "bag_added" in Kinds and "started" not in Kinds
    assert S["gallery"]["design_id"] == Did and len(S["gallery_usage"]) == 1
    assert sum(1 for X in S["pipeline"]["steps"] if X["kind"] == "movie") == 1       # only the movie this customer asked for
    M = (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()
    assert not M["session"]["shared"] and len(M["gallery_usage"]) == 1 and M["session"]["ring_size_chosen"] is False
    All = (await H.Client.get("/api/admin/sessions?include_mock=true", headers=Admin)).json()["sessions"]
    assert {X["session_id"] for X in All} >= {Did, Uses[0]["session_id"], Fork["design_id"]}
    # Generate 3D from the customer's journey: their size and material, the shared design's model
    T = (await H.Client.post(f"/api/admin/sessions/{Uses[0]['session_id']}/3d", json={}, headers=Admin)).json()
    assert (T["design_id"], T["production_size"], T["size_source"], T["material_id"]) == (Did, 7, "customer", "vermeil")
    assert T["candidate_id"] == Sib["id"]                                   # the customer's own selected option
    await H.Idle()
    assert (await H.Client.get("/api/admin/gallery", headers=Admin)).json()["items"][0]["three_d"] == 1
    # The master now has a valid model: every later request on any journey reuses it — never a second Hi3D call
    MeshSubs = len(H.Provider.SubmissionsFor(endpoints.Mesh))
    T2 = (await H.Client.post(f"/api/admin/sessions/{Uses[0]['session_id']}/3d", json={"production_size": 9}, headers=Admin)).json()
    await H.Idle()
    assert T2["mesh_id"] == T["mesh_id"] and T2["production_size"] == 9 and len(H.Provider.SubmissionsFor(endpoints.Mesh)) == MeshSubs
    assert (await H.Client.post(f"/api/admin/sessions/{Uses[0]['session_id']}/3d", json={"candidate_id": Cand["id"]},
                                headers=Admin)).status_code == 409          # another option needs the typed confirmation
    S2 = (await H.Client.get(f"/api/admin/sessions/{Uses[0]['session_id']}", headers=Admin)).json()
    Ex = S2["three_d_defaults"]["existing_model"]
    assert Ex["candidate_id"] == Sib["id"] and Ex["ring_id"] == "R-1001-B" and Ex["measured"] and Ex["preview_ready"]
    # The journey's own size/material (US 7 · vermeil) already has a result: the scaled STL can be prepared from it
    assert Ex["journey_3d_id"] == T["id"] and (Ex["journey_size"], Ex["journey_material_id"]) == (7, "vermeil")
    assert Ex["latest_3d_id"] == T2["id"] and Ex["production_state"] == "complete" and Ex["review"] == []
    # The master's own page shows the same model (XJet made no choices: default US 10 · silver has no result yet)
    Mx = (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()["three_d_defaults"]["existing_model"]
    assert Mx["ring_id"] == "R-1001-B" and Mx["journey_3d_id"] is None
    Dash = (await H.Client.get("/api/admin/dashboard", headers=Admin)).json()
    assert set(Dash["funnel_by_origin"]) == {"prompt", "gallery"} and [R["size"] for R in Dash["geometry"]["by_size"]] == [7, 8, 10, 11]
    # Removing the tile keeps the customer's link; starting from the tile is no longer possible
    await H.Client.delete(f"/api/admin/gallery/{Item['id']}", headers=Admin)
    assert (await H.Client.get(f"/api/designs/{Did}", headers=Cust)).status_code == 200
    assert (await H.Client.post(f"/api/gallery/{Item['id']}/start", json={}, headers=Cust)).status_code == 404
    Former = (await H.Client.get("/api/admin/gallery", headers=Admin)).json()["items"]
    assert Former and not Former[0]["in_gallery"] and Former[0]["sessions"] == 1


async def test_dashboard_geometry_by_size_scales_measured_models_by_arithmetic(HG):
    """One mock model measured at US 10: the table gives its volume at the reference sizes, with the
    weight per material from the pricing table, and the fixed-price basis of 1 cm³."""
    H = HG
    Batch = await H.NewDesign("Plain band")
    from tests.conftest import MakeLive
    MakeLive(H)                                                            # dashboard statistics are live-only
    H.Ctx.Db.Execute("UPDATE designs SET ai_mode = 'fal'")
    await H.Client.post(f"/api/admin/sessions/{Batch['design_id']}/3d", json={}, headers=Admin)
    await H.Idle()
    Geo = (await H.Client.get("/api/admin/dashboard", headers=Admin)).json()["geometry"]
    assert Geo["measured"] == 1 and Geo["models"] == 1 and Geo["avg_volume_cc"] > 0
    assert (Geo["models_created"], Geo["models_measured"], Geo["models_used"]) == (1, 1, 1)
    assert set(Geo["avg_weight_g_by_material"]) >= {"silver", "vermeil", "stainless_steel"}
    Rows = {R["size"]: R for R in Geo["by_size"]}
    Ten = Rows[10]
    assert all(R["models"] == 1 for R in Geo["by_size"])
    assert Ten["volume_cc"] == pytest.approx(Geo["avg_volume_cc"], rel=1e-3)             # measured at US 10
    assert Rows[7]["volume_cc"] < Rows[8]["volume_cc"] < Ten["volume_cc"] < Rows[11]["volume_cc"]
    Silver = H.Ctx.MaterialPrices.Row("silver")
    assert Ten["materials"]["silver"]["weight_g"] == pytest.approx(Ten["volume_cc"] * Silver["density_g_cm3"], abs=0.01)
    assert Ten["materials"]["silver"]["price_3d"] == pytest.approx(Ten["materials"]["silver"]["weight_g"] * Silver["price_per_g"], abs=0.2)
    assert Geo["fixed_basis"]["volume_cc"] == 1.0 and Geo["fixed_basis"]["materials"]["silver"]["weight_g"] == pytest.approx(Silver["density_g_cm3"])
    assert Geo["fixed_basis"]["materials"]["silver"]["fixed_price"] == Silver["fixed_price"]


async def test_remove_from_my_designs_hides_it_for_the_customer_and_keeps_the_admin_journey(HG):
    H = HG
    Did, Cand, Item = await _Curated(H)
    Token, _ = H.Ctx.Accounts.IssueToken("customer")
    Cust = {"X-Access-Token": Token}
    await H.Client.post(f"/api/gallery/{Item['id']}/start", json={}, headers=Cust)
    Own = (await H.Client.post("/api/designs", data={"prompt": "My own band"}, headers=Cust)).json()["design_id"]
    await H.Idle()
    assert [d["id"] for d in (await H.Client.get("/api/designs", headers=Cust)).json()["designs"]] == [Own, Did]
    # Removing the shared design unlinks it for the customer; the Admin keeps the journey and the statistics
    assert (await H.Client.delete(f"/api/designs/{Did}", headers=Cust)).json() == {"removed": True, "shared": True}
    assert [d["id"] for d in (await H.Client.get("/api/designs", headers=Cust)).json()["designs"]] == [Own]
    assert (await H.Client.get(f"/api/designs/{Did}", headers=Cust)).status_code == 404
    Uses = (await H.Client.get(f"/api/admin/gallery/usage/{Did}", headers=Admin)).json()["uses"]
    assert len(Uses) == 1 and Uses[0]["removed"]
    assert (await H.Client.get("/api/admin/gallery", headers=Admin)).json()["items"][0]["sessions"] == 1
    # Starting it again from the gallery restores the same link — still one journey
    assert (await H.Client.post(f"/api/gallery/{Item['id']}/start", json={}, headers=Cust)).json()["id"] == Did
    assert (await H.Client.get("/api/designs", headers=Cust)).json()["designs"][0]["id"] == Did
    assert not (await H.Client.get(f"/api/admin/gallery/usage/{Did}", headers=Admin)).json()["uses"][0]["removed"]
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM gallery_uses")["n"] == 1
    # Removing a design of their own hides it; the Admin still has the session, marked as removed
    assert (await H.Client.delete(f"/api/designs/{Own}", headers=Cust)).json() == {"removed": True, "shared": False}
    assert (await H.Client.get(f"/api/designs/{Own}", headers=Cust)).status_code == 404
    assert [d["id"] for d in (await H.Client.get("/api/designs", headers=Cust)).json()["designs"]] == [Did]
    S = (await H.Client.get(f"/api/admin/sessions/{Own}", headers=Admin)).json()
    assert S["session"]["removed"] and any(E["kind"] == "design_removed" for E in S["timeline"])
    # Twice, or someone else's design: 404
    assert (await H.Client.delete(f"/api/designs/{Own}", headers=Cust)).status_code == 404
    Stranger = {"X-Access-Token": H.Ctx.Accounts.IssueToken("stranger")[0]}
    assert (await H.Client.delete(f"/api/designs/{Did}", headers=Stranger)).status_code == 404
