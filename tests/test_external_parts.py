"""External parts and 3D versions (docs/EXTERNAL-PARTS.md): Session → Design → 3D version.

An STL made outside Atelier arrives in pieces straight to disk, becomes a numbered 3D version of a design (or a new design
in a session), is measured once like a Hi3D model, and is used exactly as uploaded or as a scalable master. Order lines
are pinned to the version they are produced from; nothing is ever deleted."""

import hashlib
import io
import json

import numpy as np
import pytest

from p3 import uploads as UploadsModule
from p3.db import Database
from p3.geometry import MeasureRaw, UsSizeToInnerDiameterMm, WritePreview
from tests.conftest import Harness
from tests.test_orders import Address, Customer

AdminKey = "external-parts-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Phrase = "GENERATE NEW 3D"


@pytest.fixture
async def HE(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    assert (await Obj.Client.post("/api/admin/login", json={"key": AdminKey})).status_code == 200
    yield Obj
    await Obj.Close()


def Torus(Major: float, Minor: float = 1.5, Ascii: bool = False) -> bytes:
    """A ring band as an STL (millimetres): its inner diameter is 2 × (Major − Minor)."""
    import trimesh
    M = trimesh.creation.torus(major_radius=Major, minor_radius=Minor, major_sections=256, minor_sections=32)
    Data = M.export(file_type="stl_ascii" if Ascii else "stl")
    return Data.encode("ascii") if isinstance(Data, str) else Data


def CharmStl() -> bytes:
    from p3.providers.mock import _CharmMeshBytes
    return _CharmMeshBytes("stl")


Us7 = UsSizeToInnerDiameterMm(7) / 2 + 1.5        # a torus whose inner diameter is exactly US 7 (17.32 mm)


async def Upload(H, Data: bytes, Name: str = "part.stl", Piece: int | None = None) -> dict:
    R = await H.Client.post("/api/admin/uploads", json={"filename": Name, "bytes": len(Data)}, headers=Admin)
    assert R.status_code == 200, R.text
    U = R.json()
    Step = Piece or U["chunk_bytes"]
    for Off in range(0, len(Data), Step):
        R = await H.Client.put(f"/api/admin/uploads/{U['id']}?offset={Off}", content=Data[Off:Off + Step],
                               headers={**Admin, "Content-Type": "application/octet-stream"})
        assert R.status_code == 200, R.text
    R = await H.Client.post(f"/api/admin/uploads/{U['id']}/complete", headers=Admin)
    assert R.status_code == 200, R.text
    return R.json()


async def Session(H, Key):
    R = await H.Client.get(f"/api/admin/sessions/{Key}", headers=Admin)
    assert R.status_code == 200, R.text
    return R.json()


async def ModelledRing(H, Prompt="A slim twisted band"):
    """A ring design with its Hi3D model (mock) — version 1."""
    B = await H.NewDesign(Prompt)
    Did = B["design_id"]
    R = await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={}, headers=Admin)
    assert R.status_code == 200, R.text
    await H.Idle()
    return B, Did


async def Ordered(H, Prompt="Aurora twist band", Size=7):
    B = await H.NewDesign(Prompt)
    Cus = (await H.Client.post(f"/api/designs/{B['design_id']}/customize", json={"candidate_id": B["candidates"][0]["id"]})).json()
    await H.Idle()
    await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"ring_size": Size, "material_id": "silver"})
    await H.Client.post("/api/bag", json={"customization_id": Cus["id"]})
    R = await H.Client.post("/api/orders", json={"customer": Customer, "address": Address, "shipping_method": "standard",
                                                 "terms_accepted": True, "client_request_id": "c-" + Prompt})
    assert R.status_code == 200, R.text
    return B, R.json()


