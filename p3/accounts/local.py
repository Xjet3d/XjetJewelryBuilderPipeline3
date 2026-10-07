"""LocalAccountProvider — P3's own accounts, tokens, registration and quota in accounts.db.

Behaviour mirrors Pipeline 2's token_store.py (revision 1e871734):
  * access tokens are 6 uppercase letters (profanity-filtered), entered case-insensitively;
  * every account has a credit allowance (max_generations, default 10). Since 2026-10-07 the credits follow
    p3/credits.py: reserved (tokens_reserved) when paid work is created, charged (generations_used) when a
    result is delivered — an image, a finished movie — and released when it fails; at 0 remaining no paid
    work may start (P2 _EnforceTokenQuota, extended to images);
  * self-service email registration, exactly as P2: registering mints the account's 6-letter
    token immediately but keeps it inactive (unusable, unrevealed) behind a 24 h verification
    link; verifying activates it and reveals it; re-registering a pending email re-issues the
    link; re-registering a verified email re-sends the SAME token.

Authentication looks tokens up by SHA-256 hash. Self-registered accounts also keep their token
on the account row (accounts.delivery_token), because — as in P2, which stores tokens in
plaintext — the token must be re-sent by email and shown again on an already-verified link.
Legacy "p3_..." tokens issued before this keep working.

Kept in a separate database file from P3's application data so the whole account domain can
later be replaced by (or migrated into) a shared service without touching designs or the bag.
"""

import hashlib
import random
import re
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone

from p3.accounts import AccountNotFound, AuthError, DuplicateEmail, InsufficientCredits, Principal, UsageMovie
from p3.db import Database, Now

Issuer = "p3local"
DefaultMaxGenerations = 10
VerifyTtlHours = 24
QuotaMessage = "You have used all the credits on this account. Contact us to extend your allowance."

Schema = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id    TEXT PRIMARY KEY,          -- "p3local:acct_<hex>"
    display_name  TEXT NOT NULL DEFAULT '',
    email         TEXT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS access_tokens (
    token_hash    TEXT PRIMARY KEY,          -- sha256(normalised token); plaintext is never stored
    token_hint    TEXT NOT NULL,             -- first characters, for operators
    account_id    TEXT NOT NULL REFERENCES accounts(account_id),
    label         TEXT NOT NULL DEFAULT '',
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT
);

CREATE TABLE IF NOT EXISTS usage_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT NOT NULL,
    pipeline      TEXT NOT NULL DEFAULT 'p3',
    kind          TEXT NOT NULL,
    units         INTEGER NOT NULL,
    ref_id        TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- Account lifecycle/activity that is not provider usage (sign-ins, admin actions).
CREATE TABLE IF NOT EXISTS account_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT NOT NULL,
    kind          TEXT NOT NULL,             -- sign_in | admin_created | admin_edited | deactivated | activated | removed | restored
    detail        TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS account_events_account ON account_events(account_id, created_at);
