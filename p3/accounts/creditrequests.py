"""Credit requests — a customer out of credits asks for more, an Admin approves or rejects (P1, 2026-11-12).

No payment and no automatic grant: the customer's request only records that they asked. Credits change in one place,
an Admin's approval, which adds the approved number to the account's allowance (accounts.max_generations — the same
balance every credit rule reads; nothing else about credits changes):

  * one PENDING request per account at a time — a partial unique index makes a second one impossible, so a double
    click or two tabs return the request that exists;
  * approval and rejection are single conditional updates (status = 'pending') inside one write transaction, with
    the grant in the same transaction: a double click, a refresh, a retry or two Admins at once grant once — the
    second sees the request resolved (409 credit_request_resolved);
  * every grant and rejection is written to the account's history (account_events: credits_granted,
    credit_request_rejected) with who, when and how many; the request keeps them too;
  * the suggested grant is an Admin setting (product setting credit_request_default, 5 until changed, logged);
    each approval stores its own number, so a later change never alters a past approval;
  * spam: at most MaxPerDay requests in 24 hours, and a new request waits RejectCooldownH hours after a rejection
    (an approved request can be followed at once).

The table lives in the accounts database, beside the balance it changes, so the grant and the request's resolution
are one transaction.
"""

import uuid
from datetime import datetime, timedelta, timezone

from p3.context import HttpError

Schema = """
CREATE TABLE IF NOT EXISTS credit_requests (
    id                  TEXT PRIMARY KEY,                 -- "crq_<hex>"
    account_id          TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    source              TEXT NOT NULL DEFAULT 'account',  -- blocked_action | account
    balance_at_request  INTEGER,
    requested_at        TEXT NOT NULL,
    credits_granted     INTEGER,
    balance_after       INTEGER,
    resolved_at         TEXT,
    resolved_by         TEXT,
    reason              TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS credit_requests_one_pending ON credit_requests(account_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS credit_requests_account ON credit_requests(account_id, requested_at);
CREATE INDEX IF NOT EXISTS credit_requests_status ON credit_requests(status, requested_at);
"""

DefaultKey = "credit_request_default"      # product setting: the suggested grant (Admin → Users)
DefaultGrant = 5
MaxGrant = 100
MaxPerDay = 3
RejectCooldownH = 24
Sources = ("blocked_action", "account")
PendingText = "Your request is pending approval."


def _Now() -> datetime:
    return datetime.now(timezone.utc)


def _Iso(T: datetime) -> str:
    return T.isoformat(timespec="milliseconds")


def _Db(Ctx):
    return Ctx.Accounts.Db


def Install(Ctx) -> None:
    with _Db(Ctx).Connect() as Conn:
        Conn.executescript(Schema)


def Default(Ctx) -> int:
    Stored = Ctx.Products.Get(DefaultKey)
    return DefaultGrant if Stored is None else int(Stored)


def ValidateGrant(Raw, What: str = "The credits to grant") -> int:
    """A positive whole number up to MaxGrant (a JSON number; "5" or 5.5 is refused)."""
    if isinstance(Raw, bool) or not isinstance(Raw, int) or not 1 <= Raw <= MaxGrant:
        raise HttpError(400, "invalid_credit_amount", f"{What} must be a whole number from 1 to {MaxGrant}.")
    return Raw


def _Remaining(Row: dict) -> int:
    return max(0, int(Row["max_generations"]) - int(Row["generations_used"]) - int(Row.get("tokens_reserved") or 0))


def _Public(R: dict | None) -> dict | None:
    """What the customer sees of their own request (never who resolved it or the Admin's note)."""
    if not R:
        return None
    return {"id": R["id"], "status": R["status"], "requested_at": R["requested_at"], "resolved_at": R["resolved_at"],
            "credits_granted": R["credits_granted"] if R["status"] == "approved" else None}


def Latest(Ctx, AccountId: str) -> dict | None:
    return _Db(Ctx).One("SELECT * FROM credit_requests WHERE account_id = ? ORDER BY requested_at DESC LIMIT 1", (AccountId,))


