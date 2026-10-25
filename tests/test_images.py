"""Four-candidate generation, selection, refinement lineage, partial failure, dedupe, recovery."""

import asyncio

import pytest

from p3 import assets
from p3.config import CandidatesPerBatch as N
from p3.providers import endpoints
from p3.providers.mock import MockProvider, _RingImage
from tests.conftest import Harness


async def test_requires_access_token(H):
    R = await H.Client.post("/api/designs", data={"prompt": "a ring"}, headers={"X-Access-Token": ""})
    assert R.status_code == 401
    R = await H.Client.post("/api/designs", data={"prompt": "a ring"}, headers={"X-Access-Token": "p3_bogus"})
    assert R.status_code == 401
    assert H.Provider.Submissions == []


async def test_initial_batch_is_n_distinct_candidates_from_one_prompt(H):
    Batch = await H.NewDesign("Art deco band with stepped shoulders")
    assert Batch["kind"] == "initial" and Batch["status"] == "complete"
    Cands = Batch["candidates"]
    assert N == 4 and [C["slot"] for C in Cands] == list(range(N))
    assert all(C["status"] == "ready" and C["image_url"] for C in Cands)
    assert len({C["id"] for C in Cands}) == N
    assert len({assets.Sha256(H.AssetBytes(C["image_url"])) for C in Cands}) == N

    Subs = H.Provider.SubmissionsFor(endpoints.ImageGenerate)
    assert len(Subs) == N
    Prompts = {S[1]["prompt"] for S in Subs}
    assert len(Prompts) == 1 and Prompts.pop().startswith("Art deco band with stepped shoulders\n\n")
    assert len({S[1]["seed"] for S in Subs}) == N
    assert all(S[1]["num_images"] == 1 and "image_urls" not in S[1] for S in Subs)


async def test_prompt_validation(H):
    R = await H.Client.post("/api/designs", data={"prompt": "  "})
    assert R.status_code == 400 and R.json()["error"]["code"] == "text_too_short"


async def test_create_is_idempotent_per_client_request_id(H):
    A = await H.NewDesign("Plain band", client_request_id="req-1")
    B = await H.NewDesign("Plain band", client_request_id="req-1")
    assert A["id"] == B["id"]
    assert len(H.Provider.Submissions) == N


async def test_reference_upload_requires_rights_and_uses_edit(H):
    Png = assets.NormalizeReferenceImage(_RingImage(7, "ref"))
    R = await H.Client.post("/api/designs", data={"prompt": "Make it like this"},
                            files={"reference": ("ref.png", Png, "image/png")})
    assert R.status_code == 400 and R.json()["error"]["code"] == "rights_not_confirmed"
    R = await H.Client.post("/api/designs", data={"prompt": "Make it like this", "rights_confirmed": "true"},
                            files={"reference": ("ref.png", Png, "image/png")})
    assert R.status_code == 200
    await H.Idle()
    Subs = H.Provider.SubmissionsFor(endpoints.ImageEdit)
    assert len(Subs) == N and len({S[1]["image_urls"][0] for S in Subs}) == 1


async def test_selection_requires_ready_candidate_of_same_design(H):
    A = await H.NewDesign("Design A")
    B = await H.NewDesign("Design B")
    R = await H.Client.put(f"/api/designs/{A['design_id']}/selection",
                           json={"candidate_id": B["candidates"][0]["id"]})
    assert R.status_code == 404
    R = await H.Client.put(f"/api/designs/{A['design_id']}/selection",
                           json={"candidate_id": A["candidates"][2]["id"]})
    assert R.status_code == 200
    assert (await H.Design(A["design_id"]))["selected_candidate_id"] == A["candidates"][2]["id"]


