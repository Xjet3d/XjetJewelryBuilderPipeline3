"""Audit B4 (AI-01, PRD-01, CON-05, CON-07, PRV-05): the site says what it does — an AI-preview note where designs are
chosen, no absolute claims for steel, no Apple sign-in while it is not offered, production "up to 14 days", payment that
never holds production, and every storage item of the site in the Privacy page."""

import pytest

from p3.settings import RepoRoot
from tests.conftest import Harness

Web, P3 = RepoRoot / "web", RepoRoot / "p3"
Sources = [Web / "index.html", Web / "app.js", P3 / "mail.py", P3 / "quotes.py", P3 / "payments.py", P3 / "sitepages.py"]


def Text(P):
    return P.read_text(encoding="utf-8")


def test_no_absolute_claims_for_steel():
    for P in Sources:
        assert "hypoallergenic" not in Text(P).lower() and "does not tarnish" not in Text(P), P.name
    assert "If you have a metal allergy, check the material with your doctor before ordering." in Text(Web / "app.js")


def test_production_is_up_to_14_days_and_payment_never_holds_it():
    for P in Sources:
        assert "7–10" not in Text(P) and "before production starts" not in Text(P), P.name
        assert "Production starts once your payment is confirmed" not in Text(P), P.name
    assert "production takes up to 14 days" in Text(P3 / "mail.py") and "Production takes up to 14 days." in Text(P3 / "sitepages.py")


def test_the_ai_preview_note_on_design_and_customize():
    Html = Text(Web / "index.html")
    assert Html.count('data-testid="ai-preview-note"') == 2
    assert "AI-generated preview: these images are renderings of your design, not photographs." in Html
    assert "AI-generated preview: the images and the movie are renderings of your design, not photographs" in Html


def test_privacy_names_google_only_and_every_storage_item():
    Html = Text(Web / "index.html")
    Privacy = Html[Html.index("<template x-if=\"view === 'privacy'\">"):Html.index("<template x-if=\"view === 'contact'\">")]
    assert "Apple" not in Privacy
    for Name in ("atelier_session", "atelier_oauth", "atelier_pending", "p3_session", "p3_profile", "p3_state", "atelier_consent",
                 "p3_gallery_pending", "p3_fav_pending", "p3_co_draft"):
        assert f">{Name}</span>" in Privacy, Name


@pytest.fixture
async def H(tmp_path):
    Obj = Harness(tmp_path)
    yield Obj
    await Obj.Close()


async def test_the_served_faq_and_shipping_description_say_up_to_14_days(H):
    assert "Production takes up to 14 days." in (await H.Client.get("/faq")).text
    assert "7–10" not in (await H.Client.get("/shipping-returns")).text
