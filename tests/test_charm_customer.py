"""Charms, phase 5 — the customer experience. While charms are hidden a customer gets exactly the ring-only site and
API answers of before; with charms shown (or an Admin previewing) the Design screen offers Ring / Charm, and every
answer carries the product. A customer who already holds charms keeps seeing them as they are."""

import re

import httpx
import pytest

from tests.conftest import Harness, OfferCharmSizes
from tests.test_orders import Address, Customer

AdminKey = "charm-customer-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
ProductKeys = {"product_type", "product_types", "charm_size", "size_label"}


@pytest.fixture
async def HC5(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    await OfferCharmSizes(Obj, AdminKey)                                     # 15–30 mm, as these tests were written
    yield Obj
    await Obj.Close()


def Keys(X) -> set:
    """Every key anywhere in a JSON answer."""
    if isinstance(X, dict):
        return set(X) | set().union(*(Keys(V) for V in X.values())) if X else set()
    if isinstance(X, list):
        return set().union(*(Keys(V) for V in X)) if X else set()
    return set()


async def RingOrder(H):
    B = await H.NewDesign()
    C = await H.Proceed(B["design_id"], B["candidates"][0]["id"])
    await H.Idle()
    await H.Client.patch(f"/api/customizations/{C['id']}", json={"ring_size": 7})
    await H.Client.post("/api/bag", json={"customization_id": C["id"]})
    return B, C


async def Answers(H, Did):
    return {"designs": (await H.Client.get("/api/designs")).json(), "design": (await H.Client.get(f"/api/designs/{Did}")).json(),
            "bag": (await H.Client.get("/api/bag")).json(), "checkout": (await H.Client.get("/api/checkout")).json(),
            "quote": (await H.Client.post("/api/checkout/quote", json={})).json()}


async def test_a_ring_customer_gets_the_ring_only_answers_while_charms_are_hidden(HC5):
    H = HC5
    B, C = await RingOrder(H)
    A = await Answers(H, B["design_id"])
    for Name, Doc in A.items():
        assert not ProductKeys & Keys(Doc), (Name, ProductKeys & Keys(Doc))
    O = (await H.Client.post("/api/orders", json={"customer": Customer, "address": Address, "terms_accepted": True,
                                                  "client_request_id": "o1"})).json()
    assert not ProductKeys & Keys(O) and not ProductKeys & Keys((await H.Client.get("/api/orders")).json())
    assert not ProductKeys & Keys((await H.Client.get(f"/api/orders/{O['id']}")).json())
    Gold = await H.Client.post("/api/quote-requests", json={"design_id": B["design_id"], "candidate_id": C["candidate_id"],
                                                            "material_id": "gold_14k_yellow", "ring_size": 7, "customer": Customer})
    assert Gold.status_code == 200 and not ProductKeys & Keys(Gold.json())
    # The Admin always sees the product, and the order keeps its snapshot
    L = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()["lines"][0]
    assert (L["product_type"], L["size_label"]) == ("ring", "US 7")
    # An Admin previewing charms gets the product fields on the same customer answers
    assert (await H.Client.post("/api/admin/login", json={"key": AdminKey})).status_code == 200
    A = await Answers(H, B["design_id"])
    assert A["design"]["product_type"] == "ring" and A["designs"]["designs"][0]["product_type"] == "ring"
    assert O["ref"] in [X["ref"] for X in (await H.Client.get("/api/orders")).json()["orders"]]
    assert (await H.Client.get("/api/orders")).json()["orders"][0]["product_types"] == ["ring"]


async def test_a_customer_who_holds_charms_keeps_seeing_them_after_charms_are_hidden(HC5):
    H = HC5
    On = await H.Client.put("/api/admin/products/availability", json={"charms_available": True}, headers=Admin)
    assert On.json()["charms_available"] is True
    await H.Client.put("/api/admin/charm-prices", json={"materials": {"silver": {"fixed_prices": {"20": 145}}}}, headers=Admin)
    B = await H.NewDesign("A crescent moon charm", product="charm")
    C = await H.Proceed(B["design_id"], B["candidates"][0]["id"])
    await H.Idle()
    await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": 20})
    await H.Client.post("/api/bag", json={"customization_id": C["id"]})
    O = (await H.Client.post("/api/orders", json={"customer": Customer, "address": Address, "terms_accepted": True,
                                                  "client_request_id": "c1"})).json()
    assert O["product_types"] == ["charm"] and O["lines"][0]["size_label"] == "20 mm"
    await H.Client.put("/api/admin/products/availability", json={"charms_available": False}, headers=Admin)
    # Charms hidden again: no new charm, no charm in the catalog or gallery — but this customer's order still reads right
    assert "products" not in (await H.Client.get("/api/catalog")).json()
    Mine = (await H.Client.get("/api/orders")).json()["orders"][0]
    assert Mine["product_types"] == ["charm"] and Mine["lines"][0]["size_label"] == "20 mm"
    assert (await H.Client.get(f"/api/designs/{B['design_id']}")).status_code == 404       # the design itself stays hidden


async def test_the_customer_page_offers_charms_only_through_the_catalog(HC5):
    H = HC5
    Page = (await H.Client.get("/")).text
    # Products are offered only while the catalog lists them (productsOn); the ring-only note stays for the ring-only site
    assert 'x-show="!design && productsOn"' in Page and "What would you like to design?" in Page
    assert 'x-show="!design && !productsOn"' in Page and 'x-text="composerNote"' in Page
    assert 'id="charm-size-heading" x-show="custIsCharm"' in Page and 'id="ring-size-heading" x-show="!custIsCharm"' in Page
    assert Page.count('x-for="g in galleryShown"') == 2 and 'x-for="g in homeGallery"' in Page        # the home strip is as it was
    assert 'x-show="charmPreview"' in Page and "Admin preview" in Page
    assert re.search(r'/static/products\.js\?v=\d+', Page)
    App = (await H.Client.get("/static/app.js")).text
    assert "if (this.catalog?.products) fd.append('product', this.newProduct);" in App
    # The catalog's charm block, for an Admin previewing: charm materials with their customer names
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=H.App), base_url="http://p3.test") as Browser:
        assert (await Browser.post("/api/admin/login", json={"key": AdminKey})).status_code == 200
        Cat = (await Browser.get("/api/catalog")).json()
    assert Cat["products"]["preview"] is True
    assert [M["label"] for M in Cat["products"]["charm"]["materials"]][:3] == ["Stainless Steel", "Sterling Silver", "14K Gold Vermeil"]