async def test_refine_uses_selected_image_for_all_outputs(H):
    First = await H.NewDesign("Signet ring with a hexagon face")
    Selected = First["candidates"][3]
    await H.Client.put(f"/api/designs/{First['design_id']}/selection", json={"candidate_id": Selected["id"]})
    R = await H.Client.post(f"/api/designs/{First['design_id']}/batches",
                            json={"parent_candidate_id": Selected["id"], "instruction": "Add fine milgrain edges"})
    assert R.status_code == 200, R.text
    await H.Idle()
    Refined = (await H.Client.get(f"/api/batches/{R.json()['id']}")).json()
    assert Refined["kind"] == "refine" and Refined["parent_candidate_id"] == Selected["id"]
    assert Refined["status"] == "complete" and len(Refined["candidates"]) == N

    Subs = H.Provider.SubmissionsFor(endpoints.ImageEdit)
    assert len(Subs) == N
    Prompts = [S[1]["prompt"] for S in Subs]            # the same instruction for all four; each image adds its own variation directive
    assert __import__("os").path.commonprefix(Prompts).startswith("Add fine milgrain edges") and len(set(Prompts)) == N
    assert Subs[0][1]["prompt"].startswith("Add fine milgrain edges\n\n")
    RefUrls = {S[1]["image_urls"][0] for S in Subs}
    assert len(RefUrls) == 1
    assert H.Provider.Uploads[RefUrls.pop()] == H.AssetBytes(Selected["image_url"])
    # The earlier batch is preserved, and the selection did not move on its own.
    D = await H.Design(First["design_id"])
    assert [B["id"] for B in D["batches"]] == [First["id"], Refined["id"]]
    assert D["selected_candidate_id"] == Selected["id"]


async def test_refine_with_missing_reference_is_recoverable_error_not_text_only(H):
    First = await H.NewDesign("Band")
    Selected = First["candidates"][0]
    (H.Settings.AssetsDir / Selected["image_url"].removeprefix("/assets/")).unlink()
    Before = len(H.Provider.Submissions)
    R = await H.Client.post(f"/api/designs/{First['design_id']}/batches",
                            json={"parent_candidate_id": Selected["id"], "instruction": "Thicker band"})
    assert R.status_code == 409 and R.json()["error"]["code"] == "reference_unavailable"
    assert len(H.Provider.Submissions) == Before


async def test_refine_rejects_not_ready_or_foreign_parent(H):
    A = await H.NewDesign("Design A")
    B = await H.NewDesign("Design B")
    R = await H.Client.post(f"/api/designs/{A['design_id']}/batches",
                            json={"parent_candidate_id": B["candidates"][0]["id"], "instruction": "Thicker band"})
    assert R.status_code == 404


async def test_partial_failure_keeps_successes_and_retries_only_failed_slot(H):
    H.Provider.Script(endpoints.ImageGenerate, "ok", "fail", "ok", "fail")
    Batch = await H.NewDesign("Braided band")
    assert Batch["status"] == "partial"
    Failed = [C for C in Batch["candidates"] if C["status"] == "failed"]
    Ready = {C["id"]: C["image_url"] for C in Batch["candidates"] if C["status"] == "ready"}
    assert len(Failed) == 2 and len(Ready) == N - 2 and all(C["retryable"] for C in Failed)

    R = await H.Client.post(f"/api/candidates/{Failed[0]['id']}/retry")
    assert R.status_code == 200
    await H.Idle()
    After = (await H.Client.get(f"/api/batches/{Batch['id']}")).json()
    assert len(H.Provider.Submissions) == N + 1      # exactly one extra request
    assert {C["id"]: C["image_url"] for C in After["candidates"] if C["id"] in Ready} == Ready
    assert After["status"] == "partial"

    R = await H.Client.post(f"/api/batches/{Batch['id']}/retry-failed")
    await H.Idle()
    After = (await H.Client.get(f"/api/batches/{Batch['id']}")).json()
    assert After["status"] == "complete" and len(H.Provider.Submissions) == N + 2

    R = await H.Client.post(f"/api/candidates/{Failed[0]['id']}/retry")
    assert R.status_code == 409                       # ready slots are never regenerated


async def test_entire_batch_failure_is_reported_and_retryable(H):
    H.Provider.Script(endpoints.ImageGenerate, *["fail"] * N)
    Batch = await H.NewDesign("Band")
    assert Batch["status"] == "failed"
    assert Batch["user_text"] == "Band"               # prompt preserved for retry
    await H.Client.post(f"/api/batches/{Batch['id']}/retry-failed")
    await H.Idle()
    assert (await H.Client.get(f"/api/batches/{Batch['id']}")).json()["status"] == "complete"