async def OrderLine(H, O):
    return (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()["lines"][0]


# ── uploads ──────────────────────────────────────────────────────────────────
async def test_a_chunked_upload_is_written_to_disk_checked_and_hashed(HE, monkeypatch):
    H = HE
    Data = Torus(10)
    Bad = [({"filename": "part.obj", "bytes": 1000}, 400, "unsupported_file"), ({"filename": "part.stl", "bytes": 10}, 400, "invalid_size")]
    for Body, Status, Code in Bad:
        R = await H.Client.post("/api/admin/uploads", json=Body, headers=Admin)
        assert (R.status_code, R.json()["error"]["code"]) == (Status, Code)
    monkeypatch.setenv("P3_MAX_UPLOAD_MB", "0.001")
    R = await H.Client.post("/api/admin/uploads", json={"filename": "big.stl", "bytes": 5000}, headers=Admin)
    assert (R.status_code, R.json()["error"]["code"]) == (413, "file_too_large")
    monkeypatch.delenv("P3_MAX_UPLOAD_MB")
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=H.App), base_url="http://p3.test" + H.Base) as Anyone:
        assert (await Anyone.post("/api/admin/uploads", json={"filename": "p.stl", "bytes": 100})).status_code in (401, 403)   # Admin only
    # Pieces in order, each at most the piece size; a piece at the wrong offset is refused with what has arrived
    monkeypatch.setattr(UploadsModule, "ChunkBytes", 4096)
    U = (await H.Client.post("/api/admin/uploads", json={"filename": "Eclat Heart v2.stl", "bytes": len(Data)}, headers=Admin)).json()
    Put = lambda Off, Chunk: H.Client.put(f"/api/admin/uploads/{U['id']}?offset={Off}", content=Chunk,
                                          headers={**Admin, "Content-Type": "application/octet-stream"})
    assert (await Put(0, Data[:4096])).json()["received"] == 4096
    R = await Put(0, Data[:4096])
    assert (R.status_code, R.json()["error"]["code"], R.json()["error"]["received"]) == (409, "offset_mismatch", 4096)
    R = await Put(4096, Data[4096:4096 + 5000])
    assert (R.status_code, R.json()["error"]["code"]) == (413, "piece_too_large")
    R = await H.Client.post(f"/api/admin/uploads/{U['id']}/complete", headers=Admin)
    assert (R.status_code, R.json()["error"]["code"]) == (409, "upload_incomplete")
    for Off in range(4096, len(Data), 4096):
        assert (await Put(Off, Data[Off:Off + 4096])).status_code == 200
    R = (await H.Client.post(f"/api/admin/uploads/{U['id']}/complete", headers=Admin)).json()
    assert (R["status"], R["format"], R["sha256"]) == ("complete", "binary", hashlib.sha256(Data).hexdigest())
    assert R["faces"] == (len(Data) - 84) // 50 and R["filename"] == "Eclat Heart v2.stl"
    assert (H.Settings.DevDir / f"uploads/{U['id']}.part").read_bytes() == Data
    # An ASCII STL is an STL; anything else is refused and its file deleted
    A = await Upload(H, Torus(10, Ascii=True), "ascii.stl")
    assert (A["format"], A["faces"]) == ("ascii", None)
    Junk = b"not a mesh at all " * 20
    J = (await H.Client.post("/api/admin/uploads", json={"filename": "junk.stl", "bytes": len(Junk)}, headers=Admin)).json()
    await H.Client.put(f"/api/admin/uploads/{J['id']}?offset=0", content=Junk, headers={**Admin, "Content-Type": "application/octet-stream"})
    R = await H.Client.post(f"/api/admin/uploads/{J['id']}/complete", headers=Admin)
    assert (R.status_code, R.json()["error"]["code"]) == (422, "not_an_stl")
    assert not (H.Settings.DevDir / f"uploads/{J['id']}.part").exists()
    # An upload nobody used is deleted after a day (the row stays, expired)
    H.Ctx.Db.Execute("UPDATE uploads SET updated_at = '2020-01-01T00:00:00.000+00:00' WHERE id = ?", (A["id"],))
    assert H.Svc.Uploads.Cleanup() == 1
    assert H.Ctx.Db.One("SELECT status FROM uploads WHERE id = ?", (A["id"],))["status"] == "expired"
    assert not (H.Settings.DevDir / f"uploads/{A['id']}.part").exists()


