"""FE-03 (owner's approved plan, 14 Nov 2026): one refinement at a time per design. A second Enter, click or request
while a refinement is being made is refused (409 refinement_in_progress) before any credit is taken; requests sent
together make one batch and one credit; a retry of the same request is answered with what it made; after the
refinement has finished a new one works as before. Mock provider only."""

import asyncio
import re
from pathlib import Path

import pytest

from p3.providers.mock import MockProvider
from tests.conftest import Harness

Root = Path(__file__).resolve().parents[1]


@pytest.fixture
async def H(tmp_path):
    Obj = Harness(tmp_path, Provider=MockProvider(LatencyS=0.0, RenderVideo=False))
    yield Obj
    Obj.Provider.LatencyS = 0.0
    await Obj.Close()


async def _Status(H) -> dict:
    return (await H.Client.get("/api/token-status")).json()


def _Body(Cid, Words, Rid):
    return {"parent_candidate_id": Cid, "instruction": Words, "client_request_id": Rid}


async def _Ready(H):
    B = await H.NewDesign("A crescent moon with a small star")
    return B["design_id"], B["candidates"][0]["id"]


async def test_a_second_request_while_one_runs_is_refused_and_costs_nothing(H):
    Did, Cid = await _Ready(H)
    H.Provider.LatencyS = 30.0                                      # the first refinement stays "generating"
    R1 = await H.Client.post(f"/api/designs/{Did}/batches", json=_Body(Cid, "thinner band", "rq-1"))
    assert R1.status_code == 200, R1.text
    Before = await _Status(H)
    R2 = await H.Client.post(f"/api/designs/{Did}/batches", json=_Body(Cid, "thinner band", "rq-2"))   # Enter again
    assert R2.status_code == 409 and R2.json()["error"]["code"] == "refinement_in_progress", R2.text
    After = await _Status(H)
    assert (After["used"], After["reserved"], After["remaining"]) == (Before["used"], Before["reserved"], Before["remaining"])
    # A network retry of the first request is answered with the batch it made, not refused and not charged again
    R3 = await H.Client.post(f"/api/designs/{Did}/batches", json=_Body(Cid, "thinner band", "rq-1"))
    assert R3.status_code == 200 and R3.json()["id"] == R1.json()["id"]
    assert (await _Status(H))["remaining"] == Before["remaining"]
    # Once it has finished, the next refinement is an ordinary one
    H.Provider.LatencyS = 0.0
    await H.Idle()
    R4 = await H.Client.post(f"/api/designs/{Did}/batches", json=_Body(Cid, "add a tiny star", "rq-3"))
    assert R4.status_code == 200, R4.text
    await H.Idle()
    D = await H.Design(Did)
    assert [B["kind"] for B in D["batches"]] == ["initial", "refine", "refine"]


async def test_requests_sent_together_make_one_batch_and_one_credit(H):
    Did, Cid = await _Ready(H)
    Before = await _Status(H)
    H.Provider.LatencyS = 30.0
    Rs = await asyncio.gather(*[H.Client.post(f"/api/designs/{Did}/batches", json=_Body(Cid, "more organic", f"rq-{I}"))
                                for I in range(6)])
    Codes = sorted(R.status_code for R in Rs)
    assert Codes == [200, 409, 409, 409, 409, 409], [R.text for R in Rs]
    After = await _Status(H)
    assert Before["remaining"] - After["remaining"] == 1               # one credit, for the one refinement made
    H.Provider.LatencyS = 0.0
    await H.Idle()
    assert len([B for B in (await H.Design(Did))["batches"] if B["kind"] == "refine"]) == 1


async def test_the_check_under_the_write_lock_refuses_and_releases_the_credit(H, monkeypatch):
    """Two requests that both passed the early check (a race) still make one batch: the second is refused inside the
    write transaction and the credit it reserved is released."""
    Did, Cid = await _Ready(H)
    H.Provider.LatencyS = 30.0
    assert (await H.Client.post(f"/api/designs/{Did}/batches", json=_Body(Cid, "slimmer", "rq-a"))).status_code == 200
    Before = await _Status(H)
    Svc = H.Svc.Images
    Original = Svc.RequireNoRefinementRunning
    monkeypatch.setattr(Svc, "RequireNoRefinementRunning",
                        lambda Who, D, Conn=None: Original(Who, D, Conn) if Conn is not None else None)
    R = await H.Client.post(f"/api/designs/{Did}/batches", json=_Body(Cid, "slimmer", "rq-b"))
    assert R.status_code == 409 and R.json()["error"]["code"] == "refinement_in_progress"
    After = await _Status(H)
    assert (After["used"], After["reserved"], After["remaining"]) == (Before["used"], Before["reserved"], Before["remaining"])


async def test_a_master_refined_into_a_new_design_is_not_forked_twice(H):
    """A design with a movie is a master: its refinement is a new design. A second press while that one is being made
    must not make (and charge) a second new design."""
    Did, Cid = await _Ready(H)
    await H.Proceed(Did, Cid)                                       # the 360° movie makes it a master
    await H.Idle()
    Before = await _Status(H)
    H.Provider.LatencyS = 30.0
    R1 = await H.Client.post(f"/api/designs/{Did}/batches", json=_Body(Cid, "a lattice inside", "rq-f1"))
    assert R1.status_code == 200 and R1.json()["design_id"] != Did, R1.text
    R2 = await H.Client.post(f"/api/designs/{Did}/batches", json=_Body(Cid, "a lattice inside", "rq-f2"))
    assert R2.status_code == 409 and R2.json()["error"]["code"] == "refinement_in_progress", R2.text
    assert Before["remaining"] - (await _Status(H))["remaining"] == 1
    H.Provider.LatencyS = 0.0
    await H.Idle()
    Mine = (await H.Client.get("/api/designs")).json()["designs"]
    assert len(Mine) == 2                                           # the master and one new design, not two


def test_the_page_sends_one_request_per_press():
    """Enter goes through the same guard as the Send button: nothing while a request is on its way or a refinement is
    being made (the button was disabled, Enter was not)."""
    Js = (Root / "web" / "app.js").read_text(encoding="utf-8")
    Enter = re.search(r"handleEnterKey\(ev\) \{(.*?)\},\n", Js, re.S).group(1)
    assert "sendComposer()" in Enter and "ev.repeat" in Enter
    Send = re.search(r"async sendComposer\(\) \{(.*?)\n    \},", Js, re.S).group(1)
    assert "if (this.submitting) return;" in Send
    Refine = re.search(r"async refine\(\) \{(.*?)\n    \},", Js, re.S).group(1)
    assert Refine.index("if (this.submitting) return;") < Refine.index("this.api('POST'")
    assert Refine.index("if (this.pendingBatch)") < Refine.index("this.api('POST'")
    Generate = re.search(r"async generate\(\) \{(.*?)\n    \},", Js, re.S).group(1)
    assert Generate.index("if (this.submitting) return;") < Generate.index("this.api('POST'")
