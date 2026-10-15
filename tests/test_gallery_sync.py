"""Gallery sync (p3/gallerysync.py): proto's approved Inspiration Gallery onto another site. Only XJet's own published
masters move — their designs (and the XJet design a variation came from), images, 360° movies and the thumbnails already
made of them, and only the rows the gallery and the homepage story need. Nothing of a customer's, nothing made in mock
mode; a second import changes nothing; a row the target made itself is never touched."""

import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest

from p3 import gallerysync as Sync
from p3 import ringids as RingIds
from p3.providers.mock import MockProvider
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


async def test_a_bundle_pushed_to_the_sites_admin_api_lands_once_and_only_with_its_admin_key(tmp_path):
    """The bundle goes to the site's own Admin API (atelier's through an SSH tunnel: its Admin is not public), in pieces,
    and only the files it does not have yet are sent."""
    H, X = await _Proto(tmp_path)
    T = Harness(tmp_path / "atelier", AdminKey=AdminKey)
    U = Harness(tmp_path / "third", AdminKey=AdminKey)
    try:
        Bundle = tmp_path / "bundle"
        M = Sync.Export(H.Settings.DataDir, Bundle, "proto")
        with pytest.raises(Sync.SyncError, match=r"HTTP 40[13]"):
            await Sync.Push(Bundle, "", "not-the-key", Client=T.Client, Out=io.StringIO())
        assert (await T.Client.get("/api/gallery", headers=Anon)).json()["items"] == []
        Out = io.StringIO()
        Pasted = "\x1b[200~" + AdminKey + "\x1b[201~\r\n"     # pasted at a terminal: bracketed-paste markers, Enter
        R = await Sync.Push(Bundle, "", Pasted, Client=T.Client, Out=Out, ChunkBytes=64 << 10)     # several pieces a file
        assert R["applied"] and R["insert"]["gallery_items"] == 2 and R["snapshot"].startswith("pipeline3-")
        Tiles = (await T.Client.get("/api/gallery", headers=Anon)).json()["items"]
        assert {I["design_id"] for I in Tiles} == {X["master"], X["var"]}
        Sync_ = T.Settings.DataDir / "gallery-sync"
        assert list((Sync_ / "uploads").iterdir()) == [] and len(list((Sync_ / "backups").glob("pipeline3-*.db"))) == 1
        assert (await T.Client.get("/api/health")).json()["gallery_sync"]["status"] == "ok"
        # Again: the site has every file — nothing is sent and nothing changes
        Out = io.StringIO()
        R = await Sync.Push(Bundle, "", AdminKey, Client=T.Client, Out=Out)
        assert re.search(r"Upload up_[0-9a-f]{32}: 0 of \d+ files to send", Out.getvalue()), Out.getvalue()
        assert sum(R["insert"].values()) == sum(R["update"].values()) == R["files_write"] == 0

        # The protocol refuses what does not fit: a customer, a piece out of order, a wrong file, a file not in the bundle
        Api = "/api/admin/gallery-sync/uploads"
        assert (await U.Client.post(Api, json=M)).status_code in (401, 403)
        Bad = json.loads(json.dumps(M))
        Bad["files"][0]["path"] = "../../etc/passwd"
        assert (await U.Client.post(Api, json=Bad, headers=Admin)).status_code == 400
        S = (await U.Client.post(Api, json=M, headers=Admin)).json()
        Sha = max(M["files"], key=lambda F: F["bytes"])["sha256"]
        assert Sha in S["need"] and len(S["need"]) == len({F["sha256"] for F in M["files"]})        # a new site needs them all
        Data = (Bundle / "files" / Sha).read_bytes()
        assert (await U.Client.put(f"{Api}/{S['upload_id']}/files/{Sha}", params={"offset": 5}, content=Data[:10],
                                   headers=Admin)).status_code == 409
        Wrong = bytes([Data[0] ^ 1]) + Data[1:]
        R = await U.Client.put(f"{Api}/{S['upload_id']}/files/{Sha}", params={"offset": 0}, content=Wrong, headers=Admin)
        assert R.status_code == 400 and R.json()["error"]["code"] == "file_mismatch"
        assert (await U.Client.put(f"{Api}/{S['upload_id']}/files/{'0' * 64}", content=b"x", headers=Admin)).status_code == 404
        R = await U.Client.post(f"{Api}/{S['upload_id']}/plan", headers=Admin)            # no rows yet, files missing
        assert R.status_code == 409 and R.json()["error"]["code"] == "gallery_sync_refused"
        assert (await U.Client.delete(f"{Api}/{S['upload_id']}", headers=Admin)).status_code == 200
        assert (await U.Client.post(f"{Api}/{S['upload_id']}/apply", headers=Admin)).status_code == 404
        assert (await U.Client.get("/api/gallery", headers=Anon)).json()["items"] == []
    finally:
        await H.Close()
        await T.Close()
        await U.Close()


