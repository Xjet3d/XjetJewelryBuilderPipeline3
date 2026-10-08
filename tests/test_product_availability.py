"""Customer availability per product (Admin → Settings → Products): rings and charms each have their own switch.
A product that is OFF is not offered, not in the gallery or the sitemap, not in the page's wording, and a design of
it is refused; the Admin configures everything regardless and previews hidden products on the site."""

import httpx

from tests.conftest import Harness

AdminKey = "availability-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}


async def _Set(H, Product, On):
    R = await H.Client.put("/api/admin/products/availability", json={"product": Product, "available": On}, headers=Admin)
    assert R.status_code == 200, R.text
    return R.json()


async def test_rings_can_be_switched_off_and_charms_carry_the_site(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        St = (await H.Client.get("/api/admin/products", headers=Admin)).json()
        assert St["availability"] == {"ring": True, "charm": False} and [P["id"] for P in St["products"]] == ["ring", "charm"]
        assert St["products"][0]["available"] and St["products"][0]["configuration_ready"] and St["products"][0]["plural"] == "Rings"
        assert "products" not in (await H.Client.get("/api/catalog")).json()                 # the ring-only site of before
        assert 'content="Design custom rings with AI' in (await H.Client.get("/")).text
        # Charms on, rings off: the catalog offers charms alone, a charm is the default, and a ring cannot be started
        await _Set(H, "charm", True)
        assert 'content="Design custom rings and charms with AI' in (await H.Client.get("/")).text
        St = await _Set(H, "ring", False)
        assert St["availability"] == {"ring": False, "charm": True}
        assert St["log"][0]["key"] == "rings_available" and St["log"][0]["value"] is False and St["products"][0]["changed"]["updated_by"]
        Cat = (await H.Client.get("/api/catalog")).json()
        assert Cat["products"]["available"] == ["charm"] and Cat["products"]["default"] == "charm"
        assert Cat["products"]["preview"] is False and Cat["products"]["previewed"] == []
        assert Cat["products"]["names"]["charm"] == {"label": "Charm", "plural": "Charms"}
        assert all(isinstance(M["prices"], list) for M in Cat["products"]["charm"]["materials"])
        R = await H.Client.post("/api/designs", data={"prompt": "A slim band", "product": "ring"})
        assert R.status_code == 400 and R.json()["error"]["code"] == "unknown_product"
        B = await H.NewDesign("A heart charm")                                               # no product named: the one on offer
        assert (await H.Design(B["design_id"]))["product_type"] == "charm"
        assert (await H.Client.get("/api/designs")).json()["designs"][0]["product_type"] == "charm"   # product fields stay
        assert 'content="Design custom charms with AI' in (await H.Client.get("/")).text
        # Everything off: nothing to design, the start routes refuse, the lists are empty
        St = await _Set(H, "charm", False)
        assert St["availability"] == {"ring": False, "charm": False}
        Cat = (await H.Client.get("/api/catalog")).json()
        assert Cat["products"]["available"] == [] and Cat["products"]["default"] == "ring"
        assert (await H.Client.post("/api/designs", data={"prompt": "A slim band"})).status_code == 400
        assert (await H.Client.get("/api/designs")).json()["designs"] == []
        assert 'content="Design custom jewelry with AI' in (await H.Client.get("/")).text
        # Back to rings only: the ring-only answers of before; the charm design stays hidden
        await _Set(H, "ring", True)
        assert "products" not in (await H.Client.get("/api/catalog")).json()
        assert (await H.Client.get(f"/api/designs/{B['design_id']}")).status_code == 404
        # The older body still switches charms; bad values and customers are refused
        R = await H.Client.put("/api/admin/products/availability", json={"charms_available": True}, headers=Admin)
        assert R.json()["charms_available"] is True and R.json()["availability"]["charm"] is True
        assert (await H.Client.put("/api/admin/products/availability", json={"product": "ring", "available": "no"}, headers=Admin)).status_code == 400
        assert (await H.Client.put("/api/admin/products/availability", json={"product": "ring", "available": False})).status_code == 403
    finally:
        await H.Close()


async def test_the_gallery_the_sitemap_and_the_admin_preview_follow_availability(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _Set(H, "charm", True)
        Ring = await H.NewDesign("A slim band", product="ring")
        Charm = await H.NewDesign("A heart charm", product="charm")
        Items = {}
        for Name, B in (("ring", Ring), ("charm", Charm)):
            await H.Client.put(f"/api/designs/{B['design_id']}/selection", json={"candidate_id": B["candidates"][0]["id"]})
            R = await H.Client.post("/api/admin/gallery", json={"design_id": B["design_id"]}, headers=Admin)
            assert R.status_code == 200, R.text
            Items[Name] = R.json()["id"]
        Tiles = (await H.Client.get("/api/gallery")).json()["items"]
        assert sorted(T["product_type"] for T in Tiles) == ["charm", "ring"]
        Slugs = {N: (await H.Client.get(f"/api/gallery/{I}/share")).json()["slug"] for N, I in Items.items()}
        Sm = (await H.Client.get("/sitemap.xml")).text
        assert f"/design/{Slugs['ring']}" in Sm and f"/design/{Slugs['charm']}" in Sm
        # Rings off: the ring tile, its share link, its start and its sitemap entry are gone; the charm's stay
        await _Set(H, "ring", False)
        Tiles = (await H.Client.get("/api/gallery")).json()["items"]
        assert len(Tiles) == 1 and "product_type" not in Tiles[0]                            # one product: no label needed
        assert (await H.Client.get(f"/api/gallery/{Items['ring']}/share")).status_code == 404
        assert (await H.Client.post(f"/api/gallery/{Items['ring']}/start", json={})).status_code in (400, 404)
        assert (await H.Client.post(f"/api/gallery/{Items['charm']}/start", json={})).status_code == 200
        Sm = (await H.Client.get("/sitemap.xml")).text
        assert f"/design/{Slugs['ring']}" not in Sm and f"/design/{Slugs['charm']}" in Sm
        assert (await H.Client.get(f"/design/{Slugs['ring']}")).status_code == 404
        # A browser signed in to the Admin still sees rings on the site — as a preview, marked as such
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=H.App), base_url="http://p3.test") as Browser:
            assert (await Browser.post("/api/admin/login", json={"key": AdminKey})).status_code == 200
            Cat = (await Browser.get("/api/catalog")).json()
            assert Cat["products"]["available"] == ["ring", "charm"] and Cat["products"]["previewed"] == ["ring"] and Cat["products"]["preview"] is True
            assert len((await Browser.get("/api/gallery")).json()["items"]) == 2
            assert (await Browser.get(f"/api/gallery/{Items['ring']}/share")).status_code == 200   # (the Admin cookie reaches the API only)
    finally:
        await H.Close()


