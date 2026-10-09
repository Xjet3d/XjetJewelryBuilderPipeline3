"""Search engines and messaging apps: the home page carries Open Graph / Twitter tags and a canonical link (the share
page keeps its own, for the design); the Admin, the tools and the API are never indexed; design polling pauses while
the tab is hidden."""

from p3.settings import WebDir
from tests.conftest import Harness

AdminKey = "seo-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}


async def test_the_home_page_carries_social_tags_and_a_canonical_link(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey, PublicBaseUrl="https://atelier.example.com")
    try:
        Page = (await H.Client.get("/")).text
        assert '<meta property="og:title" content="XJet Atelier — Custom AI Jewelry, Designed by You">' in Page
        assert '<meta property="og:url" content="https://atelier.example.com/">' in Page
        assert '<link rel="canonical" href="https://atelier.example.com/">' in Page
        assert '<meta name="twitter:card" content="summary_large_image">' in Page
        assert 'property="og:image"' not in Page                                   # no gallery design yet: no preview image
        # A published design becomes the preview image
        B = await H.NewDesign("A slim band")
        await H.Client.put(f"/api/designs/{B['design_id']}/selection", json={"candidate_id": B["candidates"][0]["id"]})
        R = await H.Client.post("/api/admin/gallery", json={"design_id": B["design_id"]}, headers=Admin)
        assert R.status_code == 200, R.text
        Page = (await H.Client.get("/")).text
        assert 'property="og:image" content="https://atelier.example.com/thumb/' in Page and Page.count('property="og:title"') == 1
        # The share page has the design's own tags, once
        Slug = (await H.Client.get(f"/api/gallery/{R.json()['id']}/share")).json()["slug"]
        Share = (await H.Client.get(f"/design/{Slug}")).text
        assert Share.count('property="og:title"') == 1 and f'content="https://atelier.example.com/design/{Slug}"' in Share
        assert "{{OG}}" not in Share and "{{OG}}" not in Page
    finally:
        await H.Close()


async def test_mock_mode_text_reaches_only_a_mock_site(tmp_path):
    """The mock-mode banner and badges are in the page only while the AI is in mock mode: a live site (and a search
    engine reading it) never gets "Mock mode … simulated placeholders"."""
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Page = (await H.Client.get("/")).text
        assert "data-mock-banner" in Page and "simulated placeholders" in Page and "<!--mock-->" not in Page
        H.App.state.Modes.Mode = "live"
        for Path_ in ("/", "/design/no-such-design"):
            Page = (await H.Client.get(Path_)).text
            assert "data-mock-banner" not in Page and "simulated placeholders" not in Page and ">Mock Mode<" not in Page, Path_
            assert "<!--mock-->" not in Page and "<!--/mock-->" not in Page and "x-data=\"p3App()\"" in Page, Path_
    finally:
        H.App.state.Modes.Mode = "mock"
        await H.Close()


def test_the_copy_says_jewelry_and_xjet_atelier():
    """American "Jewelry" everywhere a customer reads (pages, scripts, emails, server-rendered pages) and "XJet Atelier"
    in text — the logo artwork is not text."""
    for Name in ("index.html", "app.js", "showcase.html"):
        Text = (WebDir / Name).read_text(encoding="utf-8")
        assert "ewellery" not in Text, Name
        assert "XJET Atelier" not in Text and "XJET ATELIER" not in Text, Name
    for Name in ("app.py", "mail.py", "quotes.py", "registration.py", "orders.py"):
        Text = (WebDir.parent / "p3" / Name).read_text(encoding="utf-8")
        assert "Jewellery" not in Text and "XJET Atelier" not in Text and "XJET ATELIER" not in Text, Name


async def test_the_admin_the_tools_and_the_api_are_never_indexed(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        for P in ("/admin/", "/api/catalog", "/dev", "/static/admin.js"):
            assert (await H.Client.get(P)).headers.get("x-robots-tag") == "noindex, nofollow", P
        assert "x-robots-tag" not in (await H.Client.get("/")).headers
        assert "x-robots-tag" not in (await H.Client.get("/static/app.js")).headers
    finally:
        await H.Close()


def test_polling_pauses_while_the_tab_is_hidden():
    App = (WebDir / "app.js").read_text(encoding="utf-8")
    Tick = App[App.index("    ensurePolling() {"):App.index("    stopPolling()")]
    assert "if (document.hidden) return;" in Tick