def _NextAllowed(Ctx, AccountId: str, Now_: datetime) -> datetime | None:
    """When this account may send a new request (None: now). A pending request is not a wait: it is returned as is."""
    Db = _Db(Ctx)
    Waits = []
    Day = Db.All("SELECT requested_at FROM credit_requests WHERE account_id = ? AND requested_at >= ? ORDER BY requested_at",
                 (AccountId, _Iso(Now_ - timedelta(hours=24))))
    if len(Day) >= MaxPerDay:
        Waits.append(datetime.fromisoformat(Day[0]["requested_at"]) + timedelta(hours=24))
    Rej = Db.One("SELECT resolved_at FROM credit_requests WHERE account_id = ? AND status = 'rejected' "
                 "ORDER BY resolved_at DESC LIMIT 1", (AccountId,))
    if Rej and Rej["resolved_at"]:
        Until = datetime.fromisoformat(Rej["resolved_at"]) + timedelta(hours=RejectCooldownH)
        Last = Latest(Ctx, AccountId)
        if Last and Last["status"] == "rejected" and Until > Now_:
            Waits.append(Until)
    Waits = [W for W in Waits if W > Now_]
    return max(Waits) if Waits else None


def Status(Ctx, AccountId: str) -> dict:
    """The customer's view: their latest request and whether a new one can be sent now."""
    Now_ = _Now()
    L = Latest(Ctx, AccountId)
    Next = None if L and L["status"] == "pending" else _NextAllowed(Ctx, AccountId, Now_)
    return {"request": _Public(L), "can_request": not (L and L["status"] == "pending") and Next is None,
            "next_allowed_at": _Iso(Next) if Next else None}


def Create(Ctx, AccountId: str, Source: str = "account") -> dict:
    """Send a request — or return the pending one (a double click, a second tab). Never changes a balance."""
    Source = Source if Source in Sources else "account"
    Db = _Db(Ctx)
    Pending = Db.One("SELECT * FROM credit_requests WHERE account_id = ? AND status = 'pending'", (AccountId,))
    if Pending:
        return {**Status(Ctx, AccountId), "created": False}
    Now_ = _Now()
    Next = _NextAllowed(Ctx, AccountId, Now_)
    if Next:
        raise HttpError(429, "credit_request_limit", "You can send another request for credits later.",
                        Details={"next_allowed_at": _Iso(Next)})
    Id, T = "crq_" + uuid.uuid4().hex, _Iso(Now_)
    try:
        with Db.Transaction() as Conn:
            A = Conn.execute("SELECT max_generations, generations_used, tokens_reserved FROM accounts WHERE account_id = ?",
                             (AccountId,)).fetchone()
            if A is None:
                raise HttpError(404, "account_not_found", "Account not found.")
            Conn.execute("INSERT INTO credit_requests (id, account_id, status, source, balance_at_request, requested_at) "
                         "VALUES (?,?,?,?,?,?)", (Id, AccountId, "pending", Source, _Remaining(dict(A)), T))
            Conn.execute("INSERT INTO account_events (account_id, kind, detail, created_at) VALUES (?,?,?,?)",
                         (AccountId, "credit_requested", f"request {Id} ({Source})", T))
    except HttpError:
        raise
    except Exception as E:  # noqa: BLE001 — the unique pending index: another tab sent one a moment ago
        if "UNIQUE" not in str(E).upper():
            raise
        return {**Status(Ctx, AccountId), "created": False}
    return {**Status(Ctx, AccountId), "created": True}


# ── Admin ───────────────────────────────────────────────────────────────────
_AdminSelect = ("SELECT r.*, a.display_name, a.email, a.max_generations, a.generations_used, a.tokens_reserved, "
                "a.status AS account_status, a.removed_at FROM credit_requests r JOIN accounts a ON a.account_id = r.account_id")