async def test_the_line_under_start_designing_follows_the_products_customers_can_design(tmp_path):
    """Rings only: "Rings — the first XJet Atelier collection, …"; Charms only: "Charms — …"; both: "Rings & Charms —
    XJet Atelier collections, …". Computed on the page from the catalog's products (the availability switches), never
    from an Admin's preview; the static text is the rings line for a page read before its script runs."""
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Index = (await H.Client.get("/")).text
        Bound = ('<span class="font-semibold text-zinc-800" x-text="heroCollection.label">Rings</span><span '
                 'x-text="heroCollection.text"> — the first XJet Atelier collection, made to order in real metal.</span>')
        assert Index.count(Bound) == 2 and Index.count('x-show="heroCollection.label"') == 2     # desktop and phone
        App = (await H.Client.get("/static/app.js")).text
        Getter = App[App.index("get heroCollection()"):App.index("chooseProduct(p)")]
        assert "this.productsList.filter(p => !this.previewedProducts.includes(p))" in Getter    # customers' products only
        assert "' — the first XJet Atelier collection, made to order in real metal.'" in Getter
        assert "' — XJet Atelier collections, made to order in real metal.'" in Getter and "' & '" in Getter
        # The products it reads are the switches: the catalog lists what customers can design
        assert "products" not in (await H.Client.get("/api/catalog")).json()                    # rings only → ['ring']
        await _Set(H, "charm", True)
        assert (await H.Client.get("/api/catalog")).json()["products"]["available"] == ["ring", "charm"]
        await _Set(H, "ring", False)
        assert (await H.Client.get("/api/catalog")).json()["products"]["available"] == ["charm"]
    finally:
        await H.Close()