"""

# Columns added for P2-style registration and quota; applied to existing accounts.db files.
_AccountColumns = {
    "max_generations":   "ALTER TABLE accounts ADD COLUMN max_generations INTEGER NOT NULL DEFAULT 10",
    "generations_used":  "ALTER TABLE accounts ADD COLUMN generations_used INTEGER NOT NULL DEFAULT 0",
    "source":            "ALTER TABLE accounts ADD COLUMN source TEXT NOT NULL DEFAULT 'admin'",
    "verify_hash":       "ALTER TABLE accounts ADD COLUMN verify_hash TEXT",
    "verify_expires_at": "ALTER TABLE accounts ADD COLUMN verify_expires_at TEXT",
    "verified_at":       "ALTER TABLE accounts ADD COLUMN verified_at TEXT",
    "activated_at":      "ALTER TABLE accounts ADD COLUMN activated_at TEXT",
    "delivery_token":    "ALTER TABLE accounts ADD COLUMN delivery_token TEXT",   # retrievable token (shown/re-sent)
    "last_sign_in_at":   "ALTER TABLE accounts ADD COLUMN last_sign_in_at TEXT",
    "removed_at":        "ALTER TABLE accounts ADD COLUMN removed_at TEXT",       # soft remove (admin)
    "tokens_reserved":   "ALTER TABLE accounts ADD COLUMN tokens_reserved INTEGER NOT NULL DEFAULT 0",   # credits held by work in flight
}

# Provider/cost dimensions on each usage event, so cost per user can be reported later. cost_usd
# stays NULL until a provider price table exists — no cost is ever estimated or invented here.
_UsageColumns = {
    "provider":    "ALTER TABLE usage_events ADD COLUMN provider TEXT",      # mock | fal
    "endpoint":    "ALTER TABLE usage_events ADD COLUMN endpoint TEXT",      # provider model/endpoint id
    "mode":        "ALTER TABLE usage_events ADD COLUMN mode TEXT",          # mock | live
    "cost_usd":    "ALTER TABLE usage_events ADD COLUMN cost_usd REAL",
    "cost_source": "ALTER TABLE usage_events ADD COLUMN cost_source TEXT",   # e.g. price table version / invoice
    "internal":    "ALTER TABLE usage_events ADD COLUMN internal INTEGER NOT NULL DEFAULT 0",   # XJet's own work: never a credit
}

try:
    from better_profanity import profanity as _Profanity
    _Profanity.load_censor_words()
except ImportError:          # optional: tokens are still random, just not word-filtered
    _Profanity = None


def NormalizeToken(Token: str) -> str:
    """P2 tokens are case-insensitive (6 letters, stored upper-case); legacy p3_ tokens are exact."""
    Token = (Token or "").strip()
    return Token if Token.startswith("p3_") else Token.upper()


def HashToken(Token: str) -> str:
    return hashlib.sha256(NormalizeToken(Token).encode("utf-8")).hexdigest()


def _HashSecret(Secret: str) -> str:
    return hashlib.sha256(("verify:" + (Secret or "").strip()).encode("utf-8")).hexdigest()


def NewAccountId() -> str:
    return f"{Issuer}:acct_{uuid.uuid4().hex}"


def _Utc() -> datetime:
    return datetime.now(timezone.utc)


class LocalAccountProvider:
    Issuer = Issuer

    def __init__(self, DbPath):
        self.Db = Database(DbPath, SchemaSql=Schema)
        with self.Db.Transaction() as Conn:
            Existing = {R[1] for R in Conn.execute("PRAGMA table_info(accounts)")}
            for Column, Sql in _AccountColumns.items():
                if Column not in Existing:
                    Conn.execute(Sql)
            Existing = {R[1] for R in Conn.execute("PRAGMA table_info(usage_events)")}
            for Column, Sql in _UsageColumns.items():
                if Column not in Existing:
                    Conn.execute(Sql)
            Conn.execute("CREATE INDEX IF NOT EXISTS accounts_self_email ON accounts(source, email)")
            Conn.execute("CREATE INDEX IF NOT EXISTS usage_events_account ON usage_events(account_id, created_at)")

    # ── tokens ───────────────────────────────────────────────────────────
    def GenerateToken(self) -> str:
        """A unique 6-letter upper-case token, rejecting profane candidates (as P2)."""
        Rng = random.SystemRandom()
        for _ in range(200):
            Candidate = "".join(Rng.choices(string.ascii_uppercase, k=6))
            if _Profanity and _Profanity.contains_profanity(Candidate.lower()):
                continue
            if not self.Db.One("SELECT 1 AS x FROM access_tokens WHERE token_hash = ?", (HashToken(Candidate),)):
                return Candidate
        raise RuntimeError("Could not generate a clean unique token after 200 attempts")

    def _Row(self, Token: str) -> dict | None:
        return self.Db.One(
            "SELECT t.token_hash, t.active, t.label, a.* FROM access_tokens t "
            "JOIN accounts a ON a.account_id = t.account_id WHERE t.token_hash = ?", (HashToken(Token),))

    # ── authentication ───────────────────────────────────────────────────
    def Authenticate(self, Token: str | None) -> Principal:
        if not Token or not Token.strip():
            raise AuthError("token_required", "An access token is required.")
        Row = self._Row(Token)
        if Row is None:
            raise AuthError("token_not_found", "Token not found. Check the code and try again.")
        if not Row["active"] or Row["status"] != "active" or Row["removed_at"]:
            raise AuthError("token_inactive", "This token has been deactivated. Contact XJet.")
        T = Now()
        self.Db.Execute("UPDATE access_tokens SET last_used_at = ? WHERE token_hash = ?", (T, Row["token_hash"]))
        self.Db.Execute("UPDATE accounts SET activated_at = COALESCE(activated_at, ?) WHERE account_id = ?",
                        (T, Row["account_id"]))
        return Principal(AccountId=Row["account_id"], DisplayName=Row["display_name"] or "",
                         Issuer=Issuer, Attributes={"token_label": Row["label"]})

    # ── quota / usage ────────────────────────────────────────────────────
    def _Quota(self, AccountId: str) -> dict:
        Row = self.Db.One("SELECT display_name, email, generations_used, max_generations, tokens_reserved FROM accounts "
                          "WHERE account_id = ?", (AccountId,))
        Used, Max, Reserved = Row["generations_used"], Row["max_generations"], Row["tokens_reserved"] or 0
        return {"used": Used, "max": Max, "reserved": Reserved, "remaining": max(0, Max - Used - Reserved),
                "name": Row["display_name"] or "", "email": Row["email"] or ""}

    def AuthorizeSpend(self, Who: Principal, Kind: str, Units: int) -> None:
        """Reserve Units credits in one atomic step: used + reserved + Units must fit the allowance, so parallel
        requests can never overspend it (P2's rule — nothing starts at 0 remaining — extended to every paid kind)."""
        if Units <= 0:
            return
        with self.Db.Transaction() as Conn:
            N = Conn.execute("UPDATE accounts SET tokens_reserved = tokens_reserved + ? WHERE account_id = ? "
                             "AND generations_used + tokens_reserved + ? <= max_generations",
                             (int(Units), Who.AccountId, int(Units))).rowcount
        if not N:
            raise InsufficientCredits(QuotaMessage)

    def Settle(self, AccountId: str, Kind: str, RefId: str, Charged: bool, Units: int = 1) -> bool:
        """The reserved credits of one piece of work: charged (a delivered result, once per RefId — the 'generation'
        usage event is the receipt) or released (a failure)."""
        with self.Db.Transaction() as Conn:
            if Charged:
                if Conn.execute("SELECT 1 FROM usage_events WHERE kind = 'generation' AND ref_id = ?", (RefId,)).fetchone():
                    return False
                Conn.execute("UPDATE accounts SET generations_used = generations_used + ?, "
                             "tokens_reserved = MAX(0, tokens_reserved - ?) WHERE account_id = ?",
                             (int(Units), int(Units), AccountId))
                Conn.execute("INSERT INTO usage_events (account_id, kind, units, ref_id, created_at) "
                             "VALUES (?, 'generation', ?, ?, ?)", (AccountId, int(Units), RefId, Now()))
            else:
                Conn.execute("UPDATE accounts SET tokens_reserved = MAX(0, tokens_reserved - ?) WHERE account_id = ?",
                             (int(Units), AccountId))
            return True

    def Release(self, AccountId: str, Units: int) -> None:
        self.Db.Execute("UPDATE accounts SET tokens_reserved = MAX(0, tokens_reserved - ?) WHERE account_id = ?",
                        (int(Units), AccountId))

    def RebuildReservations(self, Reserved: dict) -> None:
        with self.Db.Transaction() as Conn:
            Conn.execute("UPDATE accounts SET tokens_reserved = 0")
            for AccountId, Units in Reserved.items():
                Conn.execute("UPDATE accounts SET tokens_reserved = ? WHERE account_id = ?", (int(Units), AccountId))

    def RecordUsage(self, AccountId: str, Kind: str, Units: int, RefId: str,
                    Provider: str | None = None, Endpoint: str | None = None, CostUsd: float | None = None,
                    CostSource: str | None = None, Internal: bool = False) -> None:
        Mode = None if Provider is None else ("mock" if Provider == "mock" else "live")
        self.Db.Execute("INSERT INTO usage_events (account_id, kind, units, ref_id, created_at, provider, endpoint, mode, "
                        "cost_usd, cost_source, internal) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (AccountId, Kind, int(Units), RefId, Now(), Provider, Endpoint, Mode,
                         CostUsd, CostSource if CostUsd is not None else None, 1 if Internal else 0))

    def SpendSince(self, DayIso: str) -> float:
        R = self.Db.One("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM usage_events WHERE mode = 'live' AND created_at >= ?",
                        (DayIso,))
        return float(R["s"] or 0.0)

    def UnannotatedUsageRefs(self) -> set[str]:
        """Usage events recorded before provider/endpoint were captured (for the one-time backfill)."""
        return {R["ref_id"] for R in self.Db.All(
            "SELECT DISTINCT ref_id FROM usage_events WHERE provider IS NULL AND kind != 'generation'")}

    def AnnotateUsage(self, RefId: str, Provider: str, Endpoint: str | None) -> None:
        Mode = "mock" if Provider == "mock" else "live"
        self.Db.Execute("UPDATE usage_events SET provider = ?, endpoint = COALESCE(endpoint, ?), mode = ? "
                        "WHERE ref_id = ? AND provider IS NULL", (Provider, Endpoint, Mode, RefId))

    def RecordSignIn(self, AccountId: str, Method: str) -> None:
        T = Now()
        self.Db.Execute("UPDATE accounts SET last_sign_in_at = ? WHERE account_id = ?", (T, AccountId))
        self._Event(AccountId, "sign_in", Method, T)

    def _Event(self, AccountId: str, Kind: str, Detail: str = "", At: str | None = None) -> None:
        self.Db.Execute("INSERT INTO account_events (account_id, kind, detail, created_at) VALUES (?,?,?,?)",
                        (AccountId, Kind, Detail, At or Now()))

    def CommitCharge(self, AccountId: str, Kind: str, RefId: str) -> bool:
        """The older name (P2 IncrementUsage): charge one reserved credit for a finished result, once per RefId."""
        return self.Settle(AccountId, Kind, RefId, True, 1)

    def UsageSummary(self, AccountId: str) -> dict:
        """{kind: units} of the customer's own requests (XJet's internal work for the account is left out)."""
        return {R["kind"]: R["units"] for R in self.Db.All(
            "SELECT kind, SUM(units) AS units FROM usage_events WHERE account_id = ? AND kind != 'generation' "
            "AND internal = 0 GROUP BY kind", (AccountId,))}

    def Profile(self, Who: Principal) -> dict:
        """P2 /api/token-status shape (used, max, remaining, name, email) plus P3 fields."""
        Q = self._Quota(Who.AccountId)
        Usage = self.UsageSummary(Who.AccountId)
        return {**Q, "label": Q["name"] or Who.Attributes.get("token_label", ""), "account_id": Who.AccountId,
                "issuer": Who.Issuer, "balance": Q["remaining"],
                "usage": {"image_requests": Usage.get("image", 0), "movie_requests": Usage.get("movie", 0)}}

    # ── self-service email registration (P2 /api/register + /verify) ─────
    def _SelfAccountByEmail(self, Email: str) -> dict | None:
        return self.Db.One("SELECT * FROM accounts WHERE source = 'self' AND lower(email) = ? "
                           "ORDER BY created_at DESC LIMIT 1", ((Email or "").strip().lower(),))

    def StartEmailRegistration(self, Name: str, Email: str) -> dict:
        """P2 RegisterEndpoint storage rules. Returns {"status": "verification_sent"|"verification_resent"|
        "already_registered", "name", "email", "verify_secret" (new/pending) or "token" (verified)}."""
        Name, Email = (Name or "").strip(), (Email or "").strip()
        Existing = self._SelfAccountByEmail(Email)
        if Existing and Existing["verified_at"]:          # register-once: re-send the existing token
            Token = Existing["delivery_token"] or self._RotateToken(Existing["account_id"], "self-registration")
            return {"status": "already_registered", "name": Existing["display_name"] or "", "email": Email,
                    "token": Token}
        Secret = secrets.token_urlsafe(32)
        Expires = (_Utc() + timedelta(hours=VerifyTtlHours)).isoformat()
        if Existing:   # pending → re-issue the link (P2 RefreshVerification keeps the stored name)
            self.Db.Execute("UPDATE accounts SET verify_hash = ?, verify_expires_at = ? WHERE account_id = ?",
                            (_HashSecret(Secret), Expires, Existing["account_id"]))
            return {"status": "verification_resent", "name": Name or Existing["display_name"] or "",
                    "email": Email, "verify_secret": Secret}
        # P2 CreateSelfRegistration: the token is minted now, inactive until the email is verified.
        AccountId, Token, T = NewAccountId(), self.GenerateToken(), Now()
        with self.Db.Transaction() as Conn:
            Conn.execute("INSERT INTO accounts (account_id, display_name, email, created_at, source, max_generations, "
                         "verify_hash, verify_expires_at, delivery_token) VALUES (?,?,?,?, 'self', ?,?,?,?)",
                         (AccountId, Name, Email, T, DefaultMaxGenerations, _HashSecret(Secret), Expires, Token))
            Conn.execute("INSERT INTO access_tokens (token_hash, token_hint, account_id, label, active, created_at) "
                         "VALUES (?,?,?, 'self-registration', 0, ?)", (HashToken(Token), Token[:3], AccountId, T))
        return {"status": "verification_sent", "name": Name, "email": Email, "verify_secret": Secret}

    def VerifyEmail(self, Secret: str) -> dict:
        """P2 VerifyRegistration. {"status": "verified"|"already"|"expired"|"invalid", "name", "email",
        "token" (verified / already)}."""
        Secret = (Secret or "").strip()
        Row = self.Db.One("SELECT * FROM accounts WHERE verify_hash = ?", (_HashSecret(Secret),)) if Secret else None
        if Row is None:
            return {"status": "invalid", "name": "", "email": ""}
        Base = {"name": Row["display_name"] or "", "email": Row["email"] or ""}
        if Row["verified_at"]:
            return {"status": "already", "token": Row["delivery_token"], **Base}
        if Row["verify_expires_at"] and Row["verify_expires_at"] < _Utc().isoformat():
            return {"status": "expired", **Base}
        T = Now()
        Token = Row["delivery_token"]
        with self.Db.Transaction() as Conn:
            Conn.execute("UPDATE accounts SET verified_at = ?, activated_at = COALESCE(activated_at, ?) "
                         "WHERE account_id = ?", (T, T, Row["account_id"]))
            if Token:
                Conn.execute("UPDATE access_tokens SET active = 1 WHERE token_hash = ?", (HashToken(Token),))
        if not Token:     # pending registration created before tokens were minted at registration
            Token = self._RotateToken(Row["account_id"], "self-registration")
        return {"status": "verified", "token": Token, **Base}

    def _RotateToken(self, AccountId: str, Label: str) -> str:
        """Issue a new active token for a self-registered account (retiring older ones) and keep it
        on the account for re-sending. Only used for accounts that pre-date delivery_token."""
        Token = self.GenerateToken()
        with self.Db.Transaction() as Conn:
            Conn.execute("UPDATE access_tokens SET active = 0 WHERE account_id = ?", (AccountId,))
            Conn.execute("INSERT INTO access_tokens (token_hash, token_hint, account_id, label, active, created_at) "
                         "VALUES (?,?,?,?,1,?)", (HashToken(Token), Token[:3], AccountId, Label, Now()))
            Conn.execute("UPDATE accounts SET delivery_token = ? WHERE account_id = ?", (Token, AccountId))
        return Token

    # ── administration ───────────────────────────────────────────────────
    def IssueToken(self, Label: str, DisplayName: str | None = None, Email: str | None = None,
                   AccountId: str | None = None, Token: str | None = None,
                   MaxGenerations: int = DefaultMaxGenerations) -> tuple[str, Principal]:
        """Create (or reuse AccountId for) an admin account and issue a token for it. The token is
        kept on the account (delivery_token) so the admin table can show it, as in P2."""
        Token = NormalizeToken(Token) if Token else self.GenerateToken()
        AccountId = AccountId or NewAccountId()
        T = Now()
        with self.Db.Transaction() as Conn:
            Conn.execute("INSERT OR IGNORE INTO accounts (account_id, display_name, email, created_at, max_generations) "
                         "VALUES (?,?,?,?,?)", (AccountId, DisplayName or Label, Email, T, int(MaxGenerations)))
            Conn.execute("INSERT INTO access_tokens (token_hash, token_hint, account_id, label, active, created_at) "
                         "VALUES (?,?,?,?,1,?)", (HashToken(Token), Token[:3], AccountId, Label, T))
            Conn.execute("UPDATE accounts SET delivery_token = ? WHERE account_id = ?", (Token, AccountId))
        return Token, Principal(AccountId=AccountId, DisplayName=DisplayName or Label, Issuer=Issuer)

    def SetQuota(self, AccountId: str, MaxGenerations: int | None = None, ResetUsage: bool = False) -> None:
        if MaxGenerations is not None:
            self.Db.Execute("UPDATE accounts SET max_generations = ? WHERE account_id = ?", (int(MaxGenerations), AccountId))
        if ResetUsage:
            self.Db.Execute("UPDATE accounts SET generations_used = 0 WHERE account_id = ?", (AccountId,))

    # ── migration support (local provider only) ──────────────────────────
    def ImportLegacyToken(self, Token: str, Label: str, Active: bool) -> str:
        """Import a plaintext token from a version-0 pipeline3.db; returns its account id (idempotent)."""
        Row = self.Db.One("SELECT account_id FROM access_tokens WHERE token_hash = ?", (HashToken(Token),))
        if Row:
            return Row["account_id"]
        _, Who = self.IssueToken(Label or "migrated", Token=Token)
        if not Active:
            self.DeactivateToken(Token)
        return Who.AccountId

    def ImportLegacyUsage(self, AccountId: str, Kind: str, Units: int, RefId: str, CreatedAt: str) -> None:
        self.Db.Execute("INSERT INTO usage_events (account_id, kind, units, ref_id, created_at) VALUES (?,?,?,?,?)",
                        (AccountId, Kind, int(Units), RefId, CreatedAt))

    def DeactivateToken(self, Token: str) -> bool:
        return self.Db.Execute("UPDATE access_tokens SET active = 0 WHERE token_hash = ?", (HashToken(Token),)) > 0

    # ── admin (user management) ──────────────────────────────────────────
    def _ActiveAccountByEmail(self, Email: str, Except: str | None = None) -> dict | None:
        return self.Db.One("SELECT account_id FROM accounts WHERE lower(email) = ? AND removed_at IS NULL "
                           "AND account_id != ? LIMIT 1", ((Email or "").strip().lower(), Except or ""))

    @staticmethod
    def _ValidateIdentity(Name: str, Email: str) -> tuple[str, str]:
        Name, Email = (Name or "").strip(), (Email or "").strip()
        if not Name:
            raise ValueError("Name is required.")
        if not EmailPattern.match(Email):
            raise ValueError("Please enter a valid email address.")
        return Name, Email

    @staticmethod
    def _ValidateMax(MaxGenerations) -> int:
        try:
            Max = int(MaxGenerations)
        except (TypeError, ValueError):
            raise ValueError("Max generations must be a whole number.") from None
        if not 1 <= Max <= 9999:
            raise ValueError("Max generations must be between 1 and 9999.")
        return Max

    def AdminCreate(self, Name: str, Email: str, MaxGenerations=DefaultMaxGenerations) -> dict:
        """B2C2 "Generate Token", with Name and Email required and one account per email."""
        Name, Email = self._ValidateIdentity(Name, Email)
        Max = self._ValidateMax(MaxGenerations)
        Dup = self._ActiveAccountByEmail(Email)
        if Dup:
            raise DuplicateEmail(Dup["account_id"])
        Token, Who = self.IssueToken(Name, DisplayName=Name, Email=Email, MaxGenerations=Max)
        self._Event(Who.AccountId, "admin_created")
        return self.AdminGet(Who.AccountId) | {"token": Token}

    def AdminUpdate(self, AccountId: str, Name: str, Email: str, MaxGenerations, ResetUsage: bool = False) -> dict:
        self._Account(AccountId)
        Name, Email = self._ValidateIdentity(Name, Email)
        Max = self._ValidateMax(MaxGenerations)
        Dup = self._ActiveAccountByEmail(Email, Except=AccountId)
        if Dup:
            raise DuplicateEmail(Dup["account_id"])
        self.Db.Execute("UPDATE accounts SET display_name = ?, email = ?, max_generations = ? WHERE account_id = ?",
                        (Name, Email, Max, AccountId))
        if ResetUsage:
            self.Db.Execute("UPDATE accounts SET generations_used = 0 WHERE account_id = ?", (AccountId,))
        self._Event(AccountId, "admin_edited", "usage reset" if ResetUsage else "")
        return self.AdminGet(AccountId)

    def AdminSetActive(self, AccountId: str, Active: bool) -> dict:
        """B2C2 Deactivate / Activate. Activating re-enables the account's current token."""
        A = self._Account(AccountId)
        if Active:
            Current = A["delivery_token"]
            if Current:
                self.Db.Execute("UPDATE access_tokens SET active = 1 WHERE token_hash = ?", (HashToken(Current),))
            else:
                self.Db.Execute("UPDATE access_tokens SET active = 1 WHERE token_hash = (SELECT token_hash FROM "
                                "access_tokens WHERE account_id = ? ORDER BY created_at DESC LIMIT 1)", (AccountId,))
        else:
            self.Db.Execute("UPDATE access_tokens SET active = 0 WHERE account_id = ?", (AccountId,))
        self._Event(AccountId, "activated" if Active else "deactivated")
        return self.AdminGet(AccountId)

    def AdminRemove(self, AccountId: str) -> dict:
        """Soft remove: tokens revoked, account hidden; designs and usage history are kept."""
        self._Account(AccountId)
        T = Now()
        self.Db.Execute("UPDATE access_tokens SET active = 0 WHERE account_id = ?", (AccountId,))
        self.Db.Execute("UPDATE accounts SET removed_at = COALESCE(removed_at, ?) WHERE account_id = ?", (T, AccountId))
        self._Event(AccountId, "removed", "", T)
        return self.AdminGet(AccountId)

    def AdminRestore(self, AccountId: str) -> dict:
        """Undo a soft remove. The token stays inactive until an admin activates it."""
        A = self._Account(AccountId)
        if A["email"] and self._ActiveAccountByEmail(A["email"], Except=AccountId):
            raise DuplicateEmail(self._ActiveAccountByEmail(A["email"], Except=AccountId)["account_id"])
        self.Db.Execute("UPDATE accounts SET removed_at = NULL WHERE account_id = ?", (AccountId,))
        self._Event(AccountId, "restored")
        return self.AdminGet(AccountId)

    def _Account(self, AccountId: str) -> dict:
        Row = self.Db.One("SELECT * FROM accounts WHERE account_id = ?", (AccountId,))
        if Row is None:
            raise AccountNotFound(AccountId)
        return Row

    _AdminSelect = (
        "SELECT a.*, "
        "(SELECT COUNT(*) FROM access_tokens t WHERE t.account_id = a.account_id AND t.active = 1) AS active_tokens, "
        "(SELECT MAX(t.last_used_at) FROM access_tokens t WHERE t.account_id = a.account_id) AS last_activity_at, "
        "(SELECT t.token_hint FROM access_tokens t WHERE t.account_id = a.account_id "
        " ORDER BY t.active DESC, t.created_at DESC LIMIT 1) AS token_hint "
        "FROM accounts a")

    @staticmethod
    def _AdminRow(R: dict) -> dict:
        Used, Max = R["generations_used"], R["max_generations"]
        Reserved = (dict(R).get("tokens_reserved") or 0) if hasattr(R, "keys") else 0
        if R["removed_at"]:
            Status = "removed"
        elif R["source"] == "self" and not R["verified_at"]:
            Status = "pending_verification"
        elif not R["active_tokens"] or R["status"] != "active":
            Status = "inactive"
        elif Used >= Max:
            Status = "exhausted"
        elif R["activated_at"] or R["last_activity_at"]:     # older accounts never stamped activated_at
            Status = "active"
        else:
            Status = "unused"
        return {
            "account_id": R["account_id"], "name": R["display_name"] or "", "email": R["email"] or "",
            "token": R["delivery_token"] or ((R["token_hint"] + "…") if R["token_hint"] else ""),
            "token_complete": bool(R["delivery_token"]),
            "used": Used, "max": Max, "reserved": Reserved, "remaining": max(0, Max - Used - Reserved), "status": Status,
            "source": R["source"],
            "created_at": R["created_at"], "verified_at": R["verified_at"], "first_activity_at": R["activated_at"],
            "last_activity_at": R["last_activity_at"], "last_sign_in_at": R["last_sign_in_at"],
            "removed_at": R["removed_at"],
        }

    def AdminList(self, IncludeRemoved: bool = False) -> list[dict]:
        Where = "" if IncludeRemoved else " WHERE a.removed_at IS NULL"
        return [self._AdminRow(R) for R in self.Db.All(self._AdminSelect + Where + " ORDER BY a.created_at DESC")]

    def AdminGet(self, AccountId: str) -> dict:
        Row = self.Db.One(self._AdminSelect + " WHERE a.account_id = ?", (AccountId,))
        if Row is None:
            raise AccountNotFound(AccountId)
        return self._AdminRow(Row)

    def AdminActivity(self, AccountId: str) -> dict:
        """Identity-side activity for the admin detail view: usage events and account events."""
        self._Account(AccountId)
        Usage = self.Db.All("SELECT kind, units, ref_id, provider, endpoint, mode, cost_usd, cost_source, created_at "
                            "FROM usage_events WHERE account_id = ? ORDER BY created_at", (AccountId,))
        Events = self.Db.All("SELECT kind, detail, created_at FROM account_events WHERE account_id = ? "
                             "ORDER BY created_at", (AccountId,))
        return {"usage": Usage, "events": Events}

    def ListAccounts(self) -> list[dict]:
        return self.Db.All(
            "SELECT a.account_id, a.display_name, a.email, a.source, a.status, a.created_at, a.verified_at, "
            "a.generations_used, a.max_generations, t.token_hint, t.label, t.active, t.last_used_at "
            "FROM accounts a LEFT JOIN access_tokens t ON t.account_id = a.account_id ORDER BY a.created_at")


EmailPattern = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")   # P2 _EMAIL_RE
