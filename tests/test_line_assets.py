"""An order line's assets, after the owner's report of 2026-10-17 (ORD-10002 and ORD-10003 on tron; the evidence is kept in
/home/yakir/p3-checkpoints/2026-10-17_100002-evidence-ORD-10002-10003):

- The same STL in another material (ORD-10002 Fil Rope: the STL existed at its size in Silver, the Stainless Steel line was
  asked to prepare it again). A scaled STL is made from the master and the size only, so it is recognized as ready — the
  same file — and recorded for the line's material when the line is sent, at once, with that material's weight. Never for
  another size, never from a flagged file, never from a bore-corrected one (FixBore's reuse stays explicit).
- Another option's model (ORD-10002 Aurora Curve: ordered as option C, the design's model is of option B). It is never the
  line's: no Prepare, no other option's result shown as its latest, and sending is refused naming both options.
- A line whose picture, 3D version or STL is not of its design, option, size or material is never sent; the problem is named.
- An Admin page older than the server's (ORD-10003: a tab open since before the release kept the old page logic and showed
  outdated verdicts) changes nothing until it is reloaded; every Admin answer tells the page the current build.
- Several sizes and materials of the same design: each line is sent with the STL of its own size and material."""

import re

from p3 import app as AppModule
from tests.conftest import OfferCharmSizes
from tests.test_bore_fix_workflow import _Another, _Oval, _Propose
from tests.test_charm_3d import Prices
from tests.test_fulfillment_bridge import Admin, AdminKey, Prefix, Service, _On, _Placed, _Prepared
from tests.test_send_to_production_rules import _Design, _InBag, _Model, _Place, _Prepare, _State


def _Admin(H, OrderId: str) -> dict:
    return H.Svc.Orders.AdminGet(OrderId)["lines"][0]


async def _Verdict(H, OrderId: str) -> dict:
    return (await _State(H, OrderId))["lines"][0]


async def _SendLine(H, OrderId: str, Links: dict | None = None) -> object:
    L = _Admin(H, OrderId)
    return await H.Client.post(f"/api/admin/orders/{OrderId}/production", json={"line_ids": [L["id"]], **({"links": Links} if Links else {})}, headers=Admin)


def _Geo(H, Sid: str) -> dict:
    return H.Ctx.Db.One("SELECT volume_mm3, size_x_mm, size_y_mm, size_z_mm FROM geometry_results WHERE session_3d_id = ? AND stage = 'production'", (Sid,))


def _Weight(H, Sid: str) -> float:
    return H.Ctx.Db.One("SELECT weight_g FROM price_calculations WHERE session_3d_id = ? ORDER BY created_at DESC LIMIT 1", (Sid,))["weight_g"]


def _Results(H, Did: str, Material: str, Size) -> list[dict]:
    return H.Ctx.Db.All("SELECT * FROM session_3d WHERE design_id = ? AND material_id = ? AND production_size = ? ORDER BY created_at",
                        (Did, Material, float(Size)))


