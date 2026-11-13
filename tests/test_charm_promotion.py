"""P1 (12 Nov 2026) — the Christmas charm promotion: promotional prices per material and size in the charm price table,
one promotion with an optional start and end, the regular price crossed out and the saving shown only while they are
verified reference prices, and a banner that never claims more than the promotion gives. Charms only; rings untouched."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from p3 import charmprices as CharmPrices
from p3.ilspricing import LocalizePromotion, Market
from p3.mail import OrderConfirmationEmail
from tests.conftest import Harness
from tests.test_charm_pricing import Admin, AdminKey, CharmCustomize
from tests.test_orders import Address, Customer

Regular = {"stainless_steel": {"level_prices": {"1": 59, "2": 69, "3": 99}}, "silver": {"level_prices": {"1": 89, "2": 109, "3": 159}},
           "vermeil": {"level_prices": {"1": 59, "2": 99, "3": 139}}}
Promo = {"stainless_steel": {"18": 39}, "silver": {"18": 69}}
Banner = {"enabled": True, "text": "A little holiday magic · Save $20 on charms",
          "plain_text": "A little holiday magic · Christmas prices on charms", "link": "/design"}
Verified = {"reference_verified": True, "reference_note": "Test: verified by counsel"}


def Iso(Dt):
    return Dt.astimezone(timezone.utc).isoformat(timespec="seconds")


@pytest.fixture
async def HC(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    assert (await Obj.Client.post("/api/admin/login", json={"key": AdminKey})).status_code == 200
    Obj.Client.headers["X-P3-Preview"] = "1"
    assert (await Obj.Client.put("/api/admin/products/availability", json={"product": "charm", "available": True},
                                 headers=Admin)).status_code == 200
    R = await Obj.Client.put("/api/admin/products/charm-sizes", json={"sizes": [18, 22, 26], "enabled": [18]}, headers=Admin)
    assert R.status_code == 200, R.text
    # the regular prices, then the promotional prices for 18 mm (Stainless Steel and Sterling Silver)
    await Prices(Obj, {M: {**R, "promo_prices": Promo.get(M, {})} for M, R in Regular.items()})
    # as on proto: 14K Gold over Stainless Steel is Preview only for charms (not sold), so every charm on sale is in it
    R = await Obj.Client.put("/api/admin/material-statuses", headers=Admin, json={
        "changes": [{"product": "charm", "material_id": "vermeil", "status": "preview"}], "note": "as on proto"})
    assert R.status_code == 200, R.text
    yield Obj
    await Obj.Close()


async def Prices(H, Materials, Ok=True):
    R = await H.Client.put("/api/admin/charm-prices", json={"materials": Materials, "note": "test"}, headers=Admin)
    assert (R.status_code == 200) == Ok, R.text
    return R.json()


async def Promotion(H, Ok=True, **P):
    R = await H.Client.put("/api/admin/charm-prices/promotion", json={"promotion": {"enabled": True, "label": "Christmas price", **P}},
                           headers=Admin)
    assert (R.status_code == 200) == Ok, R.text
    return R.json()


async def SetBanner(H, Ok=True, **B):
    R = await H.Client.put("/api/admin/promo-banner", json={"banner": {**Banner, **B}}, headers=Admin)
    assert (R.status_code == 200) == Ok, R.text
    return R.json()


async def Quote(H, Material, Size=18):
    return (await H.Client.get(f"/api/quote?material_id={Material}&product=charm&charm_size={Size}")).json()


async def test_the_promotional_price_is_charged_and_the_saving_shown_only_when_verified(HC):
    H = HC
    Off = await Quote(H, "stainless_steel")
    assert Off["unit_price"] == 59.0 and "promotion" not in Off                       # no promotion yet: the regular price
    Ring = (await H.Client.get("/api/quote?material_id=silver")).json()
    State = await Promotion(H)
    assert State["promotion_state"] == "active" and State["promotion"]["label"] == "Christmas price"
    assert [(X["label"], X["price"], X["savings"]) for X in State["coverage"]["eligible"]] == \
        [("18 mm Stainless Steel", 39.0, 20.0), ("18 mm Sterling Silver", 69.0, 20.0)]
    assert State["coverage"]["all"] is True and State["coverage"]["savings"] == [20.0]
    # Not verified as reference prices: the promotional price and its label — no regular price, no saving
    Q = await Quote(H, "stainless_steel")
    assert Q["unit_price"] == 39.0 and Q["pricing_version"].endswith("-promo")
    assert Q["promotion"] == {"label": "Christmas price", "ends_at": None, "claims": False}
    await Promotion(H, reference_verified=True, Ok=False)                              # verified needs how
    await Promotion(H, **Verified)
    for Mat, Was, Now_ in (("stainless_steel", 59.0, 39.0), ("silver", 89.0, 69.0)):
        Q = await Quote(H, Mat)
        assert (Q["unit_price"], Q["promotion"]["regular_unit_price"], Q["promotion"]["savings"], Q["promotion"]["claims"]) == \
            (Now_, Was, 20.0, True)
    assert (await H.Client.get("/api/quote?material_id=silver")).json() == Ring         # the ring price is untouched
    V = await Quote(H, "vermeil")
    assert V["unit_price"] is None and "promotion" not in V                            # preview only (as on proto): no price
    # The catalog (Materials & Pricing) and Customize say the same
    Cat = (await H.Client.get("/api/catalog")).json()["products"]["charm"]
    Steel = next(M for M in Cat["materials"] if M["id"] == "stainless_steel")["prices"]
    assert [(X["size"], X["price"], X["promotion"]["regular_unit_price"]) for X in Steel] == [(18.0, 39.0, 59.0)]
    _B, C = await CharmCustomize(H, Material="stainless_steel")
    assert C["quote"]["unit_price"] == 39.0 and C["quote"]["promotion"]["savings"] == 20.0 and C["line_total"] == 39.0
    assert C["charm_sizes"][0]["unit_price"] == 39.0 and C["charm_sizes"][0]["promotion"]["regular_unit_price"] == 59.0
    C = (await H.Client.patch(f"/api/customizations/{C['id']}", json={"material_id": "silver"})).json()
    assert (C["quote"]["unit_price"], C["quote"]["promotion"]["regular_unit_price"]) == (69.0, 89.0)   # switching metal


async def test_the_bag_checkout_and_order_carry_the_promotional_price_and_the_order_keeps_it(HC):
    H = HC
    await Promotion(H, **Verified)
    _B, C = await CharmCustomize(H, Material="silver")
    assert (await H.Client.post("/api/bag", json={"customization_id": C["id"]})).status_code == 200
    Line = (await H.Client.get("/api/bag")).json()["lines"][0]
    assert (Line["unit_price"], Line["promotion"]["regular_unit_price"], Line["promotion"]["savings"]) == (69.0, 89.0, 20.0)
    Q = (await H.Client.post("/api/checkout/quote", json={})).json()
    assert Q["lines"][0]["unit_price"] == 69.0 and Q["subtotal"] == 69.0 and Q["lines"][0]["promotion"]["savings"] == 20.0
    assert "_promotion" not in Q["lines"][0]
    O = (await H.Client.post("/api/orders", json={"customer": Customer, "address": Address, "terms_accepted": True,
                                                  "client_request_id": "promo-1", "expected_total": Q["total"]})).json()
    assert O["subtotal"] == 69.0 and O["lines"][0]["unit_price"] == 69.0
    assert O["lines"][0]["promotion"] == {"label": "Christmas price", "ends_at": None, "claims": True, "regular_unit_price": 89.0, "savings": 20.0}
    Snap = json.loads(H.Ctx.Db.One("SELECT purchase_json FROM order_lines")["purchase_json"])
    assert Snap["promotion"]["regular_unit_price"] == 89.0 and Snap["price"]["pricing_version"].endswith("-promo")
    _S, Html = OrderConfirmationEmail(O)
    assert "Christmas price · regular $89.00, you save $20.00 each" in Html
    AdminLine = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()["lines"][0]
    assert (AdminLine["unit_price"], AdminLine["promotion"]["regular_unit_price"], AdminLine["promotion"]["claims"]) == (69.0, 89.0, True)
    # The promotion off: regular prices again — the order keeps what it was placed at
    await Promotion(H, enabled=False)
    assert (await Quote(H, "silver"))["unit_price"] == 89.0
    Mine = (await H.Client.get(f"/api/orders/{O['ref']}")).json()
    assert (Mine["lines"][0]["unit_price"], Mine["lines"][0]["promotion"]["regular_unit_price"]) == (69.0, 89.0)


async def test_the_promotion_starts_and_ends_by_the_clock_and_a_bag_priced_before_is_reviewed(HC):
    H = HC
    Now_ = datetime.now(timezone.utc)
    State = await Promotion(H, starts_at=Iso(Now_ + timedelta(days=1)))
    assert State["promotion_state"] == "scheduled" and (await Quote(H, "stainless_steel"))["unit_price"] == 59.0
    Book = H.Ctx.CharmPrices
    assert Book.QuoteFor("stainless_steel", 18, At=Now_ + timedelta(days=2)).unit_price == 39.0     # once it starts
    await Promotion(H, ends_at=Iso(Now_ + timedelta(days=30)), Ok=True)
    _B, C = await CharmCustomize(H, Material="stainless_steel")
    assert (await H.Client.post("/api/bag", json={"customization_id": C["id"]})).status_code == 200
    Before = (await H.Client.post("/api/checkout/quote", json={})).json()
    assert Book.QuoteFor("stainless_steel", 18, At=Now_ + timedelta(days=31)).unit_price == 59.0   # after its end
    # It ended (its end in the past now): the bag says the price changed, checkout charges the regular price and an order
    # confirmed at the old total is refused — the customer reviews it
    await Promotion(H, starts_at=Iso(Now_ - timedelta(days=2)), ends_at=Iso(Now_ - timedelta(seconds=1)))
    assert (await H.Client.get("/api/admin/charm-prices", headers=Admin)).json()["promotion_state"] == "ended"
    Line = (await H.Client.get("/api/bag")).json()["lines"][0]
    assert Line["price_is_stale"] is True and Line["current_unit_price"] == 59.0 and "promotion" not in Line
    R = await H.Client.post("/api/orders", json={"customer": Customer, "address": Address, "terms_accepted": True,
                                                 "client_request_id": "late", "expected_total": Before["total"]})
    assert R.status_code == 409 and R.json()["error"]["code"] == "bag_changed"


async def test_a_size_shown_later_is_not_in_the_promotion_unless_given_a_promotional_price(HC):
    H = HC
    await Promotion(H, **Verified)
    await H.Client.put("/api/admin/products/charm-sizes", json={"sizes": [18, 22, 26], "enabled": [18, 22]}, headers=Admin)
    Q22 = await Quote(H, "stainless_steel", 22)
    assert Q22["unit_price"] == 69.0 and "promotion" not in Q22
    C = (await H.Client.get("/api/admin/charm-prices", headers=Admin)).json()["coverage"]
    assert C["all"] is False and [X["label"] for X in C["not_eligible"]] == ["22 mm Stainless Steel", "22 mm Sterling Silver"]


async def test_promotional_prices_and_the_promotion_are_checked(HC):
    H = HC
    for Bad in ({"stainless_steel": {**Regular["stainless_steel"], "promo_prices": {"18": 59}}},       # not below the regular
                {"stainless_steel": {**Regular["stainless_steel"], "promo_prices": {"18": 0}}},
                {"stainless_steel": {**Regular["stainless_steel"], "promo_prices": {"30": 20}}},       # not a size on offer
                {"gold_14k_yellow": {"promo_prices": {"18": 20}}}):
        await Prices(H, {**Regular, **Bad}, Ok=False)
    await Promotion(H, starts_at="2026-12-31T00:00:00Z", ends_at="2026-12-01T00:00:00Z", Ok=False)
    await Promotion(H, starts_at="2026-12-01", Ok=False)                                               # no time zone
    # Editing only the regular prices keeps the promotional ones
    T = await Prices(H, {M: dict(R) for M, R in Regular.items()})
    assert {R["id"]: R["promo_prices"] for R in T["rows"]}["silver"] == {"18": 69.0}
    assert [H_["note"] for H_ in T["history"]][:1] == ["test"]


async def test_the_banner_never_claims_more_than_the_promotion_gives(HC):
    H = HC
    await SetBanner(H, link="https://example.com", Ok=False)
    await SetBanner(H, link="//example.com", Ok=False)
    await SetBanner(H, text="", Ok=False)
    State = await SetBanner(H)
    assert State["banner"]["usd"]["why"] == "The charm promotion is off."
    assert "banner" not in (await H.Client.get("/api/catalog")).json() and 'id="promo-banner"' not in (await H.Client.get("/")).text
    await Promotion(H)                                                      # active, not verified: the text without a saving
    assert (await H.Client.get("/api/catalog")).json()["banner"] == {"text": Banner["plain_text"], "link": "/design", "until": None, "snow": True}
    await Promotion(H, **Verified)
    Cat = (await H.Client.get("/api/catalog")).json()
    assert Cat["banner"]["text"] == Banner["text"]
    Page = (await H.Client.get("/")).text
    assert 'id="promo-banner"' in Page and "A little holiday magic · Save $20 on charms" in Page and 'href="/design"' in Page
    # the falling snow on both sides (13 Nov 2026): decoration only, on by default, off with its switch
    assert Page.count('class="promo-snow promo-snow-') == 2 and Page.count('<i style="--l:') + Page.count('<i class=') >= 36
    Left = Page[Page.index('class="promo-snow promo-snow-l"'):Page.index('class="promo-snow promo-snow-r"')]
    assert Left.count("<i") == 18 and Left.count('class="c') == 4 and Left.count('class="d"') + Left.count('class="c d"') == 7
    assert "@media (prefers-reduced-motion: reduce) { .promo-snow i { animation: none;" in Page        # still, not gone
    await SetBanner(H, snow=False)
    assert "promo-snow promo-snow-" not in (await H.Client.get("/")).text
    await SetBanner(H)
    # A visitor reading shekels or dollars including VAT: never the dollar saving
    Ils = H.Ctx.IlsPricing
    M = Market(rate=D("3.25"), vat_pct=D("18"), stage="after_vat", ending="9", info={})
    from p3 import promobanner as PromoBanner
    assert PromoBanner.Shown(H.Ctx, M)["text"] == Banner["plain_text"]
    assert Ils is not None
    # Its end: the earlier of the banner's and the promotion's
    End = Iso(datetime.now(timezone.utc) + timedelta(days=3))
    await Promotion(H, ends_at=End, **Verified)
    assert (await H.Client.get("/api/catalog")).json()["banner"]["until"] == End
    # Not every charm on offer in the promotion: no banner, unless the text names its scope
    await H.Client.put("/api/admin/products/charm-sizes", json={"sizes": [18, 22, 26], "enabled": [18, 22]}, headers=Admin)
    State = (await H.Client.get("/api/admin/charm-prices", headers=Admin)).json()
    assert State["banner"]["usd"]["shown"] is None and "22 mm Stainless Steel" in State["banner"]["usd"]["why"]
    await SetBanner(H, names_scope=True, text="A little holiday magic · Save $20 on 18 mm charms")
    assert (await H.Client.get("/api/catalog")).json()["banner"]["text"] == "A little holiday magic · Save $20 on 18 mm charms"
    # The promotion off, or the banner's own dates: no banner
    await Promotion(H, enabled=False)
    assert "banner" not in (await H.Client.get("/api/catalog")).json()
    await Promotion(H, **Verified)
    await SetBanner(H, names_scope=True, starts_at=Iso(datetime.now(timezone.utc) + timedelta(days=1)))
    assert "banner" not in (await H.Client.get("/api/catalog")).json()
    Log = (await H.Client.get("/api/admin/charm-prices", headers=Admin)).json()["banner"]["log"]
    assert len(Log) >= 3 and Log[0]["value"]["names_scope"] is True


async def test_a_promo_code_applies_on_top_of_a_promotional_price_and_on_rings(HC):
    """Owner, 13 Nov 2026: promo codes combine with the promotion — the discount is taken from the promotional price."""
    H = HC
    await Promotion(H, **Verified)
    assert (await H.Client.post("/api/admin/promo-codes", headers=Admin, json={"code": "XMAS10", "kind": "percent", "value": 10,
                                                                       "active": True})).status_code in (200, 201)
    _B, C = await CharmCustomize(H, Material="stainless_steel")
    await H.Client.post("/api/bag", json={"customization_id": C["id"]})
    Q = (await H.Client.post("/api/checkout/quote", json={"promo_code": "XMAS10"})).json()
    assert Q["promo_error"] is None and Q["promo"]["eligible_subtotal"] == 39.0
    assert Q["subtotal"] == 39.0 and Q["discount"] == 3.9 and Q["total"] == 35.1
    assert Q["lines"][0]["promotion"]["regular_unit_price"] == 59.0                    # the promotion still shows
    # with a ring in the bag the code discounts both
    B = await H.NewDesign(product="ring")
    R = await H.Proceed(B["design_id"], B["candidates"][0]["id"])
    await H.Idle()
    await H.Client.patch(f"/api/customizations/{R['id']}", json={"ring_size": 7, "material_id": "silver"})
    await H.Client.post("/api/bag", json={"customization_id": R["id"]})
    Q = (await H.Client.post("/api/checkout/quote", json={"promo_code": "XMAS10"})).json()
    assert Q["subtotal"] == 239.0 and Q["promo"]["eligible_subtotal"] == 239.0 and Q["discount"] == 23.9 and Q["total"] == 215.1
    # the order charges exactly what the quote showed, keeps the promotion on the charm line and counts one use
    O = await H.Client.post("/api/orders", json={"customer": Customer, "address": Address, "shipping_method": "standard",
                                                 "terms_accepted": True, "client_request_id": "stack-1", "promo_code": "XMAS10"})
    assert O.status_code == 200, O.text
    O = O.json()
    assert (O["subtotal"], O["discount"], O["total"]) == (239.0, 23.9, 215.1)
    Charm = [L for L in O["lines"] if L["unit_price"] == 39.0]
    assert Charm and Charm[0]["promotion"]["regular_unit_price"] == 59.0
    Codes = (await H.Client.get("/api/admin/promo-codes", headers=Admin)).json()
    assert [X["usage_count"] for X in (Codes.get("promo_codes") or Codes.get("codes")) if X["code"] == "XMAS10"] == [1]


def test_a_promotion_in_shekels_converts_both_prices_by_the_rule_and_the_saving_is_their_difference():
    M = Market(rate=D("3.25"), vat_pct=D("18"), stage="after_vat", ending="9", info={})
    P = {"label": "Christmas price", "regular_unit_price": 59.0, "savings": 20.0, "claims": True}
    Out = LocalizePromotion(P, M.Unit(39.0), M)
    # each price rounded UP to …9 (Rule 2): ₪229 and ₪159 — a saving of ₪70, not $20 converted
    assert (M.Unit(59.0), M.Unit(39.0)) == (229.0, 159.0) and Out["regular_unit_price"] == 229.0 and Out["savings"] == 70.0
    assert Out["usd_regular_unit_price"] == 59.0 and Out["usd_savings"] == 20.0
    assert LocalizePromotion({"label": "x", "claims": False}, 149.0, M) == {"label": "x", "claims": False}   # nothing to convert
    assert CharmPrices.PromotionState({"enabled": True, "ends_at": "2020-01-01T00:00:00+00:00"}) == "ended"
