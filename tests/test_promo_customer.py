"""Promo codes for one customer (owner, 8 Nov 2026): a code with a customer email works only for the account whose verified
email it is — checked by the server at the quote and again when the order is placed; without one it works for anyone, as
before. The minimum and a fixed amount read in the customer's currency: $ as listed, ₪ (or $ for a visitor from Israel)
including VAT."""

import pytest

from p3 import promos as Promos
from tests.conftest import Harness
from tests.test_b6_ils_pricing import AdminKey as IlsKey, Israeli, _TurnOn
from tests.test_orders import Admin, AdminKey, _InBag, _Order

Email = "dana@atelier-customer.co.il"


def _Verify(H, Address=Email):
    H.Ctx.Accounts.Db.Execute("UPDATE accounts SET email = ?, verified_at = '2026-11-08T10:00:00.000+00:00' WHERE account_id = ?",
                              (Address, H.Who.AccountId))


async def _Code(H, Key=None, **Over):
    R = await H.Client.post("/api/admin/promo-codes", json={"code": "DANA-15", "kind": "percent", "value": 15, "usage_limit": 1,
                                                            "customer_email": " Dana@Atelier-Customer.co.il ", **Over},
                            headers={"Authorization": f"Bearer {Key}"} if Key else Admin)
    assert R.status_code == 200, R.text
    return R.json()


async def test_a_code_for_one_customer_works_only_for_that_verified_account(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        P = await _Code(H)
        assert P["customer_email"] == Email and P["usage_limit"] == 1                 # stored lower case, the limit kept
        Bad = await H.Client.post("/api/admin/promo-codes", json={"code": "BAD-1", "kind": "percent", "value": 5,
                                                                  "customer_email": "not-an-email"}, headers=Admin)
        assert Bad.status_code == 400
        await _InBag(H)
        # not verified yet, then another address: refused at the quote and at the order (the server decides, not the page)
        for Setup in (lambda: None, lambda: _Verify(H, "someone.else@example.org")):
            Setup()
            Q = (await H.Client.post("/api/checkout/quote", json={"promo_code": "DANA-15"})).json()
            assert Q["promo"] is None and Q["promo_error"]["code"] == "promo_not_for_account" and Q["discount"] == 0
            R = await H.Client.post("/api/orders", json=_Order(promo_code="DANA-15"))
            assert R.status_code == 400 and R.json()["error"]["code"] == "promo_not_for_account"
        # the account with that verified email: the code applies and the order counts the use
        _Verify(H)
        Q = (await H.Client.post("/api/checkout/quote", json={"promo_code": "dana-15"})).json()
        assert Q["promo"]["code"] == "DANA-15" and Q["discount"] == pytest.approx(round(Q["subtotal"] * 0.15, 2))
        O = (await H.Client.post("/api/orders", json=_Order(promo_code="DANA-15"))).json()
        assert O["promo_code"] == "DANA-15" and O["discount"] == Q["discount"]
        Row = next(X for X in (await H.Client.get("/api/admin/promo-codes", headers=Admin)).json()["promo_codes"] if X["id"] == P["id"])
        assert Row["usage_count"] == 1                                                  # the usage limit still counts
        # cleared: any customer again
        await H.Client.patch(f"/api/admin/promo-codes/{P['id']}", json={**{K: P[K] for K in ("code", "kind", "value")},
                                                                       "customer_email": "", "usage_limit": 5}, headers=Admin)
        _Verify(H, "someone.else@example.org")
        await _InBag(H, "Plain band", Size=8)
        Q = (await H.Client.post("/api/checkout/quote", json={"promo_code": "DANA-15"})).json()
        assert Q["promo"] and Q["promo_error"] is None
    finally:
        await H.Close()


async def test_the_service_refuses_a_code_for_another_or_no_account(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        S = Promos.PromoService(H.Ctx)
        S.Save({"code": "ONLY-YOU", "kind": "percent", "value": 10, "customer_email": Email}, "test")
        S.Save({"code": "ANYONE", "kind": "percent", "value": 10}, "test")
        Lines = [{"material_id": "silver", "line_total": 100.0}]
        for Who in (None, "", "other@example.org"):
            with pytest.raises(Promos.PromoError) as E:
                S.Evaluate("ONLY-YOU", Lines, AccountEmail=Who)
            assert E.value.Code == "promo_not_for_account"
        assert S.Evaluate("ONLY-YOU", Lines, AccountEmail="DANA@atelier-customer.co.il")["discount"] == 10.0
        assert S.Evaluate("ANYONE", Lines)["discount"] == 10.0
    finally:
        await H.Close()


async def test_the_minimum_and_a_fixed_discount_read_in_the_customers_currency(tmp_path):
    H = Harness(tmp_path, AdminKey=IlsKey)                                              # the key _TurnOn signs in with
    try:
        await _Code(H, IlsKey, code="BIG-ORDER", kind="fixed", value=10, customer_email="", min_subtotal=1000, usage_limit=None)
        await _Code(H, IlsKey, code="TEN-OFF", kind="fixed", value=10, customer_email="", usage_limit=None)
        await _InBag(H)
        Usd = (await H.Client.post("/api/checkout/quote", json={"promo_code": "BIG-ORDER"})).json()
        assert Usd["promo_error"]["message"] == "This promo code needs a subtotal of at least $1,000."
        await _TurnOn(H)
        Ils = (await H.Client.post("/api/checkout/quote", json={"promo_code": "BIG-ORDER", "country": "IL"})).json()
        M = H.Ctx.IlsPricing.MarketFor("ILS")
        assert Ils["promo_error"]["message"] == f"This promo code needs a subtotal of at least ₪{M.Plain(1000):,.2f} including VAT."
        Fixed = (await H.Client.post("/api/checkout/quote", json={"promo_code": "TEN-OFF", "country": "IL"})).json()
        assert Fixed["currency"] == "ILS" and Fixed["discount"] == pytest.approx(M.Plain(10))   # ₪ including VAT
        assert Fixed["promo"]["label"] == f"₪{M.Plain(10):,.2f} off" and Fixed["vat_amount"] > 0
        assert Fixed["total"] == pytest.approx(Fixed["subtotal"] - Fixed["discount"] + Fixed["shipping"])
        # a visitor from Israel who chose $: dollars including VAT; delivery abroad: plain dollars
        UsdVat = (await H.Client.post("/api/checkout/quote", json={"promo_code": "TEN-OFF"}, headers=Israeli,
                                      cookies={"p3_currency": "USD"})).json()
        assert UsdVat["currency"] == "USD" and UsdVat["discount"] == pytest.approx(11.8) and UsdVat["promo"]["label"] == "$11.80 off"
        Min = (await H.Client.post("/api/checkout/quote", json={"promo_code": "BIG-ORDER"}, headers=Israeli,
                                   cookies={"p3_currency": "USD"})).json()
        assert Min["promo_error"]["message"] == "This promo code needs a subtotal of at least $1,180.00 including VAT."
        Abroad = (await H.Client.post("/api/checkout/quote", json={"promo_code": "TEN-OFF", "country": "US"})).json()
        assert Abroad["currency"] == "USD" and Abroad["discount"] == 10.0 and "vat_amount" not in Abroad
    finally:
        await H.Close()
