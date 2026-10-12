"""The Atelier ↔ Fulfillment bridge (docs/FULFILLMENT.md). Everything here exists only while P3_FULFILLMENT_INTEGRATION
is on; off, the routes answer 404, the Admin action is refused, and the triggers fire for nobody (no record is enrolled).

Atelier hands a record to Fulfillment by an explicit Admin action (enrollment). From then on every commercial change
to that record — payment, address, customer, totals, a status the Admin sets, a quote becoming an order, a 3D result —
is captured by database triggers into an outbox, in the same transaction as the change. Fulfillment initiates every
connection: it reads the outbox (`changes`) with a durable cursor, fetches the record's snapshot (`requests`), fetches
files through restricted asset routes, and posts its own events (`events`), which land in an inbox (idempotent by
event id) and update a projection the Admin shows. Atelier never writes to Fulfillment's database; Fulfillment never
writes to this one. The projection's own status writes (production / qc / shipped / completed) are not commercial
changes and never go back out (no loop).

Authentication: a dedicated service credential (P3_FULFILLMENT_SERVICE_KEY) as a Bearer token — never the Admin
key, never a customer token. The integration routes live in the Admin context (the route-separation middleware keeps
them off the public host).
"""

import hashlib
import hmac
import json
import os
import secrets
import time

from fastapi import Body, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse

from p3 import assets
from p3 import orders as OrdersModule
from p3.admin import RequireAdmin
from p3.context import Context, HttpError
from p3.db import Dumps, NewId, Now

ContractVersion = "atelier-fulfillment/v1"
EnvironmentKey = "fulfillment_environment_id"
Prefix = "/api/integrations/fulfillment/v1"
ProjectionStatuses = ("production", "qc", "shipped", "completed")    # written by the projection; never a commercial change
EligibleOrderStatuses = ("new", "payment_confirmed", "three_d_ready")   # a quotation is never enrolled (D-10): its order is
MaxChanges, MaxEvents, MaxCheckItems = 500, 200, 100
CommandActions = {"cancel_line": "decide_cancellation"}            # an Admin command and the Fulfillment permission it delegates
CommandOutcomeEvents = ("fulfillment.command_applied", "fulfillment.command_rejected")
ReviewSeverity = ("cannot_produce", "changes_required", "in_review", "not_started", "approved")   # the worst review state of an order's parts first
Filters = {"enrolled": "With Fulfillment", "review": "Awaiting review", "changes": "Changes required / cannot produce", "blocked": "Blocked",
           "in_progress": "In progress", "ready_to_ship": "Ready to ship", "shipped": "Shipped", "commercial_action": "Commercial action pending",
           "awaiting_sync": "Awaiting synchronization"}

# The timestamp format of p3.db.Now(), produced by SQLite inside the triggers
_SqlNow = "strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')"

Schema = f"""
CREATE TABLE IF NOT EXISTS fulfillment_enrollments (
    id              TEXT PRIMARY KEY,
    entity_type     TEXT NOT NULL CHECK (entity_type IN ('order', 'quote_request')),
    entity_id       TEXT NOT NULL,
    display_ref     TEXT NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('enrolled', 'released')),
    source_version  INTEGER NOT NULL DEFAULT 1,       -- advanced by the triggers on every commercial change
    enrolled_at     TEXT NOT NULL,
    enrolled_by     TEXT NOT NULL,                    -- the Admin principal (today "developer-key": shared-key access)
    note            TEXT,
    inherited_from  TEXT,                             -- the enrolled quote request an order's enrollment came from
    released_at     TEXT,
    released_by     TEXT,
    release_note    TEXT,
    updated_at      TEXT NOT NULL,
    UNIQUE (entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS fulfillment_outbox (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT, -- committed order: the durable cursor Fulfillment keeps
    kind            TEXT NOT NULL,                     -- enrolled | updated | released
    entity_type     TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    source_version  INTEGER NOT NULL,
    reason          TEXT,
    correlation_id  TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fulfillment_outbox_entity ON fulfillment_outbox(entity_type, entity_id, seq);
CREATE TABLE IF NOT EXISTS fulfillment_inbox (
    event_id           TEXT PRIMARY KEY,
    event_type         TEXT NOT NULL,
    aggregate_type     TEXT,
    aggregate_id       TEXT,
    aggregate_version  INTEGER,
    entity_type        TEXT,
    entity_id          TEXT,
    payload_hash       TEXT NOT NULL,
    payload_json       TEXT NOT NULL,
    result             TEXT NOT NULL,                  -- applied | ignored_stale | unknown_entity
    received_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fulfillment_inbox_conflicts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    received_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fulfillment_projection (
    entity_type        TEXT NOT NULL,
    entity_id          TEXT NOT NULL,
    aggregate_version  INTEGER NOT NULL DEFAULT 0,
    state_json         TEXT NOT NULL DEFAULT '{{}}',
    last_event_id      TEXT,
    last_event_at      TEXT,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS fulfillment_sync (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
-- A command the Admin sent to Fulfillment (spec §7.4): pending until Fulfillment's answer arrives as an event.
CREATE TABLE IF NOT EXISTS fulfillment_commands (
    id                    TEXT PRIMARY KEY,                -- the command id Fulfillment echoes back
    entity_type           TEXT NOT NULL,
    entity_id             TEXT NOT NULL,
    action                TEXT NOT NULL,                   -- cancel_line
    payload_json          TEXT NOT NULL,
    expected_version      INTEGER,                         -- the enrollment's source version the Admin saw
    actor                 TEXT NOT NULL,                   -- the Admin principal this side authenticated
    delegated_permission  TEXT NOT NULL,
    state                 TEXT NOT NULL CHECK (state IN ('pending', 'applied', 'rejected')),
    result_json           TEXT NOT NULL DEFAULT '{{}}',
    created_at            TEXT NOT NULL,
    decided_at            TEXT,
    updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fulfillment_commands_entity ON fulfillment_commands(entity_type, entity_id, created_at);

-- A commercial change to an enrolled order: the enrollment's source version advances and the outbox gets one row,
-- in the transaction of the change itself. A status the projection writes is not a commercial change.
CREATE TRIGGER IF NOT EXISTS fulfillment_orders_changed AFTER UPDATE ON orders
WHEN EXISTS (SELECT 1 FROM fulfillment_enrollments e WHERE e.entity_type = 'order' AND e.entity_id = NEW.id AND e.state = 'enrolled')
 AND (OLD.payment_status IS NOT NEW.payment_status OR OLD.payment_json IS NOT NEW.payment_json OR OLD.shipping_json IS NOT NEW.shipping_json
      OR OLD.customer_json IS NOT NEW.customer_json OR OLD.total IS NOT NEW.total OR OLD.shipping_method IS NOT NEW.shipping_method
      OR (OLD.status IS NOT NEW.status AND NEW.status NOT IN ('production', 'qc', 'shipped', 'completed')))
BEGIN
  UPDATE fulfillment_enrollments SET source_version = source_version + 1, updated_at = {_SqlNow}
   WHERE entity_type = 'order' AND entity_id = NEW.id;
  INSERT INTO fulfillment_outbox (kind, entity_type, entity_id, source_version, reason, created_at)
  SELECT 'updated', 'order', NEW.id, e.source_version,
         CASE WHEN OLD.payment_status IS NOT NEW.payment_status OR OLD.payment_json IS NOT NEW.payment_json THEN 'payment'
              WHEN OLD.shipping_json IS NOT NEW.shipping_json OR OLD.shipping_method IS NOT NEW.shipping_method THEN 'address'
              WHEN OLD.status IS NOT NEW.status THEN 'status' ELSE 'commercial' END, {_SqlNow}
    FROM fulfillment_enrollments e WHERE e.entity_type = 'order' AND e.entity_id = NEW.id;
END;

-- Quotations are never enrolled (D-10, 2026-10-13): the Batch 3 trigger on quote_requests is gone
DROP TRIGGER IF EXISTS fulfillment_quotes_changed;

-- A 3D result for a design changes the model availability of every enrolled order line of that design
CREATE TRIGGER IF NOT EXISTS fulfillment_model_inserted AFTER INSERT ON session_3d
BEGIN
  UPDATE fulfillment_enrollments SET source_version = source_version + 1, updated_at = {_SqlNow}
   WHERE entity_type = 'order' AND state = 'enrolled' AND entity_id IN (SELECT order_id FROM order_lines WHERE design_id = NEW.design_id);
  INSERT INTO fulfillment_outbox (kind, entity_type, entity_id, source_version, reason, created_at)
  SELECT 'updated', 'order', e.entity_id, e.source_version, 'model', {_SqlNow} FROM fulfillment_enrollments e
   WHERE e.entity_type = 'order' AND e.state = 'enrolled' AND e.entity_id IN (SELECT order_id FROM order_lines WHERE design_id = NEW.design_id);
END;
CREATE TRIGGER IF NOT EXISTS fulfillment_model_changed AFTER UPDATE OF status, accepted_at ON session_3d
WHEN OLD.status IS NOT NEW.status OR OLD.accepted_at IS NOT NEW.accepted_at
BEGIN
  UPDATE fulfillment_enrollments SET source_version = source_version + 1, updated_at = {_SqlNow}
   WHERE entity_type = 'order' AND state = 'enrolled' AND entity_id IN (SELECT order_id FROM order_lines WHERE design_id = NEW.design_id);
  INSERT INTO fulfillment_outbox (kind, entity_type, entity_id, source_version, reason, created_at)
  SELECT 'updated', 'order', e.entity_id, e.source_version, 'model', {_SqlNow} FROM fulfillment_enrollments e
   WHERE e.entity_type = 'order' AND e.state = 'enrolled' AND e.entity_id IN (SELECT order_id FROM order_lines WHERE design_id = NEW.design_id);
END;
"""


