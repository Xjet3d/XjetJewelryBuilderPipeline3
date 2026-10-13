"""Cookie consent and analytics (docs/ANALYTICS.md, p3/analytics.py, web/consent.js): off by default — no configuration
in the pages and the strict Content-Security-Policy unchanged; the Admin's settings checked and logged; with a tool on,
only the customer pages get its configuration and its hosts in the policy (never the Admin, the API or the files); the
quote page offers the same consent, never Clarity, and states its funnel events without any personal detail."""

import json
import re

from tests.conftest import Harness
from tests.test_quotes import AdminKey, Address, Customer, _Link, _Offer, _Request      # the quote helpers act as this Admin

Admin = {"Authorization": f"Bearer {AdminKey}"}
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
        assert len(Log) == 2 and "analytics on · host proto · GA4 G-TEST1234 · Clarity abcd1234ef" in Log[-1]["note"]
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
        assert Events["purchase"]["transaction_id"] == "ORD-10001" and Events["purchase"]["items"] == [
            {"item_id": "custom-ring", "item_name": "Custom ring", "item_category": "ring", "item_variant": "gold_18k_yellow", "price": 2600, "quantity": 2}]
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
