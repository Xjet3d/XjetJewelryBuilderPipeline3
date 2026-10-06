"""Charms, phase 1 — the foundation: every design has a product (existing ones are rings), the product never
changes, charms have their own ID sequence, the bag and order tables accept a charm line without a ring
size, and a customer who cannot see charms never meets one. The ring behaviour of before is the reference:
the ring tests elsewhere stay as they are."""

import json
import sqlite3

import pytest

from p3 import ringids as RingIds
from p3.db import Database, NewId, Now
from p3.mail import OrderConfirmationEmail, QuoteRequestEmail
from p3.providers import endpoints
from tests.conftest import Harness, OfferCharmSizes

AdminKey = "charm-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}


@pytest.fixture
async def HC(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    await OfferCharmSizes(Obj, AdminKey)                                     # 15–30 mm, as these tests were written
    yield Obj
    await Obj.Close()


def CharmDesign(H, AssetPath: str, Title: str = "Moon Drop", Owner: str | None = None) -> tuple[str, list[str]]:
    """A charm design straight in the database (four ready options on an existing image file)."""
    Db, T = H.Ctx.Db, Now()
    Did, Bid = NewId("dsg"), NewId("bat")
    Cids = [NewId("cand") for _ in range(4)]
    with Db.Transaction() as Conn:
        Conn.execute("INSERT INTO designs (id, owner_account_id, title, prompt, selected_candidate_id, created_at, updated_at, ai_mode, "
                     "product_type) VALUES (?,?,?,?,?,?,?,?,?)",
                     (Did, Owner or H.Who.AccountId, Title, "A crescent moon charm", Cids[0], T, T, "mock", "charm"))
        Conn.execute("INSERT INTO batches (id, design_id, kind, user_text, effective_prompt, endpoint, desired_count, config_version, "
                     "created_at) VALUES (?,?,?,?,?,?,?,?,?)", (Bid, Did, "initial", "A crescent moon charm", "-", endpoints.ImageGenerate, 4,
                                                                "test", T))
        for Slot, Cid in enumerate(Cids):
            Conn.execute("INSERT INTO candidates (id, batch_id, slot, status, seed, asset_path, created_at, updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?)", (Cid, Bid, Slot, "ready", Slot + 1, AssetPath, T, T))
    return Did, Cids


async def test_every_existing_and_new_design_is_a_ring_with_its_usual_id(HC):
    H = HC
    B1 = await H.NewDesign("A slim twisted band")
    B2 = await H.NewDesign("A signet with a hexagon face", product="ring")
    for B in (B1, B2):
        D = await H.Design(B["design_id"])
        assert "product_type" not in D                      # charms hidden: the customer's answer is the ring-only one
    Rows = H.Ctx.Db.All("SELECT product_type, ring_no, charm_no FROM designs ORDER BY created_at")
    assert [(R["product_type"], R["ring_no"], R["charm_no"]) for R in Rows] == [("ring", 1001, None), ("ring", 1002, None)]
    L = (await H.Client.get("/api/admin/sessions?include_mock=true", headers=Admin)).json()["sessions"]
    assert {X["ring_id"] for X in L} == {"R-1001", "R-1002"} and {X["product_type"] for X in L} == {"ring"}


async def test_the_product_is_fixed_once_and_checked_by_the_database(HC):
    H = HC
    B = await H.NewDesign()
    with pytest.raises(sqlite3.DatabaseError):
        H.Ctx.Db.Execute("UPDATE designs SET product_type = 'charm' WHERE id = ?", (B["design_id"],))
    assert H.Ctx.Db.One("SELECT product_type FROM designs WHERE id = ?", (B["design_id"],))["product_type"] == "ring"
    with pytest.raises(sqlite3.DatabaseError):
        H.Ctx.Db.Execute("INSERT INTO designs (id, owner_account_id, title, prompt, created_at, updated_at, product_type) "
                         "VALUES ('dsg_x', 'acc', 'X', 'p', 'now', 'now', 'pendant')")
    # Other updates of the design are untouched by the rule
    H.Ctx.Db.Execute("UPDATE designs SET title = 'Renamed' WHERE id = ?", (B["design_id"],))


async def test_charms_have_their_own_id_sequence_and_rings_keep_theirs(HC):
    H = HC
    Ring1 = await H.NewDesign()
    Asset = H.Ctx.Db.One("SELECT asset_path FROM candidates WHERE status = 'ready' LIMIT 1")["asset_path"]
    C1, C1Cands = CharmDesign(H, Asset, "Moon Drop")
    Ring2 = await H.NewDesign("A second ring")
    C2, _ = CharmDesign(H, Asset, "Star Leaf")
    Ids = {R["id"]: R for R in H.Ctx.Db.All("SELECT * FROM designs")}
    assert RingIds.Ref(Ids[Ring1["design_id"]]) == "R-1001" and RingIds.Ref(Ids[Ring2["design_id"]]) == "R-1002"   # no gap
    assert RingIds.Ref(Ids[C1]) == "C-1001" and RingIds.Ref(Ids[C2]) == "C-1002"
    assert Ids[C1]["ring_no"] is None and Ids[Ring2["design_id"]]["charm_no"] is None
    Refs = RingIds.CandidateRefs(H.Ctx.Db, [C1, Ring1["design_id"]])
    assert Refs[C1Cands[1]] == "C-1001-B" and Refs[Ring1["candidates"][0]["id"]] == "R-1001-A"
    L = (await H.Client.get("/api/admin/sessions?include_mock=true", headers=Admin)).json()["sessions"]
    Charm = next(X for X in L if X["design_id"] == C1)
    assert Charm["ring_id"] == "C-1001" and Charm["product_type"] == "charm"
    # A retired charm number is never given out again — in the charm sequence only
    with H.Ctx.Db.Transaction() as Conn:
        RingIds.RetireDesign(Conn, Ids[C2], C1, Now())
    C3, _ = CharmDesign(H, Asset, "Sun Disc")
    assert H.Ctx.Db.One("SELECT charm_no FROM designs WHERE id = ?", (C3,))["charm_no"] == 1003
    assert (await H.NewDesign("A third ring"))["design_id"]
    assert H.Ctx.Db.One("SELECT MAX(ring_no) AS n FROM designs")["n"] == 1003


async def test_customers_cannot_create_a_charm_while_charms_are_hidden(HC):
    H = HC
    for Product in ("charm", "CHARM"):
        R = await H.Client.post("/api/designs", data={"prompt": "A crescent moon charm", "product": Product})
        assert R.status_code == 400 and R.json()["error"]["code"] == "unknown_product"     # as if charms did not exist
    assert (await H.Client.post("/api/designs", data={"prompt": "A charm", "product": "pendant"})).status_code == 400
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM designs")["n"] == 0


async def test_hidden_charms_never_reach_the_customer_site(HC):
    H = HC
    Ring = await H.NewDesign()
    await H.Client.put(f"/api/designs/{Ring['design_id']}/selection", json={"candidate_id": Ring["candidates"][0]["id"]})
    RingItem = (await H.Client.post("/api/admin/gallery", json={"design_id": Ring["design_id"]}, headers=Admin)).json()
    Asset = Ring["candidates"][0]["image_url"].split("/assets/", 1)[1]
    Cid, Cands = CharmDesign(H, Asset)
    CharmItem = (await H.Client.post("/api/admin/gallery", json={"design_id": Cid}, headers=Admin)).json()
    assert CharmItem["product_type"] == "charm" and CharmItem["design_ring_id"] == "C-1001"
    Customer = {"X-Access-Token": H.Ctx.Accounts.IssueToken("customer")[0]}
    # Charms hidden: the ring-only site of before — no charm tile, no product field on the tiles
    Tiles = (await H.Client.get("/api/gallery", headers=Customer)).json()["items"]
    assert [T["id"] for T in Tiles] == [RingItem["id"]] and set(Tiles[0]) == {"id", "design_id", "title", "image_url"}
    assert (await H.Client.post(f"/api/gallery/{CharmItem['id']}/start", json={}, headers=Customer)).status_code == 404
    assert (await H.Client.put(f"/api/favorites/{Cid}", headers=Customer)).status_code == 404
    assert (await H.Client.get(f"/api/gallery/{CharmItem['id']}/share", headers=Customer)).status_code == 404
    Slug = H.Ctx.Db.One("SELECT title FROM designs WHERE id = ?", (Cid,))["title"].lower().replace(" ", "-")
    assert (await H.Client.get(f"/design/{Slug}", headers=Customer)).status_code == 404
    assert Cid not in [D["id"] for D in (await H.Client.get("/api/designs")).json()["designs"]]      # the owner's own charm too
    assert (await H.Client.get(f"/api/designs/{Cid}")).status_code == 404
    # The Admin always sees both products
    assert {X["product_type"] for X in (await H.Client.get("/api/admin/gallery", headers=Admin)).json()["items"]} == {"ring", "charm"}
    # Charms switched on for customers: the charm tile appears and every tile says which product it is
    H.Ctx.Products.Set("charms_available", True, "test")
    Tiles = (await H.Client.get("/api/gallery", headers=Customer)).json()["items"]
    assert [T["product_type"] for T in Tiles] == ["ring", "charm"]
    assert (await H.Client.get(f"/api/gallery/{CharmItem['id']}/share", headers=Customer)).status_code == 200
    Page = (await H.Client.get(f"/design/{Slug}", headers=Customer)).text
    assert "a charm from the Inspiration Gallery" in Page
    assert Cid in [D["id"] for D in (await H.Client.get("/api/designs")).json()["designs"]]
    assert (await H.Design(Cid))["product_type"] == "charm"


async def test_a_signed_in_admin_previews_charms_while_customers_cannot_see_them(HC):
    H = HC
    Ring = await H.NewDesign()
    Asset = Ring["candidates"][0]["image_url"].split("/assets/", 1)[1]
    Cid, _ = CharmDesign(H, Asset)
    Item = (await H.Client.post("/api/admin/gallery", json={"design_id": Cid}, headers=Admin)).json()
    assert (await H.Client.get("/api/gallery")).json()["items"] == []
    assert (await H.Client.post("/api/admin/login", json={"key": AdminKey})).status_code == 200      # the browser's admin session
    Tiles = (await H.Client.get("/api/gallery")).json()["items"]
    assert [T["id"] for T in Tiles] == [Item["id"]] and Tiles[0]["product_type"] == "charm"
    assert H.Ctx.Products.CharmsAvailable is False                                                   # still hidden for customers


async def test_an_old_database_is_migrated_without_losing_a_row(tmp_path):
    """The bag and order tables of before required a ring size; they are rebuilt so a charm line can carry a charm
    size instead — every existing row, id and value is kept, and each one is a ring."""
    Path = tmp_path / "old.db"
    Database(Path)                                                    # today's schema …
    with sqlite3.connect(Path) as Conn:                               # … taken back to the shape before charms
        Conn.executescript("""
            DROP TRIGGER designs_product_valid; DROP TRIGGER designs_product_immutable;
            DROP TRIGGER designs_ring_no_assign; DROP TRIGGER designs_charm_no_assign; DROP INDEX designs_charm_no;
            ALTER TABLE designs DROP COLUMN charm_no; ALTER TABLE designs DROP COLUMN product_type;
            DROP TABLE retired_charms;
            ALTER TABLE customizations DROP COLUMN charm_size;
            ALTER TABLE quote_requests DROP COLUMN product_type; ALTER TABLE quote_requests DROP COLUMN charm_size;
            DROP TABLE bag_lines; DROP TABLE order_lines;
            CREATE TABLE bag_lines (id TEXT PRIMARY KEY, owner_account_id TEXT NOT NULL, design_id TEXT NOT NULL REFERENCES designs(id),
                candidate_id TEXT NOT NULL REFERENCES candidates(id), customization_id TEXT NOT NULL REFERENCES customizations(id),
                material_id TEXT NOT NULL, ring_size REAL NOT NULL, quantity INTEGER NOT NULL, unit_price REAL NOT NULL,
                currency TEXT NOT NULL, pricing_version TEXT NOT NULL, quote_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE order_lines (id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id), position INTEGER NOT NULL,
                design_id TEXT NOT NULL REFERENCES designs(id), candidate_id TEXT NOT NULL REFERENCES candidates(id), bag_line_id TEXT,
                customization_id TEXT, title TEXT NOT NULL, ring_id TEXT, material_id TEXT NOT NULL, material_label TEXT NOT NULL,
                ring_size REAL NOT NULL, quantity INTEGER NOT NULL, unit_price REAL NOT NULL, line_total REAL NOT NULL,
                currency TEXT NOT NULL, pricing_version TEXT NOT NULL, image_path TEXT);
            CREATE INDEX order_lines_order ON order_lines(order_id, position);
        """)
        Conn.execute("PRAGMA foreign_keys=OFF")
        Conn.execute("INSERT INTO designs (id, owner_account_id, title, prompt, created_at, updated_at, ring_no) VALUES "
                     "('dsg_a', 'acc_1', 'Fil Twist', 'p', '2026-10-01', '2026-10-01', 1001)")
        Conn.execute("INSERT INTO bag_lines VALUES ('bag_1', 'acc_1', 'dsg_a', 'cand_1', 'cus_1', 'silver', 7.5, 2, 200, 'USD', 'materials-v1', '{}', '2026-10-02')")
        Conn.execute("INSERT INTO orders (id, order_no, owner_account_id, status, payment_status, customer_json, shipping_json, "
                     "address_validation, shipping_method, currency, subtotal, discount, shipping, total, created_at, updated_at) VALUES "
                     "('ord_1', 10001, 'acc_1', 'new', 'pending', '{}', '{}', 'unverified', 'standard', 'USD', 400, 0, 0, 400, 'x', 'x')")
        Conn.execute("INSERT INTO order_lines VALUES ('oln_1', 'ord_1', 1, 'dsg_a', 'cand_1', 'bag_1', 'cus_1', 'Fil Twist', 'R-1001-A', "
                     "'silver', 'Sterling Silver', 7.5, 2, 200, 400, 'USD', 'materials-v1', NULL)")
    Db = Database(Path)                                               # the migration runs on start
    D = Db.One("SELECT * FROM designs WHERE id = 'dsg_a'")
    assert D["product_type"] == "ring" and D["ring_no"] == 1001 and D["charm_no"] is None
    B = Db.One("SELECT * FROM bag_lines WHERE id = 'bag_1'")
    assert (B["product_type"], B["ring_size"], B["charm_size"], B["quantity"], B["unit_price"]) == ("ring", 7.5, None, 2, 200)
    L = Db.One("SELECT * FROM order_lines WHERE id = 'oln_1'")
    assert (L["product_type"], L["ring_size"], L["ring_id"], L["line_total"], L["purchase_json"]) == ("ring", 7.5, "R-1001-A", 400, "{}")
    Cols = {R["name"]: R for R in Db.All("PRAGMA table_info(order_lines)")}
    assert Cols["ring_size"]["notnull"] == 0 and "charm_size" in Cols
    assert {R["name"] for R in Db.All("PRAGMA index_list(order_lines)")} >= {"order_lines_order", "order_lines_design"}
    # The rule the old NOT NULL gave rings now holds for both products
    with pytest.raises(sqlite3.DatabaseError):
        Db.Execute("INSERT INTO order_lines (id, order_id, position, design_id, candidate_id, title, product_type, material_id, "
                   "material_label, quantity, unit_price, line_total, currency, pricing_version) VALUES "
                   "('oln_2', 'ord_1', 2, 'dsg_a', 'cand_1', 'X', 'ring', 'silver', 'S', 1, 1, 1, 'USD', 'v')")
    Database(Path)                                                    # a second start changes nothing
    assert Db.One("SELECT COUNT(*) AS n FROM order_lines")["n"] == 1


async def test_a_ring_order_snapshots_the_product_and_reads_as_before(HC):
    from tests.test_orders import Address, Customer
    H = HC
    B = await H.NewDesign()
    C = await H.Proceed(B["design_id"], B["candidates"][0]["id"])
    await H.Idle()
    await H.Client.patch(f"/api/customizations/{C['id']}", json={"ring_size": 7, "material_id": "stainless_steel"})
    assert (await H.Client.post("/api/bag", json={"customization_id": C["id"]})).status_code == 200
    Bag = (await H.Client.get("/api/bag")).json()
    # While charms are hidden a ring customer's answers are exactly those of before: no product fields at all
    ProductKeys = {"product_type", "product_types", "charm_size", "size_label"}
    assert not ProductKeys & set(Bag["lines"][0]) and Bag["lines"][0]["ring_size"] == 7
    O = (await H.Client.post("/api/orders", json={"customer": Customer, "address": Address, "terms_accepted": True,
                                                  "client_request_id": "r1"})).json()
    assert not ProductKeys & set(O) and not ProductKeys & set(O["lines"][0])
    assert not ProductKeys & set((await H.Client.get("/api/orders")).json()["orders"][0])
    Row = H.Ctx.Db.One("SELECT * FROM order_lines")
    Snap = json.loads(Row["purchase_json"])
    assert Row["product_type"] == "ring" and Row["ring_size"] == 7 and Row["charm_size"] is None
    assert Snap["product"] == "ring" and Snap["size"] == {"value": 7.0, "system": "US", "label": "US 7"}
    Subject, Html = OrderConfirmationEmail(O)
    assert "Ring ID R-1001-A" in Html and "US 7" in Html and "Your ring is reserved" in Html
    # Admin: the order holds a ring; the product filter finds it under Rings only
    assert len((await H.Client.get("/api/admin/orders?product=ring", headers=Admin)).json()["orders"]) == 1
    assert (await H.Client.get("/api/admin/orders?product=charm", headers=Admin)).json()["orders"] == []
    assert len((await H.Client.get("/api/admin/orders", headers=Admin)).json()["orders"]) == 1
    Dash = (await H.Client.get("/api/admin/dashboard", headers=Admin)).json()
    assert set(Dash["by_product"]) == {"ring", "charm"} and Dash["orders"]["by_product"] == {"ring": 0, "charm": 0}   # mock excluded


def test_a_line_without_a_ring_size_never_breaks_an_email():
    Charm = {"title": "Moon Drop", "ring_id": "C-1003-A", "material_label": "Sterling Silver", "product_type": "charm",
             "charm_size": 20.0, "ring_size": None, "quantity": 2, "line_total": 1, "currency": "USD"}
    Ring = {**Charm, "title": "Fil Twist", "ring_id": "R-1042-B", "product_type": "ring", "charm_size": None, "ring_size": 7.0}
    Order = {"ref": "ORD-10001", "customer": {"first_name": "Noa", "email": "n@example.com"}, "lines": [Ring, Charm],
             "subtotal": 2, "discount": 0, "shipping": 0, "total": 2, "currency": "USD", "address_lines": ["1 Main St"],
             "payment_label": "Pending", "shipping_label": "Standard", "shipping_eta": "x"}
    _S, Html = OrderConfirmationEmail(Order)
    assert "Ring ID R-1042-B" in Html and "US 7" in Html and "Charm ID C-1003-A" in Html and "20 mm" in Html
    assert "Your pieces are reserved" in Html
    _S, Html = OrderConfirmationEmail({**Order, "lines": [Charm]})
    assert "Your charms are reserved" in Html
    _S, Html = QuoteRequestEmail({"ref": "Q-5001", "customer": {"first_name": "Noa"}, "title": "Moon Drop", "ring_id": "C-1003-A",
                                  "material_label": "Gold", "product_type": "charm", "charm_size": None, "ring_size": None,
                                  "quantity": 1, "message": ""})
    assert "Charm ID C-1003-A" in Html and "size to be confirmed" in Html


async def test_the_admin_order_page_handles_a_charm_line(HC):
    H = HC
    B = await H.NewDesign()
    Line = {"design_id": B["design_id"], "product_type": "charm", "charm_size": 20.0, "ring_size": None, "material_id": "silver"}
    Out = H.Svc.Orders._ThreeDForLine(Line)                        # no ring size: no crash, simply no matching 3D result
    assert Out["three_d_id"] is None and Out["three_d_match"] is False
