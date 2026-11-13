"""Customize, Bag and Checkout in the owner's Option B "Boutique" (13 Nov 2026): one Add to Bag with the quantity beside it,
the details in collapsible sections, the AI-preview note always shown, the price with the promotion's tag — the regular
price and the saving only when the server sends them (verified reference prices, unchanged rules) — the same prices and
words in the bag and checkout, Checkout with the total; the charm's ID under its name; a reload of Customize in the
visitor's own currency."""

import re

import pytest

from p3.settings import RepoRoot
from tests.conftest import Harness, PreviewEmail
from tests.test_b6_ils_pricing import _Fetcher

Web = RepoRoot / "web"
AdminKey = "boutique-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}


def _View(Html: str, Name: str) -> str:
    Start = Html.index(f'<template x-if="{Name}">')
    return Html[Start:Html.index("\n    </template>\n", Start)]


def _Pages():
    return (Web / "index.html").read_text(encoding="utf-8"), (Web / "app.js").read_text(encoding="utf-8")


def test_customize_has_one_add_to_bag_beside_the_quantity_and_quiet_links():
    Html, App = _Pages()
    Cust = _View(Html, "view === 'review' && cust")
    assert Cust.count("addToBag()") == 1 and Cust.count('data-testid="add-to-bag"') == 1   # one Add to Bag, never two
    Bar = Cust[Cust.index('class="cz-bar'):]
    assert Bar.index('aria-label="Quantity"') < Bar.index('data-testid="add-to-bag"')       # the quantity beside it
    assert 'x-text="addBagLabel"' in Bar and "openQuoteRequest()" in Bar and 'x-text="materialPreviewText"' in Bar
    assert "fixed bottom-0" in Bar[:300] and "lg:static" in Bar[:300]                        # phones: at the bottom
    # Option B (13 Nov 2026): the bag beside Add to Bag once something is in it; back = the header's "‹ Design"; a new
    # design = a quiet card in the page, never a button in the bar
    assert 'x-show="bagLines.length > 0" x-cloak @click="goToCheckout()" class="cz-bagbtn"' in Bar and 'data-testid="view-bag"' in Bar
    assert Bar.index('data-testid="add-to-bag"') < Bar.index('data-testid="view-bag"')
    assert '@click="startNew(); navigateTo(\'ai-studio\')" class="cz-new mt-6"' in Cust and "startNew()" not in Bar
    assert "@click=\"view === 'review' ? backToDesign() : (design && navigateTo('ai-studio'))\"" in Html
    assert 'class="cz-link"' not in Cust                                                    # the row of three links went
    assert "Your Selected Design" not in Html and "In Your Bag" not in Html                  # the heading and the mini bag went
    Get = App[App.index("get addBagLabel()"):App.index("\n    },", App.index("get addBagLabel()"))]
    assert "'Add to Bag · ' + this.price(this.cust.line_total ?? q.unit_price * n, q.currency)" in Get
    # "Choose your size" only when the size is what is missing; a design whose product is OFF (and any other block) says
    # so on a button that can't be pressed — found on proto before the Atelier release (a paused ring design)
    assert '@click="canAddToBag ? addToBag() : (custNeedsSize && scrollToSize())" :disabled="quotePending || (!canAddToBag && !custNeedsSize)"' in Bar
    Needs = App[App.index("get custNeedsSize()"):App.index("\n    },", App.index("get custNeedsSize()"))]
    assert "['ring_size_required', 'charm_size_required'].includes(this.cust?.add_to_bag_blocked_reason)" in Needs
    assert "if (this.sizeConfirmed) return false;" in Needs