async def test_a_description_the_model_refuses_is_never_offered_again(tmp_path):
    """Every option refused under the model's content rules (atelier, 2026-10-08: a charm asking for a real politician's
    picture): the customer gets the app's sentence and no retry — asking anyway is refused before anything is reserved or
    paid — and no credit is charged; the Admin sees the provider's words."""
    from p3.images import CustomerError
    Key = "policy-admin-key"
    H = Harness(tmp_path, AdminKey=Key)
    try:
        H.Provider.Script(endpoints.ImageGenerate, *["policy"] * N)
        Batch = await H.NewDesign("A charm with a famous politician's portrait")
        assert Batch["status"] == "failed"
        assert all(C["error_code"] == "content_policy" and not C["retryable"] for C in Batch["candidates"])
        assert {C["error"] for C in Batch["candidates"]} == {CustomerError("content_policy")}
        for Path_ in (f"/api/candidates/{Batch['candidates'][0]['id']}/retry", f"/api/batches/{Batch['id']}/retry-failed"):
            R = await H.Client.post(Path_)
            assert R.status_code == 409 and R.json()["error"]["code"] == "not_retryable", Path_
        await H.Idle()
        assert len(H.Provider.Submissions) == N                                         # nothing asked again
        S = (await H.Client.get("/api/token-status")).json()
        assert (S["used"], S["reserved"]) == (0, 0)                                       # no image, no credit
        D = (await H.Client.get(f"/api/admin/sessions/{Batch['design_id']}", headers={"Authorization": f"Bearer {Key}"})).json()
        assert {C["error"] for B in D["design"]["batches"] for C in B["candidates"]} == {
            "We couldn't process this image or description because it may not meet our content guidelines. "
            "Please revise your description."}
        assert not any(E["kind"] == "option_requested" for E in D["timeline"])
    finally:
        await H.Close()


async def test_try_again_skips_a_refused_option_and_every_retry_is_in_the_journey(tmp_path):
    """One option refused, two with no image: "Generate another option" and "Try again" ask only for the slots that can
    come out differently, and the Admin's Journey shows each customer retry (which options, why they had failed)."""
    Key = "journey-admin-key"
    H = Harness(tmp_path, AdminKey=Key)
    try:
        H.Provider.Script(endpoints.ImageGenerate, "policy", "nomedia", "nomedia")
        Batch = await H.NewDesign("Band")
        Failed = [C for C in Batch["candidates"] if C["status"] == "failed"]
        Refused = [C for C in Failed if C["error_code"] == "content_policy"]
        Empty = [C for C in Failed if C["error_code"] == "no_media_generated"]
        assert len(Refused) == 1 and len(Empty) == 2 and Batch["status"] == "partial"
        assert all(C["retryable"] for C in Empty) and not Refused[0]["retryable"]
        assert (await H.Client.post(f"/api/candidates/{Empty[0]['id']}/retry")).status_code == 200       # Generate another option
        await H.Idle()
        assert (await H.Client.post(f"/api/batches/{Batch['id']}/retry-failed")).status_code == 200      # Try again: the rest
        await H.Idle()
        After = {C["id"]: C for C in (await H.Client.get(f"/api/batches/{Batch['id']}")).json()["candidates"]}
        assert len(H.Provider.Submissions) == N + 2                                      # the two empty slots, never the refused one
        assert all(After[C["id"]]["status"] == "ready" for C in Empty)
        assert After[Refused[0]["id"]]["error_code"] == "content_policy"
        D = (await H.Client.get(f"/api/admin/sessions/{Batch['design_id']}", headers={"Authorization": f"Bearer {Key}"})).json()
        Refs = {C["id"]: C["ring_id"] for B in D["design"]["batches"] for C in B["candidates"]}
        Asked = [E["data"] for E in D["timeline"] if E["kind"] == "option_requested"]
        assert [(A["options"], A["after"]) for A in Asked] == [([Refs[Empty[0]["id"]]], ["no_media_generated"]),
                                                                ([Refs[Empty[1]["id"]]], ["no_media_generated"])]
    finally:
        await H.Close()


