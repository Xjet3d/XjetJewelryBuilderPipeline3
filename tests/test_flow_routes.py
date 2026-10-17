"""The customer's journey at its own addresses (2026-10-15): /design, /my-designs, /favorites, /customize, /bag and
/checkout serve the app, which restores the step for the signed-in customer or asks them to sign in first. They are
never indexed (noindex meta and header), never in the sitemap, and carry no canonical link, social tags, structured
data, server-side copy or anything of a customer. A design is never in an address. The public pages and the Gallery's
addresses are unchanged. Each screen has its own title, and its page view names its real address (2026-10-17)."""

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
    assert "p3Col: !!colLeft" in Route                                                     # the step left keeps its panel's state
    assert "if (v === 'ai-studio') this.sidebarTab = 'designs';" in Js                     # /design: the panel on My Designs
    assert "if (this.sidebarTab === tab) return;" in Js                                    # the tab already shown adds no step
    Flow = Js[Js.index("    async _openFlow(flow, {"):Js.index("    _signInFor(flow) {")]
    assert "await this.openDesign(id, { restoreView: at, history: 'replace' });" in Flow and "this.persist({ designId: null });" in Flow
    assert "checkout: 'order' }[flow]" in Flow and "resumed: true" in Flow                  # checkout again, straight (no bag first)
    assert "_doResetAIFlow" not in Flow                                                    # a restore is no New design click
    assert "if (this.view === 'ai-studio') { this.panelRoute = ''; this._route(history); this._screenShown(); }" in Flow   # from the panel
    # one page view per screen: startNew leaves it to its caller; a resumed checkout counts no new start
    New = Js[Js.index("    startNew({ history = 'push' } = {}) {"):Js.index("    async openDesign(designId, {")]
    assert "navigateTo(" not in New and "this._route(history);" in New
    Co = Js[Js.index("    async startCheckout({ history = 'push', resumed = false } = {}) {"):Js.index("    coGoTo(step) {")]
    assert "if (!resumed) this.track('checkout_clicked');" in Co and "if (!resumed) this.measure('begin_checkout'" in Co
    assert "backToDesign() { this._goBackOr('ai-studio'); }," in Js
    assert "if (this.co.step === 'details') { this._goBackOr('checkout'); return; }" in Js
    assert "await this.openDesign(d.id, { history: fromDialog ? 'replace' : 'push' });" in Js   # Make it yours takes the dialog's step
    assert Js.count("AtelierAnalytics?.page(") == 2                                           # the landing, then each screen
    for Call in ("panelTab('designs')", "panelTab('favorites')", "continueFromBag()"):
        assert f'@click="{Call}"' in Page, Call
    assert Page.count('@click="closePanel()"') == 2 and '@click="sidebarOpen = false"' not in Page
    assert "@click=\"sidebarTab = 'designs'\"" not in Page


async def test_each_screen_has_its_title_and_the_server_page_at_a_step_carries_it(tmp_path):
    """The tab's title follows the screen (p3/sitepages.py ScreenTitles): the page served at a step's address carries
    that step's title — the one the app shows there — so a reload or a link counts one page view, not two."""
    H = Harness(tmp_path, AdminKey=AdminKey, BasePath="/JewelryB2C3")
    try:
        assert set(SitePages.FlowScreens) == set(SitePages.FlowPaths) and set(SitePages.FlowScreens.values()) <= set(SitePages.ScreenTitles)
        Home = await H.Client.get("/")
        Titles = _Config(Home.text)["titles"]
        for View, Title in SitePages.ScreenTitles.items():
            assert Titles[View] == Title, View                                             # the app's copy of them
        assert Titles["inspiration"].startswith("Inspiration Gallery") and Titles["home"]
        for Flow, View in SitePages.FlowScreens.items():
            R = await H.Client.get("/" + Flow)
            assert re.search(r"<title>(.*?)</title>", R.text, re.S).group(1) == SitePages.ScreenTitles[View].replace("&", "&amp;"), Flow
            assert _Config(R.text)["titles"] == Titles, Flow
        assert re.search(r"<title>(.*?)</title>", Home.text, re.S).group(1) == Titles["home"].replace("&", "&amp;")
    finally:
        await H.Close()


