"""The customer's journey at its own addresses (2026-10-15): /design, /my-designs, /favorites, /customize, /bag and
/checkout serve the app, which restores the step for the signed-in customer or asks them to sign in first. They are
never indexed (noindex meta and header), never in the sitemap, and carry no canonical link, social tags, structured
data, server-side copy or anything of a customer. A design is never in an address. The public pages, the Gallery's
addresses and the analytics calls are unchanged."""

import json
import re

from p3 import app as P3App
from p3 import sitepages as SitePages
from p3.settings import WebDir
from tests.conftest import Harness

AdminKey = "flow-routes-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Origin = "https://atelier.example.com"


def _Config(Html: str) -> dict:
    return json.loads(re.search(r"window\.__p3 = (\{.*?\});</script>", Html).group(1))


async def test_each_step_of_the_journey_has_its_address_and_is_never_indexed(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey, PublicBaseUrl=Origin, BasePath="/JewelryB2C3")
    try:
        await H.Client.put("/api/admin/site-indexing", json={"on": True}, headers=Admin)      # the public pages indexable
        assert set(SitePages.FlowPaths) == {"design", "my-designs", "favorites", "customize", "bag", "checkout"}
        for Flow in SitePages.FlowPaths:
            R = await H.Client.get("/" + Flow)
            assert R.status_code == 200 and R.headers["content-type"].startswith("text/html"), Flow
            assert R.headers.get("x-robots-tag") == "noindex" and R.headers["cache-control"] == "no-cache", Flow
            Head = R.text.split("<body")[0]
            assert '<meta name="robots" content="noindex">' in Head and 'rel="canonical"' not in Head, Flow
            assert 'property="og:' not in Head and "ld+json" not in Head and "google-site-verification" not in Head, Flow
            Cfg = _Config(R.text)
            assert Cfg["route"] == Flow and Cfg["view"] == "" and Cfg["ssr"] == "", Flow       # the app shows the step
            assert "data-ssr-view=" not in R.text and "data-ssr-fallback" not in R.text, Flow   # no server-side copy
            assert 'x-data="p3App()"' in R.text and "/static/app.js?v=" in R.text, Flow
        # the public pages keep what they had
        Home = await H.Client.get("/")
        assert '<meta name="robots" content="index,follow">' in Home.text and 'rel="canonical"' in Home.text
        assert "x-robots-tag" not in Home.headers and "route" not in _Config(Home.text)
        P3App.SitemapCache.clear()
        Map = (await H.Client.get("/sitemap.xml")).text
        assert not [F for F in SitePages.FlowPaths if f"/JewelryB2C3/{F}</loc>" in Map]
        Robots = (await H.Client.get("/robots.txt")).text
        assert "Disallow: /JewelryB2C3/api/" in Robots and "Allow: /JewelryB2C3/" in Robots
        assert not [F for F in SitePages.FlowPaths if f"/{F}" in Robots]                     # a noindex page stays readable
    finally:
        await H.Close()