async def test_exact_duplicate_output_is_a_failed_option_never_re_requested(H):
    H.Provider.Script(endpoints.ImageGenerate, "duplicate", "duplicate")
    Batch = await H.NewDesign("Band")
    Codes = sorted(C["error_code"] or "" for C in Batch["candidates"])
    assert Codes.count("duplicate_output") == 1 and Batch["status"] == "partial"
    assert len({assets.Sha256(H.AssetBytes(C["image_url"])) for C in Batch["candidates"] if C["image_url"]}) == N - 1
    assert len(H.Provider.Submissions) == N          # no automatic (paid) re-request: "Generate another option" is the customer's
    Dup = next(C for C in Batch["candidates"] if C["error_code"] == "duplicate_output")
    assert Dup["retryable"] and Dup["error"] == "This option came out identical to another one."


Generic = "This option couldn't be generated."


async def _Usable(H, Batch):
    """The ready options of a batch work as usual: selecting, Customize and refining from one."""
    Ready = [C for C in Batch["candidates"] if C["status"] == "ready"]
    R = await H.Client.put(f"/api/designs/{Batch['design_id']}/selection", json={"candidate_id": Ready[0]["id"]})
    assert R.status_code == 200
    Cus = await H.Proceed(Batch["design_id"], Ready[0]["id"])
    assert Cus["image_url"] == Ready[0]["image_url"]
    R = await H.Client.post(f"/api/designs/{Batch['design_id']}/batches",
                            json={"parent_candidate_id": Ready[1]["id"], "instruction": "thinner band", "client_request_id": "r1"})
    assert R.status_code == 200 and R.json()["kind"] == "refine"
    await H.Idle()


async def test_a_slot_the_model_returns_no_image_for_is_unavailable_with_no_automatic_retry(H):
    """fal.ai's `no_media_generated` (the model finished without an image): only that slot is unavailable, at once,
    with the app's own sentence; the other three are ready and usable; nothing is re-requested by the app — the
    customer's "Generate another option" is the only second request."""
    H.Provider.Script(endpoints.ImageGenerate, "nomedia")
    Batch = await H.NewDesign("Band")
    assert len(H.Provider.Submissions) == N                                                   # one request per slot
    Failed = [C for C in Batch["candidates"] if C["status"] == "failed"]
    assert Batch["status"] == "partial" and len(Failed) == 1 and sum(C["status"] == "ready" for C in Batch["candidates"]) == 3
    assert Failed[0]["error_code"] == "no_media_generated" and Failed[0]["error"] == Generic and Failed[0]["retryable"]
    await H.Idle()
    assert len(H.Provider.Submissions) == N                                                   # still: no quiet retry
    assert H.Ctx.Db.One("SELECT attempts FROM candidates WHERE id = ?", (Failed[0]["id"],))["attempts"] == 1
    await _Usable(H, Batch)
    # The explicit replacement is one more request, with a new seed
    Before = {S[2] for S in H.Provider.Submissions}
    assert (await H.Client.post(f"/api/candidates/{Failed[0]['id']}/retry")).status_code == 200
    await H.Idle()
    After = (await H.Client.get(f"/api/batches/{Batch['id']}")).json()
    assert After["status"] == "complete" and len(H.Provider.SubmissionsFor(endpoints.ImageGenerate)) == N + 1
    assert next(C for C in After["candidates"] if C["id"] == Failed[0]["id"])["status"] == "ready"


def _Faster(H, TimeoutS=0.3, WindowS=5.0):
    """The shipped 90-second limit and 10-minute recovery window, at test speed."""
    from dataclasses import replace
    H.Ctx.Gen = replace(H.Ctx.Gen, Images=replace(H.Ctx.Gen.Images, RequestTimeoutS=TimeoutS, RecoveryWindowS=WindowS))


async def _Until(H, BatchId, Check, Tries=600):
    for _ in range(Tries):
        B = (await H.Client.get(f"/api/batches/{BatchId}")).json()
        if Check(B):
            return B
        await asyncio.sleep(0.01)
    raise AssertionError(f"batch {BatchId} never reached the expected state: {B}")


