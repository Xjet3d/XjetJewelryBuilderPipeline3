"""B5 — the legal pages as published on 4 Nov 2026 with the owner's approval of 3 Nov, legal counsel's review pending. The
wording is the Counsel Book's Appendix A: the full Terms of Service (A-1), Privacy Policy (A-2), Shipping & Returns (A-3),
the checkout and quote-form notices (A-4), the confirmation email's block (A-5), the Warranty (A-8) and Accessibility (A-9)
pages and the credits FAQ (A-10b). The company facts are XJet's own (3 Nov 2026), counsel's options are the owner's
choices, no placeholder is left, no fixed period the systems do not verify is promised, and no mechanism that does not
exist yet (N1's per-design gallery permission) is promised."""

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
Company = "XJet Ltd., 4 Oppenheimer St., Science Park, Rehovot 7670102, Israel, company number 513655233"


def _Text(Html: str) -> str:
    """What a reader gets from markup: its words, without tags and comments."""
    return re.sub(r"<[^>]+>", " ", re.sub(r"<!--.*?-->", "", Html, flags=re.S))


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
        assert Live.count("<h1") == 1 and Heading in Live and "4 November 2026" in Live, View
    Map = (await H.Client.get("/sitemap.xml")).text
    assert "/warranty</loc>" in Map and "/accessibility</loc>" in Map          # listed like the other legal pages
    for Link in ("/warranty", "/accessibility"):
        assert f'href="{{{{BASE}}}}{Link}"' in Footer, Link
    assert "XJet Ltd. · 4 Oppenheimer St., Science Park, Rehovot 7670102, Israel · Company No. 513655233 · +972-73-2694620" in Footer


def test_the_company_facts_are_xjets_and_no_placeholder_is_left():
    Terms, Privacy = SitePages.LegalText(Index, "terms"), SitePages.LegalText(Index, "privacy")
    Shipping = SitePages.LegalText(Index, "shipping-returns")
    for Text in (Terms, Privacy):
        assert Company in Text and "Last updated: 4 November 2026" in Text
    assert "Company No. 513655233 · VAT No. 513655233" in Terms and "+972-73-2694620" in Terms
    Legal = Index[Index.index("<template x-if=\"view === 'shipping-returns'\">"):Index.index("<template x-if=\"view === 'accessibility'\">")]
    for Mark in ("[registered address]", "[company number]", "[date]", "[Option", "[Q14", "[Phone", "[period]", "[30 days]",
                 "[7 years]", "[90 days]", "[10 business days]", "[Sunday", "representative: …", "[only once", "[SEC-02",
                 "if counsel decides", "if counsel so decides", "DRAFT B5", "never pushed"):
        assert Mark not in Index and Mark not in Footer, Mark
    # The owner's choices (3 Nov 2026): the customer's rights in a design (N7 Option A) and the 14-day cancellation of an
    # unchanged gallery design (Q2 Option 1); Israeli law with mandatory protections; the business days
    assert "they belong to you, subject to the license above." in Terms and "they belong to XJet Ltd." not in Terms
    assert "If you order a gallery design without changing it, you can cancel within 14 days of receiving it" in Shipping
    assert "the rules for personalised pieces apply." not in Shipping
    assert "governed by the laws of Israel, subject to the mandatory consumer protections" in Terms
    assert "Where the law permits, the courts of Tel Aviv have jurisdiction." in Terms
    assert "Sunday to Thursday, excluding public holidays in Israel" in Shipping
    # No fixed period the systems do not verify (owner, 3 Nov 2026): retention, reply times, lost parcels, remakes
    for Promise in ("delete them within", "for 7 years", "technical logs for 90", "Backups are overwritten within",
                    "We reply within 5", "reply within one business day and can help", "prepaid label",
                    "if it is lost, we remake", "remake it in another size", "at least 30 days before"):
        assert Promise not in Terms + Privacy + Shipping + _Text(Legal), Promise
    assert "If your parcel is more than 10 business days late, contact us and we will investigate with the carrier." in Shipping


def test_the_terms_promise_no_gallery_permission_that_is_not_built_yet():
    """N1 (Counsel Book A-7): nothing asks for or records a per-design gallery permission, so neither the Terms nor the
    sentence beside every Terms acceptance promise one (docs/LEGAL-DRAFT-N1-GALLERY-PERMISSION.md)."""
    Terms = SitePages.LegalText(Index, "terms")
    for Text in (Terms, SitePages.DesignUseAcceptance):
        assert "only after you allow it for that design" not in Text
    assert SitePages.DesignUseAcceptance.endswith("other people's intellectual property stays theirs.")
    # nor do the copies beside the sign-up form, the Google sign-up and the Terms gate: each is the sentence, whole
    assert "only after you allow it" not in Index
    Copies = [Index[I:Index.index("</p>", I)] for I in [M for M in range(len(Index)) if Index.startswith("By accepting, you agree that XJet", M)]]
    assert len(Copies) == 3 and all(C == SitePages.DesignUseAcceptance for C in Copies), Copies


