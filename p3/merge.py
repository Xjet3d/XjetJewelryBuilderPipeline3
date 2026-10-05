"""Legacy gallery copies → the master design they were copied from.

Before shared master designs existed, "Make it yours" copied the XJet design: a new design id, a new
Ring ID and copies of the four images. Such a copy IS the master's ring (same prompt, identical
images), so one ring appeared twice — "Fil Twist R-1012" and "Fil Line R-1015". Merging folds the
copy back into the master:
  * the customer's journey becomes their link to the master (a gallery use) — or, when the master is
    their own design, simply part of its session;
  * every row that pointed at the copy (Customize choices, bag lines, orders, quote requests, 3D
    requests and models, movies, events, forks) points at the master's matching option, so the
    master's 3D model and STL serve the copy's orders;
  * the copy's Ring ID is retired for good (never reused) and the merge is recorded on the journey.
Only a true copy is merged: no refinement of its own, and every image identical to the master's.
"""

from p3 import ringids as RingIds
from p3 import sessions as Sessions
from p3.context import Context, HttpError
from p3.db import NewId, Now
from p3.naming import NameForVariation

Live = ("queued", "running", "ready")


def _Rows(Cur) -> list[dict]:
    Names = [D[0] for D in Cur.description]
    return [dict(zip(Names, R)) for R in Cur.fetchall()]


def _Candidates(Db, DesignId: str) -> list[dict]:
    return Db.All("SELECT c.*, b.kind FROM candidates c JOIN batches b ON b.id = c.batch_id WHERE b.design_id = ? "
                  "ORDER BY b.created_at, b.id, c.slot", (DesignId,))


def CopyMapping(Db, CopyId: str, MasterId: str) -> dict[str, str] | None:
    """copy option id → master option id when the copy's images are the master's (by content hash, else
    the same first-batch slot with the same seed); None when the two designs differ."""
    Cc, Mc = _Candidates(Db, CopyId), _Candidates(Db, MasterId)
    if not Cc or any(C["kind"] == "refine" for C in Cc):
        return None
    ByHash: dict[str, str] = {}
    for M in Mc:
        if M["content_sha256"]:
            ByHash.setdefault(M["content_sha256"], M["id"])
    Initial = [M for M in Mc if M["kind"] == "initial"]
    Out = {}
    for C in Cc:
        Target = ByHash.get(C["content_sha256"]) if C["content_sha256"] else None
        if Target is None:
            Same = next((M for M in Initial if M["slot"] == C["slot"]), None)
            if Same and (Same["seed"] == C["seed"] or (C["status"] != "ready" and Same["status"] != "ready")):
                Target = Same["id"]
        if Target is None:
            return None
        Out[C["id"]] = Target
    return Out


def MergeTarget(Ctx: Context, Design: dict) -> dict | None:
    """For a legacy copy: the master it can be merged into (None for any other design)."""
    if not Design or not Design.get("source_design_id"):
        return None
    Db = Ctx.Db
    Master = Db.One("SELECT id, title, ring_no, charm_no, product_type, owner_account_id FROM designs WHERE id = ?",
                    (Design["source_design_id"],))
    if Master is None or (Master.get("product_type") or "ring") != (Design.get("product_type") or "ring") \
            or CopyMapping(Db, Design["id"], Master["id"]) is None:
        return None                                       # a copy is always the same product as its master
    return {"master_id": Master["id"], "master_ring_id": RingIds.Ref(Master), "master_title": Master["title"],
            "same_owner": Master["owner_account_id"] == Design["owner_account_id"]}


def LegacyCopies(Ctx: Context) -> list[dict]:
    """Every design that is still a legacy copy of a master, oldest first."""
    Out = []
    for D in Ctx.Db.All("SELECT * FROM designs WHERE source_design_id IS NOT NULL ORDER BY created_at"):
        Target = MergeTarget(Ctx, D)
        if Target:
            Out.append({"design_id": D["id"], "ring_id": RingIds.Ref(D), "title": D["title"],
                        "owner_account_id": D["owner_account_id"], "created_at": D["created_at"], **Target})
    return Out


