"""♥ Favorites: a customer's saved references to gallery masters, per account — never a copy, never a My Design."""

from tests.test_gallery import HG, Admin, _Curated  # noqa: F401 — the fixture is used by name


async def test_favorites_are_saved_references_to_gallery_masters_per_account(HG):
    H = HG
    Did, Cand, Item = await _Curated(H)
    Tiles = (await H.Client.get("/api/gallery", headers={"X-Access-Token": "NOPE00"})).json()["items"]
    assert Tiles[0]["design_id"] == Did and set(Tiles[0]) == {"id", "design_id", "title", "image_url"}
    Other, _ = H.Ctx.Accounts.IssueToken("other")
    Me = {"X-Access-Token": Other}
    assert (await H.Client.get("/api/favorites", headers={"X-Access-Token": "NOPE00"})).status_code in (401, 403)
    assert (await H.Client.get("/api/favorites", headers=Me)).json()["items"] == []
    R = await H.Client.put(f"/api/favorites/{Did}", headers=Me)
    assert R.status_code == 200, R.text
    assert [F["design_id"] for F in R.json()["items"]] == [Did]
    assert R.json()["items"][0]["id"] == Item["id"] and R.json()["items"][0]["title"] == Tiles[0]["title"]
    assert len((await H.Client.put(f"/api/favorites/{Did}", headers=Me)).json()["items"]) == 1      # idempotent
    # no My Design, no copy, no link: favorites live beside the designs
    assert (await H.Client.get("/api/designs", headers=Me)).json()["designs"] == []
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM designs")["n"] == 1
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM gallery_uses")["n"] == 0
    # each account sees its own list only
    assert (await H.Client.get("/api/favorites")).json()["items"] == []
    # newest first when a second design is saved
    B2 = await H.NewDesign("A plain polished band")
    await H.Client.put(f"/api/designs/{B2['design_id']}/selection", json={"candidate_id": B2["candidates"][1]["id"]})
    I2 = (await H.Client.post("/api/admin/gallery", json={"design_id": B2["design_id"]}, headers=Admin)).json()
    Fav = (await H.Client.put(f"/api/favorites/{B2['design_id']}", headers=Me)).json()["items"]
    assert [F["design_id"] for F in Fav] == [B2["design_id"], Did]
    # Make it yours on a favorite still links to My Designs as usual — the same master — and the favorite stays
    R = await H.Client.post(f"/api/gallery/{Item['id']}/start", json={}, headers=Me)
    assert R.status_code == 200 and R.json()["id"] == Did
    assert [D["id"] for D in (await H.Client.get("/api/designs", headers=Me)).json()["designs"]] == [Did]
    assert len((await H.Client.get("/api/favorites", headers=Me)).json()["items"]) == 2
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM designs WHERE source_design_id IS NOT NULL")["n"] == 0   # never a copy
    # only gallery masters can be favorites
    B3 = await H.NewDesign("Not in the gallery")
    assert (await H.Client.put(f"/api/favorites/{B3['design_id']}", headers=Me)).status_code == 404
    assert (await H.Client.put("/api/favorites/dsg_nope", headers=Me)).status_code == 404
    # removed from the gallery → hidden from favorites; back in the gallery → back in favorites (same master)
    await H.Client.delete(f"/api/admin/gallery/{I2['id']}", headers=Admin)
    assert [F["design_id"] for F in (await H.Client.get("/api/favorites", headers=Me)).json()["items"]] == [Did]
    await H.Client.post("/api/admin/gallery", json={"design_id": B2["design_id"]}, headers=Admin)
    assert [F["design_id"] for F in (await H.Client.get("/api/favorites", headers=Me)).json()["items"]] == [B2["design_id"], Did]
    # Admin sees how many customers saved each design
    L = (await H.Client.get("/api/admin/gallery", headers=Admin)).json()["items"]
    assert {X["design_id"]: X["favorites"] for X in L} == {Did: 1, B2["design_id"]: 1}
    # the heart again removes it
    R = await H.Client.delete(f"/api/favorites/{Did}", headers=Me)
    assert [F["design_id"] for F in R.json()["items"]] == [B2["design_id"]]
    assert (await H.Client.delete(f"/api/favorites/{Did}", headers=Me)).status_code == 200     # already gone: fine


