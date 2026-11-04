"""Charm sizes of 2026-10-06: 10 mm Delicate · 14 mm Classic (recommended) · 18 mm Bold. The choices read
"14 mm — Classic · Recommended" on the customer site and in Admin; the names and the recommended size are edited
with the sizes (Settings → Products). A size is still the charm's total height, the loop included."""

import pytest

from tests.conftest import Harness, PreviewEmail

AdminKey = "charm-sizes-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}


@pytest.fixture
async def HS(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey, AccountEmail=PreviewEmail)   # a test account
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
    # Customize: the choices with their prices (none set yet); a new charm opens on the recommended size (WP3)
    B = await H.NewDesign("A crescent moon charm with a small star", product="charm")
    C = await H.Proceed(B["design_id"], B["candidates"][0]["id"])
    assert C["charm_size"] == C["recommended_size"] == 14.0 and C["add_to_bag_blocked_reason"] == "price_unavailable"
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
    """The names belong to the levels (small, medium, large) and follow them when the sizes change; the recommended size
    is one of the three, the middle one when none is given (5 Nov 2026)."""
    H = HS
    R = await H.Client.put("/api/admin/products/charm-sizes", headers=Admin,
                           json={"sizes": [26, 18, 22], "level_names": {"1": "Petite", "3": "Statement"}, "default": 22, "note": "new range"})
    assert R.status_code == 200, R.text
    P = R.json()
    assert P["charm_sizes"] == [18.0, 22.0, 26.0] and P["charm_default_size"] == 22.0
    assert P["charm_level_names"] == {"1": "Petite", "3": "Statement"} and P["charm_size_names"] == {"18": "Petite", "26": "Statement"}
    assert [O["label"] for O in P["charm_size_options"]] == ["18 mm — Petite", "22 mm · Recommended", "26 mm — Statement"]
    assert [(O["level"], O["level_label"]) for O in P["charm_size_options"]] == [("1", "small"), ("2", "medium"), ("3", "large")]
    assert [L["key"] for L in P["log"][:3]] == ["charm_default_size", "charm_level_names", "charm_sizes"]
    # The recommended size must be on offer; names given per size (the older Admin page) name the levels
    assert (await H.Client.put("/api/admin/products/charm-sizes", json={"sizes": [18, 22, 26], "default": 14}, headers=Admin)).status_code == 400
    P = (await H.Client.put("/api/admin/products/charm-sizes", headers=Admin,
                            json={"sizes": [10, 14, 18], "names": {"10": "Delicate", "14": "Classic", "18": "Bold", "30": "Gone"}})).json()
    assert P["charm_default_size"] == 14.0 and P["charm_level_names"] == {"1": "Delicate", "2": "Classic", "3": "Bold"}
    # Without names the levels keep theirs for their new sizes; without a recommended size, the middle one
    P = (await H.Client.put("/api/admin/products/charm-sizes", json={"sizes": [30, 18, 22]}, headers=Admin)).json()
    assert P["charm_size_names"] == {"18": "Delicate", "22": "Classic", "30": "Bold"} and P["charm_default_size"] == 22.0
    # The charm price table names its columns: the size, its level and the level's name
    T = (await H.Client.get("/api/admin/charm-prices", headers=Admin)).json()
    assert T["sizes"] == [18.0, 22.0, 30.0] and T["default_size"] == 22.0
    assert [(L["size"], L["label"], L["name"]) for L in T["levels"]] == [(18.0, "small", "Delicate"), (22.0, "medium", "Classic"), (30.0, "large", "Bold")]
    # A name is a short text, given for a level
    for Bad in ({"1": "x" * 31}, {"4": "Huge"}, "Petite"):
        assert (await H.Client.put("/api/admin/products/charm-sizes", json={"sizes": [10, 14, 18], "level_names": Bad}, headers=Admin)).status_code == 400
