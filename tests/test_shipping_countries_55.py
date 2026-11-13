"""55 shipping countries (owner, 13 Nov 2026): the 12 the UPS check marked "Delete from atelier" are no longer offered.

Checkout lists exactly the 55, Israel included; an order or a quote approval to one of the 12 is refused with a clear
message (nothing is placed); an order already addressed to one keeps its country and shows its name."""

import pytest

from p3 import addressing as Addressing
from tests.conftest import Harness
from tests.test_orders import AdminKey, Address, _InBag, _Order

Admin = {"Authorization": f"Bearer {AdminKey}"}
Removed = {"TR", "AE", "SA", "QA", "KW", "BH", "OM", "JO", "EG", "MA", "MY", "ID"}
Kept55 = ["US", "CA", "GB", "IE", "IL", "DE", "FR", "IT", "ES", "PT", "NL", "BE", "LU", "CH", "AT", "DK", "SE", "NO", "FI",
          "IS", "PL", "CZ", "SK", "HU", "RO", "BG", "GR", "CY", "MT", "HR", "SI", "EE", "LV", "LT", "ZA", "KE", "NG", "IN",
          "SG", "TH", "VN", "PH", "JP", "KR", "TW", "HK", "CN", "AU", "NZ", "MX", "BR", "AR", "CL", "CO", "PE"]


@pytest.fixture
async def H(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    yield Obj
    await Obj.Close()


def test_the_list_is_the_55_and_the_12_are_retired():
    assert [C for C, _ in Addressing.Countries] == Kept55 and len(Kept55) == 55
    assert set(Addressing.RetiredCountries) == Removed and not Removed & set(Addressing.CountryNames)
    assert set(Addressing.DialCodes) == set(Kept55)


async def test_checkout_offers_the_55_and_refuses_the_12(H):
    Info = (await H.Client.get("/api/checkout")).json()
    assert [C["code"] for C in Info["countries"]] == Kept55
    for Code in sorted(Removed):
        V = (await H.Client.post("/api/checkout/address", json={"address": {**Address, "country": Code}})).json()
        P = [X for X in V["problems"] if X["field"] == "country"]
        assert P and "We don't ship to" in P[0]["message"] and Addressing.RetiredCountries[Code] in P[0]["message"], Code
    await _InBag(H)
    R = await H.Client.post("/api/orders", json=_Order(address={**Address, "country": "AE", "postal_code": ""}))
    assert R.status_code == 400 and R.json()["error"]["problems"][0]["field"] == "country"
    assert (await H.Client.get("/api/orders")).json()["orders"] == []
    O = (await H.Client.post("/api/orders", json=_Order())).json()          # Israel still ships
    assert O["address"]["country"] == "IL" and O["address_lines"][-1] == "Israel"


async def test_an_order_already_addressed_to_a_retired_country_keeps_it(H):
    await _InBag(H)
    O = (await H.Client.post("/api/orders", json=_Order())).json()
    H.Ctx.Db.Execute("UPDATE orders SET shipping_json = json_set(shipping_json, '$.country', 'AE') WHERE id = ?", (O["id"],))
    Mine = (await H.Client.get(f"/api/orders/{O['id']}")).json()
    assert Mine["address"]["country"] == "AE" and Mine["address_lines"][-1] == "United Arab Emirates"
    Ad = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()
    assert Ad["address_lines"][-1] == "United Arab Emirates"