def test_a_pasted_key_loses_its_terminal_characters_and_an_impossible_one_is_refused_before_sending():
    """A key pasted at a terminal arrives wrapped in bracketed-paste markers; sent as is, Cloudflare answered atelier's
    first push with an empty HTTP 400 (2026-10-08). The markers, control characters and spaces around it go; a key that
    still cannot be a header value is refused before anything is sent."""
    assert Sync.CleanKey("\x1b[200~Abc123-def_456\x1b[201~\r\n") == "Abc123-def_456"
    assert Sync.CleanKey("  \x16Abc123def456\x1b ") == "Abc123def456"
    for Bad in ("", "\x1b[200~\x1b[201~", "two words here", "café-key-123"):
        with pytest.raises(Sync.SyncError):
            Sync.CleanKey(Bad)


# nginx's own 404 page: what atelier's public domain answers for the Admin since 2026-10-14
NginxPage = ("<html>\r\n<head><title>404 Not Found</title></head>\r\n<body>\r\n<center><h1>404 Not Found</h1></center>\r\n"
             "<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n")


async def test_a_site_whose_admin_takes_no_key_over_the_web_stops_the_push_at_once_and_says_to_push_over_ssh(tmp_path):
    """atelier's Admin is not public: on its public domain the proxy answers its own 404 page for the Admin API, and once
    administrator sign-in is enforced the app itself refuses the key (403). The push stops at the first request and says
    to push over SSH; any other answer of the app keeps its own message."""
    H = Harness(tmp_path / "proto", AdminKey=AdminKey)
    T = Harness(tmp_path / "atelier", AdminKey=AdminKey, AdminHost="admin.atelier.example")   # its Admin answers there only
    Strict = Harness(tmp_path / "enforced", AdminKey=AdminKey, AdminIdentityEnforced=True)
    try:
        Bundle = tmp_path / "bundle"
        Sync.Export(H.Settings.DataDir, Bundle, "proto")                   # an empty gallery: a valid bundle all the same
        Seen = []

        def Proxy(Req):
            Seen.append((Req.method, Req.url.path))
            return httpx.Response(404, headers={"Content-Type": "text/html"}, text=NginxPage)

        async with httpx.AsyncClient(transport=httpx.MockTransport(Proxy)) as Client:
            with pytest.raises(Sync.SyncError) as E:
                await Sync.Push(Bundle, "https://atelier.example/JewelryB2C3", AdminKey, Client=Client, Out=io.StringIO())
        assert str(E.value).startswith("start: HTTP 404: the site's Admin is not public"), E.value
        assert "P3_TARGET_SSH=<ssh host> bash scripts/gallery-sync.sh push" in str(E.value) and "<html>" not in str(E.value)
        assert Seen == [("POST", "/JewelryB2C3/api/admin/gallery-sync/uploads")]          # nothing else sent, no retry
        with pytest.raises(Sync.SyncError) as E:                             # the key refused: administrator sign-in only
            await Sync.Push(Bundle, "", AdminKey, Client=Strict.Client, Out=io.StringIO())
        assert str(E.value).startswith("start: HTTP 403: the site's Admin takes no key over the web"), E.value
        assert "P3_TARGET_SSH=<ssh host> bash scripts/gallery-sync.sh push" in str(E.value)
        with pytest.raises(Sync.SyncError) as E:                             # the app's own 404 (JSON): its message
            await Sync.Push(Bundle, "", AdminKey, Client=T.Client, Out=io.StringIO())
        assert str(E.value) == "start: HTTP 404: Not found."
    finally:
        await H.Close()
        await T.Close()
        await Strict.Close()


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


