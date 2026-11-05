"""Rings OFF on the customer site (owner, 5 Nov 2026): a browser signed in to the Admin sees exactly what customers see —
no product choice, no ring action, no technical notice — unless it opened the site in preview on purpose (?preview=on,
this tab: the X-P3-Preview header); a gallery card of a product switched OFF has no "Make it yours"."""

from p3 import products as Products
from p3.settings import WebDir
from tests.conftest import Harness

AdminKey = "rings-off-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Index = (WebDir / "index.html").read_text(encoding="utf-8")
App = (WebDir / "app.js").read_text(encoding="utf-8")


async def _Rings(H, On: bool):
    for Product, Value in (("charm", True), ("ring", On)):
        R = await H.Client.put("/api/admin/products/availability", json={"product": Product, "available": Value}, headers=Admin)
        assert R.status_code == 200, R.text


async def test_an_admin_browser_sees_what_customers_see_unless_it_opens_the_preview(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        await _Rings(H, False)
        assert (await H.Client.post("/api/admin/login", json={"key": AdminKey})).status_code == 200   # signed in to the Admin
        P = (await H.Client.get("/api/catalog")).json()["products"]
        assert P["available"] == ["charm"] and P["previewed"] == []                                   # no ring choice
        R = await H.Client.post("/api/designs", data={"prompt": "A twisted band", "product": "ring"})
        assert R.status_code >= 400                                                                    # no ring action
        Previewing = (await H.Client.get("/api/catalog", headers={"X-P3-Preview": "1"})).json()["products"]
        assert Previewing["previewed"] == ["ring"]                                                     # on purpose: the preview
        Customer = Harness(tmp_path / "c", AdminKey=AdminKey)
        try:
            # without an Admin sign-in the header changes nothing
            assert (await Customer.Client.get("/api/catalog", headers={"X-P3-Preview": "1"})).json().get("products", {}).get("previewed", []) == []
        finally:
            await Customer.Close()
        await _Rings(H, True)                                                                          # OFF → ON: back as it was
        P = (await H.Client.get("/api/catalog")).json()["products"]
        assert sorted(P["available"]) == ["charm", "ring"] and P["previewed"] == []
    finally:
        await H.Close()


def test_the_preview_is_short_opt_in_and_can_be_ended():
    Pill = Index.split("<!-- ── Admin preview:", 1)[1].split("</template>", 1)[0]
    assert "End preview" in Pill and "<strong class=\"font-semibold\">Preview</strong>" in Pill and "test account" not in Pill.split("-->", 1)[1]
    assert "searchParams.get('preview')" in App or "get('preview')" in App
    assert "headers['X-P3-Preview'] = '1'" in App and "sessionStorage" in App
    assert Products.PreviewOnlyError == "Preview only: use a test account to try this product."
    assert len(Products.PreviewOnlyError) < 70


def test_a_card_of_a_product_switched_off_has_no_make_it_yours():
    Buttons = [Part.split(">", 1)[0] for Part in Index.split('@click.stop="makeItYoursFromCard(g)"')[:-1]]
    assert Index.count('x-show="!isOff(g)" @click.stop="makeItYoursFromCard(g)"') == 3
    assert 'x-show="!isOff(galleryItem)" @click="makeItYours()"' in Index                         # the dialog's button too
    assert "Currently unavailable for customization or ordering." in Index