async def test_a_step_page_carries_nothing_of_the_customer_and_the_gallery_addresses_stay(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Mine = await H.NewDesign("An engraved signet with a family crest")
        Title = (await H.Design(Mine["design_id"]))["title"]
        for Flow in SitePages.FlowPaths:                                                    # asked with the customer's own token
            T = (await H.Client.get("/" + Flow)).text
            assert Title not in T and Mine["design_id"] not in T, Flow
        # /design is the Design screen's address; /design/<name> still moves a public gallery design to its page
        B = await H.NewDesign("A slim band")
        await H.Client.put(f"/api/designs/{B['design_id']}/selection", json={"candidate_id": B["candidates"][0]["id"]})
        Item = (await H.Client.post("/api/admin/gallery", json={"design_id": B["design_id"]}, headers=Admin)).json()
        Slug = (await H.Client.get(f"/api/gallery/{Item['id']}/share")).json()["slug"]
        R = await H.Client.get(f"/design/{Slug}")
        assert R.status_code == 301 and R.headers["location"] == f"/gallery/{Slug}"
        assert (await H.Client.get("/design/no-such-design")).status_code == 404
        assert (await H.Client.get(f"/gallery/{Slug}")).status_code == 200
        assert (await H.Client.get("/design")).status_code == 200
    finally:
        await H.Close()


def test_the_app_walks_the_journey_with_the_browser():
    """web/app.js and web/index.html: the route table, a new step per screen the visitor goes to, Back / Forward showing
    the screen their address names, the restore after a reload, the sign-in first, and the app's own Back buttons."""
    Js = (WebDir / "app.js").read_text(encoding="utf-8")
    Page = (WebDir / "index.html").read_text(encoding="utf-8")
    Table = re.search(r"const FLOW_PATHS = (\{[^}]*\});", Js).group(1)
    assert set(re.findall(r":\s*'([a-z-]+)'", Table)) == set(SitePages.FlowPaths)              # the server serves the same
    assert "view: P3_FLOW ? '' : P3_PAGE," in Js and "const P3_FLOW = flowOf(location.pathname);" in Js
    assert "window.addEventListener('popstate', () => this._onHistory());" in Js
    assert "if (P3_FLOW) await this._openFlow(P3_FLOW, { history: 'replace', restore: true });" in Js
    assert "STUDIO_VIEWS.includes(st.view) && P3_PAGE === 'home'" not in Js            # the Design screen is not at '/' any more
    assert "if (!this.token) { if (P3_FLOW) this._signInFor(P3_FLOW); return; }" in Js     # signed out: sign in first
    assert "sessionStorage.setItem('p3_pending_route', flow);" in Js                      # kept across Google / Apple
    Route = Js[Js.index("    routePath() {"):Js.index("    _goBackOr(v) {")]
    assert "design.id" not in Route and "designId" not in Route                          # no design in an address
    assert "history.pushState({ p3Pushed: PAGE_LOAD, p3Prev: here }, '', to);" in Route
    Flow = Js[Js.index("    async _openFlow(flow, {"):Js.index("    _signInFor(flow) {")]
    assert "await this.openDesign(id, { restoreView: at, history: 'replace' });" in Flow and "this.persist({ designId: null });" in Flow
    assert "checkout: 'order' }[flow]" in Flow and "resumed: true" in Flow                  # checkout again, straight (no bag first)
    assert "_doResetAIFlow" not in Flow                                                    # a restore is no New design click
    assert "if (this.view === 'ai-studio') { this.panelRoute = ''; this._route(history); }" in Flow   # Back from the panel: no page view
    # one page view per screen, as before: startNew leaves it to its caller; a resumed checkout counts no new start
    New = Js[Js.index("    startNew({ history = 'push' } = {}) {"):Js.index("    async openDesign(designId, {")]
    assert "navigateTo(" not in New and "this._route(history);" in New
    Co = Js[Js.index("    async startCheckout({ history = 'push', resumed = false } = {}) {"):Js.index("    coGoTo(step) {")]
    assert "if (!resumed) this.track('checkout_clicked');" in Co and "if (!resumed) this.measure('begin_checkout'" in Co
    assert "backToDesign() { this._goBackOr('ai-studio'); }," in Js
    assert "if (this.co.step === 'details') { this._goBackOr('checkout'); return; }" in Js
    assert "await this.openDesign(d.id, { history: fromDialog ? 'replace' : 'push' });" in Js   # Make it yours takes the dialog's step
    assert Js.count("AtelierAnalytics?.page(") == 2                                           # the same page views as before
    for Call in ("panelTab('designs')", "panelTab('favorites')", "continueFromBag()"):
        assert f'@click="{Call}"' in Page, Call
    assert Page.count('@click="closePanel()"') == 2 and '@click="sidebarOpen = false"' not in Page
    assert "@click=\"sidebarTab = 'designs'\"" not in Page
