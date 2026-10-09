"""Sessions — one design journey per session, for the Admin and its statistics.

A session starts when the customer submits the first prompt of a New Design (designs.created_at;
a "New Design" click with no prompt is only counted as a top-of-funnel event). It is resumed when
the design is reopened, and it ends when the customer starts another New Design or after
IdleMinutes without activity.

Stage times come from data P3 already keeps (batches, candidates, customizations, movies) plus the
append-only session_events table for what those do not keep: material/size history together with
the fixed price shown, bag adds/removes, Bag viewed, Checkout clicked, design reopened, and admin
actions. Nothing is estimated.

Stages, in order: started → generated → selected → customize → bag → checkout_clicked → order.
Refinements are counted alongside ("Generated → Selected → Refined ×2 → Customize → Bag"); the 360°
movie and 3D are separate status tracks.
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from p3 import charmprices as CharmPrices
from p3 import ringids as RingIds
from p3.context import Context, HttpError
from p3.db import Dumps, Now
from p3.products import CharmSizeLabel
from p3.providers import endpoints

IdleMinutes = 30

# Events the customer site may send (POST /api/events). Everything else is recorded server-side.
ClientEvents = {"new_design_clicked", "design_opened", "bag_viewed", "checkout_clicked"}

StageOrder = ["started", "generated", "selected", "customize", "bag", "checkout_clicked", "order"]
StageLabels = {"started": "Started", "generated": "Generated", "selected": "Selected", "refined": "Refined",
               "customize": "Customize", "bag": "Bag", "checkout_clicked": "Checkout", "order": "Order"}


def Record(Ctx: Context, OwnerAccountId: str, Kind: str, DesignId: str | None = None, **Data) -> None:
    """Append one session event. Never raises into the customer flow."""
    try:
        Data.setdefault("ai_mode", "mock" if Ctx.Provider.Name == "mock" else "live")
        Ctx.Db.Execute("INSERT INTO session_events (design_id, owner_account_id, kind, data_json, created_at) "
                       "VALUES (?,?,?,?,?)", (DesignId, OwnerAccountId, Kind, Dumps(Data), Now()))
    except Exception:  # noqa: BLE001 — analytics must not break the product
        import logging
        logging.getLogger("p3.sessions").exception("Could not record session event %s", Kind)


def RecordClientEvent(Ctx: Context, OwnerAccountId: str, Kind: str, DesignId: str | None) -> dict:
    if Kind not in ClientEvents:
        raise HttpError(400, "unknown_event", "Unknown event.")
    if Kind in ("bag_viewed", "checkout_clicked"):
        # The bag can hold rings from several sessions: the event belongs to each of them.
        Designs = [R["design_id"] for R in Ctx.Db.All(
            "SELECT DISTINCT design_id FROM bag_lines WHERE owner_account_id = ?", (OwnerAccountId,))]
        for Did in Designs:
            Record(Ctx, OwnerAccountId, Kind, Did)
        return {"ok": True, "sessions": len(Designs)}
    if DesignId is not None:
        Own = Ctx.Db.One("SELECT 1 AS x FROM designs WHERE id = ? AND owner_account_id = ?", (DesignId, OwnerAccountId))
        Linked = Own or Ctx.Db.One("SELECT 1 AS x FROM gallery_uses WHERE design_id = ? AND owner_account_id = ? AND removed_at IS NULL",
                                   (DesignId, OwnerAccountId))
        if not Linked:
            raise HttpError(404, "design_not_found", "Design not found.")
    elif Kind != "new_design_clicked":
        raise HttpError(400, "design_required", "A design is required for this event.")
    Record(Ctx, OwnerAccountId, Kind, DesignId)
    return {"ok": True}


def BackfillBagEvents(Ctx: Context) -> int:
    """Bag lines can be removed later, so give existing ones a bag_added event once (idempotent)."""
    Rows = Ctx.Db.All("SELECT b.* FROM bag_lines b WHERE NOT EXISTS (SELECT 1 FROM session_events e "
                      "WHERE e.kind = 'bag_added' AND json_extract(e.data_json, '$.line_id') = b.id)")
    for B in Rows:
        Ctx.Db.Execute("INSERT INTO session_events (design_id, owner_account_id, kind, data_json, created_at) "
                       "VALUES (?,?,?,?,?)", (B["design_id"], B["owner_account_id"], "bag_added", Dumps({
                           "line_id": B["id"], "material_id": B["material_id"], "ring_size": B["ring_size"],
                           "quantity": B["quantity"], "unit_price": B["unit_price"], "currency": B["currency"],
                           "pricing_version": B["pricing_version"], "backfilled": True}), B["created_at"]))
    return len(Rows)


def BackfillDesignModes(Ctx: Context) -> int:
    """Designs created before ai_mode existed: 'fal' if any of their requests went to fal.ai, else 'mock'."""
    Rows = Ctx.Db.All("SELECT id FROM designs WHERE ai_mode IS NULL")
    for D in Rows:
        Live = Ctx.Db.One(
            "SELECT 1 AS x FROM candidates c JOIN batches b ON b.id = c.batch_id WHERE b.design_id = ? "
            "AND c.provider_request_id IS NOT NULL AND c.provider_request_id NOT LIKE 'mockreq_%' "
            "UNION SELECT 1 FROM movies m JOIN candidates c ON c.id = m.candidate_id JOIN batches b ON b.id = c.batch_id "
            "WHERE b.design_id = ? AND m.provider_request_id IS NOT NULL AND m.provider_request_id NOT LIKE 'mockreq_%'",
            (D["id"], D["id"]))
        Ctx.Db.Execute("UPDATE designs SET ai_mode = ? WHERE id = ?", ("fal" if Live else "mock", D["id"]))
    return len(Rows)


def MockDesignIds(Ctx: Context, OwnerAccountId: str | None = None) -> set[str]:
    """Sessions that are mock-only: created in mock mode and never sent a request to a live provider."""
    Where, Params = ("AND d.owner_account_id = ?", (OwnerAccountId,)) if OwnerAccountId else ("", ())
    Rows = Ctx.Db.All(f"SELECT d.id FROM designs d WHERE COALESCE(d.ai_mode, 'mock') = 'mock' {Where} AND NOT EXISTS ("
                      "SELECT 1 FROM candidates c JOIN batches b ON b.id = c.batch_id WHERE b.design_id = d.id "
                      "AND c.provider_request_id IS NOT NULL AND c.provider_request_id NOT LIKE 'mockreq_%') AND NOT EXISTS ("
                      "SELECT 1 FROM movies m JOIN candidates c ON c.id = m.candidate_id JOIN batches b ON b.id = c.batch_id "
                      "WHERE b.design_id = d.id AND m.provider_request_id IS NOT NULL AND m.provider_request_id NOT LIKE 'mockreq_%')",
                      Params)
    return {R["id"] for R in Rows}


def _Parse(Iso: str | None) -> datetime | None:
    return datetime.fromisoformat(Iso) if Iso else None


def _ProductionState(Status: str, Integrity: str | None, Accepted=False) -> str:
    from p3.production3d import ProductionState          # late import: production3d imports this module
    return ProductionState(Status, Integrity, bool(Accepted))


def _Min(*Values):
    V = [X for X in Values if X]
    return min(V) if V else None


def _Max(*Values):
    V = [X for X in Values if X]
    return max(V) if V else None


def Summaries(Ctx: Context, DesignIds: list[str] | None = None, OwnerAccountId: str | None = None,
              IncludeMock: bool = True, SessionIds: list[str] | None = None) -> list[dict]:
    """Session summaries (newest first). A session is one customer's journey: their own design, or
    their use of a shared XJet master design from the gallery (session id = the gallery_uses id).
    DesignIds → the owners' sessions of those designs · SessionIds → exactly those sessions (design
    ids or use ids) · OwnerAccountId → one customer's journeys. Admin lists pass IncludeMock=False."""
    Db, Url = Ctx.Db, Ctx.AssetUrl
    UseIds = None
    if SessionIds is not None:
        UseIds = [X for X in SessionIds if X.startswith("use_")]
        DesignIds = [X for X in SessionIds if not X.startswith("use_")]
    Where, Params = [], []
    if DesignIds is not None:
        Where.append(f"id IN ({','.join('?' * len(DesignIds))})" if DesignIds else "0")
        Params += DesignIds
    if OwnerAccountId:
        Where.append("owner_account_id = ?")
        Params.append(OwnerAccountId)
    Designs = Db.All("SELECT * FROM designs" + (" WHERE " + " AND ".join(Where) if Where else "")
                     + " ORDER BY created_at DESC", Params)
    # Gallery uses: a customer on a shared XJet master design is a journey of their own.
    Uses = []
    if DesignIds is None or UseIds:
        UWhere, UParams = [], []
        if UseIds is not None:
            UWhere.append(f"u.id IN ({','.join('?' * len(UseIds))})" if UseIds else "0")
            UParams += UseIds
        if OwnerAccountId:
            UWhere.append("u.owner_account_id = ?")
            UParams.append(OwnerAccountId)
        Uses = Db.All("SELECT u.* FROM gallery_uses u" + (" WHERE " + " AND ".join(UWhere) if UWhere else "")
                      + " ORDER BY u.last_active_at DESC", UParams)
    Masters = {}
    if Uses:
        MIds = list({U["design_id"] for U in Uses})
        Masters = {D["id"]: D for D in Db.All(f"SELECT * FROM designs WHERE id IN ({','.join('?' * len(MIds))})", MIds)}
        Uses = [U for U in Uses if U["design_id"] in Masters]
    Mock = MockDesignIds(Ctx)
    if not IncludeMock:
        Designs = [D for D in Designs if D["id"] not in Mock]
        Uses = [U for U in Uses if U["design_id"] not in Mock]
    Journeys = [(D, None) for D in Designs] + [(Masters[U["design_id"]], U) for U in Uses]
    if not Journeys:
        return []
    Ids = list({D["id"] for D, _ in Journeys})
    Q = ",".join("?" * len(Ids))
    InGallery = {R["design_id"] for R in Db.All(f"SELECT design_id FROM gallery_items WHERE design_id IN ({Q})", Ids)}
    Batches = Db.All(f"SELECT id, design_id, kind, user_text, created_at FROM batches WHERE design_id IN ({Q})", Ids)
    Cands = Db.All(f"SELECT c.id, c.batch_id, c.slot, c.status, c.asset_path, c.updated_at, b.design_id, b.kind "
                   f"FROM candidates c JOIN batches b ON b.id = c.batch_id WHERE b.design_id IN ({Q})", Ids)
    Custs = Db.All(f"SELECT * FROM customizations WHERE design_id IN ({Q}) ORDER BY updated_at", Ids)
    Movies = Db.All(f"SELECT m.id, m.status, m.created_at, m.updated_at, m.requested_by, b.design_id FROM movies m "
                    f"JOIN candidates c ON c.id = m.candidate_id JOIN batches b ON b.id = c.batch_id "
                    f"WHERE b.design_id IN ({Q})", Ids)
    Lines = Db.All(f"SELECT * FROM bag_lines WHERE design_id IN ({Q}) ORDER BY created_at", Ids)
    Events = Db.All(f"SELECT * FROM session_events WHERE design_id IN ({Q}) ORDER BY created_at", Ids)
    ThreeD = Db.All(f"SELECT s.*, r.integrity FROM session_3d s LEFT JOIN raw_geometry r ON r.mesh_id = s.mesh_id "
                    f"WHERE s.design_id IN ({Q}) ORDER BY s.created_at", Ids)
    Owners = list({D["owner_account_id"] for D, _ in Journeys} | {U["owner_account_id"] for U in Uses})
    QO = ",".join("?" * len(Owners))
    # A newer journey by the same customer ends the previous session ("new_design").
    Starts = defaultdict(list)
    for R in Db.All(f"SELECT owner_account_id, created_at AS at FROM designs WHERE owner_account_id IN ({QO}) "
                    f"UNION ALL SELECT owner_account_id, started_at AS at FROM gallery_uses WHERE owner_account_id IN ({QO})",
                    Owners + Owners):
        Starts[R["owner_account_id"]].append(R["at"])

    def Group(Rows, Key="design_id"):
        G = defaultdict(list)
        for R in Rows:
            G[R[Key]].append(R)
        return G

    B, C, Cu, M, L, E, T3 = (Group(X) for X in (Batches, Cands, Custs, Movies, Lines, Events, ThreeD))
    Names, Status = {}, {}
    for Oid in Owners:
        try:
            U_ = Ctx.Accounts.AdminGet(Oid)
            Names[Oid], Status[Oid] = (U_["name"], U_["email"]), U_["status"]
        except Exception:  # noqa: BLE001 — an unknown owner must not hide the session
            Names[Oid], Status[Oid] = ("", ""), "unknown"
    # Journeys that started from a gallery image carry that image's ring ID; every option's ring ID is
    # listed too, so the Admin can search a session by R-1013-B.
    SourceRefs = RingIds.CandidateRefs(Db, list({D["source_design_id"] for D, _ in Journeys if D.get("source_design_id")}
                                                 | {U["design_id"] for U in Uses}))
    OptionRefs = RingIds.CandidateRefs(Db, Ids)
    SourceOwners = {R["id"]: R["owner_account_id"] for R in Db.All(
        f"SELECT id, owner_account_id FROM designs WHERE id IN ({','.join('?' * len(SourceIds))})", SourceIds)} if (
        SourceIds := list({D["source_design_id"] for D, _ in Journeys if D.get("source_design_id")})) else {}
    OrderRefsByOwner = defaultdict(set)              # (design, owner) → order references
    for R in Db.All(f"SELECT l.design_id, o.owner_account_id, o.order_no FROM order_lines l JOIN orders o ON o.id = l.order_id "
                    f"WHERE l.design_id IN ({Q})", Ids):
        OrderRefsByOwner[(R["design_id"], R["owner_account_id"])].add(f"ORD-{R['order_no']}")
    NowDt = datetime.now(timezone.utc)
    Out = []
    for D, U in Journeys:
        Did = D["id"]
        Owner = U["owner_account_id"] if U else D["owner_account_id"]
        Ready = [X for X in C[Did] if X["status"] == "ready"]
        # A design's first generation is its initial batch — or, for a design that began as a refinement of
        # another (a fork or a split-off refinement), its first refinement batch
        FirstKind = min(B[Did], key=lambda X: X["created_at"])["kind"] if B[Did] else "initial"
        InitialReady = [X for X in Ready if X["kind"] == FirstKind]
        RefineBatches = [] if U else [X for X in B[Did] if X["kind"] == "refine"]
        # Only this customer's steps count on a shared design (their choices, bag lines, events).
        Ev = [X for X in E[Did] if X["owner_account_id"] == Owner]
        Cus = [X for X in Cu[Did] if X["owner_account_id"] == Owner]
        Ln = [X for X in L[Did] if X["owner_account_id"] == Owner]
        EvAt = lambda K: _Min(*[X["created_at"] for X in Ev if X["kind"] == K])
        Customize = _Min(EvAt("customize_opened"), *[X["created_at"] for X in Cus])
        Times = {
            "started": U["started_at"] if U else D["created_at"],
            "generated": (U["started_at"] if InitialReady else None) if U else _Min(*[X["updated_at"] for X in InitialReady]),
            # A gallery pick is selected from the start; otherwise the first option chosen (or Customize, which implies one)
            "selected": U["started_at"] if U else _Min(EvAt("option_selected"), Customize),
            "customize": Customize,
            "bag": _Min(EvAt("bag_added"), *[X["created_at"] for X in Ln]),
            "checkout_clicked": EvAt("checkout_clicked"),
            "order": EvAt("order_placed"),
        }
        Reached = [S for S in StageOrder if Times[S]]
        Stage = Reached[-1] if Reached else "started"
        OrderRefs = list(dict.fromkeys(json.loads(X["data_json"] or "{}").get("order_ref") for X in Ev if X["kind"] == "order_placed"))
        OwnMovies = [X for X in M[Did] if (X["requested_by"] or D["owner_account_id"]) == Owner]
        LastActivity = _Max(U["last_active_at"] if U else D["updated_at"],
                            *([] if U else [X["updated_at"] for X in C[Did]]), *[X["updated_at"] for X in Cus],
                            *[X["updated_at"] for X in OwnMovies],
                            *[X["created_at"] for X in Ev if not X["kind"].startswith("admin_")])
        Later = [S for S in Starts[Owner] if S > (LastActivity or Times["started"])]
        Idle = (NowDt - _Parse(LastActivity)) > timedelta(minutes=IdleMinutes) if LastActivity else True
        if Later:
            State, EndReason = "ended", "new_design"
        elif Idle:
            State, EndReason = "ended", "idle"
        else:
            State, EndReason = "active", None
        Selected = (U["selected_candidate_id"] or U["source_candidate_id"]) if U else D["selected_candidate_id"]
        # Customer choices: latest bag line, else the latest customization (selected option first).
        Cust = next((X for X in reversed(Cus) if X["candidate_id"] == Selected), None) or (Cus[-1] if Cus else None)
        Line = Ln[-1] if Ln else None
        Material = (Line or Cust or {}).get("material_id")
        # Customize opens on the default material and US 10; only a bag line or an explicit change is a
        # choice. Customizations from before event tracking had no default size, so a size there was chosen.
        Changed = [json.loads(X["data_json"] or "{}") for X in Ev if X["kind"] == "customization_changed"]
        MaterialChosen = bool(Line) or any("material_id" in X for X in Changed)
        Tracked = {json.loads(X["data_json"] or "{}").get("candidate_id") for X in Ev if X["kind"] == "customize_opened"}
        SizeChosen = bool(Line) or any("ring_size" in X and X["ring_size"] is not None for X in Changed) or bool(
            Cust and Cust["ring_size"] is not None and Cust["candidate_id"] not in Tracked)
        Size = (Line or Cust or {}).get("ring_size")
        Product = D.get("product_type") or "ring"
        CharmSize = (Line or Cust or {}).get("charm_size")
        # A charm opens without a size, so any size on it was chosen by the customer
        CharmChosen = bool(Line) or any(X.get("charm_size") is not None for X in Changed) or bool(Cust and Cust.get("charm_size") is not None)
        Fixed = FixedPrice(Ctx, Line, Ev, Material, Product, CharmSize)
        Thumb = next((X for X in Ready if X["id"] == Selected), Ready[0] if Ready else None)
        Last3D = T3[Did][-1] if T3[Did] else None
        Failed = (not U) and bool(C[Did]) and not InitialReady and all(X["status"] == "failed" for X in C[Did] if X["kind"] == FirstKind)
        Path = [StageLabels["generated"]] if Times["generated"] else []
        if Times["selected"]:
            Path.append(StageLabels["selected"])
        if RefineBatches:
            Path.append(StageLabels["refined"] + (f" ×{len(RefineBatches)}" if len(RefineBatches) > 1 else ""))
        Path += [StageLabels[S] for S in ("customize", "bag", "checkout_clicked", "order") if Times[S]]
        if Failed:
            Path.append("Generation failed")
        if State == "ended" and not Times["bag"] and not Times["order"]:
            Path.append("stopped")
        Name, Email = Names.get(Owner, ("", ""))
        SourceCandidate = U["source_candidate_id"] if U else D.get("source_candidate_id")
        Out.append({
            "session_id": U["id"] if U else Did, "design_id": Did, "ring_id": RingIds.Ref(D),
            "product_type": D.get("product_type") or "ring",
            "selected_candidate_id": Selected, "selected_ring_id": OptionRefs.get(Selected) if Selected else None,
            "option_ring_ids": sorted(OptionRefs[X["id"]] for X in C[Did] if X["id"] in OptionRefs),
            "title": D["title"], "prompt": D["prompt"], "mock": Did in Mock,
            # A journey from a gallery image (a customer's use, or their fork of a master); the owner's own
            # refinement of their own master is a new design of their own, not a gallery journey
            "origin": "gallery" if (U or (D.get("source_design_id") and SourceOwners.get(D["source_design_id"]) != Owner)) else "prompt",
            # A design copied from the gallery before shared master designs existed (kept as history, labelled)
            "legacy_copy": bool(not U and D.get("source_design_id") and not Db.One(
                "SELECT 1 AS x FROM batches WHERE design_id = ? AND kind = 'refine'", (Did,))),
            "source_ring_id": SourceRefs.get(SourceCandidate) if SourceCandidate else None,
            "shared": bool(U), "use_id": U["id"] if U else None,
            "in_gallery": Did in InGallery,              # a master design shown in the Inspiration Gallery
            "removed": bool(U.get("removed_at")) if U else bool(D.get("removed_at")),
            "account_id": Owner, "customer_name": Name, "customer_email": Email,
            "thumbnail_url": Url(Thumb["asset_path"]) if Thumb else None,
            "started_at": Times["started"], "last_activity_at": LastActivity, "state": State, "end_reason": EndReason,
            "stage_reached": Stage, "stage_times": Times, "path": " → ".join(Path) if Path else "Started",
            "generations": 0 if U else sum(1 for X in B[Did] if X["kind"] == "initial"), "refinements": len(RefineBatches),
            "images_ready": len(Ready), "images_failed": sum(1 for X in C[Did] if X["status"] == "failed"),
            "movie_status": (sorted(M[Did], key=lambda X: X["created_at"])[-1]["status"] if M[Did] else None),
            "ring_size": Size, "ring_size_chosen": SizeChosen,
            "material_id": Material, "material_chosen": MaterialChosen,
            "material_label": (Ctx.Catalog.Get(Material).Label if Material and Ctx.Catalog.Get(Material) else None)
            if Product != "charm" else (CharmPrices.Label(Ctx.Catalog, Material) if Material else None),
            "add_to_bag": bool(Times["bag"]), "checkout_clicked": bool(Times["checkout_clicked"]),
            "ordered": bool(Times["order"]) or bool(OrderRefsByOwner.get((Did, Owner))),
            "order_refs": sorted({R for R in OrderRefs if R} | OrderRefsByOwner.get((Did, Owner), set())),
            "fixed_price": Fixed,
            "three_d_status": Last3D["status"] if Last3D else None, "three_d_id": Last3D["id"] if Last3D else None,
            # Production readiness of the latest result (processing · complete · review_required · failed · cancelled)
            "three_d_state": _ProductionState(Last3D["status"], Last3D["integrity"], Last3D["accepted_at"]) if Last3D else None,
            "three_d_review": any(_ProductionState(X["status"], X["integrity"], X["accepted_at"]) == "review_required" for X in T3[Did]),
            "user_status": Status.get(Owner, "unknown"),
            "has_image": bool(Ready), "has_movie": any(X["status"] == "ready" for X in M[Did]),
            "has_3d": any(X["status"] in ("measured", "needs_review") for X in T3[Did]),
        })
        if Product == "charm":                       # a charm's size in mm (rings keep exactly the fields of before)
            Out[-1].update({"ring_size": None, "ring_size_chosen": False, "charm_size": CharmSize, "charm_size_chosen": CharmChosen})
    Out.sort(key=lambda X: X["started_at"] or "", reverse=True)
    return Out

