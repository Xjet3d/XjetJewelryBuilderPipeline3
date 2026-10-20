"""The design review of 2026-10-20 (20 findings, approved for proto): one footer at the end of every public page, XJet's
own logo files, the consent card clear of the hero's buttons, About and Technology stating only the figures XJet
publishes, one label for starting a design, a way to the team from Home and the FAQ, the How It Works accessibility fix,
gold that reads on dark, Materials' prices in whole dollars, and the home band's photograph of printed jewelry."""

import re
from pathlib import Path

from PIL import Image

from p3 import sitepages as SitePages
from tests.conftest import Harness

WebDir = Path(__file__).resolve().parents[1] / "web"
Index = (WebDir / "index.html").read_text(encoding="utf-8")
Footer = (WebDir / "partials" / "site-footer.html").read_text(encoding="utf-8")


def _Live(Page: str, View: str) -> str:
    """The page's own markup as served (its live copy, outside any <template>)."""
    I = Page.index(f'data-ssr-view="{View}"')
    Kept = Page.index(f" && ssrView !== '{View}'\">", I)
    return Page[I:Page.rindex("<template", I, Kept)]


def _Public(Html: str) -> str:
    """index.html's public pages: everything before the Design screen."""
    return Html[:Html.index("Design studio — four candidates")]


async def test_every_public_page_ends_with_the_one_site_footer(tmp_path):
    """F4 + F8: every public page, as served, ends with the same footer — the main pages, help, the legal pages, Cookie
    Settings and XJet's site — readable on its dark ground; the home page's own footer and the legal pages' link row
    are gone; the footer's line is in the page for readers that run no scripts."""
    assert _Public(Index).count("{{SITE_FOOTER}}") == 7                                   # one per public view
    H = Harness(tmp_path)
    try:
        for View, Path_ in SitePages.Paths.items():
            Page = (await H.Client.get("/" + Path_)).text
            assert "{{SITE_FOOTER}}" not in Page and "<!-- ── The site's footer" not in Page, View   # the partial's comment stays home
            Live = _Live(Page, View)
            assert Live.count("data-site-footer") == 1, View
            Foot = Live[Live.index("data-site-footer"):]
            for Target in ("/gallery", "/materials", "/#how-it-works", "/technology", "/about", "/faq", "/contact",
                           "/shipping-returns", "/terms", "/privacy"):
                assert f'href="{Target}"' in Foot, (View, Target)
            assert "data-cookie-settings" in Foot and 'href="https://xjet3d.com/"' in Foot, View
            assert re.search(r'src="/static/images/xjet-logo-white\.png\?v=\d+" width="270" height="96" alt="XJet"', Foot), View
            assert "Custom rings designed with AI, made in real metal with NanoParticle Jetting™.</p>" in Foot, View
            assert "© 2026 XJet Ltd. XJet Atelier is a service of XJet Ltd." in Foot, View
        Contact = (await H.Client.get("/contact")).text
        assert "← Home" not in Contact and 'class="mt-12 flex flex-wrap gap-x-6' not in Contact      # the old link row
    finally:
        await H.Close()
    # readable: text #A1A1AA (6.8:1), links #D4D4D8 (12:1), headings in gold #C9A96E (7.9:1), on #18181B
    assert '<footer class="bg-zinc-900 text-[#A1A1AA]' in Footer
    assert ".f-link { display: block; padding: .5rem 0; font-size: 14px; line-height: 1.4; color: #D4D4D8;" in Index
    assert "text-transform: uppercase; color: #C9A96E; }" in Index[Index.index(".f-head {"):]
    assert "text-zinc-400 text-[12px]" not in Index                                      # the old 2.29:1 footer