async def test_a_charm_s_stl_finished_in_silver_is_the_stainless_line_s_stl_recorded_for_stainless_when_sent(tmp_path):
    H = _On(tmp_path)
    try:
        assert (await H.Client.post("/api/admin/login", json={"key": AdminKey})).status_code == 200
        await OfferCharmSizes(H, AdminKey)
        Both = {**Prices, "stainless_steel": {"fixed_prices": {"20": 90, "25": 110}, "price_per_g": 10, "cost_per_g": 4}}
        assert (await H.Client.put("/api/admin/charm-prices", json={"materials": Both}, headers=Admin)).status_code == 200
        Did, Cand = await _Design(H, "A rope charm with a twist", Charm=True)
        await _Model(H, Did, 20, "silver")                                       # the 20 mm STL, finished in Silver
        Silver = _Results(H, Did, "silver", 20)[0]
        await _InBag(H, Did, Cand, "stainless_steel", Charm=20)
        O = await _Place(H, "fil-rope")
        X = await _Verdict(H, O["id"])
        assert X["stl"]["state"] == "ready_shared" and X["eligible"], X["stl"]
        assert X["stl"]["label"] == "STL ready — the same file as the Sterling Silver STL at 20 mm" and "Stainless Steel" in X["stl"]["action"]
        assert X["stl"]["link"]["id"] == Silver["id"] and X["stl"]["link"]["from_material_id"] == "silver"
        assert not _Results(H, Did, "stainless_steel", 20)                      # nothing recorded yet — and nothing to prepare
        R = await _SendLine(H, O["id"])                                          # the link is confirmed, never silent
        assert R.status_code == 409 and R.json()["error"]["code"] == "link_confirmation_required"
        assert "the same file as the Sterling Silver STL" in R.json()["error"]["message"]
        assert not _Results(H, Did, "stainless_steel", 20) and not _Admin(H, O["id"])["three_d_pinned"]   # a refused hand-over records nothing
        R = await _SendLine(H, O["id"], {_Admin(H, O["id"])["id"]: Silver["id"]})
        assert R.status_code == 200, R.text
        Mine = _Results(H, Did, "stainless_steel", 20)
        assert len(Mine) == 1 and Mine[0]["status"] == "measured" and Mine[0]["mesh_id"] == Silver["mesh_id"]
        assert _Geo(H, Mine[0]["id"]) == _Geo(H, Silver["id"])                   # the very same geometry …
        assert _Weight(H, Mine[0]["id"]) != _Weight(H, Silver["id"])             # … and Stainless Steel's own weight
        L = _Admin(H, O["id"])
        assert L["three_d_id"] == Mine[0]["id"] and L["three_d_pinned"] and L["three_d_match"] and not L["three_d_problems"]
        P = H.Ctx.Db.One("SELECT * FROM fulfillment_line_production WHERE line_id = ?", (L["id"],))
        assert P["three_d_id"] == Mine[0]["id"]
        E = H.Ctx.Db.One("SELECT data_json FROM order_events WHERE order_id = ? AND kind = '3d_prepared'", (O["id"],))
        assert f'"shared_from": "{Silver["id"]}"' in E["data_json"]
        Snap = (await H.Client.get(Prefix + f"/requests/order/{O['id']}", headers=Service)).json()
        Line = Snap["lines"][0]
        assert Line["material_id"] == "stainless_steel" and Line["production"]["pinned"]["three_d_id"] == Mine[0]["id"]
    finally:
        await H.Close()


async def test_a_shared_stl_is_never_of_another_size_nor_a_flagged_or_bore_corrected_file(tmp_path, monkeypatch):
    from p3.providers import mock
    monkeypatch.setattr(mock, "_MeshBytes", _Oval)                              # every result of this model is flagged (oval bore)
    H = _On(tmp_path)
    try:
        Did, A = await _Placed(H, Qty=1, Click="sh-a")                          # Silver US 7: prepared, flagged
        LA = await _Prepared(H, Did, A["id"])
        assert LA["three_d_state"] == "review_required"
        Cand = LA["candidate_id"]
        V = await _Another(H, Did, Cand, "sh-v", Material="vermeil")            # Vermeil US 7
        X = await _Verdict(H, V["id"])
        assert X["stl"]["state"] == "needs_preparation", X["stl"]                 # a flagged STL is never another material's
        P1 = await _Propose(H, LA["three_d_id"])                                  # FixBore on Silver, accepted: a file of its own
        assert (await H.Client.post(f"/api/admin/bore-fixes/{P1['id']}/accept", json={"note": "round US 7"}, headers=Admin)).status_code == 200
        X = await _Verdict(H, V["id"])
        assert X["stl"]["state"] == "needs_preparation", X["stl"]                 # the corrected file is reused explicitly only
        Big = await _Another(H, Did, Cand, "sh-8", Size=8)                        # Silver US 8: another size, nothing shared
        assert (await _Verdict(H, Big["id"]))["stl"]["state"] == "needs_preparation"
        await _Prepare(H, V["id"], _Admin(H, V["id"])["id"])                     # Vermeil prepared: flagged too — FixBore's reuse offered
        X = await _Verdict(H, V["id"])
        assert X["stl"]["state"] == "review_required" and X["stl"]["reusable_correction"]["bore_fix_id"] == P1["id"]
    finally:
        await H.Close()


