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
from p3.testrecords import IsTestCustomer
from p3.admin import RequireAdmin
from p3.context import Context, HttpError
from p3.db import Dumps, NewId, Now

ContractVersion = "atelier-fulfillment/v1"
EnvironmentKey = "fulfillment_environment_id"
Prefix = "/api/integrations/fulfillment/v1"
ProjectionStatuses = ("production", "qc", "shipped", "completed")    # written by the projection; never a commercial change
EligibleOrderStatuses = ("new", "payment_confirmed", "three_d_ready")   # a quotation is never enrolled (D-10): its order is
MaxChanges, MaxEvents, MaxCheckItems = 500, 200, 100
# Why an order line cannot be sent to Production now (docs/order-to-production-review.md §B.2): the page shows the
# message under the disabled button, the server answers it as 409 print_file_required / already_sent
ProductionReasons = {
    "already_sent": "already sent to Production",
    "order_cancelled": "the order is cancelled",
    "order_completed": "the order is completed",
    "legacy_production": "the order is in the existing production flow (set here by hand) and stays there",
    "no_model": "no 3D model exists for this design yet (it is generated on the session page — a paid Hi3D request)",
    "not_prepared": "the production STL is not prepared for the ordered size and material: Prepare STL on this line first",
    "not_pinned": "the line is not pinned to a 3D version yet: Prepare STL on this line first",
    "processing": "the 3D result is still being measured",
    "review_required": "the 3D result is flagged for production review: accept it on the session page first",
    "failed": "the 3D result failed; prepare it again",
    "cancelled": "the 3D result was cancelled; prepare it again",
    "other_size": "the prepared result is for another size or material: Prepare STL on this line",
    "export_failed": "the production file cannot be exported",
}
# The controlled phase's allowlist says no (P3_FULFILLMENT_ORDER_ALLOWLIST): an order-level rule, never a print-file
# problem — a line keeps its own verdict (`print_ready`) beside it (the owner's question about ORD-10001, 2026-10-13)
NotEnabledReason = ("Production is not enabled for this order during the controlled phase — only the orders on the integration's allowlist "
                    "(P3_FULFILLMENT_ORDER_ALLOWLIST) are sent")
# The stages Fulfillment reports per part (its admin payload; docs/order-to-production-review.md §I), in order of progress —
# `sent` is Atelier's own: the line is sent and Fulfillment has not reported its part yet
ProductionStageLabels = {
    "sent": "Sent to Production", "received": "Received by Fulfillment", "attention": "Attention required", "rejected": "Rejected — cannot produce",
    "on_hold": "On hold", "planned": "Production planned", "in_production": "In production", "post_processing": "Post-processing", "qc": "QC",
    "rework": "Rework / reprint", "ready_to_ship": "Ready to ship", "partially_shipped": "Partially shipped", "shipped": "Shipped",
    "delivered": "Delivered", "cancelled": "Cancelled",
}
LineEventLabels = {"production_sent": "Sent to Production", "production_file": "Production file recorded", "production_revision_submitted": "Revised file submitted to Production",
                   "3d_prepared": "STL prepared", "3d_pinned": "3D version pinned", "3d_version_changed": "3D version changed", "3d_version_uploaded": "Revised STL uploaded"}
FulfillmentEventLabels = {
    "fulfillment.request_enrolled": "Received by Fulfillment", "fulfillment.request_updated": "Updated", "fulfillment.review_started": "Manufacturing review started",
    "fulfillment.review_approved": "Approved for production", "fulfillment.review_changes_required": "Changes required", "fulfillment.review_cannot_produce": "Cannot produce",
    "fulfillment.commercial_action_required": "Commercial action required", "fulfillment.commercial_action_closed": "Commercial action closed",
    "fulfillment.command_applied": "Request applied", "fulfillment.command_rejected": "Request rejected",
    "fulfillment.production_released": "Released to a build", "fulfillment.production_started": "Production started", "fulfillment.production_completed": "Production completed",
    "fulfillment.production_failed": "Production failed", "fulfillment.production_withdrawn": "Release withdrawn", "fulfillment.qc_recorded": "QC recorded",
    "fulfillment.qc_corrected": "QC corrected", "fulfillment.reprint_planned": "Reprint planned", "fulfillment.cancellation_accepted": "Cancellation accepted",
    "fulfillment.cancellation_rejected": "Cancellation rejected", "fulfillment.shipment_packed": "Packed", "fulfillment.shipment_shipped": "Shipped",
    "fulfillment.shipment_delivered": "Delivered", "fulfillment.shipment_updated": "Tracking updated", "fulfillment.shipment_exception": "Shipping exception",
    "fulfillment.shipment_cancelled": "Parcel cancelled", "fulfillment.revision_received": "Revised file received", "fulfillment.attention_required": "Attention required",
    "fulfillment.attention_cleared": "Attention request closed", "fulfillment.note_added": "Internal note",
}

# The timeline's technical entries (the owner's request of 2026-10-13, §5): the synchronisation's generic "Updated", the
# production file recorded, the version pinned — kept, shown under "Detailed history" only; nothing is deleted
TechnicalTimelineKinds = frozenset({"fulfillment.request_updated", "production_file", "3d_pinned"})


def _LineEventText(Kind: str, D: dict) -> str:
    Sha = (D.get("file_sha256") or "")[:12]
    if Kind == "production_sent":
        return " · ".join(X for X in (D.get("version_label"), f"file {Sha}…" if Sha else None, D.get("note")) if X)
    if Kind == "production_file":
        return f"file {Sha}… ({D.get('file_bytes')} bytes)"
    if Kind == "production_revision_submitted":
        # The hand-over number (how many times Atelier sent this line), never called a revision: Fulfillment numbers its own
        # production revisions (file + size + material + profile), so a profile assignment there is a revision too
        return " · ".join(X for X in (f"hand-over {D.get('handoff_no')}", D.get("version_label"), D.get("reason")) if X)
    if Kind == "3d_version_changed":
        return f"v{D.get('from_version_no') or '?'} → {D.get('label') or 'v' + str(D.get('version_no') or '?')}" + (f": {D['note']}" if D.get("note") else "")
    if Kind == "3d_version_uploaded":
        return f"v{D.get('version_no')} · {D.get('filename') or ''}"
    return "v" + str(D.get("version_no") or "?")
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