async def test_a_slot_past_the_limit_is_finished_in_the_background_without_a_second_request(tmp_path):
    """R-41: a slot still running at images.request_timeout_s (90 s) is "Still finishing", not failed. The customer goes
    on with the three ready options while the same request is asked again in the background (images.recovery_window_s,
    10 minutes) and its image is attached when it arrives: no second request, and the credit — taken at the start — is
    kept once all four are in. The Admin's journey shows the late one."""
    from p3.config import LoadGenerationConfig
    Shipped = LoadGenerationConfig().Images
    assert (Shipped.RequestTimeoutS, Shipped.RecoveryWindowS) == (90, 600)                  # the shipped limit and window
    Key = "late-admin-key"
    H = Harness(tmp_path, AdminKey=Key)
    try:
        _Faster(H)
        H.Provider.Script(endpoints.ImageGenerate, "hang")
        R = await H.Client.post("/api/designs", data={"prompt": "Band"})
        Batch = await _Until(H, R.json()["id"], lambda B: any(C["finishing"] for C in B["candidates"]))
        Late = next(C for C in Batch["candidates"] if C["finishing"])
        Ready = [C for C in Batch["candidates"] if C["status"] == "ready"]
        assert Batch["status"] == "partial" and Late["status"] == "generating" and not Late["retryable"] and len(Ready) == 3
        S = (await H.Client.get("/api/token-status")).json()
        assert (S["used"], S["reserved"]) == (0, 1)                                          # still held, not settled
        # The ready ones work meanwhile: choosing one and Customize (its movie: 1 credit)
        assert (await H.Client.put(f"/api/designs/{Batch['design_id']}/selection", json={"candidate_id": Ready[0]["id"]})).status_code == 200
        await H.Proceed(Batch["design_id"], Ready[0]["id"])
        Row = H.Ctx.Db.One("SELECT provider_request_id, recovering_since FROM candidates WHERE id = ?", (Late["id"],))
        assert Row["recovering_since"]
        H.Provider.Requests[Row["provider_request_id"]]["outcome"] = "ok"                     # fal finishes it, late
        await H.Idle()
        After = (await H.Client.get(f"/api/batches/{Batch['id']}")).json()
        assert After["status"] == "complete" and not any(C["finishing"] for C in After["candidates"])
        assert len(H.Provider.SubmissionsFor(endpoints.ImageGenerate)) == N                   # no second request
        S = (await H.Client.get("/api/token-status")).json()
        assert (S["used"], S["reserved"]) == (2, 0)                                          # the design and the movie
        D = (await H.Client.get(f"/api/admin/sessions/{Batch['design_id']}", headers={"Authorization": f"Bearer {Key}"})).json()
        Refs = {C["id"]: C["ring_id"] for B in D["design"]["batches"] for C in B["candidates"]}
        assert [E["data"]["options"] for E in D["timeline"] if E["kind"] == "option_recovered"] == [[Refs[Late["id"]]]]
    finally:
        await H.Close()


async def test_a_slot_still_missing_after_the_window_fails_and_retry_missing_fetches_it_for_free(H):
    """After the recovery window the slot fails (timeout) and the request's credit comes back. "Retry missing" is free:
    it asks fal for that request's image first — here finished meanwhile — and attaches it without a new request, and
    the request is not charged again."""
    _Faster(H, WindowS=0.3)
    H.Provider.Script(endpoints.ImageGenerate, "hang")
    Batch = await H.NewDesign("Band")
    Failed = [C for C in Batch["candidates"] if C["status"] == "failed"]
    assert Batch["status"] == "partial" and len(Failed) == 1 and Failed[0]["error_code"] == "timeout"
    assert Failed[0]["error"] == Generic and Failed[0]["retryable"] and not Failed[0]["finishing"]
    S = (await H.Client.get("/api/token-status")).json()
    assert (S["used"], S["reserved"]) == (0, 0)                                              # three of four: restored
    Row = H.Ctx.Db.One("SELECT provider_request_id, attempts, error FROM candidates WHERE id = ?", (Failed[0]["id"],))
    assert Row["provider_request_id"] in H.Provider.Requests and Row["attempts"] == 1 and "took too long" in Row["error"]
    H.Provider.Requests[Row["provider_request_id"]]["outcome"] = "ok"                         # it finished after all
    assert (await H.Client.post(f"/api/batches/{Batch['id']}/retry-failed")).status_code == 200
    await H.Idle()
    After = (await H.Client.get(f"/api/batches/{Batch['id']}")).json()
    assert After["status"] == "complete" and len(H.Provider.Submissions) == N                 # fetched, not bought again
    assert H.Ctx.Db.One("SELECT attempts FROM candidates WHERE id = ?", (Failed[0]["id"],))["attempts"] == 1
    S = (await H.Client.get("/api/token-status")).json()
    assert (S["used"], S["reserved"]) == (0, 0)                                              # never charged again


