"""Charm sizes of 5 Nov 2026 (the owner's rule): three sizes of the 10–30 mm grid in 4 mm steps, 18 mm always among them,
smallest first everywhere; three price levels per metal, the biggest size at the highest; the stored prices per size of
before carried over level by level (proto and Atelier: 10 / 14 / 18 mm); a 3D made at any size of the grid; each size's
price in Customize in the customer's own currency."""

import json

import pytest

from p3 import charmprices as CharmPrices
from p3 import products as Products
from p3.context import HttpError
from tests.conftest import Harness, PreviewEmail
from tests.test_b6_ils_pricing import _Fetcher

AdminKey = "charm-grid-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
# The prices on proto and Atelier on 5 Nov 2026 (memory proto-charm-pricing): 10 / 14 / 18 mm
LivePrices = {"stainless_steel": {"10": 49.0, "14": 69.0, "18": 99.0}, "silver": {"10": 69.0, "14": 109.0, "18": 159.0},
              "vermeil": {"10": 59.0, "14": 99.0, "18": 139.0}}


@pytest.fixture
async def HG(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey, AccountEmail=PreviewEmail)
    assert (await Obj.Client.post("/api/admin/login", json={"key": AdminKey})).status_code == 200
    assert (await Obj.Client.put("/api/admin/products/availability", json={"product": "charm", "available": True},
                                 headers=Admin)).status_code == 200
    yield Obj
    await Obj.Close()


def test_the_sizes_are_three_of_the_grid_with_18_mm_smallest_first():
    assert Products.CharmSizeGrid == (10.0, 14.0, 18.0, 22.0, 26.0, 30.0)
    assert Products.ValidateCharmSizes([26, 18, 22]) == [18.0, 22.0, 26.0]
    assert Products.ValidateCharmSizes(["30", 10, 18.0]) == [10.0, 18.0, 30.0]
    for Bad in ([10, 14, 22], [18], [10, 18], [10, 14, 18, 22], [18, 19, 22], [18, 18, 22], [], None, "18", [18, 22, "x"]):
        with pytest.raises(HttpError) as E:
            Products.ValidateCharmSizes(Bad)
        assert E.value.Code == "invalid_charm_sizes", Bad
    with pytest.raises(HttpError) as E:
        Products.ValidateCharmSizes([10, 14, 22])
    assert "18 mm always among them" in E.value.Message
    assert [Products.CharmLevelOf(S, [26, 18, 22]) for S in (18, 22, 26, 30)] == ["1", "2", "3", None]


async def test_prices_are_three_levels_rising_with_the_size(tmp_path):
    H = Harness(tmp_path)
    try:
        Book = H.Ctx.CharmPrices
        Cat = H.Ctx.Catalog
        # Given per size on offer (10 / 14 / 18) or per level: the same three levels
        A = CharmPrices.Validate({"materials": {"silver": {"fixed_prices": {"14": 109, "10": 69, "18": 159}}}}, Cat, [10, 14, 18])
        B = CharmPrices.Validate({"materials": {"silver": {"level_prices": {"1": 69, "2": 109, "3": 159}}}}, Cat, [10, 14, 18])
        assert A["materials"]["silver"]["level_prices"] == B["materials"]["silver"]["level_prices"] == {"1": 69.0, "2": 109.0, "3": 159.0}
        for Bad, Text in (({"1": 109, "2": 69}, "must rise with the size"), ({"2": 100, "3": 100}, "must rise with the size"),
                          ({"4": 10}, "is not a price level")):
            with pytest.raises(CharmPrices.CharmPriceError) as E:
                CharmPrices.Validate({"materials": {"silver": {"level_prices": Bad}}}, Cat, [10, 14, 18])
            assert Text in str(E.value), Bad
        with pytest.raises(CharmPrices.CharmPriceError) as E:                  # a price for a size not on offer
            CharmPrices.Validate({"materials": {"silver": {"fixed_prices": {"22": 200}}}}, Cat, [10, 14, 18])
        assert "not a size on offer" in str(E.value)
        # The price of a size is its level's, whatever sizes are on offer: the biggest is the dearest
        Book.Save({"materials": {"silver": {"level_prices": {"1": 69, "2": 109, "3": 159}}}}, "test")
        assert [Book.QuoteFor("silver", S).unit_price for S in (10, 14, 18)] == [69.0, 109.0, 159.0]
        H.Ctx.Products.Set("charm_sizes", [18.0, 22.0, 26.0], "test")
        assert [Book.QuoteFor("silver", S).unit_price for S in (18, 22, 26)] == [69.0, 109.0, 159.0]
        assert Book.QuoteFor("silver", 10).unavailable_reason == "charm_size_not_offered"
        assert Book.Row("silver")["fixed_prices"] == {"18": 69.0, "22": 109.0, "26": 159.0}
    finally:
        await H.Close()


