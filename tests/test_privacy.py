"""Privacy and sharing: a shared master design never exposes its prompt, its reference image or the words of its
refinements; a fork keeps the master's prompt private; the link name is assigned when a design is published (a GET
has no side effect); a customer's design is published only with a consent note; a used verification link reveals
nothing; registration answers the same for a new, a pending and a registered email; an uploaded photo is turned the
right way up (EXIF orientation); signing out forgets the profile kept in the browser."""

import io
import re

from PIL import Image

from p3 import assets
from p3.settings import WebDir
from tests.conftest import Harness

AdminKey = "privacy-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Anon = {"X-Access-Token": ""}


async def _Publish(H, Prompt="A twisted band with a leaf", **Body):
    B = await H.NewDesign(Prompt)
    await H.Client.put(f"/api/designs/{B['design_id']}/selection", json={"candidate_id": B["candidates"][0]["id"]})
    R = await H.Client.post("/api/admin/gallery", json={"design_id": B["design_id"], **Body}, headers=Admin)
    assert R.status_code == 200, R.text
    return B, R.json()


async def test_a_shared_master_shows_its_images_and_name_only_and_the_link_name_is_set_at_publication(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        B = await H.NewDesign("A secret prompt naming a client")
        R = await H.Client.post(f"/api/designs/{B['design_id']}/batches",            # refined in place, before publication
                                json={"parent_candidate_id": B["candidates"][0]["id"], "instruction": "Secret refinement words"})
        assert R.status_code == 200, R.text
        await H.Idle()
        await H.Client.put(f"/api/designs/{B['design_id']}/selection", json={"candidate_id": B["candidates"][0]["id"]})
        R = await H.Client.post("/api/admin/gallery", json={"design_id": B["design_id"]}, headers=Admin)
        assert R.status_code == 200, R.text
        Item = R.json()
        # The link name exists from publication (no GET ever writes); the share answer carries no prompt
        assert H.Ctx.Db.One("SELECT share_slug FROM designs WHERE id = ?", (B["design_id"],))["share_slug"]
        Share = (await H.Client.get(f"/api/gallery/{Item['id']}/share", headers=Anon)).json()
        assert "prompt" not in Share and Share["slug"]
        Html = (await H.Client.get(f"/design/{Share['slug']}", headers=Anon)).text
        assert "Secret" not in Html
        # Another customer opens it: the images and the name — never the prompt, the reference or the refinement words
        Other = {"X-Access-Token": H.Ctx.Accounts.IssueToken("other")[0]}
        R = await H.Client.post(f"/api/gallery/{Item['id']}/start", json={}, headers=Other)
        assert R.status_code == 200, R.text
        D = R.json()
        assert D["shared"] and D["prompt"] is None
        assert all(Bt["user_text"] is None and Bt["reference_url"] is None for Bt in D["batches"])
        assert any(C["image_url"] for Bt in D["batches"] for C in Bt["candidates"])
        # Their refinement forks into a design of their own: their words stay, the master's prompt does not
        R = await H.Client.post(f"/api/designs/{B['design_id']}/batches",
                                json={"parent_candidate_id": B["candidates"][0]["id"], "instruction": "Make it mine"}, headers=Other)
        assert R.status_code == 200, R.text
        await H.Idle()
        Fork = (await H.Client.get(f"/api/designs/{R.json()['design_id']}", headers=Other)).json()
        assert Fork["prompt"] is None and Fork["origin"] == "gallery" and Fork["batches"][0]["user_text"] == "Make it mine"
        # The owner still sees everything of their own design
        Mine = await H.Design(B["design_id"])
        assert Mine["prompt"] == "A secret prompt naming a client" and Mine["batches"][1]["user_text"] == "Secret refinement words"
    finally:
        await H.Close()


async def test_a_customers_design_is_published_only_with_a_consent_note(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        B = await H.NewDesign("A band")
        await H.Client.put(f"/api/designs/{B['design_id']}/selection", json={"candidate_id": B["candidates"][0]["id"]})
        R = await H.Client.post("/api/admin/gallery", json={"design_id": B["design_id"], "owner_kind": "customer"}, headers=Admin)
        assert R.status_code == 400 and R.json()["error"]["code"] == "consent_required"
        assert (await H.Client.post("/api/admin/gallery", json={"design_id": B["design_id"], "owner_kind": "nobody"}, headers=Admin)).status_code == 400
        R = await H.Client.post("/api/admin/gallery", json={"design_id": B["design_id"], "owner_kind": "customer",
                                                             "consent_note": "Agreed by email on 7 Oct 2026"}, headers=Admin)
        assert R.status_code == 200, R.text
        I = R.json()
        assert I["owner_kind"] == "customer" and I["consent_note"] == "Agreed by email on 7 Oct 2026" and I["consent_at"] and I["consent_by"]
        Items = (await H.Client.get("/api/admin/gallery", headers=Admin)).json()["items"]
        assert Items[0]["owner_kind"] == "customer" and Items[0]["consent_note"]
        # An XJet design needs none (the default); the public tile says nothing about any of it
        _B2, Item2 = await _Publish(H, "Another band")
        assert Item2["owner_kind"] == "xjet" and not Item2["consent_note"]
        for Tile in (await H.Client.get("/api/gallery", headers=Anon)).json()["items"]:
            assert set(Tile) <= {"id", "design_id", "title", "image_url", "product_type"}
    finally:
        await H.Close()


async def test_a_used_verification_link_reveals_nothing_and_registration_answers_alike(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        R = await H.Client.post("/api/register", json={"Name": "Dana", "Email": "dana@example.com"}, headers=Anon)
        assert R.status_code == 200, R.text
        First = R.json()
        assert First["status"] == "verification_sent" and "dana@example.com" in First["message"]
        Box = (await H.Client.get("/api/dev/outbox", headers=Admin)).json()["messages"]
        Html = (await H.Client.get(f"/api/dev/outbox/{Box[0]['id']}", headers=Admin)).json()["html"]
        Secret = re.search(r'/verify\?token=([A-Za-z0-9_\-]+)"', Html).group(1)
        Page = (await H.Client.get(f"/verify?token={Secret}", headers=Anon)).text
        Token = re.search(r'<div class="token">([A-Z]{6})</div>', Page).group(1)
        Again = (await H.Client.get(f"/verify?token={Secret}", headers=Anon)).text
        assert "Already verified" in Again and Token not in Again and 'class="token"' not in Again
        # Registering again — pending or verified — answers exactly like the first time (nothing says who is registered)
        assert (await H.Client.post("/api/register", json={"Name": "Dana", "Email": "dana@example.com"}, headers=Anon)).json() == First
        Lee = (await H.Client.post("/api/register", json={"Name": "Lee", "Email": "lee@example.com"}, headers=Anon)).json()
        assert Lee["status"] == First["status"] and Lee["message"].replace("lee@", "dana@") == First["message"]
    finally:
        await H.Close()


def test_an_uploaded_photo_is_turned_the_right_way_up():
    Img = Image.new("RGB", (40, 20), "white")
    Exif = Img.getexif()
    Exif[0x0112] = 6                                     # the camera held upright: shown rotated 90° without the tag
    Buf = io.BytesIO()
    Img.save(Buf, format="JPEG", exif=Exif.tobytes())
    with Image.open(io.BytesIO(assets.NormalizeReferenceImage(Buf.getvalue()))) as Out:
        assert Out.format == "PNG" and Out.size == (20, 40) and not Out.getexif().get(0x0112)


def test_signing_out_forgets_the_profile_kept_in_the_browser():
    App = (WebDir / "app.js").read_text(encoding="utf-8")
    Logout = App[App.index("    logout() {"):App.index("    signOutFromAccount()")]
    assert "localStorage.removeItem(PROFILE_KEY)" in Logout and "this.userProfile = { name: '', email: '' };" in Logout
