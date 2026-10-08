"""Gallery sync (p3/gallerysync.py): proto's approved Inspiration Gallery onto another site. Only XJet's own published
masters move — their designs (and the XJet design a variation came from), images, 360° movies and the thumbnails already
made of them, and only the rows the gallery and the homepage story need. Nothing of a customer's, nothing made in mock
mode; a second import changes nothing; a row the target made itself is never touched."""

import hashlib
import json

import pytest

from p3 import gallerysync as Sync
from p3 import ringids as RingIds
from tests.conftest import Harness, MakeLive

AdminKey = "sync-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Anon = {"X-Access-Token": "NOPE00"}


async def _DesignAs(H, Headers, Prompt):
    R = await H.Client.post("/api/designs", data={"prompt": Prompt}, headers=Headers)
    assert R.status_code == 200, R.text
    await H.Idle()
    return (await H.Client.get(f"/api/batches/{R.json()['id']}", headers=Headers)).json()


async def _Publish(H, DesignId, **Extra):
    R = await H.Client.post("/api/admin/gallery", json={"design_id": DesignId, **Extra}, headers=Admin)
    assert R.status_code == 200, R.text
    return R.json()


async def _Master(H, Prompt):
    """The whole story: four options, a refinement of option C, the 360° movie of the refined ring."""
    B = await H.NewDesign(Prompt)
    Did, Cands = B["design_id"], B["candidates"]
    R = await H.Client.post(f"/api/designs/{Did}/batches", json={"parent_candidate_id": Cands[2]["id"], "instruction": "change to lattice"})
    assert R.status_code == 200, R.text
    await H.Idle()
    Ref = (await H.Client.get(f"/api/batches/{R.json()['id']}")).json()
    await H.Proceed(Did, Ref["candidates"][1]["id"])
    await H.Idle()
    return Did, await _Publish(H, Did)


async def _Variation(H, Prompt):
    """XJet's variation of its own master: the refinement became a design of its own (source_design_id)."""
    B = await H.NewDesign(Prompt)
    Did, Cands = B["design_id"], B["candidates"]
    await H.Proceed(Did, Cands[0]["id"])                                   # a movie makes it a master …
    await H.Idle()
    R = await H.Client.post(f"/api/designs/{Did}/batches", json={"parent_candidate_id": Cands[1]["id"], "instruction": "wave lattice"})
    await H.Idle()
    Fork = (await H.Client.get(f"/api/batches/{R.json()['id']}")).json()   # … so its refinement is a design of its own
    await H.Proceed(Fork["design_id"], Fork["candidates"][0]["id"])
    await H.Idle()
    return Fork["design_id"], Did, await _Publish(H, Fork["design_id"])


async def _Proto(tmp_path):
    """A source site with two XJet masters (one a variation), a customer's design published with consent, a design made
    in mock mode and published, and a customer who made a master theirs and saved it as a favourite."""
    H = Harness(tmp_path / "proto", AdminKey=AdminKey)
    Master, Item = await _Master(H, "A slim band with a heart motif, delicate and light")
    Var, VarSource, VarItem = await _Variation(H, "A slim twisted band with a small leaf motif")
    CustTok, CustWho = H.Ctx.Accounts.IssueToken("customer", MaxGenerations=20)
    Cust = {"X-Access-Token": CustTok}
    Theirs = await _DesignAs(H, Cust, "A wide band with an engraved wave, my own")
    await _Publish(H, Theirs["design_id"], candidate_id=Theirs["candidates"][0]["id"], owner_kind="customer",
                   consent_note="Agreed by email on 7 Oct 2026")
    assert (await H.Client.post(f"/api/gallery/{Item['id']}/start", json={}, headers=Cust)).status_code == 200
    assert (await H.Client.put(f"/api/favorites/{Master}", headers=Cust)).status_code == 200
    MakeLive(H)                                     # all of the above as made with the real provider …
    Mock = await H.NewDesign("A placeholder band made in mock mode")      # … and this one in mock mode
    await H.Proceed(Mock["design_id"], Mock["candidates"][0]["id"])
    await H.Idle()
    await _Publish(H, Mock["design_id"])
    Tile = next(T for T in (await H.Client.get("/api/gallery")).json()["items"] if T["design_id"] == Master)
    assert (await H.Client.get(Tile["image_url"].replace("/assets/", "/thumb/", 1) + "?w=320")).status_code == 200   # a thumbnail exists
    return H, {"master": Master, "item": Item, "var": Var, "var_source": VarSource, "var_item": VarItem,
               "theirs": Theirs["design_id"], "mock": Mock["design_id"], "customer": CustWho.AccountId}


