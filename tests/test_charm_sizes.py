"""Charm sizes of 2026-10-06: 10 mm Delicate · 14 mm Classic (recommended) · 18 mm Bold. The choices read
"14 mm — Classic · Recommended" on the customer site and in Admin; the names and the recommended size are edited
with the sizes (Settings → Products). A size is still the charm's total height, the loop included."""

import pytest

from tests.conftest import Harness

AdminKey = "charm-sizes-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}


@pytest.fixture
async def HS(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    assert (await Obj.Client.post("/api/admin/login", json={"key": AdminKey})).status_code == 200   # Admin preview of charms
    yield Obj
    await Obj.Close()


async def test_the_sizes_on_offer_are_delicate_classic_and_bold_with_classic_recommended(HS):
    H = HS
    P = (await H.Client.get("/api/admin/products", headers=Admin)).json()
    assert P["charm_sizes"] == [10.0, 14.0, 18.0] and P["charm_default_size"] == 14.0
    assert P["charm_size_names"] == {"10": "Delicate", "14": "Classic", "18": "Bold"}
    assert [O["label"] for O in P["charm_size_options"]] == ["10 mm — Delicate", "14 mm — Classic · Recommended", "18 mm — Bold"]
    assert P["charm_size_definition"]["measure"] == "total_height" and P["charm_size_definition"]["includes_loop"] is True
    Cat = (await H.Client.get("/api/catalog")).json()["products"]["charm"]
    assert Cat["sizes"] == [10.0, 14.0, 18.0] and Cat["recommended_size"] == 14.0
    assert [(O["size"], O["name"], O["recommended"]) for O in Cat["size_options"]] == \
        [(10.0, "Delicate", False), (14.0, "Classic", True), (18.0, "Bold", False)]
    # Customize: the choices with their prices (none set yet), the recommended size suggested, no size chosen yet
    B = await H.NewDesign("A crescent moon charm with a small star", product="charm")
    C = await H.Proceed(B["design_id"], B["candidates"][0]["id"])
    assert C["charm_size"] is None and C["recommended_size"] == 14.0 and C["add_to_bag_blocked_reason"] == "charm_size_required"
    assert [(S["label"], S["unit_price"]) for S in C["charm_sizes"]] == \
        [("10 mm — Delicate", None), ("14 mm — Classic · Recommended", None), ("18 mm — Bold", None)]
    # A size no longer on offer is refused; the recommended one is a normal choice with the plain size label
    R = await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": 20})
    assert R.status_code == 400 and R.json()["error"]["code"] == "invalid_charm_size"
    C = (await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": 14})).json()
    assert C["charm_size"] == 14.0 and C["size_label"] == "14 mm"
    # A 3D for a charm whose customer chose no size is made in the recommended size
    B2 = await H.NewDesign("A tiny star charm", product="charm")
    await H.Proceed(B2["design_id"], B2["candidates"][0]["id"])
    await H.Idle()
    D = (await H.Client.get(f"/api/admin/sessions/{B2['design_id']}", headers=Admin)).json()
    assert D["three_d_defaults"]["production_size"] == 14.0 and D["catalog"]["charm_default_size"] == 14.0
    assert [O["label"] for O in D["catalog"]["charm_size_options"]][1] == "14 mm — Classic · Recommended"
    T = (await H.Client.post(f"/api/admin/sessions/{B2['design_id']}/3d", json={}, headers=Admin)).json()
    assert (T["production_size"], T["size_source"]) == (14.0, "default")


async def test_names_and_the_recommended_size_are_edited_with_the_sizes(HS):
    H = HS
    R = await H.Client.put("/api/admin/products/charm-sizes", headers=Admin,
                           json={"sizes": [12, 16, 20], "names": {"12": "Petite", "20": "Statement", "30": "Gone"}, "default": 16, "note": "new range"})
    assert R.status_code == 200, R.text
    P = R.json()
    assert P["charm_sizes"] == [12.0, 16.0, 20.0] and P["charm_default_size"] == 16.0
    assert P["charm_size_names"] == {"12": "Petite", "20": "Statement"}                     # a name of a size not on offer is dropped
    assert [O["label"] for O in P["charm_size_options"]] == ["12 mm — Petite", "16 mm · Recommended", "20 mm — Statement"]
    assert [L["key"] for L in P["log"][:3]] == ["charm_default_size", "charm_size_names", "charm_sizes"]
    # The recommended size must be on offer; a removed recommended size falls back to the middle size, names stay
    assert (await H.Client.put("/api/admin/products/charm-sizes", json={"sizes": [12, 16, 20], "default": 14}, headers=Admin)).status_code == 400
    P = (await H.Client.put("/api/admin/products/charm-sizes", json={"sizes": [12, 20, 24]}, headers=Admin)).json()
    assert P["charm_default_size"] == 20.0 and P["charm_size_names"] == {"12": "Petite", "20": "Statement"}
    # The charm price table shows the names next to the sizes
    T = (await H.Client.get("/api/admin/charm-prices", headers=Admin)).json()
    assert T["sizes"] == [12.0, 20.0, 24.0] and T["size_names"] == {"12": "Petite", "20": "Statement"} and T["default_size"] == 20.0
    # A name is a short text
    assert (await H.Client.put("/api/admin/products/charm-sizes", json={"sizes": [12], "names": {"12": "x" * 31}}, headers=Admin)).status_code == 400