def MergeLegacyCopy(Ctx: Context, DesignId: str, By: str) -> dict:
    Db = Ctx.Db
    Copy = Db.One("SELECT * FROM designs WHERE id = ?", (DesignId,))
    if Copy is None:
        raise HttpError(404, "design_not_found", "Design not found.")
    if not Copy["source_design_id"]:
        raise HttpError(409, "not_a_copy", "This design was not copied from a gallery design.")
    Master = Db.One("SELECT * FROM designs WHERE id = ?", (Copy["source_design_id"],))
    if Master is None:
        raise HttpError(409, "master_missing", "The gallery design this copy came from no longer exists.")
    if (Master.get("product_type") or "ring") != (Copy.get("product_type") or "ring"):
        raise HttpError(409, "not_a_copy", "A ring and a charm are never merged into each other.")
    if Db.One("SELECT 1 AS x FROM batches WHERE design_id = ? AND kind = 'refine'", (DesignId,)):
        raise HttpError(409, "not_a_copy", "This design has refinements of its own: it is a variation, not a copy of the master.")
    Map = CopyMapping(Db, DesignId, Master["id"])
    if Map is None:
        raise HttpError(409, "not_a_copy", "This design's images differ from the master's: it is not a copy.")
    Owner = Copy["owner_account_id"]
    SameOwner = Owner == Master["owner_account_id"]
    MasterRefs = RingIds.CandidateRefs(Db, [Master["id"]])
    CopyRefs = RingIds.CandidateRefs(Db, [DesignId])
    CopyRing, MasterRing = RingIds.Ref(Copy), RingIds.Ref(Master)
    Assets = {C["id"]: C["asset_path"] for C in _Candidates(Db, Master["id"])}

    def Mapped(CandidateId):
        if CandidateId in Map:
            return Map[CandidateId]
        return CandidateId if CandidateId in MasterRefs else None

    T = Now()
    Moved = {K: 0 for K in ("customizations", "bag_lines", "order_lines", "quote_requests", "session_3d", "meshes", "movies",
                            "events", "forks")}
    Removed, Orders, UseId = [], [], None
    with Db.Transaction() as Conn:
        # 1. The journey: another customer's copy becomes their link to the master; the owner's own copy
        #    is simply part of the master's session.
        Selected = Mapped(Copy["selected_candidate_id"]) if Copy["selected_candidate_id"] else None
        Source = Mapped(Copy["source_candidate_id"]) if Copy["source_candidate_id"] else None
        if not SameOwner:
            Item = _Rows(Conn.execute("SELECT id FROM gallery_items WHERE design_id = ?", (Master["id"],)))
            Existing = _Rows(Conn.execute("SELECT * FROM gallery_uses WHERE design_id = ? AND owner_account_id = ?", (Master["id"], Owner)))
            if Existing:
                UseId = Existing[0]["id"]
                Conn.execute("UPDATE gallery_uses SET started_at = MIN(started_at, ?), last_active_at = MAX(last_active_at, ?), "
                             "selected_candidate_id = COALESCE(selected_candidate_id, ?), source_candidate_id = COALESCE(source_candidate_id, ?), "
                             "removed_at = CASE WHEN ? IS NULL THEN NULL ELSE removed_at END WHERE id = ?",
                             (Copy["created_at"], Copy["updated_at"], Selected, Source, Copy["removed_at"], UseId))
            else:
                UseId = NewId("use")
                Conn.execute("INSERT INTO gallery_uses (id, design_id, owner_account_id, gallery_item_id, source_candidate_id, "
                             "selected_candidate_id, started_at, last_active_at, removed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                             (UseId, Master["id"], Owner, Item[0]["id"] if Item else None, Source, Selected or Source,
                              Copy["created_at"], Copy["updated_at"], Copy["removed_at"]))
        # 2. Customize choices are unique per customer, design and option: the most recent choice wins.
        for Cu in _Rows(Conn.execute("SELECT * FROM customizations WHERE design_id = ?", (DesignId,))):
            Target = Map[Cu["candidate_id"]]
            Clash = _Rows(Conn.execute("SELECT * FROM customizations WHERE owner_account_id = ? AND design_id = ? AND candidate_id = ?",
                                       (Cu["owner_account_id"], Master["id"], Target)))
            if not Clash:
                Conn.execute("UPDATE customizations SET design_id = ?, candidate_id = ? WHERE id = ?", (Master["id"], Target, Cu["id"]))
            else:
                Keep = Clash[0]
                if (Cu["updated_at"] or "") > (Keep["updated_at"] or ""):
                    Conn.execute("UPDATE customizations SET material_id = ?, ring_size = ?, quantity = ?, updated_at = ? WHERE id = ?",
                                 (Cu["material_id"], Cu["ring_size"], Cu["quantity"], Cu["updated_at"], Keep["id"]))
                for Table in ("bag_lines", "order_lines"):
                    Conn.execute(f"UPDATE {Table} SET customization_id = ? WHERE customization_id = ?", (Keep["id"], Cu["id"]))
                Conn.execute("DELETE FROM customizations WHERE id = ?", (Cu["id"],))
            Moved["customizations"] += 1
        # 3. Everything that points at the copy's design and options.
        for Table in ("bag_lines", "session_3d", "quote_requests", "order_lines"):
            for Row in _Rows(Conn.execute(f"SELECT * FROM {Table} WHERE design_id = ?", (DesignId,))):
                Target = Mapped(Row["candidate_id"]) or Map[next(iter(Map))]
                Conn.execute(f"UPDATE {Table} SET design_id = ?, candidate_id = ? WHERE id = ?", (Master["id"], Target, Row["id"]))
                if Table in ("order_lines", "quote_requests"):
                    Conn.execute(f"UPDATE {Table} SET ring_id = ?, title = ? WHERE id = ?", (MasterRefs.get(Target), Master["title"], Row["id"]))
                if Table == "order_lines":
                    Conn.execute("UPDATE order_lines SET image_path = COALESCE(?, image_path) WHERE id = ?", (Assets.get(Target), Row["id"]))
                    O = _Rows(Conn.execute("SELECT id, order_no FROM orders WHERE id = ?", (Row["order_id"],)))
                    if O:
                        Ref = f"ORD-{O[0]['order_no']}" if O[0]["order_no"] else O[0]["id"]
                        Orders.append(Ref)
                        Conn.execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                                     (Row["order_id"], "note", Sessions.Dumps({
                                         "note": f"{Row['ring_id'] or CopyRing} ({Row['title']}) was a copy of {MasterRefs.get(Target)} "
                                                 f"({Master['title']}) — the same ring. The line now uses the master design, so its 3D model "
                                                 f"and STL come from {MasterRing}.",
                                         "merged_from": {"design_id": DesignId, "ring_id": Row["ring_id"], "title": Row["title"]}}), By, T))
                Moved[Table] += 1
        # 4. Movies follow the option; a duplicate of a live movie the master already has is removed.
        for Mv in _Rows(Conn.execute("SELECT m.* FROM movies m JOIN candidates c ON c.id = m.candidate_id JOIN batches b ON b.id = c.batch_id "
                                     "WHERE b.design_id = ?", (DesignId,))):
            Target = Map[Mv["candidate_id"]]
            Dup = Mv["status"] in Live and _Rows(Conn.execute(
                "SELECT id FROM movies WHERE candidate_id = ? AND config_version = ? AND status IN ('queued','running','ready')",
                (Target, Mv["config_version"])))
            if Dup:
                Conn.execute("DELETE FROM movies WHERE id = ?", (Mv["id"],))
                Removed.append({"movie_id": Mv["id"], "asset_path": Mv["asset_path"], "kept": Dup[0]["id"]})
            else:
                Conn.execute("UPDATE movies SET candidate_id = ? WHERE id = ?", (Target, Mv["id"]))
                Moved["movies"] += 1
        # 5. 3D models follow the option (their ids stay, so geometry, jobs and costs stay attached).
        for Old, New in Map.items():
            Moved["meshes"] += Conn.execute("UPDATE meshes SET candidate_id = ? WHERE candidate_id = ?", (New, Old)).rowcount
            Conn.execute("UPDATE batches SET parent_candidate_id = ? WHERE parent_candidate_id = ?", (New, Old))
        # 6. Forks of the copy become forks of the master.
        for F in _Rows(Conn.execute("SELECT id, source_candidate_id FROM designs WHERE source_design_id = ?", (DesignId,))):
            Conn.execute("UPDATE designs SET source_design_id = ?, source_candidate_id = ? WHERE id = ?",
                         (Master["id"], Mapped(F["source_candidate_id"]), F["id"]))
            Moved["forks"] += 1
        for U in _Rows(Conn.execute("SELECT id, owner_account_id FROM gallery_uses WHERE design_id = ?", (DesignId,))):
            Clash = _Rows(Conn.execute("SELECT id FROM gallery_uses WHERE design_id = ? AND owner_account_id = ?", (Master["id"], U["owner_account_id"])))
            if Clash:
                Conn.execute("DELETE FROM gallery_uses WHERE id = ?", (U["id"],))
            else:
                Conn.execute("UPDATE gallery_uses SET design_id = ? WHERE id = ?", (Master["id"], U["id"]))
        Conn.execute("DELETE FROM gallery_items WHERE design_id = ?", (DesignId,))
        # 7. The journey's history moves to the master; option ids inside the events follow.
        for Ev in _Rows(Conn.execute("SELECT id, data_json FROM session_events WHERE design_id = ?", (DesignId,))):
            Data = Ev["data_json"] or "{}"
            for Old, New in Map.items():
                Data = Data.replace(Old, New)
            Conn.execute("UPDATE session_events SET design_id = ?, data_json = ? WHERE id = ?",
                         (Master["id"], Data.replace(DesignId, Master["id"]), Ev["id"]))
            Moved["events"] += 1
        # 8. The copy's Ring ID is retired for good; the copy itself goes.
        RingIds.RetireDesign(Conn, Copy, Master["id"], T)
        Conn.execute("DELETE FROM candidates WHERE batch_id IN (SELECT id FROM batches WHERE design_id = ?)", (DesignId,))
        Conn.execute("DELETE FROM batches WHERE design_id = ?", (DesignId,))
        Conn.execute("DELETE FROM designs WHERE id = ?", (DesignId,))
        Conn.execute("UPDATE designs SET updated_at = ? WHERE id = ?", (T, Master["id"]))
    Sessions.Record(Ctx, Owner, "legacy_copy_merged", Master["id"], copy_design_id=DesignId, copy_ring_id=CopyRing,
                    copy_title=Copy["title"], master_ring_id=MasterRing, use_id=UseId, orders=sorted(set(Orders)),
                    moved=Moved, movies_removed=[R["movie_id"] for R in Removed], by=By)
    return {"copy": {"id": DesignId, "ring_id": CopyRing, "title": Copy["title"], "options": CopyRefs},
            "master": {"id": Master["id"], "ring_id": MasterRing, "title": Master["title"]},
            "session_id": UseId or Master["id"], "use_id": UseId, "same_owner": SameOwner,
            "options": {CopyRefs.get(Old): MasterRefs.get(New) for Old, New in Map.items()},
            "moved": Moved, "movies_removed": Removed, "orders": sorted(set(Orders))}