async def test_promo_messages_are_product_neutral(HC5):
    H = HC5
    H.Svc.Promos.Save({"code": "VERMEIL10", "kind": "percent", "value": 10, "materials": ["vermeil"]}, "test")
    Empty = (await H.Client.post("/api/checkout/quote", json={"promo_code": "VERMEIL10"})).json()["promo_error"]
    assert Empty == {"code": "promo_no_items", "message": "Add a piece to your bag before using a promo code."}
    # A ring in the bag: the material restriction speaks of pieces, not rings
    await RingOrder(H)
    Ring = (await H.Client.post("/api/checkout/quote", json={"promo_code": "VERMEIL10"})).json()["promo_error"]
    assert Ring == {"code": "promo_not_applicable", "message": "This promo code applies to Vermeil pieces only."}
    # … and the same message for a charm
    await H.Client.put("/api/admin/products/availability", json={"charms_available": True}, headers=Admin)
    await H.Client.put("/api/admin/charm-prices", json={"materials": {"silver": {"fixed_prices": {"20": 145}}}}, headers=Admin)
    for L in (await H.Client.get("/api/bag")).json()["lines"]:
        await H.Client.delete(f"/api/bag/{L['id']}")
    B = await H.NewDesign("A heart charm", product="charm")
    C = await H.Proceed(B["design_id"], B["candidates"][0]["id"])
    await H.Idle()
    await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": 20})
    await H.Client.post("/api/bag", json={"customization_id": C["id"]})
    Charm = (await H.Client.post("/api/checkout/quote", json={"promo_code": "VERMEIL10"})).json()["promo_error"]
    assert Charm == Ring
    for Text in (Empty["message"], Ring["message"]):
        assert "ring" not in Text.lower() and "charm" not in Text.lower()