def _Sha(Text: str) -> str:
    return hashlib.sha256(Text.encode("utf-8")).hexdigest()


class FulfillmentBridge:
    def __init__(self, Ctx: Context, Orders, Production):
        self.Ctx, self.Orders, self.Production = Ctx, Orders, Production
        self._ShaCache: dict = {}
        with Ctx.Db.Connect() as Conn:
            Conn.executescript(Schema)
            if "payload_json" not in {R[1] for R in Conn.execute("PRAGMA table_info(fulfillment_outbox)")}:   # Batch 4: commands travel in the feed
                Conn.execute("ALTER TABLE fulfillment_outbox ADD COLUMN payload_json TEXT")
        if self.Enabled and not Ctx.Db.One("SELECT 1 AS x FROM product_settings WHERE key = ?", (EnvironmentKey,)):
            # The environment's identity, generated once and kept with the data: never a hostname
            Ctx.Products.Set(EnvironmentKey, "atelier-" + secrets.token_hex(5), "system", "Fulfillment integration: this environment's id")
        from p3.fulfillment_notifications import NotificationService
        self.Notifications = NotificationService(Ctx, self)      # Batch 8: the customer notifications derived from the events (off by default)

    @property
    def Enabled(self) -> bool:
        return bool(self.Ctx.Settings.FulfillmentIntegration)

    def _Require(self) -> None:
        if not self.Enabled:
            raise HttpError(409, "fulfillment_disabled", "The Fulfillment integration is not enabled on this site (P3_FULFILLMENT_INTEGRATION).")

    def EnvironmentId(self) -> str | None:
        R = self.Ctx.Db.One("SELECT value_json FROM product_settings WHERE key = ?", (EnvironmentKey,))
        return json.loads(R["value_json"]) if R else None

    def RequireService(self, Authorization: str | None) -> None:
        """The service credential, constant-time; the Admin key is never accepted here, nor a customer token."""
        Expected = self.Ctx.Settings.FulfillmentServiceKey or ""
        Supplied = (Authorization or "").strip()
        Supplied = Supplied[len("Bearer "):].strip() if Supplied.lower().startswith("bearer ") else ""
        if not Expected or not Supplied or not hmac.compare_digest(Supplied.encode(), Expected.encode()):
            raise HttpError(403, "service_auth_required", "The Fulfillment service credential is required.")

    def Identity(self) -> dict:
        return {"contract_version": ContractVersion, "environment_id": self.EnvironmentId(), "site": {"env": self.Ctx.Settings.Env,
                "base_path": self.Ctx.Settings.BasePath or "/"},
                "capabilities": ["changes", "requests", "events", "assets.image", "assets.production_stl", "commands", "release_checks", "shipping_checks"], "server_now": Now()}

    # ── enrollment ───────────────────────────────────────────────────────
    def Enrollment(self, EntityType: str, EntityId: str) -> dict | None:
        return self.Ctx.Db.One("SELECT * FROM fulfillment_enrollments WHERE entity_type = ? AND entity_id = ?", (EntityType, EntityId))

    def IsEnrolled(self, EntityType: str, EntityId: str) -> bool:
        E = self.Enrollment(EntityType, EntityId)
        return bool(E and E["state"] == "enrolled")

    def _Order(self, OrderId: str) -> dict:
        return self.Orders.AdminGet(OrderId)

    def _Quote(self, RequestId: str) -> dict:
        import re
        M = re.fullmatch(r"(?:Q-)?(\d+)", (RequestId or "").strip().upper())
        R = self.Ctx.Db.One("SELECT * FROM quote_requests WHERE id = ? OR request_no = ?", (RequestId, int(M.group(1)) if M else -1))
        if R is None:
            raise HttpError(404, "request_not_found", "Quote request not found.")
        return R

    def Eligibility(self, EntityType: str, EntityId: str) -> tuple[bool, str | None, dict]:
        """Whether the record may be handed to Fulfillment now, with the reason when it may not, and the record."""
        if EntityType == "order":
            O = self._Order(EntityId)
            if O["status"] not in EligibleOrderStatuses:
                return False, f"an order in status '{OrdersModule.StatusLabels.get(O['status'], O['status'])}' stays with the existing flow", O
            return True, None, O
        Q = self._Quote(EntityId)
        return False, "quotations are managed here; once the quote has become an order, the order is sent", Q

    def Enroll(self, EntityType: str, EntityId: str, By: str, Note: str = "", InheritedFrom: str | None = None) -> dict:
        """The explicit hand-over of an order. One row per order, an outbox row in the same transaction, a line in the
        order's history. Repeating it is refused, never duplicated. A quotation is never sent (D-10)."""
        self._Require()
        if EntityType != "order":
            raise HttpError(400, "invalid_entity", "Orders are sent to Fulfillment; a quotation stays here until it becomes an order.")
        Ok, Why, Record = self.Eligibility(EntityType, EntityId)
        Id = Record["id"]
        Ref = Record["ref"]
        if self.IsEnrolled(EntityType, Id):                  # the truer answer first: a record already with Fulfillment is never "not eligible"
            raise HttpError(409, "already_enrolled", f"{Ref} is already with Fulfillment.")
        if not Ok:
            raise HttpError(409, "not_eligible", f"{Ref} cannot be sent to Fulfillment: {Why}.")
        T = Now()
        with self.Ctx.Db.Transaction() as Conn:
            Existing = Conn.execute("SELECT * FROM fulfillment_enrollments WHERE entity_type = ? AND entity_id = ?", (EntityType, Id)).fetchone()
            if Existing and Existing["state"] == "enrolled":
                raise HttpError(409, "already_enrolled", f"{Ref} is already with Fulfillment (since {Existing['enrolled_at']}).")
            if Existing:                                     # released earlier: enrolled again as a new version
                Conn.execute("UPDATE fulfillment_enrollments SET state = 'enrolled', source_version = source_version + 1, enrolled_at = ?, enrolled_by = ?, "
                             "note = ?, inherited_from = ?, released_at = NULL, released_by = NULL, release_note = NULL, updated_at = ? WHERE id = ?",
                             (T, By, (Note or "").strip()[:500] or None, InheritedFrom, T, Existing["id"]))
                Version = Existing["source_version"] + 1
            else:
                Conn.execute("INSERT INTO fulfillment_enrollments (id, entity_type, entity_id, display_ref, state, source_version, enrolled_at, enrolled_by, "
                             "note, inherited_from, updated_at) VALUES (?,?,?,?,'enrolled',1,?,?,?,?,?)",
                             (NewId("fen"), EntityType, Id, Ref, T, By, (Note or "").strip()[:500] or None, InheritedFrom, T))
                Version = 1
            Conn.execute("INSERT INTO fulfillment_outbox (kind, entity_type, entity_id, source_version, reason, created_at) VALUES ('enrolled', ?, ?, ?, ?, ?)",
                         (EntityType, Id, Version, "inherited" if InheritedFrom else "admin", T))
            Data = {"action": "enrolled", "by": By, "note": (Note or "").strip()[:500] or None, "inherited_from": InheritedFrom}
            if EntityType == "order":
                Conn.execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)", (Id, "fulfillment", Dumps(Data), By, T))
            else:
                OrdersModule.QuoteEvent(Conn, Id, "fulfillment", Data, By)
        return self.AdminState(EntityType, Id)

    def GuardStatusChange(self, Order: dict, Status: str) -> None:
        """The legacy production / QC / shipping statuses are Fulfillment's once a record is enrolled (spec §7.5)."""
        if Status in OrdersModule.ProductionStatuses and self.IsEnrolled("order", Order["id"]):
            raise HttpError(409, "owned_by_fulfillment", f"{Order['ref']} is with Fulfillment: production, QC, shipping and completion are recorded "
                                                        "there and reach this page as its progress. Cancellation and payment stay here.")

    # ── commands from the Admin (spec §7.4) ──────────────────────────────
    def Command(self, EntityType: str, EntityId: str, Action: str, Body: dict, By: str) -> dict:
        """A controlled request to Fulfillment: stored here with who asked and the version they saw, delivered through
        the feed Fulfillment reads, answered by an event. Pending until then; a repeated click is the same request."""
        self._Require()
        if EntityType != "order":
            raise HttpError(400, "invalid_entity", "Commands are sent for orders.")
        if Action not in CommandActions:
            raise HttpError(400, "invalid_action", "Unknown command.", {"actions": list(CommandActions)})
        O = self._Order(EntityId)
        E = self.Enrollment("order", O["id"])
        if not (E and E["state"] == "enrolled"):
            raise HttpError(409, "not_enrolled", f"{O['ref']} is not with Fulfillment.")
        LineId = str(Body.get("line_id") or "")
        Line = next((L for L in O["lines"] if L["id"] == LineId), None)
        if Line is None:
            raise HttpError(404, "line_not_found", "No such line on this order.")
        Reason = " ".join(str(Body.get("reason") or "").split())[:500]
        if not Reason:
            raise HttpError(400, "reason_required", "Say why the cancellation is requested (the reason is kept with the order).")
        Qty = Body.get("quantity")
        if Qty is not None and (not isinstance(Qty, int) or isinstance(Qty, bool) or not 1 <= Qty <= Line["quantity"]):
            raise HttpError(400, "invalid_quantity", f"The quantity is between 1 and {Line['quantity']} (or empty for the whole line).")
        T = Now()
        with self.Ctx.Db.Transaction() as Conn:
            for R in Conn.execute("SELECT * FROM fulfillment_commands WHERE entity_type = 'order' AND entity_id = ? AND action = ? AND state = 'pending'",
                                  (O["id"], Action)).fetchall():
                if json.loads(R["payload_json"]).get("line_id") == LineId:
                    return self._CommandJson(dict(R))
            Id = "cmd_" + secrets.token_hex(8)
            Payload = {"command_id": Id, "action": Action, "line_id": LineId, "item_ref": Line["ring_id"], "quantity": Qty, "reason": Reason,
                       "expected_version": E["source_version"], "actor": By, "delegated_permission": CommandActions[Action], "created_at": T}
            Conn.execute("INSERT INTO fulfillment_commands (id, entity_type, entity_id, action, payload_json, expected_version, actor, delegated_permission, state, "
                         "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,'pending',?,?)",
                         (Id, "order", O["id"], Action, Dumps(Payload), E["source_version"], By, CommandActions[Action], T, T))
            Conn.execute("INSERT INTO fulfillment_outbox (kind, entity_type, entity_id, source_version, reason, payload_json, created_at) VALUES ('command', 'order', ?, ?, ?, ?, ?)",
                         (O["id"], E["source_version"], Action, Dumps(Payload), T))
            Conn.execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                         (O["id"], "fulfillment", Dumps({"action": "command", "command_id": Id, "command": Action, "line_id": LineId, "item_ref": Line["ring_id"],
                                                         "quantity": Qty, "reason": Reason, "state": "pending"}), By, T))
        return self.CommandGet(Id)

    def CommandGet(self, CommandId: str) -> dict:
        R = self.Ctx.Db.One("SELECT * FROM fulfillment_commands WHERE id = ?", (CommandId,))
        if R is None:
            raise HttpError(404, "command_not_found", "No such command.")
        return self._CommandJson(R)

    def CommandList(self, EntityType: str, EntityId: str) -> list[dict]:
        return [self._CommandJson(R) for R in self.Ctx.Db.All("SELECT * FROM fulfillment_commands WHERE entity_type = ? AND entity_id = ? ORDER BY created_at DESC",
                                                                (EntityType, EntityId))]

    @staticmethod
    def _CommandJson(R: dict) -> dict:
        Out = {K: V for K, V in R.items() if K not in ("payload_json", "result_json")}
        return {**Out, "payload": json.loads(R["payload_json"]), "result": json.loads(R["result_json"] or "{}")}

    def _ApplyCommandOutcome(self, Conn, E: dict, T: str) -> None:
        """Fulfillment's answer to a command: applied or rejected, with the result. The first answer stands."""
        Public = E.get("public_payload") if isinstance(E.get("public_payload"), dict) else {}
        Cmd = Public.get("command") if isinstance(Public.get("command"), dict) else {}
        Id = Cmd.get("command_id")
        R = Conn.execute("SELECT * FROM fulfillment_commands WHERE id = ?", (Id,)).fetchone() if isinstance(Id, str) else None
        if R is None or R["state"] != "pending":
            return
        State = "applied" if E["event_type"] == "fulfillment.command_applied" else "rejected"
        Conn.execute("UPDATE fulfillment_commands SET state = ?, result_json = ?, decided_at = ?, updated_at = ? WHERE id = ?", (State, Dumps(Cmd), E["occurred_at"], T, Id))
        Data = {"action": "command_outcome", "command_id": Id, "command": R["action"], "state": State, "result": Cmd.get("result"), "reason": Cmd.get("reason"),
                "part": Cmd.get("part")}
        if R["entity_type"] == "order":
            Conn.execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)", (R["entity_id"], "fulfillment", Dumps(Data), "fulfillment", T))

    # ── the live commercial check at release time (spec §15) ─────────────
    def ReleaseChecks(self, Body: dict) -> dict:
        """Payment, cancellation and quantity for the exact lines and quantities about to be released, read now — no
        price is changed, no payment created. Fulfillment stores the answer with its request id."""
        Items = Body.get("items") if isinstance(Body, dict) else None
        if not isinstance(Items, list) or not Items or len(Items) > MaxCheckItems:
            raise HttpError(400, "invalid_items", f"items is a list of 1–{MaxCheckItems} {{line_id, quantity}}.")
        Out, T = [], Now()
        for It in Items:
            It = It if isinstance(It, dict) else {}
            LineId, Qty = str(It.get("line_id") or ""), It.get("quantity")
            L = self.Ctx.Db.One("SELECT * FROM order_lines WHERE id = ?", (LineId,))
            if L is None or not self.IsEnrolled("order", L["order_id"]):
                Out.append({"line_id": LineId, "ok": False, "blockers": [{"code": "LINE_NOT_FOUND", "text": "No such enrolled order line."}]})
                continue
            O = self.Ctx.Db.One("SELECT id, order_no, status, payment_status FROM orders WHERE id = ?", (L["order_id"],))
            E = self.Enrollment("order", O["id"])
            B = []
            if O["payment_status"] != "paid":
                B.append({"code": "PAYMENT_PENDING", "text": f"Payment is {O['payment_status']}; only a paid order releases production."})
            if O["status"] == "cancelled":
                B.append({"code": "ORDER_CANCELLED", "text": "The order is cancelled."})
            if not isinstance(Qty, int) or isinstance(Qty, bool) or Qty < 1:
                B.append({"code": "INVALID_QUANTITY", "text": "The quantity to release is a positive whole number."})
            elif Qty > L["quantity"]:
                B.append({"code": "QUANTITY_EXCEEDS_ORDER", "text": f"{Qty} to release, {L['quantity']} ordered."})
            Out.append({"line_id": LineId, "order_id": O["id"], "order_ref": OrdersModule.OrderRef(O["order_no"]), "ok": not B, "blockers": B,
                        "payment": {"status": O["payment_status"], "basis": "paid" if O["payment_status"] == "paid" else None}, "order_status": O["status"],
                        "ordered_quantity": L["quantity"], "source_version": E["source_version"]})
        return {"contract_version": ContractVersion, "environment_id": self.EnvironmentId(), "check_id": "chk_" + secrets.token_hex(8), "checked_at": T,
                "ok": all(X["ok"] for X in Out), "items": Out}

    # ── the live shipping check at packing time (spec §23, C05) ──────────
    def ShippingChecks(self, Body: dict) -> dict:
        """The authoritative delivery address and its version for an enrolled order, with the order's state, read
        now: Fulfillment packs against the snapshot this answers and keeps it. Nothing is changed here."""
        OrderId = str(Body.get("order_id") or "") if isinstance(Body, dict) else ""
        Base = {"contract_version": ContractVersion, "environment_id": self.EnvironmentId(), "check_id": "shk_" + secrets.token_hex(8), "checked_at": Now(), "order_id": OrderId}
        if not OrderId or not self.IsEnrolled("order", OrderId):
            return {**Base, "ok": False, "blockers": [{"code": "ORDER_NOT_FOUND", "text": "No such enrolled order."}]}
        O = self._Order(OrderId)
        E = self.Enrollment("order", OrderId)
        B = []
        if O["status"] == "cancelled":
            B.append({"code": "ORDER_CANCELLED", "text": "The order is cancelled."})
        if O["payment_status"] != "paid":
            B.append({"code": "PAYMENT_PENDING", "text": f"Payment is {O['payment_status']}."})
        Address = O["address"]
        return {**Base, "ok": not B, "blockers": B, "order_ref": O["ref"], "address": Address, "address_lines": O["address_lines"],
                "address_version": _Sha(Dumps(Address))[:16], "address_validation": O["address_validation"], "shipping_method": O["shipping_method"],
                "shipping_label": O.get("shipping_label"), "order_status": O["status"],
                "payment": {"status": O["payment_status"], "basis": "paid" if O["payment_status"] == "paid" else None},
                "cancellation": {"state": "cancelled" if O["status"] == "cancelled" else "none"}, "source_version": E["source_version"]}

    # ── the changes feed ─────────────────────────────────────────────────
    def Changes(self, Cursor: int, Limit: int) -> dict:
        Limit = max(1, min(int(Limit), MaxChanges))
        Rows = self.Ctx.Db.All("SELECT * FROM fulfillment_outbox WHERE seq > ? ORDER BY seq LIMIT ?", (max(0, int(Cursor)), Limit + 1))
        More = len(Rows) > Limit
        Rows = Rows[:Limit]
        for R in Rows:                                        # a command travels with its payload; other changes are read as snapshots
            P = R.pop("payload_json", None)
            if P:
                R["payload"] = json.loads(P)
        T = Now()
        self.Ctx.Db.Execute("INSERT INTO fulfillment_sync (key, value_json, updated_at) VALUES ('pull', ?, ?) ON CONFLICT(key) DO UPDATE SET "
                            "value_json = excluded.value_json, updated_at = excluded.updated_at", (Dumps({"cursor": int(Cursor), "at": T}), T))
        return {"contract_version": ContractVersion, "environment_id": self.EnvironmentId(), "changes": Rows,
                "next_cursor": Rows[-1]["seq"] if Rows else int(Cursor), "has_more": More, "server_now": T}

    def LastPull(self) -> dict | None:
        R = self.Ctx.Db.One("SELECT value_json FROM fulfillment_sync WHERE key = 'pull'")
        return json.loads(R["value_json"]) if R else None

    # ── snapshots ────────────────────────────────────────────────────────
    def Snapshot(self, EntityType: str, EntityId: str) -> dict:
        """Everything Fulfillment needs about an enrolled order, read-only, without internal notes or history. The
        quotation an order came from is a reference on it (`order.quote`, `lines[].quote_request_id`), never a
        record of its own (D-10)."""
        if EntityType != "order":
            raise HttpError(404, "not_supported", "Only orders are with Fulfillment; a quotation is managed in Atelier.")
        O = self._Order(EntityId)
        E = self.Enrollment("order", O["id"])
        if not E:
            raise HttpError(404, "not_enrolled", "This order is not with Fulfillment.")
        return self._OrderSnapshot(O, E)

    def _Envelope(self, EntityType: str, Id: str, Ref: str, E: dict) -> dict:
        return {"contract_version": ContractVersion, "environment_id": self.EnvironmentId(), "entity_type": EntityType, "entity_id": Id,
                "display_ref": Ref, "source_version": E["source_version"],
                "enrollment": {"state": E["state"], "enrolled_at": E["enrolled_at"], "enrolled_by": E["enrolled_by"], "inherited_from": E["inherited_from"],
                               "released_at": E["released_at"]}, "server_now": Now()}

    def _OrderSnapshot(self, O: dict, E: dict) -> dict:
        Address = O["address"]
        Cancelled = O["status"] == "cancelled"
        CancelEvent = next((Ev for Ev in reversed(O["events"]) if Ev["kind"] == "status" and Ev["data"].get("to") == "cancelled"), None) if Cancelled else None
        PayEvent = next((Ev for Ev in reversed(O["events"]) if Ev["kind"] == "payment"), None)
        Lines = []
        for I, L in enumerate(O["lines"], start=1):
            Size = {"ring_size": L["ring_size"]} if L["product_type"] == "ring" else {"charm_size": L["charm_size"], "unit": "mm"}
            Lines.append({"line_id": L["id"], "position": I, "design_id": L["design_id"], "candidate_id": L["candidate_id"], "item_ref": L["ring_id"],
                          "title": L["title"], "product_type": L["product_type"], "material_id": L["material_id"], "material_label": L["material_label"],
                          "size": {**Size, "label": L["size_label"]}, "quantity": L["quantity"], "unit_price": L["unit_price"], "currency": L["currency"],
                          "quote_request_id": O["quote"]["id"] if O.get("quote") else None,
                          "image": {"design_id": L["design_id"], "candidate_id": L["candidate_id"]}, "model": self._Model(L)})
        return {**self._Envelope("order", O["id"], O["ref"], E),
                "order": {"status": O["status"], "placed_at": O["created_at"], "updated_at": O["updated_at"],
                          "payment": {"status": O["payment_status"], "basis": "paid" if O["payment_status"] == "paid" else None,
                                      "provider": O["payment_provider"], "ref": O.get("payment_ref"),
                                      "updated_at": PayEvent["created_at"] if PayEvent else O["created_at"]},
                          "customer": {"name": (O["customer"].get("first_name", "") + " " + O["customer"].get("last_name", "")).strip(),
                                       "email": O["customer"].get("email")},
                          "address": Address, "address_lines": O["address_lines"], "address_version": _Sha(Dumps(Address))[:16],
                          "address_validation": O["address_validation"], "shipping_method": O["shipping_method"], "shipping_label": O.get("shipping_label"),
                          "currency": O["currency"], "subtotal": O["subtotal"], "total": O["total"],
                          "cancellation": {"state": "cancelled" if Cancelled else "none", "at": CancelEvent["created_at"] if CancelEvent else None,
                                           "reason": (CancelEvent["data"].get("note") or None) if CancelEvent else None},
                          "quote": O.get("quote"), "promised_delivery": None},
                "lines": Lines}

    def _Model(self, L: dict) -> dict:
        """What Atelier knows about the production model of a line: available only when a measured, unflagged (or
        accepted) result exists for the ordered size and material. Never invented; a missing model says so."""
        Info = self.Orders._ThreeDForLine(L)
        Sid, Match, State = Info["three_d_id"], Info["three_d_match"], Info["three_d_state"]
        Out = {"available": False, "state": "no_model", "session_3d_id": None, "mesh_id": None, "raw_sha256": None, "method_version": None,
               "integrity": None, "production_size": None, "material_id": None, "accepted": None, "review": [], "geometry": None, "weight": None,
               "latest_other": Info.get("three_d_latest"), "model_item_ref": Info.get("model_ring_id"),
               "note": "No 3D model exists for this design yet: it is generated in Atelier (a paid Hi3D request) on the session page."}
        if not Sid:
            if Info.get("has_model"):
                Out.update({"state": "not_prepared", "note": "A 3D model exists for this design but is not yet prepared for the ordered size and material; "
                                                             "Fulfillment's file request prepares it (no paid call)."})
            return Out
        G = self.Production.Get(Sid)
        Raw = self.Ctx.Db.One("SELECT sha256, method_version, integrity FROM raw_geometry WHERE mesh_id = ?", (G["mesh_id"],)) if G.get("mesh_id") else None
        Prod = (G.get("geometry") or {}).get("production") or {}
        Price = G.get("price") or {}
        Out.update({"state": State if Match else "other_size", "session_3d_id": Sid, "mesh_id": G.get("mesh_id"),
                    "raw_sha256": Raw["sha256"] if Raw else None, "method_version": Raw["method_version"] if Raw else None,
                    "integrity": Raw["integrity"] if Raw else None, "production_size": G.get("production_size"), "material_id": G.get("material_id"),
                    "accepted": G.get("accepted"), "review": [I["code"] for I in (G.get("review") or [])],
                    "geometry": {"x_mm": Prod.get("size_x_mm"), "y_mm": Prod.get("size_y_mm"), "z_mm": Prod.get("size_z_mm"),
                                 "inner_diameter_mm": Prod.get("inner_diameter_mm"), "volume_mm3": Prod.get("volume_mm3"),
                                 "surface_area_mm2": Prod.get("surface_area_mm2"), "watertight": bool(Prod.get("watertight")) if Prod else None,
                                 "method_version": Prod.get("method_version")} if Prod else None,
                    "weight": {"g": Price.get("weight_g"), "density_g_cm3": Price.get("density_g_cm3"),
                               "source": f"calculated: volume × density ({Price.get('fixed_price_version') or 'Atelier material table'})"} if Price.get("weight_g") else None})
        if Match and State == "complete":
            Out.update({"available": True, "note": "Measured and unflagged (or accepted) for the ordered size and material."})
        elif Match and State == "review_required":
            Out["note"] = "The 3D result is flagged for production review in Atelier (" + ", ".join(Out["review"]) + "): accept it there first."
        elif Match:
            Out["note"] = f"The 3D result is {State} in Atelier."
        else:
            Out["note"] = "The newest 3D result is for another size or material; Fulfillment's file request prepares the ordered one (no paid call)."
        return Out

    # ── restricted assets ────────────────────────────────────────────────
    def ImagePath(self, DesignId: str, CandidateId: str):
        R = self.Ctx.Db.One("SELECT c.asset_path FROM candidates c JOIN batches b ON b.id = c.batch_id WHERE c.id = ? AND b.design_id = ? AND c.status = 'ready'",
                            (CandidateId, DesignId))
        if not R or not R["asset_path"]:
            raise HttpError(404, "image_not_found", "No such image.")
        if not self.IsEnrolled("order", self._OrderOfDesign(DesignId) or ""):
            raise HttpError(404, "image_not_found", "No such image.")
        try:
            P = assets.Resolve(self.Ctx.Settings.AssetsDir, R["asset_path"])
        except assets.AssetError:
            raise HttpError(404, "image_not_found", "No such image.")
        if not P.is_file():
            raise HttpError(404, "image_not_found", "No such image.")
        return P, assets.ImageContentType(R["asset_path"])

    def _OrderOfDesign(self, DesignId: str) -> str | None:
        R = self.Ctx.Db.One("SELECT l.order_id FROM order_lines l JOIN fulfillment_enrollments e ON e.entity_type = 'order' AND e.entity_id = l.order_id "
                            "AND e.state = 'enrolled' WHERE l.design_id = ? LIMIT 1", (DesignId,))
        return R["order_id"] if R else None

    def StartProductionStl(self, LineId: str) -> dict:
        """The production file for an enrolled order line: the design's model scaled to the ordered size and material
        (prepared by arithmetic when it does not exist yet — never a paid request), exported as a temporary STL."""
        L = self.Ctx.Db.One("SELECT order_id FROM order_lines WHERE id = ?", (LineId,))
        if L is None or not self.IsEnrolled("order", L["order_id"]):
            raise HttpError(404, "line_not_found", "No such enrolled order line.")
        O = self._Order(L["order_id"])
        Line = next(X for X in O["lines"] if X["id"] == LineId)
        if not Line["three_d_match"]:
            if not Line["has_model"]:
                return {"status": "unavailable", "reason": "no_model", "message": "No 3D model exists for this design; it is generated in Atelier (paid)."}
            try:
                self.Orders.PrepareLine3D(O["id"], LineId, "fulfillment")
            except HttpError as E:
                return {"status": "unavailable", "reason": E.Code, "message": E.Message}
            Line = next(X for X in self._Order(O["id"])["lines"] if X["id"] == LineId)
        if Line["three_d_state"] != "complete":
            return {"status": "unavailable", "reason": Line["three_d_state"], "session_3d_id": Line["three_d_id"],
                    "message": "The 3D result must be measured and unflagged (or accepted) in Atelier before the production file is released."}
        Sid = Line["three_d_id"]
        Export = self.Production.StartExport(Sid)
        Out = {"status": Export["status"], "job_id": Export["job_id"], "session_3d_id": Sid, "bytes": Export.get("bytes"), "ahead": Export.get("ahead"),
               "expires_at": Export.get("expires_at"), "error": Export.get("error")}
        if Export["status"] == "done":
            Out["sha256"] = self._ExportSha(Sid, Export["job_id"])
        return Out

    def ExportStatus(self, Sid: str, Jid: str) -> dict:
        S = self.Production.StoredProduction(Sid) if Jid == "stored" else self.Production.ExportStatus(Sid, Jid)
        if S is None:
            raise HttpError(404, "job_not_found", "Export not found.")
        Out = {"status": S["status"], "job_id": S["job_id"], "session_3d_id": Sid, "bytes": S.get("bytes"), "ahead": S.get("ahead"), "error": S.get("error")}
        if S["status"] == "done":
            Out["sha256"] = self._ExportSha(Sid, S["job_id"])
        return Out

    def _ExportPath(self, Sid: str, Jid: str):
        return self.Production.FilePath(Sid, "production" if Jid == "stored" else f"export-{Jid}")

    def _ExportSha(self, Sid: str, Jid: str) -> str:
        P = self._ExportPath(Sid, Jid)
        Key = (str(P), P.stat().st_mtime_ns, P.stat().st_size)
        if Key not in self._ShaCache:
            H = hashlib.sha256()
            with open(P, "rb") as F:
                for Chunk in iter(lambda: F.read(1024 * 1024), b""):
                    H.update(Chunk)
            self._ShaCache = {Key: H.hexdigest()}             # one entry is enough: files are fetched one at a time
        return self._ShaCache[Key]

    # ── events from Fulfillment ──────────────────────────────────────────
    def ReceiveEvents(self, Events) -> list[dict]:
        if not isinstance(Events, list) or len(Events) > MaxEvents:
            raise HttpError(400, "invalid_events", f"events is a list of at most {MaxEvents} events.")
        Out = []
        for E in Events:
            Out.append(self._ReceiveEvent(E))
        return Out

    def _ReceiveEvent(self, E) -> dict:
        if not isinstance(E, dict) or not isinstance(E.get("event_id"), str) or not 1 <= len(E["event_id"]) <= 120:
            return {"event_id": E.get("event_id") if isinstance(E, dict) else None, "status": "rejected", "error": "event_id missing"}
        Id = E["event_id"]
        if E.get("schema_version") != 1:
            return {"event_id": Id, "status": "rejected", "error": "unsupported schema_version"}
        for F in ("event_type", "aggregate_type", "aggregate_id", "occurred_at"):
            if not isinstance(E.get(F), str) or not E[F]:
                return {"event_id": Id, "status": "rejected", "error": f"{F} missing"}
        Version = E.get("aggregate_version")
        if not isinstance(Version, int) or isinstance(Version, bool) or Version < 0:
            return {"event_id": Id, "status": "rejected", "error": "aggregate_version missing"}
        Entity = E.get("source_entity") or {}
        EntityType, EntityId = Entity.get("type"), Entity.get("id")
        Hash = _Sha(Dumps(E))
        T = Now()
        with self.Ctx.Db.Transaction() as Conn:
            Existing = Conn.execute("SELECT payload_hash, result FROM fulfillment_inbox WHERE event_id = ?", (Id,)).fetchone()
            if Existing:
                if Existing["payload_hash"] == Hash:
                    return {"event_id": Id, "status": "duplicate", "result": Existing["result"]}
                Conn.execute("INSERT INTO fulfillment_inbox_conflicts (event_id, payload_hash, payload_json, received_at) VALUES (?,?,?,?)", (Id, Hash, Dumps(E), T))
                return {"event_id": Id, "status": "conflict", "error": "an event with this id was received before with a different payload"}
            Result = "unknown_entity"
            if E["event_type"] in CommandOutcomeEvents:
                self._ApplyCommandOutcome(Conn, E, T)
            if EntityType == "order" and EntityId and Conn.execute(
                    "SELECT 1 FROM fulfillment_enrollments WHERE entity_type = ? AND entity_id = ?", (EntityType, EntityId)).fetchone():
                Result = self._Project(Conn, E, EntityType, EntityId, T)
            Conn.execute("INSERT INTO fulfillment_inbox (event_id, event_type, aggregate_type, aggregate_id, aggregate_version, entity_type, entity_id, "
                         "payload_hash, payload_json, result, received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (Id, E["event_type"], E["aggregate_type"], E["aggregate_id"], Version, EntityType, EntityId, Hash, Dumps(E), Result, T))
        return {"event_id": Id, "status": "applied" if Result == "applied" else "accepted", "result": Result}

    def _Project(self, Conn, E: dict, EntityType: str, EntityId: str, T: str) -> str:
        """The projection: the newest version of each aggregate wins; an older event never rolls a newer state back."""
        Agg = f"{E['aggregate_type']}:{E['aggregate_id']}"
        Row = Conn.execute("SELECT * FROM fulfillment_projection WHERE entity_type = ? AND entity_id = ?", (EntityType, EntityId)).fetchone()
        State = json.loads(Row["state_json"]) if Row else {}
        Versions = State.get("aggregate_versions") or {}
        if Agg in Versions and int(E["aggregate_version"]) <= int(Versions[Agg]):
            return "ignored_stale"
        Versions[Agg] = int(E["aggregate_version"])
        Public = E.get("public_payload") if isinstance(E.get("public_payload"), dict) else {}
        State.update({K: V for K, V in Public.items() if K in ("customer_status", "summary", "parts", "review_status", "blockers", "file_state", "shipment", "shipments",
                                                                 "order_status", "review", "commercial_action_pending", "commercial_action")})
        State.update({"aggregate_versions": Versions, "last_event_type": E["event_type"], "last_event_at": E["occurred_at"], "last_received_at": T})
        if EntityType == "order" and Public.get("order_status") in ProjectionStatuses:
            self._ProjectStatus(Conn, EntityId, Public["order_status"], T)
        Summary = str(Public.get("summary") or E.get("summary") or E["event_type"])[:300]
        if Row:
            Conn.execute("UPDATE fulfillment_projection SET aggregate_version = ?, state_json = ?, last_event_id = ?, last_event_at = ?, updated_at = ? "
                         "WHERE entity_type = ? AND entity_id = ?", (int(E["aggregate_version"]), Dumps(State), E["event_id"], E["occurred_at"], T, EntityType, EntityId))
        else:
            Conn.execute("INSERT INTO fulfillment_projection (entity_type, entity_id, aggregate_version, state_json, last_event_id, last_event_at, updated_at) "
                         "VALUES (?,?,?,?,?,?,?)", (EntityType, EntityId, int(E["aggregate_version"]), Dumps(State), E["event_id"], E["occurred_at"], T))
        Data = {"event_type": E["event_type"], "summary": Summary, "event_id": E["event_id"], "at": E["occurred_at"]}
        if EntityType == "order":
            Conn.execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)", (EntityId, "fulfillment", Dumps(Data), "fulfillment", T))
            self.Notifications.Intend(Conn, EntityId, E, Public, T)       # a milestone becomes one intent per scope, in this transaction (C09)
        else:
            OrdersModule.QuoteEvent(Conn, EntityId, "fulfillment", Data, "fulfillment")
        return "applied"

    # ── what the customer sees in My Account (spec §24.1, C08) ───────────
    CustomerLabels = {"fulfillment.request_enrolled": "Received for production review", "fulfillment.review_approved": "Approved for production",
                      "fulfillment.review_changes_required": "Changes required", "fulfillment.review_cannot_produce": "Changes required",
                      "fulfillment.production_started": "In production", "fulfillment.production_completed": "Quality check",
                      "fulfillment.shipment_packed": "Ready to ship", "fulfillment.shipment_shipped": "Shipped", "fulfillment.shipment_delivered": "Delivered",
                      "fulfillment.cancellation_accepted": "Cancelled"}

    def CustomerView(self, OrderId: str) -> dict | None:
        """The order's Fulfillment stage, each part's stage, the parcels with their tracking, and a timeline of the
        milestones that concern the customer — from the projection (customer-safe by contract) and the fixed labels
        above; never an internal note, a queue, a cost, a reason code or a file link. None when the order is not with
        Fulfillment, so nothing changes for the others."""
        if not self.Enabled:
            return None
        E = self.Enrollment("order", OrderId)
        if not (E and E["state"] == "enrolled"):
            return None
        Proj = self.Projection("order", OrderId) or {}
        Parts = [{"display_no": P.get("display_no"), "stage": P.get("customer_stage")} for P in (Proj.get("parts") or []) if isinstance(P, dict)]
        Message = next((P["review"].get("customer_message") for P in (Proj.get("parts") or []) if isinstance(P, dict) and isinstance(P.get("review"), dict)
                        and P["review"].get("decision") in ("changes_required", "cannot_produce") and P["review"].get("customer_message")), None)
        Shipments = [{K: S.get(K) for K in ("display_no", "state", "carrier", "tracking_no", "tracking_url", "shipped_at", "delivered_at", "alternative_delivery")}
                     for S in (Proj.get("shipments") or []) if isinstance(S, dict)]
        Timeline, Last = [], None
        for R in self.Ctx.Db.All("SELECT data_json, created_at FROM order_events WHERE order_id = ? AND kind = 'fulfillment' ORDER BY id", (OrderId,)):
            D = json.loads(R["data_json"] or "{}")
            Label = self.CustomerLabels.get(D.get("event_type"))
            if Label and Label != Last:
                Timeline.append({"at": D.get("at") or R["created_at"], "label": Label, "status": D.get("event_type")})
                Last = Label
        return {"stage": Proj.get("customer_status"), "parts": Parts, "shipments": Shipments, "timeline": Timeline, "message": Message}

    def _ProjectStatus(self, Conn, OrderId: str, Target: str, T: str) -> None:
        """Fulfillment's progress written into the order's status (D-4): production → qc → shipped → completed, forward
        only, on the same connection as the projection; a cancelled or completed order is left alone; the trigger
        ignores these statuses, so nothing goes back out (no loop)."""
        Row = Conn.execute("SELECT status FROM orders WHERE id = ?", (OrderId,)).fetchone()
        Old = Row["status"] if Row else None
        if Old is None or Old == Target or Old in ("cancelled", "completed"):
            return
        if Old in OrdersModule.StatusOrder and OrdersModule.StatusOrder.index(Target) <= OrdersModule.StatusOrder.index(Old):
            return
        Conn.execute("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?", (Target, T, OrderId))
        Conn.execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                     (OrderId, "status", Dumps({"from": Old, "to": Target, "note": "Fulfillment progress"}), "fulfillment", T))

    def Projection(self, EntityType: str, EntityId: str) -> dict | None:
        R = self.Ctx.Db.One("SELECT * FROM fulfillment_projection WHERE entity_type = ? AND entity_id = ?", (EntityType, EntityId))
        if not R:
            return None
        return {**json.loads(R["state_json"]), "last_event_id": R["last_event_id"], "last_event_at": R["last_event_at"], "updated_at": R["updated_at"]}

    # ── what the Admin sees ──────────────────────────────────────────────
    def _Awaiting(self, EntityType: str, Id: str, E: dict | None, Pull: dict | None) -> bool:
        if not (E and E["state"] == "enrolled"):
            return False
        Newest = self.Ctx.Db.One("SELECT MAX(seq) AS s FROM fulfillment_outbox WHERE entity_type = ? AND entity_id = ?", (EntityType, Id))
        return bool(Newest and Newest["s"] and (not Pull or Newest["s"] > Pull["cursor"]))

    def AdminState(self, EntityType: str, EntityId: str) -> dict:
        if not self.Enabled:
            return {"enabled": False}
        Ok, Why, Record = self.Eligibility(EntityType, EntityId)
        if EntityType == "quote_request":
            # A quotation is never with Fulfillment; the order it became may be, and that is what the quote page shows
            Order = self.Ctx.Db.One("SELECT id, order_no FROM orders WHERE id = ?", (Record["order_id"],)) if Record.get("order_id") else None
            return {"enabled": True, "quotation": True, "enrolled": False, "eligible": False, "ineligible_reason": Why,
                    "order": {"id": Order["id"], "ref": OrdersModule.OrderRef(Order["order_no"])} if Order else None,
                    "order_fulfillment": self.Summary("order", Order["id"]) if Order else None}
        Id = Record["id"]
        E = self.Enrollment("order", Id)
        Pull = self.LastPull()
        return {"enabled": True, "enrolled": bool(E and E["state"] == "enrolled"), "eligible": Ok and not (E and E["state"] == "enrolled"),
                "ineligible_reason": Why, "enrollment": E, "awaiting_sync": self._Awaiting("order", Id, E, Pull), "last_pull_at": Pull["at"] if Pull else None,
                "projection": self.Projection("order", Id), "commands": self.CommandList("order", Id), "command_actions": list(CommandActions),
                "notifications": self.Notifications.List(OrderId=Id, Limit=20)}

    def Summary(self, EntityType: str, EntityId: str) -> dict | None:
        """One list row's Fulfillment column: the stage, the worst review state, the first blocker, pending commands and
        commercial action, whether Fulfillment has read the latest change. None when the record is not with Fulfillment."""
        E = self.Enrollment(EntityType, EntityId)
        if not (E and E["state"] == "enrolled"):
            return None
        Proj = self.Projection(EntityType, EntityId) or {}
        Parts = [P for P in (Proj.get("parts") or []) if isinstance(P, dict)]
        Reviews = [P.get("review_status") for P in Parts if P.get("review_status") in ReviewSeverity]
        Review = min(Reviews, key=ReviewSeverity.index) if Reviews else None
        Blocker = next((B for P in Parts for B in (P.get("blockers") or []) if isinstance(B, str)), None)
        Pending = self.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_commands WHERE entity_type = ? AND entity_id = ? AND state = 'pending'", (EntityType, EntityId))["n"]
        return {"enrolled": True, "enrolled_at": E["enrolled_at"], "customer_status": Proj.get("customer_status"), "order_status": Proj.get("order_status"),
                "review_status": Review, "blocker": Blocker, "commercial_action_pending": bool(Proj.get("commercial_action_pending")), "pending_commands": Pending,
                "last_event_at": Proj.get("last_event_at"), "awaiting_sync": self._Awaiting(EntityType, EntityId, E, self.LastPull()),
                "parts": [{"display_no": P.get("display_no"), "status": P.get("status"), "summary": P.get("summary"), "file_state": P.get("file_state")} for P in Parts]}

    def Decorate(self, Rows: list[dict], EntityType: str) -> list[dict]:
        """The Fulfillment column on a page of orders, or of quote requests (a quotation shows its order's state once
        it has become one; before that there is nothing to show — D-10)."""
        if not self.Enabled:
            return Rows
        for R in Rows:
            OrderId = R["id"] if EntityType == "order" else R.get("order_id")
            R["fulfillment"] = self.Summary("order", OrderId) if OrderId else None
        return Rows

    @staticmethod
    def FilterLabels() -> dict:
        return dict(Filters)

    def FilterIds(self, Filter: str) -> list[str]:
        """The enrolled orders a Fulfillment filter of the Orders screen selects (computed from the projection)."""
        if Filter not in Filters:
            raise HttpError(400, "invalid_filter", "Unknown Fulfillment filter.", {"filters": list(Filters)})
        Out = []
        for E in self.Ctx.Db.All("SELECT entity_id FROM fulfillment_enrollments WHERE entity_type = 'order' AND state = 'enrolled'"):
            S = self.Summary("order", E["entity_id"]) or {}
            Statuses = [P.get("status") for P in S.get("parts") or []]
            Keep = {"enrolled": True, "review": S.get("review_status") in ("not_started", "in_review"),
                    "changes": S.get("review_status") in ("changes_required", "cannot_produce"),
                    "blocked": any(X in ("blocked", "awaiting_payment") for X in Statuses),
                    "in_progress": any(X in ("ready", "in_production", "qc") for X in Statuses),
                    "ready_to_ship": any(X in ("ready_to_ship", "partially_shipped") for X in Statuses),
                    "shipped": bool(Statuses) and all(X in ("shipped", "delivered", "completed") for X in Statuses),
                    "commercial_action": bool(S.get("commercial_action_pending")), "awaiting_sync": bool(S.get("awaiting_sync"))}[Filter]
            if Keep:
                Out.append(E["entity_id"])
        return Out