def test_xjet_is_xjets_own_logo_file():
    """F5: the header and the footer show XJet's logo files (as xjet3d.com serves them: transparent, trimmed to the
    logo, scaled — never redrawn or recoloured), not "XJet" typed in the display face."""
    for Name in ("xjet-logo.png", "xjet-logo-white.png"):
        Im = Image.open(WebDir / "images" / Name)
        assert Im.mode == "RGBA" and Im.size == (270, 96), Name
        assert Im.split()[-1].getbbox() == (0, 0, 270, 96), Name                      # trimmed: the logo fills it
    assert '/static/images/xjet-logo.png" width="270" height="96" alt="XJet"' in Index
    assert "italic brand-font text-zinc-900\">XJet</span>" not in Index and ">XJet <span" not in Index


def test_the_consent_card_sits_bottom_right_clear_of_the_heros_buttons():
    """F1: the first layer is the same card, anchored to the bottom right (over the story panel, never over "Start
    designing" and "How it works"); phones keep their sheet along the bottom."""
    Js = (WebDir / "consent.js").read_text(encoding="utf-8")
    assert "'.ac-bar{position:fixed;right:24px;bottom:24px;" in Js and "left:24px;bottom:24px" not in Js
    assert "'.ac-bar{left:12px;right:12px;bottom:calc(12px + env(safe-area-inset-bottom,0px));width:auto;" in Js


async def test_about_and_technology_say_only_what_xjet_publishes(tmp_path):
    """F12 and "keep claims verifiable": About tells a jewelry buyer who makes the pieces, with XJet's own figures
    (xjet3d.com, 20 Oct 2026: established 2014; 80 patents issued and pending; ISO 9001 and ISO 13485; layers ~8±2
    microns; metal density >98% on its metal systems) — no vague facts, no figure XJet does not state (99.8%)."""
    H = Harness(tmp_path)
    try:
        About = _Live((await H.Client.get("/about")).text, "designers")
        for Fact in (">2014</div>", ">80+</div>", ">~8</div>", "Microns per layer", "ISO 9001", "ISO 13485", "Rehovot, Israel",
                     "The Makers Behind Atelier</h1>"):
            assert Fact in About, Fact
        for Vague in ("#1", "Leading Customers", "Strong Benefits", "disruptive", "maximum automation", "ESG is in our DNA",
                      "<strong"):
            assert Vague not in About, Vague
        Tech = _Live((await H.Client.get("/technology")).text, "technology")
        assert ">~8 µm</div>" in Tech and ">&gt;98%</div>" in Tech and "Ink.JPG" in Tech       # the bottles moved here
    finally:
        await H.Close()
    for Text in (Index, (WebDir.parent / "p3" / "sitepages.py").read_text(encoding="utf-8")):
        assert "99.8" not in Text
    Title, Description = SitePages.Meta("designers", type("C", (), {"Catalog": type("K", (), {"ToJson": staticmethod(lambda: {"groups": []})})}), ("ring",), "")
    assert Title == "About XJet — The Makers Behind Atelier · XJet Atelier" and "80+ patents" in Description


def test_one_label_starts_a_design_and_the_team_is_one_click_away():
    """F9: "Start Designing" starts a new design everywhere (Make it yours only for a gallery design); the secondary
    button has the primary's type. F10: Home's support card and the FAQ's end lead to the Contact page."""
    Public = _Public(Index)
    assert "Start Your Design" not in Public and "Start Designing Now" not in Public
    assert ".btn-outline {" in Index and "font-weight: 600 !important; font-size: 14px !important; letter-spacing: 0.08em !important;" in Index
    Home = Public[Public.index("<template x-if=\"view === 'home'\">"):Public.index("<template x-if=\"view === 'inspiration'\">")]
    Support = Home[Home.index("Expert Design Support"):Home.index("Production &amp; Delivery")]
    assert '<a href="{{BASE}}/contact" @click="pageLink($event, \'contact\')" class="btn-outline mt-6 px-7 py-3">Contact us</a>' in Support
    Faq = Public[Public.index("<template x-if=\"view === 'faq'\">"):Public.index("<template x-if=\"view === 'designers'\">")]
    End = Faq[Faq.index("Still have questions?"):]
    assert End.index(">Contact us</a>") < End.index('class="btn-outline px-10 py-4 transition">Start Designing</button>')