# ── Case C: an external revision of an existing design ───────────────────────
async def test_an_external_revision_becomes_the_active_version_and_every_older_one_is_kept(HE):
    H = HE
    B, Did = await ModelledRing(H)
    V = (await H.Client.get(f"/api/admin/designs/{Did}/versions", headers=Admin)).json()["versions"]
    assert [(X["version_no"], X["source"], X["state"], X["scaling"]) for X in V] == [(1, "hi3d", "active", "scalable")]
    V1 = V[0]
    V1File = H.Settings.DevDir / H.Ctx.Db.One("SELECT original_path FROM meshes WHERE id = ?", (V1["id"],))["original_path"]
    U = await Upload(H, Torus(10, 2.0), "Twist band rev2.stl")
    R = await H.Client.post(f"/api/admin/designs/{Did}/versions",
                            json={"upload_id": U["id"], "scaling": "scalable", "source_tool": "Rhino 8", "note": "Thicker band"}, headers=Admin)
    assert R.status_code == 200, R.text
    V2 = R.json()
    assert (V2["version_no"], V2["source"], V2["source_tool"], V2["state"], V2["parent"]["version_no"]) == (2, "external", "Rhino 8", "active", 1)
    assert V2["label"] == "v2 · External · Rhino 8" and V2["original_filename"] == "Twist band rev2.stl"
    assert (H.Settings.DevDir / f"meshes/{V2['id']}/original.stl").read_bytes() == Torus(10, 2.0)      # moved into place
    assert H.Ctx.Db.One("SELECT status, used_by FROM uploads WHERE id = ?", (U["id"],)) == {"status": "used", "used_by": V2["id"]}
    R = await H.Client.post(f"/api/admin/designs/{Did}/versions", json={"upload_id": U["id"]}, headers=Admin)
    assert (R.status_code, R.json()["error"]["code"]) == (409, "upload_not_complete")              # an upload is used once
    await H.Idle()
    V = {X["version_no"]: X for X in (await H.Client.get(f"/api/admin/designs/{Did}/versions", headers=Admin)).json()["versions"]}
    assert V[2]["measure"]["status"] == "measured" and V[2]["measure"]["inner_diameter"] == pytest.approx(16.0, abs=0.05)
    assert V[2]["preview_ready"] and V[1]["state"] == "superseded" and V1File.is_file()            # nothing deleted
    # New 3D results use the active version — arithmetic on its one measurement, no Hi3D request
    from p3.providers import endpoints
    Calls = len(H.Provider.SubmissionsFor(endpoints.Mesh))
    T = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 8}, headers=Admin)).json()
    await H.Idle()
    T = next(X for X in (await Session(H, Did))["three_d"] if X["id"] == T["id"])
    assert T["mesh_id"] == V2["id"] and T["version"]["label"] == "v2 · External · Rhino 8" and T["hi3d"]["provider"] == "external"
    assert T["geometry"]["production"]["inner_diameter_mm"] == pytest.approx(UsSizeToInnerDiameterMm(8), abs=0.01)
    assert T["scaled_stl"] == "on_demand" and not T["exact"]
    assert len(H.Provider.SubmissionsFor(endpoints.Mesh)) == Calls == 1
    # Rollback = making v1 active again; the archive hides a version (never the active one) and keeps its file
    R = await H.Client.post(f"/api/admin/versions/{V2['id']}/archive", headers=Admin)
    assert (R.status_code, R.json()["error"]["code"]) == (409, "version_active")
    R = (await H.Client.post(f"/api/admin/versions/{V1['id']}/activate", json={"note": "back to Hi3D"}, headers=Admin)).json()
    assert R["active_mesh_id"] == V1["id"]
    T1 = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 8}, headers=Admin)).json()
    assert T1["mesh_id"] == V1["id"]
    R = (await H.Client.post(f"/api/admin/versions/{V2['id']}/archive", headers=Admin)).json()
    assert [X["state"] for X in R["versions"]] == ["archived", "active"]
    assert (H.Settings.DevDir / f"meshes/{V2['id']}/original.stl").is_file()
    R = await H.Client.post(f"/api/admin/versions/{V2['id']}/activate", headers=Admin)
    assert (R.status_code, R.json()["error"]["code"]) == (409, "version_archived")
    assert (await H.Client.post(f"/api/admin/versions/{V2['id']}/restore", headers=Admin)).json()["versions"][0]["state"] == "superseded"
    D = await Session(H, Did)
    Kinds = [E["kind"] for E in D["timeline"]]
    for K in ("admin_3d_version_added", "admin_3d_version_activated", "admin_3d_version_archived", "admin_3d_version_restored"):
        assert K in Kinds, K
    assert D["three_d_defaults"]["existing_model"]["version"]["mesh_id"] == V1["id"]
    assert [X["version_no"] for X in D["versions"]] == [2, 1]
    # The raw download says which version it is
    R = await H.Client.post(f"/api/admin/3d/{T['id']}/download-link", json={"stage": "raw"}, headers=Admin)
    File = await H.Client.get(R.json()["url"].removeprefix(H.Ctx.Settings.BasePath))
    assert "_v2_raw.stl" in File.headers["content-disposition"] and File.content == Torus(10, 2.0)