def test_customize_details_collapse_and_the_ai_note_stays_in_view():
    Html, App = _Pages()
    Cust = _View(Html, "view === 'review' && cust")
    Acc = Cust[Cust.index('<div class="cz-acc">'):Cust.index("<!-- The other ways on")]
    for Name, Id in (("size", "charm-size-about-text"), ("pay", "cust-pay-text"), ("ship", "cust-ship-text")):
        assert f"@click=\"toggleCustPanel('{Name}')\"" in Acc and f'aria-controls="{Id}"' in Acc
        assert f'id="{Id}" x-show="custPanel === \'{Name}\'"' in Acc
    # the ordering facts of B4/B6, word for word, inside the sections; "No payment online" also always in view
    for Text in ("<strong class=\"font-semibold text-zinc-800\">No payment online.</strong> After you order, we review your design "
                 "(about 2 business days) and contact you to arrange payment.",
                 "Production: up to 14 business days after your payment is confirmed.", "Standard shipping: up to 14 business days.",
                 'href="{{BASE}}/shipping-returns"'):
        assert Text in Acc, Text
    Trust = Cust[Cust.index('<ul class="cz-trust'):Cust.index("</ul>", Cust.index('<ul class="cz-trust'))]
    assert "No payment online</li>" in Trust and "Free delivery</li>" in Trust and "2-year warranty</li>" in Trust
    # AI-01: the whole note under the picture, never inside a collapsed section
    Note = Cust.index('data-testid="ai-preview-note"')
    assert Note < Cust.index('<div class="cz-acc">') and "AI-generated preview: the images and the movie are renderings" in Cust[Note:Note + 400]
    assert "toggleCustPanel(name) { this.custPanel = this.custPanel === name ? '' : name; }," in App


def test_prices_show_the_tag_and_only_server_sent_claims_everywhere():
    Html, App = _Pages()
    # the regular price and the saving come only with claims (verified reference prices) — the server's rule, unchanged
    assert "promoWas(p, cur, whole = false) { return p?.claims && p.regular_unit_price != null ?" in App
    assert "promoSave(p, cur, whole = false) { return p?.claims && p.savings ?" in App
    assert "promoPill(p, cur, qty = 1) { return p ? [p.label, this.promoSave(p, cur, true)" in App
    Saved = App[App.index("get bagSavingsText()"):App.index("get bagTaxNote()")]
    assert "!l.promotion?.claims" in Saved
    Cust, Bag, Co = (_View(Html, V) for V in ("view === 'review' && cust", "view === 'checkout'", "view === 'order'"))
    for Part in (Cust, Bag, Co):
        assert "promoPill(" in Part and "promoWas(" in Part                                     # one tag, one crossed-out price
        assert "promoLine(" not in Part and "promo-note" not in Part
    assert 'x-text="custPriceFigure"' in Cust and "VAT included" in Cust
    # the same amounts: whole shekels and dollars without cents in all three ("₪159", "$39"; VAT "₪106.32")
    assert "price(v, cur = 'USD') { return this.money(v, cur, true); }," in App
    for Part in (Bag, Co[Co.index('aria-labelledby="co-sum"'):]):
        assert "x-text=\"money(" not in Part


def test_the_bag_summary_and_checkout_with_the_total():
    Html, App = _Pages()
    Bag = _View(Html, "view === 'checkout'")
    Btn = Bag[Bag.index("@click=\"startCheckout()\""):Bag.index("</button>", Bag.index("@click=\"startCheckout()\""))]
    assert 'data-testid="bag-checkout"' in Btn and 'x-text="bagTotalText"' in Btn
    assert Bag.count("startCheckout()") == 1 and "continueFromBag()" in Bag
    Line = Bag[Bag.index('<template x-for="l in bagLines"'):]
    assert "price(l.unit_price, l.currency) + ' each'" in Line and 'x-show="l.quantity > 1 ||' in Line   # each, for two or more
    assert ">Remove</button>" in Line and "removeLine(l)" in Line
    for Text in ("Free · Standard", 'x-text="bagTaxNote"', 'x-text="bagSavingsText"', "Any promo code is added at checkout.",
                 "No payment is taken online: after our review (about 2 business days) we contact you to arrange payment. "
                 "You can cancel an order free of charge until we confirm it to you."):
        assert Text in Bag, Text
    Co = _View(Html, "view === 'order'")
    Tax = App[App.index("get bagTaxNote()"):App.index("\n    },", App.index("get bagTaxNote()"))]
    for Words in ("US dollars · no sales tax added · duties, where charged, paid by the recipient.",
                  "US dollars · including 18% Israeli VAT · a delivery to Israel is charged in shekels.",
                  "Shekels · including 18% VAT for delivery in Israel."):
        assert Words in Tax and Words in Co                                                    # checkout's own words
    Changed = App[App.index("linePriceChanged(l)"):App.index("\n", App.index("linePriceChanged(l)"))]
    assert "Math.abs(l.current_unit_price - l.unit_price) > 0.005" in Changed and 'x-show="linePriceChanged(l)"' in Bag


