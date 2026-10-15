"""After merging main df2c3e4 (AJW-39 sign-in, Analytics) into the Fulfillment integration branch (2026-10-15): the
pieces together, as tron would run them — the integration on and the sign-in mode main ships by default (dual).

Customers register and sign in the new way, the six-letter code still works beside it and the old registration is
closed; the Admin signs in with its key (identity not enforced) and only the Admin reaches the Production actions; the
integration keeps its own service credential under main's session middleware — never the Admin key, never a customer
session — and works without any exemption, since Fulfillment sends no Origin and no cookie (a request that does is
refused like any cross-site one)."""

from tests.test_auth import Browser, Csrf, LinkSecret, Mails, Pw, Register
from tests.test_fulfillment_bridge import Admin, AdminKey, Prefix, Service, _Enrolled, _Event, _On, _Placed


async def test_customers_register_and_sign_in_the_new_way_with_the_integration_on(tmp_path):
    H = _On(tmp_path, AuthMode="dual")                         # what a deployed server runs by default (P3_AUTH_MODE unset); the harness keeps legacy
    try:
        assert H.Ctx.Settings.AuthMode == "dual" and H.Ctx.Settings.FulfillmentIntegration
        B = Browser(H)
        await Register(H, B, "noa@atelier-customer.co.il", "Noa")
        Sent = [M for M in Mails(H) if M["to"] == "noa@atelier-customer.co.il"]
        V = await B.post("/api/auth/verify-email", json={"token": LinkSecret(Sent[-1]["html"])})
        assert V.status_code == 200 and "atelier_session" in V.headers["set-cookie"], V.text
        assert (await B.get("/api/auth/session")).json()["authenticated"]
        assert (await B.post("/api/auth/logout", headers=Csrf(V))).status_code == 200
        L = await B.post("/api/auth/login", json={"email": "noa@atelier-customer.co.il", "password": Pw})
        assert L.status_code == 200, L.text
        # The six-letter code still opens the old account (dual), and the old registration is closed
        assert (await H.Client.get("/api/token-status")).status_code == 200
        R = await Browser(H).post("/api/register", json={"Name": "Old way", "Email": "old.way@atelier-customer.co.il"})
        assert R.status_code == 410 and R.json()["error"]["code"] == "sign_in_changed"
        # A signed-in customer reaches neither the Admin nor the integration
        assert (await B.get("/api/admin/orders")).status_code in (401, 403)
        R = await B.post(Prefix + "/events", json={"events": []}, headers=Csrf(L))
        assert R.status_code == 403 and R.json()["error"]["code"] == "service_auth_required"
    finally:
        await H.Close()


async def test_the_admin_signs_in_with_its_key_and_only_the_admin_reaches_production(tmp_path):
    H = _On(tmp_path)
    try:
        Cfg = (await H.Client.get("/api/admin/auth/config")).json()
        assert Cfg["identity_enforced"] is False and Cfg["key_login"] is True
        B = Browser(H)
        assert (await B.post("/api/admin/login", json={"key": AdminKey})).status_code == 200
        assert (await B.get("/api/admin/orders")).status_code == 200                       # the remembered browser, no header
        Did, O = await _Placed(H, Qty=1)
        Url = f"/api/admin/orders/{O['id']}/production"
        Line = H.Svc.Orders.AdminGet(O["id"])["lines"][0]["id"]
        for Who in ({}, {"X-Access-Token": H.Token}, Service):                             # nobody, a customer, the service credential
            R = await Browser(H).post(Url, json={"line_ids": [Line]}, headers=Who)
            assert R.status_code in (401, 403), (Who, R.status_code)
            assert (await Browser(H).get(f"/api/admin/orders/{O['id']}/fulfillment", headers=Who)).status_code in (401, 403)
        await _Enrolled(H, Did, O)                                                         # the Admin key: prepared and sent
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_line_production WHERE order_id = ?", (O["id"],))["n"] == 1
    finally:
        await H.Close()