def test_how_it_works_numerals_read_and_its_titles_are_h3():
    """F20 (approved 2026-10-20): the step numerals at 3.1:1 (large text), the step titles h3 under the section's h2
    with the same look; nothing else in How It Works changes."""
    S = Index.index('<section id="how-it-works"')
    How = Index[S:Index.index("</section>", S)]
    assert ".step-numeral { color: #B08D57 !important; }" in Index and ".step-title { font-family: 'Inter', sans-serif; }" in Index
    assert '<h3 class="step-title text-sm font-semibold text-zinc-900 mb-2" x-text="s.title"></h3>' in How and "<h4" not in How
    for Kept in ("Custom Jewelry Made Simple", "From idea to finished piece in four simple steps.", "Share Your Idea",
                 "Choose From Four", "Choose Your Details", "Add to Your Bag", "You see your design before anything is made."):
        assert Kept in How, Kept


def test_gold_reads_on_dark_and_the_chips_on_cream():
    """F14: gold labels on the dark bands in #C9A96E (7.9:1); the gallery filter's chips in #5F5F5F (5.6:1)."""
    assert 'style="color:#C9A96E">Technology</span>' in Index and '<span class="italic" style="color:#C9A96E">Your Design.</span>' in Index
    assert Index.count("'text-[#5F5F5F] hover:text-[#1A1A1A]'") == 2 and "'text-[#6F6F6F] hover:text-[#1A1A1A]'" not in Index


def test_materials_prices_are_whole_dollars_and_links_stay_on_one_line():
    """F17: "$90 · any ring size" — the figure in the display face, the words in Inter, no cents on a whole amount;
    "Design in steel" never wraps."""
    App = (WebDir / "app.js").read_text(encoding="utf-8")
    assert "const bare = whole && Number.isInteger(Number(v)) ? { minimumFractionDigits: 0, maximumFractionDigits: 0 } : {};" in App
    assert "{ before: '', figure: this.money(q.unit_price, q.currency, true), words: 'any ring size' }" in App
    assert '<span class="brand-font text-2xl" x-text="materialPrice(m.id).figure"></span>' in Index
    assert "x-text=\"'Design in ' + m.label.split(' ').pop().toLowerCase()\"" in Index and "materialPriceText" not in Index + App


async def test_materials_loading_cards_follow_the_products_on_offer(tmp_path):
    """The server's loading cards on /materials keep the real cards' height (CLS): with rings and charms both on offer
    a fashion card has one line more (its charm price), so its loading card is taller (.ph-charm, measured on proto)."""
    H = Harness(tmp_path)
    try:
        Rings = _Live((await H.Client.get("/materials")).text, "materials")
        assert Rings.count('<div class="material-ph"></div>') >= 3 and "ph-charm" not in Rings
        H.Ctx.Products.Set("charms_available", True, "test")
        Both = _Live((await H.Client.get("/materials")).text, "materials")
        assert Both.count('<div class="material-ph ph-charm"></div>') == Rings.count('<div class="material-ph"></div>')
    finally:
        await H.Close()
    for Rule in (".material-ph.ph-charm { height: 478px; }", "@media (min-width: 1150px) { .material-ph { height: 464px; } .material-ph.ph-charm { height: 497px; } }"):
        assert Rule in Index, Rule


def test_the_home_band_shows_xjets_photograph_of_printed_jewelry():
    """F16: the technology band shows XJet's own photograph of printed rings and charms (the brand book's PR picture,
    cropped, its watermark left out), sized; the bottles picture stays on Technology."""
    for Name in ("printed-jewelry.webp", "printed-jewelry.jpg"):
        assert Image.open(WebDir / "images" / Name).size == (960, 740), Name
    Home = Index[Index.index("<template x-if=\"view === 'home'\">"):Index.index("<template x-if=\"view === 'inspiration'\">")]
    assert '<source srcset="{{BASE}}/static/images/printed-jewelry.webp" type="image/webp"><img width="960" height="740" src="{{BASE}}/static/images/printed-jewelry.jpg"' in Home
    assert "Ink.JPG" not in Home
