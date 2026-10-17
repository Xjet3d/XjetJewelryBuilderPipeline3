"""Cookie consent and analytics (docs/ANALYTICS.md, p3/analytics.py, web/consent.js): off by default — no configuration
in the pages and the strict Content-Security-Policy unchanged; the Admin's settings checked and logged; with a tool on,
only the customer pages get its configuration and its hosts in the policy (never the Admin, the API or the files); the
quote page offers the same consent, never Clarity, and states its funnel events without any personal detail."""

import json
import re
from pathlib import Path

from tests.conftest import Harness
from tests.test_quotes import AdminKey, Address, Customer, _Link, _Offer, _Request      # the quote helpers act as this Admin

Admin = {"Authorization": f"Bearer {AdminKey}"}
WebDir = Path(__file__).resolve().parent.parent / "web"
Google = ("https://www.googletagmanager.com", "https://*.google-analytics.com")
Clarity = ("https://*.clarity.ms", "https://c.bing.com")


def _Config(Html: str) -> dict:
    return json.loads(re.search(r"window\.__p3 = (\{.*?\});</script>", Html).group(1))


def _Csp(R) -> str:
    return R.headers["content-security-policy"]


async def test_off_by_default_nothing_is_configured_and_the_policy_is_unchanged(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        R = await H.Client.get("/")
        assert _Config(R.text)["analytics"] is None
        assert "connect-src 'self';" in _Csp(R) and not any(X in _Csp(R) for X in Google + Clarity)
        assert '/static/consent.js?v=' in R.text and R.text.count("data-cookie-settings") >= 2      # the banner's script, both footers
        S = (await H.Client.get("/api/admin/analytics", headers=Admin)).json()
        assert S["enabled"] is False and S["host"] == S["ga4"] == S["clarity"] == "" and S["active"] is False and S["consent_version"] == 1
        assert (await H.Client.get("/api/admin/analytics")).status_code == 403
        assert (await H.Client.put("/api/admin/analytics", json={"enabled": True})).status_code == 403
        assert (await H.Client.get("/static/consent.js")).status_code == 200
    finally:
        await H.Close()


async def test_the_admin_settings_are_checked_and_logged(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        for Body, Code in (({"ga4": "UA-1234-1"}, "invalid_ga4"), ({"clarity": "Not An Id!"}, "invalid_clarity"),
                           ({"host": "xjet atelier"}, "invalid_host"), ({"enabled": True}, "host_required"),
                           ({"enabled": "yes"}, "invalid_value"), ({}, "invalid_value")):
            R = await H.Client.put("/api/admin/analytics", json=Body, headers=Admin)
            assert R.status_code == 400 and R.json()["error"]["code"] == Code, (Body, R.text)
        R = await H.Client.put("/api/admin/analytics", json={"host": "https://Proto/JewelryB2C3/", "ga4": " g-test1234 ", "clarity": "abcd1234ef"}, headers=Admin)
        S = R.json()
        assert R.status_code == 200 and S["host"] == "proto" and S["ga4"] == "G-TEST1234" and S["clarity"] == "abcd1234ef"
        assert S["enabled"] is False and S["active"] is False                     # IDs alone run nothing
        S = (await H.Client.put("/api/admin/analytics", json={"enabled": True}, headers=Admin)).json()
        assert S["enabled"] and S["active"] and S["changed_by"]
        Log = H.Ctx.Db.All("SELECT note FROM product_settings_log WHERE key = 'analytics' ORDER BY id")
        assert len(Log) == 2 and Log[-1]["note"] == ("analytics on · host proto · Google Analytics 4 on G-TEST1234 · "
                                                      "Microsoft Clarity on abcd1234ef")
        S = (await H.Client.put("/api/admin/analytics", json={"ga4": "", "clarity": ""}, headers=Admin)).json()
        assert S["enabled"] and not S["active"]                                   # on, but no tool: nothing to consent to
    finally:
        await H.Close()


async def test_with_a_tool_on_only_the_customer_pages_get_it(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await H.Client.put("/api/admin/analytics", json={"enabled": True, "host": "xjetatelier.xjet3d.com", "ga4": "G-PROD12345"}, headers=Admin)
        R = await H.Client.get("/")
        assert _Config(R.text)["analytics"] == {"version": 1, "host": "xjetatelier.xjet3d.com", "ga4": "G-PROD12345", "clarity": None}
        Csp = _Csp(R)
        assert all(X in Csp for X in Google) and not any(X in Csp for X in Clarity)
        assert re.search(r"script-src 'self' 'unsafe-inline' 'unsafe-eval' https://www\.googletagmanager\.com;", Csp)
        assert "connect-src 'self' https://www.googletagmanager.com https://*.google-analytics.com https://*.google.com;" in Csp
        for Path in ("/faq", "/privacy", "/gallery"):
            Page = await H.Client.get(Path)
            assert all(X in _Csp(Page) for X in Google), Path
        await H.Client.put("/api/admin/analytics", json={"clarity": "abcd1234ef"}, headers=Admin)
        Csp = _Csp(await H.Client.get("/"))
        assert all(X in Csp for X in Google + Clarity)
        # The Admin, the API and the files keep the strict policy: no tool can ever run there
        for Path, Headers in (("/admin", {}), ("/api/gallery", {}), ("/api/admin/analytics", Admin), ("/static/app.js", {}), ("/robots.txt", {})):
            R = await H.Client.get(Path, headers=Headers)
            assert not any(X in _Csp(R) for X in Google + Clarity), Path
        Admin_ = (await H.Client.get("/admin")).text
        assert "consent.js" not in Admin_ and "googletagmanager" not in Admin_ and "clarity.ms" not in Admin_
        # Off again: the configuration and the hosts are gone at once
        await H.Client.put("/api/admin/analytics", json={"enabled": False}, headers=Admin)
        R = await H.Client.get("/")
        assert _Config(R.text)["analytics"] is None and not any(X in _Csp(R) for X in Google + Clarity)
    finally:
        await H.Close()


def _Tools(S: dict) -> dict:
    return {P["key"]: {K: P[K] for K in ("enabled", "id", "configured", "active")} for P in S["providers"]}


async def test_each_tool_has_its_own_switch_and_id_and_loads_only_when_both_are_set(tmp_path):
    """Admin-managed tools: each with its own ID and on/off, read back from the database. A tool loads (its ID in the
    page, its hosts in the policy) only when analytics is offered, the host is set, the tool is on and its ID is valid;
    a tool without an ID is never in the page; a tool cannot be switched on without its ID; marketing has no tool."""
    H = Harness(tmp_path, AdminKey=AdminKey)
    Put = lambda Body: H.Client.put("/api/admin/analytics", json=Body, headers=Admin)      # noqa: E731
    try:
        S = (await H.Client.get("/api/admin/analytics", headers=Admin)).json()
        assert [P["key"] for P in S["providers"]] == ["ga4", "clarity"] and S["marketing_providers"] == []
        assert {P["key"]: (P["name"], P["id_label"], P["example"]) for P in S["providers"]} == {
            "ga4": ("Google Analytics 4", "Measurement ID", "G-XXXXXXXXXX"), "clarity": ("Microsoft Clarity", "Project ID", "abcd1234ef")}
        assert _Tools(S)["ga4"] == _Tools(S)["clarity"] == {"enabled": False, "id": "", "configured": False, "active": False}
        # Switching a tool on needs its ID
        R = await Put({"providers": {"clarity": {"enabled": True}}})
        assert R.status_code == 400 and R.json()["error"]["code"] == "id_required"
        R = await Put({"providers": {"ads": {"enabled": True, "id": "AW-1"}}})
        assert R.status_code == 400 and R.json()["error"]["code"] == "invalid_value"                 # no such tool
        # GA4 only: Clarity has no ID, so it is never in the page nor in the policy
        S = (await Put({"enabled": True, "host": "xjetatelier.xjet3d.com", "providers": {"ga4": {"enabled": True, "id": "G-PROD12345"}}})).json()
        assert _Tools(S) == {"ga4": {"enabled": True, "id": "G-PROD12345", "configured": True, "active": True},
                             "clarity": {"enabled": False, "id": "", "configured": False, "active": False}}
        R = await H.Client.get("/")
        assert _Config(R.text)["analytics"] == {"version": 1, "host": "xjetatelier.xjet3d.com", "ga4": "G-PROD12345", "clarity": None}
        assert all(X in _Csp(R) for X in Google) and not any(X in _Csp(R) for X in Clarity)
        # Clarity only: GA4 kept but switched off
        S = (await Put({"providers": {"ga4": {"enabled": False}, "clarity": {"enabled": True, "id": "ABCD1234EF"}}})).json()
        assert _Tools(S)["ga4"] == {"enabled": False, "id": "G-PROD12345", "configured": True, "active": False}
        assert _Tools(S)["clarity"] == {"enabled": True, "id": "abcd1234ef", "configured": True, "active": True}
        R = await H.Client.get("/")
        assert _Config(R.text)["analytics"] == {"version": 1, "host": "xjetatelier.xjet3d.com", "ga4": None, "clarity": "abcd1234ef"}
        assert all(X in _Csp(R) for X in Clarity) and not any(X in _Csp(R) for X in Google)
        # Both, then analytics off: everything stays saved, nothing reaches the page
        await Put({"providers": {"ga4": {"enabled": True}}})
        assert _Config((await H.Client.get("/")).text)["analytics"]["ga4"] == "G-PROD12345"
        S = (await Put({"enabled": False})).json()
        assert S["host"] == "xjetatelier.xjet3d.com" and S["ga4"] == "G-PROD12345" and S["clarity"] == "abcd1234ef" and not S["active"]
        R = await H.Client.get("/")
        assert _Config(R.text)["analytics"] is None and not any(X in _Csp(R) for X in Google + Clarity)
        # What the Admin reads back is what the database holds
        Row = json.loads(H.Ctx.Db.One("SELECT value_json FROM product_settings WHERE key = 'analytics'")["value_json"])
        assert Row == {"enabled": False, "host": "xjetatelier.xjet3d.com",
                       "providers": {"ga4": {"enabled": True, "id": "G-PROD12345"}, "clarity": {"enabled": True, "id": "abcd1234ef"}}}
    finally:
        await H.Close()


async def test_a_configuration_saved_before_the_tools_had_their_own_switch_reads_the_same(tmp_path):
    """The flat shape of 2026-10-13 ({"ga4": …, "clarity": …}, a tool on whenever its ID is set) — what Atelier holds
    today — is read as exactly that until the Admin saves again: nothing changes for the site."""
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        H.Ctx.Products.Set("analytics", {"enabled": True, "host": "xjetatelier.xjet3d.com", "ga4": "G-PROD12345", "clarity": ""}, "test")
        S = (await H.Client.get("/api/admin/analytics", headers=Admin)).json()
        assert S["enabled"] and S["ga4"] == "G-PROD12345" and S["clarity"] == ""
        assert _Tools(S) == {"ga4": {"enabled": True, "id": "G-PROD12345", "configured": True, "active": True},
                             "clarity": {"enabled": False, "id": "", "configured": False, "active": False}}
        assert _Config((await H.Client.get("/")).text)["analytics"] == {"version": 1, "host": "xjetatelier.xjet3d.com", "ga4": "G-PROD12345", "clarity": None}
        S = (await H.Client.put("/api/admin/analytics", json={"providers": {"clarity": {"enabled": True, "id": "abcd1234ef"}}}, headers=Admin)).json()
        assert _Tools(S)["clarity"]["active"] and _Tools(S)["ga4"]["active"]                         # saved in today's shape
    finally:
        await H.Close()


def test_no_tool_id_is_written_in_the_code():
    """The IDs belong to each environment's Admin: no GA4 measurement ID or Clarity project ID is written into the
    application (only the format examples and the test placeholders)."""
    Root = WebDir.parent
    Allowed = {"G-XXXXXXXXXX", "G-XXXXXXX"}
    for Path_ in [*Root.joinpath("p3").rglob("*.py"), *Root.joinpath("web").glob("*.js"), *Root.joinpath("web").glob("*.html"),
                  *Root.joinpath("config").rglob("*.*")]:
        Text = Path_.read_text(encoding="utf-8", errors="replace")
        for Id in re.findall(r"\bG-[A-Z0-9]{6,20}\b", Text):
            assert Id in Allowed or Id.startswith("G-TEST"), (Path_.name, Id)
        assert not re.search(r"clarity\.ms/tag/[a-z0-9]{6,}", Text), Path_.name


async def test_placeholder_ids_run_the_consent_but_never_open_the_policy(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        S = (await H.Client.put("/api/admin/analytics", json={"enabled": True, "host": "proto", "ga4": "G-TEST0000", "clarity": "test0000"},
                                headers=Admin)).json()
        assert S["active"] and S["placeholder"] == {"ga4": True, "clarity": True}
        R = await H.Client.get("/")
        assert _Config(R.text)["analytics"] == {"version": 1, "host": "proto", "ga4": "G-TEST0000", "clarity": "test0000"}
        assert "connect-src 'self';" in _Csp(R) and not any(X in _Csp(R) for X in Google + Clarity)     # nothing could load
        S = (await H.Client.put("/api/admin/analytics", json={"ga4": "G-REAL12345"}, headers=Admin)).json()
        assert S["placeholder"] == {"ga4": False, "clarity": True}
        Csp = _Csp(await H.Client.get("/"))
        assert all(X in Csp for X in Google) and not any(X in Csp for X in Clarity)
    finally:
        await H.Close()


async def test_the_quote_page_offers_consent_never_clarity_and_states_events_without_personal_details(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await H.Client.put("/api/admin/analytics", json={"enabled": True, "host": "test", "ga4": "G-TEST1234", "clarity": "abcd1234ef"}, headers=Admin)
        _B, Q = await _Request(H)
        assert (await _Offer(H, Q)).status_code == 200
        Link = _Link(H)
        Page = await H.Client.get(Link)
        assert "window.__p3NoClarity = true" in Page.text and '/static/consent.js?v=' in Page.text and "data-cookie-settings" in Page.text
        Cfg = json.loads(re.search(r"window\.__p3Analytics = (\{.*?\});", Page.text).group(1))
        assert Cfg["ga4"] == "G-TEST1234" and Cfg["host"] == "test"
        Events = json.loads(re.search(r"window\.__p3Events = (\[.*?\]);</script>", Page.text).group(1))
        assert Events == [{"name": "quote_viewed", "params": {"product_type": "ring"}}]
        Token = Link.split("token=")[1]
        R = await H.Client.post("/quote/approve", data={"token": Token, **Address, "shipping_method": "standard", "terms": "1"})
        assert R.status_code == 200 and "ORD-10001" in R.text
        Raw = re.search(r"window\.__p3Events = (\[.*?\]);</script>", R.text).group(1)
        Events = {E["name"]: E["params"] for E in json.loads(Raw)}
        assert Events["quote_approved"] == {"product_type": "ring", "currency": "USD", "value": 5200}
        assert Events["order_created"] == {"source": "quote", "currency": "USD", "value": 5200, "item_count": 2}
        assert "purchase" not in Events                                     # an order placed is not a payment received
        for Personal in (Customer["first_name"], Customer["last_name"], Customer["email"], Customer["phone"], Address["line1"], Address["city"],
                         Address["postal_code"], Token, "Engrave", "crescent"):
            assert Personal not in Raw, Personal
        # Declined: one event, nothing else
        _B, Q2 = await _Request(H, Prompt="A band with waves")
        await _Offer(H, Q2)
        Token2 = _Link(H).split("token=")[1]
        R = await H.Client.post("/quote/reject", data={"token": Token2, "note": "Too expensive for me, Dana"})
        Raw = re.search(r"window\.__p3Events = (\[.*?\]);</script>", R.text).group(1)
        assert json.loads(Raw) == [{"name": "quote_declined", "params": {"product_type": "ring"}}] and "Dana" not in Raw
    finally:
        await H.Close()


async def test_the_privacy_page_tells_customers_what_is_used_with_no_draft_notice(tmp_path):
    """The Privacy page as served (its live copy): no draft notice (nor on Terms); today's sign-in (Google, Apple,
    password); the three categories, both tools by name with what Clarity masks; a summary (Technology / Purpose / How
    long) with verified figures, the exact names only behind "View technical cookie names", never the staff cookie or
    Microsoft's own cookies; Marketing & Advertising said not to be used; nothing claimed "anonymous"."""
    from tests.test_seo import _Live
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Live = _Live((await H.Client.get("/privacy")).text, "privacy")
        assert "Draft pending legal review" not in Live and "adapted for this preview" not in Live
        for Text in ("with Google or Apple", "with a password", "Cookies and similar technologies", "Essential — always active.",
                     "Analytics &amp; Performance — only with your consent.", "Google Analytics 4", "Microsoft Clarity",
                     "configured to mask sensitive information", "Atelier does not currently use marketing or advertising cookies.",
                     ">Technology<", ">Purpose<", ">How long<", "Cookies: up to 2 years. Detailed data: 14 months.",
                     "Session recordings: 30 days", "View technical cookie names", ">_ga<", ">_clck<", ">_clsk<", "atelier@xjet3d.com"):
            assert Text in Live, Text
        Names = Live[Live.index("View technical cookie names"):]
        assert Live.index(">Technology<") < Live.index("View technical cookie names") and ">atelier_session<" in Names
        for Absent in ("atelier_admin_session", "MUID", "CLID", "anonym", "Sign-in code (stored hashed)"):
            assert Absent not in Live, Absent
        assert "data-cookie-settings" in Live
        Terms = _Live((await H.Client.get("/terms")).text, "terms")
        assert "Draft pending legal review" not in Terms and "adapted for this preview" not in Terms
    finally:
        await H.Close()


def test_the_banner_names_no_tool_and_marketing_is_shown_as_not_used():
    """The first layer says what for, never which tool, cookie or retention; Privacy preferences names the tools under
    Analytics & Performance only, shows Marketing & Advertising as not used (no switch), and the stored choice keeps
    marketing false. No marketing tag exists in the script."""
    Js = (WebDir / "consent.js").read_text(encoding="utf-8")
    Banner = Js[Js.index("function showBanner()"):Js.index("function closeBanner()")]
    for Text in ("Your privacy matters", "Accept all", "Reject optional", "Manage preferences", "Privacy Policy"):
        assert Text in Banner, Text
    for Word in ("Google", "Clarity", "Microsoft", "_ga", "_cl", "month", "year"):
        assert Word not in Banner, Word
    Settings = Js[Js.index("function openSettings()"):Js.index("function closeSettings()")]
    for Text in ("Privacy preferences", "Essential", "Always active", "Analytics &amp; Performance", "Marketing &amp; Advertising",
                 "Not currently used", "Save preferences", "Reject optional", "Accept all", "data-ac=\"learn\""):
        assert Text in Settings, Text
    Marketing = Settings[Settings.index("'Marketing &amp; Advertising'"):]
    assert 'class="ac-sw"' not in Marketing                                    # no switch for a category that is not used
    assert "marketing: false" in Js[Js.index("function save("):Js.index("function decide(")]
    for Tag in ("fbq(", "connect.facebook", "googleadservices", "doubleclick", "AW-", "ads_data_redaction"):
        assert Tag not in Js, Tag


def test_sign_ins_and_new_accounts_name_their_method_and_an_order_is_no_purchase():
    """web/app.js, web/consent.js, web/authpage.html (2026-10-17): login and sign_up for every way in — a password (an
    email or a username), Google, Apple, the six-letter code and its link — and sign_up when Google or Apple made the
    account or its email was confirmed by its link; the method of a return from Google, Apple or an emailed link is
    the server's session's. An order placed sends order_created, never "purchase": no payment is taken online yet, and
    "purchase" is kept for a payment received."""
    Js = (WebDir / "app.js").read_text(encoding="utf-8")
    Consent = (WebDir / "consent.js").read_text(encoding="utf-8")
    Page = (WebDir / "authpage.html").read_text(encoding="utf-8")
    assert "sign_up: { method: ['email', 'google', 'apple'] }," in Consent
    assert "login: { method: ['password', 'google', 'apple', 'code', 'link'] }," in Consent
    assert "purchase" not in Consent[Consent.index("var SCHEMA = {"):Consent.index("var ITEM = {")]
    assert "this.measure('purchase'" not in Js and "this.measure('order_created', { source: 'checkout'" in Js
    Map = Js[Js.index("    _measureSignIn(created) {"):Js.index("    submitSignIn() {")]
    assert "m === 'google' || m === 'apple' ? m : m === 'password' ? (created ? 'email' : 'password') : ''" in Map
    assert "this.measure(created ? 'sign_up' : 'login', { method });" in Map
    SignIn = Js[Js.index("    submitSignIn() {"):Js.index("    submitRegister() {")]
    assert SignIn.index("this.measure('login', { method: 'password' });") < SignIn.index("await this._signedIn(R);")
    Claim = Js[Js.index("    submitClaimPassword() {"):Js.index("    // ── Account page: Sign-in & security")]
    assert "this.measure('login', { method: 'password' });" in Claim
    Init = Js[Js.index("    async init() {"):Js.index("    persist(patch) {")]
    assert "this._measureSignIn(authStatus === 'created');" in Init
    assert "this.authCsrf = S.csrf; this._authMethod = S.method || '';" in Js
    assert "location.replace(BASE + '/#auth=' + (MODE === 'verify' ? 'created' : 'ok'))" in Page