def _AdminRow(R: dict) -> dict:
    return {"id": R["id"], "account_id": R["account_id"], "name": R["display_name"] or "", "email": R["email"] or "",
            "status": R["status"], "source": R["source"], "requested_at": R["requested_at"],
            "balance_at_request": R["balance_at_request"], "current_credits": _Remaining(R),
            "credits_granted": R["credits_granted"], "balance_after": R["balance_after"], "resolved_at": R["resolved_at"],
            "resolved_by": R["resolved_by"], "reason": R["reason"],
            "account_active": R["account_status"] == "active" and not R["removed_at"]}


def AdminList(Ctx, Limit: int = 50) -> dict:
    """Pending requests (oldest first) and the latest resolved ones, with each customer's current balance."""
    Db = _Db(Ctx)
    Pending = Db.All(_AdminSelect + " WHERE r.status = 'pending' ORDER BY r.requested_at")
    Done = Db.All(_AdminSelect + " WHERE r.status != 'pending' ORDER BY r.resolved_at DESC LIMIT ?", (int(Limit),))
    return {"pending": [_AdminRow(R) for R in Pending], "history": [_AdminRow(R) for R in Done],
            "pending_count": len(Pending), "default_credits": Default(Ctx), "max_credits": MaxGrant}


def PendingByAccount(Ctx) -> dict:
    return {R["account_id"]: R["id"] for R in _Db(Ctx).All("SELECT id, account_id FROM credit_requests WHERE status = 'pending'")}


def _Resolve(Ctx, RequestId: str, By: str, Approve: bool, Credits: int | None, Reason: str) -> dict:
    T = _Iso(_Now())
    with _Db(Ctx).Transaction() as Conn:              # BEGIN IMMEDIATE: one resolver at a time
        R = Conn.execute("SELECT * FROM credit_requests WHERE id = ?", (RequestId,)).fetchone()
        if R is None:
            raise HttpError(404, "credit_request_not_found", "Credit request not found.")
        if R["status"] != "pending":
            raise HttpError(409, "credit_request_resolved", f"This request was already {R['status']}.",
                            Details={"request_status": R["status"]})
        Aid = R["account_id"]
        if Approve:
            N = Conn.execute("UPDATE accounts SET max_generations = max_generations + ? WHERE account_id = ?",
                             (Credits, Aid)).rowcount
            if N != 1:
                raise HttpError(404, "account_not_found", "The customer's account no longer exists.")
            A = dict(Conn.execute("SELECT max_generations, generations_used, tokens_reserved FROM accounts WHERE account_id = ?",
                                  (Aid,)).fetchone())
            Done = Conn.execute("UPDATE credit_requests SET status = 'approved', credits_granted = ?, balance_after = ?, "
                                "resolved_at = ?, resolved_by = ?, reason = ? WHERE id = ? AND status = 'pending'",
                                (Credits, _Remaining(A), T, By, Reason or None, RequestId)).rowcount
            Kind, Detail = "credits_granted", f"+{Credits} credits (request {RequestId}) by {By}"
        else:
            Done = Conn.execute("UPDATE credit_requests SET status = 'rejected', resolved_at = ?, resolved_by = ?, reason = ? "
                                "WHERE id = ? AND status = 'pending'", (T, By, Reason or None, RequestId)).rowcount
            Kind, Detail = "credit_request_rejected", f"request {RequestId} by {By}" + (f": {Reason}" if Reason else "")
        if Done != 1:                                 # cannot happen under the write lock; never grant without it
            raise HttpError(409, "credit_request_resolved", "This request was already resolved.")
        Conn.execute("INSERT INTO account_events (account_id, kind, detail, created_at) VALUES (?,?,?,?)", (Aid, Kind, Detail, T))
    return _AdminRow(_Db(Ctx).One(_AdminSelect + " WHERE r.id = ?", (RequestId,)))


def Approve(Ctx, RequestId: str, Credits, By: str, Note: str = "") -> dict:
    return _Resolve(Ctx, RequestId, By, True, ValidateGrant(Credits), str(Note or "").strip()[:300])


def Reject(Ctx, RequestId: str, By: str, Reason: str = "") -> dict:
    return _Resolve(Ctx, RequestId, By, False, None, str(Reason or "").strip()[:300])
