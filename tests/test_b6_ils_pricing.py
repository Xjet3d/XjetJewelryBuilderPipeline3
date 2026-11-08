"""Israeli pricing (B6): ₪ including VAT for Israeli customers, USD for everyone else, OFF by default.

The arithmetic matches the specification's simulation tables exactly (Decimal, never floats); switched off, the site,
the bag, checkout and orders are what they were; switched on, the visitor's choice or connection country shows ₪, the
delivery country decides at checkout, and an order in ₪ keeps its VAT and how it was priced. No network: the Bank of
Israel fetch is replaced."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from p3 import ilspricing as Ils
from tests.conftest import Harness

AdminKey = "b6-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Customer = {"first_name": "Dana", "last_name": "Levi", "email": "dana@atelier-customer.co.il", "phone": "+972 54 123 4567"}
Address = {"recipient": "Dana Levi", "line1": "12 Rothschild Blvd", "line2": "", "city": "Tel Aviv", "region": "",
           "postal_code": "6688112", "country": "IL"}
UsAddress = {"recipient": "Dana Levi", "line1": "1 Main St", "line2": "", "city": "Austin", "region": "TX",
             "postal_code": "73301", "country": "US"}
Israeli = {"CF-IPCountry": "IL"}
D = Decimal


def _Total(Usd, Rate, Stage="after_vat", Ending="9", Vat="18"):
    return Ils.PriceIls(Usd, D(Rate), D(Vat), Stage, Ending)["total"]


# ── the arithmetic: the specification's tables ──────────────────────────
def test_rule_2_and_rule_1_match_the_specification_tables_at_3_136():
    Usd = [90, 200, 49, 69, 99, 109, 159, 59, 139]
    Rule2 = [339, 749, 189, 259, 369, 409, 589, 219, 519]
    Rule1 = ["341.02", "742.22", "187.62", "258.42", "376.42", "411.82", "588.82", "223.02", "518.02"]
    assert [_Total(U, "3.136") for U in Usd] == [D(X) for X in Rule2]
    assert [_Total(U, "3.136", "before_vat") for U in Usd] == [D(X) for X in Rule1]


def test_the_owners_decision_gives_419_for_the_109_dollar_charm():
    # D1 + D5: Rule 2 with the 2% buffer — $109 × 3.14 × 1.02 × 1.18 = ₪411.95 → ₪419; Rule 1 of the brief = ₪411.82
    assert _Total(109, D("3.14") * D("1.02")) == D(419)
    assert _Total(109, "3.14", "before_vat") == D("411.82")
    # every list price at the 2 Nov rate with the buffer (the spec's "2% buffer" table, Rule 2 column)
    Rate = D("3.136") * D("1.02")
    assert [_Total(U, Rate) for U in (90, 200, 49, 69, 99, 109, 159, 59, 139)] == \
        [D(X) for X in (349, 759, 189, 269, 379, 419, 609, 229, 529)]


def test_rounding_is_always_up_exact_values_stay_and_vat_adds_back_to_the_total():
    assert Ils.RoundUp(D("349"), "9") == 349 and Ils.RoundUp(D("349.01"), "9") == 359 and Ils.RoundUp(D("340"), "9") == 349
    assert Ils.RoundUp(D("350"), "49_99") == 399 and Ils.RoundUp(D("349"), "49_99") == 349
    assert Ils.RoundUp(D("341"), "0") == 350 and Ils.RoundUp(D("340.2"), "1") == 341
    for U in (49, 90, 109, 200, 1234.5):
        P = Ils.PriceIls(U, D("3.2"), D("18"), "after_vat", "9")
        assert P["net"] + P["vat"] == P["total"] and P["total"] % 10 == 9


def test_the_settings_are_checked_and_keep_the_owners_defaults():
    S = Ils.Validate({})
    assert (S["enabled"], S["buffer_pct"], S["reprice_threshold_pct"], S["stage"], S["ending"], S["vat_pct"]) == \
        (False, 2.0, 1.5, "after_vat", "9", 18.0)
    for Bad in ({"stage": "x"}, {"ending": "7"}, {"rate_mode": "manual"}, {"buffer_pct": 50}, {"vat_next_pct": 17}):
        with pytest.raises(Ils.IlsPricingError):
            Ils.Validate(Bad)
    assert Ils.Validate({"countries": "il, ps"})["countries"] == ["IL", "PS"]


# ── the site ────────────────────────────────────────────────────────────
def _Fetcher(Rate="3.136", Day="2026-11-02"):
    return lambda: (Day, D(Rate), "boi_sdmx")


async def _InBag(H, Prompt="Twisted rope band", **Headers):
    Batch = await H.NewDesign(Prompt)
    Cand = Batch["candidates"][0]
    Cus = (await H.Client.post(f"/api/designs/{Batch['design_id']}/customize", json={"candidate_id": Cand["id"]})).json()
    await H.Idle()
    await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"ring_size": 7, "material_id": "silver"})
    R = await H.Client.post("/api/bag", json={"customization_id": Cus["id"]}, headers=Headers)
    assert R.status_code == 200, R.text
    return Cus


def _Order(**Over):
    return {"customer": Customer, "address": Address, "shipping_method": "standard", "terms_accepted": True,
            "client_request_id": "click-1", **Over}


async def _TurnOn(H, **Settings):
    H.Ctx.IlsPricing.Fetcher = _Fetcher()
    R = await H.Client.post("/api/admin/ils-pricing/fetch", headers=Admin)
    assert R.status_code == 200 and R.json()["fetch"]["ok"], R.text
    R = await H.Client.put("/api/admin/ils-pricing", headers=Admin, json={"settings": {"enabled": True, **Settings}, "note": "test"})
    assert R.status_code == 200, R.text
    return R.json()


async def test_off_by_default_nothing_changes_even_for_an_israeli_connection(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Cat = (await H.Client.get("/api/catalog", headers=Israeli)).json()
        assert "pricing" not in Cat
        Q = (await H.Client.get("/api/quote", params={"material_id": "silver"}, headers=Israeli)).json()
        assert Q["currency"] == "USD" and "incl_vat" not in Q
        await _InBag(H)
        Bag = (await H.Client.get("/api/bag", headers=Israeli)).json()
        assert list(Bag["totals"]) == ["USD"] and "incl_vat" not in Bag
        Co = (await H.Client.post("/api/checkout/quote", json={"country": "IL"}, headers=Israeli)).json()
        assert Co["currency"] == "USD" and "vat_amount" not in Co and "shipping_options" not in Co
        O = (await H.Client.post("/api/orders", json=_Order(), headers=Israeli)).json()
        assert O["currency"] == "USD" and "vat_amount" not in O
        Row = H.Ctx.Db.One("SELECT vat_amount, fx_json FROM orders WHERE id = ?", (O["id"],))
        assert Row == {"vat_amount": None, "fx_json": None}
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM fx_rates")["n"] == 0          # off: nothing fetched
    finally:
        await H.Close()


async def test_on_an_israeli_visitor_sees_shekels_and_the_delivery_country_decides(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Card = await _TurnOn(H)
        assert Card["status"]["price_rate"]["rate"] == "3.1987"                     # 3.136 + 2%
        Usd = (await H.Client.get("/api/quote", params={"material_id": "silver"})).json()
        Q = (await H.Client.get("/api/quote", params={"material_id": "silver"}, headers=Israeli)).json()
        assert Usd["currency"] == "USD" and Q["currency"] == "ILS" and Q["incl_vat"] is True
        assert D(str(Q["unit_price"])) == _Total(Usd["unit_price"], "3.1987")
        Cat = (await H.Client.get("/api/catalog", headers=Israeli)).json()
        assert Cat["pricing"] == {"currency": "ILS", "choices": ["ILS", "USD"], "incl_vat": True, "ils_ready": True}
        # the visitor's own choice wins over the connection
        Chosen = (await H.Client.get("/api/quote", params={"material_id": "silver"}, headers=Israeli,
                                     cookies={"p3_currency": "USD"})).json()
        assert Chosen["currency"] == "USD"
        await _InBag(H, **Israeli)
        Bag = (await H.Client.get("/api/bag", headers=Israeli)).json()
        assert list(Bag["totals"]) == ["ILS"] and Bag["lines"][0]["unit_price"] == Q["unit_price"]
        # checkout: delivery abroad → USD without Israeli VAT, even on an Israeli connection
        Abroad = (await H.Client.post("/api/checkout/quote", json={"country": "US", "shipping_method": "express"}, headers=Israeli)).json()
        assert Abroad["currency"] == "USD" and Abroad["shipping"] == 45.0 and "vat_amount" not in Abroad
        # delivery to Israel from abroad → ₪ including VAT, express converted by the same rule
        Home = (await H.Client.post("/api/checkout/quote", json={"country": "IL", "shipping_method": "express"})).json()
        assert Home["currency"] == "ILS" and Home["shipping"] == float(_Total(45, "3.1987"))
        assert Home["total"] == Home["subtotal"] + Home["shipping"]
        assert Home["vat_amount"] == round(Home["total"] - Home["total"] / 1.18, 2)
        assert {S["id"]: S["currency"] for S in Home["shipping_options"]} == {"standard": "ILS", "express": "ILS"}
    finally:
        await H.Close()


async def test_an_order_to_israel_keeps_its_shekels_vat_and_rate_and_its_email_says_so(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _TurnOn(H)
        await _InBag(H)
        Shown = (await H.Client.post("/api/checkout/quote", json={"country": "IL"})).json()
        O = (await H.Client.post("/api/orders", json=_Order(expected_total=Shown["total"]))).json()
        assert O["currency"] == "ILS" and O["total"] == Shown["total"] and O["vat_amount"] == Shown["vat_amount"]
        assert "fx" not in O                                                        # how it was priced stays in the Admin
        Row = H.Ctx.Db.One("SELECT o.fx_json, l.usd_unit_price, l.unit_price, l.currency FROM orders o "
                           "JOIN order_lines l ON l.order_id = o.id WHERE o.id = ?", (O["id"],))
        import json
        Fx = json.loads(Row["fx_json"])
        assert (Fx["rate"], Fx["base_rate"], Fx["base_date"], Fx["source"], Fx["buffer_pct"], Fx["vat_pct"], Fx["stage"]) == \
            ("3.1987", "3.136", "2026-11-02", "boi_sdmx", 2.0, 18.0, "after_vat")
        assert Row["currency"] == "ILS" and D(str(Row["unit_price"])) == _Total(Row["usd_unit_price"], "3.1987")
        Ad = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()
        assert Ad["fx"]["rate"] == "3.1987"
        Mail = [M for M in H.App.state.Mailer.List() if M["to"] == Customer["email"]][-1]
        assert "₪" in Mail["html"] and "Including VAT" in Mail["html"]
        # a later rate never touches it
        H.Ctx.IlsPricing.Fetcher = _Fetcher("3.5", "2026-11-03")
        await H.Client.post("/api/admin/ils-pricing/fetch", headers=Admin)
        assert (await H.Client.get(f"/api/orders/{O['id']}")).json()["total"] == O["total"]
        # turned OFF: the next quote is USD again, the order keeps its ₪
        await H.Client.put("/api/admin/ils-pricing", headers=Admin, json={"settings": {"enabled": False}})
        assert (await H.Client.get("/api/quote", params={"material_id": "silver"}, headers=Israeli)).json()["currency"] == "USD"
        assert (await H.Client.get(f"/api/orders/{O['id']}")).json()["currency"] == "ILS"
    finally:
        await H.Close()


async def test_a_bag_changed_by_the_delivery_country_is_reviewed_again(tmp_path):
    # The page showed USD (an Israeli on a foreign VPN); ordering to Israel prices in ₪: the customer sees it first
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _TurnOn(H)
        await _InBag(H)
        Usd = (await H.Client.post("/api/checkout/quote", json={})).json()
        R = await H.Client.post("/api/orders", json=_Order(expected_total=Usd["total"]))
        assert R.status_code == 409 and R.json()["error"]["code"] == "bag_changed"
    finally:
        await H.Close()


async def test_a_percent_promo_is_taken_from_the_shekel_lines(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _TurnOn(H)
        R = await H.Client.post("/api/admin/promo-codes", headers=Admin, json={"code": "TEN", "kind": "percent", "value": 10, "active": True})
        assert R.status_code == 200, R.text
        await _InBag(H)
        Q = (await H.Client.post("/api/checkout/quote", json={"country": "IL", "promo_code": "TEN"})).json()
        assert Q["currency"] == "ILS" and Q["discount"] == round(Q["subtotal"] * 0.10, 2)
        assert Q["total"] == round(Q["subtotal"] - Q["discount"], 2)
    finally:
        await H.Close()


# ── the rate ────────────────────────────────────────────────────────────
async def test_the_price_rate_moves_only_by_the_threshold_the_weekly_day_or_on_purpose(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        I = H.Ctx.IlsPricing
        I.Fetcher = _Fetcher("3.136")
        I.Fetch()
        I.Save({**I.Settings(), "enabled": True}, "test")
        assert I.PriceRate()["rate"] == "3.1987"
        Wed = datetime(2026, 11, 4, 17, tzinfo=Ils.Israel)
        I.Fetcher = _Fetcher("3.16", "2026-11-03")                  # +0.77%: below 1.5%, prices stay
        I.Fetch()
        assert I.Reprice(Now_=Wed) is None and I.PriceRate()["rate"] == "3.1987"
        I.Fetcher = _Fetcher("3.20", "2026-11-04")                  # +2.04%: prices follow
        I.Fetch()
        assert I.Reprice(Now_=Wed)["rate"] == "3.2640" and "moved" in I.PriceRate()["reason"]
        I.Fetcher = _Fetcher("3.21", "2026-11-05")                  # small move, but Sunday is the weekly day
        I.Fetch()
        Today = datetime.now(Ils.Israel)                            # the stored rate is dated by the real clock
        Sun = (Today + timedelta(days=7 - (Today.weekday() + 1) % 7)).replace(hour=17)   # the next Sunday after today
        assert I.Reprice(Now_=Sun)["reason"] == "weekly update"
        # both official sources down: reported, nothing invented, prices keep the last good rate
        def Down():
            raise RuntimeError("SDMX: timeout; PublicApi: timeout")
        I.Fetcher = Down
        assert I.Fetch()["ok"] is False and I.PriceRate()["rate"] == "3.2742"
        # a manual rate with its end date wins over the Bank of Israel while it applies
        I.Save({**I.Settings(), "rate_mode": "manual", "manual_rate": 3.3, "manual_until": "2099-01-01"}, "test")
        assert I.BaseRate()["source"] == "manual" and I.Reprice(Force=True)["source"] == "manual"
    finally:
        await H.Close()


def test_the_bank_of_israel_answers_are_read_from_sdmx_then_the_public_api(monkeypatch):
    import httpx

    class R:
        def __init__(self, Text="", Json=None, Status=200):
            self.text, self._J, self.status_code = Text, Json, Status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("down", request=None, response=None)

        def json(self):
            return self._J

    Csv = ("SERIES_CODE,FREQ,BASE_CURRENCY,COUNTER_CURRENCY,UNIT_MEASURE,DATA_TYPE,DATA_SOURCE,TIME_COLLECT,CONF_STATUS,"
           "PUB_WEBSITE,UNIT_MULT,COMMENTS,TIME_PERIOD,OBS_VALUE,RELEASE_STATUS\n"
           "RER_USD_ILS,D,USD,ILS,ILS,OF00,BOI_MRKT,V,F,Y,0,,2026-11-02,3.136,YP\n")
    monkeypatch.setattr(httpx, "get", lambda Url, timeout: R(Csv))
    assert Ils.FetchBankOfIsrael() == ("2026-11-02", D("3.136"), "boi_sdmx")
    Api = {"key": "USD", "currentExchangeRate": 3.136, "lastUpdate": "2026-11-02T13:21:08.3289971Z"}
    monkeypatch.setattr(httpx, "get", lambda Url, timeout: R(Status=503) if "sdmx" in Url else R(Json=Api))
    assert Ils.FetchBankOfIsrael() == ("2026-11-02", D("3.136"), "boi_publicapi")
    monkeypatch.setattr(httpx, "get", lambda Url, timeout: R(Status=503))
    with pytest.raises(RuntimeError):
        Ils.FetchBankOfIsrael()


# ── the Admin ───────────────────────────────────────────────────────────
async def test_the_admin_previews_without_saving_versions_every_save_and_only_admins_may(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        assert (await H.Client.get("/api/admin/ils-pricing")).status_code in (401, 403)
        await _TurnOn(H)
        Before = (await H.Client.get("/api/admin/ils-pricing", headers=Admin)).json()
        P = (await H.Client.post("/api/admin/ils-pricing/preview", headers=Admin,
                                 json={"settings": {"stage": "before_vat"}, "extra_usd": 109})).json()
        assert P["settings"]["stage"] == "after_vat"                                   # nothing saved
        Any = [R for R in P["simulation"]["rows"] if R["product"] == "Any amount"][0]
        assert Any["current"]["total"] == str(_Total(109, "3.1987")) and Any["proposed"]["total"] == str(_Total(109, "3.1987", "before_vat"))
        assert all("current" in R for R in P["simulation"]["rows"])
        R = await H.Client.put("/api/admin/ils-pricing", headers=Admin, json={"settings": {"ending": "7"}})
        assert R.status_code == 400
        Saved = (await H.Client.put("/api/admin/ils-pricing", headers=Admin, json={"settings": {"stage": "before_vat"}})).json()
        assert Saved["settings"]["stage"] == "before_vat" and len(Saved["versions"]) == len(Before["versions"]) + 1
        Back = (await H.Client.post("/api/admin/ils-pricing/restore", headers=Admin,
                                    json={"version_id": Before["versions"][0]["id"]})).json()
        assert Back["settings"]["stage"] == "after_vat"
    finally:
        await H.Close()
