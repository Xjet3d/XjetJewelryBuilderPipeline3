"""WP5 — the Terms a customer accepts. A new order records the archived Terms version its customer accepted (M6): the
version its "I accept the Terms of Service" links to when the order is placed, by number and SHA-256, the same
identifiers a consent record keeps — never the fixed label "2026-10-02", which older orders keep (nothing is back-filled).
And no material status or product switch can change the Terms version: the text is static, the sentences that follow
the products on offer are archived in every form."""

import re
from pathlib import Path

from p3 import materialstatus as MaterialStatus
from p3 import orders as Orders
from p3 import sitepages as SitePages
from p3.config import LoadCatalog
from p3.db import Now
from tests.conftest import Harness
from tests.test_consent import ChangeTerms
from tests.test_orders import Address, Customer, _InBag, _Order
from tests.test_quotes import Admin, AdminKey, _Link, _Offer, _Request

Web = Path(__file__).resolve().parent.parent / "web"
Gold = ("vermeil", "gold_10k_yellow", "gold_14k_yellow", "gold_18k_yellow", "gold_10k_rose", "gold_14k_rose", "gold_18k_rose")


def _Terms(H) -> dict:
    """The archived Terms the site serves now: {"version", "sha256"}."""
    return H.Ctx.Accounts.Consent.Current()["terms"]


