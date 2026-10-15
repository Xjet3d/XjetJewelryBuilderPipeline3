"""The active STL of a design, material and size — the FixBore workflow update (the owner's decisions of 2026-10-15).

Accepting a FixBore correction activates the corrected revision at once, with no separate switch: every order line of
the same design, material and size that uses the result it corrects takes it — within limits set by where the line is:

    not yet with Fulfillment (the order New, Payment confirmed or 3D ready) ........ switched here at once
    with Fulfillment, its part still in review (not approved) ........................ asked of Fulfillment (the
                                                                                        replace_file command): switched
                                                                                        here only once it confirms
    approved for manufacturing · in a draft or released build · in production ·
    QC, packing, shipped · cancelled · in the existing production flow .............. never changed by itself (a revised
                                                                                        file with a reason is the way)

A line linked to another result, or produced from another 3D version, was chosen independently and is left as it is.
The Admin sees the complete list before accepting; every activation is recorded — who, when, why — with each line's
outcome. Restore makes the other file of the pair active again: an explicit action with a reason, under the same
rules. Nothing is deleted or overwritten — the original result, its revision and their files stay, so either can be
made active again. Accepting a correction approves nothing for manufacturing (Fulfillment's own decision), and wall
thickness and strength are not checked (deferred)."""

from p3 import orders as OrdersModule
from p3 import products as Products
from p3 import sessions as Sessions
from p3 import versions as Versions
from p3.context import Context, HttpError
from p3.db import Dumps, NewId, Now

# Fulfillment's stages in which a part's STL is never replaced by itself, and why (the owner's table of 2026-10-15)
LockedStages = {
    "approved": "approved for manufacturing", "planned": "assigned to a build", "in_production": "in production",
    "post_processing": "in post-processing", "qc": "in QC", "rework": "in rework", "ready_to_ship": "ready to ship (packing)",
    "partially_shipped": "partly shipped", "shipped": "shipped", "delivered": "delivered", "cancelled": "cancelled",
}
Outcomes = {"switched": "Switched", "linked": "Linked", "pending": "Waiting for Fulfillment", "applied": "Switched — Fulfillment confirmed",
            "refused": "Kept — Fulfillment refused", "skipped": "Not changed"}
Never = "its STL is never replaced by itself — a revised file, with a reason, is the way"