def _Rewrite(Bundle, Change):
    """Change rows.json and re-seal the manifest — an internally consistent bundle with something it may not carry."""
    Rows = json.loads((Bundle / "rows.json").read_text(encoding="utf-8"))
    Change(Rows)
    Data = json.dumps(Rows, sort_keys=True).encode("utf-8")
    (Bundle / "rows.json").write_bytes(Data)
    M = json.loads((Bundle / "manifest.json").read_text(encoding="utf-8"))
    M["rows_sha256"] = hashlib.sha256(Data).hexdigest()
    M["content_id"] = Sync._ContentId(M["rows_sha256"], M["files"])
    (Bundle / "manifest.json").write_text(json.dumps(M), encoding="utf-8")


async def test_only_xjets_published_masters_move_and_the_gallery_and_story_work_on_the_target(tmp_path):
    H, X = await _Proto(tmp_path)
    T = Harness(tmp_path / "atelier", AdminKey=AdminKey)
    try:
        Bundle = tmp_path / "bundle"
        M = Sync.Export(H.Settings.DataDir, Bundle, "proto", "abc1234")
        assert M["counts"]["gallery_items"] == 2 and M["counts"]["designs"] == 3          # two masters + the variation's source
        assert {S["why"] for S in M["skipped"]} == {"a customer's design", "made in mock mode"}
        Rows = json.loads((Bundle / "rows.json").read_text(encoding="utf-8"))
        assert set(Rows) == set(Sync.Order)
        for Table, Rs in Rows.items():
            assert all(set(R) <= set(Sync.Columns[Table]) for R in Rs)
        Text = (Bundle / "rows.json").read_text(encoding="utf-8") + (Bundle / "manifest.json").read_text(encoding="utf-8")
        for Never in (X["customer"], X["theirs"], X["mock"], "consent", "requested_by", "reference_upload_url", "mockreq_"):
            assert Never not in Text, Never
        assert {D["id"] for D in Rows["designs"]} == {X["master"], X["var"], X["var_source"]}
        Kinds = {F["kind"] for F in M["files"]}
        assert {"image", "movie", "derived"} <= Kinds                         # the thumbnail made on proto travels too
        assert Sync.Main(["check", str(Bundle)]) == 0

        # Dry run: the plan, nothing written
        Dry = Sync.Import(Bundle, T.Settings.DataDir)
        assert Dry["applied"] is False and Dry["insert"]["gallery_items"] == 2 and Dry["files_write"] == M["counts"]["files"]
        assert (await T.Client.get("/api/gallery", headers=Anon)).json()["items"] == []

        R = Sync.Import(Bundle, T.Settings.DataDir, Apply=True, Ref="c0ffee")
        assert R["applied"] and R["insert"]["designs"] == 3 and R["files_write"] == M["counts"]["files"]
        Want = [(I["id"], I["design_id"], I["title"]) for I in (await H.Client.get("/api/gallery", headers=Anon)).json()["items"]
                if I["design_id"] in (X["master"], X["var"])]
        Tiles = (await T.Client.get("/api/gallery", headers=Anon)).json()["items"]
        assert [(I["id"], I["design_id"], I["title"]) for I in Tiles] == Want
        for I in Tiles:
            assert (await T.Client.get(I["image_url"])).status_code == 200
        Thumb = next(F for F in M["files"] if F["kind"] == "derived" and "_w320." in F["path"])
        Copied = T.Settings.AssetsDir / Thumb["path"]
        Original = T.Settings.AssetsDir / Thumb["path"].removeprefix("_derived/").rsplit("_w320.", 1)[0]
        assert Copied.is_file() and Copied.stat().st_mtime >= Original.stat().st_mtime    # counts as current: not made again
        # The homepage story, the same as on proto — also the variation's, told from the design it came from
        for D in (X["master"], X["var"]):
            Slug = H.Ctx.Db.One("SELECT share_slug FROM designs WHERE id = ?", (D,))["share_slug"]
            Src = (await H.Client.get(f"/api/showcase?design={Slug}", headers=Anon)).json()["story"]
            Dst = (await T.Client.get(f"/api/showcase?design={Slug}", headers=Anon)).json()["story"]
            assert Dst == Src and Dst["slug"] == Slug and len(Dst["options"]) == 4
            for U in (Dst["movie_url"], Dst["still_url"], *Dst["options"]):
                assert (await T.Client.get(U)).status_code == 200
        Same = lambda Did: (RingIds.Ref(H.Ctx.Db.One("SELECT * FROM designs WHERE id = ?", (Did,))),
                            RingIds.Ref(T.Ctx.Db.One("SELECT * FROM designs WHERE id = ?", (Did,))))
        assert Same(X["master"])[0] == Same(X["master"])[1] and Same(X["var"])[0] == Same(X["var"])[1]   # free here: kept
        Health = (await T.Client.get("/api/health")).json()["gallery_sync"]
        assert Health["status"] == "ok" and Health["source"] == "proto" and Health["items"] == 2 and Health["bundle"] == "c0ffee"
        assert (await T.Client.get("/api/admin/health", headers=Admin)).json()["gallery_sync"]["status"] == "ok"
        # Nothing of a customer's arrived
        for Table in ("gallery_uses", "gallery_favorites", "customizations", "bag_lines", "orders", "quote_requests", "session_3d"):
            assert T.Ctx.Db.One(f"SELECT COUNT(*) AS n FROM {Table}")["n"] == 0, Table
        assert T.Ctx.Db.One("SELECT COUNT(*) AS n FROM designs WHERE owner_account_id = ?", (X["customer"],))["n"] == 0

        # Again: nothing changes, nothing is duplicated
        Before = [T.Ctx.Db.One(f"SELECT COUNT(*) AS n FROM {Tab}")["n"] for Tab in Sync.Order]
        Again = Sync.Import(Bundle, T.Settings.DataDir, Apply=True)
        assert sum(Again["insert"].values()) == sum(Again["update"].values()) == Again["files_write"] == 0 and not Again["unpublish"]
        assert [T.Ctx.Db.One(f"SELECT COUNT(*) AS n FROM {Tab}")["n"] for Tab in Sync.Order] == Before

        # The target's own tile stays, after the synced ones; a change on proto arrives; a tile proto took off goes
        Own = await T.NewDesign("Atelier's own plain band")
        await T.Proceed(Own["design_id"], Own["candidates"][0]["id"])
        await T.Idle()
        OwnItem = await _Publish(T, Own["design_id"])
        H.Ctx.Db.Execute("UPDATE designs SET title = 'Heart Lattice' WHERE id = ?", (X["master"],))
        assert (await H.Client.delete(f"/api/admin/gallery/{X['var_item']['id']}", headers=Admin)).status_code == 200
        Sync.Export(H.Settings.DataDir, tmp_path / "bundle2", "proto")
        R = Sync.Import(tmp_path / "bundle2", T.Settings.DataDir, Apply=True)
        assert R["update"]["designs"] == 1 and R["unpublish"] == [X["var_item"]["id"]] and R["insert"]["designs"] == 0
        Tiles = (await T.Client.get("/api/gallery", headers=Anon)).json()["items"]
        assert [I["id"] for I in Tiles] == [X["item"]["id"], OwnItem["id"]] and Tiles[0]["title"] == "Heart Lattice"
        assert T.Ctx.Db.One("SELECT id FROM designs WHERE id = ?", (X["var"],))       # the design stays: customers may use it
    finally:
        await H.Close()
        await T.Close()