# ── scripts/gallery-sync.sh push over SSH (P3_TARGET_SSH): a stand-in ssh and server, local apps, no host reached ──
RepoRoot = Path(__file__).resolve().parent.parent

StubSsh = """#!/usr/bin/env bash
# ssh for the tests of scripts/gallery-sync.sh, never reaching a host: "ssh atelier COMMAND" runs COMMAND here, in a
# stand-in for the site's server (its home, its own settings); another host, or STUB_SSH_FAIL, fails as ssh does.
echo "$*" >> "$STUB_SSH_LOG"
if [[ -n "${STUB_SSH_FAIL:-}" ]]; then echo "$STUB_SSH_FAIL" >&2; exit 255; fi
[[ "${1:-}" == atelier && $# -ge 2 ]] || { echo "ssh: Could not resolve hostname ${1:-}" >&2; exit 255; }
shift
cd "$STUB_SSH_HOME" || exit 255
export HOME="$PWD" TMPDIR="$PWD/tmp" P3_DATA_DIR="$STUB_SSH_DATA_DIR" P3_LOCAL_URL="$STUB_SSH_LOCAL_URL"
exec bash -c "$*"
"""


def _OnServer(Dir: str = "", Checkout: str = "~/'git/XjetJewelryBuilderPipeline3'") -> list[str]:
    """What the script runs on the site's server, as the stand-in ssh records it (Dir: the temporary directory there)."""
    return [f"atelier cd {Checkout} && test -f scripts/gallery-sync.sh && " + 'mktemp -d "${TMPDIR:-/tmp}/p3-gallery-import.XXXXXX"',
            f"atelier tar -C '{Dir}' -xf - && cd {Checkout} && " + 'P3_BACKUP_DIR="${P3_BACKUP_DIR:-$HOME/p3-backups}" '
            f"bash scripts/gallery-sync.sh import '{Dir}'; Rc=$?; rm -rf '{Dir}'; exit $Rc"]   # two connections: two prompts at most