async def test_the_integration_keeps_its_own_service_credential_under_the_session_middleware(tmp_path):
    H = _On(tmp_path)
    try:
        Did, O = await _Placed(H, Qty=1)
        await _Enrolled(H, Did, O)
        Line = H.Svc.Orders.AdminGet(O["id"])["lines"][0]["id"]
        Fulfillment = Browser(H)                                                           # like Fulfillment's client: the credential only
        assert (await Fulfillment.get(Prefix + "/identity", headers=Service)).json()["environment_id"]
        R = await Fulfillment.post(Prefix + "/events", json={"events": [_Event("m-1", O["id"], 1)]}, headers=Service)
        assert R.status_code == 200 and R.json()["results"][0]["status"] == "applied", R.text
        R = await Fulfillment.post(Prefix + "/release-checks", json={"items": [{"line_id": Line, "quantity": 1}]}, headers=Service)
        assert R.status_code == 200 and R.json()["check_id"].startswith("chk_") and "ok" in R.json()["items"][0], R.text
        # Anything but the service credential is refused: no credential, the Admin key, a customer's code
        for Who in ({}, Admin, {"X-Access-Token": H.Token}):
            R = await Browser(H).post(Prefix + "/release-checks", json={"items": []}, headers=Who)
            assert R.status_code == 403 and R.json()["error"]["code"] == "service_auth_required", Who
        # main's middleware treats a request that claims to come from a web page like any other: a foreign Origin is refused
        # before the credential is even read — Fulfillment never sends one (no exemption was added, none is needed)
        R = await Browser(H).post(Prefix + "/events", json={"events": []}, headers={**Service, "Origin": "https://elsewhere.example"})
        assert R.status_code == 403 and R.json()["error"]["code"] == "bad_origin"
    finally:
        await H.Close()


async def test_tron_keeps_today_s_sign_in_in_legacy_mode_behind_proto(tmp_path, monkeypatch):
    """tron and proto keep today's sign-in (the owner's message of 2026-10-15, §3): the integrated code with
    P3_AUTH_MODE=legacy and tron's own P3_PUBLIC_BASE_URL (http://proto) — the old dialog (no provider), /api/register
    mails a code, a six-letter code signs in and writes; the Admin's key; Fulfillment's calls (no Origin). Customer
    writes through proto (proto sends Host: tron; tron's nginx overwrites X-Forwarded-Host) and on tron itself pass
    main's Origin check. A browser on proto's IP address does not — today it does: the release plan's decision."""
    monkeypatch.setenv("P3_PUBLIC_BASE_URL", "http://proto")
    H = _On(tmp_path, AuthMode="legacy", PublicBaseUrl="http://proto")
    try:
        Cfg = (await H.Client.get("/api/auth/config")).json()
        assert Cfg["mode"] == "legacy" and not any(Cfg["providers"].values())

        def Via(Origin: str, Token: str = "") -> dict:
            return {"Host": "tron", "X-Forwarded-Host": "tron", "Origin": Origin, "X-Access-Token": Token}
        for N, Origin in enumerate(("http://proto", "https://proto", "https://tron", "http://tron")):
            R = await H.Client.post("/api/register", json={"Name": "Pilot", "Email": f"pilot{N}@atelier-customer.co.il"}, headers=Via(Origin))
            assert R.status_code == 200 and R.json()["status"] == "verification_sent", (Origin, R.text)
        assert (await H.Client.post("/api/events", json={"kind": "bag_viewed"}, headers=Via("http://proto", H.Token))).status_code == 200
        # A browser on proto's IP address (seen in tron's log, 4–8 Oct): refused by the Origin check of the integrated code
        R = await H.Client.post("/api/register", json={"Name": "Pilot", "Email": "pilot.ip@atelier-customer.co.il"}, headers=Via("http://172.16.10.27"))
        assert R.status_code == 403 and R.json()["error"]["code"] == "bad_origin"
        # The Admin's key and Fulfillment's service credential, as on tron
        assert (await H.Client.get("/api/admin/orders", headers={**Admin, "Host": "tron"})).status_code == 200
        assert (await H.Client.get(Prefix + "/identity", headers={**Service, "Host": "tron"})).status_code == 200
        assert (await H.Client.post(Prefix + "/events", json={"events": []}, headers={**Service, "Host": "tron"})).status_code == 200
    finally:
        await H.Close()