def test_page_views_name_the_real_address_and_title_once_per_screen():
    """web/app.js and web/consent.js (2026-10-17): a step's page view is its real address (/design, /customize, /bag,
    /checkout …), never BASE/app/<step>; the tab's title is the screen's; a gallery design opened in its dialog has its
    own page view (/gallery/<slug>) and closing it gives the screen behind one; one page view per screen (address and
    title; a burst of screens counts the last); events carry the screen they happen on; GA4's address is set, never
    pinned in config; the site's own scroll depth replaces GA4's."""
    Js = (WebDir / "app.js").read_text(encoding="utf-8")
    Consent = (WebDir / "consent.js").read_text(encoding="utf-8")
    assert "'app/'" not in Js and "app/' + v" not in Js
    Measure = Js[Js.index("    measureScreen() {"):Js.index("    measureItem(")]
    assert "this.routePath()" in Measure and "this.galleryHref(this.galleryItem)" in Measure and ".replace(/#.*$/, '')" in Measure
    Nav = Js[Js.index("    navigateTo(v, {"):Js.index("    pageUrl(v) {")]
    assert "this._screenShown();" in Nav and "measureScreen(v)" not in Nav
    assert "_screenShown() { document.title = this.screenTitle(); this.measureScreen(); }," in Js
    for Name, Start, End in (("openPanel", "    openPanel(tab = 'designs'", "    panelTab(tab) {"), ("panelTab", "    panelTab(tab) {", "    closePanel() {"),
                             ("closePanel", "    closePanel() {", "    signOutFromAccount()")):
        assert "this._screenShown();" in Js[Js.index(Start):Js.index(End)], Name
    Open = Js[Js.index("    openGallery(g, {"):Js.index("    metalLabel(id) {")]
    assert Open.index("this.measureScreen();") < Open.index("this.measure('gallery_design_opened'")      # the page view, then the event
    Close = Js[Js.index("    closeGallery(how = '') {"):Js.index("    _onHistory() {")]
    assert "if (wasOpen && how !== 'navigate') this.measureScreen();" in Close
    Proceed = Js[Js.index("    async proceed() {"):Js.index("    showCustomization(c, {")]
    assert Proceed.index("this.navigateTo('review');") < Proceed.index("this.measure('customize_opened'")
    Co = Js[Js.index("    async startCheckout({"):Js.index("    coGoTo(step) {")]
    assert Co.index("this.navigateTo('order', { history });") < Co.index("this.measure('begin_checkout'")
    # consent.js: the address set (not in config), a screen = address + title, sent once a burst is over, events after it
    Ga4 = Consent[Consent.index("function startGa4()"):Consent.index("// ── Microsoft Clarity")]
    assert Ga4.index("gtag('set', { page_location: cleanUrl(location.href)") < Ga4.index("gtag('config', CFG.ga4, {")
    assert "page_location" not in Ga4[Ga4.index("gtag('config', CFG.ga4, {"):]
    Send = Consent[Consent.index("function sendPage(path, title) {"):Consent.index("function flushPage() {")]
    assert "if (loc + '\\n' + t === pageSent) return;" in Send and "window.gtag('set', { page_location: loc" in Send
    Track = Consent[Consent.index("function track(name, params) {"):Consent.index("// Events a server-rendered page states")]
    assert "flushPage();" in Track and "data.page_location = pageLoc; data.page_title = pageTitle;" in Track
    Page = Consent[Consent.index("    page: function (path, title) {"):Consent.index("    openSettings: openSettings,")]
    assert "pageTimer = setTimeout(flushPage, 0);" in Page and "sendPage(" not in Page
    Scroll = Consent[Consent.index("  function scrolled(e) {"):Consent.index("  var P = ['ring', 'charm']")]
    assert "Date.now() - lastGesture > 4000" in Scroll and "el.tagName !== 'MAIN'" in Scroll and "percent_scrolled: 90" in Scroll
    assert "el.scrollHeight * 0.9 <= seen" in Scroll                                           # a page that fits the window: never


def test_back_closes_the_enlarged_view_and_a_cleared_design_breaks_nothing():
    """WP7 (2026-10-17): the enlarged view is a step of its own in the browser's history, at the same address — Back
    closes it and the screen stays; ×, Escape and the backdrop take that step back; the next screen the app opens takes
    it (no dead step). The parts that read an option or a design the app has just cleared are guarded (the two errors in
    Clarity: "reading 'id'" with Back from the enlarged view, "reading 'batches'" on a new design after checkout)."""
    Js = (WebDir / "app.js").read_text(encoding="utf-8")
    Page = (WebDir / "index.html").read_text(encoding="utf-8")
    Open = Js[Js.index("    openPreview(media, src, candidate = null) {"):Js.index("    get previewSiblings() {")]
    assert "history.pushState({ ...(history.state || {}), p3Preview: PAGE_LOAD }, '', location.pathname + location.search + location.hash);" in Open
    assert "closePreview(how = 'leave') {" in Open
    assert "if (how === 'user' && history.state && history.state.p3Preview === PAGE_LOAD) { this._previewBack = true; history.back(); }" in Open
    Hist = Js[Js.index("    _onHistory() {"):Js.index("    async _openFlow(flow, {")]
    assert Hist.index("if (this._previewBack) { this._previewBack = false; return; }") < Hist.index("this.closePreview('popstate');")
    assert "if (this.routePath() === location.pathname + location.hash) return;" in Hist
    Route = Js[Js.index("    _route(how = 'push', colLeft = this.sidebarOpen) {"):Js.index("    _goBackOr(v) {")]
    assert "if (how === 'push' && history.state && history.state.p3Preview === PAGE_LOAD) {" in Route
    assert "const { p3Preview, ...kept } = history.state || {};" in Route
    Overlay = Page[Page.index('<template x-if="previewOpen">'):Page.index("</template>", Page.index('<template x-if="previewOpen">'))]
    assert Overlay.count("closePreview('user')") == 3 and "closePreview()" not in Overlay
    assert Page.count("closePreview('user')") == 5
    assert 'x-show="previewCandidate?.id !== selectedId"' in Page and 'x-show="previewCandidate?.id === selectedId"' in Page
    assert "previewCandidate.id" not in Page
    assert "const n = (this.design?.batches || []).filter(x => x.kind === 'refine').indexOf(b) + 1;" in Js
    Label = Js[Js.index("    batchLabel(b) {"):Js.index("    get quotaText() {")]
    assert "this.design.batches" not in Label                                           # (the polling tick checks first)