async def test_a_new_hi3d_model_never_displaces_an_active_external_part(HE):
    H = HE
    B, Did = await ModelledRing(H)
    U = await Upload(H, Torus(Us7))
    V2 = (await H.Client.post(f"/api/admin/designs/{Did}/versions", json={"upload_id": U["id"]}, headers=Admin)).json()
    await H.Idle()
    assert (V2["scaling"], V2["state"]) == ("exact", "active")                         # external parts are exact by default
    Other = B["candidates"][1]["id"]
    R = await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"candidate_id": Other, "override": Phrase}, headers=Admin)
    assert R.status_code == 200, R.text
    await H.Idle()
    V = (await H.Client.get(f"/api/admin/designs/{Did}/versions", headers=Admin)).json()
    assert [(X["version_no"], X["source"], X["state"]) for X in V["versions"]] == \
        [(3, "hi3d", "superseded"), (2, "external", "active"), (1, "hi3d", "superseded")]
    assert V["active_mesh_id"] == V2["id"]


# ── exact: used as uploaded, checked against the size ────────────────────────
async def test_an_exact_part_is_the_production_file_and_is_checked_against_the_size(HE):
    H = HE
    B, Did = await ModelledRing(H)
    Data = Torus(Us7)
    U = await Upload(H, Data, "Band US7.stl")
    V2 = (await H.Client.post(f"/api/admin/designs/{Did}/versions", json={"upload_id": U["id"], "scaling": "exact"}, headers=Admin)).json()
    await H.Idle()
    V = (await H.Client.get(f"/api/admin/designs/{Did}/versions", headers=Admin)).json()["versions"][0]
    assert V["measure"]["units"] == "mm" and V["measure"]["us_size"] == pytest.approx(7, abs=0.15) and not V["measure"]["units_warning"]
    # No size chosen: the part's own size is the default
    T = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={}, headers=Admin)).json()
    assert (T["production_size"], T["size_source"], T["mesh_id"]) == (7.0, "default", V2["id"])
    await H.Idle()
    S = await Session(H, Did)
    T = next(X for X in S["three_d"] if X["id"] == T["id"])
    P = T["geometry"]["production"]
    assert T["exact"] and T["scaled_stl"] == "master" and P["scale_factor"] == 1.0
    assert P["inner_diameter_mm"] == pytest.approx(UsSizeToInnerDiameterMm(7), abs=0.05) and P["checks"]["exact"]
    assert (T["status"], T["production_state"], T["review"]) == ("measured", "complete", [])
    # The download is the file itself, under the production name — no export job
    Jobs = H.Ctx.Db.One("SELECT COUNT(*) AS n FROM geometry_jobs WHERE kind = 'export'")["n"]
    E = (await H.Client.post(f"/api/admin/3d/{T['id']}/export", headers=Admin)).json()
    assert (E["job_id"], E["status"]) == ("stored", "done")
    E = (await H.Client.get(f"/api/admin/3d/{T['id']}/export/stored?order=ORD-10077", headers=Admin)).json()
    File = await H.Client.get(E["url"].removeprefix(H.Ctx.Settings.BasePath))
    Name = File.headers["content-disposition"]
    assert File.content == Data and "_R-1001-A_ORD-10077_" in Name and Name.endswith('_US7.stl"'), Name
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM geometry_jobs WHERE kind = 'export'")["n"] == Jobs
    # Another size: the file stays as it is, and the difference is a reason for review
    T9 = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 9}, headers=Admin)).json()
    await H.Idle()
    T9 = next(X for X in (await Session(H, Did))["three_d"] if X["id"] == T9["id"])
    assert T9["production_state"] == "review_required" and [R["code"] for R in T9["review"]] == ["exact_size"]
    assert "US 9 is 18.95 mm" in T9["review"][0]["text"] and T9["geometry"]["production"]["scale_factor"] == 1.0
    R = await H.Client.post(f"/api/admin/3d/{T9['id']}/fix-bore", headers=Admin)
    assert R.status_code == 409 and R.json()["error"]["code"] in ("exact_version", "geometry_not_ready")
    assert (await H.Client.post(f"/api/admin/3d/{T9['id']}/accept", json={"note": "resized in CAD later"}, headers=Admin)).status_code == 200


