"""WP4 — charms first: the products come in the order charms, rings everywhere (the catalog, the Design screen's choice,
the Gallery's filters, titles and generated sentences), Design opens on Charm, and a design that names no product is a
charm while charms are on offer. Every Design entry names its product — Start Designing the default (Charm), an entry of
its own its product (Design a Ring: Ring) — and keeps it through sign-in. Rings stay on, one tap away. The Terms keep
their product sentences in their first order, rings then charms (M7): their text and version stay the same."""

import re
from pathlib import Path

import httpx

from tests.conftest import Harness

AdminKey = "charms-first-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Web = Path(__file__).resolve().parent.parent / "web"


async def _Set(H, Product, On):
    R = await H.Client.put("/api/admin/products/availability", json={"product": Product, "available": On}, headers=Admin)
    assert R.status_code == 200, R.text


async def test_charms_come_first_and_a_design_that_names_no_product_is_a_charm(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _Set(H, "charm", True)
        Cat = (await H.Client.get("/api/catalog")).json()["products"]
        assert Cat["available"] == ["charm", "ring"] and Cat["default"] == "charm" and list(Cat["names"]) == ["charm", "ring"]
        assert [P["id"] for P in (await H.Client.get("/api/admin/products", headers=Admin)).json()["products"]] == ["charm", "ring"]
        # No product named: a charm; a ring when the request names it (Ring is one tap away)
        B = await H.NewDesign("A crescent moon")
        assert (await H.Design(B["design_id"]))["product_type"] == "charm"
        B = await H.NewDesign("A slim twisted band", product="ring")
        assert (await H.Design(B["design_id"]))["product_type"] == "ring"
        # The site's own words put charms first: the line under Start Designing, the description, titles, the FAQ, Materials
        Home = (await H.Client.get("/")).text
        assert 'x-text="heroCollection.label">Charms &amp; Rings</span>' in Home
        assert 'content="Design custom charms and rings with AI' in Home
        assert "<title>Inspiration Gallery — Charms &amp; Rings Designed with AI · XJet Atelier</title>" in (await H.Client.get("/gallery")).text
        Faq = (await H.Client.get("/faq")).text
        assert "Charms and Rings: choose one when you start a design." in Faq
        assert "A charm is priced by material and size. A ring is priced by material — its size does not change the price." in Faq
        assert "How do sizes work?" in Faq and "A charm size is its total height in millimeters, including the attachment eyelet at the top." in Faq   # B8 (11 Nov 2026)
        assert "a charm is priced by material and size; a ring’s price does not depend on its size." in (await H.Client.get("/materials")).text
        assert "A charm size is its total height in millimeters" in (await H.Client.get("/contact")).text.split("Not sure about your ring size?")[0]
        # Rings alone: the ring-only site of before, a ring by default
        await _Set(H, "charm", False)
        assert "products" not in (await H.Client.get("/api/catalog")).json()
        B = await H.NewDesign("A signet with a hexagon face")
        assert H.Ctx.Db.One("SELECT product_type FROM designs WHERE id = ?", (B["design_id"],))["product_type"] == "ring"
    finally:
        await H.Close()


async def test_an_admins_preview_never_becomes_the_default(tmp_path):
    """A product only an Admin's browser sees (hidden from customers) comes after the ones customers can design: with
    charms hidden, the Admin's Design screen still opens on Ring (a charm there needs a test account)."""
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=H.App), base_url="http://p3.test") as Browser:
            assert (await Browser.post("/api/admin/login", json={"key": AdminKey})).status_code == 200
            Browser.headers["X-P3-Preview"] = "1"                 # opened the site in preview (5 Nov 2026)
            Cat = (await Browser.get("/api/catalog")).json()["products"]
            assert Cat["available"] == ["charm", "ring"] and Cat["previewed"] == ["charm"] and Cat["default"] == "ring"
            await _Set(H, "charm", True)
            await _Set(H, "ring", False)                                         # rings paused: charms carry the site
            Cat = (await Browser.get("/api/catalog")).json()["products"]
            assert Cat["available"] == ["charm", "ring"] and Cat["previewed"] == ["ring"] and Cat["default"] == "charm"
    finally:
        await H.Close()