async def test_favorites_survive_a_new_sign_in_on_another_device(HG):
    """Account-based, not browser-based: a new sign-in for the same account (another token, another client with
    nothing stored) sees the same list."""
    import httpx
    H = HG
    Did, _, _ = await _Curated(H)
    T1, Who = H.Ctx.Accounts.IssueToken("shopper")
    assert (await H.Client.put(f"/api/favorites/{Did}", headers={"X-Access-Token": T1})).status_code == 200
    T2, Who2 = H.Ctx.Accounts.IssueToken("shopper, phone", AccountId=Who.AccountId)
    assert T2 != T1 and Who2.AccountId == Who.AccountId
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=H.App), base_url="http://p3.test" + H.Base) as Phone:
        Items = (await Phone.get("/api/favorites", headers={"X-Access-Token": T2})).json()["items"]
    assert [F["design_id"] for F in Items] == [Did]


async def test_my_designs_and_favorites_are_one_tap_away_on_every_screen(HG):
    """The header (next to the bag), the phone menu, the account panel and the Design screen's toolbar all open the
    side panel on the right tab; the account is in the bar on phones too; below 1024 px the panel is a drawer above
    the sticky header, so its My Designs | Favorites tabs and its close button are never covered."""
    H = HG
    Index = (await H.Client.get("/")).text
    Start = Index.index('aria-label="Main"')
    Nav = Index[Start:Index.index("</nav>", Start)]
    Right = Nav[Nav.index("<!-- My Designs (signed in)"):Nav.index("<!-- Mobile menu panel -->")]
    # the header: My Designs when signed in, ♥ Favorites (with the count) always, both next to the bag
    assert Right.index("openPanel('designs')") < Right.index("openPanel('favorites')") < Right.index("goToCheckout()")
    assert 'x-show="userSession && !inStudio"' in Right and "favorites.length" in Right
    # the account is in the bar on phones too (outside the studio, where the steps take the bar)
    Acct = Right[Right.index('@click="toggleAccountPanel()"'):]
    assert ":class=\"inStudio ? 'hidden md:flex' : 'flex'\"" in Acct[:400]
    assert "hidden md:flex items-center gap-1 select-none" not in Nav
    # the phone menu
    Menu = Nav[Nav.index('id="mobile-menu"'):]
    assert "openPanel('designs')" in Menu and "openPanel('favorites')" in Menu
    # the Design screen's toolbar: My Designs and ♥ Favorites side by side while the panel is closed
    Bar = Index[Index.index('data-testid="studio-panel-buttons"'):]
    Bar = Bar[:Bar.index("</div>")]
    assert 'x-show="!sidebarOpen"' in Index[Index.index('data-testid="studio-panel-buttons"') - 120:][:200]
    assert "openPanel('designs')" in Bar and "openPanel('favorites')" in Bar and ">Favorites</span>" in Bar
    # phones and tablets: the drawer and its backdrop are above the sticky header (z-50)
    assert "sticky top-0 z-50" in Index[Index.rindex("<nav", 0, Start):Start]
    assert 'class="fixed inset-0 bg-black/40 z-[55] lg:hidden"' in Index
    assert "'fixed lg:relative inset-y-0 right-0 z-[60] lg:z-auto" in Index
    # the account panel
    Panel = Index[:Index.index('@click="signOutFromAccount()"')][-900:]
    assert "openPanel('designs')" in Panel and "openPanel('favorites')" in Panel
    # signed out: sign in first, then the panel opens on the tab they asked for
    App = (await H.Client.get("/static/app.js")).text
    Open = App[App.index("openPanel(tab = 'designs') {"):]
    Open = Open[:Open.index("\n    },")]
    assert "this._pendingPanel = tab; this.openRegModal(); return;" in Open
    assert "this.sidebarTab = tab === 'favorites' ? 'favorites' : 'designs';" in Open and "this.sidebarOpen = true;" in Open
    After = App[App.index("async _afterSignIn(fromVerification) {"):]
    assert "if (panel) this.openPanel(panel);" in After[:After.index("\n    },")]
    assert "this._pendingPanel = '';" in App[App.index("closeRegModal() {"):][:300]
