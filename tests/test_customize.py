"""Customize: static preview, Minimax movie, material groups, size-invariant price, bag enforcement."""

import pytest

from p3.providers import endpoints


async def _Ready(Hx, Prompt="Twisted rope band"):
    Batch = await Hx.NewDesign(Prompt)
    return Batch["design_id"], Batch["candidates"][3]


async def test_the_metal_preview_colours_the_whole_frame_except_the_light_backdrop(H):
    """The metal filter (web/metal.js, used on the Customize image and 360° movie) decides by brightness alone, the
    same at every point of the frame: light pixels (the white background of a still, the light grey backdrop of a
    movie) stay as generated, everything darker takes the metal. No spatial window: the former soft centred window
    left grey bands at the top and bottom of a movie and a charm's loop or a ring's rim near the edge uncoloured."""
    Js = (await H.Client.get("/static/metal.js")).text
    for Spatial in ("feFlood", "feGaussianBlur", "feOffset", "primitiveUnits", 'result="centre"', 'result="box"'):
        assert Spatial not in Js, Spatial
    # The mask: alpha = 12.82 − 4.545 × (r + g + b): fully background from an average of 0.94, the full metal from 0.87
    assert 'values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 -4.545 -4.545 -4.545 0 12.82"' in Js
    assert round(12.82 / 4.545 / 3, 3) == 0.940 and round((12.82 - 1) / 4.545 / 3, 3) == 0.867
    assert '<feComposite in="metal" in2="lum" operator="in" result="ring"/>' in Js
    assert '<feMerge><feMergeNode in="SourceGraphic"/><feMergeNode in="ring"/></feMerge>' in Js
    # The filter region is the whole element, for every material
    Page = (await H.Client.get("/")).text
    assert 'x-html="metalFilterDefs"' in Page
    assert Js.count('x="0" y="0" width="1" height="1" color-interpolation-filters="sRGB"') == 1    # the one opener


