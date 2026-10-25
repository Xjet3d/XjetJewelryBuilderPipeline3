"""Inspiration Gallery — every card and the View lightbox say Ring or Charm (one badge, the same for both products,
shown only while products are offered) and show three metal swatches: Stainless Steel, Silver and Gold. The swatches
are a visual preview through the existing metal filters — no request, no AI, and the design, the customer's material
and every price stay as they are. The homepage showcase is unchanged."""

import hashlib
import re
from pathlib import Path

import pytest

from tests.conftest import Harness
from tests.test_charm_foundation import CharmDesign

AdminKey = "gallery-products-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
HeroSha256 = "408d153d4db63cce1fba6befe9bada99a6f553eab5cb3e19acc9e3e16bc0078d"      # the homepage showcase since f7723c2; 2026-10-07: the hero line names the products on offer;
#   2026-10-08: the line under Start Designing follows the products customers can design (heroCollection)
#   2026-10-09: "Bespoke Jewelry" (never "Jewellery"); the mock-mode badges are marked <!--mock--> (sent only in mock mode)
#   2026-10-09: phones — a smaller headline, the eyebrow on one line, tighter spacing (hero-* classes; desktop unchanged)
#   2026-10-12: the story's stage carries sc-stage from the first paint, so its room is kept before the scripts start (CLS)
#   2026-10-17: the stage names its story (data-sc-design) and the story opens on its finished ring (startAt 'final': LCP)
#   2026-10-20: design review — "by You" kept together, the type scale, the room under a header of either height (--hdr),
#               the two buttons stacked where they do not fit side by side (768–1279 px)
Metals = [("stainless_steel", "Stainless Steel"), ("silver", "Silver"), ("gold_18k_yellow", "Gold")]
Web = Path(__file__).resolve().parent.parent / "web"