async def test_retry_missing_waits_for_a_request_fal_is_still_working_on(H):
    """"Retry missing" on a slot whose request is still running at fal: the slot is "Still finishing" again — the same
    request, waited for in the background — and nothing new is submitted."""
    _Faster(H, WindowS=0.3)
    H.Provider.Script(endpoints.ImageGenerate, "hang")
    Batch = await H.NewDesign("Band")
    Failed = next(C for C in Batch["candidates"] if C["status"] == "failed")
    _Faster(H, WindowS=5.0)
    R = await H.Client.post(f"/api/batches/{Batch['id']}/retry-failed")
    assert R.status_code == 200
    Again = next(C for C in R.json()["candidates"] if C["id"] == Failed["id"])
    assert Again["status"] == "generating" and Again["finishing"] and R.json()["status"] == "partial"
    Row = H.Ctx.Db.One("SELECT provider_request_id FROM candidates WHERE id = ?", (Failed["id"],))
    H.Provider.Requests[Row["provider_request_id"]]["outcome"] = "ok"
    await H.Idle()
    assert (await H.Client.get(f"/api/batches/{Batch['id']}")).json()["status"] == "complete"
    assert len(H.Provider.Submissions) == N


async def test_a_restart_during_the_recovery_resumes_it(tmp_path):
    """A slot that was still finishing when the server stopped goes back to its recovery at the start, for what is left
    of its window, and its request still holds the credit until it is settled."""
    from p3 import credits as Credits
    Provider = MockProvider(LatencyS=0.0, RenderVideo=False)
    Provider.Script(endpoints.ImageGenerate, "hang")
    First = Harness(tmp_path, Provider=Provider)
    _Faster(First, WindowS=60)
    R = await First.Client.post("/api/designs", data={"prompt": "Band"})
    await _Until(First, R.json()["id"], lambda B: any(C["finishing"] for C in B["candidates"]))
    await First.Close()                                   # simulated crash: the recovery dies with it
    for Req in Provider.Requests.values():
        Req["outcome"] = "ok"                             # fal finished it while we were down
    Second = Harness(tmp_path, Provider=Provider)
    try:
        assert Credits.Rebuild(Second.Ctx) == {First.Who.AccountId: 1}
        assert Second.Svc.Reconcile()["candidates"] == {"resumed": 1, "interrupted": 0}
        await Second.Idle()
        B = (await Second.Client.get(f"/api/batches/{R.json()['id']}", headers={"X-Access-Token": First.Token})).json()
        assert B["status"] == "complete" and len(Provider.Submissions) == N
    finally:
        await Second.Close()