def _Bash() -> str | None:
    """bash for the script's tests: Git's own on Windows (never WSL's launcher), the system's elsewhere."""
    if os.name != "nt":
        return shutil.which("bash")
    Git = shutil.which("git")
    Roots = list(Path(Git).resolve().parents)[1:3] if Git else []          # <Git>\cmd\git.exe, <Git>\mingw64\bin\git.exe
    Roots.append(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git")
    return next((str(R / "bin" / "bash.exe") for R in Roots if (R / "bin" / "bash.exe").is_file()), None)


@pytest.fixture
def Bash():
    Found = _Bash()
    if not Found:
        pytest.skip("no bash here (on Windows: Git Bash)")
    return Found


def _Server(Tmp: Path, DataDir: Path, LocalUrl: str) -> dict:
    """The stand-in for the site's server: a home with the site's checkout in ~/git/XjetJewelryBuilderPipeline3 (this
    repository's script behind a wrapper), a temporary directory, and the site's own data directory and address."""
    Scripts = Tmp / "home" / "git" / "XjetJewelryBuilderPipeline3" / "scripts"
    Scripts.mkdir(parents=True)
    (Tmp / "home" / "tmp").mkdir()
    (Scripts / "gallery-sync.sh").write_bytes(
        f"#!/usr/bin/env bash\nexec bash '{(RepoRoot / 'scripts' / 'gallery-sync.sh').as_posix()}' \"$@\"\n".encode())
    return {"STUB_SSH_HOME": (Tmp / "home").as_posix(), "STUB_SSH_DATA_DIR": DataDir.as_posix(), "STUB_SSH_LOCAL_URL": LocalUrl}


def _Script(Bash: str, Tmp: Path, Env: dict, *Args: str) -> subprocess.CompletedProcess:
    """bash scripts/gallery-sync.sh ARGS with the stand-in ssh first on PATH (checked before the script starts) and nothing
    of this machine's own P3 or fal settings in the environment."""
    Bin = Tmp / "bin"
    Bin.mkdir(exist_ok=True)
    (Bin / "ssh").write_bytes(StubSsh.encode())
    (Bin / "ssh").chmod(0o755)
    Full = {K: V for K, V in os.environ.items() if not K.startswith(("P3_", "FAL_", "STUB_SSH_"))}
    Full.update({"P3_PYTHON": Path(sys.executable).as_posix(), "PYTHONIOENCODING": "utf-8",
                 "STUB_SSH_LOG": (Tmp / "ssh.log").as_posix(), **Env})
    Guard = ('Stub="$(cd "$1" && pwd)"; PATH="$Stub:$PATH"; [[ "$(command -v ssh)" == "$Stub/ssh" ]] || exit 99; '
             'shift; exec "$BASH" scripts/gallery-sync.sh "$@"')
    return subprocess.run([Bash, "-c", Guard, "gallery-sync", Bin.as_posix(), *Args], cwd=RepoRoot, env=Full,
                          capture_output=True, encoding="utf-8", errors="replace", timeout=600)


class _Served:
    """An ASGI app on 127.0.0.1 (a free port) in a thread of its own, the way the service runs on the site's server."""

    def __init__(self, App):
        import uvicorn
        Sock = socket.create_server(("127.0.0.1", 0))
        self.Port = Sock.getsockname()[1]
        self.Server = uvicorn.Server(uvicorn.Config(App, lifespan="off", log_config=None, log_level="warning", access_log=False))
        self.Thread = threading.Thread(target=self.Server.run, kwargs={"sockets": [Sock]}, daemon=True)
        self.Thread.start()
        Until = time.monotonic() + 20
        while not self.Server.started and time.monotonic() < Until:
            time.sleep(0.05)
        assert self.Server.started, "the local test site did not start"

    def Close(self):
        self.Server.should_exit = True
        self.Thread.join(10)


def _Recorded(App, Seen: list, Public: bool = False):
    """The app, with every path asked for recorded in Seen. Public: atelier's public domain since 2026-10-14 — nginx's own
    404 page for the Admin and the developer tools, everything else passed on to the app."""
    Hidden = re.compile(r"^/JewelryB2C3/(admin|api/admin/|api/dev/|static/admin\.js|dev)")

    async def Asgi(Scope, Receive, Send):
        if Scope["type"] == "http":
            Seen.append(Scope["path"])
            if Public and Hidden.match(Scope["path"]):
                await Send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"text/html")]})
                await Send({"type": "http.response.body", "body": NginxPage.encode()})
                return
        await App(Scope, Receive, Send)
    return Asgi