async def test_an_exact_charm_is_checked_by_its_total_height_and_an_ascii_part_is_kept_binary(HE):
    H = HE
    assert (await H.Client.put("/api/admin/products/charm-sizes", json={"sizes": [10, 14, 18], "names": {}, "default": 14},
                               headers=Admin)).status_code == 200
    import trimesh
    Ascii = trimesh.load(io.BytesIO(CharmStl()), file_type="stl").export(file_type="stl_ascii").encode("ascii")
    S = (await H.Client.post("/api/admin/sessions", json={"owner_account_id": H.Who.AccountId, "title": "Workshop parts"},
                             headers=Admin)).json()
    U = await Upload(H, Ascii, "Moon charm.stl")
    R = await H.Client.post(f"/api/admin/sessions/{S['id']}/external-designs",
                            json={"upload_id": U["id"], "product_type": "charm", "title": "Moon Workshop", "source_tool": "Blender"},
                            headers=Admin)
    assert R.status_code == 200, R.text
    Did, Mid = R.json()["design_id"], R.json()["version"]["id"]
    await H.Idle()
    M = H.Ctx.Db.One("SELECT original_path, stl_path FROM meshes WHERE id = ?", (Mid,))
    assert M == {"original_path": f"meshes/{Mid}/raw.stl", "stl_path": f"meshes/{Mid}/raw.stl"}          # binary only
    assert not (H.Settings.DevDir / f"meshes/{Mid}/original.stl").exists()
    Raw = H.Ctx.Db.One("SELECT sha256, bytes FROM raw_geometry WHERE mesh_id = ?", (Mid,))
    Bin = (H.Settings.DevDir / M["stl_path"]).read_bytes()
    assert Raw == {"sha256": hashlib.sha256(Bin).hexdigest(), "bytes": len(Bin)} and len(Bin) < len(Ascii) / 3
    V = (await H.Client.get(f"/api/admin/designs/{Did}/versions", headers=Admin)).json()["versions"][0]
    assert V["file"]["uploaded_format"] == "ascii" and V["file"]["uploaded_sha256"] == hashlib.sha256(Ascii).hexdigest()
    Height = V["measure"]["height"]
    assert Height == pytest.approx(19.5, abs=0.2)
    T = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={}, headers=Admin)).json()
    assert (T["production_size"], T["size_source"]) == (round(Height, 1), "default")
    T25 = (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 25}, headers=Admin)).json()
    await H.Idle()
    Rows = {X["id"]: X for X in (await Session(H, Did))["three_d"]}
    assert Rows[T["id"]]["production_state"] == "complete" and Rows[T["id"]]["geometry"]["production"]["size_y_mm"] == pytest.approx(Height)
    assert [R["code"] for R in Rows[T25["id"]]["review"]] == ["exact_size"] and "25 mm" in Rows[T25["id"]]["review"][0]["text"]