async def test_no_provider_error_text_reaches_the_customer(tmp_path):
    """The provider's words (fal.ai's message, an HTTP status, an exception) stay with the slot for the Admin's
    session page and the log; every customer answer carries the app's own sentence instead."""
    from tests.conftest import Harness
    Key = "images-admin-key"
    H = Harness(tmp_path, AdminKey=Key)
    try:
        H.Provider.Script(endpoints.ImageGenerate, "fail", "nomedia")
        Batch = await H.NewDesign("Band")
        Failed = {C["error_code"]: C for C in Batch["candidates"] if C["status"] == "failed"}
        assert set(Failed) == {"provider_error", "no_media_generated"}
        Customer = [(await H.Client.get(f"/api/batches/{Batch['id']}")).text, (await H.Client.get(f"/api/designs/{Batch['design_id']}")).text]
        for Text in Customer:
            assert "Mock" not in Text and "no image" not in Text and Text.count(Generic) == 2
        assert all(C["error"] == Generic for C in Failed.values())
        # The Admin sees the provider's own words, the code and the request id
        Admin = {"Authorization": f"Bearer {Key}"}
        D = (await H.Client.get(f"/api/admin/sessions/{Batch['design_id']}", headers=Admin)).json()
        Seen = {C["error_code"]: C for B in D["design"]["batches"] for C in B["candidates"] if C["status"] == "failed"}
        assert Seen["provider_error"]["error"] == "Mock provider failure"
        assert Seen["no_media_generated"]["error"] == "Mock: the model returned no image."
        assert all(C["provider_request_id"] in H.Provider.Requests and C["attempts"] == 1 for C in Seen.values())
        assert len(H.Provider.Submissions) == N                                                 # and nothing was retried
    finally:
        await H.Close()


async def test_transient_poll_errors_retry_same_request(H):
    H.Provider.Script(endpoints.ImageGenerate, "status_transient:3")
    Batch = await H.NewDesign("Band")
    assert Batch["status"] == "complete" and len(H.Provider.Submissions) == N


async def test_transient_errors_are_bounded(H):
    H.Provider.Script(endpoints.ImageGenerate, "status_transient:50")
    Batch = await H.NewDesign("Band")
    Codes = [C["error_code"] for C in Batch["candidates"] if C["status"] == "failed"]
    assert Codes == ["provider_unreachable"] and len(H.Provider.Submissions) == N


async def test_late_results_do_not_touch_other_designs(H):
    H.Provider.LatencyS = 0.05
    A = await H.Client.post("/api/designs", data={"prompt": "Design A"})
    B = await H.Client.post("/api/designs", data={"prompt": "Design B"})
    await H.Idle()
    BatchA = (await H.Client.get(f"/api/batches/{A.json()['id']}")).json()
    BatchB = (await H.Client.get(f"/api/batches/{B.json()['id']}")).json()
    assert BatchA["design_id"] != BatchB["design_id"]
    assert not ({C["id"] for C in BatchA["candidates"]} & {C["id"] for C in BatchB["candidates"]})
    assert all(C["image_url"].startswith(f"/assets/designs/{BatchA['design_id']}/") for C in BatchA["candidates"])
    for D in (BatchA, BatchB):
        assert (await H.Design(D["design_id"]))["selected_candidate_id"] is None   # never auto-selected


async def test_restart_resumes_submitted_and_interrupts_unconfirmed(tmp_path):
    Provider = MockProvider(LatencyS=0.0, RenderVideo=False)
    Provider.Script(endpoints.ImageGenerate, *["hang"] * N)
    First = Harness(tmp_path, Provider=Provider)
    R = await First.Client.post("/api/designs", data={"prompt": "Band"})
    BatchId = R.json()["id"]
    for _ in range(200):
        Rows = First.Ctx.Db.All("SELECT status FROM candidates WHERE batch_id = ?", (BatchId,))
        if all(Row["status"] == "generating" for Row in Rows):
            break
        await asyncio.sleep(0.005)
    await First.Close()                                   # simulated crash: pollers die
    # One slot looks like it crashed between submit and persisting the request id.
    Victim = First.Ctx.Db.One("SELECT id FROM candidates WHERE batch_id = ? AND slot = ?", (BatchId, N - 1))
    First.Ctx.Db.Update("candidates", Victim["id"], status="pending", provider_request_id=None)
    for Req in Provider.Requests.values():
        Req["outcome"] = "ok"                             # remote work finished while we were down

    Second = Harness(tmp_path, Provider=Provider)
    Summary = Second.Svc.Reconcile()
    assert Summary["candidates"] == {"resumed": N - 1, "interrupted": 1}
    await Second.Idle()
    Rows = Second.Ctx.Db.All("SELECT status, error_code FROM candidates WHERE batch_id = ? ORDER BY slot", (BatchId,))
    assert [R_["status"] for R_ in Rows] == ["ready"] * (N - 1) + ["failed"]
    assert Rows[N - 1]["error_code"] == "interrupted"
    assert len(Provider.Submissions) == N                 # nothing was resubmitted automatically
    await Second.Close()


