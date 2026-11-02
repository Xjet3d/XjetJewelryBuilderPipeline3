"""WP5 — the Terms a customer accepts. A new order records the archived Terms version its customer accepted (M6): the
version its "I accept the Terms of Service" links to when the order is placed, by number and SHA-256, the same
identifiers a consent record keeps — never the fixed label "2026-10-02", which older orders keep (nothing is back-filled).
And no material status or product switch can change the Terms version: the text is static, the sentences that follow
the products on offer are archived in every form."""

import re

from p3 import materialstatus as MaterialStatus
from p3 import orders as Orders
from tests.conftest import Harness
from tests.test_consent import ChangeTerms
from tests.test_orders import Address, Customer, _InBag, _Order
from tests.test_quotes import Admin, AdminKey, _Link, _Offer, _Request

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