def FixedPrice(Ctx: Context, Line: dict | None, Events: list[dict], MaterialId: str | None, Product: str = "ring",
               CharmSize=None) -> dict | None:
    """The customer-facing fixed price: the bag snapshot if added to bag, else the price shown with
    the last material choice, else today's quote for the material. Never changed by 3D numbers."""
    if Line:
        return {"unit_price": Line["unit_price"], "currency": Line["currency"],
                "pricing_version": Line["pricing_version"], "source": "bag_snapshot", "at": Line["created_at"]}
    for X in reversed(Events):
        if X["kind"] in ("customization_changed", "customize_opened"):
            Data = json.loads(X["data_json"] or "{}")
            if "unit_price" in Data:
                return {"unit_price": Data["unit_price"], "currency": Data.get("currency"),
                        "pricing_version": Data.get("pricing_version"), "source": "shown_at_customize",
                        "at": X["created_at"]}
    if MaterialId:
        Q = CharmPrices.QuoteFor(Ctx, Product, MaterialId, CharmSize)
        return {"unit_price": Q.unit_price, "currency": Q.currency, "pricing_version": Q.pricing_version,
                "source": "current_quote", "at": None}
    return None


def QuoteSnapshot(Ctx: Context, MaterialId: str, Product: str = "ring", CharmSize=None) -> dict:
    Q = CharmPrices.QuoteFor(Ctx, Product, MaterialId, CharmSize)
    return {"unit_price": Q.unit_price, "currency": Q.currency, "pricing_version": Q.pricing_version,
            "pricing_status": Q.pricing_status}