async def test_a_row_the_target_made_itself_stops_the_import_and_nothing_is_written(tmp_path):
    H, X = await _Proto(tmp_path)
    T = Harness(tmp_path / "other", AdminKey=AdminKey)
    try:
        Bundle = tmp_path / "bundle"
        Sync.Export(H.Settings.DataDir, Bundle, "proto")
        T.Ctx.Db.Execute("INSERT INTO designs (id, owner_account_id, title, prompt, created_at, updated_at, product_type) "
                         "VALUES (?,?,?,?,?,?,?)", (X["master"], "p3local:someone", "Theirs", "p", "x", "x", "ring"))
        with pytest.raises(Sync.SyncError, match="did not come from proto"):
            Sync.Import(Bundle, T.Settings.DataDir, Apply=True)
        assert T.Ctx.Db.One("SELECT title FROM designs WHERE id = ?", (X["master"],))["title"] == "Theirs"
        assert T.Ctx.Db.One("SELECT COUNT(*) AS n FROM gallery_items")["n"] == 0
        assert not (T.Settings.AssetsDir / "designs" / X["var"]).exists()          # no file written either
        assert Sync.Main(["import", str(Bundle), "--data-dir", str(T.Settings.DataDir)]) == 2
    finally:
        await H.Close()
        await T.Close()


