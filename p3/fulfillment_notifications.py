"""Customer notifications derived from Fulfillment's events (Batch 8; spec §24.2–24.3; C06, C08–C10, D12, D13).

Atelier alone emails customers; Fulfillment sends events. An event that marks a customer milestone becomes a
notification intent — persisted in the transaction that projects the event, deduplicated by its business scope
(order + milestone + the revision or the parcel), so a duplicate or replayed event never makes a second one. The
dispatcher sends the pending intents with the site's mailer, one email per order and milestone even when several
parts reached it together (a short coalescing window), and only while P3_FULFILLMENT_CUSTOMER_NOTIFICATIONS is on
— off by default: intents are then recorded as *suppressed* and are never sent later (a resynchronisation or a
later activation sends no history). With a test recipient configured, every message goes there instead of to the
customer. The outcome is recorded per intent: sent (with the mailer's id), a known non-delivery (retried a few
times), or unknown (the relay may have taken it: never retried blindly — a person decides)."""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from p3.context import Context, HttpError
from p3.db import Dumps, NewId, Now
from p3.mail import FulfillmentEmail, MailNotAccepted, MailOutcomeUnknown
from p3.testrecords import ReservedEmail

Logger = logging.getLogger("p3.fulfillment.notifications")

Schema = """
CREATE TABLE IF NOT EXISTS fulfillment_notification_intents (
    id                   TEXT PRIMARY KEY,
    order_id             TEXT NOT NULL,
    milestone            TEXT NOT NULL,
    scope_key            TEXT NOT NULL UNIQUE,            -- order + milestone + revision / parcel: the deduplication key (spec §24.2)
    event_id             TEXT NOT NULL,
    event_type           TEXT NOT NULL,
    recipient            TEXT,                            -- the customer's address at the time; the test recipient replaces it at dispatch
    state                TEXT NOT NULL CHECK (state IN ('pending', 'sent', 'failed', 'unknown', 'suppressed', 'coalesced', 'resolved')),
    attempts             INTEGER NOT NULL DEFAULT 0,
    subject              TEXT,
    sent_to              TEXT,
    last_error           TEXT,
    provider_message_id  TEXT,
    coalesced_into       TEXT,
    note                 TEXT,
    payload_json         TEXT NOT NULL DEFAULT '{}',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    sent_at              TEXT
);
CREATE INDEX IF NOT EXISTS fulfillment_notification_intents_order ON fulfillment_notification_intents(order_id, created_at);
CREATE INDEX IF NOT EXISTS fulfillment_notification_intents_state ON fulfillment_notification_intents(state, created_at);
"""

# The events that are a customer milestone (spec §24.2: review approved, changes required, cannot produce, production
# started, shipped, delivered); pool assignment, tray approval, post-processing, QC and the rest are never emailed
Milestones = {"fulfillment.review_approved": "review_approved", "fulfillment.review_changes_required": "changes_required",
              "fulfillment.review_cannot_produce": "cannot_produce", "fulfillment.production_started": "production_started",
              "fulfillment.shipment_shipped": "shipped", "fulfillment.shipment_updated": "shipped", "fulfillment.shipment_delivered": "delivered"}
MilestoneLabels = {"review_approved": "Approved for production", "changes_required": "Changes required", "cannot_produce": "Changes required",
                   "production_started": "In production", "shipped": "Shipped", "delivered": "Delivered"}
MaxAttempts = 3
LoopSeconds = 30


def ScopeKey(OrderId: str, Milestone: str, Public: dict) -> str:
    """Order + milestone + what makes it a genuinely new update: the part's revision for a review, the parcel (and its
    tracking) for a shipment. Broad enough that parts of one order are one update, narrow enough that a new revision
    or a new parcel is told."""
    if Milestone in ("review_approved", "changes_required", "cannot_produce"):
        R = Public.get("review") if isinstance(Public.get("review"), dict) else {}
        return f"{OrderId}:{Milestone}:{R.get('part') or '-'}:rev{R.get('revision_no') or 0}"
    if Milestone == "production_started":
        return f"{OrderId}:production_started"
    S = Public.get("shipment") if isinstance(Public.get("shipment"), dict) else {}
    Track = f":tracking:{S.get('tracking_no') or '-'}" if Milestone == "shipped" else ""
    return f"{OrderId}:{Milestone}:{S.get('display_no') or '-'}{Track}"


