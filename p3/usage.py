"""Per-account activity read from P3's application data (pipeline3.db), for the Admin area.

Everything here is counted from the job tables that drive the product (designs, batches,
candidates, movies, meshes, bag lines) — nothing is estimated. Identity-side facts (allowance,
sign-ins, usage events with provider/endpoint/cost columns) come from the account provider and
are merged by p3/admin.py.

Provider of a job: the mock provider issues "mockreq_…" request ids; anything else was a live
(fal.ai) submission. A job that never reached the provider has no request id ("not submitted").
"""

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from p3.context import Context

MockPrefix = "mockreq_"


def ProviderOf(RequestId: str | None) -> str | None:
    if not RequestId:
        return None
    return "mock" if RequestId.startswith(MockPrefix) else "fal"


def BackfillUsageAnnotations(Ctx: Context) -> int:
    """Give usage events recorded before provider/endpoint were captured their provider and
    endpoint, from the job they refer to. Idempotent; returns the number of jobs annotated."""
    Refs = Ctx.Accounts.UnannotatedUsageRefs()
    if not Refs:
        return 0
    Jobs = Ctx.Db.All(
        "SELECT c.id, c.provider_request_id, b.endpoint FROM candidates c JOIN batches b ON b.id = c.batch_id "
        "UNION ALL SELECT id, provider_request_id, endpoint FROM movies "
        "UNION ALL SELECT id, provider_request_id, endpoint FROM meshes")
    N = 0
    for J in Jobs:
        Provider = ProviderOf(J["provider_request_id"])
        if J["id"] in Refs and Provider:
            Ctx.Accounts.AnnotateUsage(J["id"], Provider, J["endpoint"])
            N += 1
    return N


def _Day(Iso: str | None) -> str | None:
    return Iso[:10] if Iso else None


def _SizeText(L: dict) -> str:
    """'US 7' for a ring line, '20 mm' for a charm line."""
    from p3.products import SizeLabel
    return SizeLabel(L.get("product_type") or "ring", L.get("ring_size"), L.get("charm_size")) or "size not chosen"