def Timeline(Ctx: Context, DesignId: str, Owner: str | None = None, Use: dict | None = None) -> list[dict]:
    """Every recorded step of one journey, oldest first: a design's own steps for its owner, or one
    customer's steps on a shared master design (their events, choices and movies; the 3D the admin ran)."""
    Db = Ctx.Db
    D = Db.One("SELECT * FROM designs WHERE id = ?", (DesignId,))
    Owner = Owner or D["owner_account_id"]
    Batches = [] if Use else Db.All("SELECT * FROM batches WHERE design_id = ? ORDER BY created_at", (DesignId,))
    Refs = RingIds.CandidateRefs(Db, [DesignId]) if Batches else {}
    First = next((B for B in Batches if B["kind"] == "initial"), None)
    Items = [] if Use else [{"at": D["created_at"], "kind": "started", "text": D["prompt"], **_Reference(Ctx, D, First, Refs)}]
    for Bt in Batches:
        Cs = Db.All("SELECT status, updated_at FROM candidates WHERE batch_id = ?", (Bt["id"],))
        Ready = [X for X in Cs if X["status"] == "ready"]
        Items.append({"at": Bt["created_at"], "kind": "refine_requested" if Bt["kind"] == "refine" else "generate_requested",
                      "text": Bt["user_text"], **_Reference(Ctx, D, Bt, Refs)})
        if Ready or all(X["status"] == "failed" for X in Cs):
            Items.append({"at": _Max(*[X["updated_at"] for X in Cs]),
                          "kind": "refined" if Bt["kind"] == "refine" else "generated",
                          "text": f"{len(Ready)} of {len(Cs)} images ready"})
    # The prompt check (p3/promptcheck.py): what let each batch through, and this customer's requests it stopped
    for Pc in Db.All("SELECT * FROM prompt_checks WHERE (design_id = ? OR (source_design_id = ? AND decision = 'rejected')) "
                     "AND owner_account_id = ? ORDER BY created_at", (DesignId, DesignId, Owner)):
        Items.append({"at": Pc["created_at"], "kind": "prompt_check", "status": Pc["decision"], "text": CheckText(Pc)})
    for Mv in Db.All("SELECT m.* FROM movies m JOIN candidates c ON c.id = m.candidate_id JOIN batches b ON b.id = c.batch_id "
                     "WHERE b.design_id = ? ORDER BY m.created_at", (DesignId,)):
        if (Mv["requested_by"] or D["owner_account_id"]) == Owner:
            Items.append({"at": Mv["updated_at"], "kind": "movie", "status": Mv["status"], "text": ""})
    for Ev in Db.All("SELECT * FROM session_events WHERE design_id = ? AND (owner_account_id = ? OR kind LIKE 'admin_3d%') "
                     "ORDER BY created_at", (DesignId, Owner)):
        Items.append({"at": Ev["created_at"], "kind": Ev["kind"], "data": json.loads(Ev["data_json"] or "{}")})
    # Before event tracking: each customization row is one option opened in Customize (its first time).
    Tracked = {(X.get("data") or {}).get("candidate_id") for X in Items if X["kind"] == "customize_opened"}
    for Cu in Db.All("SELECT * FROM customizations WHERE design_id = ? AND owner_account_id = ? ORDER BY created_at",
                     (DesignId, Owner)):
        if Cu["candidate_id"] not in Tracked:
            Items.append({"at": Cu["created_at"], "kind": "customize_opened",
                          "data": {"backfilled": True, "candidate_id": Cu["candidate_id"]}})
    Items.sort(key=lambda X: X["at"] or "")
    return Items


