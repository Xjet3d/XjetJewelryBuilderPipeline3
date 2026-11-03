"""Launch UX fixes (3 Nov 2026, Open Tasks rows 14 and 25): the consent card is a compact sheet on phones (the same words
and buttons, in the same order) that leaves the hero's Start Design in view; text laid out before the web fonts arrive
takes the room it will take (fallback faces sized like Inter and Cinzel); and the parts of a page the server knows the app
starts with keep their room from the first paint — the product chips and the Gallery's filter, the designs at the foot
of the info pages, the Materials intro's gold sentence."""

import json
import re
from pathlib import Path

from tests.conftest import Harness

Web = Path(__file__).resolve().parent.parent / "web"
AdminKey = "launch-ux-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}


def _Live(Page: str, View: str) -> str:
    """The page's own markup as served (its live copy, outside any <template>)."""
    I = Page.index(f'data-ssr-view="{View}"')
    Kept = Page.index(f" && ssrView !== '{View}'\">", I)
    return Page[I:Page.rindex("<template", I, Kept)]


def _Config(Page: str) -> dict:
    return json.loads(re.search(r"<script>window.__p3 = (\{.*?\});</script>", Page, re.S).group(1))


async def _Publish(H, Prompt: str, Product: str = "ring") -> None:
    B = await H.NewDesign(Prompt, product=Product)
    await H.Client.put(f"/api/designs/{B['design_id']}/selection", json={"candidate_id": B["candidates"][0]["id"]})
    R = await H.Client.post("/api/admin/gallery", json={"design_id": B["design_id"]}, headers=Admin)
    assert R.status_code == 200, R.text


def test_the_fallback_faces_are_sized_like_the_web_fonts_and_follow_them_in_every_stack():
    Fonts = (Web / "vendor/fonts.css").read_text(encoding="utf-8")
    for Rule in ("font-family: 'Inter Fallback';\n  src: local('Arial'), local('ArialMT');\n  size-adjust: 106.77%;\n  ascent-override: 90.76%;\n"
                 "  descent-override: 22.57%;\n  line-gap-override: 0%;",
                 "font-family: 'Cinzel Fallback';\n  src: local('Georgia');\n  size-adjust: 112%;\n  ascent-override: 87.14%;\n"
                 "  descent-override: 33.21%;\n  line-gap-override: 0%;"):
        assert Rule in Fonts
    Index = (Web / "index.html").read_text(encoding="utf-8")
    assert "body { font-family: 'Inter', 'Inter Fallback', sans-serif;" in Index
    assert "h1, h2, h3, .brand-font { font-family: 'Cinzel', 'Cinzel Fallback', serif; }" in Index
    assert ".hv-display { font-family: 'Cinzel', 'Cinzel Fallback', Georgia, serif;" in Index
    # no stack names a web font without its fallback (the customer pages, the consent card, the story, the 404 page)
    for File in ("index.html", "consent.js", "showcase.css", "quote.html"):
        Text = (Web / File).read_text(encoding="utf-8")
        assert not re.search(r"'Inter',(?!\s*'Inter Fallback')", Text), File
        assert not re.search(r"'Cinzel',(?!\s*'Cinzel Fallback')", Text), File
    Consent = (Web / "consent.js").read_text(encoding="utf-8")
    assert "var SANS = \"Inter,'Inter Fallback'," in Consent and "SERIF = \"Cinzel,'Cinzel Fallback',Georgia," in Consent
    Pages = (Web.parent / "p3/sitepages.py").read_text(encoding="utf-8")
    assert "font-family: 'Inter', 'Inter Fallback', system-ui, sans-serif;" in Pages and "'Cinzel', 'Cinzel Fallback', Georgia, serif" in Pages


def test_the_consent_card_is_a_compact_sheet_on_phones_with_the_same_words_and_buttons():
    Consent = (Web / "consent.js").read_text(encoding="utf-8")
    Phone = Consent[Consent.index("'@media (max-width:640px){',"):Consent.index("'.ac-shade{align-items:flex-end;padding:0}',")]
    assert ".ac-bar{left:8px;right:8px;bottom:calc(8px + env(safe-area-inset-bottom,0px));" in Phone
    assert ".ac-bar .ac-title{display:inline;" in Phone and ".ac-bar p.ac-text{display:inline;margin:0;font-size:12px;line-height:1.45}" in Phone
    assert ".ac-bar .ac-row .ac-btn{min-height:38px;" in Phone and ".ac-bar .ac-quiet{display:block;min-height:30px;" in Phone
    # the words, the buttons and their order are as before (only the layout changed)
    Bar = Consent[Consent.index("bar = el('div', { 'class': 'ac-bar'"):Consent.index("// Early in the keyboard order")]
    assert "'<h2 class=\"ac-title\" id=\"ac-bar-title\">Your privacy matters</h2>' +" in Bar
    assert ("'<p class=\"ac-text\">Atelier uses essential cookies and similar technologies to make the site work. With your permission, we also ' +\n"
            "      'use optional technologies to understand how the site is used and improve your experience. You can change your choices at any ' +\n"
            "      'time. <a href=\"' + privacyUrl + '\">Privacy Policy</a></p>' +") in Bar
    Order = [Bar.index(T) for T in ('data-ac="reject">Reject optional', 'data-ac="accept">Accept all', 'data-ac="manage">Manage preferences')]
    assert Order == sorted(Order)
    # the page keeps the card's height clear as it is now (a resize of the card, not only of the window)
    assert "barSize = new ResizeObserver(clearance); barSize.observe(bar);" in Consent and "barSize.disconnect()" in Consent


