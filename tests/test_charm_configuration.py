"""Charms, phase 2 — their own AI configuration: separate models, prompts, versions and history, edited in Admin →
AI models & prompts with the Ring | Charm selector; and the separate switch "Charms available to customers". Saving
one product's configuration never touches the other's."""

import pytest

from p3 import naming as Naming
from p3.providers import endpoints
from tests.conftest import Harness

AdminKey = "charm-config-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
RingModels = ("nano-banana-pro", "nano-banana-pro-edit", "minimax-camera", "hi3d")
CharmModels = ("nano-banana-pro-charm", "nano-banana-pro-edit-charm", "minimax-camera-charm", "hi3d-charm")


@pytest.fixture
async def HK(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    yield Obj
    await Obj.Close()


async def _Model(H, Id):
    return (await H.Client.get(f"/api/admin/models/{Id}", headers=Admin)).json()


async def test_charm_models_start_from_the_ring_settings_with_their_own_prompts(HK):
    H = HK
    L = (await H.Client.get("/api/admin/models", headers=Admin)).json()["models"]
    assert {M["model"]["id"]: M["model"]["product"] for M in L} == {
        "any-llm": "ring", **{M: "ring" for M in RingModels}, "any-llm-charm": "charm", **{M: "charm" for M in CharmModels}}
    Gen, RingGen = await _Model(H, "nano-banana-pro-charm"), await _Model(H, "nano-banana-pro")
    assert Gen["active"]["id"].startswith("nano-banana-pro-charm@v1-") and len(Gen["history"]) == 1
    P = Gen["active"]["params"]
    assert "production-feasible jewelry charm" in P["system_prompt"] and "plain, smooth, round, closed metal loop" in P["system_prompt"]
    assert "Do not attach a charm to the ring" not in P["system_prompt"] and "jewelry ring" not in P["system_prompt"]
    assert P["prompt"].startswith("{{user_text}}") and "one plain, round, undecorated loop" in P["prompt"]
    # provider settings copied from the ring's active version; the prompts are the charm's own
    Same = {K: V for K, V in RingGen["active"]["params"].items() if K not in ("prompt", "system_prompt")}
    assert {K: V for K, V in P.items() if K not in ("prompt", "system_prompt")} == Same
    Edit = (await _Model(H, "nano-banana-pro-edit-charm"))["active"]["params"]
    assert "The reference image is one of two things" in Edit["system_prompt"] and "plain loop at the top" in Edit["variation_a"]
    Movie, RingMovie = (await _Model(H, "minimax-camera-charm"))["active"]["params"], (await _Model(H, "minimax-camera"))["active"]["params"]
    assert "The charm in the reference is rigid" in Movie["prompt"] and Movie["camera_trajectory"] == RingMovie["camera_trajectory"]
    assert (await _Model(H, "hi3d-charm"))["active"]["params"] == (await _Model(H, "hi3d"))["active"]["params"]
    Health = (await H.Client.get("/api/admin/health", headers=Admin)).json()     # the versions are Admin information
    assert set(Health["config_versions"]) == set(RingModels) and set(Health["charm_config_versions"]) == set(CharmModels)


async def test_saving_one_products_configuration_never_changes_the_other(HK):
    H = HK
    Before = {M: (await _Model(H, M)) for M in RingModels + CharmModels}
    New = {**Before["nano-banana-pro-charm"]["active"]["params"], "system_prompt": "A charm prompt, edited in the Admin"}
    R = await H.Client.post("/api/admin/models/nano-banana-pro-charm/activate", json={"params": New, "note": "charm edit"}, headers=Admin)
    assert R.status_code == 200 and R.json()["changed"] and R.json()["active"]["id"].startswith("nano-banana-pro-charm@v2-")
    for M in RingModels:                                         # every ring version, active pointer and history untouched
        Now_ = await _Model(H, M)
        assert Now_["active"] == Before[M]["active"] and len(Now_["history"]) == len(Before[M]["history"])
    # … and the other way round
    Ring = {**Before["minimax-camera"]["active"]["params"], "duration": Before["minimax-camera"]["active"]["params"]["duration"] + 1}
    assert (await H.Client.post("/api/admin/models/minimax-camera/activate", json={"params": Ring}, headers=Admin)).json()["changed"]
    for M in ("nano-banana-pro-edit-charm", "minimax-camera-charm", "hi3d-charm"):
        assert (await _Model(H, M))["active"] == Before[M]["active"]
    assert (await _Model(H, "nano-banana-pro-charm"))["active"]["params"]["system_prompt"] == "A charm prompt, edited in the Admin"
    # Restoring the charm's first version is a charm action only
    V1 = Before["nano-banana-pro-charm"]["active"]["id"]
    R = await H.Client.post("/api/admin/models/nano-banana-pro-charm/restore", json={"version_id": V1}, headers=Admin)
    assert R.json()["active"]["number"] == 3 and R.json()["active"]["params"] == Before["nano-banana-pro-charm"]["active"]["params"]
    # A version of one product cannot be restored into the other
    Bad = await H.Client.post("/api/admin/models/nano-banana-pro/restore", json={"version_id": V1}, headers=Admin)
    assert Bad.status_code == 400


async def test_exports_are_per_product_and_the_ring_export_is_unchanged(HK):
    H = HK
    Rings = (await H.Client.get("/api/admin/models/export?format=json", headers=Admin)).json()["models"]
    assert [M["model"] for M in Rings] == ["any-llm", *RingModels] and {M["product"] for M in Rings} == {"ring"}
    Charms = await H.Client.get("/api/admin/models/export?format=json&product=charm", headers=Admin)
    assert [M["model"] for M in Charms.json()["models"]] == ["any-llm-charm", *CharmModels]
    assert 'filename="p3-ai-config-all-charm.json"' in Charms.headers["content-disposition"]
    Text = (await H.Client.get("/api/admin/models/export?format=txt&product=charm", headers=Admin)).text
    assert "Charm configuration" in Text and "Ring configuration" not in Text


async def test_an_admin_previews_the_charm_pipeline_end_to_end_in_mock_mode(HK):
    H = HK
    assert (await H.Client.post("/api/admin/login", json={"key": AdminKey})).status_code == 200    # this browser: Admin preview
    Ring = await H.NewDesign("A slim twisted band")
    Batch = await H.NewDesign("A crescent moon charm with a small star", product="charm")
    D = await H.Design(Batch["design_id"])
    assert D["product_type"] == "charm" and Batch["config_version"].startswith("nano-banana-pro-charm@")
    assert Ring["config_version"].startswith("nano-banana-pro@")                                     # rings keep their configuration
    Sub = H.Provider.SubmissionsFor(endpoints.ImageGenerate)
    assert all("jewelry charm" in S[1]["system_prompt"] for S in Sub[-4:]) and all("jewelry ring" in S[1]["system_prompt"] for S in Sub[:4])
    Row = H.Ctx.Db.One("SELECT charm_no, ring_no FROM designs WHERE id = ?", (Batch["design_id"],))
    assert (Row["charm_no"], Row["ring_no"]) == (1001, None)
    assert D["title"] == "Nova Star"                                         # the local naming rules, charm words left out
    Names = (await H.Client.get(f"/api/admin/designs/{Batch['design_id']}/names", headers=Admin)).json()["suggestions"]
    assert Names and not {W.lower() for N in Names for W in N.split()} & Naming.CharmForbidden
    # A refinement uses the charm edit configuration, with the charm's own variation directives
    R = await H.Client.post(f"/api/designs/{Batch['design_id']}/batches", json={
        "parent_candidate_id": Batch["candidates"][0]["id"], "instruction": "Make the moon thinner"})
    assert R.status_code == 200 and R.json()["config_version"].startswith("nano-banana-pro-edit-charm@")
    await H.Idle()
    Edits = H.Provider.SubmissionsFor(endpoints.ImageEdit)[-4:]
    assert all("jewelry charm" in S[1]["system_prompt"] for S in Edits)
    assert any("plain loop at the top" in S[1]["prompt"] for S in Edits)
    # The movie uses the charm movie configuration
    M = await H.Client.post(f"/api/candidates/{Batch['candidates'][1]['id']}/movie")
    assert M.status_code == 200 and M.json()["config_version"].startswith("minimax-camera-charm@")
    await H.Idle()
    assert "The charm in the reference" in H.Provider.SubmissionsFor(endpoints.Movie)[-1][1]["prompt"]
    # Committed (a movie): the next refinement is a new design — still a charm, with its own C- number
    R2 = await H.Client.post(f"/api/designs/{Batch['design_id']}/batches", json={
        "parent_candidate_id": Batch["candidates"][1]["id"], "instruction": "Add a small star"})
    await H.Idle()
    Fork = H.Ctx.Db.One("SELECT product_type, charm_no, ring_no FROM designs WHERE id = ?", (R2.json()["design_id"],))
    assert R2.json()["design_id"] != Batch["design_id"] and (Fork["product_type"], Fork["charm_no"], Fork["ring_no"]) == ("charm", 1002, None)


async def test_customers_create_charms_only_while_charms_are_available(HK):
    H = HK
    Customer = {"X-Access-Token": H.Ctx.Accounts.IssueToken("customer")[0]}

    async def Try():
        return await H.Client.post("/api/designs", data={"prompt": "A heart charm", "product": "charm"}, headers=Customer)
    assert (await Try()).status_code == 400
    Cat = (await H.Client.get("/api/catalog", headers=Customer)).json()
    assert "products" not in Cat                                                                   # the ring-only catalog of before
    # A simple switch: no typed phrase (the Admin confirms in a normal dialog)
    On = await H.Client.put("/api/admin/products/availability", json={"charms_available": True}, headers=Admin)
    assert On.status_code == 200 and On.json()["charms_available"] is True and "show_confirmation" not in On.json()
    assert On.json()["log"][0]["key"] == "charms_available" and On.json()["log"][0]["value"] is True
    R = await Try()
    assert R.status_code == 200 and R.json()["config_version"].startswith("nano-banana-pro-charm@")
    Cat = (await H.Client.get("/api/catalog", headers=Customer)).json()
    assert Cat["products"]["available"] == ["ring", "charm"] and Cat["products"]["preview"] is False
    assert Cat["products"]["charm"]["sizes"] == [10.0, 14.0, 18.0] and Cat["products"]["charm"]["recommended_size"] == 14.0
    assert Cat["products"]["charm"]["size_definition"]["text"] == "The total height of the charm, including the attachment loop at the top."
    assert Cat["products"]["charm"]["size_definition"]["includes_loop"] is True
    Off = await H.Client.put("/api/admin/products/availability", json={"charms_available": False}, headers=Admin)
    assert Off.json()["charms_available"] is False and (await Try()).status_code == 400
    assert (await H.Client.put("/api/admin/products/availability", json={"charms_available": True}, headers=Customer)).status_code == 403


@pytest.mark.parametrize("Prompt, Expected", [
    ("A crescent moon charm with a small star", "Nova Star"),
    ("A heart pendant on a chain with a loop at the top", "Amour"),          # how it hangs is not a design word
    ("A Pendant inspired by Haaland", "Haaland"),
    ("Open cuff heart charm", "Amour"),                                      # ring-only descriptors are never offered
    ("a chain link charm, chunky", "Bold Link"),
])
def test_charm_names_follow_the_naming_rules_without_charm_framing_words(Prompt, Expected):
    assert Naming.RingName(Prompt, Product="charm") == Expected
    for Name in Naming.Suggestions(Prompt, set(), N=8, Product="charm") + list(Naming.Candidates(Prompt, Product="charm"))[:30]:
        assert not {W.lower() for W in Name.split()} & (Naming.CharmForbidden | Naming.Forbidden), Name
    # a refinement keeps its lineage, still without the framing words
    V = Naming.RingName(Prompt, Lineage=Expected, Instruction="hang it from a thicker chain with a bigger loop", Product="charm")
    assert V.split()[0] == Expected.split()[0] and not {W.lower() for W in V.split()} & Naming.CharmForbidden


def test_rings_are_named_exactly_as_before():
    assert Naming.RingName("Open cuff heart charm") == "Amour Open"
    assert Naming.RingName("A heart pendant on a chain with a loop at the top") == "Amour Knot"
    for P in ("A slim twisted band", "Bold signet with a lion crest", "an infinity loop ring", "Midi knuckle ring, slim"):
        assert Naming.RingName(P) == Naming.RingName(P, Product="ring")
        assert list(Naming.Candidates(P)) == list(Naming.Candidates(P, Product="ring"))


async def test_any_llm_has_a_charm_configuration_of_its_own(HK):
    H = HK
    Ring, Charm = await _Model(H, "any-llm"), await _Model(H, "any-llm-charm")
    assert Charm["model"]["product"] == "charm" and Charm["model"]["endpoint"] == Ring["model"]["endpoint"] == "fal-ai/any-llm"
    assert Charm["active"]["id"].startswith("any-llm-charm@v1-") and len(Charm["history"]) == 1
    P = Charm["active"]["params"]
    assert P["prompt"] == "{{user_prompt}}" and P["system_prompt"].startswith("You validate requests for an AI charm-design application.")
    assert "wearable finger rings" not in P["system_prompt"] and "plain loop at its top" in P["system_prompt"]
    # Saving the charm configuration never changes the ring one, and the reverse
    New = {**P, "model": "openai/gpt-4o-mini", "temperature": 0.2, "system_prompt": "Charm check, edited"}
    R = await H.Client.post("/api/admin/models/any-llm-charm/activate", json={"params": New, "note": "charm"}, headers=Admin)
    assert R.json()["changed"] and R.json()["active"]["number"] == 2
    assert (await _Model(H, "any-llm"))["active"] == Ring["active"]
    RingNew = {**Ring["active"]["params"], "system_prompt": "Ring check, edited", "max_tokens": 300}
    assert (await H.Client.post("/api/admin/models/any-llm/activate", json={"params": RingNew}, headers=Admin)).json()["changed"]
    Now_ = (await _Model(H, "any-llm-charm"))["active"]["params"]
    assert (Now_["system_prompt"], Now_["model"], Now_.get("max_tokens")) == ("Charm check, edited", "openai/gpt-4o-mini", None)
    # Restoring stays within each product's own history
    R = await H.Client.post("/api/admin/models/any-llm-charm/restore", json={"version_id": Charm["active"]["id"]}, headers=Admin)
    assert R.json()["active"]["number"] == 3 and R.json()["active"]["params"] == P
    assert (await H.Client.post("/api/admin/models/any-llm-charm/restore", json={"version_id": Ring["active"]["id"]},
                                headers=Admin)).status_code == 400
    Preview = (await H.Client.post("/api/admin/models/any-llm-charm/preview", json={}, headers=Admin)).json()
    assert "charm" in Preview["payload"]["prompt"].lower() and Preview["submitted"] is False


def test_the_charm_any_llm_seed_copies_the_ring_settings_by_value():
    from p3.modelconfig import CharmSeed
    Ring = {"prompt": "{{user_prompt}}", "system_prompt": "ring rules", "model": "google/gemini-2.5-flash", "temperature": 0.0,
            "max_tokens": 512, "priority": "latency", "reasoning": False}
    Seed = CharmSeed("any-llm-charm", Ring)
    assert {K: Seed[K] for K in ("model", "temperature", "max_tokens", "priority", "reasoning")} == \
        {K: Ring[K] for K in ("model", "temperature", "max_tokens", "priority", "reasoning")}
    assert Seed["system_prompt"] != "ring rules" and Seed["prompt"] == "{{user_prompt}}"
    Seed["model"] = "openai/gpt-4o"
    assert Ring["model"] == "google/gemini-2.5-flash"                     # a copy, never a reference