def CheckText(Pc: dict) -> str:
    """One prompt check in words: accepted (and how long it took), stopped (the request and the reason), or no decision."""
    Took = f" · {Pc['seconds']:g} s" if Pc.get("seconds") is not None else ""
    if Pc["decision"] == "rejected":
        return f"Stopped “{Pc['text']}” — {Pc['reason'] or ''}{Took}"
    if Pc["decision"] == "undecided":
        return f"No decision, the request went ahead — {(Pc['error'] or '')[:120]}{Took}"
    return f"Accepted{Took}"


def _Reference(Ctx: Context, Design: dict, Batch: dict | None, Refs: dict) -> dict:
    """The image the customer gave for a step: their uploaded or pasted reference (a new design), or the
    option they refined (a refinement) — with a download name that says what it is. {} when there is none."""
    if not Batch or not Batch["reference_asset"]:
        return {}
    from p3.production3d import SlugPart
    Ring = RingIds.Ref(Design)
    Slug = SlugPart(Design["title"])
    Url = Ctx.AssetUrl(Batch["reference_asset"])
    if Batch["kind"] == "initial":
        return {"reference_url": Url, "reference_kind": "upload", "reference_label": "Customer's reference image",
                "download_name": f"{Slug}_{Ring}_reference.png"}
    Option = Refs.get(Batch["parent_candidate_id"])
    return {"reference_url": Url, "reference_kind": "option", "reference_label": f"Refined from {Option or 'the selected option'}",
            "download_name": f"{Slug}_{Option or Ring}.png"}