# ── Sessions with 0, 1 or many designs: new external designs (Cases A, B, D) ─
async def test_external_designs_live_in_sessions_of_zero_one_or_many_designs(HE):
    H = HE
    R = await H.Client.post("/api/admin/sessions", json={"owner_account_id": "acct_nobody", "title": "x"}, headers=Admin)
    assert (R.status_code, R.json()["error"]["code"]) == (404, "account_not_found")
    S = (await H.Client.post("/api/admin/sessions", json={"owner_account_id": H.Who.AccountId, "title": "Bridal set"}, headers=Admin)).json()
    assert (S["ref"], S["design_count"], S["origin"]) == ("S-1001", 0, "admin")
    L = (await H.Client.get("/api/admin/sessions", headers=Admin)).json()
    assert [C["ref"] for C in L["containers"]] == ["S-1001"]
    assert (await Session(H, S["id"]))["container"]["journeys"] == []                        # Case D: an empty session
    # Case A: the first part in an empty session
    U = await Upload(H, Torus(Us7), "band.stl")
    A = (await H.Client.post(f"/api/admin/sessions/{S['id']}/external-designs",
                             json={"upload_id": U["id"], "product_type": "ring", "title": "Bridal Band", "source_tool": "Rhino"},
                             headers=Admin)).json()
    assert A["session"]["design_count"] == 1 and A["version"]["label"] == "v1 · External · Rhino · exact"
    Did = A["design_id"]
    Placeholder = H.Ctx.Db.One("SELECT asset_path FROM candidates WHERE id = ?", (A["version"]["candidate_id"],))["asset_path"]
    assert Placeholder.endswith("/external/placeholder.png")
    await H.Idle()
    D = await Session(H, Did)
    assert D["external"] and D["session"]["origin"] == "external" and D["session"]["path"] == "External part"
    assert D["session"]["ring_id"] == "R-1001" and D["session_group"]["ref"] == "S-1001"
    assert D["session"]["thumbnail_url"].endswith(f"/external/render-{A['version']['id']}.png")          # the 3D render
    Png = H.AssetBytes(D["session"]["thumbnail_url"])
    from PIL import Image
    Img = Image.open(io.BytesIO(Png))
    assert Img.size == (1024, 1024) and len(set(Img.convert("L").getdata())) > 20                       # shaded, not blank
    Steps = D["pipeline"]["steps"]
    assert Steps[0]["kind"] == "external" and Steps[0]["cost"] == 0.0 and D["pipeline"]["total_cost"] == 0.0
    assert [E["kind"] for E in D["timeline"]][:2] == ["started", "external_part"]
    # Case B: another independent design next to it
    U2 = await Upload(H, Torus(Us7 + 1), "band2.stl")
    R = await H.Client.post(f"/api/admin/sessions/{S['id']}/external-designs",
                            json={"upload_id": U2["id"], "product_type": "ring", "title": "bridal band"}, headers=Admin)
    assert (R.status_code, R.json()["error"]["code"]) == (409, "duplicate_title")
    B2 = (await H.Client.post(f"/api/admin/sessions/{S['id']}/external-designs",
                              json={"upload_id": U2["id"], "product_type": "ring", "title": "Bridal Band Wide"}, headers=Admin)).json()
    C = (await Session(H, S["id"]))["container"]
    assert C["design_count"] == 2 and {J["design_id"] for J in C["journeys"]} == {Did, B2["design_id"]}
    assert all(J["active_version"]["source"] == "external" for J in C["journeys"])
    # Next to a design made in the customer flow: its journey becomes an explicit session holding both
    Ai = (await H.NewDesign("A twisted rope band"))["design_id"]
    U3 = await Upload(H, Torus(Us7), "matching.stl")
    R = (await H.Client.post(f"/api/admin/sessions/{Ai}/external-designs",
                             json={"upload_id": U3["id"], "product_type": "ring", "title": "Rope Match"}, headers=Admin)).json()
    assert R["session"]["origin"] == "design" and R["session"]["design_count"] == 2 and R["session"]["ref"] == "S-1002"
    assert (await Session(H, Ai))["session_group"]["id"] == R["session"]["id"]
    U4 = await Upload(H, Torus(Us7), "x.stl")
    R = await H.Client.post("/api/admin/sessions/use_123/external-designs", json={"upload_id": U4["id"], "title": "Xx"}, headers=Admin)
    assert (R.status_code, R.json()["error"]["code"]) == (409, "shared_design")
    # Admin-only for now: not in the customer's My Designs, not openable there, not in the gallery
    Mine = [X["id"] for X in (await H.Client.get("/api/designs")).json()["designs"]]
    assert Did not in Mine and Ai in Mine
    assert (await H.Client.get(f"/api/designs/{Did}")).status_code == 404
    R = await H.Client.post("/api/admin/gallery", json={"design_id": Did}, headers=Admin)
    assert R.status_code == 409 and R.json()["error"]["code"] == "external_part"