class NotificationService:
    def __init__(self, Ctx: Context, Bridge):
        self.Ctx, self.Bridge = Ctx, Bridge
        with Ctx.Db.Connect() as Conn:
            Conn.executescript(Schema)

    @property
    def Enabled(self) -> bool:
        return bool(self.Ctx.Settings.FulfillmentIntegration and self.Ctx.Settings.FulfillmentCustomerNotifications)

    # ── recording, inside the projection's transaction ───────────────────
    def Intend(self, Conn, OrderId: str, Event: dict, Public: dict, T: str) -> str | None:
        """The intent for a milestone event — once per scope; nothing when the event is no milestone or a repeat."""
        Milestone = Milestones.get(Event.get("event_type") or "")
        if not Milestone:
            return None
        Key = ScopeKey(OrderId, Milestone, Public)
        if Conn.execute("SELECT 1 FROM fulfillment_notification_intents WHERE scope_key = ?", (Key,)).fetchone():
            return None
        O = Conn.execute("SELECT customer_json FROM orders WHERE id = ?", (OrderId,)).fetchone()
        Email = (json.loads(O["customer_json"]).get("email") or "").strip() if O else ""
        Test = ReservedEmail(Email)                          # a synthetic order (p3/testrecords.py): recorded, never sent
        Chosen = Milestone in tuple(self.Ctx.Settings.FulfillmentNotificationMilestones or ())    # the milestones the owner chose to e-mail
        Id = NewId("nti")
        Payload = {K: Public.get(K) for K in ("customer_status", "order_status", "review", "shipment") if Public.get(K) is not None}
        Payload["parts"] = [{"display_no": P.get("display_no"), "stage": P.get("customer_stage")} for P in (Public.get("parts") or []) if isinstance(P, dict)]
        Conn.execute("INSERT INTO fulfillment_notification_intents (id, order_id, milestone, scope_key, event_id, event_type, recipient, state, payload_json, "
                     "note, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (Id, OrderId, Milestone, Key, Event["event_id"], Event["event_type"], Email or None, "pending" if self.Enabled and not Test and Chosen else "suppressed",
                      Dumps(Payload), "test order: never emailed" if Test else (None if Chosen else "milestone not e-mailed (P3_FULFILLMENT_NOTIFICATION_MILESTONES)"), T, T))
        return Id

    # ── reading ──────────────────────────────────────────────────────────
    def _Json(self, R: dict) -> dict:
        return {**{K: V for K, V in R.items() if K != "payload_json"}, "label": MilestoneLabels.get(R["milestone"], R["milestone"]), "payload": json.loads(R["payload_json"] or "{}")}

    def List(self, State: str | None = None, OrderId: str | None = None, Limit: int = 100) -> list[dict]:
        Where, Params = [], []
        if State:
            Where.append("state = ?")
            Params.append(State)
        if OrderId:
            Where.append("order_id = ?")
            Params.append(OrderId)
        Sql = "SELECT * FROM fulfillment_notification_intents" + (" WHERE " + " AND ".join(Where) if Where else "") + " ORDER BY created_at DESC LIMIT ?"
        return [self._Json(R) for R in self.Ctx.Db.All(Sql, (*Params, max(1, min(int(Limit), 500))))]

    def Counts(self) -> dict:
        return {R["state"]: R["n"] for R in self.Ctx.Db.All("SELECT state, COUNT(*) AS n FROM fulfillment_notification_intents GROUP BY state")}

    def Get(self, Id: str) -> dict:
        R = self.Ctx.Db.One("SELECT * FROM fulfillment_notification_intents WHERE id = ?", (Id,))
        if R is None:
            raise HttpError(404, "intent_not_found", "The notification was not found.")
        return self._Json(R)

    # ── dispatch ─────────────────────────────────────────────────────────
    def Dispatch(self, Now_: str | None = None) -> dict:
        """Send what is pending and older than the coalescing window: one email per order and milestone (the newest
        intent; the others of the group are marked coalesced into it). Never while sending is off."""
        if not self.Enabled:
            return {"skipped": "customer notifications are off (P3_FULFILLMENT_CUSTOMER_NOTIFICATIONS)", "sent": 0}
        T = Now_ or Now()
        Cutoff = (datetime.fromisoformat(T) - timedelta(seconds=self.Ctx.Settings.FulfillmentNotificationWindowS)).isoformat(timespec="milliseconds")
        Rows = self.Ctx.Db.All("SELECT * FROM fulfillment_notification_intents WHERE state = 'pending' AND created_at <= ? ORDER BY order_id, milestone, created_at", (Cutoff,))
        Groups: dict = {}
        for R in Rows:
            Groups.setdefault((R["order_id"], R["milestone"]), []).append(R)
        Out = {"sent": 0, "failed": 0, "unknown": 0, "coalesced": 0, "pending": 0}
        for (_OrderId, _Milestone), Members in Groups.items():
            Newest = Members[-1]
            for Older in Members[:-1]:
                self.Ctx.Db.Execute("UPDATE fulfillment_notification_intents SET state = 'coalesced', coalesced_into = ?, updated_at = ? WHERE id = ? AND state = 'pending'",
                                    (Newest["id"], T, Older["id"]))
                Out["coalesced"] += 1
            Out[self._Send(Newest, T)] += 1
        return Out

    def _Send(self, Intent: dict, T: str) -> str:
        """One intent: the attempt is persisted before the mailer is called; the outcome afterwards."""
        Order = self.Bridge.Orders.AdminGet(Intent["order_id"])
        View = self.Bridge.CustomerView(Intent["order_id"]) or {}
        To = self.Ctx.Settings.FulfillmentNotificationRecipient or Intent["recipient"]
        if not To:
            self.Ctx.Db.Execute("UPDATE fulfillment_notification_intents SET state = 'failed', last_error = ?, updated_at = ? WHERE id = ?", ("no recipient", T, Intent["id"]))
            return "failed"
        Base = (self.Ctx.Settings.PublicBaseUrl or "").rstrip("/")
        AccountUrl = f"{Base}{self.Ctx.Settings.BasePath or ''}/#account" if Base else ""
        Subject, Html = FulfillmentEmail(Intent["milestone"], Order, View, json.loads(Intent["payload_json"] or "{}"), AccountUrl)
        Attempt = Intent["attempts"] + 1
        self.Ctx.Db.Execute("UPDATE fulfillment_notification_intents SET attempts = ?, subject = ?, sent_to = ?, updated_at = ? WHERE id = ?", (Attempt, Subject, To, T, Intent["id"]))
        Mailer = self.Ctx.Mailer
        try:
            Mid = Mailer.Send(To, Subject, Html)
        except MailNotAccepted as E:                            # known non-delivery: a retry cannot duplicate it
            State = "failed" if Attempt >= MaxAttempts else "pending"
            self.Ctx.Db.Execute("UPDATE fulfillment_notification_intents SET state = ?, last_error = ?, updated_at = ? WHERE id = ?", (State, str(E)[:300], T, Intent["id"]))
            Logger.warning("Fulfillment notification %s not accepted (attempt %d): %s", Intent["id"], Attempt, E)
            return "failed" if State == "failed" else "pending"
        except (MailOutcomeUnknown, Exception) as E:  # noqa: BLE001 — anything else is an unknown outcome: never retried by itself
            self.Ctx.Db.Execute("UPDATE fulfillment_notification_intents SET state = 'unknown', last_error = ?, updated_at = ? WHERE id = ?", (str(E)[:300], T, Intent["id"]))
            Logger.error("Fulfillment notification %s: delivery outcome unknown: %s", Intent["id"], E)
            return "unknown"
        self.Ctx.Db.Execute("UPDATE fulfillment_notification_intents SET state = 'sent', provider_message_id = ?, sent_at = ?, updated_at = ? WHERE id = ?",
                            (str(Mid or "")[:200] or None, T, T, Intent["id"]))
        self.Ctx.Db.Execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                            (Intent["order_id"], "email", Dumps({"type": "fulfillment_" + Intent["milestone"], "to": To, "delivery": getattr(Mailer, "Mode", "?"),
                                                                  "intent_id": Intent["id"], "redirected": bool(self.Ctx.Settings.FulfillmentNotificationRecipient)}), "fulfillment", T))
        return "sent"

    async def Loop(self) -> None:
        """The periodic dispatch while the application runs (the Lifespan starts it only when sending is on)."""
        while True:
            try:
                await asyncio.to_thread(self.Dispatch)
            except Exception as E:  # noqa: BLE001
                Logger.exception("Fulfillment notification dispatch failed: %s", E)
            await asyncio.sleep(LoopSeconds)

    # ── the Admin's hand on it ───────────────────────────────────────────
    def Retry(self, Id: str, Who: str, Note: str = "") -> dict:
        """A failed intent is sent again; an unknown one only when a person checked that the relay did not take it."""
        R = self.Get(Id)
        if R["state"] not in ("failed", "unknown"):
            raise HttpError(409, "not_retryable", f"A notification in state {R['state']} is not sent again.")
        if R["state"] == "unknown" and len((Note or "").strip()) < 5:
            raise HttpError(400, "note_required", "An unknown outcome is sent again only after someone checked the relay and the outbox: say what was found.")
        T = Now()
        self.Ctx.Db.Execute("UPDATE fulfillment_notification_intents SET state = 'pending', attempts = 0, last_error = NULL, note = ?, updated_at = ? WHERE id = ?",
                            (f"{Who}: retry — {(Note or '').strip()[:300]}".strip(" —"), T, Id))
        return self.Get(Id)

    def Resolve(self, Id: str, Who: str, Note: str) -> dict:
        """An unknown outcome settled by a person (the customer confirmed, the relay log showed it): closed with the note."""
        R = self.Get(Id)
        if R["state"] != "unknown":
            raise HttpError(409, "not_unknown", "Only a notification with an unknown outcome is resolved by hand.")
        if len((Note or "").strip()) < 5:
            raise HttpError(400, "note_required", "Say how the outcome was established.")
        T = Now()
        self.Ctx.Db.Execute("UPDATE fulfillment_notification_intents SET state = 'resolved', note = ?, updated_at = ? WHERE id = ?", (f"{Who}: {Note.strip()[:300]}", T, Id))
        return self.Get(Id)
