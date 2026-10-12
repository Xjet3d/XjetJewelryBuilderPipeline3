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
EligibleOrderStatuses = ("new", "payment_confirmed", "three_d_ready")
EligibleQuoteStatuses = ("new", "quoted", "answered")
MaxChanges, MaxEvents = 500, 200

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

CREATE TRIGGER IF NOT EXISTS fulfillment_quotes_changed AFTER UPDATE OF status, order_id, offer_json, ring_size, charm_size, quantity, material_id ON quote_requests
WHEN EXISTS (SELECT 1 FROM fulfillment_enrollments e WHERE e.entity_type = 'quote_request' AND e.entity_id = NEW.id AND e.state = 'enrolled')
BEGIN
  UPDATE fulfillment_enrollments SET source_version = source_version + 1, updated_at = {_SqlNow}
   WHERE entity_type = 'quote_request' AND entity_id = NEW.id;
  INSERT INTO fulfillment_outbox (kind, entity_type, entity_id, source_version, reason, created_at)
  SELECT 'updated', 'quote_request', NEW.id, e.source_version,
         CASE WHEN OLD.order_id IS NOT NEW.order_id THEN 'converted' WHEN OLD.status IS NOT NEW.status THEN 'status' ELSE 'offer' END, {_SqlNow}
    FROM fulfillment_enrollments e WHERE e.entity_type = 'quote_request' AND e.entity_id = NEW.id;