# ── orders: pinned to the exact geometry ─────────────────────────────────────
async def test_an_order_line_keeps_its_version_and_a_revised_stl_for_it_becomes_its_version(HE):
    H = HE
    B, O = await Ordered(H)
    Did = B["design_id"]
    assert (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={}, headers=Admin)).status_code == 200      # Hi3D: v1
    await H.Idle()
    L = await OrderLine(H, O)
    assert not L["three_d_pinned"] and L["three_d_version"]["version_no"] == 1
    R = (await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{L['id']}/3d", headers=Admin)).json()
    await H.Idle()
    L = await OrderLine(H, O)
    assert L["three_d_pinned"] and L["three_d_match"] and L["three_d_version"]["version_no"] == 1 and L["three_d_state"] == "complete"
    V1, First = L["three_d_version"]["mesh_id"], L["three_d_id"]
    # A new active version of the design never changes the line
    U = await Upload(H, Torus(Us7), "rev2.stl")
    V2 = (await H.Client.post(f"/api/admin/designs/{Did}/versions", json={"upload_id": U["id"]}, headers=Admin)).json()
    await H.Idle()
    assert (await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 7, "material_id": "silver"},
                                headers=Admin)).json()["mesh_id"] == V2["id"]
    await H.Idle()
    L = await OrderLine(H, O)
    assert (L["three_d_id"], L["three_d_version"]["mesh_id"]) == (First, V1)
    assert {X["version_no"] for X in L["three_d_versions"]} == {1, 2}
    # … only an explicit choice does (logged)
    R = await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{L['id']}/3d-version", json={"mesh_id": V2["id"]}, headers=Admin)
    assert R.status_code == 200 and R.json()["changed"]
    L = await OrderLine(H, O)
    assert L["three_d_version"]["mesh_id"] == V2["id"] and L["three_d_match"] and L["three_d_pinned"]
    # A revised STL uploaded for this order only: a new version of the ordered option; the design keeps its active version
    U3 = await Upload(H, Torus(Us7 + 0.1, 1.6), "ORD fix.stl")
    R = await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{L['id']}/3d-upload",
                            json={"upload_id": U3["id"], "scaling": "exact", "source_tool": "Rhino", "activate": False}, headers=Admin)
    assert R.status_code == 200, R.text
    V3 = R.json()["version"]
    assert (V3["version_no"], V3["state"], V3["parent"]["version_no"]) == (3, "superseded", 2)
    await H.Idle()
    L = await OrderLine(H, O)
    assert L["three_d_version"]["mesh_id"] == V3["id"] and L["three_d_state"] == "complete"
    assert (await H.Client.get(f"/api/admin/designs/{Did}/versions", headers=Admin)).json()["active_mesh_id"] == V2["id"]
    Od = (await H.Client.get(f"/api/admin/orders/{O['id']}", headers=Admin)).json()
    Kinds = [E["kind"] for E in Od["events"]]
    assert Kinds.count("3d_version_changed") == 2 and "3d_version_uploaded" in Kinds and "3d_pinned" in Kinds   # prepared: the match pinned
    Changed = [E for E in Od["events"] if E["kind"] == "3d_version_changed"][-1]["data"]
    assert (Changed["from_mesh_id"], Changed["mesh_id"], Changed["version_no"]) == (V2["id"], V3["id"], 3)
    # In production a change needs a reason
    await H.Client.post(f"/api/admin/orders/{O['id']}/payment", json={"status": "paid", "note": "bank transfer"}, headers=Admin)
    R = await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": "production"}, headers=Admin)
    assert R.status_code == 200, R.text
    R = await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{L['id']}/3d-version", json={"mesh_id": V1}, headers=Admin)
    assert (R.status_code, R.json()["error"]["code"]) == (400, "note_required")


