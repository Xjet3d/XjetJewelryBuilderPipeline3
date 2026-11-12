"""B6 — a visitor from Israel who chooses $ on the ₪/$ switch reads US dollars including Israeli VAT, to the cent (owner,
4 Nov 2026: "the price in $ is without 18% VAT; if you give me the option we need 18% VAT"). A price to read only: the
delivery country still decides what is charged — Israel in ₪ including VAT, anywhere else in $ without Israeli VAT."""

from decimal import ROUND_HALF_UP, Decimal

from tests.conftest import Harness
from tests.test_b6_ils_pricing import AdminKey, Israeli, _InBag, _Order, _TurnOn

Dollars = {"p3_currency": "USD"}


def _WithVat(Usd) -> float:
    return float((Decimal(str(Usd)) * Decimal("1.18")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


async def test_a_visitor_from_israel_who_chooses_dollars_reads_them_including_vat(tmp_path, monkeypatch):
    # a priced delivery method converts by the same rule (Express is retired for customers since 12 Nov 2026; offered here only to
    # keep the conversion of a priced method under test)
    from p3 import orders as OrdersModule
    monkeypatch.setattr(OrdersModule, "ShippingOptions", dict(OrdersModule.ShippingMethods))
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _TurnOn(H)
        List = (await H.Client.get("/api/quote", params={"material_id": "silver"})).json()
        Q = (await H.Client.get("/api/quote", params={"material_id": "silver"}, headers=Israeli, cookies=Dollars)).json()
        assert (Q["currency"], Q["incl_vat"], Q["unit_price"]) == ("USD", True, _WithVat(List["unit_price"]))
        assert Q["usd_unit_price"] == List["unit_price"]
        Cat = (await H.Client.get("/api/catalog", headers=Israeli, cookies=Dollars)).json()
        assert Cat["pricing"] == {"currency": "USD", "choices": ["ILS", "USD"], "incl_vat": True, "ils_ready": True}
        await _InBag(H, **Israeli)
        Bag = (await H.Client.get("/api/bag", headers=Israeli, cookies=Dollars)).json()
        assert list(Bag["totals"]) == ["USD"] and Bag["incl_vat"] is True and Bag["lines"][0]["unit_price"] == Q["unit_price"]
        # before an address: $ including VAT, with the VAT inside the total; express converted the same way
        Co = (await H.Client.post("/api/checkout/quote", json={"shipping_method": "express"}, headers=Israeli, cookies=Dollars)).json()
        assert (Co["currency"], Co["incl_vat"], Co["shipping"]) == ("USD", True, _WithVat(45))
        assert Co["vat_amount"] == round(Co["total"] - Co["total"] / 1.18, 2)
        # the delivery country decides: Israel ₪, abroad $ without Israeli VAT
        Home = (await H.Client.post("/api/checkout/quote", json={"country": "IL"}, headers=Israeli, cookies=Dollars)).json()
        Abroad = (await H.Client.post("/api/checkout/quote", json={"country": "US", "shipping_method": "express"},
                                      headers=Israeli, cookies=Dollars)).json()
        assert Home["currency"] == "ILS" and Home["incl_vat"] is True
        assert Abroad["currency"] == "USD" and "vat_amount" not in Abroad and Abroad["shipping"] == 45.0
    finally:
        await H.Close()


async def test_dollars_stay_plain_for_a_visitor_from_elsewhere_and_while_israeli_pricing_is_off(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Off = (await H.Client.get("/api/quote", params={"material_id": "silver"}, headers=Israeli, cookies=Dollars)).json()
        assert Off["currency"] == "USD" and "incl_vat" not in Off                   # off: nothing changes
        await _TurnOn(H)
        Elsewhere = (await H.Client.get("/api/quote", params={"material_id": "silver"}, headers={"CF-IPCountry": "DE"},
                                        cookies=Dollars)).json()
        NoHeader = (await H.Client.get("/api/quote", params={"material_id": "silver"}, cookies=Dollars)).json()
        for Q in (Elsewhere, NoHeader):
            assert Q["currency"] == "USD" and "incl_vat" not in Q and Q["unit_price"] == Off["unit_price"]
    finally:
        await H.Close()


async def test_an_order_is_never_charged_in_dollars_including_vat(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _TurnOn(H)
        await _InBag(H, **Israeli)
        Home = (await H.Client.post("/api/orders", json=_Order(), headers=Israeli, cookies=Dollars)).json()
        assert Home["currency"] == "ILS" and Home["vat_amount"]                     # delivered to Israel: shekels
        await _InBag(H, **Israeli)
        Abroad = _Order(client_request_id="click-2", address={"recipient": "Dana Levi", "line1": "1 Main St", "line2": "",
                                                              "city": "Austin", "region": "TX", "postal_code": "73301",
                                                              "country": "US"})
        O = (await H.Client.post("/api/orders", json=Abroad, headers=Israeli, cookies=Dollars)).json()
        assert O["currency"] == "USD" and "vat_amount" not in O                     # delivered abroad: plain dollars
    finally:
        await H.Close()