END;

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
        if self.Enabled and not Ctx.Db.One("SELECT 1 AS x FROM product_settings WHERE key = ?", (EnvironmentKey,)):
            # The environment's identity, generated once and kept with the data: never a hostname
            Ctx.Products.Set(EnvironmentKey, "atelier-" + secrets.token_hex(5), "system", "Fulfillment integration: this environment's id")

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
                "base_path": self.Ctx.Settings.BasePath or "/"}, "capabilities": ["changes", "requests", "events", "assets.image", "assets.production_stl"],
                "server_now": Now()}

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
        if Q["status"] == "approved":
            return False, "this quote became an order: send the order", Q
        if Q["status"] not in EligibleQuoteStatuses:
            return False, f"a quote request in status '{Q['status']}' is not sent for review", Q
        return True, None, Q

    def Enroll(self, EntityType: str, EntityId: str, By: str, Note: str = "", InheritedFrom: str | None = None) -> dict:
        """The explicit hand-over. One row per record, an outbox row in the same transaction, a line in the record's
        history. Repeating it is refused, never duplicated."""
        self._Require()
        if EntityType not in ("order", "quote_request"):
            raise HttpError(400, "invalid_entity", "An order or a quote request is sent to Fulfillment.")
        Ok, Why, Record = self.Eligibility(EntityType, EntityId)
        Id = Record["id"]
        Ref = Record["ref"] if EntityType == "order" else OrdersModule.QuoteRef(Record["request_no"])
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

    # ── the changes feed ─────────────────────────────────────────────────
    def Changes(self, Cursor: int, Limit: int) -> dict:
        Limit = max(1, min(int(Limit), MaxChanges))
        Rows = self.Ctx.Db.All("SELECT * FROM fulfillment_outbox WHERE seq > ? ORDER BY seq LIMIT ?", (max(0, int(Cursor)), Limit + 1))
        More = len(Rows) > Limit
        Rows = Rows[:Limit]
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
        """Everything Fulfillment needs about an enrolled record, read-only, without internal notes or history."""
        if EntityType == "order":
            O = self._Order(EntityId)
            E = self.Enrollment("order", O["id"])
            if not E:
                # An order made from an enrolled quote inherits the enrollment: the same requirement, now an order
                Quote = O.get("quote")
                QE = self.Enrollment("quote_request", Quote["id"]) if Quote else None
                if QE and QE["state"] == "enrolled":
                    self.Enroll("order", O["id"], QE["enrolled_by"], f"inherited from {Quote['ref']}", InheritedFrom=Quote["id"])
                    E = self.Enrollment("order", O["id"])
            if not E:
                raise HttpError(404, "not_enrolled", "This order is not with Fulfillment.")
            return self._OrderSnapshot(O, E)
        if EntityType == "quote_request":
            Q = self._Quote(EntityId)
            E = self.Enrollment("quote_request", Q["id"])
            if not E:
                raise HttpError(404, "not_enrolled", "This quote request is not with Fulfillment.")
            return self._QuoteSnapshot(Q, E)
        raise HttpError(404, "not_found", "Not found.")

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

    def _QuoteSnapshot(self, Q: dict, E: dict) -> dict:
        J = self.Orders.QuoteRequestJson(Q)
        Order = self.Ctx.Db.One("SELECT id, order_no, status, payment_status FROM orders WHERE id = ?", (Q["order_id"],)) if Q.get("order_id") else None
        Size = {"ring_size": Q["ring_size"]} if J["product_type"] == "ring" else {"charm_size": Q.get("charm_size"), "unit": "mm"}
        Line = {"line_id": None, "position": 1, "design_id": Q["design_id"], "candidate_id": Q["candidate_id"], "item_ref": Q["ring_id"], "title": Q["title"],
                "product_type": J["product_type"], "material_id": Q["material_id"], "material_label": Q["material_label"], "size": {**Size, "label": J["size_label"]},
                "quantity": Q["quantity"], "unit_price": (J["offer"] or {}).get("unit_price"), "currency": "USD", "quote_request_id": Q["id"],
                "image": {"design_id": Q["design_id"], "candidate_id": Q["candidate_id"]},
                "model": self._Model({"design_id": Q["design_id"], "candidate_id": Q["candidate_id"], "material_id": Q["material_id"],
                                      "product_type": J["product_type"], "ring_size": Q["ring_size"], "charm_size": Q.get("charm_size")})}
        return {**self._Envelope("quote_request", Q["id"], J["ref"], E),
                "quote_request": {"status": Q["status"], "requested_at": Q["created_at"], "updated_at": Q["updated_at"],
                                  "customer": {"name": (J["customer"].get("first_name", "") + " " + J["customer"].get("last_name", "")).strip(),
                                               "email": J["customer"].get("email")},
                                  "offer": J["offer"], "offer_expired": J["offer_expired"], "decided_at": Q.get("decided_at"),
                                  "payment": {"status": "not_applicable", "basis": None},
                                  "order": {"id": Order["id"], "ref": OrdersModule.OrderRef(Order["order_no"]), "status": Order["status"],
                                            "payment_status": Order["payment_status"]} if Order else None},
                "lines": [Line]}

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
        if not (self.IsEnrolled("order", self._OrderOfDesign(DesignId) or "") or self._QuoteEnrolledForDesign(DesignId)):
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

    def _QuoteEnrolledForDesign(self, DesignId: str) -> bool:
        return bool(self.Ctx.Db.One("SELECT 1 AS x FROM quote_requests q JOIN fulfillment_enrollments e ON e.entity_type = 'quote_request' AND e.entity_id = q.id "
                                    "AND e.state = 'enrolled' WHERE q.design_id = ? LIMIT 1", (DesignId,)))

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
            if EntityType in ("order", "quote_request") and EntityId and Conn.execute(
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
        State.update({K: V for K, V in Public.items() if K in ("customer_status", "summary", "parts", "review_status", "blockers", "file_state", "shipment")})
        State.update({"aggregate_versions": Versions, "last_event_type": E["event_type"], "last_event_at": E["occurred_at"], "last_received_at": T})
        Summary = str(Public.get("summary") or E.get("summary") or E["event_type"])[:300]
        if Row:
            Conn.execute("UPDATE fulfillment_projection SET aggregate_version = ?, state_json = ?, last_event_id = ?, last_event_at = ?, updated_at = ? "
                         "WHERE entity_type = ? AND entity_id = ?", (int(E["aggregate_version"]), Dumps(State), E["event_id"], E["occurred_at"], T, EntityType, EntityId))
        else:
            Conn.execute("INSERT INTO fulfillment_projection (entity_type, entity_id, aggregate_version, state_json, last_event_id, last_event_at, updated_at) "
                         "VALUES (?,?,?,?,?,?,?)", (EntityType, EntityId, int(E["aggregate_version"]), Dumps(State), E["event_id"], E["occurred_at"], T))
        Data = {"event_type": E["event_type"], "summary": Summary, "event_id": E["event_id"]}
        if EntityType == "order":
            Conn.execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)", (EntityId, "fulfillment", Dumps(Data), "fulfillment", T))
        else:
            OrdersModule.QuoteEvent(Conn, EntityId, "fulfillment", Data, "fulfillment")
        return "applied"

    def Projection(self, EntityType: str, EntityId: str) -> dict | None:
        R = self.Ctx.Db.One("SELECT * FROM fulfillment_projection WHERE entity_type = ? AND entity_id = ?", (EntityType, EntityId))
        if not R:
            return None
        return {**json.loads(R["state_json"]), "last_event_id": R["last_event_id"], "last_event_at": R["last_event_at"], "updated_at": R["updated_at"]}

    # ── what the Admin sees ──────────────────────────────────────────────
    def AdminState(self, EntityType: str, EntityId: str) -> dict:
        if not self.Enabled:
            return {"enabled": False}
        Ok, Why, Record = self.Eligibility(EntityType, EntityId)
        Id = Record["id"]
        E = self.Enrollment(EntityType, Id)
        Pull = self.LastPull()
        Newest = self.Ctx.Db.One("SELECT MAX(seq) AS s FROM fulfillment_outbox WHERE entity_type = ? AND entity_id = ?", (EntityType, Id))
        Awaiting = bool(E and E["state"] == "enrolled" and Newest and Newest["s"] and (not Pull or Newest["s"] > Pull["cursor"]))
        return {"enabled": True, "enrolled": bool(E and E["state"] == "enrolled"), "eligible": Ok and not (E and E["state"] == "enrolled"),
                "ineligible_reason": Why, "enrollment": E, "awaiting_sync": Awaiting, "last_pull_at": Pull["at"] if Pull else None,
                "projection": self.Projection(EntityType, Id)}


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

    @App_.post("/api/admin/orders/{OrderId}/fulfillment/enroll")
    async def EnrollOrder(OrderId: str, Body_: dict = Body(default={}), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        return Bridge.Enroll("order", OrderId, Who.Id, str(Body_.get("note") or ""))

    @App_.get("/api/admin/quote-requests/{RequestId}/fulfillment")
    async def QuoteFulfillment(RequestId: str, authorization: str | None = Header(None)):
        Admin(authorization)
        return Bridge.AdminState("quote_request", RequestId)

    @App_.post("/api/admin/quote-requests/{RequestId}/fulfillment/enroll")
    async def EnrollQuote(RequestId: str, Body_: dict = Body(default={}), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        return Bridge.Enroll("quote_request", RequestId, Who.Id, str(Body_.get("note") or ""))