async def test_the_prices_and_names_per_size_of_before_carry_over_by_level(tmp_path):
    """Proto and Atelier on 5 Nov 2026: 10 / 14 / 18 mm on offer, prices and names stored per size. After the release the
    same three sizes are on offer at the same prices and names; the table is a new version, by level."""
    H = Harness(tmp_path)
    try:
        Db = H.Ctx.Db
        Legacy = {"currency": "USD", "materials": {M.Id: {"price_per_g": None, "cost_per_g": None,
                                                           "fixed_prices": LivePrices.get(M.Id, {})}
                                                    for M in CharmPrices.Offered(H.Ctx.Catalog)}}
        Db.Execute("INSERT INTO charm_price_lists (price_json, created_at, created_by, note) VALUES (?,?,?,?)",
                   (json.dumps(Legacy), "2026-10-07T10:00:00+00:00", "owner", "charm prices (per size)"))
        Db.Execute("DELETE FROM product_settings WHERE key = 'charm_level_names'")
        H.Ctx.Products.Set("charm_sizes", [10.0, 14.0, 18.0], "owner")
        H.Ctx.Products.Set("charm_size_names", {"10": "Delicate", "14": "Classic", "18": "Bold"}, "owner")
        Book = CharmPrices.CharmPriceBook(Db, H.Ctx.Catalog, H.Ctx.Products)          # the app starting on the new code
        Last = Db.One("SELECT * FROM charm_price_lists ORDER BY id DESC LIMIT 1")
        assert Last["created_by"] == "migration" and "10, 14, 18 mm" in Last["note"]
        Doc = json.loads(Last["price_json"])
        # the per-size prices are stored beside the levels: the code before 5 Nov reads them after a rollback
        assert Doc["materials"]["silver"] == {"price_per_g": None, "cost_per_g": None, "level_prices": {"1": 69.0, "2": 109.0, "3": 159.0},
                                              "fixed_prices": {"10": 69.0, "14": 109.0, "18": 159.0}}
        for Mid, Prices in LivePrices.items():
            assert {K: Book.QuoteFor(Mid, float(K)).unit_price for K in Prices} == Prices, Mid
        assert H.Ctx.Products.CharmLevelNames == {"1": "Delicate", "2": "Classic", "3": "Bold"}
        assert H.Ctx.Products.CharmSizeNames == {"10": "Delicate", "14": "Classic", "18": "Bold"}
        # Once more: nothing to carry over
        CharmPrices.CharmPriceBook(Db, H.Ctx.Catalog, H.Ctx.Products)
        assert Db.One("SELECT COUNT(*) AS n FROM charm_price_lists WHERE created_by = 'migration'")["n"] == 1
    finally:
        await H.Close()


async def test_a_list_stored_unsorted_is_offered_smallest_first(HG):
    H = HG
    H.Ctx.Products.Set("charm_sizes", [22.0, 18.0, 14.0], "an older save")
    Cat = (await H.Client.get("/api/catalog")).json()["products"]["charm"]
    assert Cat["sizes"] == [14.0, 18.0, 22.0] and [O["size"] for O in Cat["size_options"]] == [14.0, 18.0, 22.0]
    assert [O["level"] for O in Cat["size_options"]] == ["1", "2", "3"]


async def test_a_3d_is_made_at_any_size_of_the_grid(HG):
    H = HG
    B = await H.NewDesign("A crescent moon charm with a small star", product="charm")
    await H.Proceed(B["design_id"], B["candidates"][0]["id"])
    await H.Idle()
    D = (await H.Client.get(f"/api/admin/sessions/{B['design_id']}", headers=Admin)).json()
    assert D["catalog"]["charm_3d_sizes"] == [10.0, 14.0, 18.0, 22.0, 26.0, 30.0]
    assert D["catalog"]["charm_sizes"] == [10.0, 14.0, 18.0]                              # on offer: 10 / 14 / 18
    for Size in (26, 30):                                                                   # on the grid, not on offer
        R = await H.Client.post(f"/api/admin/sessions/{B['design_id']}/3d", json={"production_size": Size}, headers=Admin)
        assert R.status_code == 200, R.text
        assert (R.json()["production_size"], R.json()["size_source"]) == (float(Size), "admin_override")
    await H.Idle()
    Done = {T["production_size"]: T for T in (await H.Client.get(f"/api/admin/sessions/{B['design_id']}", headers=Admin)).json()["three_d"]}
    for Size in (26.0, 30.0):
        Prod = Done[Size]["geometry"]["production"]
        assert Prod["size_y_mm"] == pytest.approx(Size) and Done[Size]["size_label"] == f"{Size:g} mm"


async def test_each_size_s_price_in_customize_names_its_currency(HG):
    H = HG
    assert (await H.Client.put("/api/admin/charm-prices", headers=Admin, json={"materials": {
        "silver": {"fixed_prices": {"10": 69, "14": 109, "18": 159}}}})).status_code == 200
    B = await H.NewDesign("A crescent moon charm with a small star", product="charm")
    C = await H.Proceed(B["design_id"], B["candidates"][0]["id"])
    C = (await H.Client.patch(f"/api/customizations/{C['id']}", json={"material_id": "silver"})).json()
    assert [(S["size"], S["unit_price"], S["currency"]) for S in C["charm_sizes"]] == \
        [(10.0, 69.0, "USD"), (14.0, 109.0, "USD"), (18.0, 159.0, "USD")]
    # Israeli pricing on, a visitor who chose shekels: each size in ₪ including VAT, and says so
    I = H.Ctx.IlsPricing
    I.Fetcher = _Fetcher("3.136")
    I.Fetch()
    I.Save({**I.Settings(), "enabled": True}, "test")
    H.Client.cookies.set("p3_currency", "ILS")
    C = (await H.Client.get(f"/api/customizations/{C['id']}")).json()
    assert [S["currency"] for S in C["charm_sizes"]] == ["ILS", "ILS", "ILS"] and C["quote"]["currency"] == "ILS"
    assert [S["unit_price"] for S in C["charm_sizes"]] == sorted(S["unit_price"] for S in C["charm_sizes"])
    assert C["charm_sizes"][0]["unit_price"] > 69.0 * 3                                   # shekels, not dollars


def test_customize_shows_each_size_price_in_its_own_currency():
    """web/index.html: the size choices never format a ₪ amount as dollars (an Israeli visitor saw "$189")."""
    from p3.settings import RepoRoot
    Html = (RepoRoot / "web" / "index.html").read_text(encoding="utf-8")
    assert "money(s.unit_price)" not in Html
    assert Html.count("money(s.unit_price, s.currency || cust?.quote?.currency || 'USD')") == 2