def AccountActivity(Ctx: Context, AccountId: str, Days: int = 90, IncludeMock: bool = True) -> dict:
    Db, Url = Ctx.Db, Ctx.AssetUrl
    Designs = Db.All("SELECT * FROM designs WHERE owner_account_id = ? ORDER BY created_at DESC", (AccountId,))
    if not IncludeMock:
        from p3.sessions import MockDesignIds
        Mock = MockDesignIds(Ctx, AccountId)
        Designs = [D for D in Designs if D["id"] not in Mock]
    Ids = [D["id"] for D in Designs]
    Q = ",".join("?" * len(Ids)) or "NULL"
    Batches = Db.All(f"SELECT * FROM batches WHERE design_id IN ({Q}) ORDER BY created_at", Ids)
    Cands = Db.All(f"SELECT c.*, b.design_id, b.kind AS batch_kind FROM candidates c JOIN batches b ON b.id = c.batch_id "
                   f"WHERE b.design_id IN ({Q}) ORDER BY c.created_at, c.slot", Ids)
    CandIds = [C["id"] for C in Cands]
    QC = ",".join("?" * len(CandIds)) or "NULL"
    Movies = Db.All(f"SELECT m.*, b.design_id FROM movies m JOIN candidates c ON c.id = m.candidate_id "
                    f"JOIN batches b ON b.id = c.batch_id WHERE m.candidate_id IN ({QC}) ORDER BY m.created_at", CandIds)
    Meshes = Db.All(f"SELECT m.id, m.candidate_id, m.endpoint, m.status, m.provider_request_id, m.error_code, "
                    f"m.created_at, b.design_id FROM meshes m JOIN candidates c ON c.id = m.candidate_id "
                    f"JOIN batches b ON b.id = c.batch_id WHERE m.candidate_id IN ({QC}) ORDER BY m.created_at", CandIds)
    Bag = Db.All("SELECT * FROM bag_lines WHERE owner_account_id = ? ORDER BY created_at", (AccountId,))

    def Jobs(Rows) -> dict:
        Status = Counter(R["status"] for R in Rows)
        Providers = Counter(ProviderOf(R["provider_request_id"]) or "not_submitted" for R in Rows)
        return {"total": len(Rows), "by_status": dict(Status), "by_provider": dict(Providers)}

    Initial = [C for C in Cands if C["batch_kind"] == "initial"]
    Refined = [C for C in Cands if C["batch_kind"] == "refine"]
    Totals = {
        "designs": len(Designs),
        "design_batches": sum(1 for B in Batches if B["kind"] == "initial"),
        "refinements": sum(1 for B in Batches if B["kind"] == "refine"),
        "images": Jobs(Initial),
        "refinement_images": Jobs(Refined),
        "movies": Jobs(Movies),
        "meshes": Jobs(Meshes),
        "bag_lines": len(Bag),
        "designs_by_product": {P: sum(1 for D in Designs if (D.get("product_type") or "ring") == P) for P in ("ring", "charm")},
        "bag_units": sum(L["quantity"] for L in Bag),
    }
    AllJobs = Initial + Refined + Movies + Meshes
    Totals["jobs_succeeded"] = sum(1 for J in AllJobs if J["status"] == "ready")
    Totals["jobs_failed"] = sum(1 for J in AllJobs if J["status"] in ("failed", "interrupted"))
    Totals["jobs_in_progress"] = len(AllJobs) - Totals["jobs_succeeded"] - Totals["jobs_failed"]

    # Designs gallery: thumbnail = selected option, else the first ready image.
    ByDesign = defaultdict(list)
    for C in Cands:
        ByDesign[C["design_id"]].append(C)
    MoviesByCand = defaultdict(list)
    for M in Movies:
        MoviesByCand[M["candidate_id"]].append(M)
    BatchesByDesign = defaultdict(list)
    for B in Batches:
        BatchesByDesign[B["design_id"]].append(B)
    Gallery = []
    for D in Designs:
        Ready = [C for C in ByDesign[D["id"]] if C["status"] == "ready"]
        Thumb = next((C for C in Ready if C["id"] == D["selected_candidate_id"]), Ready[0] if Ready else None)
        Gallery.append({
            "id": D["id"], "title": D["title"], "prompt": D["prompt"], "created_at": D["created_at"],
            "product_type": D.get("product_type") or "ring",
            "updated_at": D["updated_at"], "thumbnail_url": Url(Thumb["asset_path"]) if Thumb else None,
            "batches": [{
                "id": B["id"], "kind": B["kind"], "user_text": B["user_text"], "created_at": B["created_at"],
                "candidates": [{
                    "id": C["id"], "slot": C["slot"], "status": C["status"], "image_url": Url(C["asset_path"]),
                    "selected": C["id"] == D["selected_candidate_id"],
                    # Admin only: why a slot failed, in the provider's own (capped) words, and its request
                    "error": C["error"], "error_code": C["error_code"], "provider_request_id": C["provider_request_id"],
                    "attempts": C["attempts"],
                    "movie_id": C["movie_id"],                  # the Admin's choice of the movie shown, when there are several
                    "movies": [{"id": M["id"], "status": M["status"], "url": Url(M["asset_path"]),
                                "created_at": M["created_at"], "chosen": M["id"] == C["movie_id"]} for M in MoviesByCand[C["id"]]],
                } for C in ByDesign[D["id"]] if C["batch_id"] == B["id"]],
            } for B in BatchesByDesign[D["id"]]],
            "in_bag": sum(1 for L in Bag if L["design_id"] == D["id"]),
        })

    Titles = {D["id"]: D["title"] for D in Designs}
    Timeline = (
        [{"at": D["created_at"], "kind": "design_created", "text": D["title"], "design_id": D["id"]} for D in Designs]
        + [{"at": B["created_at"], "kind": "refinement", "text": B["user_text"], "design_id": B["design_id"]}
           for B in Batches if B["kind"] == "refine"]
        + [{"at": M["created_at"], "kind": "movie", "status": M["status"], "text": Titles.get(M["design_id"], ""),
            "design_id": M["design_id"]} for M in Movies]
        + [{"at": M["created_at"], "kind": "mesh", "status": M["status"], "text": Titles.get(M["design_id"], ""),
            "design_id": M["design_id"]} for M in Meshes]
        + [{"at": L["created_at"], "kind": "bag_add", "text": f"{Titles.get(L['design_id'], '')} · {L['material_id']} · "
            f"{_SizeText(L)} × {L['quantity']}", "design_id": L["design_id"]} for L in Bag]
        # The prompt check (p3/promptcheck.py): every LLM request before the images, a stopped one with its reason
        + [{"at": P["created_at"], "kind": "prompt_check", "status": P["decision"],
            "text": P["text"] + (f" — {P['reason']}" if P["reason"] else ""), "design_id": P["design_id"] or P["source_design_id"]}
           for P in Db.All("SELECT * FROM prompt_checks WHERE owner_account_id = ? ORDER BY created_at", (AccountId,))
           if IncludeMock or P["ai_mode"] != "mock"]
    )

    # Daily counts over the last `Days` days (UTC dates), per kind.
    Since = (datetime.now(timezone.utc) - timedelta(days=Days - 1)).date().isoformat()
    Daily = defaultdict(Counter)
    for C in Cands:
        Daily[_Day(C["created_at"])]["refinement_images" if C["batch_kind"] == "refine" else "images"] += 1
    for M in Movies:
        Daily[_Day(M["created_at"])]["movies"] += 1
    for M in Meshes:
        Daily[_Day(M["created_at"])]["meshes"] += 1
    for D in Designs:
        Daily[_Day(D["created_at"])]["designs"] += 1
    Series = [{"day": Day, **Counts} for Day, Counts in sorted(Daily.items()) if Day and Day >= Since]

    Activity = [D["updated_at"] for D in Designs] + [J["updated_at"] for J in Cands + Movies if J.get("updated_at")]
    return {"totals": Totals, "designs": Gallery, "timeline": Timeline, "daily": Series, "daily_since": Since,
            "last_design_activity_at": max(Activity) if Activity else None}
