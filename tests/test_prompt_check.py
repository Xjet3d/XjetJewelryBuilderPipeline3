"""The prompt check (p3/promptcheck.py): with its switch on for a product, the LLM reads every new design and
refinement request before any paid image request. Off by default on every site; only instructions that ask for its
decision can drive it; a stopped request creates nothing and costs no credit; no decision lets the request through;
every check shows on the session page, the customer's activity and the any-llm page."""

import pytest

from p3 import assets
from p3.config import CandidatesPerBatch as N
from p3.providers import endpoints
from p3.providers.mock import _RingImage
from tests.conftest import Harness

AdminKey = "prompt-check-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
RingInstructions = {
    "model": "google/gemini-2.5-flash", "prompt": "{{user_prompt}}", "temperature": 0.0, "max_tokens": 512,
    "priority": "latency", "reasoning": False,
    "system_prompt": "You validate requests for an AI ring-design application. Return one JSON object with is_jewelry, "
                     "is_feasible, reason_for_rejection and refined_prompt.",
}


@pytest.fixture
async def HP(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    yield Obj
    await Obj.Close()


async def RingCheckOn(H):
    R = await H.Client.post("/api/admin/models/any-llm/activate", json={"params": RingInstructions, "note": "test"}, headers=Admin)
    assert R.status_code == 200, R.text
    R = await H.Client.put("/api/admin/prompt-check", json={"product": "ring", "on": True}, headers=Admin)
    assert R.status_code == 200 and R.json()["on"] == {"ring": True, "charm": False}, R.text


def Checks(H) -> list[dict]:
    return H.Ctx.Db.All("SELECT * FROM prompt_checks ORDER BY created_at")


async def test_off_by_default_nothing_is_checked(HP):
    H = HP
    S = (await H.Client.get("/api/admin/prompt-check", headers=Admin)).json()
    assert S["on"] == {"ring": False, "charm": False} and S["checks"] == []
    assert "prompt_check" not in (await H.Client.get("/api/catalog")).json()
    Batch = await H.NewDesign("A slim twisted band")
    assert Batch["status"] == "complete"
    assert H.Provider.SubmissionsFor(endpoints.Llm) == [] and Checks(H) == []
    Models = {M["model"]["id"]: M["model"] for M in (await H.Client.get("/api/admin/models", headers=Admin)).json()["models"]}
    assert Models["any-llm"]["connected"] is False and Models["any-llm-charm"]["connected"] is False


async def test_it_turns_on_only_with_instructions_that_ask_for_its_decision(HP):
    H = HP
    R = await H.Client.put("/api/admin/prompt-check", json={"product": "ring", "on": True}, headers=Admin)
    assert R.status_code == 409 and R.json()["error"]["code"] == "prompt_check_not_ready"        # the seeded ring version has none
    assert "is_jewelry" in R.json()["error"]["message"]
    await RingCheckOn(H)
    assert (await H.Client.get("/api/catalog")).json()["prompt_check"] == ["ring"]
    M = (await H.Client.get("/api/admin/models/any-llm", headers=Admin)).json()
    assert M["model"]["connected"] is True
    Log = H.Ctx.Db.All("SELECT * FROM product_settings_log WHERE key = 'prompt_check'")
    assert len(Log) == 1 and Log[0]["by"] and "on for rings" in Log[0]["note"]
    # While it is on, the instructions must keep asking for the decision
    Bad = {**RingInstructions, "system_prompt": "Describe the ring nicely."}
    R = await H.Client.post("/api/admin/models/any-llm/activate", json={"params": Bad}, headers=Admin)
    assert R.status_code == 400 and "Turn the check off first" in R.text
    assert (await H.Client.put("/api/admin/prompt-check", json={"product": "ring", "on": False}, headers=Admin)).json()["on"]["ring"] is False
    assert (await H.Client.post("/api/admin/models/any-llm/activate", json={"params": Bad}, headers=Admin)).status_code == 200


async def test_a_stopped_request_creates_nothing_and_costs_no_credit(HP):
    H = HP
    await RingCheckOn(H)
    Before = (await H.Client.get("/api/token-status")).json()
    H.Provider.Script(endpoints.Llm, "reject")
    R = await H.Client.post("/api/designs", data={"prompt": "A ring with a famous politician's face"})
    assert R.status_code == 422 and R.json()["error"] == {"code": "prompt_rejected", "message": "Mock: this request can't be designed."}
    await H.Idle()
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM designs")["n"] == 0
    assert H.Provider.SubmissionsFor(endpoints.ImageGenerate) == [] and len(H.Provider.SubmissionsFor(endpoints.Llm)) == 1
    After = (await H.Client.get("/api/token-status")).json()
    assert (After["used"], After["reserved"]) == (Before["used"], Before["reserved"]) == (0, 0)
    [C] = Checks(H)
    assert (C["decision"], C["kind"], C["product_type"], C["text"]) == ("rejected", "design", "ring", "A ring with a famous politician's face")
    assert C["reason"] == "Mock: this request can't be designed." and C["design_id"] is None and C["ai_mode"] == "mock"
    U = H.Ctx.Accounts.Db.One("SELECT * FROM usage_events WHERE kind = 'prompt_check'")
    assert U["ref_id"] == C["id"] and U["endpoint"] == endpoints.Llm and U["internal"] == 1               # XJet's cost, never a credit
    # The any-llm page shows it; the customer's activity shows live checks (mock activity stays out of the Admin there)
    P = (await H.Client.get("/api/admin/prompt-check?product=ring", headers=Admin)).json()
    assert P["checks"][0]["decision"] == "rejected" and P["counts"]["mock"] == {"rejected": 1}
    Seen = lambda D: [E for E in D["timeline"] if E["kind"] == "prompt_check"]
    assert Seen((await H.Client.get(f"/api/admin/users/{H.Who.AccountId}", headers=Admin)).json()) == []
    H.Ctx.Db.Execute("UPDATE prompt_checks SET ai_mode = 'live'")
    Ev = Seen((await H.Client.get(f"/api/admin/users/{H.Who.AccountId}", headers=Admin)).json())
    assert Ev and Ev[0]["status"] == "rejected" and "Mock: this request can't be designed." in Ev[0]["text"]


async def test_an_accepted_request_is_made_with_the_customers_words_and_shows_its_check(HP):
    H = HP
    await RingCheckOn(H)
    Batch = await H.NewDesign("Art deco band with stepped shoulders")
    assert Batch["status"] == "complete"
    [Llm] = H.Provider.SubmissionsFor(endpoints.Llm)
    assert Llm[1]["prompt"] == "Art deco band with stepped shoulders" and "is_feasible" in Llm[1]["system_prompt"]
    Images = H.Provider.SubmissionsFor(endpoints.ImageGenerate)
    assert len(Images) == N and all(S[1]["prompt"].startswith("Art deco band with stepped shoulders\n\n") for S in Images)  # never the rewrite
    [C] = Checks(H)
    assert (C["decision"], C["design_id"], C["batch_id"]) == ("accepted", Batch["design_id"], Batch["id"])
    assert C["refined_prompt"].startswith("Mock: ")
    D = (await H.Client.get(f"/api/admin/sessions/{Batch['design_id']}", headers=Admin)).json()
    Steps = [S["kind"] for S in D["pipeline"]["steps"]]
    assert Steps[:2] == ["prompt_check", "design"] and D["pipeline"]["steps"][0]["requests"] == 1
    assert any(E["kind"] == "prompt_check" and E["status"] == "accepted" for E in D["timeline"])


@pytest.mark.parametrize("Outcome", ["prose", "fail"])
async def test_no_decision_lets_the_request_through(HP, Outcome):
    H = HP
    await RingCheckOn(H)
    H.Provider.Script(endpoints.Llm, Outcome)
    Batch = await H.NewDesign("A plain band")
    assert Batch["status"] == "complete" and len(H.Provider.SubmissionsFor(endpoints.ImageGenerate)) == N
    [C] = Checks(H)
    assert C["decision"] == "undecided" and C["batch_id"] == Batch["id"] and C["error"]


async def test_a_refinement_is_read_with_its_design_and_a_stopped_one_shows_on_the_session(HP):
    H = HP
    await RingCheckOn(H)
    First = await H.NewDesign("A silver snake ring with tiny scales")
    Option = First["candidates"][0]["id"]
    await H.Client.put(f"/api/designs/{First['design_id']}/selection", json={"candidate_id": Option})
    H.Provider.Script(endpoints.Llm, "reject")
    R = await H.Client.post(f"/api/designs/{First['design_id']}/batches",
                            json={"parent_candidate_id": Option, "instruction": "put a famous face on it"})
    assert R.status_code == 422 and R.json()["error"]["code"] == "prompt_rejected"
    await H.Idle()
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM batches WHERE design_id = ?", (First["design_id"],))["n"] == 1
    Read = H.Provider.SubmissionsFor(endpoints.Llm)[-1][1]["prompt"]
    assert Read == ("Refinement of an existing design.\nPrevious request: A silver snake ring with tiny scales\n"
                    "Requested change: put a famous face on it")
    D = (await H.Client.get(f"/api/admin/sessions/{First['design_id']}", headers=Admin)).json()
    Stopped = [E for E in D["timeline"] if E["kind"] == "prompt_check" and E["status"] == "rejected"]
    assert Stopped and "put a famous face on it" in Stopped[0]["text"]
    assert [S["status"] for S in D["pipeline"]["steps"] if S["kind"] == "prompt_check"] == ["rejected", "accepted"]
    # An accepted refinement is made, and its check is linked to its batch
    R = await H.Client.post(f"/api/designs/{First['design_id']}/batches",
                            json={"parent_candidate_id": Option, "instruction": "make it thinner"})
    assert R.status_code == 200
    await H.Idle()
    assert Checks(H)[-1]["batch_id"] == R.json()["id"] and Checks(H)[-1]["kind"] == "refinement"


async def test_a_reference_image_is_named_to_the_check_and_a_repeat_is_not_checked_again(HP):
    H = HP
    await RingCheckOn(H)
    Png = assets.NormalizeReferenceImage(_RingImage(3, "ref"))
    Form = {"prompt": "like this but in gold", "rights_confirmed": "true", "client_request_id": "req-ref-1"}
    for _ in range(2):
        R = await H.Client.post("/api/designs", data=Form, files={"reference": ("ref.png", Png, "image/png")})
        assert R.status_code == 200, R.text
    await H.Idle()
    [Llm] = H.Provider.SubmissionsFor(endpoints.Llm)                                           # one check for one request
    assert Llm[1]["prompt"] == "Reference image: attached (not shown to you).\nCustomer request: like this but in gold"


async def test_each_product_has_its_own_switch(HP):
    H = HP
    assert (await H.Client.put("/api/admin/products/availability", json={"charms_available": True}, headers=Admin)).status_code == 200
    await RingCheckOn(H)
    Charm = await H.NewDesign("A small crescent moon", product="charm")                      # charms: still off
    assert Charm["status"] == "complete" and Checks(H) == []
    R = await H.Client.put("/api/admin/prompt-check", json={"product": "charm", "on": True}, headers=Admin)
    assert R.status_code == 200 and R.json()["on"] == {"ring": True, "charm": True}            # the charm seed asks for the decision
    await H.NewDesign("A tiny star", product="charm")
    [C] = Checks(H)
    assert C["product_type"] == "charm" and C["config_version"].startswith("any-llm-charm@")
    assert "charm" in H.Provider.SubmissionsFor(endpoints.Llm)[-1][1]["system_prompt"]
    assert (await H.Client.get("/api/catalog")).json()["prompt_check"] == ["ring", "charm"]