# ── the other direction: a refinement that landed inside a master becomes a design of its own ─────
def SplitRefinement(Ctx: Context, BatchId: str, By: str) -> dict:
    """Before 'a design with a movie, 3D or order is never changed by a refinement', a refinement by the
    owner was added inside the design — so a master that already had a 360° movie or a 3D model also
    carried a second generation of images. This moves that refinement (and any refinements made from it)
    into a new design of its own, named in the lineage of the master, with everything that followed it:
    the movie, Customize choices, bag lines, orders, quote requests and 3D requests on its images, and
    the recorded steps about them. The master's selection goes back to the option it was refined from
    (or its gallery image). The design is otherwise untouched."""
    Db = Ctx.Db
    B = Db.One("SELECT * FROM batches WHERE id = ?", (BatchId,))
    if B is None:
        raise HttpError(404, "batch_not_found", "Batch not found.")
    if B["kind"] != "refine":
        raise HttpError(409, "not_a_refinement", "Only a refinement can be moved into a design of its own.")
    D = Db.One("SELECT * FROM designs WHERE id = ?", (B["design_id"],))
    # The refinement and every later refinement made from one of its images (a chain stays together)
    Batches = Db.All("SELECT * FROM batches WHERE design_id = ? ORDER BY created_at, id", (D["id"],))
    ByParent = {}
    for X in Batches:
        Cs = Db.All("SELECT id, slot FROM candidates WHERE batch_id = ?", (X["id"],))
        X["_cands"] = Cs
        for C in Cs:
            ByParent[C["id"]] = X["id"]
    Moving, Queue = [], [B["id"]]
    while Queue:
        Bid = Queue.pop(0)
        X = next(Y for Y in Batches if Y["id"] == Bid)
        Moving.append(X)
        Own = {C["id"] for C in X["_cands"]}
        Queue += [Y["id"] for Y in Batches if Y["parent_candidate_id"] in Own and Y["id"] not in [M["id"] for M in Moving] + Queue]
    CandIds = {C["id"] for X in Moving for C in X["_cands"]}
    Tile = Db.One("SELECT candidate_id FROM gallery_items WHERE design_id = ?", (D["id"],))
    if Tile and Tile["candidate_id"] in CandIds:
        raise HttpError(409, "gallery_image", "This refinement is the design's gallery image: it is what the design shows and sells, "
                                              "so it stays. Change the gallery image first if you really want to move it.")
    if len(Moving) == len(Batches):
        raise HttpError(409, "nothing_left", "Every batch of this design would move; nothing would be left.")
    Title = NameForVariation(Db, D["title"], B["user_text"], D["prompt"], D.get("product_type") or "ring")
    New, T = NewId("dsg"), Now()
    Q = ",".join("?" * len(CandIds))
    Ids = list(CandIds)
    # Everything that points at the moved images, for the recorded steps that mention them
    Mentions = set(Ids)
    for Table in ("customizations", "bag_lines", "movies", "session_3d"):
        Mentions |= {R["id"] for R in Db.All(f"SELECT id FROM {Table} WHERE candidate_id IN ({Q})", Ids)}
    MasterSelection = D["selected_candidate_id"]
    if MasterSelection in CandIds:
        MasterSelection = (Tile["candidate_id"] if Tile else None) or B["parent_candidate_id"]
    Moved = {}
    with Db.Transaction() as Conn:
        # The split-off refinement is the same product as the design it came from
        Conn.execute("INSERT INTO designs (id, owner_account_id, title, prompt, selected_candidate_id, created_at, updated_at, ai_mode, "
                     "source_design_id, source_candidate_id, product_type) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (New, D["owner_account_id"], Title, D["prompt"], D["selected_candidate_id"] if D["selected_candidate_id"] in CandIds else None,
                      B["created_at"], D["updated_at"], D["ai_mode"], D["id"], B["parent_candidate_id"], D.get("product_type") or "ring"))
        for X in Moving:
            Conn.execute("UPDATE batches SET design_id = ? WHERE id = ?", (New, X["id"]))
        Moved["batches"] = len(Moving)
        for Table in ("customizations", "bag_lines", "session_3d", "order_lines", "quote_requests"):
            Moved[Table] = Conn.execute(f"UPDATE {Table} SET design_id = ? WHERE candidate_id IN ({Q})", [New] + Ids).rowcount
        Conn.execute("UPDATE designs SET selected_candidate_id = ?, updated_at = ? WHERE id = ?", (MasterSelection, T, D["id"]))
        # Forks made from a moved image follow it
        Moved["forks"] = Conn.execute(f"UPDATE designs SET source_design_id = ? WHERE source_design_id = ? AND source_candidate_id IN ({Q})",
                                      [New, D["id"]] + Ids).rowcount
        Moved["events"] = 0
        for Ev in _Rows(Conn.execute("SELECT id, data_json FROM session_events WHERE design_id = ? AND created_at >= ?", (D["id"], B["created_at"]))):
            if any(M in (Ev["data_json"] or "") for M in Mentions):
                Conn.execute("UPDATE session_events SET design_id = ? WHERE id = ?", (New, Ev["id"]))
                Moved["events"] += 1
    NewRow = Db.One("SELECT ring_no, charm_no, product_type FROM designs WHERE id = ?", (New,))
    Refs = RingIds.CandidateRefs(Db, [New])
    for Table in ("order_lines", "quote_requests"):                   # snapshots carry the option's new Ring ID and the new name
        for R in Db.All(f"SELECT id, candidate_id FROM {Table} WHERE design_id = ?", (New,)):
            Db.Execute(f"UPDATE {Table} SET ring_id = ?, title = ? WHERE id = ?", (Refs.get(R["candidate_id"]), Title, R["id"]))
    Ring = RingIds.Ref(NewRow)
    Sessions.Record(Ctx, D["owner_account_id"], "design_forked", New, source_design_id=D["id"],
                    source_ring_id=RingIds.CandidateRef(Db, B["parent_candidate_id"]), parent_candidate_id=B["parent_candidate_id"],
                    reason="split", by=By, batch_id=BatchId)
    Sessions.Record(Ctx, D["owner_account_id"], "admin_refinement_split", D["id"], batch_id=BatchId, text=B["user_text"],
                    new_design_id=New, new_ring_id=Ring, new_title=Title, by=By, moved=Moved)
    return {"design_id": New, "ring_id": Ring, "title": Title, "master": {"id": D["id"], "ring_id": RingIds.Ref(D),
            "title": D["title"], "selected_candidate_id": MasterSelection}, "moved": Moved}