-- Order → Production (docs/order-to-production-review.md): the per-line hand-over and the exact file it pins. One row
-- per order line ever sent; what happened (sent, the file recorded, revised, received) is in order_events. A row is
-- never deleted: there is no un-send (K7) — a part leaves production through the cancellation request Fulfillment decides.
CREATE TABLE IF NOT EXISTS fulfillment_line_production (
    id                  TEXT PRIMARY KEY,                -- flp_…
    order_id            TEXT NOT NULL,
    line_id             TEXT NOT NULL UNIQUE,
    sent_at             TEXT NOT NULL,
    sent_by             TEXT NOT NULL,
    note                TEXT,
    handoff_no          INTEGER NOT NULL DEFAULT 1,      -- 1, then +1 for every revised file submitted to Production
    mesh_id             TEXT NOT NULL,                   -- the pinned 3D version …
    version_no          INTEGER,
    version_label       TEXT,
    source              TEXT,                            -- hi3d | external
    scaling             TEXT,                            -- scalable | exact
    master_sha256       TEXT,                            -- the version's own file (raw_geometry.sha256)
    three_d_id          TEXT NOT NULL,                   -- … and the sized result the line is produced from
    method_version      TEXT,
    file_sha256         TEXT,                            -- the production file as handed over (null until its export finished)
    file_bytes          INTEGER,
    file_job_id         TEXT,                            -- the export job (or 'stored') the hash was taken from
    revision_reason     TEXT,                            -- why the file changed after the hand-over (handoff_no > 1)
    answers_request_id  TEXT,                            -- Fulfillment's attention request this revision answers
    state               TEXT NOT NULL CHECK (state IN ('sent', 'received', 'revised')),
    received_at         TEXT,                            -- when Fulfillment first reported the part
    part_id             TEXT,
    part_display_no     TEXT,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fulfillment_line_production_order ON fulfillment_line_production(order_id);

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
                "capabilities": ["changes", "requests", "events", "assets.image", "assets.production_stl", "commands", "release_checks", "shipping_checks",
                                 "line_production", "admin_payload", "attention", "about_parts"],
                # The commercial rule Atelier owns (docs/order-to-production-review.md §J): while True, an unpaid order's
                # lines are released only once paid — the checks say PAYMENT_PENDING; False: payment is shown, never gating
                "payment_gate": bool(self.Ctx.Settings.FulfillmentPaymentGate), "server_now": Now()}

    # ── enrollment ───────────────────────────────────────────────────────
    def Enrollment(self, EntityType: str, EntityId: str) -> dict | None:
        return self.Ctx.Db.One("SELECT * FROM fulfillment_enrollments WHERE entity_type = ? AND entity_id = ?", (EntityType, EntityId))

    def IsEnrolled(self, EntityType: str, EntityId: str) -> bool:
        E = self.Enrollment(EntityType, EntityId)
        return bool(E and E["state"] == "enrolled")

    def OrderAllowed(self, O: dict) -> bool:
        """The controlled phase's allowlist (P3_FULFILLMENT_ORDER_ALLOWLIST, ids or references): while set, only those orders
        go to Production — the server's rule, whatever the page shows; an order already in Production stays allowed."""
        Allow = tuple(self.Ctx.Settings.FulfillmentOrderAllowlist or ())
        if not Allow:
            return True
        Keys = {str(O.get("id") or "").strip(), str(O.get("ref") or "").strip().upper()}
        return any(X.strip() in Keys or X.strip().upper() in Keys for X in Allow) or self.IsEnrolled("order", O["id"])

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
        """Whether the record may send lines to Production now, with the reason when it may not, and the record. An
        order's lines decide the rest (LineEligibility); a quotation never goes (D-10)."""
        if EntityType == "order":
            O = self._Order(EntityId)
            if O["status"] == "cancelled":
                return False, "the order is cancelled", O
            if O["status"] == "completed":
                return False, "the order is completed", O
            if O["status"] not in EligibleOrderStatuses and not self.IsEnrolled("order", O["id"]):
                return False, f"an order in status '{OrdersModule.StatusLabels.get(O['status'], O['status'])}' stays with the existing flow", O
            if not self.OrderAllowed(O):
                return False, NotEnabledReason, O
            return True, None, O
        Q = self._Quote(EntityId)
        return False, "quotations are managed here; once the quote has become an order, the order is sent", Q

    # ── Order → Production: the per-line hand-over (docs/order-to-production-review.md §B, §G) ──
    def LineProduction(self, LineId: str) -> dict | None:
        return self.Ctx.Db.One("SELECT * FROM fulfillment_line_production WHERE line_id = ?", (LineId,))

    def ProductionRows(self, OrderId: str) -> list[dict]:
        return self.Ctx.Db.All("SELECT * FROM fulfillment_line_production WHERE order_id = ? ORDER BY sent_at, id", (OrderId,))

    def LineEligibility(self, O: dict, L: dict, Prod: dict | None = None, Known: bool = False) -> dict:
        """Whether this order line may be sent to Production now: pinned to a 3D version, with a complete sized result
        (measured, closed or accepted) for the ordered size and material — "Print file required" otherwise, with the
        exact reason. Prod = its production row when it was sent already (Known: the caller looked it up)."""
        Prod = Prod if Known else self.LineProduction(L["id"])

        def No(Reason: str, Message: str | None = None) -> dict:
            return {"eligible": False, "reason": Reason, "message": Message or ProductionReasons[Reason], "production": Prod}

        if Prod:
            return No("already_sent")
        if O["status"] == "cancelled":
            return No("order_cancelled")
        if O["status"] == "completed":
            return No("order_completed")
        if O["status"] not in EligibleOrderStatuses and not self.IsEnrolled("order", O["id"]):
            return No("legacy_production")
        if not L.get("has_model"):
            return No("no_model")
        if not L.get("three_d_id"):
            return No("not_prepared")
        if not L.get("three_d_pinned"):
            return No("not_pinned")
        State = L.get("three_d_state")
        if not L.get("three_d_match"):
            # Pinned to a result still being made (a revised STL measuring, a prepared result queued): its state, not "another size"
            if State in ("processing", "failed", "cancelled"):
                return No(State)
            return No("other_size")
        if State != "complete":
            return No(State if State in ProductionReasons else "processing")
        return {"eligible": True, "reason": None, "message": None, "production": None}

    def _PinInfo(self, L: dict) -> dict:
        """The exact geometry a line is produced from: its pinned 3D version (number, source, scaling, the master
        file's hash) and the sized result."""
        V = L.get("three_d_version") or {}
        Raw = self.Ctx.Db.One("SELECT sha256, method_version FROM raw_geometry WHERE mesh_id = ?", (V.get("mesh_id"),)) if V.get("mesh_id") else None
        return {"mesh_id": V.get("mesh_id"), "version_no": V.get("version_no"), "version_label": V.get("label"), "source": V.get("source"),
                "scaling": V.get("scaling"), "master_sha256": Raw["sha256"] if Raw else None, "method_version": Raw["method_version"] if Raw else None,
                "three_d_id": L["three_d_id"]}

    def _ProductionFile(self, Sid: str) -> dict | None:
        """The production file of a sized result as it would be served now — its hash, size and the export job (or
        'stored': an exact part's master at its own size) — or None while the export is still queued or running.
        Raises when no export can be made (no bore, not measured): then there is no valid print file."""
        Export = self.Production.StartExport(Sid)
        if Export["status"] != "done":
            return None
        return {"sha256": self._ExportSha(Sid, Export["job_id"]), "bytes": self._ExportPath(Sid, Export["job_id"]).stat().st_size, "job_id": Export["job_id"]}

    def _EnrollRow(self, Conn, O: dict, By: str, Note: str, T: str) -> int:
        """The order's enrollment, written by its first line sent to Production: the row, the outbox's `enrolled`, the
        history line — inside the caller's transaction. Version 1."""
        Conn.execute("INSERT INTO fulfillment_enrollments (id, entity_type, entity_id, display_ref, state, source_version, enrolled_at, enrolled_by, note, "
                     "inherited_from, updated_at) VALUES (?,?,?,?,'enrolled',1,?,?,?,NULL,?)", (NewId("fen"), "order", O["id"], O["ref"], T, By, Note or None, T))
        Conn.execute("INSERT INTO fulfillment_outbox (kind, entity_type, entity_id, source_version, reason, created_at) VALUES ('enrolled', 'order', ?, 1, 'production', ?)",
                     (O["id"], T))
        Conn.execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                     (O["id"], "fulfillment", Dumps({"action": "enrolled", "by": By, "note": Note or None, "inherited_from": None}), By, T))
        return 1

    def SendToProduction(self, OrderId: str, LineIds, By: str, Note: str = "") -> dict:
        """The explicit hand-over of order lines to Production: every line checked again on the server, the order
        enrolled on its first line, one production row per line with the pinned version and the file's hash, one
        outbox row, one history line per line — all in one transaction. A line already sent is refused, never
        duplicated; a retry after a timeout finds the rows and is told so. Nothing is sent by itself, ever."""
        self._Require()
        O = self._Order(OrderId)
        Ids = list(dict.fromkeys(X for X in (LineIds or []) if isinstance(X, str) and X)) if isinstance(LineIds, list) else []
        if not Ids:
            raise HttpError(400, "lines_required", "Choose at least one order line to send to Production.")
        Lines = {L["id"]: L for L in O["lines"]}
        Unknown = [X for X in Ids if X not in Lines]
        if Unknown:
            raise HttpError(404, "line_not_found", "No such line on this order.", {"line_ids": Unknown})
        Note = " ".join(str(Note or "").split())[:500]
        Ok, Why, _ = self.Eligibility("order", O["id"])
        if not Ok:
            raise HttpError(409, "not_eligible", f"{O['ref']} cannot be sent to Production: {Why}.")
        Blocked, Pins = [], {}
        for Id in Ids:
            El = self.LineEligibility(O, Lines[Id])
            if not El["eligible"]:
                Blocked.append({"line_id": Id, "item_ref": Lines[Id]["ring_id"], "reason": El["reason"], "message": El["message"]})
                continue
            try:                                              # the export is started (or found) before the transaction: never a paid call
                Pins[Id] = {**self._PinInfo(Lines[Id]), "file": self._ProductionFile(Lines[Id]["three_d_id"])}
            except HttpError as E:
                Blocked.append({"line_id": Id, "item_ref": Lines[Id]["ring_id"], "reason": "export_failed", "message": f"{ProductionReasons['export_failed']}: {E.Message}"})
        if Blocked:
            Code = "already_sent" if all(B["reason"] == "already_sent" for B in Blocked) else "print_file_required"
            Text = "; ".join(f"{B['item_ref']}: {B['message']}" for B in Blocked)
            raise HttpError(409, Code, (f"{Text}." if Code == "already_sent" else f"Print file required before sending to Production — {Text}."), {"blocked": Blocked})
        T = Now()
        with self.Ctx.Db.Transaction() as Conn:
            for Id in Ids:                                    # a double click, or a retry that already landed
                if Conn.execute("SELECT 1 FROM fulfillment_line_production WHERE line_id = ?", (Id,)).fetchone():
                    raise HttpError(409, "already_sent", f"{Lines[Id]['ring_id']} was already sent to Production.",
                                    {"blocked": [{"line_id": Id, "item_ref": Lines[Id]["ring_id"], "reason": "already_sent", "message": ProductionReasons["already_sent"]}]})
            E = Conn.execute("SELECT * FROM fulfillment_enrollments WHERE entity_type = 'order' AND entity_id = ?", (O["id"],)).fetchone()
            if E is None:
                Version = self._EnrollRow(Conn, O, By, Note, T)
            else:
                Version = E["source_version"] + 1
                Conn.execute("UPDATE fulfillment_enrollments SET source_version = ?, updated_at = ? WHERE id = ?", (Version, T, E["id"]))
                Conn.execute("INSERT INTO fulfillment_outbox (kind, entity_type, entity_id, source_version, reason, created_at) VALUES ('updated', 'order', ?, ?, 'production', ?)",
                             (O["id"], Version, T))
            for Id in Ids:
                P, L = Pins[Id], Lines[Id]
                F = P["file"] or {}
                Conn.execute("INSERT INTO fulfillment_line_production (id, order_id, line_id, sent_at, sent_by, note, handoff_no, mesh_id, version_no, version_label, source, "
                             "scaling, master_sha256, three_d_id, method_version, file_sha256, file_bytes, file_job_id, state, updated_at) "
                             "VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,'sent',?)",
                             (NewId("flp"), O["id"], Id, T, By, Note or None, P["mesh_id"], P["version_no"], P["version_label"], P["source"], P["scaling"],
                              P["master_sha256"], P["three_d_id"], P["method_version"], F.get("sha256"), F.get("bytes"), F.get("job_id"), T))
                Conn.execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                             (O["id"], "production_sent", Dumps({"line_id": Id, "item_ref": L["ring_id"], "title": L["title"], "mesh_id": P["mesh_id"],
                                                                 "version_no": P["version_no"], "version_label": P["version_label"], "session_3d_id": P["three_d_id"],
                                                                 "file_sha256": F.get("sha256"), "handoff_no": 1, "note": Note or None, "source_version": Version}), By, T))
        return self.AdminState("order", O["id"])

    def _RecordFile(self, Row: dict, F: dict) -> None:
        """The production file's hash and size, recorded once on a sent line whose export finished after the
        hand-over; Fulfillment is told through a snapshot change (reason production_file)."""
        T = Now()
        with self.Ctx.Db.Transaction() as Conn:
            Cur = Conn.execute("SELECT file_sha256, handoff_no FROM fulfillment_line_production WHERE id = ?", (Row["id"],)).fetchone()
            if Cur is None or Cur["file_sha256"]:
                return
            Conn.execute("UPDATE fulfillment_line_production SET file_sha256 = ?, file_bytes = ?, file_job_id = ?, updated_at = ? WHERE id = ?",
                         (F["sha256"], F["bytes"], F["job_id"], T, Row["id"]))
            E = Conn.execute("SELECT * FROM fulfillment_enrollments WHERE entity_type = 'order' AND entity_id = ?", (Row["order_id"],)).fetchone()
            if E:
                Conn.execute("UPDATE fulfillment_enrollments SET source_version = source_version + 1, updated_at = ? WHERE id = ?", (T, E["id"]))
                Conn.execute("INSERT INTO fulfillment_outbox (kind, entity_type, entity_id, source_version, reason, created_at) VALUES ('updated', 'order', ?, ?, 'production_file', ?)",
                             (Row["order_id"], E["source_version"] + 1, T))
            Conn.execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                         (Row["order_id"], "production_file", Dumps({"line_id": Row["line_id"], "session_3d_id": Row["three_d_id"], "file_sha256": F["sha256"],
                                                                     "file_bytes": F["bytes"], "handoff_no": Cur["handoff_no"]}), "system", T))

    def _FinalizeFiles(self, OrderId: str) -> None:
        """Sent lines whose export was still running at the hand-over: record the file once it exists."""
        for Row in self.Ctx.Db.All("SELECT * FROM fulfillment_line_production WHERE order_id = ? AND file_sha256 IS NULL", (OrderId,)):
            try:
                F = self._ProductionFile(Row["three_d_id"])
            except HttpError:
                continue
            if F:
                self._RecordFile(Row, F)

    def _ProductionJson(self, Prod: dict | None) -> dict:
        """The snapshot's (and the page's) view of a line's hand-over: not sent, or sent with the exact pin."""
        if not Prod:
            return {"sent": False}
        return {"sent": True, "sent_at": Prod["sent_at"], "sent_by": Prod["sent_by"], "note": Prod["note"], "handoff_no": Prod["handoff_no"], "state": Prod["state"],
                "received_at": Prod["received_at"], "part_id": Prod["part_id"], "part_display_no": Prod["part_display_no"],
                "pinned": {K: Prod[K] for K in ("mesh_id", "version_no", "version_label", "source", "scaling", "master_sha256", "three_d_id", "method_version",
                                                "file_sha256", "file_bytes", "file_job_id")},
                "revision_reason": Prod["revision_reason"], "answers_request_id": Prod["answers_request_id"]}

    # ── a revised file after the hand-over (docs/order-to-production-review.md §B.6, §G.4) ──
    def GuardLineVersionChange(self, Order: dict, Line: dict) -> None:
        """A line in Production changes its geometry only through the explicit revision workflow (a reason, Fulfillment
        told, a new revision there): the plain *Use another version* / *Upload revised STL* of the order page are refused."""
        if self.Enabled and self.LineProduction(Line["id"]):
            raise HttpError(409, "line_in_production", f"{Line['ring_id']} is in Production: a revised file is submitted to Production explicitly, with a reason, "
                                                       "from the Production page — never changed in place.")

    def SubmitRevision(self, OrderId: str, LineId: str, By: str, Body: dict) -> dict:
        """A revised production file for a line in Production: the line re-pinned through the existing version choice or
        upload (a reason required), the production row advanced to the next hand-over number with the new pin and
        file hash, Fulfillment told through a snapshot change (reason production_revision). The former version, its
        file and everything made from it stay in the history on both sides; a file already used in a build is never
        replaced — Fulfillment holds the part until Operations re-approve."""
        self._Require()
        Prod = self.LineProduction(LineId)
        if Prod is None or Prod["order_id"] != OrderId:
            raise HttpError(404, "production_not_found", "This order line was not sent to Production.")
        Reason = " ".join(str(Body.get("reason") or "").split())[:300]
        if not Reason:
            raise HttpError(400, "reason_required", "Say why the production file changes (kept with the order and sent to Fulfillment).")
        MeshId, UploadId = Body.get("mesh_id"), Body.get("upload_id")
        if not (isinstance(MeshId, str) and MeshId) and not (isinstance(UploadId, str) and UploadId):
            raise HttpError(400, "version_required", "Choose another 3D version or upload a revised STL.")
        Answers = str(Body.get("answers_request_id") or "") or None
        if UploadId:
            self.Orders.UploadVersion(OrderId, LineId, UploadId, By, str(Body.get("scaling") or "exact"), str(Body.get("source_tool") or ""), Reason,
                                      bool(Body.get("activate", True)), ForProduction=True)
        else:
            self.Orders.UseVersion(OrderId, LineId, MeshId, By, Reason, ForProduction=True)
        O = self._Order(OrderId)
        L = next(X for X in O["lines"] if X["id"] == LineId)
        if L.get("three_d_id") == Prod["three_d_id"] and (L.get("three_d_version") or {}).get("mesh_id") == Prod["mesh_id"]:
            raise HttpError(409, "nothing_changed", "That is the version already in Production.")
        # The new pin must be a real result of the line's option. Unlike the first hand-over it may still be measuring (an
        # uploaded STL is measured in the background) or flagged for review: Fulfillment receives the revision now and
        # fetches its file once the result is complete (or accepted) — exactly as it waits for any other file.
        El = self.LineEligibility(O, L, None, Known=True)
        Problem = None
        if not El["eligible"] and El["reason"] not in ("processing", "review_required"):
            Problem = HttpError(409, "print_file_required", f"The revised file is not a valid print file — {El['message']}.", {"blocked": [{"line_id": LineId, "reason": El["reason"], "message": El["message"]}]})
        Pin = self._PinInfo(L)
        File = {}
        if Problem is None and El["eligible"]:
            try:
                File = self._ProductionFile(L["three_d_id"]) or {}
            except HttpError as E:
                Problem = HttpError(409, "print_file_required", f"The revised file cannot be exported: {E.Message}")
        if Problem is not None:
            # A refused revision changes nothing in Production: the line goes back to the version pinned there (the attempt
            # and its reason stay in the order's history)
            try:
                self.Orders.UseVersion(OrderId, LineId, Prod["mesh_id"], By, f"revision refused: {Problem.Message}", ForProduction=True)
            except HttpError:
                pass
            raise Problem
        T = Now()
        with self.Ctx.Db.Transaction() as Conn:
            Cur = Conn.execute("SELECT * FROM fulfillment_line_production WHERE id = ?", (Prod["id"],)).fetchone()
            E = Conn.execute("SELECT * FROM fulfillment_enrollments WHERE entity_type = 'order' AND entity_id = ?", (OrderId,)).fetchone()
            Version = E["source_version"] + 1
            Conn.execute("UPDATE fulfillment_enrollments SET source_version = ?, updated_at = ? WHERE id = ?", (Version, T, E["id"]))
            Conn.execute("INSERT INTO fulfillment_outbox (kind, entity_type, entity_id, source_version, reason, created_at) VALUES ('updated', 'order', ?, ?, 'production_revision', ?)",
                         (OrderId, Version, T))
            Conn.execute("UPDATE fulfillment_line_production SET handoff_no = handoff_no + 1, mesh_id = ?, version_no = ?, version_label = ?, source = ?, scaling = ?, "
                         "master_sha256 = ?, three_d_id = ?, method_version = ?, file_sha256 = ?, file_bytes = ?, file_job_id = ?, revision_reason = ?, answers_request_id = ?, "
                         "state = 'revised', updated_at = ? WHERE id = ?",
                         (Pin["mesh_id"], Pin["version_no"], Pin["version_label"], Pin["source"], Pin["scaling"], Pin["master_sha256"], Pin["three_d_id"], Pin["method_version"],
                          File.get("sha256"), File.get("bytes"), File.get("job_id"), Reason, Answers, T, Prod["id"]))
            Conn.execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                         (OrderId, "production_revision_submitted", Dumps({"line_id": LineId, "item_ref": L["ring_id"], "handoff_no": Cur["handoff_no"] + 1, "mesh_id": Pin["mesh_id"],
                                                                           "version_no": Pin["version_no"], "version_label": Pin["version_label"], "session_3d_id": Pin["three_d_id"],
                                                                           "file_sha256": File.get("sha256"), "reason": Reason, "answers_request_id": Answers,
                                                                           "from_mesh_id": Cur["mesh_id"], "from_version_no": Cur["version_no"], "source_version": Version}), By, T))
        return self.ProductionDetail(LineId)

    def _AllLinesSent(self, Conn, OrderId: str) -> bool:
        N = Conn.execute("SELECT COUNT(*) FROM order_lines WHERE order_id = ?", (OrderId,)).fetchone()[0]
        S = Conn.execute("SELECT COUNT(*) FROM fulfillment_line_production WHERE order_id = ?", (OrderId,)).fetchone()[0]
        return N > 0 and S >= N

    def GuardStatusChange(self, Order: dict, Status: str) -> None:
        """The legacy production / QC / shipping statuses are Fulfillment's once a line of the order was sent to
        Production (spec §7.5)."""
        if Status in OrdersModule.ProductionStatuses and self.IsEnrolled("order", Order["id"]):
            raise HttpError(409, "owned_by_fulfillment", f"{Order['ref']} has lines in Production: production, QC, shipping and completion are recorded "
                                                        "in Fulfillment and reach this page as its progress. Cancellation and payment stay here.")

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
            if self.Ctx.Settings.FulfillmentPaymentGate and O["payment_status"] != "paid":     # the gate Atelier owns (identity.payment_gate)
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
        if self.Ctx.Settings.FulfillmentPaymentGate and O["payment_status"] != "paid":         # the gate Atelier owns (identity.payment_gate)
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
        Prods = {R["line_id"]: R for R in self.ProductionRows(O["id"])}
        for I, L in enumerate(O["lines"], start=1):
            Size = {"ring_size": L["ring_size"]} if L["product_type"] == "ring" else {"charm_size": L["charm_size"], "unit": "mm"}
            Lines.append({"line_id": L["id"], "position": I, "design_id": L["design_id"], "candidate_id": L["candidate_id"], "item_ref": L["ring_id"],
                          "title": L["title"], "product_type": L["product_type"], "material_id": L["material_id"], "material_label": L["material_label"],
                          "size": {**Size, "label": L["size_label"]}, "quantity": L["quantity"], "unit_price": L["unit_price"], "currency": L["currency"],
                          "quote_request_id": O["quote"]["id"] if O.get("quote") else None,
                          "image": {"design_id": L["design_id"], "candidate_id": L["candidate_id"]}, "model": self._Model(L),
                          # The hand-over (docs/order-to-production-review.md §B.4): only a sent line becomes a manufacturing part
                          "production": self._ProductionJson(Prods.get(L["id"]))})
        return {**self._Envelope("order", O["id"], O["ref"], E),
                "order": {"status": O["status"], "placed_at": O["created_at"], "updated_at": O["updated_at"],
                          "payment": {"status": O["payment_status"], "basis": "paid" if O["payment_status"] == "paid" else None,
                                      "provider": O["payment_provider"], "ref": O.get("payment_ref"),
                                      "updated_at": PayEvent["created_at"] if PayEvent else O["created_at"]},
                          "customer": {"name": (O["customer"].get("first_name", "") + " " + O["customer"].get("last_name", "")).strip(),
                                       "email": O["customer"].get("email")},
                          # Atelier's verdict on a synthetic order (p3/testrecords.py): Fulfillment marks it Test and keeps it apart
                          "test": bool(O.get("test")),
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
        V = Info.get("three_d_version") or {}
        Out = {"available": False, "state": "no_model", "session_3d_id": None, "mesh_id": None, "raw_sha256": None, "method_version": None,
               "integrity": None, "production_size": None, "material_id": None, "accepted": None, "review": [], "geometry": None, "weight": None,
               "latest_other": Info.get("three_d_latest"), "model_item_ref": Info.get("model_ring_id"),
               # The 3D version the line is (or would be) produced from (p3/versions.py), pinned or still following the design
               "version": {"mesh_id": V.get("mesh_id"), "version_no": V.get("version_no"), "label": V.get("label"), "source": V.get("source"),
                           "scaling": V.get("scaling"), "original_filename": V.get("original_filename"), "pinned": bool(Info.get("three_d_version_pinned"))} if V else None,
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
        """The production file of an order line sent to Production: the export of the sized result pinned at the
        hand-over (or the stored master of an exact part at its own size), served only while its hash is the one
        recorded then — a different file is an integration fault, said as such, never served. A line not sent has no
        production file here, whatever its model: nothing is prepared or exported for it."""
        Prod = self.LineProduction(LineId)
        if Prod is None or not self.IsEnrolled("order", Prod["order_id"]):
            raise HttpError(404, "line_not_found", "No such order line sent to Production.")
        Sid = Prod["three_d_id"]
        try:
            Export = self.Production.StartExport(Sid)
        except HttpError as E:
            return {"status": "unavailable", "reason": E.Code, "message": E.Message, "session_3d_id": Sid, "handoff_no": Prod["handoff_no"]}
        Out = {"status": Export["status"], "job_id": Export["job_id"], "session_3d_id": Sid, "bytes": Export.get("bytes"), "ahead": Export.get("ahead"),
               "expires_at": Export.get("expires_at"), "error": Export.get("error"), "handoff_no": Prod["handoff_no"], "pinned_sha256": Prod["file_sha256"]}
        if Export["status"] == "done":
            Sha = self._ExportSha(Sid, Export["job_id"])
            if not Prod["file_sha256"]:
                self._RecordFile(Prod, {"sha256": Sha, "bytes": self._ExportPath(Sid, Export["job_id"]).stat().st_size, "job_id": Export["job_id"]})
            elif Sha != Prod["file_sha256"]:
                raise HttpError(409, "file_changed", "The export no longer reproduces the file pinned at the hand-over; a revised file must be submitted to "
                                                     "Production explicitly.", {"line_id": LineId, "pinned_sha256": Prod["file_sha256"], "export_sha256": Sha})
            Out.update({"sha256": Sha, "pinned_sha256": Prod["file_sha256"] or Sha})
        return Out

    def ExportStatus(self, Sid: str, Jid: str) -> dict:
        """A production file is served only for the sized result pinned by a line sent to Production (the service
        credential alone opens nothing), and only while it is the file recorded at the hand-over."""
        Prod = self.Ctx.Db.One("SELECT * FROM fulfillment_line_production WHERE three_d_id = ? ORDER BY handoff_no DESC LIMIT 1", (Sid,))
        if Prod is None or not self.IsEnrolled("order", Prod["order_id"]):
            raise HttpError(404, "job_not_found", "Export not found.")
        S = self.Production.StoredProduction(Sid) if Jid == "stored" else self.Production.ExportStatus(Sid, Jid)
        if S is None:
            raise HttpError(404, "job_not_found", "Export not found.")
        Out = {"status": S["status"], "job_id": S["job_id"], "session_3d_id": Sid, "bytes": S.get("bytes"), "ahead": S.get("ahead"), "error": S.get("error"),
               "handoff_no": Prod["handoff_no"], "pinned_sha256": Prod["file_sha256"]}
        if S["status"] == "done":
            Sha = self._ExportSha(Sid, S["job_id"])
            if Prod["file_sha256"] and Sha != Prod["file_sha256"]:
                raise HttpError(409, "file_changed", "The export no longer reproduces the file pinned at the hand-over.",
                                {"line_id": Prod["line_id"], "pinned_sha256": Prod["file_sha256"], "export_sha256": Sha})
            Out["sha256"] = Sha
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
        AdminP = E.get("admin_payload") if isinstance(E.get("admin_payload"), dict) else {}
        State.update({K: V for K, V in Public.items() if K in ("customer_status", "summary", "parts", "review_status", "blockers", "file_state", "shipment", "shipments",
                                                                 "order_status", "review", "commercial_action_pending", "commercial_action")})
        if AdminP:
            # The staff-only view (docs/order-to-production-review.md §C.4): the parts' stages, builds, holds, attention
            # and latest notes replace the previous ones; every note is kept for the Production page; never shown to a customer
            Admin = State.get("admin") if isinstance(State.get("admin"), dict) else {}
            Admin.update({K: V for K, V in AdminP.items() if K in ("parts", "attention", "revision")})
            if isinstance(AdminP.get("note"), dict):
                Admin["notes"] = ([AdminP["note"]] + [N for N in (Admin.get("notes") or []) if N.get("id") != AdminP["note"].get("id")])[:200]
            State["admin"] = Admin
        State.update({"aggregate_versions": Versions, "last_event_type": E["event_type"], "last_event_at": E["occurred_at"], "last_received_at": T})
        # K1: Fulfillment's order-level status is written only when every line of the order is in Production; a partly
        # sent order keeps its commercial status, and every line shows its own stage
        if EntityType == "order" and Public.get("order_status") in ProjectionStatuses and self._AllLinesSent(Conn, EntityId):
            self._ProjectStatus(Conn, EntityId, Public["order_status"], T)
        Summary = str(Public.get("summary") or E.get("summary") or E["event_type"])[:300]
        if Row:
            Conn.execute("UPDATE fulfillment_projection SET aggregate_version = ?, state_json = ?, last_event_id = ?, last_event_at = ?, updated_at = ? "
                         "WHERE entity_type = ? AND entity_id = ?", (int(E["aggregate_version"]), Dumps(State), E["event_id"], E["occurred_at"], T, EntityType, EntityId))
        else:
            Conn.execute("INSERT INTO fulfillment_projection (entity_type, entity_id, aggregate_version, state_json, last_event_id, last_event_at, updated_at) "
                         "VALUES (?,?,?,?,?,?,?)", (EntityType, EntityId, int(E["aggregate_version"]), Dumps(State), E["event_id"], E["occurred_at"], T))
        # The history line names the parts the event is about (K9), so each line's timeline on the Production page is exact
        PartLines = {P.get("display_no"): P.get("line_id") for P in (Public.get("parts") or []) if isinstance(P, dict) and P.get("display_no")}
        About = [X for X in E.get("about_parts") if isinstance(X, str)] if isinstance(E.get("about_parts"), list) else list(PartLines)
        Data = {"event_type": E["event_type"], "summary": Summary, "event_id": E["event_id"], "at": E["occurred_at"], "about_parts": About,
                "line_ids": [PartLines[X] for X in About if PartLines.get(X)]}
        if EntityType == "order":
            Conn.execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)", (EntityId, "fulfillment", Dumps(Data), "fulfillment", T))
            self._LinesReported(Conn, EntityId, Public, AdminP, E["occurred_at"], T)
            self.Notifications.Intend(Conn, EntityId, E, Public, T)       # a milestone becomes one intent per scope, in this transaction (C09)
        else:
            OrdersModule.QuoteEvent(Conn, EntityId, "fulfillment", Data, "fulfillment")
        return "applied"

    def _LinesReported(self, Conn, OrderId: str, Public: dict, AdminP: dict, At: str, T: str) -> None:
        """A sent line whose part Fulfillment reports is *received* (with the part's id and number); a revised line is
        received again once Fulfillment reports the revision at that hand-over number."""
        for P in Public.get("parts") or []:
            if isinstance(P, dict) and isinstance(P.get("line_id"), str):
                Conn.execute("UPDATE fulfillment_line_production SET part_id = COALESCE(part_id, ?), part_display_no = COALESCE(part_display_no, ?), updated_at = ? "
                             "WHERE line_id = ? AND order_id = ?", (P.get("part_id"), P.get("display_no"), T, P["line_id"], OrderId))
                Conn.execute("UPDATE fulfillment_line_production SET state = 'received', received_at = COALESCE(received_at, ?), updated_at = ? "
                             "WHERE line_id = ? AND order_id = ? AND state = 'sent'", (At, T, P["line_id"], OrderId))
        Rev = AdminP.get("revision") if isinstance(AdminP.get("revision"), dict) else None
        if Rev and isinstance(Rev.get("part"), str):
            Conn.execute("UPDATE fulfillment_line_production SET state = 'received', updated_at = ? WHERE order_id = ? AND part_display_no = ? AND state = 'revised' "
                         "AND handoff_no = ?", (T, OrderId, Rev["part"], Rev.get("handoff_no")))

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
        if E:
            self._FinalizeFiles(Id)
        Rows = {R["line_id"]: R for R in self.ProductionRows(Id)}
        NotEnabled = not Ok and Why == NotEnabledReason
        Lines = []
        for L in Record["lines"]:
            El = self.LineEligibility(Record, L, Rows.get(L["id"]), Known=True)
            # The print file's own verdict, kept apart from the order's: a valid print file is never called "required"
            PrintReady = bool(El["eligible"])
            if El["eligible"] and not Ok:                 # the order itself may not go (status, the controlled phase's allowlist): its lines say why
                El = {**El, "eligible": False, "reason": "not_enabled" if NotEnabled else "order", "message": Why}
            Prod = Rows.get(L["id"])
            Lines.append({"line_id": L["id"], "item_ref": L["ring_id"], "title": L["title"], "quantity": L["quantity"], "eligible": El["eligible"],
                          "reason": El["reason"], "message": El["message"], "print_ready": PrintReady, "print_version": L.get("three_d_version"),
                          "print_state": L.get("three_d_state"), "print_match": bool(L.get("three_d_match")), "pinned": bool(L.get("three_d_pinned")),
                          "production": self._ProductionJson(Prod),
                          # Where the part is, once Fulfillment confirmed it (I-32): the hand-over pending or confirmed, the MP number, the link
                          "fulfillment_url": self._FulfillmentPartUrl(Prod["part_id"]) if Prod and Prod["part_id"] else None})
        Pull = self.LastPull()
        AnyEligible = any(X["eligible"] for X in Lines)
        return {"enabled": True, "enrolled": bool(E and E["state"] == "enrolled"), "eligible": Ok and AnyEligible, "not_enabled": NotEnabled,
                "ineligible_reason": Why if not Ok else (None if AnyEligible else ("every line is in Production" if Rows and len(Rows) >= len(Lines) else
                                                                                   "no line has a valid print file yet")),
                "lines": Lines, "lines_sent": len(Rows), "lines_total": len(Lines),
                "enrollment": E, "awaiting_sync": self._Awaiting("order", Id, E, Pull), "last_pull_at": Pull["at"] if Pull else None,
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
        Sent = self.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_line_production WHERE order_id = ?", (EntityId,))["n"]
        Total = self.Ctx.Db.One("SELECT COUNT(*) AS n FROM order_lines WHERE order_id = ?", (EntityId,))["n"]
        return {"enrolled": True, "enrolled_at": E["enrolled_at"], "customer_status": Proj.get("customer_status"), "order_status": Proj.get("order_status"),
                "review_status": Review, "blocker": Blocker, "commercial_action_pending": bool(Proj.get("commercial_action_pending")), "pending_commands": Pending,
                "last_event_at": Proj.get("last_event_at"), "awaiting_sync": self._Awaiting(EntityType, EntityId, E, self.LastPull()),
                "lines_sent": Sent, "lines_total": Total,
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

    # ── the Production page (docs/order-to-production-review.md §H): every line sent, Atelier's facts and Fulfillment's side by side ──
    def _PartOf(self, Proj: dict, LineId: str) -> dict | None:
        """The projected part of a line: the customer-safe summary and the staff-only view merged (the staff view wins)."""
        Public = next((P for P in (Proj.get("parts") or []) if isinstance(P, dict) and P.get("line_id") == LineId), None)
        Admin = next((P for P in ((Proj.get("admin") or {}).get("parts") or []) if isinstance(P, dict) and P.get("line_id") == LineId), None)
        if Public is None and Admin is None:
            return None
        return {**(Public or {}), **(Admin or {})}

    def _ProductionRows(self, OrderId: str | None = None, LineId: str | None = None) -> list[dict]:
        Where = " WHERE p.order_id = ?" if OrderId else " WHERE p.line_id = ?" if LineId else ""
        return self.Ctx.Db.All("SELECT p.*, l.title, l.ring_id, l.material_id, l.material_label, l.product_type, l.ring_size, l.charm_size, l.quantity, l.image_path, "
                               "l.position, o.order_no, o.status AS order_status, o.customer_json, o.payment_status FROM fulfillment_line_production p "
                               "JOIN order_lines l ON l.id = p.line_id JOIN orders o ON o.id = p.order_id" + Where + " ORDER BY p.sent_at DESC, p.id",
                               (OrderId or LineId,) if Where else ())

    def _ProductionRow(self, R: dict, Part: dict | None, Proj: dict, Awaiting: bool) -> dict:
        from p3 import products as Products
        Customer = json.loads(R["customer_json"] or "{}")
        Stage = (Part or {}).get("stage") or "sent"
        Pin = {K: R[K] for K in ("mesh_id", "version_no", "version_label", "source", "scaling", "master_sha256", "three_d_id", "method_version", "file_sha256", "file_bytes", "file_job_id")}
        Detail = (Part or {}).get("stage_detail") if Part else ("awaiting synchronization" if Awaiting else "not reported by Fulfillment yet")
        Updates = [X for X in (R["updated_at"], (Part or {}).get("updated_at"), Proj.get("last_event_at")) if X]
        return {"line_id": R["line_id"],
                "order": {"id": R["order_id"], "ref": OrdersModule.OrderRef(R["order_no"]), "status": R["order_status"],
                          "status_label": OrdersModule.StatusLabels.get(R["order_status"], R["order_status"]), "payment_status": R["payment_status"],
                          "customer": (str(Customer.get("first_name", "")) + " " + str(Customer.get("last_name", ""))).strip(), "customer_email": Customer.get("email"),
                          "test": IsTestCustomer(Customer)},
                "line": {"title": R["title"], "item_ref": R["ring_id"], "product_type": R["product_type"], "material_id": R["material_id"], "material_label": R["material_label"],
                         "size_label": Products.SizeLabel(R["product_type"] or Products.Ring, R["ring_size"], R["charm_size"]), "quantity": R["quantity"],
                         "position": R["position"], "image_url": self.Ctx.AssetUrl(R["image_path"])},
                "sent_at": R["sent_at"], "sent_by": R["sent_by"], "note": R["note"], "handoff_no": R["handoff_no"], "state": R["state"], "received_at": R["received_at"],
                "pinned": Pin, "revision_reason": R["revision_reason"], "answers_request_id": R["answers_request_id"],
                "part": Part, "part_display_no": (Part or {}).get("display_no") or R["part_display_no"], "part_id": (Part or {}).get("part_id") or R["part_id"],
                "stage": Stage, "stage_label": ProductionStageLabels.get(Stage, Stage), "stage_detail": Detail,
                "build": (Part or {}).get("build"), "attention": (Part or {}).get("attention"), "latest_note": (Part or {}).get("latest_note"),
                "holds": (Part or {}).get("holds") or [], "quantities": (Part or {}).get("quantities") or {"ordered": R["quantity"]},
                "file_state": ((Part or {}).get("revision") or {}).get("file_state"), "revision": (Part or {}).get("revision"),
                "fulfillment_url": self._FulfillmentPartUrl((Part or {}).get("part_id") or R["part_id"]), "last_updated": max(Updates) if Updates else R["sent_at"]}

    def _FulfillmentPartUrl(self, PartId: str | None) -> str | None:
        Base = self.Ctx.Settings.FulfillmentUrl
        return f"{Base}/#/orders/{PartId}" if Base and PartId else None

    def ProductionList(self, Stage: str | None = None, Attention: bool = False, Material: str | None = None, Product: str | None = None, Query: str | None = None,
                       Build: str | None = None, OrderId: str | None = None, Since: str | None = None, Until: str | None = None, Offset: int = 0, Limit: int = 50) -> dict:
        """The Production page's rows: every order line sent to Production, newest hand-over first, with Atelier's facts
        (the order, the customer, the line, the pin) and Fulfillment's (the part's stage, build, attention, latest note)
        joined by line id; filtered, searched and paged here (the pilot's volume is small); grouped by order."""
        if not self.Enabled:
            return {"enabled": False, "rows": [], "orders": [], "total": 0, "offset": 0, "limit": Limit, "stages": [], "materials": [], "products": []}
        Pull = self.LastPull()
        Projections, Enrollments, Out = {}, {}, {}
        for R in self._ProductionRows(OrderId):
            if R["order_id"] not in Projections:
                Projections[R["order_id"]] = self.Projection("order", R["order_id"]) or {}
                Enrollments[R["order_id"]] = self.Enrollment("order", R["order_id"])
            Proj = Projections[R["order_id"]]
            Row = self._ProductionRow(R, self._PartOf(Proj, R["line_id"]), Proj, self._Awaiting("order", R["order_id"], Enrollments[R["order_id"]], Pull))
            Out[R["line_id"]] = Row
        Rows = list(Out.values())
        Facets = {"stages": sorted({X["stage"] for X in Rows}, key=lambda S: list(ProductionStageLabels).index(S) if S in ProductionStageLabels else 99),
                  "materials": sorted({(X["line"]["material_id"], X["line"]["material_label"]) for X in Rows}),
                  "products": sorted({X["line"]["product_type"] for X in Rows})}
        Q = (Query or "").strip().lower()
        Keep = []
        for X in Rows:
            if Stage and X["stage"] != Stage:
                continue
            if Attention and not X["attention"]:
                continue
            if Material and X["line"]["material_id"] != Material:
                continue
            if Product and X["line"]["product_type"] != Product:
                continue
            if Build and ((X["build"] or {}).get("display_no") or "").lower() != Build.strip().lower():
                continue
            if Since and X["sent_at"][:10] < Since[:10]:
                continue
            if Until and X["sent_at"][:10] > Until[:10]:
                continue
            if Q:
                Hay = " ".join(str(V) for V in (X["order"]["ref"], X["order"]["customer"], X["order"]["customer_email"], X["line"]["title"], X["line"]["item_ref"],
                                                 X["part_display_no"], (X["build"] or {}).get("display_no"), X["pinned"]["version_label"]) if V).lower()
                if Q not in Hay:
                    continue
            Keep.append(X)
        Offset, Limit = max(0, int(Offset)), max(1, min(int(Limit), 200))
        Page = Keep[Offset:Offset + Limit]
        Groups: dict = {}
        for X in Page:
            G = Groups.setdefault(X["order"]["id"], {"order": X["order"], "lines": [], "lines_sent": 0, "lines_total": 0, "attention": False})
            G["lines"].append(X)
            G["attention"] = G["attention"] or bool(X["attention"])
        for Oid, G in Groups.items():
            G["lines_sent"] = self.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_line_production WHERE order_id = ?", (Oid,))["n"]
            G["lines_total"] = self.Ctx.Db.One("SELECT COUNT(*) AS n FROM order_lines WHERE order_id = ?", (Oid,))["n"]
            Stages = [L["stage"] for L in G["lines"]]
            G["stage"] = min(Stages, key=lambda S: list(ProductionStageLabels).index(S) if S in ProductionStageLabels else 99)   # the least advanced line
            G["stage_label"] = ProductionStageLabels.get(G["stage"], G["stage"])
        return {"enabled": True, "rows": Page, "orders": list(Groups.values()), "total": len(Keep), "offset": Offset, "limit": Limit,
                "stages": [{"id": S, "label": ProductionStageLabels.get(S, S)} for S in Facets["stages"]],
                "materials": [{"id": M, "label": Lb} for M, Lb in Facets["materials"]], "products": Facets["products"],
                "fulfillment_url": self.Ctx.Settings.FulfillmentUrl or None, "last_pull_at": Pull["at"] if Pull else None}

    def ProductionDetail(self, LineId: str) -> dict:
        """One sent line: the row, the line's 3D choices for a revision, the holds and attention, the notes, the commands
        about it and its timeline — Atelier's own events and Fulfillment's, in time order, each with its source."""
        self._Require()
        Prod = self.LineProduction(LineId)
        if Prod is None:
            raise HttpError(404, "production_not_found", "This order line was not sent to Production.")
        self._FinalizeFiles(Prod["order_id"])
        O = self._Order(Prod["order_id"])
        L = next((X for X in O["lines"] if X["id"] == LineId), None)
        if L is None:
            raise HttpError(404, "line_not_found", "No such line on this order.")
        E = self.Enrollment("order", O["id"])
        Proj = self.Projection("order", O["id"]) or {}
        Part = self._PartOf(Proj, LineId)
        R = self._ProductionRows(LineId=LineId)[0]
        Row = self._ProductionRow(R, Part, Proj, self._Awaiting("order", O["id"], E, self.LastPull()))
        DisplayNo = Row["part_display_no"]
        Notes = [N for N in ((Proj.get("admin") or {}).get("notes") or []) if isinstance(N, dict) and N.get("part") == DisplayNo]
        return {**Row, "enrollment": E, "awaiting_sync": self._Awaiting("order", O["id"], E, self.LastPull()),
                "line": {**Row["line"], "id": LineId, "session_id": L.get("session_id"), "design_id": L["design_id"], "candidate_id": L["candidate_id"],
                         "three_d_id": L.get("three_d_id"), "three_d_state": L.get("three_d_state"), "three_d_version": L.get("three_d_version"),
                         "three_d_versions": L.get("three_d_versions") or [], "three_d_pinned": bool(L.get("three_d_pinned"))},
                "notes": Notes, "commands": [C for C in self.CommandList("order", O["id"]) if C["payload"].get("line_id") == LineId],
                "timeline": self._LineTimeline(O, LineId, DisplayNo), "order_lines_sent": self.Ctx.Db.One("SELECT COUNT(*) AS n FROM fulfillment_line_production WHERE order_id = ?", (O["id"],))["n"],
                "order_lines_total": len(O["lines"])}

    def _LineTimeline(self, O: dict, LineId: str, DisplayNo: str | None) -> list[dict]:
        """The line's history from the order's events (K9: no second history): Atelier's hand-over, file, version and
        revision events for this line, the requests about it, and every Fulfillment event about its part."""
        Items = []
        for Ev in O["events"]:
            K, D = Ev["kind"], Ev["data"] or {}
            if K in LineEventLabels and D.get("line_id") == LineId:
                Items.append({"at": Ev["created_at"], "source": "atelier", "kind": K, "label": LineEventLabels[K], "text": _LineEventText(K, D), "by": Ev["by"]})
            elif K == "fulfillment" and D.get("action") in ("command", "command_outcome"):
                if D.get("line_id") == LineId or (DisplayNo and D.get("part") == DisplayNo):
                    if D["action"] == "command":
                        Items.append({"at": Ev["created_at"], "source": "atelier", "kind": "command", "label": "Cancellation requested",
                                      "text": (f"{D.get('quantity')} unit(s)" if D.get("quantity") else "the whole line") + " · " + str(D.get("reason") or ""), "by": Ev["by"]})
                    else:
                        Items.append({"at": Ev["created_at"], "source": "fulfillment", "kind": "command_outcome", "label": "Request " + str(D.get("state") or ""),
                                      "text": str(D.get("result") or "") + (": " + D["reason"] if D.get("reason") else ""), "by": "Fulfillment"})
            elif K == "fulfillment" and D.get("event_type"):
                About, Lines = D.get("about_parts"), D.get("line_ids")
                Mine = (isinstance(Lines, list) and LineId in Lines) or (isinstance(About, list) and DisplayNo and DisplayNo in About) or (not About and not Lines)
                if Mine:
                    Items.append({"at": D.get("at") or Ev["created_at"], "source": "fulfillment", "kind": D["event_type"],
                                  "label": FulfillmentEventLabels.get(D["event_type"], D["event_type"].replace("fulfillment.", "").replace("_", " ")),
                                  "text": D.get("summary"), "by": "Fulfillment"})
        Items.sort(key=lambda X: X["at"])
        for X in Items:
            X["significant"] = X["kind"] not in TechnicalTimelineKinds
        return Items

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

    # Send to Production: the explicit, per-line hand-over (docs/order-to-production-review.md §B.3); the order is enrolled by its first line
    @App_.post("/api/admin/orders/{OrderId}/production")
    async def SendToProduction(OrderId: str, Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        return Bridge.SendToProduction(OrderId, Body_.get("line_ids"), Who.Id, str(Body_.get("note") or ""))

    # The Production page (§H): the lines sent, with Fulfillment's side of each; one line's detail and timeline
    @App_.get("/api/admin/production")
    async def ProductionList(stage: str | None = None, attention: bool = False, material: str | None = None, product: str | None = None, q: str | None = None,
                             build: str | None = None, order_id: str | None = None, since: str | None = None, until: str | None = None, offset: int = 0, limit: int = 50,
                             authorization: str | None = Header(None)):
        Admin(authorization)
        return Bridge.ProductionList(stage or None, bool(attention), material or None, product or None, q or None, build or None, order_id or None, since or None,
                                     until or None, offset, limit)

    @App_.get("/api/admin/production/{LineId}")
    async def ProductionDetail(LineId: str, authorization: str | None = Header(None)):
        Admin(authorization)
        return Bridge.ProductionDetail(LineId)

    # A revised file for a line in Production (§B.6): another version of the option, or an uploaded STL, with a reason
    @App_.post("/api/admin/orders/{OrderId}/lines/{LineId}/production-revision")
    async def ProductionRevision(OrderId: str, LineId: str, Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        return Bridge.SubmitRevision(OrderId, LineId, Who.Id, Body_)

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
