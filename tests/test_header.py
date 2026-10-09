"""The header: a utility row (My Designs · ♥ Favorites · bag · account) above the main row (XJet Atelier · the
pages · Start Designing) on the site's pages; one row on phones and on the Design screen. One grid, so every control
exists once; Sign out lives in the account menu, not in the header."""

import re

from tests.test_gallery import HG  # noqa: F401 — the fixture is used by name


def _Header(Index):
    Start = Index.index('aria-label="Main"')
    Open = Index.rindex("<nav", 0, Start)
    Nav = Index[Open:Index.index("</nav>", Start)]
    Bar = Nav[:Nav.index('<div id="mobile-menu"')]           # the header itself, without the menu panel
    return Nav, Bar


def _Part(Bar, Cls):
    I = Bar.index(f'<div class="{Cls} ')
    Ends = [Bar.index(F'<div class="{C} ', I + 1) for C in ("h-brand", "h-nav", "h-util", "h-cta") if F'<div class="{C} ' in Bar[I + 1:]]
    return Bar[I:min(Ends) if Ends else len(Bar)]


async def test_one_grid_two_rows_on_the_site_one_row_on_phones_and_in_the_studio(HG):
    H = HG
    Index = (await H.Client.get("/")).text
    Nav, Bar = _Header(Index)
    assert 'class="site-header ' in Nav and ":class=\"inStudio ? 'is-studio' : ''\"" in Nav
    for Cls in ("h-brand", "h-nav", "h-util", "h-cta"):
        assert Bar.count(f'<div class="{Cls} ') == 1, Cls
    Css = Index[Index.index("<style>"):Index.index("</style>")]
    assert 'grid-template-areas: "brand nav util cta";' in Css                  # phones and the Design screen
    Wide = Css[Css.index("@media (min-width: 768px) {", Css.index(".site-header {")):]
    assert '.site-header:not(.is-studio) {' in Wide and 'grid-template-areas: "util util util" "brand nav cta";' in Wide
    # every control exists once (the menu panel below 1024 px is a list of its own)
    for Call in ("openPanel('designs')", "openPanel('favorites')", "openBag()", "toggleAccountPanel()", "resetAIFlow()"):
        assert Bar.count(Call) == 1, Call


async def test_the_utility_row_my_designs_favorites_bag_account(HG):
    H = HG
    Index = (await H.Client.get("/")).text
    Nav, Bar = _Header(Index)
    Util = _Part(Bar, "h-util")
    Order = [Util.index(X) for X in ("openPanel('designs')", "openPanel('favorites')", "openBag()", "toggleAccountPanel()")]
    assert Order == sorted(Order)
    assert "favorites.length" in Util and 'x-text="bagCount"' in Util                      # both counts kept
    # the account: person icon · name · coin · generations left · ˅, anchored for its dropdown
    Acct = Util[Util.index("data-account-button"):]
    Acct = Acct[:Acct.index("</button>")]
    for Piece in ('<circle cx="12" cy="8.2" r="3.6"/>', 'x-text="displayName"', '>·</span>', 'fill="#C9A96E"', 'x-text="quotaRemaining"', 'd="M19 9l-7 7-7-7"'):
        assert Piece in Acct, Piece
    # signed out: Sign in in the account's place; no Sign out in the header any more
    assert 'x-show="!userSession && !inStudio" @click="openRegModal()"' in Util and "<span>Sign in</span>" in Util
    assert "logout()" not in Bar and not re.search(r">\s*Sign out\s*<", Bar) and "'Sign out'" not in Bar


async def test_the_main_row_brand_a_short_navigation_and_start_designing(HG):
    H = HG
    Index = (await H.Client.get("/")).text
    Nav, Bar = _Header(Index)
    Main = _Part(Bar, "h-nav")
    Links = re.findall(r">([A-Za-z ]+)</button>", Main[:Main.index('<div id="about-menu"')])
    assert [L.strip() for L in Links] == ["How It Works", "Inspiration", "Materials"]
    About = Main[Main.index('<div id="about-menu"'):]
    for View, Label in (("designers", "About XJet"), ("technology", "Technology"), ("faq", "FAQ"), ("contact", "Support")):
        assert f"navigateTo('{View}')" in About and f">{Label}</button>" in About
    assert 'aria-controls="about-menu"' in Main and "@click.outside=\"aboutOpen = false\"" in Main
    Cta = _Part(Bar, "h-cta")
    assert 'x-show="!inStudio" @click="resetAIFlow()" class="btn-gold' in Cta and ">Start Designing</button>" in Cta
    assert 'x-show="inStudio" @click="navigateTo(\'home\')"' in Cta and ">Exit</button>" in Cta   # Exit only in the studio
    assert "XJet Atelier — home" in _Part(Bar, "h-brand")
    # the menu panel (below 1024 px) still lists every page
    Menu = Nav[Nav.index('<div id="mobile-menu"'):]
    for View in ("inspiration", "materials", "technology", "faq", "designers", "contact"):
        assert f"navigateTo('{View}')" in Menu, View


async def test_the_account_menu_is_a_dropdown_with_sign_out_last(HG):
    H = HG
    Index = (await H.Client.get("/")).text
    I = Index.index('data-testid="account-menu"')
    Menu = Index[I:Index.index("\n    </template>", I)]
    assert ":style=\"{ top: accountPanelPos.top + 'px', right: accountPanelPos.right + 'px'" in Menu
    for Piece in ('x-text="displayName || \'Your account\'"', 'x-text="displayEmail"', 'x-text="quotaRemaining"', "Your sign-in code", "Quotes &amp; orders", "openAccountPage()",
                  "openPanel('designs')", "openPanel('favorites')"):
        assert Piece in Menu, Piece
    Buttons = re.findall(r'<button type="button" @click="([^"]+)"', Menu)
    assert Buttons[-1] == "signOutFromAccount()"
    App = (await H.Client.get("/static/app.js")).text
    Place = App[App.index("_placeAccountPanel() {"):]
    assert "document.querySelector('[data-account-button]')" in Place[:Place.index("\n    },")]
    Bag = App[App.index("openBag() {"):]
    assert "this._pendingPanel = 'bag'; this.openRegModal(); return;" in Bag[:Bag.index("\n    },")]
    After = App[App.index("async _afterSignIn(fromVerification) {"):]
    assert "if (panel === 'bag') { this.signInNotice = null; this.goToCheckout(); }" in After[:After.index("\n    },")]
    assert "this.menuOpen = false; this.aboutOpen = false;" in App[App.index("navigateTo(v) {"):][:200]
