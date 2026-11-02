"""Audit B2 (SEC-05): a customer's uploaded reference photo is never served at a media address (/assets, /thumb) — only
to the design's owner and to the Admin, never kept by a shared cache; a design's other media may be cached by shared
caches only while the design is public (in the gallery), and stays private to the visitor's browser otherwise."""

import httpx
import pytest

from p3 import assets
from tests.conftest import Harness
from tests.test_images import _RingImage

AdminKey = "reference-privacy-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Public = "public, max-age=31536000, immutable"
Private = "private, max-age=31536000, immutable"


@pytest.fixture
async def H(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    yield Obj
    await Obj.Close()


def As(H, Token=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=H.App), base_url="http://p3.test" + H.Base,
                             headers={"X-Access-Token": Token} if Token else {})


async def _WithReference(H):
    Png = assets.NormalizeReferenceImage(_RingImage(7, "ref"))
    R = await H.Client.post("/api/designs", data={"prompt": "Make it like this", "rights_confirmed": "true"},
                            files={"reference": ("ref.png", Png, "image/png")})
    assert R.status_code == 200, R.text
    await H.Idle()
    Batch = (await H.Client.get(f"/api/batches/{R.json()['id']}")).json()
    Rel = H.Ctx.Db.One("SELECT reference_asset FROM batches WHERE id = ?", (Batch["id"],))["reference_asset"]
    return Batch, Rel


async def test_the_reference_photo_is_not_at_any_media_address(H):
    Batch, Rel = await _WithReference(H)
    assert "/references/" in Rel and (H.Settings.AssetsDir / Rel).is_file()
    for Path in (f"/assets/{Rel}", f"/thumb/{Rel}?w=320", f"/thumb/{Rel}?w=320&f=jpg", f"/assets/_derived/{Rel}.w320.jpg"):
        R = await As(H).get(Path, headers=Admin)
        assert R.status_code == 404 and R.headers["cache-control"] == "no-store", Path


async def test_the_owner_and_the_admin_see_it_and_no_one_else(H):
    Batch, Rel = await _WithReference(H)
    Did, Bid = Batch["design_id"], Batch["id"]
    assert Batch["reference_url"] == f"{H.Base}/api/designs/{Did}/references/{Bid}"
    Mine = await H.Client.get(f"/api/designs/{Did}/references/{Bid}")
    assert Mine.status_code == 200 and Mine.content == (H.Settings.AssetsDir / Rel).read_bytes()
    assert Mine.headers["cache-control"] == "private, no-store" and Mine.headers["content-type"] == "image/png"
    Other, _ = H.Ctx.Accounts.IssueToken("other", MaxGenerations=5)
    assert (await As(H, Other).get(f"/api/designs/{Did}/references/{Bid}")).status_code == 404
    assert (await As(H).get(f"/api/designs/{Did}/references/{Bid}")).status_code == 401
    AdminUrl = f"/api/admin/designs/{Did}/references/{Bid}"
    Seen = await As(H).get(AdminUrl, headers=Admin)
    assert Seen.status_code == 200 and Seen.headers["cache-control"] == "private, no-store"
    assert (await As(H).get(AdminUrl)).status_code in (401, 403)
    Detail = (await As(H).get(f"/api/admin/sessions/{Did}", headers=Admin)).text      # the Admin's journey view
    assert f'"reference_url":"{H.Base}{AdminUrl}"' in Detail.replace(" ", "") and Rel not in Detail


async def test_design_media_is_shared_cacheable_only_while_the_design_is_in_the_gallery(H):
    Batch = await H.NewDesign("Twisted bands joined by a small knot")
    Did, Url = Batch["design_id"], Batch["candidates"][0]["image_url"]
    Rel = Url.removeprefix(H.Base + "/assets/")
    for Path in (f"/assets/{Rel}", f"/thumb/{Rel}?w=320&f=jpg"):
        R = await As(H).get(Path)                                     # the random address still works: emails, orders
        assert R.status_code == 200 and R.headers["cache-control"] == Private, Path
    await H.Proceed(Did, Batch["candidates"][0]["id"])
    await H.Idle()
    assert (await H.Client.post("/api/admin/gallery", json={"design_id": Did}, headers=Admin)).status_code == 200
    for Path in (f"/assets/{Rel}", f"/thumb/{Rel}?w=320&f=jpg"):
        R = await As(H).get(Path)
        assert R.status_code == 200 and R.headers["cache-control"] == Public, Path
    assert (await As(H).get("/assets/designs/nope/candidates/nope.png")).headers["cache-control"] == "no-store"