def test_customize_changes_go_in_order_and_the_newest_answer_wins():
    _, App = _Pages()
    Patch = App[App.index("patchCustomization(custId, patch, opts = {}) {"):App.index("get isLuxury()")]
    assert "const seq = this._custSeq = (this._custSeq || 0) + 1;" in Patch
    assert "seq !== this._custSeq) return;" in Patch
    assert "return (this._custQueue = (this._custQueue || Promise.resolve()).then(send, send));" in Patch


@pytest.fixture
async def HB(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey, AccountEmail=PreviewEmail)
    assert (await Obj.Client.post("/api/admin/login", json={"key": AdminKey})).status_code == 200
    Obj.Client.headers["X-P3-Preview"] = "1"
    assert (await Obj.Client.put("/api/admin/products/availability", json={"product": "charm", "available": True},
                                 headers=Admin)).status_code == 200
    yield Obj
    await Obj.Close()


async def test_customize_names_the_charm_id_as_the_bag_does(HB):
    H = HB
    B = await H.NewDesign("A crescent moon charm with a small star", product="charm")
    C = await H.Proceed(B["design_id"], B["candidates"][0]["id"])
    assert re.fullmatch(r"C-\d{4}-[A-D]", C["ring_id"] or ""), C.get("ring_id")
    Sizes = [S["size"] for S in C["charm_sizes"]]
    assert (await H.Client.put("/api/admin/charm-prices", headers=Admin, json={"materials": {
        "silver": {"fixed_prices": {f"{S:g}": 69 + 20 * I for I, S in enumerate(Sizes)}}}})).status_code == 200
    R = await H.Client.patch(f"/api/customizations/{C['id']}", json={"quantity": 2, "charm_size": Sizes[0], "material_id": "silver"})
    assert R.status_code == 200 and R.json()["ring_id"] == C["ring_id"]
    Bag = await H.Client.post("/api/bag", json={"customization_id": C["id"]})
    assert Bag.status_code == 200, Bag.text
    assert Bag.json()["lines"][0]["ring_id"] == C["ring_id"]


async def test_a_reload_of_customize_shows_the_visitor_s_own_currency(HB):
    H = HB
    B = await H.NewDesign("A crescent moon charm with a small star", product="charm")
    C = await H.Proceed(B["design_id"], B["candidates"][0]["id"])
    I = H.Ctx.IlsPricing
    I.Fetcher = _Fetcher("3.136")
    I.Fetch()
    I.Save({**I.Settings(), "enabled": True}, "test")
    H.Client.cookies.set("p3_currency", "ILS")
    Fresh = (await H.Client.get(f"/api/customizations/{C['id']}")).json()
    D = (await H.Client.get(f"/api/designs/{B['design_id']}")).json()                           # what a reload opens on
    Q = D["customization"]["quote"]
    assert Fresh["quote"]["currency"] == "ILS" and Q["currency"] == "ILS" and Q["incl_vat"] is True
    assert Q["unit_price"] == Fresh["quote"]["unit_price"]
    assert [S["currency"] for S in D["customization"]["charm_sizes"]] == ["ILS"] * len(D["customization"]["charm_sizes"])
    H.Client.cookies.set("p3_currency", "USD")                                                  # a visitor who chose $ abroad
    D = (await H.Client.get(f"/api/designs/{B['design_id']}")).json()
    assert D["customization"]["quote"]["currency"] == "USD"
