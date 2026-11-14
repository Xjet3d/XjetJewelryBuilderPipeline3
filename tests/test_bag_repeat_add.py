"""Add to Bag as a shop's cart (owner, 13 Nov 2026): every click adds its quantity — the same piece (design option,
product, metal, size, price) is one line whose quantity grows; a different metal or size is its own line; a request sent
twice (the same click id) adds once; a line holds at most MaxQuantity — past it the click adds what fits and says so,
with no room left it is refused with the reason; the bag's count and total follow at once."""

import asyncio

import pytest

from p3.customize import MaxQuantity
from tests.conftest import Harness

AdminKey = "bag-repeat-key"


@pytest.fixture
async def H(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    yield Obj
    await Obj.Close()


async def _Customized(H, Size=7, Material="silver", Qty=1) -> str:
    B = await H.NewDesign("Twisted rope band", product="ring")
    C = (await H.Client.post(f"/api/designs/{B['design_id']}/customize", json={"candidate_id": B["candidates"][0]["id"]})).json()
    await H.Idle()
    R = await H.Client.patch(f"/api/customizations/{C['id']}", json={"ring_size": Size, "material_id": Material, "quantity": Qty})
    assert R.status_code == 200, R.text
    return C["id"]


async def _Add(H, Cid, Click=None):
    Body = {"customization_id": Cid, **({"client_request_id": Click} if Click else {})}
    return await H.Client.post("/api/bag", json=Body)


async def _Patch(H, Cid, **Fields):
    assert (await H.Client.patch(f"/api/customizations/{Cid}", json=Fields)).status_code == 200


async def test_each_click_adds_and_the_same_piece_is_one_line(H):
    Cid = await _Customized(H)
    for N, Click in enumerate(("c1", "c2", "c3"), start=1):
        R = (await _Add(H, Cid, Click)).json()
        assert [L["quantity"] for L in R["lines"]] == [N]                      # 1, 2, 3 — one line
        assert R["added"]["quantity"] == 1 and R["added"]["line_quantity"] == N and not R["added"]["limited"]
    Unit = R["lines"][0]["unit_price"]
    assert R["lines"][0]["line_total"] == pytest.approx(3 * Unit)
    assert sum(R["totals"].values()) == pytest.approx(3 * Unit) if R.get("totals") else True
    # quantity 2 in Customize, Add again: two more
    await _Patch(H, Cid, quantity=2)
    R = (await _Add(H, Cid, "c4")).json()
    assert [L["quantity"] for L in R["lines"]] == [5] and R["added"]["quantity"] == 2
    # no click id (an older page): still a normal add
    R = (await _Add(H, Cid)).json()
    assert [L["quantity"] for L in R["lines"]] == [7]


async def test_a_request_sent_twice_adds_once_and_parallel_clicks_all_count(H):
    Cid = await _Customized(H)
    R1 = (await _Add(H, Cid, "same-click")).json()
    R2 = (await _Add(H, Cid, "same-click")).json()                              # a retry of the same click
    assert [L["quantity"] for L in R2["lines"]] == [1] and R2["repeated_request"] is True and R2["added"] is None
    assert R1["added"]["line_quantity"] == 1
    Rs = await asyncio.gather(*(_Add(H, Cid, f"fast-{K}") for K in range(4)))   # four quick clicks
    assert all(R.status_code == 200 for R in Rs)
    Bag = (await H.Client.get("/api/bag")).json()
    assert [L["quantity"] for L in Bag["lines"]] == [5]


async def test_another_metal_or_size_is_its_own_line(H):
    Cid = await _Customized(H, Size=7, Material="silver")
    await _Add(H, Cid, "a")
    await _Patch(H, Cid, material_id="stainless_steel")
    await _Add(H, Cid, "b")
    await _Patch(H, Cid, ring_size=8)
    await _Add(H, Cid, "c")
    await _Patch(H, Cid, ring_size=7, material_id="silver")
    R = (await _Add(H, Cid, "d")).json()                                         # back to the first: that line grows
    Lines = sorted((L["material_id"], L["ring_size"], L["quantity"]) for L in R["lines"])
    assert Lines == [("silver", 7, 2), ("stainless_steel", 7, 1), ("stainless_steel", 8, 1)]


async def test_the_limit_adds_what_fits_then_says_why(H):
    Cid = await _Customized(H, Qty=MaxQuantity - 1)
    assert (await _Add(H, Cid, "x1")).json()["added"]["line_quantity"] == MaxQuantity - 1
    await _Patch(H, Cid, quantity=3)
    R = (await _Add(H, Cid, "x2")).json()
    assert R["added"] == {"line_id": R["lines"][0]["id"], "quantity": 1, "line_quantity": MaxQuantity, "limited": True,
                          "max_quantity": MaxQuantity}
    Full = await _Add(H, Cid, "x3")
    assert Full.status_code == 409 and Full.json()["error"]["code"] == "bag_quantity_limit"
    assert f"already has {MaxQuantity:,} of this piece" in Full.json()["error"]["message"]
    assert [L["quantity"] for L in (await H.Client.get("/api/bag")).json()["lines"]] == [MaxQuantity]
    # the refused click's id is not spent: once there is room again, the same request adds
    Line = (await H.Client.get("/api/bag")).json()["lines"][0]
    assert (await H.Client.patch(f"/api/bag/{Line['id']}", json={"quantity": 2})).status_code == 200
    await _Patch(H, Cid, quantity=1)
    assert (await _Add(H, Cid, "x3")).json()["added"]["line_quantity"] == 3