async def test_a_line_that_follows_its_design_is_pinned_when_released(HE):
    H = HE
    B, O = await Ordered(H, "Signet band")
    Did = B["design_id"]
    await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 7, "material_id": "silver"}, headers=Admin)
    await H.Idle()
    L = await OrderLine(H, O)
    assert L["three_d_match"] and not L["three_d_pinned"]                 # made on the session page: it follows the design
    await H.Client.post(f"/api/admin/orders/{O['id']}/payment", json={"status": "paid", "note": "card"}, headers=Admin)
    assert (await H.Client.post(f"/api/admin/orders/{O['id']}/status", json={"status": "three_d_ready"}, headers=Admin)).status_code == 200
    L2 = await OrderLine(H, O)
    assert L2["three_d_pinned"] and L2["three_d_id"] == L["three_d_id"]
    Row = H.Ctx.Db.One("SELECT three_d_id, mesh_id FROM order_lines WHERE id = ?", (L["id"],))
    assert Row == {"three_d_id": L["three_d_id"], "mesh_id": L["three_d_version"]["mesh_id"]}


# ── migration: what the code used before, written down once ──────────────────
async def test_the_backfill_numbers_versions_activates_the_newest_and_pins_order_lines(HE):
    H = HE
    B, O = await Ordered(H)
    Did = B["design_id"]
    await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"production_size": 7, "material_id": "silver"}, headers=Admin)
    await H.Idle()
    R = await H.Client.post(f"/api/admin/sessions/{Did}/3d", json={"candidate_id": B["candidates"][2]["id"], "override": Phrase}, headers=Admin)
    assert R.status_code == 200
    await H.Idle()
    Line = (await OrderLine(H, O))
    Want = {M["id"]: M["version_no"] for M in H.Ctx.Db.All("SELECT id, version_no FROM meshes")}
    Active = H.Ctx.Db.One("SELECT active_mesh_id FROM designs WHERE id = ?", (Did,))["active_mesh_id"]
    # A database from before versions: no numbers, no active version, no pins, no mark
    Db = H.Ctx.Db
    Db.Execute("UPDATE meshes SET version_no = NULL")
    Db.Execute("UPDATE designs SET active_mesh_id = NULL")
    Db.Execute("UPDATE order_lines SET three_d_id = NULL, mesh_id = NULL")
    Db.Execute("DELETE FROM schema_marks")
    Database(H.Settings.DbPath)
    assert {M["id"]: M["version_no"] for M in Db.All("SELECT id, version_no FROM meshes")} == Want and sorted(Want.values()) == [1, 2]
    assert Db.One("SELECT active_mesh_id FROM designs WHERE id = ?", (Did,))["active_mesh_id"] == Active
    Pin = Db.One("SELECT three_d_id, mesh_id FROM order_lines WHERE order_id = ?", (O["id"],))
    assert Pin["three_d_id"] == Line["three_d_id"] and Pin["mesh_id"] is not None
    Db.Execute("UPDATE order_lines SET three_d_id = NULL")
    Database(H.Settings.DbPath)                                                # once only
    assert Db.One("SELECT three_d_id FROM order_lines WHERE order_id = ?", (O["id"],))["three_d_id"] is None


# ── the render ───────────────────────────────────────────────────────────────
def test_a_part_is_rendered_from_its_light_preview(tmp_path):
    from p3.render3d import RenderPng
    from PIL import Image
    Stl = tmp_path / "band.stl"
    Stl.write_bytes(Torus(Us7))
    Raw = MeasureRaw(Stl)
    WritePreview(Stl, Raw, tmp_path / "band.p3pv")
    R = RenderPng(tmp_path / "band.p3pv", tmp_path / "band.png", "ring", 256)
    Img = Image.open(tmp_path / "band.png").convert("L")
    Pixels = np.asarray(Img)
    assert R["size"] == 256 and Img.size == (256, 256)
    assert (Pixels < 235).mean() > 0.05 and Pixels[0, 0] >= 240                    # a part in the middle, background at the edge
