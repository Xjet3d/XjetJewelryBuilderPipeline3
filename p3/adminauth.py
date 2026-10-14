"""Persistent Admin sign-in: one successful key entry per browser, remembered until Sign out.

The admin key itself is never stored in the browser. A successful POST /api/admin/login creates a
server-side session (admin_sessions: a random token, hashed) and sets it as an HttpOnly cookie that
survives closed tabs, a closed browser and a restart. Each browser / device establishes its own
session (nothing is assumed about cookie sync between devices), and every session can be revoked on
the server (revoked_at). Requests still accept the key as a Bearer header (tests, scripts).

The cookie is turned into the Bearer header by a small middleware, so RequireAdmin() stays the one
authorization seam and no admin route changes.

Isolation from other applications on the same host (production hardening, 2026-10-07): the cookie has a name of
its own (CookieName), is scoped to this app's API path only (never sent for pages or assets, never to another
app's path), and is SameSite=Strict. Exactly two things sign an admin in: this cookie and "Authorization:
Bearer <P3_ADMIN_KEY>". Any other Authorization scheme (a Basic challenge answered for another app and replayed
by the browser, Digest, …) is ignored and dropped; any other cookie is ignored; the generic cookie name of before
is cleared and never read.
"""

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Request
from fastapi.responses import JSONResponse

from p3.context import Context
from p3.db import NewId, Now

Log = logging.getLogger("p3.adminauth")

CookieName = "atelier_admin_session"          # unique to the Atelier Admin: no other app on the host sets or reads it
LegacyCookieNames = ("p3_admin_session",)     # the generic name of before 2026-10-07: cleared on sign-in/out, never read
SessionDays = 180                 # key sessions: sliding, refreshed while the admin keeps visiting
IdentityHours = 12                # identity sessions: a fixed window from the provider sign-in (no sliding)
IdentityProviders = ("google", "apple")
TouchMinutes = 60                 # last_seen_at is written at most this often


def CookiePath(BasePath: str | None) -> str:
    """Only this app's API ever receives the cookie: /<base>/api/ (the Admin API, and the customer API for the
    Admin's preview of hidden products) — not the pages, not the assets, not another app on the host."""
    return (BasePath or "") + "/api/"