def RegisterBridge(App_: FastAPI, Ctx: Context, Bridge: FulfillmentBridge) -> None:
    def Service(Authorization: str | None) -> None:
        Bridge.RequireService(Authorization)

    def Admin(Authorization: str | None):
        return RequireAdmin(Ctx, Authorization)

    @App_.get(Prefix + "/identity")
    async def Identity(authorization: str | None = Header(None)):
        Service(authorization)
        return Bridge.Identity()

    @App_.get(Prefix + "/changes")
    async def Changes(cursor: int = 0, limit: int = 100, authorization: str | None = Header(None)):
        Service(authorization)
        return Bridge.Changes(cursor, limit)

    @App_.get(Prefix + "/requests/{EntityType}/{EntityId}")
    async def Snapshot(EntityType: str, EntityId: str, authorization: str | None = Header(None)):
        Service(authorization)
        return Bridge.Snapshot(EntityType, EntityId)

    @App_.post(Prefix + "/events")
    async def Events(Body_: dict = Body(...), authorization: str | None = Header(None)):
        Service(authorization)
        return {"results": Bridge.ReceiveEvents(Body_.get("events")), "server_now": Now()}

    @App_.post(Prefix + "/release-checks")
    async def ReleaseChecks(Body_: dict = Body(...), authorization: str | None = Header(None)):
        Service(authorization)
        return Bridge.ReleaseChecks(Body_)

    @App_.post(Prefix + "/shipping-checks")
    async def ShippingChecks(Body_: dict = Body(...), authorization: str | None = Header(None)):
        Service(authorization)
        return Bridge.ShippingChecks(Body_)

    @App_.get(Prefix + "/assets/image/{DesignId}/{CandidateId}")
    async def Image(DesignId: str, CandidateId: str, authorization: str | None = Header(None)):
        Service(authorization)
        Path_, Ctype = Bridge.ImagePath(DesignId, CandidateId)
        return FileResponse(Path_, media_type=Ctype, headers={"Cache-Control": "private, no-store"})

    @App_.post(Prefix + "/assets/production-stl")
    async def ProductionStl(Body_: dict = Body(...), authorization: str | None = Header(None)):
        Service(authorization)
        return Bridge.StartProductionStl(str(Body_.get("line_id") or ""))

    @App_.get(Prefix + "/assets/production-stl/{Sid}/{Jid}")
    async def ProductionStlFile(Sid: str, Jid: str, authorization: str | None = Header(None)):
        Service(authorization)
        S = Bridge.ExportStatus(Sid, Jid)
        if S["status"] != "done":
            return JSONResponse(status_code=202 if S["status"] in ("queued", "running") else 409, content=S)
        P = Bridge._ExportPath(Sid, Jid)
        return FileResponse(P, media_type="model/stl", filename=f"{Sid}.stl",
                            headers={"Cache-Control": "private, no-store", "X-Content-Sha256": S["sha256"], "X-Content-Bytes": str(P.stat().st_size)})

    # ── the Admin's hand-over and its view of the state ──
    @App_.get("/api/admin/orders/{OrderId}/fulfillment")
    async def OrderFulfillment(OrderId: str, authorization: str | None = Header(None)):
        Admin(authorization)
        return Bridge.AdminState("order", OrderId)

    # ── the customer notifications derived from the events (Batch 8): what was intended, what was sent, what is unknown ──
    @App_.get("/api/admin/fulfillment/notifications")
    async def Notifications(state: str | None = None, order_id: str | None = None, authorization: str | None = Header(None)):
        Admin(authorization)
        N = Bridge.Notifications
        return {"enabled": N.Enabled, "recipient_override": Ctx.Settings.FulfillmentNotificationRecipient, "window_s": Ctx.Settings.FulfillmentNotificationWindowS,
                "counts": N.Counts(), "intents": N.List(state or None, order_id or None)}

    @App_.post("/api/admin/fulfillment/notifications/dispatch")
    async def DispatchNotifications(authorization: str | None = Header(None)):
        Admin(authorization)
        return Bridge.Notifications.Dispatch()

    @App_.post("/api/admin/fulfillment/notifications/{IntentId}/retry")
    async def RetryNotification(IntentId: str, Body_: dict = Body(default={}), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        return Bridge.Notifications.Retry(IntentId, Who.Id, str(Body_.get("note") or ""))

    @App_.post("/api/admin/fulfillment/notifications/{IntentId}/resolve")
    async def ResolveNotification(IntentId: str, Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        return Bridge.Notifications.Resolve(IntentId, Who.Id, str(Body_.get("note") or ""))

    @App_.post("/api/admin/orders/{OrderId}/fulfillment/enroll")
    async def EnrollOrder(OrderId: str, Body_: dict = Body(default={}), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        return Bridge.Enroll("order", OrderId, Who.Id, str(Body_.get("note") or ""))

    @App_.get("/api/admin/orders/{OrderId}/fulfillment/commands")
    async def OrderCommands(OrderId: str, authorization: str | None = Header(None)):
        Admin(authorization)
        return {"commands": Bridge.CommandList("order", Bridge._Order(OrderId)["id"])}

    @App_.post("/api/admin/orders/{OrderId}/fulfillment/commands")
    async def OrderCommand(OrderId: str, Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        return Bridge.Command("order", OrderId, str(Body_.get("action") or ""), Body_, Who.Id)

    @App_.get("/api/admin/quote-requests/{RequestId}/fulfillment")
    async def QuoteFulfillment(RequestId: str, authorization: str | None = Header(None)):
        """What the quote page shows: never an enrollment of the quotation itself, the state of its order once it exists (D-10)."""
        Admin(authorization)
        return Bridge.AdminState("quote_request", RequestId)