def _Seconds(A: str | None, B: str | None) -> float | None:
    if not A or not B:
        return None
    return max(0.0, (_Parse(B) - _Parse(A)).total_seconds())


def Pipeline(Ctx: Context, DesignId: str, Usage: list[dict], Prices, Owner: str | None = None) -> dict:
    """Per-step duration and estimated AI cost for one session: each design / refinement batch, each
    movie and each 3D job. Cost = every provider submission recorded for the job (retries included)
    × the list price for that request's parameters (Prices.Estimate). Owner = whose journey: on a
    shared master design a customer's pipeline holds only the movies they asked for (and the 3D)."""
    Db = Ctx.Db
    Design = Db.One("SELECT owner_account_id, product_type FROM designs WHERE id = ?", (DesignId,))
    DesignOwner, Charm = Design["owner_account_id"], Design["product_type"] == "charm"
    Owner = Owner or DesignOwner
    Shared = Owner != DesignOwner
    ByRef = defaultdict(list)
    for U in Usage:
        if U["kind"] != "generation":
            ByRef[U["ref_id"]].append(U)
    Steps = []

    def Cost(RefIds, Endpoint, Params):
        Total, Basis, Providers, Requests = 0.0, set(), set(), 0
        Unknown = False
        for R in RefIds:
            for U in ByRef.get(R, []):
                E = Prices.Estimate(U["endpoint"] or Endpoint, U["provider"], Params)
                Requests += 1
                Providers.add(U["provider"] or "unknown")
                Basis.add(E["basis"])
                if E["cost"] is None:
                    Unknown = True
                else:
                    Total += E["cost"]
        return {"cost": None if Unknown and not Total else round(Total, 4), "cost_partial": Unknown and bool(Total),
                "requests": Requests, "providers": sorted(Providers), "basis": sorted(Basis)}

    def CheckStep(Pc: dict) -> dict:
        """The prompt check before a batch (p3/promptcheck.py): one LLM request, accepted / stopped / no decision."""
        V = Ctx.Models.Get(Pc["config_version"])
        return {"kind": "prompt_check", "label": "Prompt check", "endpoint": endpoints.Llm, "text": Pc["text"],
                "started_at": Pc["created_at"], "finished_at": Pc["created_at"], "duration_s": Pc["seconds"],
                "status": Pc["decision"], "detail": {"accepted": "Accepted", "rejected": "Stopped — nothing generated",
                                                     "undecided": "No decision — the request went ahead"}.get(Pc["decision"], ""),
                **Cost([Pc["id"]], endpoints.Llm, V.Params if V else {})}

    Checks = Db.All("SELECT * FROM prompt_checks WHERE (design_id = ? OR (source_design_id = ? AND decision = 'rejected')) "
                    "AND owner_account_id = ? ORDER BY created_at", (DesignId, DesignId, Owner))
    for Pc in Checks:
        if Pc["decision"] == "rejected":                # a stopped refinement: its check is all it cost
            Steps.append(CheckStep(Pc))
    ByBatch = {Pc["batch_id"]: Pc for Pc in Checks if Pc["batch_id"]}
    for B in ([] if Shared else Db.All("SELECT * FROM batches WHERE design_id = ? ORDER BY created_at", (DesignId,))):
        if B["id"] in ByBatch:
            Steps.append(CheckStep(ByBatch[B["id"]]))
        Cs = Db.All("SELECT id, status, updated_at FROM candidates WHERE batch_id = ?", (B["id"],))
        Done = all(C["status"] in ("ready", "failed") for C in Cs)
        Params = Ctx.Models.Resolve(B["config_version"], B["endpoint"]).Params
        Steps.append({
            "kind": "refinement" if B["kind"] == "refine" else "design", "label": "Refinement" if B["kind"] == "refine" else "Design images",
            "endpoint": B["endpoint"], "text": B["user_text"], "started_at": B["created_at"],
            "finished_at": _Max(*[C["updated_at"] for C in Cs]) if Done else None,
            "duration_s": _Seconds(B["created_at"], _Max(*[C["updated_at"] for C in Cs])) if Done else None,
            "status": "running" if not Done else ("ready" if any(C["status"] == "ready" for C in Cs) else "failed"),
            "detail": f"{sum(1 for C in Cs if C['status'] == 'ready')} of {len(Cs)} images ready",
            **Cost([C["id"] for C in Cs], B["endpoint"], Params),
        })
    for M in Db.All("SELECT m.* FROM movies m JOIN candidates c ON c.id = m.candidate_id JOIN batches b ON b.id = c.batch_id "
                    "WHERE b.design_id = ? ORDER BY m.created_at", (DesignId,)):
        if (M["requested_by"] or DesignOwner) != Owner:      # another customer's movie on a shared design
            continue
        Done = M["status"] in ("ready", "failed", "interrupted")
        Params = Ctx.Models.Resolve(M["config_version"], M["endpoint"]).Params
        Steps.append({
            "kind": "movie", "label": "360° movie", "endpoint": M["endpoint"], "text": "", "started_at": M["created_at"],
            "finished_at": M["updated_at"] if Done else None, "duration_s": _Seconds(M["created_at"], M["updated_at"]) if Done else None,
            "status": M["status"], "detail": f"{Params.get('duration', 5)} s · {Params.get('resolution', '480P')}",
            **Cost([M["id"]], M["endpoint"], Params),
        })
    SeenMeshes = set()
    for T in Db.All("SELECT s.*, m.endpoint, m.settings_json, m.status AS mesh_status, m.created_at AS mesh_created, "
                    "m.updated_at AS mesh_updated FROM session_3d s LEFT JOIN meshes m ON m.id = s.mesh_id "
                    "WHERE s.design_id = ? ORDER BY s.created_at", (DesignId,)):
        Done = T["status"] in ("measured", "needs_review", "failed")
        Params = json.loads(T["settings_json"] or "{}")
        Reused = T["mesh_id"] in SeenMeshes          # an earlier 3D request already paid for this Hi3D model
        SeenMeshes.add(T["mesh_id"])
        Steps.append({
            "kind": "3d", "label": "3D (Hi3D + measurement)", "endpoint": T["endpoint"],
            "text": CharmSizeLabel(T["production_size"]) if Charm else f"US {T['production_size']:g}",   # a charm: "20 mm"
            "started_at": T["created_at"], "finished_at": T["updated_at"] if Done else None,
            "duration_s": _Seconds(T["created_at"], T["updated_at"]) if Done else None, "status": T["status"],
            "detail": "reused existing Hi3D model — only re-measured" if Reused else f"{Params.get('resolution', '2048quality')}",
            **(Cost([], T["endpoint"], Params) if Reused else Cost([T["mesh_id"]], T["endpoint"], Params)),
        })
    Known = [S["cost"] for S in Steps if S["cost"] is not None]
    return {"steps": Steps, "total_cost": round(sum(Known), 4) if Known else 0.0,
            "total_duration_s": sum(S["duration_s"] or 0 for S in Steps),
            "has_unpriced": any(S["cost"] is None for S in Steps if S["requests"])}
