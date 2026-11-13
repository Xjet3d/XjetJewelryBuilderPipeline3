"""The charm sizes customers choose from (owner, 12 Nov 2026 — Task 1, a single charm size of 18 mm on proto).

Admin → Settings → Products → Charm sizes keeps the three priced sizes (their ranks are the price levels) and says which
of them are "Shown to customers" — at least one. One alone: Customize selects it and shows no size choice; the site names
that size. Two or more: the size choice as before. Switching a size off never changes any price; a charm saved, bagged or
ordered in another size keeps it (an order is never rewritten). Nothing is fixed to 18 mm: everything follows the Admin."""

import json
import re
from pathlib import Path

import pytest

from tests.conftest import Harness, OfferCharmSizes
from tests.test_charm_pricing import Admin, AdminKey, CharmCustomize, Prices, SetCharmPrices
from tests.test_orders import Address, Customer

Web = Path(__file__).resolve().parent.parent / "web"


@pytest.fixture
async def HZ(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    assert (await Obj.Client.post("/api/admin/login", json={"key": AdminKey})).status_code == 200
    Obj.Client.headers["X-P3-Preview"] = "1"
    assert (await Obj.Client.put("/api/admin/products/availability", json={"product": "charm", "available": True},
                                 headers=Admin)).status_code == 200
    await OfferCharmSizes(Obj, AdminKey)                         # 14 / 18 / 22 mm: small · medium · large
    await SetCharmPrices(Obj, Prices)                            # Sterling Silver: 18 mm 145, 22 mm 165
    yield Obj
    await Obj.Close()


async def Shown(H, Enabled, Sizes=(14, 18, 22), **More):
    R = await H.Client.put("/api/admin/products/charm-sizes", json={"sizes": list(Sizes), "enabled": Enabled, **More},
                           headers=Admin)
    assert R.status_code == 200, R.text
    return R.json()


async def Quote(H, Size):
    return (await H.Client.get(f"/api/quote?material_id=silver&product=charm&charm_size={Size}")).json()


async def test_one_size_shown_is_selected_for_the_customer_at_its_own_price(HZ):
    H = HZ
    Rings = (await H.Client.get("/api/catalog")).json()["ring_sizes"]
    assert (await Quote(H, 18))["unit_price"] == 145.0
    P = await Shown(H, [18], note="one size for launch")
    assert P["charm_sizes"] == [14.0, 18.0, 22.0] and P["charm_sizes_enabled"] == [18.0] and P["charm_default_size"] == 18.0
    assert [(O["size"], O["enabled"], O["level"]) for O in P["charm_size_options"]] == \
        [(14.0, False, "1"), (18.0, True, "2"), (22.0, False, "3")]                      # the Admin sees every size
    Log = next(L for L in P["log"] if L["key"] == "charm_sizes_enabled")
    assert Log["value"] == [18.0] and Log["note"] == "one size for launch"
    Cat = (await H.Client.get("/api/catalog")).json()
    C = Cat["products"]["charm"]
    assert C["sizes"] == [18.0] and [O["size"] for O in C["size_options"]] == [18.0] and C["recommended_size"] == 18.0
    Silver = next(M for M in C["materials"] if M["id"] == "silver")
    assert [(X["size"], X["price"]) for X in Silver["prices"]] == [(18.0, 145.0)]
    assert Cat["ring_sizes"] == Rings                                                     # ring sizing is untouched
    # The price is the one it had: 18 mm is still the medium size of the three
    assert (await Quote(H, 18))["unit_price"] == 145.0
    Q22 = await Quote(H, 22)
    assert Q22["pricing_status"] == "unavailable" and Q22["unavailable_reason"] == "charm_size_not_offered"
    # Customize: that size, selected, priced and ready for the bag — no choice to make
    _B, Cz = await CharmCustomize(H)
    assert Cz["charm_size"] == 18.0 and Cz["size_label"] == "18 mm" and [S["size"] for S in Cz["charm_sizes"]] == [18.0]
    assert Cz["size_confirmed"] is True and Cz["can_add_to_bag"] is True and Cz["quote"]["unit_price"] == 145.0
    Bad = await H.Client.patch(f"/api/customizations/{Cz['id']}", json={"charm_size": 22})
    assert Bad.status_code == 400 and Bad.json()["error"]["code"] == "invalid_charm_size"
    assert (await H.Client.post("/api/bag", json={"customization_id": Cz["id"]})).status_code == 200
    Line = (await H.Client.get("/api/bag")).json()["lines"][0]
    assert (Line["size_label"], Line["unit_price"], Line["orderable"]) == ("18 mm", 145.0, True) and "size_not_offered" not in Line
    # The page tells the app the sizes before the catalog arrives
    Html = (await H.Client.get("/materials")).text
    Config = json.loads(re.search(r"<script>window.__p3 = (.*?);</script>", Html).group(1))
    assert Config["charm_sizes"] == [18.0]


async def test_switching_a_size_off_never_moves_another_sizes_price(HZ):
    H = HZ
    await Shown(H, [22])                                       # only the large size shown: still at the large price
    assert (await Quote(H, 22))["unit_price"] == 165.0
    assert (await Quote(H, 18))["unavailable_reason"] == "charm_size_not_offered"
    T = (await H.Client.get("/api/admin/charm-prices", headers=Admin)).json()
    assert T["sizes"] == [14.0, 18.0, 22.0] and T["sizes_enabled"] == [22.0]
    assert {R["id"]: R["fixed_prices"] for R in T["rows"]}["silver"] == {"18": 145.0, "22": 165.0}    # the table as it was


async def test_two_or_more_sizes_shown_bring_the_size_choice_back(HZ):
    H = HZ
    await Shown(H, [18])
    P = await Shown(H, [18, 22])
    assert P["charm_sizes_enabled"] == [18.0, 22.0]
    assert (await H.Client.get("/api/catalog")).json()["products"]["charm"]["sizes"] == [18.0, 22.0]
    _B, C = await CharmCustomize(H)
    assert [S["size"] for S in C["charm_sizes"]] == [18.0, 22.0] and C["charm_size"] == 18.0     # the middle of 14/18/22 is shown
    C22 = (await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": 22})).json()
    assert C22["charm_size"] == 22.0 and C22["quote"]["unit_price"] == 165.0
    # Saving the sizes without saying which are shown keeps them; all three shown again is stored as "all" (null)
    R = await H.Client.put("/api/admin/products/charm-sizes", json={"sizes": [14, 18, 22]}, headers=Admin)
    assert R.json()["charm_sizes_enabled"] == [18.0, 22.0]
    P = await Shown(H, [14, 18, 22])
    assert P["charm_sizes_enabled"] == [14.0, 18.0, 22.0] and H.Ctx.Products.Get("charm_sizes_enabled") is None
    assert (await H.Client.get("/api/catalog")).json()["products"]["charm"]["sizes"] == [14.0, 18.0, 22.0]


async def test_at_least_one_size_on_offer_is_shown(HZ):
    H = HZ
    for Bad in ([], [30], [26], [18, "x"], "18", None):
        R = await H.Client.put("/api/admin/products/charm-sizes", json={"sizes": [14, 18, 22], "enabled": Bad}, headers=Admin)
        assert R.status_code == 400 and R.json()["error"]["code"] == "invalid_charm_sizes_enabled", Bad
    # A recommended size customers cannot choose falls back to a shown one
    P = await Shown(H, [18], default=22)
    assert P["charm_default_size"] == 18.0


async def test_a_charm_kept_in_a_size_switched_off_keeps_it_and_the_order_is_never_rewritten(HZ):
    H = HZ
    _B, C = await CharmCustomize(H)
    await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": 22})
    assert (await H.Client.post("/api/bag", json={"customization_id": C["id"]})).status_code == 200
    O = (await H.Client.post("/api/orders", json={"customer": Customer, "address": Address, "terms_accepted": True,
                                                  "client_request_id": "before-one-size"})).json()
    assert [(L["size_label"], L["unit_price"]) for L in O["lines"]] == [("22 mm", 165.0)]
    assert (await H.Client.post("/api/bag", json={"customization_id": C["id"]})).status_code == 200    # in the bag again at 22 mm
    P = await Shown(H, [18])
    assert P["removed_in_bags"] == [{"size": 22.0, "bag_lines": 1}]
    # The bag keeps the line in its own size, says that size is no longer offered and leaves it out of the order
    Bag = (await H.Client.get("/api/bag")).json()
    L = Bag["lines"][0]
    assert (L["size_label"], L["orderable"], L["size_not_offered"]) == ("22 mm", False, True) and Bag["checkout_available"] is False
    Line = (await H.Client.post("/api/checkout/quote", json={})).json()["lines"][0]
    assert Line["problem"] == "This charm size is no longer offered — remove this line and add the charm again from Customize."
    # Customize shows the one size there is, and adding it saves that size
    Cz = (await H.Client.get(f"/api/customizations/{C['id']}")).json()
    assert Cz["charm_size"] == 18.0 and Cz["can_add_to_bag"] is True and Cz["quote"]["unit_price"] == 145.0
    assert (await H.Client.post("/api/bag", json={"customization_id": C["id"]})).status_code == 200
    assert H.Ctx.Db.One("SELECT charm_size FROM customizations WHERE id = ?", (C["id"],))["charm_size"] == 18.0
    assert sorted(L["size_label"] for L in (await H.Client.get("/api/bag")).json()["lines"]) == ["18 mm", "22 mm"]
    # The order placed before keeps its size and price, for the customer and in the database
    Mine = (await H.Client.get(f"/api/orders/{O['ref']}")).json()
    assert [(L["size_label"], L["unit_price"]) for L in Mine["lines"]] == [("22 mm", 165.0)]
    assert [R["charm_size"] for R in H.Ctx.Db.All("SELECT charm_size FROM order_lines")] == [22.0]


async def test_the_site_names_the_one_size_and_says_size_again_with_several(HZ):
    H = HZ
    # Both products on offer (rings by default)
    await Shown(H, [18])
    Faq = (await H.Client.get("/faq")).text
    assert "A charm is priced by material. A ring is priced by material — its size does not change the price." in Faq
    assert "Every charm is 18 mm tall: its total height, including the attachment eyelet at the top." in Faq
    assert "a charm is priced by material; a ring’s price does not depend on its size." in (await H.Client.get("/materials")).text
    # Charms alone
    assert (await H.Client.put("/api/admin/products/availability", json={"product": "ring", "available": False},
                               headers=Admin)).status_code == 200
    H.Client.headers.pop("X-P3-Preview")                        # a customer's view: charms only
    Faq = (await H.Client.get("/faq")).text
    assert "Fashion jewelry pieces are priced per piece by material." in Faq and "by material and size" not in Faq
    assert "Every charm is 18 mm tall: its total height, including the attachment eyelet at the top." in Faq
    Mat = (await H.Client.get("/materials")).text
    assert "Fashion metals have a fixed price per charm, set by material — any design." in Mat
    assert "Prices are per charm, fixed for the material." in Mat and "material and size" not in Mat
    assert "Every charm is 18 mm tall, including the attachment eyelet — ask us if you have any questions about size." in \
        (await H.Client.get("/contact")).text
    Home = (await H.Client.get("/")).text
    assert '<span x-text="charmFacts.sizesLabel">Size</span><b x-text="charmFacts.sizes">18 mm</b>' in Home     # before the app runs
    # Several sizes again: the size wording is back
    await Shown(H, [18, 22])
    Faq = (await H.Client.get("/faq")).text
    assert "Fashion jewelry pieces are priced per piece by material and size." in Faq
    assert "A charm size is its total height in millimeters, including the attachment eyelet at the top." in Faq
    assert "Fashion metals have a fixed price per charm, set by material and size — any design." in (await H.Client.get("/materials")).text
    assert '<span x-text="charmFacts.sizesLabel">Sizes</span><b x-text="charmFacts.sizes">18 or 22 mm</b>' in (await H.Client.get("/")).text


def test_customize_hides_the_size_choice_with_one_size_and_explains_the_size_collapsed():
    Html, App, Adm = ((Web / F).read_text(encoding="utf-8") for F in ("index.html", "app.js", "admin.html"))
    assert '<div id="charm-size-heading" x-show="custSizeChoice" x-cloak>' in Html
    assert "get custSizeChoice() { return this.custIsCharm && (this.cust?.charm_sizes || []).length > 1; }" in App
    # About Charm Size (Boutique, 13 Nov 2026): the Size section — "Size · 18 mm total height" with one size, "About Charm
    # Size" with several — after the size choice, collapsed whenever a customization opens
    Choice = Html.index('<div id="charm-size-heading" x-show="custSizeChoice" x-cloak>')
    About = Html.index('data-testid="charm-size-about"')
    assert Choice < About < Html.index('data-testid="add-to-bag"')
    Section = Html[About:About + 2500]
    assert "custSizeChoice ? 'About Charm Size' : 'Size'" in Section and "(cust?.size_label || '') + ' total height'" in Section
    assert 'x-show="custPanel === \'size\'"' in Section and 'x-text="charmSizeAbout"' in Section
    assert "custPanel: ''," in App and "if (!same) this.custPanel = '';" in App
    Start = App.index("get charmSizeAbout()")
    Text = App[Start:App.index("\n    },", Start)]
    assert "attachment eyelet" in Text and "total height" in Text
    assert not re.search(r"chain|bracelet|necklace", Text, re.I)                          # no compatibility claims
    assert "18" not in Text                                                                # never a fixed size
    # The homepage says "Size" for one size; the Admin says which sizes customers see
    assert '<span x-text="charmFacts.sizesLabel">Sizes</span>' in Html
    assert "Shown to customers" in Adm and "toggleSizeOn(s, $event.target.checked)" in Adm