Schema = """
CREATE TABLE IF NOT EXISTS admin_sessions (
    id            TEXT PRIMARY KEY,
    session_hash    TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    user_agent    TEXT,
    revoked_at    TEXT
);

-- Administrators (AJW-39): an enrolled provider identity per person. Only an operator (CLI) writes this table.
CREATE TABLE IF NOT EXISTS admin_identities (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    provider  TEXT NOT NULL CHECK (provider IN ('google', 'apple')),
    subject   TEXT NOT NULL,                 -- the provider's stable user id (not the email)
    email     TEXT NOT NULL,                 -- shown to the operator and re-checked at every sign-in
    enabled   INTEGER NOT NULL DEFAULT 1,
    added_at  TEXT NOT NULL,
    added_by  TEXT NOT NULL,
    UNIQUE (provider, subject)
);

-- Every attempt to sign in to the Admin with a provider: how an operator finds the subject to enrol.
CREATE TABLE IF NOT EXISTS admin_login_attempts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    provider  TEXT NOT NULL,
    subject   TEXT NOT NULL,
    email     TEXT,
    email_verified INTEGER NOT NULL DEFAULT 0,
    name      TEXT,
    outcome   TEXT NOT NULL,                 -- ok | not_enrolled | disabled | email_mismatch | unverified
    created_at TEXT NOT NULL
);

-- What an identity-signed administrator changed (every unsafe request) and the operator's CLI actions.
CREATE TABLE IF NOT EXISTS admin_audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    admin     TEXT NOT NULL,
    action    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_SessionColumns = {
    "admin_identity_id": "ALTER TABLE admin_sessions ADD COLUMN admin_identity_id TEXT",   # NULL: a session opened with the key
    "hard_expires_at":   "ALTER TABLE admin_sessions ADD COLUMN hard_expires_at TEXT",     # identity sessions: no sliding past this
}


def _Hash(Token: str) -> str:
    return hashlib.sha256(("admin-session:" + Token).encode("utf-8")).hexdigest()


def _Iso(Dt: datetime) -> str:
    return Dt.isoformat(timespec="milliseconds")


def Install(Ctx: Context) -> None:
    with Ctx.Db.Connect() as Conn:
        Conn.executescript(Schema)
        Existing = {R[1] for R in Conn.execute("PRAGMA table_info(admin_sessions)")}
        for Column, Sql in _SessionColumns.items():
            if Column not in Existing:
                Conn.execute(Sql)


def Login(Ctx: Context, Key: str, UserAgent: str | None) -> str | None:
    """A new session token when the key is right, else None (no detail about why)."""
    Expected = Ctx.Settings.AdminKey or ""
    Supplied = (Key or "").strip()
    if not Expected or not Supplied or not hmac.compare_digest(Supplied.encode(), Expected.encode()):
        return None
    return NewSession(Ctx, UserAgent)


def NewSession(Ctx: Context, UserAgent: str | None) -> str:
    """A fresh remembered session (the caller has already proven the key)."""
    Token = secrets.token_urlsafe(32)
    T = datetime.now(timezone.utc)
    Ctx.Db.Execute("INSERT INTO admin_sessions (id, session_hash, created_at, last_seen_at, expires_at, user_agent) "
                   "VALUES (?,?,?,?,?,?)", (NewId("adm"), _Hash(Token), _Iso(T), _Iso(T), _Iso(T + timedelta(days=SessionDays)),
                                            (UserAgent or "")[:200]))
    return Token


def Validate(Ctx: Context, Token: str | None) -> dict | None:
    """The live session row (truthy) or None. A key session slides its expiry while in use and stops working when identity
    enforcement is on; an identity session lasts IdentityHours from the sign-in and only while its administrator is enabled."""
    if not Token:
        return None
    Row = Ctx.Db.One("SELECT * FROM admin_sessions WHERE session_hash = ?", (_Hash(Token),))
    if Row is None or Row["revoked_at"]:
        return None
    T = datetime.now(timezone.utc)
    if Row["expires_at"] < _Iso(T):
        return None
    if Row["admin_identity_id"]:
        Who = Ctx.Db.One("SELECT * FROM admin_identities WHERE id = ?", (Row["admin_identity_id"],))
        if Who is None or not Who["enabled"] or (Row["hard_expires_at"] or "") < _Iso(T):
            return None
        return {**Row, "admin_name": Who["name"]}
    if Ctx.Settings.AdminIdentityEnforced:                 # the shared key opens nothing through the web any more
        return None
    if Row["last_seen_at"] < _Iso(T - timedelta(minutes=TouchMinutes)):
        Ctx.Db.Execute("UPDATE admin_sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
                       (_Iso(T), _Iso(T + timedelta(days=SessionDays)), Row["id"]))
    return Row


def NewIdentitySession(Ctx: Context, IdentityId: str, UserAgent: str | None) -> str:
    """A session for an enrolled administrator who has just signed in with the provider."""
    Token = secrets.token_urlsafe(32)
    T = datetime.now(timezone.utc)
    Hard = _Iso(T + timedelta(hours=IdentityHours))
    Ctx.Db.Execute("INSERT INTO admin_sessions (id, session_hash, created_at, last_seen_at, expires_at, user_agent, admin_identity_id, "
                   "hard_expires_at) VALUES (?,?,?,?,?,?,?,?)",
                   (NewId("adm"), _Hash(Token), _Iso(T), _Iso(T), Hard, (UserAgent or "")[:200], IdentityId, Hard))
    return Token


def Candidates(Db, Limit: int = 30) -> list[dict]:
    """Recent provider sign-in attempts to the Admin that were not (or are no longer) enrolled: what an operator reads to
    find the identity to enrol, after confirming with the person that the shown email is theirs."""
    return Db.All("SELECT provider, subject, email, email_verified, name, outcome, MAX(created_at) AS last_seen FROM admin_login_attempts "
                  "WHERE outcome != 'ok' GROUP BY provider, subject ORDER BY last_seen DESC LIMIT ?", (Limit,))


def Enroll(Db, Name: str, Provider: str, Subject: str, Email: str, By: str) -> dict:
    """Allow one provider identity to open the Admin. Operator-only (the CLI); an existing row is re-enabled."""
    if Provider not in IdentityProviders or not Subject.strip() or "@" not in Email:
        raise ValueError("Provider must be google or apple, with the provider's subject id and the person's email.")
    Email = Email.strip().lower()
    Row = Db.One("SELECT id FROM admin_identities WHERE provider = ? AND subject = ?", (Provider, Subject.strip()))
    if Row:
        Db.Execute("UPDATE admin_identities SET enabled = 1, name = ?, email = ? WHERE id = ?", (Name.strip(), Email, Row["id"]))
        Id = Row["id"]
    else:
        Id = NewId("adi")
        Db.Execute("INSERT INTO admin_identities (id, name, provider, subject, email, added_at, added_by) VALUES (?,?,?,?,?,?,?)",
                   (Id, Name.strip(), Provider, Subject.strip(), Email, Now(), By))
    Db.Execute("INSERT INTO admin_audit (admin, action, created_at) VALUES (?,?,?)", (By, f"enrol {Provider} {Email}", Now()))
    return Db.One("SELECT * FROM admin_identities WHERE id = ?", (Id,))


def SetEnabled(Db, IdentityId: str, Enabled: bool, By: str) -> bool:
    N = Db.Execute("UPDATE admin_identities SET enabled = ? WHERE id = ?", (1 if Enabled else 0, IdentityId))
    Db.Execute("INSERT INTO admin_audit (admin, action, created_at) VALUES (?,?,?)", (By, f"{'enable' if Enabled else 'disable'} {IdentityId}", Now()))
    return N > 0


def Administrators(Db) -> list[dict]:
    return Db.All("SELECT id, name, provider, email, enabled, added_at, added_by FROM admin_identities ORDER BY added_at")


def SignInOutcome(Ctx: Context, Provider: str, Claims) -> tuple[str, dict | None]:
    """('ok', identity) when this provider identity is an enabled administrator whose verified email is the enrolled one;
    otherwise (reason, None). Every attempt is recorded."""
    Row = Ctx.Db.One("SELECT * FROM admin_identities WHERE provider = ? AND subject = ?", (Provider, Claims.Subject))
    if Row is None:
        Why = "not_enrolled"
    elif not Row["enabled"]:
        Why = "disabled"
    elif not Claims.EmailVerified:
        Why = "unverified"
    elif Claims.Email.strip().lower() != Row["email"]:
        Why = "email_mismatch"
    else:
        Why = "ok"
    Ctx.Db.Execute("INSERT INTO admin_login_attempts (provider, subject, email, email_verified, name, outcome, created_at) VALUES (?,?,?,?,?,?,?)",
                   (Provider, Claims.Subject, Claims.Email, 1 if Claims.EmailVerified else 0, Claims.Name, Why, Now()))
    return Why, (Row if Why == "ok" else None)


def Audit(Ctx: Context, Admin: str, Action: str) -> None:
    Ctx.Db.Execute("INSERT INTO admin_audit (admin, action, created_at) VALUES (?,?,?)", (Admin, Action[:300], Now()))


def Revoke(Ctx: Context, Token: str | None) -> bool:
    if not Token:
        return False
    return Ctx.Db.Execute("UPDATE admin_sessions SET revoked_at = ? WHERE session_hash = ? AND revoked_at IS NULL",
                          (Now(), _Hash(Token))) > 0


def RevokeAll(Ctx: Context) -> int:
    """Server-side kill switch: every remembered browser must sign in again."""
    return Ctx.Db.Execute("UPDATE admin_sessions SET revoked_at = ? WHERE revoked_at IS NULL", (Now(),))


def BrowserScheme(Req: Request) -> str:
    """The scheme the visitor's browser used. Proto's front proxy passes it as X-Forwarded-Scheme: it reaches tron over
    HTTPS, and tron's nginx rewrites X-Forwarded-Proto to its own scheme, so that header would say https even for a
    visitor on http://proto. Elsewhere (Atelier, tron directly) the nearest proxy's X-Forwarded-Proto or the connection."""
    Outer = (Req.headers.get("x-forwarded-scheme") or "").strip().lower()
    if Outer in ("http", "https"):
        return Outer
    Proto = (Req.headers.get("x-forwarded-proto") or "").strip().lower()
    return "https" if Proto == "https" or Req.url.scheme == "https" else "http"