async def test_a_new_order_records_the_terms_version_its_customer_accepted(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        T1 = _Terms(H)
        Info = (await H.Client.get("/api/checkout")).json()
        assert Info["terms_version"] == f"v{T1['version']}"                       # what the checkout's box links to
        await _InBag(H)
        R = await H.Client.post("/api/orders", json=_Order())
        assert R.status_code == 200, R.text
        O = R.json()
        assert (O["terms_version"], O["terms_sha256"]) == (f"v{T1['version']}", T1["sha256"]) and O["terms_accepted_at"]
        Row = H.Ctx.Db.One("SELECT terms_version, terms_sha256 FROM orders WHERE id = ?", (O["id"],))
        assert (Row["terms_version"], Row["terms_sha256"]) == (f"v{T1['version']}", T1["sha256"])
        Doc = H.Ctx.Accounts.Db.One("SELECT version FROM legal_documents WHERE doc = 'terms' AND sha256 = ?", (Row["terms_sha256"],))
        assert Doc and Doc["version"] == T1["version"]                              # the archived text it points to
        Ad = (await H.Client.get(f"/api/admin/orders/{O['ref']}", headers=Admin)).json()
        assert (Ad["terms_version"], Ad["terms_sha256"]) == (f"v{T1['version']}", T1["sha256"])
        # An order placed before (the fixed label) keeps it: nothing is back-filled
        H.Ctx.Db.Execute("UPDATE orders SET terms_version = ?, terms_sha256 = NULL WHERE id = ?", (Orders.LegacyTermsLabel, O["id"]))
        # The Terms' wording changes (a release): the next order records the next version; the earlier one keeps its own
        ChangeTerms(H)
        T2 = _Terms(H)
        assert T2["version"] == T1["version"] + 1 and T2["sha256"] != T1["sha256"]
        await _InBag(H, "A plain signet")
        O2 = (await H.Client.post("/api/orders", json=_Order(client_request_id="click-2"))).json()
        assert (O2["terms_version"], O2["terms_sha256"]) == (f"v{T2['version']}", T2["sha256"])
        assert H.Ctx.Db.One("SELECT terms_version FROM orders WHERE id = ?", (O["id"],))["terms_version"] == "2026-10-02"
    finally:
        await H.Close()


async def test_an_order_from_an_approved_quote_records_the_terms_version_accepted_on_approval(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        _B, Q = await _Request(H)
        assert (await _Offer(H, Q)).status_code == 200
        Token = _Link(H).split("token=")[1]
        R = await H.Client.post("/quote/approve", data={"token": Token, **Address, "shipping_method": "standard", "terms": "1"})
        assert R.status_code == 200 and re.search(r"ORD-1000\d", R.text), R.text
        T = _Terms(H)
        Row = H.Ctx.Db.One("SELECT terms_version, terms_sha256 FROM orders")
        assert (Row["terms_version"], Row["terms_sha256"]) == (f"v{T['version']}", T["sha256"])
    finally:
        await H.Close()


async def test_no_material_status_or_product_switch_changes_the_terms_version(tmp_path):
    """Every status a metal can take for each product, and every combination of the product switches: the Terms version
    (number and hash) and the archived documents stay exactly as they were."""
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Before = H.Ctx.Accounts.Consent.Current()
        Docs = H.Ctx.Accounts.Db.All("SELECT doc, version, sha256 FROM legal_documents ORDER BY doc, version")

        async def Same(Why):
            assert H.Ctx.Accounts.Consent.Current() == Before, Why
            assert (await H.Client.get("/api/auth/terms")).json()["terms"] == Before["terms"], Why
            assert H.Ctx.Accounts.Db.All("SELECT doc, version, sha256 FROM legal_documents ORDER BY doc, version") == Docs, Why

        Done = 0
        for Status in (MaterialStatus.Preview, MaterialStatus.Quote, MaterialStatus.Hidden, MaterialStatus.Available):
            for Product in ("ring", "charm"):
                Changes = [{"product": Product, "material_id": M, "status": Status} for M in Gold]
                R = await H.Client.put("/api/admin/material-statuses", json={"changes": Changes, "note": "WP5 test"}, headers=Admin)
                if R.status_code == 200:                    # a rule may refuse one (e.g. Available without a price): no change
                    Done += 1
                await Same(f"{Product}: {Status}")
        assert Done >= 6                                    # preview, quote and hidden saved for rings and charms at least
        for Ring in (True, False):
            for Charm in (True, False):
                for P, On in (("ring", Ring), ("charm", Charm)):
                    R = await H.Client.put("/api/admin/products/availability", json={"product": P, "available": On}, headers=Admin)
                    assert R.status_code == 200, R.text
                (await H.Client.get("/terms")).raise_for_status()      # the page served in this state
                await Same(f"rings {'on' if Ring else 'off'}, charms {'on' if Charm else 'off'}")
    finally:
        await H.Close()


# ── the Terms text (branch wp5-terms: out of main until legal counsel approves the exact wording) ─────────────────────────
def test_the_terms_name_materials_by_their_availability_and_never_a_metal():
    """The Materials paragraph: once, static, the same words wherever the page is read (one copy in the page's template);
    it covers a material available to order, one offered by request (Quote Only) and one for preview only, and names no
    metal, so no status the Admin sets can rewrite it. The orders sentence no longer says gold is quoted, nor "per ring"
    when no product is on offer (L5)."""
    Html = (Web / "index.html").read_text(encoding="utf-8")
    Terms = SitePages.LegalText(Html, "terms")
    Para = Terms[Terms.index("Materials. Each material is shown with its availability."):]
    Para = Para[:Para.index("Orders already placed and quotes already issued are handled separately.") + 71]
    assert Terms.count("Materials. Each material is shown with its availability.") == 1 and Html.count("Each material is shown with its availability.") == 1
    for Words in ("available to order can be purchased at the price displayed for your design", "offered by request only",
                  "nothing is ordered or charged until you approve the quote", "offered for preview only",
                  "they cannot be purchased or quoted", "We may change which materials are available"):
        assert Words in Para, Words
    Labels = [M.Label for M in LoadCatalog().Materials.values()]
    for Name in Labels + ["gold", "silver", "steel", "vermeil", "karat", "14K", "18K"]:
        assert Name.lower() not in Para.lower(), Name
    assert "quoted individually" not in Terms and "Gold pieces" not in Terms and "Gold rings" not in Terms
    assert "[no product on offer] Prices are in US dollars and are fixed per piece and material." in Terms
    App = (Web / "app.js").read_text(encoding="utf-8")
    Getter = App[App.index("get termsOrders()"):App.index("get sizingHelp()")]
    assert "quoted individually" not in Getter and "r ? 'fixed per ring and material' : 'fixed per piece and material'" in Getter


async def test_the_new_terms_are_exactly_one_new_version_whatever_the_statuses_and_switches(tmp_path):
    """Served after the Terms of before (an archived version 1), the new wording is version 2 — once: every status and
    every switch combination keeps it version 2, and nothing else is archived."""
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        Db = H.Ctx.Accounts.Db
        Db.Execute("DELETE FROM legal_documents WHERE doc = 'terms'")                # the site as it is before the release:
        Db.Execute("INSERT INTO legal_documents (doc, version, sha256, text, first_served_at) VALUES ('terms', 1, ?, ?, ?)",
                   ("0" * 64, "The Terms of Service before WP5.", Now()))           # its Terms archived as version 1
        V = H.Ctx.Accounts.Consent.Current()["terms"]
        assert V["version"] == 2
        for Status in (MaterialStatus.Preview, MaterialStatus.Quote, MaterialStatus.Hidden):
            for Product in ("ring", "charm"):
                Changes = [{"product": Product, "material_id": M, "status": Status} for M in Gold]
                assert (await H.Client.put("/api/admin/material-statuses", json={"changes": Changes}, headers=Admin)).status_code == 200
                assert H.Ctx.Accounts.Consent.Current()["terms"] == V
        for P, On in (("ring", False), ("charm", True), ("ring", True), ("charm", False)):
            assert (await H.Client.put("/api/admin/products/availability", json={"product": P, "available": On}, headers=Admin)).status_code == 200
            assert H.Ctx.Accounts.Consent.Current()["terms"] == V
        assert [R["version"] for R in Db.All("SELECT version FROM legal_documents WHERE doc = 'terms' ORDER BY version")] == [1, 2]
    finally:
        await H.Close()
