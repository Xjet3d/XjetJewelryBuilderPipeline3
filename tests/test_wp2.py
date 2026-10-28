"""Work Package 2 (2026-10-26), server side: a bag line's own quantity (R-20), a confirmed ring size kept for the design
(R-21), a charm's size in Checkout (R-02), the Checkout note (R-45), the order email's View Your Order button, pictures and
next steps (R-43), developer tools and the older sign-in left out of a page where they are off (R-34), and a gold quote's
due dates and the customer's update (R-48). Then the owner's decisions after the review (26 Oct): business days Sunday to
Thursday, R-12 reopened, the phones' Checkout stepper (R-03) and How It Works' 360° movie for rings only (R-05)."""

import re
from datetime import date
from pathlib import Path

import pytest

from tests.conftest import Harness, OfferCharmSizes
from tests.test_orders import Address, Admin, AdminKey, Customer, _InBag, _Order


@pytest.fixture
async def HW(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    yield Obj
    await Obj.Close()


async def test_a_bag_line_quantity_changes_its_total_and_the_bag_total(HW):
    H = HW
    _Did, _Cand, Bag = await _InBag(H, Size=7, Qty=1)
    L = Bag["lines"][0]
    R = await H.Client.patch(f"/api/bag/{L['id']}", json={"quantity": 3})
    assert R.status_code == 200, R.text
    New = R.json()["lines"][0]
    assert New["quantity"] == 3 and New["unit_price"] == L["unit_price"] and New["line_total"] == pytest.approx(3 * L["unit_price"])
    assert R.json()["totals"]["USD"] == pytest.approx(3 * L["unit_price"])
    for Bad in (0, 11, "2", True, None):
        assert (await H.Client.patch(f"/api/bag/{L['id']}", json={"quantity": Bad})).status_code == 400, Bad
    Other = {"X-Access-Token": H.Ctx.Accounts.IssueToken("other")[0]}
    assert (await H.Client.patch(f"/api/bag/{L['id']}", json={"quantity": 2}, headers=Other)).status_code == 404
    Q = (await H.Client.post("/api/checkout/quote", json={})).json()                   # Checkout charges the new quantity
    assert Q["lines"][0]["quantity"] == 3 and Q["count"] == 3


async def test_a_confirmed_ring_size_is_remembered_for_the_design(HW):
    H = HW
    B = await H.NewDesign("Twisted rope band")
    A, Second = B["candidates"][0], B["candidates"][1]
    C1 = (await H.Client.post(f"/api/designs/{B['design_id']}/customize", json={"candidate_id": A["id"]})).json()
    assert C1["ring_size"] == 10 and C1["size_confirmed"] is False                     # US 10 is only suggested
    C1 = (await H.Client.patch(f"/api/customizations/{C1['id']}", json={"ring_size": 7})).json()
    assert C1["size_confirmed"] is True
    Again = (await H.Client.post(f"/api/designs/{B['design_id']}/customize", json={"candidate_id": A["id"]})).json()
    assert Again["id"] == C1["id"] and Again["ring_size"] == 7 and Again["size_confirmed"] is True   # back to Customize: kept
    C2 = (await H.Client.post(f"/api/designs/{B['design_id']}/customize", json={"candidate_id": Second["id"]})).json()
    assert C2["ring_size"] == 7 and C2["size_confirmed"] is True                       # another option of the design: the same size
    C2 = (await H.Client.patch(f"/api/customizations/{C2['id']}", json={"ring_size": 8.5})).json()
    assert C2["ring_size"] == 8.5 and C2["size_confirmed"] is True                     # still the customer's to change
    Other = await H.NewDesign("A signet with a hexagon face")
    C3 = (await H.Client.post(f"/api/designs/{Other['design_id']}/customize", json={"candidate_id": Other["candidates"][0]["id"]})).json()
    assert C3["ring_size"] == 10 and C3["size_confirmed"] is False                     # another design: suggested again


async def test_checkout_names_a_charm_by_its_size(HW):
    H = HW
    await OfferCharmSizes(H, AdminKey)
    assert (await H.Client.put("/api/admin/products/availability", json={"charms_available": True}, headers=Admin)).status_code == 200
    await H.Client.put("/api/admin/charm-prices", json={"materials": {"silver": {"fixed_prices": {"20": 145}}}}, headers=Admin)
    B = await H.NewDesign("A crescent moon charm", product="charm")
    C = await H.Proceed(B["design_id"], B["candidates"][0]["id"])
    await H.Idle()
    await H.Client.patch(f"/api/customizations/{C['id']}", json={"charm_size": 20})
    await H.Client.post("/api/bag", json={"customization_id": C["id"]})
    Info = (await H.Client.get("/api/checkout")).json()
    assert Info["quote"]["lines"][0]["size_label"] == "20 mm"
    assert (await H.Client.post("/api/checkout/quote", json={})).json()["lines"][0]["size_label"] == "20 mm"


async def test_the_checkout_note_and_the_order_email(HW):
    H = HW
    await _InBag(H, Size=7, Qty=2)
    R = await H.Client.post("/api/orders", json=_Order(customer_note="  A gift — please no price inside.  "))
    assert R.status_code == 200, R.text
    O = R.json()
    A = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()
    assert A["notes"] == "Customer's note: A gift — please no price inside."
    assert "notes" not in O                                                             # the customer's view has no team notes
    Mail = H.App.state.Mailer.List()[0]["html"]
    # R-43: the confirmation page's three next steps, a picture of each piece, View Your Order (My Account: no order data in it)
    assert "What happens next" in Mail and "Design review." in Mail and "printed to order in real metal" in Mail and "tracked." in Mail
    assert 'href="http://p3.test/#account"' in Mail and "View Your Order" in Mail
    assert re.search(r'<img src="http://p3\.test/thumb/[^"]+\?w=320&amp;f=jpg" width="64" height="64"', Mail)
    assert "token" not in Mail and O["id"] not in Mail


async def test_developer_tools_and_the_older_sign_in_are_left_out_where_they_are_off(tmp_path):
    Off = Harness(tmp_path / "off", NoDevTools=True, AuthMode="dual")
    try:
        Page = (await Off.Client.get("/")).text
        for Gone in ("P3_ADMIN_KEY", "fal.ai", "Minimax", "Hitem3D", "Developer access", "Enter sign-in code", "<!--dev-->", "<!--legacy-->"):
            assert Gone not in Page, Gone
        assert "Sign in to start designing" in Page                                          # the sign-in itself is there
    finally:
        await Off.Close()
    On = Harness(tmp_path / "on")                                                            # developer tools on, the older sign-in mode
    try:
        Page = (await On.Client.get("/")).text
        assert "Developer access" in Page and "Enter sign-in code" in Page and "<!--dev-->" not in Page
    finally:
        await On.Close()


async def test_a_gold_quote_has_its_due_dates_and_the_customer_can_be_sent_an_update(tmp_path):
    from tests.test_quotes import Admin as QAdmin, AdminKey as QKey, _Request
    H = Harness(tmp_path, AdminKey=QKey)
    try:
        _B, Q = await _Request(H)
        D = (await H.Client.get(f"/api/admin/quote-requests/{Q['id']}", headers=QAdmin)).json()
        Due = D["due"]
        assert Due["review_by"] < Due["quote_by"] and not Due["reviewed"] and not Due["review_overdue"] and not Due["quote_overdue"]
        assert date.fromisoformat(Due["review_by"]).weekday() not in (4, 5) and date.fromisoformat(Due["quote_by"]).weekday() not in (4, 5)
        assert D["suggestion"]["has_model"] is False
        Url = f"/api/admin/quote-requests/{Q['id']}/update"
        assert (await H.Client.post(Url, json={"message": "short"}, headers=QAdmin)).status_code == 400
        R = await H.Client.post(Url, json={"message": "Hello Dana,\n\nYour ring needs a closer look; we expect to send your quote by Friday."},
                                headers=QAdmin)
        assert R.status_code == 200, R.text
        D = (await H.Client.get(f"/api/admin/quote-requests/{Q['id']}", headers=QAdmin)).json()
        assert D["due"]["reviewed"] and D["status"] == "new"                                # an update is no quote
        assert any(E["kind"] == "update_sent" and "closer look" in E["data"]["message"] for E in D["history"])
        assert any(E["kind"] == "email" and E["data"].get("type") == "update" for E in D["history"])
        Mail = next(M for M in H.App.state.Mailer.List(50) if M["subject"].startswith("An update on your quote request"))
        assert Mail["to"] == "dana@atelier-customer.co.il" and "closer look" in Mail["html"] and Q["ref"] in Mail["subject"]
    finally:
        await H.Close()


def test_business_days_run_sunday_to_thursday():
    """The owner's decision of 26 Oct (R-48): Friday and Saturday are the weekend."""
    from p3.quotes import _AddBusinessDays
    for Start, One, Three in (("2026-10-28", "2026-10-29", "2026-11-02"),     # Wednesday
                              ("2026-10-29", "2026-11-01", "2026-11-03"),     # Thursday: review by Sunday
                              ("2026-10-30", "2026-11-01", "2026-11-03"),     # Friday
                              ("2026-10-31", "2026-11-01", "2026-11-03"),     # Saturday
                              ("2026-11-01", "2026-11-02", "2026-11-04")):    # Sunday
        S = date.fromisoformat(Start)
        assert (_AddBusinessDays(S, 1).isoformat(), _AddBusinessDays(S, 3).isoformat()) == (One, Three), Start


def test_r12_the_design_screen_is_laid_out_as_before_work_package_2():
    """R-12 reopened for a joint UX review (26 Oct): no next step beside the grid; the design's name under the options and
    Start a New Design · Customize under the composer at every width, as before Work Package 2 (R-17's words kept)."""
    Index = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert 'aria-label="Next step"' not in Index and "lg:flex lg:items-start lg:justify-center lg:gap-8" not in Index
    assert '<div x-show="design" class="w-full flex flex-col sm:flex-row items-stretch gap-2 sm:gap-3">' in Index
    assert '<h2 class="brand-font text-[24px] sm:text-[28px] font-semibold tracking-[0.06em] text-center pt-1" style="color:#1A1A1A;" x-text="design?.title"></h2>' in Index
    assert "'Customize Your ' + designLabel + (canActOnSelection ? ' · ' + selectionLabel : '')" in Index


def test_r03_on_phones_and_tablets_the_checkout_stepper_keeps_bag_and_checkout():
    """R-03 (26 Oct, tablets added the same day): below 1024 px the Checkout step's stepper is Bag — Checkout, so it
    fits beside the bag icon (kept) and Exit; Design, Customize and Bag keep the whole stepper."""
    Index = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "@media (max-width: 1023.98px) { .site-header .h-nav.at-checkout .h-step-early { display: none; } }" in Index
    assert """<div class="h-nav flex items-center justify-center min-w-0" :class="['order','confirmation'].includes(view) ? 'at-checkout' : ''">""" in Index
    Steps = Index[Index.index('<div x-show="inStudio" x-cloak class="flex items-center gap-1 md:gap-3 leading-none">'):]
    Steps = Steps[:Steps.index("</div>")]
    assert Steps.count("h-step-early") == 4 and Steps.index(">Customize</button>") < Steps.rindex("h-step-early") < Steps.index(">Bag</button>")


def test_sign_in_wording_and_the_terms_checkbox_are_the_owners_and_await_counsel():
    """The owner's decisions of 28 Oct, business-approved for proto and Atelier alike, not yet reviewed by legal counsel
    (Legal Review Pack T1, T3, T10): the Terms say how a customer signs in — an email address and a password, or Google —
    and that a six-letter code belongs to the earlier sign-in, is not issued to a new account and only moves its account
    (R-06); Your content asks for respect of other people's intellectual property and protects the customer's photos and
    personal details; an account is created only with the Terms checkbox ticked (it links the Terms of Service and the
    Privacy Policy), for the email form, Google and Apple alike, and Create account promises no number of credits (R-16).
    The design-use clause waits for counsel, and the Terms still carry no warranty clause (R-31)."""
    Root = Path(__file__).resolve().parents[1]
    Index = (Root / "web" / "index.html").read_text(encoding="utf-8")
    App = (Root / "web" / "app.js").read_text(encoding="utf-8")
    Terms = Index[Index.index("<template x-if=\"view === 'terms'\">"):]
    Terms = Terms[:Terms.index("</template>")]
    assert "Access is by a personal 6-letter sign-in code." not in Terms
    assert ("You create an account and sign in with your email address and a password, or with your Google account." in Terms
            and "Six-letter sign-in codes belong to our earlier sign-in and are not issued when you create an account; "
                "if you have one, it only moves its account to the new sign-in and then stops working." in Terms)
    assert ("Respect other people's intellectual property, such as their artwork, designs and trademarks, as XJet does." in Terms
            and "We never show the photos you upload in the gallery or use them to promote Atelier." in Terms
            and "We do not display, promote or make for anyone else a design that shows an identifiable person or carries "
                "personal details such as a name, initials or a date, unless you allow it." in Terms)
    assert "royalt" not in Terms.lower() and "Warranty." not in Terms
    Dialog = Index[Index.index('<div x-show="!legacyAuth">'):]
    Dialog = Dialog[:Dialog.index("<!--legacy-->")]
    Box = Dialog[Dialog.index('<input id="rg-terms" type="checkbox" x-model="regForm.acceptTerms"'):]
    Box = Box[:Box.index("</label>")]
    assert ('<a href="{{BASE}}/terms"' in Box and ">Terms of Service</a>" in Box
            and '<a href="{{BASE}}/privacy"' in Box and ">Privacy Policy</a>." in Box)
    Register = Dialog[Dialog.index('<form x-show="regMode === \'register\' && !regInfo"'):]
    Register = Register[:Register.index("</form>")]
    assert Dialog.index('id="rg-terms"') < Dialog.index("Continue with Google") < Dialog.index("Continue with Apple") < Dialog.index(Register)
    Waits = ':aria-disabled="termsPending ? \'true\' : null" :class="termsPending ? \'opacity-50\' : \'\'"'
    assert Dialog.count(Waits) == 3 and Register.count(Waits) == 1 and "By creating an account" not in Index
    Legacy = Index[Index.index('<form x-show="regMode === \'email\' && !regInfo"'):]
    Legacy = Legacy[:Legacy.index("</form>")]
    assert ('<input id="reg-terms" type="checkbox" x-model="regForm.acceptTerms" required' in Legacy
            and ':disabled="regLoading || !regForm.acceptTerms"' in Legacy)
    assert "get termsPending() { return this.regMode === 'register' && !this.regForm.acceptTerms; }" in App
    assert (App.count("if (this._termsMissing()) return;") == 2                     # Google or Apple, and Create account
            and "if (!this.regForm.acceptTerms) throw new Error(TERMS_REQUIRED);" in App)  # the older sign-in's form
    assert "A new account starts with" not in Index


def test_r05_how_it_works_promises_the_360_movie_for_rings_only():
    """R-05 (26 Oct, a text-only exception in How It Works): step 3 promises the 360° movie for rings only — the same
    sentence when rings alone are on offer, "rings come with a 360° movie" beside charms, none for charms alone."""
    Index = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    How = Index[Index.index('<section id="how-it-works"'):]
    How = How[:How.index("</section>")]
    assert ("desc:'Select your metal and ' + wording.sizeWord + (wording.noun === 'ring' ? ', and watch your ring in a 360° movie.' : "
            "wording.ring ? '; rings come with a 360° movie.' : '.')}") in How