def Secure(Req: Request) -> bool:
    return BrowserScheme(Req) == "https"


def SetCookie(Resp: JSONResponse, Req: Request, BasePath: str, Token: str | None) -> None:
    """Set (or clear, Token=None) the session cookie: HttpOnly, SameSite=Strict, scoped to this app's API path
    (CookiePath). The generic cookie name of before is cleared at the same time, so a browser carries one cookie."""
    Kw = dict(httponly=True, samesite="strict", secure=Secure(Req))
    if Token:
        Resp.set_cookie(key=CookieName, value=Token, max_age=SessionDays * 86400, path=CookiePath(BasePath), **Kw)
    else:
        Resp.delete_cookie(key=CookieName, path=CookiePath(BasePath), **Kw)
    for Old in LegacyCookieNames:                       # set on the app's root path until 2026-10-07
        Resp.delete_cookie(key=Old, path=(BasePath or "") + "/", httponly=True, samesite="lax", secure=Kw["secure"])


def Tokens(Req: Request) -> list[str]:
    """Every p3_admin_session value the browser sent. Another app on the same host may set a cookie of the same name
    with a wider path; the browser then sends both, and the request's cookie dict keeps only one of them."""
    Out = []
    for Raw in Req.headers.getlist("cookie"):
        for Part in Raw.split(";"):
            K, _, V = Part.strip().partition("=")
            V = V.strip().strip('"')
            if K.strip() == CookieName and V and V not in Out:
                Out.append(V)
    return Out


