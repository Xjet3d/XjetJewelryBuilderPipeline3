"""Audit B3 (ACC-02 to ACC-06): the page keeps the accessibility structure the browser checks proved — a skip link
first, one select button per option card (Enlarge beside it), the dialogs' focus handling, radio-group arrows, the
story's Pause button, the consent banner early in the keyboard order with room kept below it, and one h1 per screen."""

import re

import pytest

from p3.settings import RepoRoot
from tests.conftest import Harness

Web = RepoRoot / "web"


@pytest.fixture
async def Page(tmp_path):
    H = Harness(tmp_path, BasePath="/JewelryB2C3")
    try:
        yield (await H.Client.get("/")).text
    finally:
        await H.Close()


async def test_skip_link_is_the_first_stop_and_its_target_exists(Page):
    Body = Page[Page.index("<body"):]
    First = re.search(r"<(a|button|input|select|textarea)\b[^>]*>", Body[Body.index(">") + 1:])
    assert 'class="skip-link"' in First.group(0) and 'href="#content"' in First.group(0)
    assert 'id="content" tabindex="-1"' in Page


async def test_option_cards_select_with_one_button_and_enlarge_is_beside_it(Page):
    Card = Page[Page.index('class="cand-card'):Page.index("<!-- Phone: which option is in view")]
    assert 'role="button"' not in Card and "@click=\"select(c)\"" in Card
    assert Card.index('class="cand-select') < Card.index("'Enlarge design '")              # siblings, not nested
    assert ':aria-pressed="String(c.id === selectedId)"' in Card
    assert Card.count("pointer-events-none") >= 2                                         # the badges never take the click


async def test_dialogs_take_and_return_the_focus(Page):
    for Name in ("preview", "grid", "quote", "account"):
        assert f"dialogFocus('{Name}'" in Page, Name
    App = (Web / "app.js").read_text(encoding="utf-8")
    assert "dialogFocus(name, open, root)" in App and "const DialogReturn = {};" in App


def test_radio_groups_answer_the_arrow_keys():
    App = (Web / "app.js").read_text(encoding="utf-8")
    assert "closest('[role=\"radiogroup\"]')" in App and "ArrowRight: 1" in App and "e.key === 'Home'" in App


async def test_the_home_story_has_a_pause_button_only_while_it_plays(Page):
    """ACC-02 (WCAG 2.2.2): the home story (under How It Works since 2 Nov; the hero is a photograph) has its own Pause /
    Play while it plays — none with reduced motion, which shows it still; the engine says when a story plays."""
    Home = Page[Page.index('data-ssr-view="home"'):Page.index("""<template x-if="view === 'home' && ssrView !== 'home'">""")]
    Hero = Home[Home.index('class="hero-section"'):Home.index('<section class="hv-sec">')]
    assert "sc-stage" not in Hero and "<video" not in Hero                                  # nothing moves in the hero
    Story = Home[Home.index('<section class="hv-story"'):]
    assert 'x-show="storyReady && !reducedMotion"' in Story and '@click="toggleStory()"' in Story
    assert "storyPaused ? 'Play the animation' : 'Pause the animation'" in Story and "min-h-[44px]" in Story
    assert "new CustomEvent('sc-ready'" in (Web / "showcase.js").read_text(encoding="utf-8")


def test_the_consent_banner_comes_early_and_keeps_room_below_it():
    Js = (Web / "consent.js").read_text(encoding="utf-8")
    assert "document.body.insertBefore(bar, skip ? skip.nextSibling : document.body.firstChild)" in Js
    assert "document.body.appendChild(bar)" not in Js
    assert "html.ac-open main::after" in Js and "scroll-padding-bottom:var(--ac-h)" in Js


def test_one_h1_per_signed_in_screen():
    Html = (Web / "index.html").read_text(encoding="utf-8")
    for View in ("view === 'review' && cust", "view === 'checkout'", "view === 'order'", "view === 'confirmation' && order",
                 "view === 'account'"):
        Start = Html.index(f'<template x-if="{View}">')
        End = Html.index("</template>\n\n", Start) if "</template>\n\n" in Html[Start:] else len(Html)
        assert Html[Start:End].count("<h1") == 1, View
    assert '<div role="main" class="flex-1 flex overflow-hidden' in Html                   # the design studio is a main area


def test_contrast_tokens_were_darkened():
    Html = (Web / "index.html").read_text(encoding="utf-8")
    assert "text-[#6F6F6F] hover:bg-white/60" not in Html
    assert "--gold:#8A6420;" in (Web / "authpage.html").read_text(encoding="utf-8")