async def test_an_images_movie_is_reused_after_the_movie_configuration_changes(tmp_path):
    """One movie per selected image: a movie configuration activated later never makes a second movie for an image
    that already has one (Marée Facet got one on proto on 2026-10-06); a new configuration applies to images without
    a movie. Customize and the Admin session page show the image's newest existing movie."""
    from p3.db import Now
    from tests.conftest import Harness
    Key = "movie-admin-key"
    H = Harness(tmp_path, AdminKey=Key)
    try:
        DesignId, Cand = await _Ready(H)
        First = await H.Proceed(DesignId, Cand["id"])
        await H.Idle()
        assert len(H.Provider.SubmissionsFor(endpoints.Movie)) == 1
        Active = H.Ctx.Models.Active("minimax-camera")
        H.Ctx.Models.SaveAndActivate("minimax-camera", dict(Active.Params, duration=8), "admin", "a longer turn")
        assert H.Ctx.Models.Active("minimax-camera").Id != Active.Id
        Again = await H.Proceed(DesignId, Cand["id"])                                  # back to the same image
        await H.Idle()
        assert Again["movie"]["id"] == First["movie"]["id"] and Again["movie"]["status"] == "ready"
        assert len(H.Provider.SubmissionsFor(endpoints.Movie)) == 1                     # no second movie, nothing paid
        assert (await H.Client.get(f"/api/customizations/{Again['id']}")).json()["movie"]["id"] == First["movie"]["id"]
        # An image without a movie gets one with the new configuration
        Other = (await H.Client.get(f"/api/batches/{Cand['batch_id']}")).json()["candidates"][0]
        await H.Proceed(DesignId, Other["id"])
        await H.Idle()
        Subs = H.Provider.SubmissionsFor(endpoints.Movie)
        assert len(Subs) == 2 and Subs[-1][1]["duration"] == 8
        # Two ready movies for one image (an older configuration's and a newer one): the newest is the one shown
        Old = H.Ctx.Db.One("SELECT * FROM movies WHERE id = ?", (First["movie"]["id"],))
        H.Ctx.Db.Execute("INSERT INTO movies (id, candidate_id, config_version, endpoint, status, asset_path, created_at, updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?)", ("mov_older", Cand["id"], "minimax-camera@v0-older", Old["endpoint"], "ready",
                                                     "older.mp4", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
        assert H.Svc.Movies.Latest(Cand["id"])["id"] == First["movie"]["id"]
        Admin = {"Authorization": f"Bearer {Key}"}
        await H.Client.put(f"/api/designs/{DesignId}/selection", json={"candidate_id": Cand["id"]})
        D = (await H.Client.get(f"/api/admin/sessions/{DesignId}", headers=Admin)).json()
        assert D["artifacts"]["movie_url"] == H.Ctx.AssetUrl(Old["asset_path"])
    finally:
        await H.Close()


async def test_proceed_shows_selected_image_immediately_and_starts_one_movie(H):
    DesignId, Cand = await _Ready(H)
    Cus = await H.Proceed(DesignId, Cand["id"])
    assert Cus["image_url"] == Cand["image_url"]
    assert Cus["candidate_id"] == Cand["id"]
    assert Cus["material_id"] == "silver" and Cus["ring_size"] == 10.0          # opens on US 10
    assert Cus["movie"]["status"] in ("queued", "running")
    assert (await H.Design(DesignId))["selected_candidate_id"] == Cand["id"]
    await H.Idle()

    Subs = H.Provider.SubmissionsFor(endpoints.Movie)
    assert len(Subs) == 1
    Args = Subs[0][1]
    assert H.Provider.Uploads[Args["image_url"]] == H.AssetBytes(Cand["image_url"])
    assert Args["prompt_expansion_mode"] in ("disabled", "balanced", "quality")
    assert 2 <= len(Args["camera_trajectory"]) <= 12 and 3 <= Args["duration"] <= 15
    assert Args["resolution"] in ("480P", "768P", "1080P")
    # Nothing Veo-specific, no hull/measurement/mesh on the customer path.
    assert not {"aspect_ratio", "negative_prompt", "generate_audio"} & set(Args)
    assert H.Provider.SubmissionsFor(endpoints.Mesh) == []

    Cus = (await H.Client.get(f"/api/customizations/{Cus['id']}")).json()
    assert Cus["movie"]["status"] == "ready" and Cus["movie"]["movie_url"].endswith(".mp4")
    assert Cus["image_url"] == Cand["image_url"]          # static image stays available


async def test_repeated_proceed_reuses_the_same_movie(H):
    DesignId, Cand = await _Ready(H)
    First = await H.Proceed(DesignId, Cand["id"])
    Second = await H.Proceed(DesignId, Cand["id"])
    assert First["id"] == Second["id"] and First["movie"]["id"] == Second["movie"]["id"]
    await H.Idle()
    await H.Proceed(DesignId, Cand["id"])
    R = await H.Client.post(f"/api/candidates/{Cand['id']}/movie")
    assert R.json()["status"] == "ready"
    assert len(H.Provider.SubmissionsFor(endpoints.Movie)) == 1


async def test_movie_failure_keeps_image_and_price_and_retries_only_movie(HDevPricing):
    H = HDevPricing
    DesignId, Cand = await _Ready(H)
    H.Provider.Script(endpoints.Movie, "fail")
    Cus = await H.Proceed(DesignId, Cand["id"])
    await H.Idle()
    Cus = (await H.Client.get(f"/api/customizations/{Cus['id']}")).json()
    assert Cus["movie"]["status"] == "failed" and Cus["movie"]["retryable"]
    assert Cus["image_url"] == Cand["image_url"] and Cus["quote"]["pricing_status"] == "available"
    ImageSubs = len(H.Provider.SubmissionsFor(endpoints.ImageGenerate))

    R = await H.Client.post(f"/api/candidates/{Cand['id']}/movie")
    assert R.status_code == 200 and R.json()["id"] != Cus["movie"]["id"]
    await H.Idle()
    Cus = (await H.Client.get(f"/api/customizations/{Cus['id']}")).json()
    assert Cus["movie"]["status"] == "ready"
    assert len(H.Provider.SubmissionsFor(endpoints.ImageGenerate)) == ImageSubs
    assert len(H.Provider.SubmissionsFor(endpoints.Movie)) == 2


async def test_different_candidate_gets_its_own_customization_and_movie(H):
    Batch = await H.NewDesign("Band")
    A = await H.Proceed(Batch["design_id"], Batch["candidates"][0]["id"])
    B = await H.Proceed(Batch["design_id"], Batch["candidates"][1]["id"])
    assert A["id"] != B["id"] and A["movie"]["id"] != B["movie"]["id"]


async def test_catalog_groups_and_default():
    from p3.config import LoadCatalog
    Cat = LoadCatalog().ToJson()
    assert Cat["default_material_id"] == "silver"
    Groups = {G["id"]: G for G in Cat["groups"]}
    assert [M["label"] for M in Groups["fashion"]["materials"]] == ["Stainless Steel", "Silver", "Vermeil"]
    assert Groups["fashion"]["purchasable"] and not Groups["luxury"]["purchasable"]
    assert all("Gold" in M["label"] for M in Groups["luxury"]["materials"])


async def test_size_changes_never_change_unit_price_and_are_preserved(HDevPricing):
    H = HDevPricing
    DesignId, Cand = await _Ready(H)
    Cus = await H.Proceed(DesignId, Cand["id"])
    Prices = set()
    for Size in (4, 6.5, 9, 12):
        R = await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"ring_size": Size})
        assert R.status_code == 200 and R.json()["ring_size"] == Size
        Prices.add(R.json()["quote"]["unit_price"])
    assert len(Prices) == 1 and Prices.pop() > 0
    Q1 = (await H.Client.get("/api/quote", params={"material_id": "silver", "ring_size": 5})).json()
    Q2 = (await H.Client.get("/api/quote", params={"material_id": "silver", "ring_size": 11})).json()
    assert Q1["unit_price"] == Q2["unit_price"] and Q1["assumed_volume_cm3"] == 1.0
    R = await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"ring_size": 7.25})
    assert R.status_code == 400 and R.json()["error"]["code"] == "invalid_ring_size"