def InstallMiddleware(App_, Ctx: Context) -> None:
    """A valid session cookie on /api/admin/* (and /api/dev/*) becomes a Bearer credential for that request, so
    RequireAdmin() / RequireDeveloper() need not know about cookies: the shared key for a key session, or the process-local
    secret (plus the administrator's id) for an identity session."""

    @App_.middleware("http")
    async def _CookieToBearer(Req: Request, CallNext):
        Path = Req.url.path
        Rel = Path[len(Ctx.Settings.BasePath):] if Ctx.Settings.BasePath and Path.startswith(Ctx.Settings.BasePath) else Path
        Who = None
        if (Rel.startswith("/api/admin/") or Rel.startswith("/api/dev/")) and not Rel.endswith("/login"):
            # Only a Bearer counts as a supplied key. A browser that once answered a Basic-auth challenge from
            # another app on the same host keeps sending "Authorization: Basic …" to every request here — that is
            # not ours, so the session cookie decides, and the header is dropped so the routes never see it.
            Raw = (Req.headers.get("authorization") or "").strip()
            Bearer = Raw[len("Bearer "):].strip() if Raw.lower().startswith("bearer ") else ""
            if Bearer.startswith(InternalPrefix):                 # the internal credential is never accepted from a client
                Bearer = ""
            Headers = list(Req.scope["headers"])
            if not Bearer:
                Headers = [(K, V) for K, V in Headers if K != b"authorization"]
                Session = next((R for R in (Validate(Ctx, T) for T in Tokens(Req)) if R), None)
                if Session and Session["admin_identity_id"]:
                    Who = Session["admin_name"]
                    Headers.append((b"authorization", f"Bearer {InternalPrefix}{Ctx.AdminInternal}.{Session['admin_identity_id']}".encode("latin-1")))
                elif Session and Ctx.Settings.AdminKey:
                    Headers.append((b"authorization", ("Bearer " + Ctx.Settings.AdminKey).encode("latin-1")))
            Req.scope["headers"] = Headers
        Resp = await CallNext(Req)
        if Who and Req.method in ("POST", "PUT", "PATCH", "DELETE"):
            try:
                Audit(Ctx, Who, f"{Req.method} {Rel} → {Resp.status_code}")
            except Exception:                                      # the audit trail must never break the request
                Log.exception("admin audit write failed")
        return Resp


InternalPrefix = "p3-internal-"