async def test_ring_ids_and_link_names_in_use_on_the_target_are_not_taken(tmp_path):
    H, X = await _Proto(tmp_path)
    T = Harness(tmp_path / "atelier", AdminKey=AdminKey)
    try:
        Bundle = tmp_path / "bundle"
        Sync.Export(H.Settings.DataDir, Bundle, "proto")
        Mine = await T.NewDesign("Atelier's first band")                    # R-1001 here: proto's first master is R-1001 too
        Slug = H.Ctx.Db.One("SELECT share_slug FROM designs WHERE id = ?", (X["master"],))["share_slug"]
        T.Ctx.Db.Execute("UPDATE designs SET share_slug = ? WHERE id = ?", (Slug, Mine["design_id"]))
        R = Sync.Import(Bundle, T.Settings.DataDir, Apply=True)
        Item = next(I for I in R["items"] if I["design_id"] == X["master"])
        assert Item["ring_id"] == "R-1001" and Item["ring_id_here"] not in (None, "R-1001")
        assert Item["slug_here"] == f"{Slug}-2"
        assert RingIds.Ref(T.Ctx.Db.One("SELECT * FROM designs WHERE id = ?", (Mine["design_id"],))) == "R-1001"
        # … and a later import keeps what was given here
        R = Sync.Import(Bundle, T.Settings.DataDir, Apply=True)
        assert next(I for I in R["items"] if I["design_id"] == X["master"])["ring_id_here"] == Item["ring_id_here"]
    finally:
        await H.Close()
        await T.Close()


async def test_a_tampered_or_unsafe_bundle_is_refused(tmp_path):
    H, X = await _Proto(tmp_path)
    try:
        Bundle = tmp_path / "bundle"
        M = Sync.Export(H.Settings.DataDir, Bundle, "proto")
        with pytest.raises(Sync.SyncError, match="not empty"):
            Sync.Export(H.Settings.DataDir, Bundle, "proto")
        Sync.Load(Bundle)
        Blob = Bundle / "files" / M["files"][0]["sha256"]
        Good = Blob.read_bytes()
        Blob.write_bytes(Good[:-1] + bytes([Good[-1] ^ 1]))                  # one bit changed on the way
        with pytest.raises(Sync.SyncError, match="does not match"):
            Sync.Load(Bundle)
        Blob.write_bytes(Good)
        Manifest = json.loads((Bundle / "manifest.json").read_text(encoding="utf-8"))
        for Bad in ("../../etc/passwd", "designs/../../x.png", "/etc/hosts", "_derived/../x", "other/dsg_x/a.png"):
            Copy = json.loads(json.dumps(Manifest))
            Copy["files"][0]["path"] = Bad
            (Bundle / "manifest.json").write_text(json.dumps(Copy), encoding="utf-8")
            with pytest.raises(Sync.SyncError, match="Unsafe"):
                Sync.Load(Bundle)
        (Bundle / "manifest.json").write_text(json.dumps(Manifest), encoding="utf-8")
        _Rewrite(Bundle, lambda Rows: Rows.update({"gallery_uses": []}))
        with pytest.raises(Sync.SyncError, match="Unexpected tables"):
            Sync.Load(Bundle)
        _Rewrite(Bundle, lambda Rows: (Rows.pop("gallery_uses"), Rows["designs"][0].update({"email": "someone@example.com"})))
        with pytest.raises(Sync.SyncError, match="allow-list"):
            Sync.Load(Bundle)
        _Rewrite(Bundle, lambda Rows: (Rows["designs"][0].pop("email"), Rows["designs"][0].update({"ai_mode": "mock"})))
        with pytest.raises(Sync.SyncError, match="mock"):
            Sync.Load(Bundle)
        _Rewrite(Bundle, lambda Rows: (Rows["designs"][0].update({"ai_mode": "fal"}), Rows["movies"][0].update({"status": "queued"})))
        with pytest.raises(Sync.SyncError, match="not ready"):                  # work in flight never moves
            Sync.Load(Bundle)
    finally:
        await H.Close()
