"""Deleting a promo code (owner, 13 Nov 2026): only a code no order has used — a used one stays on record (deactivate it);
the Admin's Promotion card keeps its own state, apart from the Promo codes screen's."""

from pathlib import Path

import pytest

from tests.conftest import Harness
from tests.test_charm_pricing import Admin, AdminKey

Web = Path(__file__).resolve().parent.parent / "web"


@pytest.fixture
async def HD(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    yield Obj
    await Obj.Close()


async def test_an_unused_code_is_deleted_and_a_used_one_stays(HD):
    H = HD
    New = lambda Code: H.Client.post("/api/admin/promo-codes", headers=Admin, json={"code": Code, "kind": "percent", "value": 100,
                                                                                    "active": True, "usage_limit": 1})
    A, B = (await New("LAUNCH10")).json(), (await New("LAUNCH")).json()
    assert (await H.Client.delete(f"/api/admin/promo-codes/{A['id']}")).status_code in (401, 403)          # the Admin only
    R = await H.Client.delete(f"/api/admin/promo-codes/{A['id']}", headers=Admin)
    assert R.status_code == 200 and R.json() == {"deleted": "LAUNCH10"}
    Codes = [P["code"] for P in (await H.Client.get("/api/admin/promo-codes", headers=Admin)).json()["promo_codes"]]
    assert Codes == ["LAUNCH"]
    assert (await H.Client.delete(f"/api/admin/promo-codes/{A['id']}", headers=Admin)).status_code == 404
    # a code an order has used stays (its use count, or an order that names it)
    H.Ctx.Db.Execute("UPDATE promo_codes SET usage_count = 1 WHERE id = ?", (B["id"],))
    R = await H.Client.delete(f"/api/admin/promo-codes/{B['id']}", headers=Admin)
    assert R.status_code == 409 and R.json()["error"]["code"] == "promo_in_use"
    assert [P["code"] for P in (await H.Client.get("/api/admin/promo-codes", headers=Admin)).json()["promo_codes"]] == ["LAUNCH"]


def test_the_admin_offers_delete_for_unused_codes_and_the_promotion_card_has_its_own_state():
    Html, Js = (Web / "admin.html").read_text(encoding="utf-8"), (Web / "admin.js").read_text(encoding="utf-8")
    assert '<button x-show="!p.usage_count" class="underline text-red-700" @click="deletePromo(p)"' in Html
    assert "await this.api('DELETE', '/api/admin/promo-codes/' + encodeURIComponent(p.id))" in Js
    Card = Html[Html.index('aria-label="Charm promotion"'):]
    Card = Card[:Card.index("</section>")]
    assert "cpromoEdit" in Card and not any(f"{N}" in Card.replace("c" + N, "") for N in ("promoEdit", "promoMsg", "promoErr", "promoBusy"))
    assert "cpromoEdit: null, cpromoNote: '', cpromoBusy: false, cpromoMsg: '', cpromoErr: false," in Js
