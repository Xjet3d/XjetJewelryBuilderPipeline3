"""R-51 (2026-10-25): a New Design from an uploaded photo has its own configuration per product — "Ring New Design — Photo
Reference" and "Charm New Design — Photo Reference" in Admin → AI prompts — instead of the refinement instructions, which
call the photo "the existing ring". Version 1 is a copy of the product's refinement configuration (what these designs
were sent until then), so the switch changes nothing by itself; refinements keep their own configuration."""

import io

import pytest
from PIL import Image

from p3.modelconfig import ModelConfigStore, Models
from p3.providers import endpoints
from tests.conftest import Harness

AdminKey = "photo-reference-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}


@pytest.fixture
async def HP(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    yield Obj
    await Obj.Close()


def _Photo() -> bytes:
    Buf = io.BytesIO()
    Image.new("RGB", (96, 64), (220, 120, 40)).save(Buf, format="PNG")
    return Buf.getvalue()


async def _Model(H, Id):
    return (await H.Client.get(f"/api/admin/models/{Id}", headers=Admin)).json()


async def _PhotoDesign(H, Prompt, **Form):
    R = await H.Client.post("/api/designs", data={"prompt": Prompt, "rights_confirmed": "true", **Form},
                            files={"reference": ("photo.png", _Photo(), "image/png")})
    assert R.status_code == 200, R.text
    await H.Idle()
    return (await H.Client.get(f"/api/batches/{R.json()['id']}")).json()


async def test_each_flow_has_its_own_named_configuration_and_photo_starts_as_the_refinement_copy(HP):
    H = HP
    L = (await H.Client.get("/api/admin/models", headers=Admin)).json()["models"]
    Labels = {M["model"]["id"]: M["model"]["label"] for M in L}
    assert [Labels[M] for M in ("nano-banana-pro", "nano-banana-pro-edit-photo", "nano-banana-pro-edit",
                                "nano-banana-pro-charm", "nano-banana-pro-edit-photo-charm", "nano-banana-pro-edit-charm")] == [
        "Ring New Design — Text", "Ring New Design — Photo Reference", "Ring Refinement",
        "Charm New Design — Text", "Charm New Design — Photo Reference", "Charm Refinement"]
    for Photo, Refine in (("nano-banana-pro-edit-photo", "nano-banana-pro-edit"), ("nano-banana-pro-edit-photo-charm", "nano-banana-pro-edit-charm")):
        P, R = await _Model(H, Photo), await _Model(H, Refine)
        assert P["model"]["endpoint"] == endpoints.ImageEdit and P["model"]["product"] == R["model"]["product"]
        assert P["active"]["id"].startswith(Photo + "@v1-") and P["active"]["created_by"] == "seed"
        assert P["active"]["params"] == {K: V for K, V in R["active"]["params"].items() if not K.startswith("variation_")}
        assert "a copy of " + R["model"]["label"] in P["active"]["note"]
        assert not any(X["internal"] for X in P["model"]["params"])                     # no refinement directives
    # Starting again adds nothing
    ModelConfigStore(H.Ctx.Db)
    assert len((await _Model(H, "nano-banana-pro-edit-photo"))["history"]) == 1
    Health = (await H.Client.get("/api/admin/health", headers=Admin)).json()
    assert "nano-banana-pro-edit-photo" in Health["config_versions"] and "nano-banana-pro-edit-photo-charm" in Health["charm_config_versions"]


async def test_a_new_design_from_a_photo_uses_its_configuration_and_a_refinement_its_own(HP):
    H = HP
    Ring = (await _Model(H, "nano-banana-pro-edit-photo"))["active"]["params"]
    New = {**Ring, "prompt": "{{user_text}}\n\nPHOTO TEMPLATE: the photo is inspiration.", "system_prompt": "PHOTO SYSTEM"}
    R = await H.Client.post("/api/admin/models/nano-banana-pro-edit-photo/activate", json={"params": New, "note": "R-51"}, headers=Admin)
    assert R.status_code == 200 and R.json()["active"]["id"].startswith("nano-banana-pro-edit-photo@v2-")
    Batch = await _PhotoDesign(H, "Make a ring with the car")
    assert Batch["config_version"].startswith("nano-banana-pro-edit-photo@v2-") and Batch["status"] == "complete"
    Subs = H.Provider.SubmissionsFor(endpoints.ImageEdit)
    assert len(Subs) == 4 and len({S[1]["image_urls"][0] for S in Subs}) == 1
    assert all(S[1]["prompt"] == "Make a ring with the car\n\nPHOTO TEMPLATE: the photo is inspiration." for S in Subs)
    assert all(S[1]["system_prompt"] == "PHOTO SYSTEM" for S in Subs)                       # no directive appended
    # A refinement of it uses the refinement configuration and its directives
    R = await H.Client.post(f"/api/designs/{Batch['design_id']}/batches",
                            json={"parent_candidate_id": Batch["candidates"][0]["id"], "instruction": "Make the band thinner"})
    assert R.status_code == 200 and R.json()["config_version"].startswith("nano-banana-pro-edit@")
    await H.Idle()
    Refine = H.Provider.SubmissionsFor(endpoints.ImageEdit)[4:]
    assert len(Refine) == 4 and not any("PHOTO" in S[1]["prompt"] or S[1].get("system_prompt") == "PHOTO SYSTEM" for S in Refine)
    assert len({S[1]["prompt"] for S in Refine}) == 4                                           # one directive per image
    # A text New Design is untouched
    Text = await H.NewDesign("A slim twisted band")
    assert Text["config_version"].startswith("nano-banana-pro@")


async def test_a_charm_from_a_photo_uses_the_charm_photo_configuration(HP):
    H = HP
    assert (await H.Client.post("/api/admin/login", json={"key": AdminKey})).status_code == 200    # this browser: Admin preview
    Charm = (await _Model(H, "nano-banana-pro-edit-photo-charm"))["active"]["params"]
    New = {**Charm, "prompt": "{{user_text}}\n\nCHARM PHOTO TEMPLATE"}
    assert (await H.Client.post("/api/admin/models/nano-banana-pro-edit-photo-charm/activate", json={"params": New},
                                headers=Admin)).status_code == 200
    Ring = (await _Model(H, "nano-banana-pro-edit-photo"))["active"]
    Batch = await _PhotoDesign(H, "Make a charm of my dog", product="charm")
    assert Batch["config_version"].startswith("nano-banana-pro-edit-photo-charm@v2-")
    assert all(S[1]["prompt"].endswith("CHARM PHOTO TEMPLATE") for S in H.Provider.SubmissionsFor(endpoints.ImageEdit))
    assert (await _Model(H, "nano-banana-pro-edit-photo"))["active"] == Ring                       # the ring's is untouched
    assert Models["nano-banana-pro-edit-photo-charm"].Product == "charm"
