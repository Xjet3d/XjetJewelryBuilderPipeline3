"""B5 (11 Nov 2026): credits pay for creating. A customer with no credit left still opens Customize, chooses metal and
size and orders: a ring's Customize then starts no 360° movie (nothing charged) and shows the still images; a movie the
design option already has is still shown. New designs stay refused at 0."""

from tests.conftest import Harness

AdminKey = "zero-credit-admin"
Admin = {"Authorization": f"Bearer {AdminKey}"}


def _Spend(H):
    """Every credit of the harness customer used."""
    H.Ctx.Accounts.Db.Execute("UPDATE accounts SET generations_used = max_generations, tokens_reserved = 0 WHERE account_id = ?",
                              (H.Who.AccountId,))
    return H.Ctx.Accounts.Profile(H.Who)


async def test_a_ring_is_customized_and_bagged_at_zero_credits_without_a_movie(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        B = await H.NewDesign("A twisted rope band", product="ring")
        Cand = B["candidates"][1]["id"]
        P = _Spend(H)
        assert P["remaining"] == 0
        R = await H.Client.post(f"/api/designs/{B['design_id']}/customize", json={"candidate_id": Cand})
        assert R.status_code == 200, R.text
        C = R.json()
        assert C["movie_available"] is True and C["movie"] is None          # still images; the switch itself is on
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM movies WHERE candidate_id = ?", (Cand,))["n"] == 0
        assert H.Ctx.Accounts.Profile(H.Who)["remaining"] == 0 and H.Ctx.Accounts.Profile(H.Who)["reserved"] == 0
        assert (await H.Client.patch(f"/api/customizations/{C['id']}", json={"ring_size": 7, "material_id": "silver"})).status_code == 200
        Bag = await H.Client.post("/api/bag", json={"customization_id": C["id"]})
        assert Bag.status_code in (200, 201), Bag.text
        Again = await H.Client.post(f"/api/designs/{B['design_id']}/customize", json={"candidate_id": Cand})
        assert Again.status_code == 200 and Again.json()["movie"] is None   # entered again: still no charge, no movie
        New = await H.Client.post("/api/designs", data={"prompt": "Another ring", "product": "ring"})
        assert New.status_code == 402 and "still customize and order" in New.json()["error"]["message"]
    finally:
        await H.Close()


async def test_an_existing_movie_is_still_shown_at_zero_credits(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        B = await H.NewDesign("A twisted rope band", product="ring")
        Cand = B["candidates"][1]["id"]
        First = (await H.Client.post(f"/api/designs/{B['design_id']}/customize", json={"candidate_id": Cand})).json()
        await H.Idle()
        assert (await H.Client.get(f"/api/customizations/{First['id']}")).json()["movie"]["status"] == "ready"
        _Spend(H)
        R = await H.Client.post(f"/api/designs/{B['design_id']}/customize", json={"candidate_id": Cand})
        assert R.status_code == 200 and R.json()["movie"]["status"] == "ready"
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM movies WHERE candidate_id = ?", (Cand,))["n"] == 1
    finally:
        await H.Close()


async def test_the_explicit_movie_request_still_needs_a_credit(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        B = await H.NewDesign("A twisted rope band", product="ring")
        Cand = B["candidates"][2]["id"]
        _Spend(H)
        R = await H.Client.post(f"/api/candidates/{Cand}/movie")
        assert R.status_code == 402 and R.json()["error"]["code"] == "quota_exhausted"
    finally:
        await H.Close()
