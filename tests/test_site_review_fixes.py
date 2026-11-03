"""The live-site review of 3 Nov 2026 (U1–U14): an address no page answers gets a page, not JSON; the page as served
shows at once (no entrance animation holding its largest paint); the homepage's section eyebrows keep their style, the
technology band keeps its contrast from 861 to 1279 px, the story's Pause / Play no longer covers its last caption on
phones; the About menu is a disclosure that Escape closes back to its button; pictures are fetched at the size they
are drawn."""

import re
from pathlib import Path

from PIL import Image

from tests.conftest import Harness

Web = Path(__file__).resolve().parent.parent / "web"
Index = (Web / "index.html").read_text(encoding="utf-8")


async def test_an_address_no_page_answers_gets_a_page_with_the_way_back(tmp_path):
    H = Harness(tmp_path, BasePath="/JewelryB2C3")
    try:
        for P in ("/no-such-page", "/Gallery", "/materials/extra/deep"):
            R = await H.Client.get(P)
            assert R.status_code == 404 and R.headers["content-type"].startswith("text/html"), P
            assert R.headers["x-robots-tag"] == "noindex" and R.headers["cache-control"] == "no-store", P
            Page = R.text
            assert '<html lang="en">' in Page and "<title>Page Not Found · XJet Atelier</title>" in Page
            assert '<meta name="robots" content="noindex">' in Page and "<h1>Page not found</h1>" in Page
            assert '<a class="home" href="/JewelryB2C3/">Go to the homepage</a>' in Page
            for Link in ("/JewelryB2C3/gallery", "/JewelryB2C3/faq", "/JewelryB2C3/contact"):
                assert f'href="{Link}"' in Page, Link
            assert 'src="/JewelryB2C3/static/images/xjet-logo.png"' in Page and 'alt="XJet"' in Page   # the logo, not typed text
        # the API, files and media keep their own 404
        R = await H.Client.get("/api/no-such-thing")
        assert R.status_code == 404 and R.headers["content-type"].startswith("application/json")
        for P in ("/static/no-such.js", "/favicon-none.ico"):
            R = await H.Client.get(P)
            assert R.status_code == 404 and not R.headers["content-type"].startswith("text/html"), P
        assert (await H.Client.get("/")).status_code == 200                             # the pages themselves are unchanged
        assert (await H.Client.get("/faq")).status_code == 200
    finally:
        await H.Close()


def test_the_page_as_served_shows_at_once_and_later_screens_still_fade_in():
    assert ".fade-in { animation: fadeIn 0.5s ease-out; }" in Index
    assert ".ssr-view > .fade-in { animation: none; }" in Index


def test_the_homepage_sections_keep_their_eyebrows_and_the_band_its_contrast():
    for Rule in (".hv-split-copy > p:not(.hv-eyebrow) {", ".hv-ring-copy > p:not(.hv-eyebrow) {", ".hv-final p:not(.hv-eyebrow) {",
                 ".hv-facts span { color: #66666E; white-space: nowrap; }"):
        assert Rule in Index, Rule
    Band = Index[Index.index("@media (min-width: 861px) and (max-width: 1279px) {"):]
    assert Band[:220].count("rgba(25,25,28,.88) 60%") == 1                         # the dark veil holds past the copy
    Light = Index[Index.index(".hv-btn-light {"):Index.index(".hv-btn-light:hover")]
    assert "font-size: 14px !important; letter-spacing: .08em !important;" in Light   # the site's button type


def test_on_phones_the_story_control_sits_under_the_stage():
    Phone = Index[Index.index("@media (max-width: 860px) {"):]
    assert ".hv-story-stage { padding-bottom: 56px; }" in Phone[:2500]
    assert ".hv-story-stage .sc-toggle { bottom: 0; right: 50%; transform: translateX(50%); }" in Phone[:2500]


def test_the_about_menu_is_a_disclosure_escape_closes_back_to_its_button():
    Nav = Index[Index.index('<div class="relative" @click.outside="aboutOpen = false"'):Index.index('<div id="about-menu"') + 2400]
    assert """@keydown.escape="if (aboutOpen) { aboutOpen = false; $refs.aboutButton.focus() }\"""" in Nav
    assert 'x-ref="aboutButton"' in Nav and 'aria-controls="about-menu"' in Nav and ':aria-expanded="aboutOpen"' in Nav
    assert 'role="menu"' not in Nav and 'role="menuitem"' not in Nav and 'aria-haspopup="menu"' not in Nav


def test_pictures_are_fetched_at_the_size_they_are_drawn():
    # gallery cards and the pages' gallery strips: about 36 % of a phone screen (the 320 px thumbnail at 2x)
    assert "42vw" not in Index and "25vw, 50vw" not in Index
    assert Index.count('sizes="(min-width: 1280px) 230px, (min-width: 768px) 21vw, 36vw"') == 4
    assert Index.count('(min-width:768px) 27vw, 36vw"') == 2 and Index.count('(min-width:768px) 26vw, 36vw"') == 1
    assert ':srcset="srcsetFor(heroRing.image_url)" sizes="(min-width: 768px) 240px, 60vw"' in Index   # Materials' card
    # the two small Technology pictures at twice their 96 px box; the heading as every other page's
    for Name, Size in (("Ink-192.jpg", (192, 192)), ("Angel-192.jpg", (121, 192))):
        assert Image.open(Web / "images" / Name).size == Size, Name
        assert f'width="{Size[0]}" height="{Size[1]}" src="{{{{BASE}}}}/static/images/{Name}"' in Index, Name
    assert '<h1 class="text-4xl brand-font mb-8">NanoParticle Jetting™</h1>' in Index
    assert re.search(r'alt="A figurine printed in metal with NanoParticle Jetting" class="w-24 h-24 shrink-0 object-contain"', Index)