async def test_customize_waits_while_a_refinement_of_the_design_is_made(H):
    """R-18: no Customize — and no paid movie — for an option while a refinement of the same design is being made."""
    First = await H.NewDesign("Band")
    Sel = First["candidates"][0]["id"]
    await H.Client.put(f"/api/designs/{First['design_id']}/selection", json={"candidate_id": Sel})
    H.Provider.LatencyS = 3.0
    R = await H.Client.post(f"/api/designs/{First['design_id']}/batches", json={"parent_candidate_id": Sel, "instruction": "Thinner band"})
    assert R.status_code == 200
    C = await H.Client.post(f"/api/designs/{First['design_id']}/customize", json={"candidate_id": Sel})
    assert C.status_code == 409 and C.json()["error"]["code"] == "refinement_running"
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM movies")["n"] == 0                      # nothing started, nothing paid
    H.Provider.LatencyS = 0.0
    await H.Idle()
    await H.Proceed(First["design_id"], Sel)                                               # once it is in, as before


async def test_each_ready_option_says_what_customize_costs(H):
    """R-07: before the click, each ready option carries what opening Customize costs — its 360° movie, 1 credit — and
    nothing once it has a movie, which is reused."""
    Batch = await H.NewDesign("Band")
    assert [C["movie_credits"] for C in Batch["candidates"]] == [1] * N
    await H.Proceed(Batch["design_id"], Batch["candidates"][0]["id"])
    await H.Idle()
    After = (await H.Client.get(f"/api/batches/{Batch['id']}")).json()
    assert [C["movie_credits"] for C in After["candidates"]] == [0] + [1] * (N - 1)


async def test_a_request_for_more_variations_is_made_from_the_fixed_instruction(tmp_path):
    """R-50 follow-up: "make more variations" and the like, in English or Hebrew, become four variations of the selected
    design from one fixed instruction (fix 1's one-ring rule still follows it); the customer's words stay the batch's
    text, and the Admin's journey marks the request. Anything else is an ordinary refinement."""
    from p3 import morevariations as MoreVariations
    Key = "variations-admin-key"
    H = Harness(tmp_path, AdminKey=Key)
    try:
        First = await H.NewDesign("Signet ring with a hexagon face")
        Sel = First["candidates"][0]["id"]
        Asked = ["Make more variations please", "עוד אפשרויות בבקשה", "Make more variations with a thinner band"]
        for Text in Asked:
            R = await H.Client.post(f"/api/designs/{First['design_id']}/batches", json={"parent_candidate_id": Sel, "instruction": Text})
            assert R.status_code == 200 and R.json()["user_text"] == Text
            await H.Idle()
        Subs = H.Provider.SubmissionsFor(endpoints.ImageEdit)
        assert len(Subs) == 3 * N
        Fixed = MoreVariations.Instruction("ring") + "\n\n"
        assert all(S[1]["prompt"].startswith(Fixed) for S in Subs[:2 * N])
        assert all(S[1]["prompt"].startswith(Asked[2] + "\n\n") for S in Subs[2 * N:])        # an ordinary refinement
        D = (await H.Client.get(f"/api/admin/sessions/{First['design_id']}", headers={"Authorization": f"Bearer {Key}"})).json()
        Steps = [(E.get("text"), E.get("status")) for E in D["timeline"] if E["kind"] == "refine_requested"]
        assert Steps == [(Asked[0], "more variations"), (Asked[1], "more variations"), (Asked[2], None)]
    finally:
        await H.Close()


def test_asset_paths_cannot_escape_root(tmp_path):
    with pytest.raises(assets.AssetError):
        assets.Resolve(tmp_path, "../outside.png")
    import os
    Absolute = "C:/Windows/win.ini" if os.name == "nt" else "/etc/passwd"   # an absolute path on this OS
    with pytest.raises(assets.AssetError):
        assets.Resolve(tmp_path, Absolute)
