"""B5 — the legal pages as publication-ready DRAFTS (branch b5-legal-drafts: never pushed, pending legal counsel). The
wording is the Counsel Book's Appendix A: the full Terms of Service (A-1), Privacy Policy (A-2), Shipping & Returns (A-3),
the checkout and quote-form notices (A-4), the confirmation email's block (A-5), the Warranty (A-8) and Accessibility (A-9)
pages and the credits FAQ (A-10b). Placeholders stay visible in brackets; nothing invents a company fact; text only."""

import re

import pytest

from p3 import mail as Mail
from p3 import sitepages as SitePages
from p3.settings import RepoRoot
from tests.conftest import Harness
from tests.test_seo import _Live

Web, P3 = RepoRoot / "web", RepoRoot / "p3"
Index = (Web / "index.html").read_text(encoding="utf-8")
App = (Web / "app.js").read_text(encoding="utf-8")
Footer = (Web / "partials" / "site-footer.html").read_text(encoding="utf-8")


@pytest.fixture
async def H(tmp_path):
    Obj = Harness(tmp_path)
    yield Obj
    await Obj.Close()


async def test_the_warranty_and_accessibility_pages_are_legal_pages_with_their_own_url(H):
    for View, Heading in (("warranty", "XJet Atelier 2-Year Limited Warranty"), ("accessibility", "Accessibility at XJet Atelier")):
        assert View in SitePages.Legal and SitePages.Paths[View] == View
        R = await H.Client.get("/" + View)
        assert R.status_code == 200, View
        Live = _Live(R.text, View)
        assert Live.count("<h1") == 1 and Heading in Live and "[date]" in Live, View
    Map = (await H.Client.get("/sitemap.xml")).text
    assert "/warranty</loc>" in Map and "/accessibility</loc>" in Map          # listed like the other legal pages
    for Link in ("/warranty", "/accessibility"):
        assert f'href="{{{{BASE}}}}{Link}"' in Footer, Link
    assert "XJet Ltd. · [registered address] · Company number [company number]" in Footer


def test_the_placeholders_stay_visible_and_no_company_fact_is_invented():
    Terms, Privacy = SitePages.LegalText(Index, "terms"), SitePages.LegalText(Index, "privacy")
    for Text in (Terms, Privacy):
        assert "[registered address]" in Text and "company number [company number]" in Text and "Last updated: [date]" in Text
    for Placeholder in ("[30 days]", "[Israel]", "[Tel Aviv]", "[Option A:", "[Option B:", "[Q14:", "[Phone: …]"):
        assert Placeholder in Terms, Placeholder
    for Placeholder in ("[7 years]", "[90 days]", "[period]", "[EU representative: …]", "[UK representative: …]"):
        assert Placeholder in Privacy, Placeholder
    Shipping = SitePages.LegalText(Index, "shipping-returns")
    for Placeholder in ("[Option 1, if counsel decides a right to cancel applies:]", "[Option 2, if counsel decides it is exempt:]",
                        "[10 business days]", "[Sunday to Thursday, except public holidays in Israel]"):
        assert Placeholder in Shipping, Placeholder


def test_the_drafts_keep_the_owner_rules():
    """No Apple sign-in; production "up to 14 days"; payment never holds production; the Privacy Policy names no
    endpoint, key, model or infrastructure; no unverified insurance claim."""
    Terms, Privacy = SitePages.LegalText(Index, "terms"), SitePages.LegalText(Index, "privacy")
    Shipping = SitePages.LegalText(Index, "shipping-returns")
    for Text in (Terms, Privacy, Shipping):
        assert "Apple" not in Text and "production starts after payment" not in Text.lower()
    assert "up to 14 days" in Shipping and "fully insured" not in Shipping + (P3 / "orders.py").read_text(encoding="utf-8")
    for Word in ("fal", "nano-banana", "gemini", "openai", "cloudflare", "linode", "akamai", "/api/", "smtp", "microsoft 365"):
        assert not re.search(r"\b" + re.escape(Word) + r"\b", Privacy.lower()), Word
    # The cookie section is the live text, word for word (PRV-05)
    for Text in ("Essential — always active.", "Atelier does not currently use marketing or advertising cookies.",
                 "View technical cookie names"):
        assert Text in Privacy


def test_the_terms_sentences_are_the_same_in_the_app_and_on_the_server():
    """termsService / termsOrders (web/app.js) and their server copies (p3/sitepages.py) say the same words."""
    Server = SitePages._PageFallbacks("terms", None, ("ring", "charm"), {})
    assert Server["termsService"].startswith("XJet Atelier lets you describe or upload an idea for a ring or a charm")
    assert "return 'XJet Atelier lets you describe or upload an idea for ' + idea" in App
    Orders = Server["termsOrders"]
    assert Orders.startswith("Prices are fixed per piece and material (a charm’s price also depends on its size; a ring’s "
                             "size does not change its price). An order is a reservation")
    for Sentence in ("You can cancel an order free of charge until we confirm it to you.",
                     "If we cannot make your design, we tell you and you pay nothing.", "your design is not exclusive."):
        assert Sentence in Orders and Sentence in App, Sentence


def test_checkout_and_quote_notices_are_text_only():
    Order = Index[Index.index("<template x-if=\"view === 'order'\">"):]
    Order = Order[:Order.index("Order confirmation — received")]
    for Text in ('data-testid="pre-order-summary"', "XJet Ltd., [registered address], company number [company number]",
                 "nothing is charged now", "You must be 18 or older to place an order.",
                 "keeps order records for [7 years] for tax and warranty purposes",
                 "'I understand that each personalised ' + bagKind + ' is made to order to my specification",
                 "'Place order — payment arranged after confirmation'"):
        assert Text in Order, Text
    assert 'x-model="co.terms"' in Order and Order.count('id="co-terms"') == 1          # the same box, the same model
    assert "XJet Ltd. uses these details to prepare and send your quote and to contact you about it." in Index


def test_the_confirmation_email_is_a_durable_record_of_the_order():
    Order = {"ref": "ORD-10001", "customer": {"first_name": "Dana"}, "lines": [], "subtotal": 0, "discount": 0, "shipping": 0,
             "total": 0, "currency": "USD", "address_lines": ["x"], "payment_status": "pending", "payment_label": "Payment pending",
             "payment_message": "", "shipping_label": "Standard delivery", "shipping_eta": "up to 14 business days",
             "promo_code": None, "terms_version": "v3"}
    _S, Html = Mail.OrderConfirmationEmail(Order, "https://a.example", "https://a.example/JewelryB2C3/#account")
    for Text in ("Our team will check that your design can be made", "You can cancel free of charge until then",
                 "Production starts as soon as we confirm your order; we will contact you separately to arrange payment.",
                 "XJet Ltd., [registered address], company number [company number]. Made in Israel.",
                 "https://a.example/JewelryB2C3/warranty", "Terms of Service version v3, available at https://a.example/JewelryB2C3/terms."):
        assert Text in Html, Text
    assert "Reply to this email" not in Html
    _S, Paid = Mail.OrderConfirmationEmail({**Order, "payment_status": "paid"})
    assert "arrange payment" not in Paid and "available on our site." in Paid


async def test_the_served_faq_explains_credits(H):
    Page = (await H.Client.get("/faq")).text
    assert "a request our content check declines uses no credit" in Page
    assert "Credits are free and do not expire, but they have no cash value and cannot be bought, sold or transferred." in Page
