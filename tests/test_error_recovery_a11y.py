"""Owner's approved plan (14 Nov 2026): Task 2 (FE-04, FE-05, FE-06 — failures are recovered, not shown as empty or
failed), Task 4 (FE-01 — the start-up's requests go out together; the journey's design beside the credits) and Task 5
(accessibility: announcements, focus, focus rings, targets, reduced motion). The page's behaviour was checked in a
browser; these checks keep the code that does it in place."""

import re
from pathlib import Path

Web = Path(__file__).resolve().parents[1] / "web"
Js = (Web / "app.js").read_text(encoding="utf-8")
Html = (Web / "index.html").read_text(encoding="utf-8")


def _Fn(Name: str) -> str:
    """The body of a method of p3App (up to the next method at the same indentation)."""
    M = re.search(r"\n    (?:async )?" + re.escape(Name) + r"\(.*?\) \{\n(.*?)\n    \},?\n", Js, re.S)
    assert M, Name
    return M.group(1)


# ── FE-04: a catalog failure no longer stops the page ────────────────────
def test_the_catalog_failure_is_caught_and_the_start_up_goes_on():
    Init = Js[Js.index("const authCfgP = "):Js.index("    // FE-04: the catalog (products, metals, prices). A failure")]
    assert "this._loadCatalog()" in Init and "this.api('GET', '/api/catalog'" not in Init
    assert "try { await this._bootSignedIn(authCfgP, sessionP, authReturn); }" in Init
    Load = _Fn("_loadCatalog")
    assert "catch (_) { this.bootError = 'catalog'; return false; }" in Load
    Boot = _Fn("_bootSignedIn")
    # sign-in, Back / Forward and the journey's screen do not depend on the catalog
    assert "this._bootAuth(" in Boot and "addEventListener('popstate'" in Boot and "this._openFlow(P3_FLOW" in Boot
    assert 'x-show="bootError"' in Html and '@click="retryCatalog()"' in Html


# ── FE-05: loading, empty and failed are three different things ──────────
def test_a_failed_read_is_never_shown_as_an_empty_bag_or_list():
    Api = _Fn("api")
    assert "new AbortController()" in Api and "method === 'GET'" in Api and "'timeout'" in Api
    assert "`Request failed (${resp.status})`" not in Api and "The service is briefly unavailable" in Api
    Bag = _Fn("refreshBag")
    assert "this.bagState = 'ok'" in Bag and "this.bagState = 'error'" in Bag
    assert 'x-show="token && bag && bagLines.length === 0"' in Html               # "empty" only after an answer
    assert 'x-show="token && bagState === \'error\'"' in Html and "Loading your bag…" in Html
    assert "ordersState === 'ok' && !orders.length" in Html and "ordersState === 'error'" in Html
    assert "myQuotesState === 'ok' && !myQuotes.length" in Html and "myQuotesState === 'error'" in Html
    for Name in ("loadOrders", "loadMyQuotes"):
        assert "State = 'error'" in _Fn(Name) and "State = 'ok'" in _Fn(Name)


# ── FE-06: a design made and charged is never reported as failed ─────────
def test_a_made_design_that_cannot_be_shown_is_kept_and_found():
    Gen = _Fn("generate")
    Post = Gen.index("this.api('POST', '/api/designs', fd)")
    Failed = Gen.index("design_generation_failed")
    Shown = Gen.index("this._showMadeDesign(batch.design_id)")
    assert Failed < Shown and Gen.index("this.persist({ designId: batch.design_id })") < Shown and Post < Failed
    assert "design_generation_failed" not in Gen[Shown:]                           # after the POST: never "failed"
    Show = _Fn("_showMadeDesign")
    assert "this.composeNotice = {" in Show and "No new request is needed" in Show
    assert "this._showMadeDesign(b.design_id)" in _Fn("refine")                     # a refinement made into a new design
    Open = _Fn("openDesign")
    assert "const seq = ++this._openSeq;" in Open and "if (seq !== this._openSeq) return;" in Open
    assert 'x-show="composeNotice"' in Html and '@click="openNoticeDesign()"' in Html and "Open My Designs" in Html


# ── FE-01: what the first screen needs, asked for at once ───────────────
def test_the_start_up_requests_go_out_together():
    Init = Js[Js.index("const authCfgP = "):Js.index("    // FE-04: the catalog (products, metals, prices). A failure")]
    assert Init.index("/api/auth/config") < Init.index("await Promise.all(") and Init.index("/api/auth/session") < Init.index("await Promise.all(")
    assert "let gallery = P3_FLOW ? null : this.loadGallery();" in Init               # a journey step: the gallery after
    Auth = _Fn("_bootAuth")
    assert "await (pre.cfg ||" in Auth and "await (pre.session ||" in Auth
    Boot = _Fn("_bootSignedIn")
    assert "if (!P3_FLOW) await quota;" in Boot
    assert "await Promise.all([this._openFlow(P3_FLOW, { history: 'replace', restore: true }), quota]);" in Boot
    Flow = _Fn("_openFlow")
    assert "at === 'checkout' || at === 'order'" in Flow                              # the bag and checkout open at once


# ── Task 5: accessibility ───────────────────────────────────────────────
def test_results_and_customize_changes_are_announced_and_screens_take_the_focus():
    assert 'role="status" aria-live="polite" aria-atomic="true" x-text="srMsg"' in Html
    assert "this._announceResults(b)" in _Fn("onBatchUpdated")
    assert Html.count("data-results-heading tabindex=\"-1\"") == 2
    assert "this.announce(this.custSpoken)" in _Fn("patchCustomization")
    assert "if (history === 'push') this._focusScreen();" in _Fn("navigateTo")
    assert "Preview only metals — shows them and previews one; not for sale" in Html


def test_focus_rings_targets_contrast_and_motion():
    Css = Html[:Html.index("</style>")]
    assert ".nav-icon:focus-visible { outline: 2px solid #8A6420" in Css
    assert ".metal-swatch:focus-visible { outline: 2px solid #8A6420" in Css
    assert "--tw-ring-color: #8A6420" in Css and ".cz-panel .text-zinc-500, .cz-panel .promo-was { color: #52525B; }" in Css
    assert "background: #8A6420; color: #fff; font-size: 11px" in Css          # the bag count badge (was #B08040, 3.5:1)
    assert ".dot-target { width: 24px; height: 24px;" in Css and 'class="dot-target"' in Html
    assert "min-target" in Html and ".min-target { min-height: 24px;" in Css
    assert "@media (prefers-reduced-motion: reduce) { .typing-dot, .shimmer, .sc-caret { animation: none !important; } }" in Css
    assert "opts.action ? 9000 : 5000" in Js and "toast-up" in Html