async def test_quantity_changes_line_total_not_unit_price(HDevPricing):
    H = HDevPricing
    DesignId, Cand = await _Ready(H)
    Cus = await H.Proceed(DesignId, Cand["id"])
    R = (await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"quantity": 3})).json()
    assert R["quote"]["unit_price"] == Cus["quote"]["unit_price"]
    assert R["line_total"] == pytest.approx(3 * Cus["quote"]["unit_price"])


async def test_luxury_clears_price_and_blocks_bag_then_fashion_restores(HDevPricing):
    H = HDevPricing
    DesignId, Cand = await _Ready(H)
    Cus = await H.Proceed(DesignId, Cand["id"])
    Cus = (await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"ring_size": 7})).json()
    SilverPrice = Cus["quote"]["unit_price"]
    assert Cus["can_add_to_bag"]

    Lux = (await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"material_id": "gold_14k_yellow"})).json()
    assert Lux["quote"]["pricing_status"] == "unavailable" and Lux["quote"]["unit_price"] is None
    assert Lux["line_total"] is None and not Lux["can_add_to_bag"]
    assert Lux["add_to_bag_blocked_reason"] == "luxury_preview_only"
    R = await H.Client.post("/api/bag", json={"customization_id": Cus["id"]})
    assert R.status_code == 409 and R.json()["error"]["code"] == "luxury_preview_only"
    assert (await H.Client.get("/api/bag")).json()["lines"] == []

    Back = (await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"material_id": "silver"})).json()
    assert Back["quote"]["unit_price"] == SilverPrice and Back["can_add_to_bag"]


async def test_bag_enforces_size_and_quote_server_side(HDevPricing):
    H = HDevPricing
    DesignId, Cand = await _Ready(H)
    Cus = await H.Proceed(DesignId, Cand["id"])
    await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"ring_size": None})   # size cleared
    R = await H.Client.post("/api/bag", json={"customization_id": Cus["id"]})
    assert R.status_code == 409 and R.json()["error"]["code"] == "ring_size_required"

    await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"ring_size": 8, "quantity": 2})
    R = await H.Client.post("/api/bag", json={"customization_id": Cus["id"]})
    assert R.status_code == 200
    Bag = R.json()
    assert len(Bag["lines"]) == 1 and Bag["checkout_available"]            # a priced, sized fashion line can be ordered
    Line = Bag["lines"][0]
    assert Line["ring_size"] == 8 and Line["quantity"] == 2 and Line["candidate_id"] == Cand["id"]
    assert Line["quote"]["assumed_volume_cm3"] == 1.0
    assert Bag["totals"]["USD"] == pytest.approx(2 * Line["unit_price"])

    await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"ring_size": 10, "quantity": 1})
    Bag = (await H.Client.post("/api/bag", json={"customization_id": Cus["id"]})).json()
    assert {L["ring_size"] for L in Bag["lines"]} == {8, 10}
    assert len({L["unit_price"] for L in Bag["lines"]}) == 1      # every size, same unit price

    R = await H.Client.delete(f"/api/bag/{Bag['lines'][0]['id']}")
    assert len(R.json()["lines"]) == 1