async def test_another_option_s_model_is_never_the_line_s_and_sending_names_both_options(tmp_path):
    H = _On(tmp_path)
    try:
        B = await H.NewDesign("A curved band with a soft wave")
        Did, OptA, OptB = B["design_id"], B["candidates"][0]["id"], B["candidates"][1]["id"]
        H.Svc.Meshes.Create(OptB, None)                                          # the design's model is of option B
        await H.Idle()
        await _Model(H, Did, 9.5, "silver")                                       # a result of option B (from the design page)
        await _InBag(H, Did, OptA, "stainless_steel", Size=9.5)                  # the customer orders option A
        O = await _Place(H, "aurora")
        L = _Admin(H, O["id"])
        RefA, RefB = L["ring_id"], L["model_ring_id"]
        assert RefA.endswith("-A") and RefB.endswith("-B")
        assert L["three_d_version"] is None and L["three_d_latest"] is None and not L["three_d_match"]   # B's result is not shown as A's
        X = await _Verdict(H, O["id"])
        assert X["stl"]["state"] == "other_option" and not X["eligible"]
        assert X["stl"]["label"] == f"No 3D model of the ordered option {RefA} — the design's model is of {RefB}"
        assert f"the model of {RefB} is never used for this line" in X["stl"]["action"]
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{L['id']}/3d", headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "model_of_other_option"
        R = await _SendLine(H, O["id"])
        assert R.status_code == 409 and RefA in R.json()["error"]["message"] and RefB in R.json()["error"]["message"]
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_line_production")["n"] == 0
    finally:
        await H.Close()


async def test_a_line_whose_stl_or_picture_is_not_its_own_is_never_sent_and_the_problem_is_named(tmp_path):
    H = _On(tmp_path)
    try:
        Did, O = await _Placed(H, Qty=1, Click="mx-1")                          # Silver US 7, prepared and pinned
        L = await _Prepared(H, Did, O["id"])
        Own, Img = L["three_d_id"], H.Ctx.Db.One("SELECT image_path FROM order_lines WHERE id = ?", (L["id"],))["image_path"]
        assert (await _Verdict(H, O["id"]))["stl"]["state"] == "ready"
        Db = H.Ctx.Db

        async def Problem(Sid=None, Image=None) -> list[str]:
            Db.Execute("UPDATE order_lines SET three_d_id = ?, image_path = ? WHERE id = ?", (Sid or Own, Image or Img, L["id"]))
            X = await _Verdict(H, O["id"])
            assert X["stl"]["state"] == "inconsistent" and not X["eligible"], X["stl"]
            R = await _SendLine(H, O["id"])
            assert R.status_code == 409 and "does not match this line" in R.json()["error"]["message"]
            assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_line_production")["n"] == 0
            return X["stl"]["problems"]

        await _Model(H, Did, 8, "silver")                                         # another size
        assert "its STL is for US 8, the line is US 7" in await Problem(Sid=_Results(H, Did, "silver", 8)[0]["id"])
        await _Model(H, Did, 7, "vermeil")                                        # another material
        assert "its STL is recorded for 14K Gold over Stainless Steel, the line is Sterling Silver" in await Problem(Sid=_Results(H, Did, "vermeil", 7)[0]["id"])
        Other = Db.One("SELECT c.id FROM candidates c JOIN batches b ON b.id = c.batch_id WHERE b.design_id = ? AND c.id != ? LIMIT 1",
                       (Did, L["candidate_id"]))["id"]
        assert any(P.startswith("its picture is not of the ordered option") for P in await Problem(Image=Img.replace(L["candidate_id"], Other)))
        Mesh = H.Svc.Meshes.Create(Other, None)                                   # another option's model and result (a paid model is
        await H.Idle()                                                            # mocked here)
        T = H.Svc.Production3D.Request(Did, ProductionSize=7, MaterialId="silver", CandidateId=Other, RequestedBy="test", MeshId=Mesh["id"])
        await H.Idle()
        Problems = await Problem(Sid=T["id"])
        assert any(P.startswith("its STL is of ") and P.endswith(f"not of the ordered {L['ring_id']}") for P in Problems), Problems
        # Prepare STL repairs a wrong STL link: the line's own STL is made from its own option and pinned in its place
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{L['id']}/3d", headers=Admin)
        assert R.status_code == 200 and R.json()["prepared"], R.text
        await H.Idle()
        Fixed = _Admin(H, O["id"])
        assert Fixed["three_d_match"] and not Fixed["three_d_problems"] and (await _Verdict(H, O["id"]))["stl"]["state"] == "ready"
        S = Db.One("SELECT candidate_id, production_size, material_id FROM session_3d WHERE id = ?", (Fixed["three_d_id"],))
        assert (S["candidate_id"], float(S["production_size"]), S["material_id"]) == (L["candidate_id"], 7.0, "silver")
        E = Db.One("SELECT data_json FROM order_events WHERE order_id = ? AND kind = '3d_prepared' ORDER BY id DESC LIMIT 1", (O["id"],))
        assert f'"replaced": "{T["id"]}"' in E["data_json"] and "its STL is of " in E["data_json"]
        # A wrong picture is the order line's own record: Prepare refuses and changes nothing
        Db.Execute("UPDATE order_lines SET image_path = ? WHERE id = ?", (Img.replace(L["candidate_id"], Other), L["id"]))
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/lines/{L['id']}/3d", headers=Admin)
        assert R.status_code == 409 and R.json()["error"]["code"] == "line_record_inconsistent"
        assert _Admin(H, O["id"])["three_d_id"] == Fixed["three_d_id"]
        Db.Execute("UPDATE order_lines SET three_d_id = ?, image_path = ? WHERE id = ?", (Own, Img, L["id"]))   # restored: sendable again
        assert (await _Verdict(H, O["id"]))["stl"]["state"] == "ready"
        assert (await _SendLine(H, O["id"])).status_code == 200
    finally:
        await H.Close()