async def test_a_push_over_ssh_imports_on_the_sites_server_and_makes_no_admin_request_at_all(tmp_path, monkeypatch, Bash):
    """P3_TARGET_SSH=atelier bash scripts/gallery-sync.sh push PUBLIC_URL: the export and its check, the bundle copied into
    a temporary directory on the site's server, the server's own `import` there (plan, a backup in ~/p3-backups, import,
    a check of the running site), then the public pages. The site enforces administrator sign-in and its public domain
    hides the Admin: neither matters, no Admin request is made anywhere. The running site shows the import made by
    another process at once (it keeps no gallery in memory), and the temporary directory is gone at the end."""
    H = Harness(tmp_path / "proto", AdminKey=AdminKey, Provider=MockProvider(LatencyS=0.0, RenderVideo=True))
    Short = Path(tempfile.mkdtemp(prefix="p3-"))        # the story clip's path stays under Windows' 260 characters
    T = Harness(Short, AdminKey=AdminKey, BasePath="/JewelryB2C3", AdminIdentityEnforced=True)
    Seen: list[str] = []
    Sites = []
    try:
        monkeypatch.setattr(shutil, "which", lambda Name: None)          # no system ffmpeg: the bundled clip, a real MP4
        Master, _ = await _Master(H, "A slim band with a heart motif, delicate and light")
        monkeypatch.undo()
        MakeLive(H)
        Sites = [_Served(_Recorded(T.App, Seen)), _Served(_Recorded(T.App, Seen, Public=True))]
        Local, Public = (f"http://127.0.0.1:{S.Port}/JewelryB2C3" for S in Sites)
        Run = _Script(Bash, tmp_path, {"P3_TARGET_SSH": "atelier", "P3_GALLERY_SOURCE_HOST": "local",
                                       "P3_GALLERY_SOURCE_DATA_DIR": str(H.Settings.DataDir),
                                       **_Server(tmp_path, T.Settings.DataDir, Local)}, "push", Public)
        Out = Run.stdout + Run.stderr
        assert Run.returncode == 0, Out
        Ssh = (tmp_path / "ssh.log").read_text(encoding="utf-8").splitlines()
        assert Ssh == _OnServer(re.match(r"atelier tar -C '([^']+)' -xf - && ", Ssh[1])[1]), Ssh
        assert not list((tmp_path / "home" / "tmp").iterdir())             # the temporary directory is gone
        assert len(list((tmp_path / "home" / "p3-backups").glob("*/pipeline3.db"))) == 1    # backed up before the import
        assert "Imported." in Out and f"Verifying {Local}" in Out and f"Verifying {Public}" in Out, Out
        assert Out.count("All checks passed.") == 2, Out                   # the server's own check, then the public pages
        assert [R["design_id"] for R in T.Ctx.Db.All("SELECT design_id FROM gallery_items")] == [Master]
        assert Seen and not [P for P in Seen if "/admin" in P or "/dev" in P], Seen
    finally:
        for S in Sites:
            S.Close()
        await H.Close()
        await T.Close()
        shutil.rmtree(Short, ignore_errors=True)


def test_without_ssh_to_the_sites_server_the_push_stops_before_anything_is_exported(tmp_path, Bash):
    """A refused key, a host alias not set up yet, or no checkout where it is expected: the script stops at once with
    ssh's own words and a clear message, before anything is exported or sent."""
    Run = _Script(Bash, tmp_path, {"P3_TARGET_SSH": "atelier", "STUB_SSH_FAIL": "atelier: Permission denied (publickey).",
                                   "P3_GALLERY_SOURCE_HOST": "local", "P3_GALLERY_SOURCE_DATA_DIR": str(tmp_path / "none")},
                  "push", "https://atelier.example/JewelryB2C3")
    assert Run.returncode == 1, Run.stdout + Run.stderr
    assert "atelier: Permission denied (publickey)." in Run.stderr
    assert "no way to the site over SSH: check that ssh atelier works" in Run.stderr and "export" not in Run.stdout
    assert (tmp_path / "ssh.log").read_text(encoding="utf-8").splitlines() == _OnServer()[:1]
    Other = tmp_path / "other"
    Other.mkdir()
    Run = _Script(Bash, Other, {"P3_TARGET_SSH": "atelier", "P3_TARGET_CHECKOUT": "/srv/no-such-checkout",
                                "P3_GALLERY_SOURCE_HOST": "local", **_Server(Other, Other / "var", "http://127.0.0.1:9")},
                  "push")
    assert Run.returncode == 1 and "/srv/no-such-checkout is the site's checkout there" in Run.stderr, Run.stderr
    assert (Other / "ssh.log").read_text(encoding="utf-8").splitlines() == _OnServer(Checkout="'/srv/no-such-checkout'")[:1]


def test_the_scripts_help_is_its_whole_header(tmp_path, Bash):
    Run = _Script(Bash, tmp_path, {})
    Lines = (RepoRoot / "scripts" / "gallery-sync.sh").read_text(encoding="utf-8").splitlines()
    assert Run.returncode == 2 and Run.stdout.splitlines() == Lines[1:Lines.index("set -euo pipefail")]