def _ClearFixedPrices(H):
    """No fixed price in Admin → Material pricing (NA): the website falls back to the pricing profile."""
    Doc = H.Ctx.MaterialPrices.Current()
    H.Ctx.MaterialPrices.Save({"materials": {M: {**R, "fixed_price": None} for M, R in Doc["materials"].items()}}, "test")


async def test_bag_line_is_stale_only_after_the_material_price_changes(H):
    DesignId, Cand = await _Ready(H)
    Cus = await H.Proceed(DesignId, Cand["id"])
    Cus = (await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"ring_size": 7})).json()
    Bag = (await H.Client.post("/api/bag", json={"customization_id": Cus["id"]})).json()
    Line = Bag["lines"][0]
    assert Line["pricing_version"] == Cus["quote"]["pricing_version"] and not Line["price_is_stale"]   # just added
    assert not (await H.Client.get("/api/bag")).json()["lines"][0]["price_is_stale"]
    Doc = H.Ctx.MaterialPrices.Current()
    H.Ctx.MaterialPrices.Save({"materials": {M: {**R, "fixed_price": (R["fixed_price"] or 0) + 5 if M == "silver" else R["fixed_price"]}
                                             for M, R in Doc["materials"].items()}}, "test", "silver +5")
    assert (await H.Client.get("/api/bag")).json()["lines"][0]["price_is_stale"]        # the table changed


async def test_bag_rejects_fashion_when_pricing_unavailable(H):
    _ClearFixedPrices(H)
    DesignId, Cand = await _Ready(H)
    Cus = await H.Proceed(DesignId, Cand["id"])
    Cus = (await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"ring_size": 7})).json()
    assert Cus["quote"]["pricing_status"] == "unavailable" and not Cus["can_add_to_bag"]
    R = await H.Client.post("/api/bag", json={"customization_id": Cus["id"]})
    assert R.status_code == 409 and R.json()["error"]["code"] == "price_unavailable"


async def test_customizations_are_owner_scoped(H):
    DesignId, Cand = await _Ready(H)
    Cus = await H.Proceed(DesignId, Cand["id"])
    Other = {"X-Access-Token": H.Ctx.Accounts.IssueToken("other")[0]}
    assert (await H.Client.get(f"/api/customizations/{Cus['id']}", headers=Other)).status_code == 404
    assert (await H.Client.get(f"/api/designs/{DesignId}", headers=Other)).status_code == 404
    assert (await H.Client.post("/api/bag", json={"customization_id": Cus["id"]}, headers=Other)).status_code == 404


async def test_reload_recovers_design_state_without_new_requests(H):
    DesignId, Cand = await _Ready(H)
    await H.Proceed(DesignId, Cand["id"])
    await H.Idle()
    Before = len(H.Provider.Submissions)
    D = await H.Design(DesignId)
    assert D["selected_candidate_id"] == Cand["id"]
    assert D["customization"]["movie"]["status"] == "ready"
    assert len(D["batches"]) == 1 and len(D["batches"][0]["candidates"]) == 4
    assert len(H.Provider.Submissions) == Before
    Listed = (await H.Client.get("/api/designs")).json()["designs"]
    assert Listed[0]["id"] == DesignId and Listed[0]["thumbnail_url"] == Cand["image_url"]


async def test_luxury_order_is_yellow_row_then_rose_row():
    from p3.config import LoadCatalog
    Lux = [M["label"] for G in LoadCatalog().ToJson()["groups"] if G["id"] == "luxury" for M in G["materials"]]
    assert Lux == ["10K Yellow Gold", "14K Yellow Gold", "18K Yellow Gold",
                   "10K Rose Gold", "14K Rose Gold", "18K Rose Gold"]


async def test_mock_movie_is_playable_without_ffmpeg(tmp_path, monkeypatch):
    """Servers without ffmpeg (e.g. tron) still get a real MP4 in mock mode."""
    import shutil
    from p3.providers.mock import MockProvider
    from tests.conftest import Harness
    monkeypatch.setattr(shutil, "which", lambda Name: None)
    H = Harness(tmp_path, Provider=MockProvider(LatencyS=0.0, RenderVideo=True))
    try:
        DesignId, Cand = await _Ready(H)
        Cus = await H.Proceed(DesignId, Cand["id"])
        await H.Idle()
        Url = (await H.Client.get(f"/api/customizations/{Cus['id']}")).json()["movie"]["movie_url"]
        Data = H.AssetBytes(Url)
        assert Data[4:8] == b"ftyp" and len(Data) > 10_000
    finally:
        await H.Close()
