"""The 360° movie switch per product (Admin → Settings → Products; default ON): OFF makes and shows no movie in that
product's flow — Customize shows the still image, an explicit movie request is refused, existing movies stay hidden —
and ON shows and reuses them again; each product has its own switch; customers cannot touch it."""

from p3.providers import endpoints
from tests.conftest import Harness

AdminKey = "movie-toggle-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}


async def _Movie(H, Product, On):
    R = await H.Client.put("/api/admin/products/movie", json={"product": Product, "on": On}, headers=Admin)
    assert R.status_code == 200, R.text
    return R.json()


async def _Used(H) -> int:
    return (await H.Client.get("/api/token-status")).json()["used"]


async def test_the_movie_switch_hides_and_stops_movies_per_product(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        St = (await H.Client.get("/api/admin/products", headers=Admin)).json()
        assert St["movies"] == {"ring": True, "charm": True} and all(P["movie"] for P in St["products"])     # default ON
        # Rings OFF: Customize makes no movie, shows none; the explicit request is refused; no credit for a movie
        St = await _Movie(H, "ring", False)
        assert St["movies"] == {"ring": False, "charm": True} and St["log"][0]["key"] == "rings_movie" and St["log"][0]["value"] is False
        assert St["products"][0]["movie"] is False and St["products"][0]["movie_changed"]["updated_by"]
        Batch = await H.NewDesign("A slim band")
        Cand = Batch["candidates"][0]["id"]
        Cus = await H.Proceed(Batch["design_id"], Cand)
        await H.Idle()
        assert Cus["movie"] is None and Cus["movie_available"] is False
        assert H.Provider.SubmissionsFor(endpoints.Movie) == [] and await _Used(H) == 1          # the design request only
        R = await H.Client.post(f"/api/candidates/{Cand}/movie")
        assert R.status_code == 409 and R.json()["error"]["code"] == "movie_off"
        assert (await H.Client.get(f"/api/customizations/{Cus['id']}")).json()["movie"] is None
        # Back ON: Customize makes the movie (one credit); OFF again hides it without deleting it; ON shows it, free
        await _Movie(H, "ring", True)
        Cus = await H.Proceed(Batch["design_id"], Cand)
        await H.Idle()
        Fresh = (await H.Client.get(f"/api/customizations/{Cus['id']}")).json()
        assert Fresh["movie_available"] is True and Fresh["movie"]["status"] == "ready" and await _Used(H) == 2
        assert len(H.Provider.SubmissionsFor(endpoints.Movie)) == 1
        await _Movie(H, "ring", False)
        Hidden = (await H.Client.get(f"/api/customizations/{Cus['id']}")).json()
        assert Hidden["movie"] is None and Hidden["movie_available"] is False
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM movies")["n"] == 1                       # kept, not deleted
        await _Movie(H, "ring", True)
        Again = await H.Proceed(Batch["design_id"], Cand)                                       # reused: no new movie, no credit
        await H.Idle()
        assert Again["movie"]["status"] == "ready" and len(H.Provider.SubmissionsFor(endpoints.Movie)) == 1 and await _Used(H) == 2
        # Charms have their own switch: ring OFF leaves a charm's movie untouched
        await _Movie(H, "ring", False)
        await H.Client.put("/api/admin/products/availability", json={"product": "charm", "available": True}, headers=Admin)
        CB = await H.NewDesign("A heart charm", product="charm")
        CC = await H.Proceed(CB["design_id"], CB["candidates"][0]["id"])
        await H.Idle()
        assert CC["movie_available"] is True and len(H.Provider.SubmissionsFor(endpoints.Movie)) == 2
        # Customers cannot switch; bad values are refused
        assert (await H.Client.put("/api/admin/products/movie", json={"product": "ring", "on": True})).status_code == 403
        assert (await H.Client.put("/api/admin/products/movie", json={"product": "ring", "on": "yes"}, headers=Admin)).status_code == 400
        assert (await H.Client.put("/api/admin/products/movie", json={"product": "hat", "on": True}, headers=Admin)).status_code == 400
        # The customer page shows the still image alone while the switch is off
        Page = (await H.Client.get("/")).text
        assert 'x-show="!movieOff"' in Page and "movieOff || mediaTab === 'image'" in Page
    finally:
        await H.Close()