async def test_a_change_from_an_outdated_admin_page_is_refused_until_it_is_reloaded(tmp_path, monkeypatch):
    H = _On(tmp_path)
    try:
        Page = (await H.Client.get("/admin")).text
        Build = re.search(r'name="p3-admin-build" content="([0-9a-f]{16})"', Page).group(1)
        R = await H.Client.get("/api/admin/orders", headers=Admin)
        assert R.status_code == 200 and R.headers["X-Admin-Build"] == Build
        _Did, O = await _Placed(H, Qty=1, Click="st-1")

        def Notes() -> int:
            return H.Ctx.Db.One("SELECT COUNT(*) AS n FROM order_events WHERE order_id = ? AND kind = 'note'", (O["id"],))["n"]
        Before = Notes()
        assert (await H.Client.post(f"/api/admin/orders/{O['id']}/note", json={"note": "from the current page"},
                                    headers={**Admin, "X-Admin-Build": Build})).status_code == 200
        Current = Notes()
        assert Current > Before
        # A release: the server's Admin build changes; the page that was open keeps its own
        monkeypatch.setattr(AppModule, "AdminBuild", lambda: "f" * 16)
        R = await H.Client.post(f"/api/admin/orders/{O['id']}/note", json={"note": "from an outdated page"}, headers={**Admin, "X-Admin-Build": Build})
        assert R.status_code == 409 and R.json()["error"]["code"] == "admin_outdated" and "Reload the page" in R.json()["error"]["message"]
        assert R.headers["X-Admin-Build"] == "f" * 16 and Notes() == Current      # nothing changed
        R = await H.Client.get(f"/api/admin/orders/{O['id']}", headers={**Admin, "X-Admin-Build": Build})
        assert R.status_code == 200 and R.headers["X-Admin-Build"] == "f" * 16   # reading still works: the page learns it is outdated
        assert (await H.Client.post(f"/api/admin/orders/{O['id']}/note", json={"note": "from a tool"}, headers=Admin)).status_code == 200   # scripts: unaffected
        assert "f" * 16 in (await H.Client.get("/admin")).text                    # a reloaded page carries the new build
    finally:
        await H.Close()