class StlActivations:
    def __init__(self, Ctx: Context, Orders, Production):
        self.Ctx, self.Orders, self.Production = Ctx, Orders, Production
        self.Bridge = None                      # the Fulfillment bridge (p3/app.py wires it)

    # ── the pair and which of its two files is active ────────────────────
    def Pair(self, Sid: str) -> dict | None:
        """A result and its accepted bore-corrected revision (or the revision and the result it corrects), and which
        of the two is active: the newest activation says; an acceptance whose activation did not complete still means
        the revision."""
        Db = self.Ctx.Db
        F = Db.One("SELECT * FROM bore_fixes WHERE status = 'accepted' AND revision_id IS NOT NULL AND (session_3d_id = ? OR revision_id = ?) "
                   "ORDER BY decided_at DESC LIMIT 1", (Sid, Sid))
        if F is None:
            return None
        Last = self._Last(F["id"])
        Active = Last["to_three_d_id"] if Last else F["revision_id"]
        return {"bore_fix_id": F["id"], "original": F["session_3d_id"], "revision": F["revision_id"], "active": Active,
                "inactive": F["session_3d_id"] if Active == F["revision_id"] else F["revision_id"], "activated": Last is not None}

    def _Last(self, Pid: str, Conn=None) -> dict | None:
        Sql, P = "SELECT * FROM stl_activations WHERE bore_fix_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (Pid,)
        R = Conn.execute(Sql, P).fetchone() if Conn is not None else self.Ctx.Db.One(Sql, P)
        return dict(R) if R else None

    # ── what an activation does to each line ─────────────────────────────
    def Plan(self, From: str, To: str | None) -> list[dict]:
        """Every order line of `From`'s design, material and size, and what activating `To` in its place does to it
        (To None: a proposal — the revision it would become). The complete list the Admin sees before deciding."""
        Db = self.Ctx.Db
        S = Db.One("SELECT * FROM session_3d WHERE id = ?", (From,))
        if S is None or S["production_size"] is None:
            return []
        Rows = Db.All("SELECT l.*, o.order_no, o.status AS order_status FROM order_lines l JOIN orders o ON o.id = l.order_id "
                      "WHERE l.design_id = ? AND l.material_id = ? ORDER BY o.created_at, o.order_no, l.position", (S["design_id"], S["material_id"]))
        Out = []
        for L in Rows:
            Size = OrdersModule.LineSize(L)
            if Size is not None and float(Size) == float(S["production_size"]):
                Out.append(self._Classify(L, S, From, To))
        return Out

    def _Classify(self, L: dict, S: dict, From: str, To: str | None) -> dict:
        Db = self.Ctx.Db
        Product = L.get("product_type") or Products.Ring
        X = {"order_id": L["order_id"], "order_ref": OrdersModule.OrderRef(L["order_no"]), "order_status": L["order_status"],
             "order_status_label": OrdersModule.StatusLabels.get(L["order_status"], L["order_status"]), "line_id": L["id"], "item_ref": L["ring_id"],
             "title": L["title"], "quantity": L["quantity"], "size_label": Products.SizeLabel(Product, L["ring_size"], L.get("charm_size")),
             "material_label": L["material_label"], "current_three_d_id": L["three_d_id"], "transferred": False, "stage": None, "stage_label": None,
             "part_display_no": None}

        def Do(Action: str, Reason: str | None = None) -> dict:
            return {**X, "action": Action, "reason": Reason}

        Prod = Db.One("SELECT * FROM fulfillment_line_production WHERE line_id = ?", (L["id"],))
        if Prod:
            Stage, Part = self._Stage(L, Prod)
            X.update({"transferred": True, "current_three_d_id": Prod["three_d_id"], "part_display_no": (Part or {}).get("display_no") or Prod["part_display_no"],
                      "stage": Stage, "stage_label": self._StageLabel(Stage)})
            if Prod["three_d_id"] != From:
                return Do("skip", "already uses this STL" if To and Prod["three_d_id"] == To else "in Production with another STL — chosen independently")
            if not (self.Bridge and self.Bridge.Enabled):
                return Do("skip", "with Fulfillment, and the integration is off on this site — its STL is not changed")
            if L["order_status"] == "cancelled":
                return Do("skip", "order cancelled")
            Locked = LockedStages.get(Stage) or ("approved for manufacturing" if (Part or {}).get("review_status") == "approved" else None) \
                or ("assigned to a build" if (Part or {}).get("build") else None)
            if Locked:
                return Do("skip", f"{Locked} — {Never}")
            if Db.One("SELECT 1 AS x FROM stl_activation_lines WHERE line_id = ? AND outcome = 'pending'", (L["id"],)):
                return Do("skip", "an earlier STL change of this line waits for Fulfillment's answer")
            return Do("request", "in Fulfillment's review: Fulfillment applies it while the part is still in review, else keeps the former STL")
        Version = L["mesh_id"] or (Versions.MeshFor(Db, L["design_id"], L.get("candidate_id")) or {}).get("id")
        Mine = L["three_d_id"] == From or (L["three_d_id"] is None and Version == S["mesh_id"])
        if not Mine:
            if To and L["three_d_id"] == To:
                return Do("skip", "already uses this STL")
            return Do("skip", "linked to another STL — chosen independently" if L["three_d_id"] else "produced from another 3D version — chosen independently")
        if L["order_status"] == "cancelled":
            return Do("skip", "order cancelled")
        if L["order_status"] == "completed":
            return Do("skip", "order completed")
        if L["order_status"] in OrdersModule.ProductionStatuses:
            return Do("skip", f"in the existing production flow ({X['order_status_label']}) — its STL is not changed by itself")
        if L["three_d_id"] == From:
            return Do("switch")
        return Do("link", "not linked to an STL yet")

    def _Stage(self, L: dict, Prod: dict) -> tuple[str, dict | None]:
        """Where Fulfillment last said the line's part is (its projection), else what Atelier knows of the hand-over."""
        if self.Bridge is None:
            return ("received" if Prod["state"] == "received" else "sent"), None
        Part = self.Bridge._PartOf(self.Bridge.Projection("order", L["order_id"]) or {}, L["id"])
        return (Part or {}).get("stage") or ("received" if Prod["state"] == "received" else "sent"), Part

    def _StageLabel(self, Stage: str | None) -> str | None:
        from p3.fulfillment_bridge import ProductionStageLabels
        return ProductionStageLabels.get(Stage, Stage) if Stage else None

    # ── activating ───────────────────────────────────────────────────────
    def Activate(self, Pid: str, Kind: str, By: str, Note: str = "") -> dict:
        """Accept (the corrected revision becomes active) or restore (the other file of the pair becomes active again):
        each line as the plan says, in one transaction with the activation's record — a line switched here, a line in
        Fulfillment's review asked of Fulfillment (switched here once it confirms), every other line left as it is, with
        the reason. An accepted correction is activated once; a repeat returns that activation."""
        Db = self.Ctx.Db
        F = Db.One("SELECT * FROM bore_fixes WHERE id = ?", (Pid,))
        if F is None:
            raise HttpError(404, "bore_fix_not_found", "Bore correction not found.")
        if F["status"] != "accepted" or not F["revision_id"]:
            raise HttpError(409, "not_accepted", "Only an accepted bore correction has an STL to activate.")
        Note = " ".join(str(Note or "").split())[:300]
        Last = self._Last(Pid)
        if Kind == "accept":
            if Last:
                return self.ActivationJson(Last["id"])
            From, To = F["session_3d_id"], F["revision_id"]
            Reason = f"bore corrected (FixBore {Pid}, accepted by {By})" + (f": {Note}" if Note else "")
        elif Kind == "restore":
            if not Last:
                raise HttpError(409, "not_activated", "The correction's activation has not completed: accept it again first.")
            if len(Note) < 3:
                raise HttpError(400, "reason_required", "Say why the other STL is made active again (kept with every order it changes).")
            From = Last["to_three_d_id"]
            To = F["session_3d_id"] if From == F["revision_id"] else F["revision_id"]
            Reason = ("restored the original STL" if To == F["session_3d_id"] else "the bore-corrected STL again") + f" (FixBore {Pid}, by {By}): {Note}"
        else:
            raise HttpError(400, "invalid_kind", "accept or restore.")
        self._NothingPending(Pid)
        Plan = self.Plan(From, To)
        # What a line asked of Fulfillment will carry — read before the transaction (an export of a restored original
        # may start here: local work, never a paid call)
        Payloads, File, Problem = {}, None, None
        Requests = [X for X in Plan if X["action"] == "request"]
        if Requests:
            try:
                File = self.Bridge._ProductionFile(To)
            except HttpError as E:
                Problem = f"no production file can be made of it: {E.Message}"
            for X in Requests:
                if Problem:
                    X.update({"action": "skip", "reason": Problem})
                    continue
                L = next(Y for Y in self.Orders.AdminGet(X["order_id"])["lines"] if Y["id"] == X["line_id"])
                Payloads[X["line_id"]] = self.Bridge.FileReplacement(L, To, File)
        T, Aid = Now(), NewId("act")
        Mesh = Db.One("SELECT s.mesh_id, m.version_no FROM session_3d s LEFT JOIN meshes m ON m.id = s.mesh_id WHERE s.id = ?", (To,))
        with Db.Transaction() as Conn:
            Latest = self._Last(Pid, Conn)
            if (Latest or {}).get("to_three_d_id") != (Last or {}).get("to_three_d_id"):
                raise HttpError(409, "changed_meanwhile", "The active STL changed meanwhile; reload and check.")
            if Conn.execute("SELECT 1 FROM stl_activation_lines x JOIN stl_activations a ON a.id = x.activation_id WHERE a.bore_fix_id = ? "
                            "AND x.outcome = 'pending'", (Pid,)).fetchone():
                raise HttpError(409, "activation_pending", "Lines of the previous change still wait for Fulfillment's answer.")
            Conn.execute("INSERT INTO stl_activations (id, bore_fix_id, kind, from_three_d_id, to_three_d_id, by, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (Aid, Pid, Kind, From, To, By, Note or None, T))
            for X in Plan:
                Acl, Outcome, Why, Cmd = NewId("acl"), "skipped", X["reason"], None
                if X["action"] == "switch":
                    N = Conn.execute("UPDATE order_lines SET three_d_id = ? WHERE id = ? AND order_id = ? AND three_d_id = ?", (To, X["line_id"], X["order_id"], From)).rowcount
                    Outcome, Why = ("switched", None) if N == 1 else ("skipped", "its STL changed meanwhile")
                elif X["action"] == "link":
                    N = Conn.execute("UPDATE order_lines SET three_d_id = ?, mesh_id = ? WHERE id = ? AND order_id = ? AND three_d_id IS NULL",
                                     (To, Mesh["mesh_id"], X["line_id"], X["order_id"])).rowcount
                    Outcome, Why = ("linked", X["reason"]) if N == 1 else ("skipped", "linked meanwhile")
                elif X["action"] == "request":
                    Cmd = self.Bridge.RequestFileReplacement(Conn, X["order_id"], X["line_id"], Payloads[X["line_id"]], Reason, By, Acl, T)
                    Outcome = "pending"
                Conn.execute("INSERT INTO stl_activation_lines (id, activation_id, order_id, line_id, item_ref, from_three_d_id, to_three_d_id, outcome, reason, "
                             "stage, part_display_no, command_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (Acl, Aid, X["order_id"], X["line_id"], X["item_ref"], X["current_three_d_id"], To, Outcome, Why, X["stage"] or X["order_status"],
                              X["part_display_no"], Cmd, T, T))
                if Outcome in ("switched", "linked", "pending"):
                    Conn.execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                                 (X["order_id"], "stl_activated", Dumps({"line_id": X["line_id"], "item_ref": X["item_ref"], "kind": Kind, "outcome": Outcome,
                                                                         "from_three_d_id": X["current_three_d_id"], "session_3d_id": To, "mesh_id": Mesh["mesh_id"],
                                                                         "version_no": Mesh["version_no"], "bore_fix_id": Pid, "activation_id": Aid,
                                                                         "command_id": Cmd, "reason": Reason}), By, T))
        Row = Db.One("SELECT s.design_id, d.owner_account_id FROM session_3d s JOIN designs d ON d.id = s.design_id WHERE s.id = ?", (To,))
        Act = self.ActivationJson(Aid)
        Sessions.Record(self.Ctx, Row["owner_account_id"], "admin_3d_stl_activated", Row["design_id"], bore_fix_id=Pid, activation_id=Aid, kind=Kind,
                        from_three_d_id=From, to_three_d_id=To, counts=Act["counts"], note=Note or None, by=By)
        return Act

    def _NothingPending(self, Pid: str) -> None:
        N = self.Ctx.Db.One("SELECT COUNT(*) AS n FROM stl_activation_lines x JOIN stl_activations a ON a.id = x.activation_id WHERE a.bore_fix_id = ? "
                            "AND x.outcome = 'pending'", (Pid,))["n"]
        if N:
            raise HttpError(409, "activation_pending", f"{N} line(s) of the previous change still wait for Fulfillment's answer: decide again once it has answered.")

    def Restore(self, Pid: str, By: str, Note: str) -> dict:
        return self.Activate(Pid, "restore", By, Note)

    # ── what the pages show ──────────────────────────────────────────────
    def ActivationJson(self, Aid: str) -> dict:
        Db = self.Ctx.Db
        A = Db.One("SELECT * FROM stl_activations WHERE id = ?", (Aid,))
        Lines = Db.All("SELECT * FROM stl_activation_lines WHERE activation_id = ? ORDER BY rowid", (Aid,))
        for L in Lines:
            L["outcome_label"] = Outcomes.get(L["outcome"], L["outcome"])
            O = Db.One("SELECT order_no FROM orders WHERE id = ?", (L["order_id"],))
            L["order_ref"] = OrdersModule.OrderRef(O["order_no"]) if O else None
        Counts = {K: sum(1 for L in Lines if L["outcome"] == K) for K in Outcomes}
        return {**A, "lines": Lines, "counts": {K: V for K, V in Counts.items() if V}}

    def History(self, Pid: str) -> list[dict]:
        return [self.ActivationJson(R["id"]) for R in self.Ctx.Db.All("SELECT id FROM stl_activations WHERE bore_fix_id = ? ORDER BY created_at DESC, rowid DESC", (Pid,))]

    def ForFix(self, F: dict) -> dict:
        """The session page's view of a correction: the lines it would change (a proposal), or which file is active, the
        activations so far, and what a restore would change (accepted)."""
        if F["status"] == "proposed":
            return {"affected": self.Plan(F["session_3d_id"], None)}
        if F["status"] != "accepted" or not F["revision_id"]:
            return {}
        P = self.Pair(F["revision_id"]) or {}
        Pending = self.Ctx.Db.One("SELECT COUNT(*) AS n FROM stl_activation_lines x JOIN stl_activations a ON a.id = x.activation_id WHERE a.bore_fix_id = ? "
                                  "AND x.outcome = 'pending'", (F["id"],))["n"]
        return {"active_three_d_id": P.get("active"), "activated": P.get("activated"), "activations": self.History(F["id"]), "pending": Pending,
                "restore_to": P.get("inactive") if P.get("activated") else None,
                "restore_affected": self.Plan(P["active"], P["inactive"]) if P.get("activated") else []}

    def LineNote(self, LineId: str) -> dict | None:
        """The newest activation that concerned an order line (the order page says it on the line)."""
        R = self.Ctx.Db.One("SELECT x.*, a.kind, a.by, a.bore_fix_id, a.note AS activation_note FROM stl_activation_lines x "
                            "JOIN stl_activations a ON a.id = x.activation_id WHERE x.line_id = ? ORDER BY x.created_at DESC, x.rowid DESC LIMIT 1", (LineId,))
        if R is None:
            return None
        return {**R, "outcome_label": Outcomes.get(R["outcome"], R["outcome"])}


def Inactive(Pair: dict | None, Sid: str) -> bool:
    """Whether a result is the inactive file of its pair (StlCandidates leaves it out for a line not yet linked)."""
    return bool(Pair and Pair["inactive"] == Sid)