async def test_the_terms_keep_their_product_order_and_their_version(tmp_path):
    """M7: the Terms name rings, then charms, as customers accepted them; charms first elsewhere changes neither their
    words nor their version."""
    H = Harness(tmp_path, AuthMode="dual", AdminKey=AdminKey)               # the site as Atelier runs it
    try:
        Before = (await H.Client.get("/api/auth/terms")).json()
        assert Before["terms"]["version"] == 1 and Before["terms"]["sha256"]
        await _Set(H, "charm", True)
        Page = (await H.Client.get("/terms")).text
        assert "upload an idea for a ring or a charm" in Page and "The current release supports rings and charms." in Page
        assert (await H.Client.get("/api/auth/terms")).json() == Before
        assert len(H.Ctx.Accounts.Db.All("SELECT 1 FROM legal_documents")) == 2              # nothing new archived
        Terms = H.Ctx.Accounts.Db.One("SELECT text FROM legal_documents WHERE doc = 'terms'")["text"]
        assert Terms.index("[rings and charms on offer]") < Terms.index("[rings on offer]") < Terms.index("[charms on offer]")
        assert "[charms and rings on offer]" not in Terms and "an idea for a charm or a ring" not in Terms
        # The page computes the same sentence, rings first (web/app.js termsService)
        App = (Web / "app.js").read_text(encoding="utf-8")
        assert "const list = ['ring', 'charm'].filter(p => this.productsList.includes(p));" in App[App.index("get termsService()"):App.index("get termsOrders()")]
    finally:
        await H.Close()


def test_every_design_entry_names_its_product_and_keeps_it_through_sign_in():
    App = (Web / "app.js").read_text(encoding="utf-8")
    Index = (Web / "index.html").read_text(encoding="utf-8")
    # The entry chooses the product before anything else — before the sign-in it may need — and the choice is stored,
    # so a sign-in by email, by Google or by Apple (a new page load) comes back to it
    Start = App[App.index("    resetAIFlow(product) {"):App.index("    _doResetAIFlow(")]
    assert "this.chooseProduct(typeof product === 'string' ? product : this.defaultProduct);" in Start
    assert Start.index("this.chooseProduct(") < Start.index("this.openRegModal()") < Start.index("this._doResetAIFlow()")
    Choose = App[App.index("    chooseProduct(p) {"):App.index("    get newNoun()")]
    assert "this.newProduct = p; this.persist({ newProduct: p });" in Choose
    assert "this.newProduct = this.productsList.includes(st.newProduct) ? st.newProduct : this.defaultProduct;" in App
    assert "get defaultProduct() { return this.catalog?.products?.default || this.productsList[0] || 'ring'; }" in App
    assert "newProduct: 'charm'," in App
    # Every Start Designing opens on the default product: called with no argument (never an event taken for a product);
    # only the homepage's Design a Charm / Design a Ring (WP6) name a product, and their labels say which
    Calls = re.findall(r'@click="resetAIFlow\(([^)]*)\)"', Index)
    assert Calls.count("") >= 7 and set(Calls) == {"", "'charm'", "'ring'"}, Calls
    Named = re.findall(r"""@click="resetAIFlow\('(charm|ring)'\)"[^>]*>([^<]+)<""", Index)
    assert len(Named) == 4 and all(Label == "Design a " + P.title() for P, Label in Named), Named
    assert not re.search(r'@click="resetAIFlow"', Index)
    # The Admin lists charms first too
    Admin_ = (Web / "admin.html").read_text(encoding="utf-8")
    assert "['ring', 'charm']" not in Admin_ and Admin_.count("x-for=\"p in ['charm', 'ring']\"") == 5
    assert "productFilters: ['', 'charm', 'ring']," in (Web / "admin.js").read_text(encoding="utf-8")