def test_the_shekel_wording_is_true_whether_israeli_pricing_is_on_or_off():
    """Terms 10 and Shipping & Returns say the currency in one static sentence (an Admin switch never makes a Terms
    version, as T12 does for materials) and name no VAT rate (a setting): the checkout shows the rate and the total."""
    Terms, Shipping = SitePages.LegalText(Index, "terms"), SitePages.LegalText(Index, "shipping-returns")
    for Text in (Terms, Shipping):
        assert ("Prices are in US dollars, and we do not add sales tax. For delivery to an address in Israel, where we offer "
                "prices in shekels, prices are shown and charged in Israeli shekels and include Israeli VAT") in Text
        assert "18%" not in Text
    Privacy = SitePages.LegalText(Index, "privacy")
    assert "p3_currency" in Privacy and "Currency choice: one year." in Privacy


def test_the_drafts_keep_the_owner_rules():
    """No Apple sign-in; production "up to 14 business days" once payment is confirmed (owner, 11 Nov 2026); the Privacy
    Policy names no endpoint, key, model or infrastructure; no unverified insurance claim."""
    Terms, Privacy = SitePages.LegalText(Index, "terms"), SitePages.LegalText(Index, "privacy")
    Shipping = SitePages.LegalText(Index, "shipping-returns")
    for Text in (Terms, Privacy, Shipping):
        assert "Apple" not in Text
    assert "Production: starts once your payment is confirmed and takes up to 14 business days." in Shipping and "fully insured" not in Shipping + (P3 / "orders.py").read_text(encoding="utf-8")
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
    for Text in ('data-testid="pre-order-summary"', Company,
                 "nothing is charged now", "You must be 18 or older to place an order.",
                 "keeps order records as long as the law requires for tax and warranty purposes",
                 "A gallery design you order unchanged can be cancelled within 14 days of delivery.",
                 "'I understand that each personalised ' + bagKind + ' is made to order to my specification",
                 "— payment after our review", "'Place order'"):
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
                 "We will then contact you to arrange payment; production starts once your payment is confirmed.",
                 Company + ". Made in Israel.", "A gallery design you order unchanged can be cancelled within 14 days",
                 "XJet Atelier is a service of XJet Ltd.,",
                 "https://a.example/JewelryB2C3/warranty", "Terms of Service version v3, available at https://a.example/JewelryB2C3/terms."):
        assert Text in Html, Text
    assert "Reply to this email" not in Html
    _S, Paid = Mail.OrderConfirmationEmail({**Order, "payment_status": "paid"})
    assert "arrange payment" not in Paid and "available on our site." in Paid


async def test_the_faq_says_the_currency_israeli_pricing_gives(tmp_path):
    from tests.test_b6_ils_pricing import AdminKey, _Fetcher
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        assert "Yes — all prices are in US dollars." in (await H.Client.get("/faq")).text
        I = H.Ctx.IlsPricing
        I.Fetcher = _Fetcher("3.136")
        I.Fetch()
        I.Save({**I.Settings(), "enabled": True}, "test")
        Page = (await H.Client.get("/faq")).text
        assert ("Prices are in US dollars, except for delivery to an address in Israel: there they are shown and charged in "
                "Israeli shekels and include VAT (18%).") in Page and "Yes — all prices are in US dollars." not in Page
    finally:
        await H.Close()


def test_the_site_names_the_company_the_same_way_everywhere():
    Org = SitePages.Organization("https://a.example/JewelryB2C3/", "atelier@xjet3d.com")
    assert Org["address"] == {"@type": "PostalAddress", "streetAddress": "4 Oppenheimer St., Science Park",
                              "addressLocality": "Rehovot", "postalCode": "7670102", "addressCountry": "IL"}
    assert Org["telephone"] == "+972-73-2694620" and Org["vatID"] == "513655233"
    Contact = Index[Index.index("<template x-if=\"view === 'contact'\">"):Index.index("<template x-if=\"view === 'warranty'\">")]
    assert "XJet Ltd., 4 Oppenheimer St., Science Park, Rehovot 7670102, Israel · Company No. 513655233 · VAT No. 513655233" in Contact
    Html = Mail._Layout("Heading", "<p>Body</p>")                  # every email's frame
    assert "XJet Atelier is a service of XJet Ltd.," in Html and "Rehovot 7670102, Israel · Company No. 513655233" in Html


async def test_the_served_faq_explains_credits(H):
    Page = (await H.Client.get("/faq")).text
    assert "a request our content check declines uses no credit" in Page
    assert "Credits are free and do not expire, but they have no cash value and cannot be bought, sold or transferred." in Page