async def test_several_sizes_and_materials_of_one_design_are_each_sent_with_their_own_stl(tmp_path):
    H = _On(tmp_path)
    try:
        Did, A = await _Placed(H, Qty=1, Click="mm-a")                          # Silver US 7
        LA = await _Prepared(H, Did, A["id"])
        Cand = LA["candidate_id"]
        V = await _Another(H, Did, Cand, "mm-v", Material="vermeil")            # Vermeil US 7: the same file, shared
        N = await _Another(H, Did, Cand, "mm-9", Size=9)                         # Silver US 9: not prepared yet
        assert [(await _Verdict(H, X["id"]))["stl"]["state"] for X in (A, V, N)] == ["ready", "ready_shared", "needs_preparation"]
        assert (await _SendLine(H, A["id"])).status_code == 200
        Link = (await _Verdict(H, V["id"]))["stl"]["link"]["id"]
        assert (await _SendLine(H, V["id"], {_Admin(H, V["id"])["id"]: Link})).status_code == 200
        await _Prepare(H, N["id"], _Admin(H, N["id"])["id"])
        assert (await _Verdict(H, N["id"]))["stl"]["state"] == "ready"
        assert (await _SendLine(H, N["id"])).status_code == 200
        for O, Size, Material in ((A, 7.0, "silver"), (V, 7.0, "vermeil"), (N, 9.0, "silver")):
            L = _Admin(H, O["id"])
            P = H.Ctx.Db.One("SELECT three_d_id FROM fulfillment_line_production WHERE line_id = ?", (L["id"],))
            S = H.Ctx.Db.One("SELECT * FROM session_3d WHERE id = ?", (P["three_d_id"],))
            assert (float(S["production_size"]), S["material_id"], S["candidate_id"]) == (Size, Material, Cand), (O["ref"], dict(S))
            assert not L["three_d_problems"]
    finally:
        await H.Close()


async def test_the_selected_option_is_ordered_with_its_own_picture_version_movie_and_stl_never_another_option_s(tmp_path):
    """ORD-10002 Aurora Curve: the line is the option the customer had selected (C there, A here), never silently another;
    the option that has the model (B) is ordered with B's own picture, version, movie and STL."""
    H = _On(tmp_path)
    try:
        B = await H.NewDesign("A curved band with a soft wave")
        Did, OptA, OptB = B["design_id"], B["candidates"][0]["id"], B["candidates"][1]["id"]
        H.Svc.Meshes.Create(OptB, None)                                          # the design's model is of option B
        await H.Idle()
        MeshB = H.Ctx.Db.One("SELECT id FROM meshes WHERE candidate_id = ? AND status = 'ready'", (OptB,))["id"]
        await _InBag(H, Did, OptB, "silver", Size=9.5)                           # the customer selects B
        OB = await _Place(H, "sel-b")
        LB = _Admin(H, OB["id"])
        assert LB["candidate_id"] == OptB and LB["ring_id"].endswith("-B") and f"/candidates/{OptB}." in LB["image_url"]
        assert LB["pinned_mesh_id"] == MeshB and LB["three_d_version"]["mesh_id"] == MeshB and not LB["three_d_problems"]
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM movies WHERE candidate_id = ?", (OptB,))["n"] >= 1     # B's own movie
        await _Prepare(H, OB["id"], LB["id"])
        assert (await _SendLine(H, OB["id"])).status_code == 200
        Sid = H.Ctx.Db.One("SELECT three_d_id FROM fulfillment_line_production WHERE line_id = ?", (LB["id"],))["three_d_id"]
        S = H.Ctx.Db.One("SELECT candidate_id, mesh_id FROM session_3d WHERE id = ?", (Sid,))
        assert (S["candidate_id"], S["mesh_id"]) == (OptB, MeshB)                 # B's STL went to Production
        await _InBag(H, Did, OptA, "silver", Size=9.5)                           # then the customer selects A: A is ordered
        OA = await _Place(H, "sel-a")
        LA = _Admin(H, OA["id"])
        assert LA["candidate_id"] == OptA and LA["ring_id"].endswith("-A") and f"/candidates/{OptA}." in LA["image_url"]
        assert LA["pinned_mesh_id"] is None and LA["three_d_version"] is None   # A has no model: never B's
        assert (await _Verdict(H, OA["id"]))["stl"]["state"] == "other_option"
        assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM movies WHERE candidate_id = ?", (OptA,))["n"] >= 1     # A's own movie
        Page = (await H.Client.get(f"/api/admin/sessions/{Did}", headers=Admin)).json()["artifacts"]
        assert Page["ring_id"].endswith("-A") and Page["movie_ring_id"].endswith("-A") and Page["model_ring_id"].endswith("-B")
    finally:
        await H.Close()