async def test_the_product_chips_and_the_gallery_filter_show_from_the_first_paint_while_there_is_a_choice(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        H.Ctx.Products.Set("charms_available", True, "test")
        Home = (await H.Client.get("/")).text
        Live = _Live(Home, "home")
        assert '<div x-show="productsOn" class="hv-chips" role="group" aria-label="Show designs">' in Live   # not cloaked
        Chips = re.findall(r'<button type="button" class="hv-chip" aria-pressed="(true|false)" tabindex="-1" aria-hidden="true" '
                           r'data-ssr-placeholder x-init="\$el.remove\(\)">(\w+)</button>', Live)
        assert Chips == [("true", "Charms"), ("false", "Rings"), ("false", "All")]                 # charms first, as the app starts
        Gallery = (await H.Client.get("/gallery")).text
        Live = _Live(Gallery, "inspiration")
        assert '<div x-show="productsOn" class="flex justify-center mb-8" role="group" aria-label="Show designs">' in Live
        Filters = re.findall(r'<button type="button" aria-pressed="(true|false)" tabindex="-1" aria-hidden="true" class="[^"]*" '
                             r'data-ssr-placeholder x-init="\$el.remove\(\)">(\w+)</button>', Live)
        assert Filters == [("true", "All"), ("false", "Charms"), ("false", "Rings")]
        # the templates the app draws from are unchanged (cloaked until it starts)
        assert '<div x-show="productsOn" x-cloak class="flex justify-center mb-8" role="group" aria-label="Show designs">' in Gallery
        # one product on offer: no choice, so no chips, no filter, nothing shown early
        H.Ctx.Products.Set("charms_available", False, "test")
        Home = (await H.Client.get("/")).text
        assert 'data-ssr-placeholder x-init="$el.remove()">Rings</button>' not in Home
        assert '<div x-show="productsOn" x-cloak class="hv-chips"' in _Live(Home, "home")
    finally:
        await H.Close()


async def test_the_designs_at_the_foot_of_a_page_keep_their_room_until_the_gallery_answers(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        R = (await H.Client.get("/technology")).text                               # no gallery yet: nothing to hold room for
        assert "page_designs" not in _Config(R) and '<section x-cloak x-show="pageDesigns.length || pageDesignsSoon"' in _Live(R, "technology")
        for N in range(5):
            await _Publish(H, f"A slim band number {N}")
        for View, Path_ in (("technology", "/technology"), ("materials", "/materials"), ("designers", "/about"), ("faq", "/faq")):
            Page = (await H.Client.get(Path_)).text
            assert _Config(Page)["page_designs"] == 4, View
            Live = _Live(Page, View)
            assert '<section x-show="pageDesigns.length || pageDesignsSoon" class=' in Live, View        # shown from the first paint
            assert Live.count('<div class="g-card" aria-hidden="true" data-ssr-placeholder x-init="$el.remove()">') == 4, View
        App = (Web / "app.js").read_text(encoding="utf-8")
        assert "return this.galleryState === 'loading' && this.view === P3_PAGE && Number.isInteger(n) && n > 0 ? n : 0;" in App
        Index = (Web / "index.html").read_text(encoding="utf-8")
        assert Index.count('<template x-if="!pageDesigns.length && pageDesignsSoon"><template x-for="i in pageDesignsSoon"') == 4
    finally:
        await H.Close()


async def test_the_materials_intro_keeps_its_gold_sentence_until_the_catalog_answers(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Page = (await H.Client.get("/materials")).text
        Groups = _Config(Page)["page_groups"]
        assert "fashion" in Groups and "luxury" in Groups                           # the default catalog: gold is quoted
        assert "Gold is quoted individually." in _Live(Page, "materials")
        App = (Web / "app.js").read_text(encoding="utf-8")
        assert "const has = g => this.catalog ? this.pageMetals(g).length > 0 : (window.__p3?.page_groups || []).includes(g);" in App
        assert "(has('luxury') ? ' Gold is quoted individually.' : '')" in App
        assert "page_groups" not in _Config((await H.Client.get("/faq")).text)
    finally:
        await H.Close()


async def test_the_heros_collection_line_and_the_contact_address_show_from_the_first_paint(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey, SupportEmail="help@atelier.example.com")
    try:
        Live = _Live((await H.Client.get("/")).text, "home")
        assert '<p x-show="heroCollection.label" class="hv-fine"><span class="font-semibold text-zinc-800" x-text="heroCollection.label">Rings</span>' in Live
        Live = _Live((await H.Client.get("/contact")).text, "contact")
        Link = re.search(r'<a x-show="SUPPORT_EMAIL" [^>]*x-text="SUPPORT_EMAIL">([^<]*)</a>', Live)
        assert Link and Link.group(1) == " "                                    # shown, its line held, not cloaked
        assert "help@atelier.example.com" not in Live                               # the address only in the settings script
        assert '<p x-cloak x-show="!SUPPORT_EMAIL"' in Live                          # its stand-in stays hidden
    finally:
        await H.Close()