@pytest.fixture
async def HG(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    yield Obj
    await Obj.Close()


def CardBlocks(Page: str) -> list[str]:
    """Each card template: the homepage strip, the Gallery page and the Gallery dialog."""
    Out = []
    for Loop in ('x-for="g in homeGallery"', 'x-for="g in galleryShown"'):
        for M in re.finditer(re.escape(Loop), Page):
            Fav = Page.index('@click.stop="toggleFav(g)"', M.start())
            Out.append(Page[M.start():Page.index("</template>", Fav)])
    return Out


async def test_every_gallery_card_says_ring_or_charm_and_has_three_metal_swatches(HG):
    Page = (await HG.Client.get("/")).text
    Cards = list(dict.fromkeys(CardBlocks(Page)))              # the home page is served as markup and as its template
    assert len(Cards) == 3                                     # homepage strip, Gallery page, Gallery dialog
    for Card in Cards:
        # One badge for both products — no charm-only exception — shown only while products are offered
        assert Card.count('class="product-badge') == 1 and '<span x-show="productsOn" x-cloak class="product-badge' in Card
        assert "productLabel(g.product_type || 'ring')" in Card and "productIcon(g.product_type || 'ring'" in Card
        assert "isCharm(g)" not in Card
        # Three swatches in this order, named for screen readers and on hover, with no visible text
        assert re.findall(r'aria-label="Preview in ([^"]+)"', Card) == [L for _, L in Metals]
        assert re.findall(r'title="Preview in ([^"]+)"', Card) == [L for _, L in Metals]
        assert re.findall(r"pickCardMetal\(g, '([a-z0-9_]+)'\)", Card) == [I for I, _ in Metals]
        assert "metalFilter(cardMetal[g.id])" in Card              # the card image takes the chosen metal's filter
        assert "<template" not in Card.split(">", 1)[1]            # no template nested in a card (Alpine 3.13 cleanup)
    # The View lightbox: the same badge and the same three swatches, plus Original
    Box = Page[Page.index('<template x-if="galleryItem">'):Page.index('<template x-if="previewOpen">')]
    assert Box.count('class="product-badge') == 1 and "productLabel(galleryItem.product_type || 'ring')" in Box
    assert re.findall(r'aria-label="Preview in ([^"]+)"', Box) == [L for _, L in Metals]
    assert re.findall(r"setGalleryMetal\('([a-z0-9_]*)'\)", Box) == ["", *[I for I, _ in Metals]]
    assert "metalFilter(galleryMetal)" in Box and "<template" not in Box.split(">", 1)[1]
    # The design review (2026-10-20): each swatch a 44 × 44 button holding its dot (16 px; 40 × 44 and 14 px on a narrow
    # card, so three fit beside the arrow), the card's name in full, nothing on the picture — the badge and the heart in
    # the card's top row, the swatches below the name; the whole card is the link (its ::after), under the buttons
    assert (".metal-swatch { position: relative; display: inline-flex; align-items: center; justify-content: center; "
            "width: 44px; height: 44px; flex: none;") in Page
    assert "@media (max-width: 639.98px) { .g-name { font-size: 15px; } .g-card .metal-swatch { width: 40px; } .g-card .metal-swatch > span { width: 14px; height: 14px; } }" in Page   # 15 px: board J (R-36)
    # by the viewport, never container queries: those were re-evaluated on every frame of the home page's story (TBT)
    assert "@container" not in Page and "container-type" not in Page
    assert ".g-card .metal-swatch { flex: 0 1 auto; min-width: 32px; }" in Page and 'class="g-grid-dialog grid' in Page
    assert ".g-card .g-link::after { content: ''; position: absolute; inset: 0; z-index: 1;" in Page
    for Card in Cards:
        assert Card.count('class="g-card group"') == 1 and "truncate" not in Card and "absolute" not in Card
        assert '<span class="g-name block px-2.5 pt-2" x-text="g.title"></span>' in Card
        Link = Card.index("data-gallery-tile")
        assert Card.index('class="product-badge') < Link and Card.index("toggleFav(g)") < Link < Card.index("pickCardMetal(g, ")
        assert Card.count("<span :style=\"'background:' + metalSwatch(") == 3 and ":style=\"'background:' + metalSwatch" not in Card.replace("<span :style=\"'background:' + metalSwatch(", "")
        assert "fav-heart is-plain ml-auto w-11 h-11" in Card                           # a 44 px heart
    assert "metal-swatch metal-swatch-lg" in Box and ".metal-swatch.metal-swatch-lg > span { width: 26px; height: 26px; }" in Page


async def test_the_metal_preview_is_visual_only(HG):
    """The swatches only change what the page shows: no request carries the chosen metal, so the master design, the
    customer's saved material and every price stay as they are. Gold is a look, not a choice of karat."""
    App = (await HG.Client.get("/static/app.js")).text
    Start = App.index("    galleryMetalLabel(id)")
    Helpers = App[Start:App.index("\n    },", App.index("    setGalleryMetal(id) {", Start)) + 7]
    assert "pickCardMetal(g, id)" in Helpers and "this.galleryMetal = id;" in Helpers
    for Call in ("api(", "fetch(", "sendBeacon", "material_id", "Customiz", "quote"):
        assert Call not in Helpers, Call
    Lines = [L for L in App.splitlines() if "cardMetal" in L or re.search(r"galleryMetal\b", L)]
    assert len(Lines) == 6 and not any("api(" in L or "fetch(" in L for L in Lines)
    assert "this.galleryMetal = this.cardMetal[g.id] || ''" in App                  # View opens in the card's metal
    assert "galleryMetalLabel(id) { return id === 'gold_18k_yellow' ? 'Gold' :" in App
    # Make it yours sends nothing but its request id: the preview metal never reaches the new design
    assert "this.api('POST', `/api/gallery/${encodeURIComponent(id)}/start`, { client_request_id: newRequestId() })" in App


async def test_ring_and_charm_gallery_items_both_open_as_their_product(HG):
    H = HG
    Ring = await H.NewDesign()
    await H.Client.put(f"/api/designs/{Ring['design_id']}/selection", json={"candidate_id": Ring["candidates"][0]["id"]})
    RingItem = (await H.Client.post("/api/admin/gallery", json={"design_id": Ring["design_id"]}, headers=Admin)).json()
    Asset = Ring["candidates"][0]["image_url"].split("/assets/", 1)[1]
    Cid, _ = CharmDesign(H, Asset, "Lune Drop")
    CharmItem = (await H.Client.post("/api/admin/gallery", json={"design_id": Cid}, headers=Admin)).json()
    H.Ctx.Products.Set("charms_available", True, "test")
    Customer = {"X-Access-Token": H.Ctx.Accounts.IssueToken("customer")[0]}
    Tiles = (await H.Client.get("/api/gallery", headers=Customer)).json()["items"]
    assert [(T["id"], T["product_type"]) for T in Tiles] == [(RingItem["id"], "ring"), (CharmItem["id"], "charm")]
    for Item, Product in ((RingItem, "ring"), (CharmItem, "charm")):
        R = await H.Client.post(f"/api/gallery/{Item['id']}/start", json={}, headers=Customer)
        assert R.status_code == 200 and R.json()["product_type"] == Product, R.text
    # Charms hidden again: the charm tile and every product field are gone; the ring tile keeps working
    H.Ctx.Products.Set("charms_available", False, "test")
    Tiles = (await H.Client.get("/api/gallery", headers=Customer)).json()["items"]
    assert [T["id"] for T in Tiles] == [RingItem["id"]] and set(Tiles[0]) == {"id", "design_id", "title", "image_url", "slug"}
    assert (await H.Client.post(f"/api/gallery/{RingItem['id']}/start", json={}, headers=Customer)).status_code == 200
    R = await H.Client.post(f"/api/gallery/{CharmItem['id']}/start", json={}, headers=Customer)
    assert R.status_code == 409 and R.json()["error"]["code"] == "design_unavailable"   # launched, then paused: viewable, not made
    # The All | Rings | Charms filter (the Gallery page and dialog) is shown only while products are offered and
    # filters by product; with charms hidden the catalog has no products, so badges and filter stay away
    Page = (await H.Client.get("/")).text
    for Gap in ("mb-8", "mb-6"):
        assert f'<div x-show="productsOn" x-cloak class="flex justify-center {Gap}" role="group" aria-label="Show designs">' in Page
    assert Page.count('x-for="f in galleryFilters"') == 2                       # All + one entry per product on offer
    App = (await H.Client.get("/static/app.js")).text
    assert "get productsOn() { return this.productsList.length > 1; }" in App
    assert "get galleryFilters() { return [['', 'All'], ...this.productsList.map(p => [p, this.productPlural(p)])]; }" in App
    assert "const f = this.productsOn && this.productsList.includes(this.galleryProduct) ? this.galleryProduct : '';" in App
    assert "products" not in (await H.Client.get("/api/catalog", headers=Customer)).json()


async def test_the_full_gallery_dialog_starts_at_the_top_when_it_is_taller_than_the_screen(HG):
    """With 14 designs the dialog is taller than a laptop screen; a centred flex child that overflows has its top cut
    off (the heading was above the screen). The panel centres itself while it fits and starts at the top otherwise."""
    Page = (await HG.Client.get("/")).text
    Start = Page.index('<template x-if="galleryGridOpen">')
    Dlg = Page[Start:Page.index('x-for="g in galleryShown"', Start)]
    assert 'class="fixed inset-0 z-[105] bg-black/60 flex items-start justify-center p-4 overflow-y-auto"' in Dlg
    assert "md:items-center" not in Dlg and "shadow-2xl p-6 md:p-8 my-auto fade-in" in Dlg


async def test_the_homepage_showcase_is_unchanged(HG):
    Source = (Web / "index.html").read_text(encoding="utf-8")
    S = Source.index('<template x-if="view === \'home\'">')
    Hero = Source[S:Source.index('<section class="py-24 px-8 md:px-20 bg-white">', S)]
    assert hashlib.sha256(Hero.encode("utf-8")).hexdigest() == HeroSha256
    assert "metal-swatch" not in Hero and "product-badge" not in Hero
    Page = (await HG.Client.get("/")).text
    assert 'data-testid="hero-showcase"' in Page
