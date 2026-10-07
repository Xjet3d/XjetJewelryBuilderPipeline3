"""Persistent Admin sign-in: one successful key entry per browser, remembered until Sign out.

The admin key itself is never stored in the browser. A successful POST /api/admin/login creates a
server-side session (admin_sessions: a random token, hashed) and sets it as an HttpOnly cookie that
survives closed tabs, a closed browser and a restart. Each browser / device establishes its own
session (nothing is assumed about cookie sync between devices), and every session can be revoked on
the server (revoked_at). Requests still accept the key as a Bearer header (tests, scripts).

The cookie is turned into the Bearer header by a small middleware, so RequireAdmin() stays the one
authorization seam and no admin route changes.
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Request
from fastapi.responses import JSONResponse

from p3.context import Context
from p3.db import NewId, Now

CookieName = "p3_admin_session"
SessionDays = 180                 # sliding: refreshed while the admin keeps visiting
TouchMinutes = 60                 # last_seen_at is written at most this often

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
"""


def _Hash(Token: str) -> str:
    return hashlib.sha256(("admin-session:" + Token).encode("utf-8")).hexdigest()


def _Iso(Dt: datetime) -> str:
    return Dt.isoformat(timespec="milliseconds")


def Install(Ctx: Context) -> None:
    with Ctx.Db.Connect() as Conn:
        Conn.executescript(Schema)


def Login(Ctx: Context, Key: str, UserAgent: str | None) -> str | None:
    """A new session token when the key is right, else None (no detail about why)."""
    Expected = Ctx.Settings.AdminKey or ""
    Supplied = (Key or "").strip()
    if not Expected or not Supplied or not hmac.compare_digest(Supplied.encode(), Expected.encode()):
        return None
    Token = secrets.token_urlsafe(32)
    T = datetime.now(timezone.utc)
    Ctx.Db.Execute("INSERT INTO admin_sessions (id, session_hash, created_at, last_seen_at, expires_at, user_agent) "
                   "VALUES (?,?,?,?,?,?)", (NewId("adm"), _Hash(Token), _Iso(T), _Iso(T), _Iso(T + timedelta(days=SessionDays)),
                                            (UserAgent or "")[:200]))
    return Token


def Validate(Ctx: Context, Token: str | None) -> bool:
    """True for a live, unexpired, unrevoked session; slides the expiry while it is in use."""
    if not Token:
        return False
    Row = Ctx.Db.One("SELECT * FROM admin_sessions WHERE session_hash = ?", (_Hash(Token),))
    if Row is None or Row["revoked_at"]:
        return False
    T = datetime.now(timezone.utc)
    if Row["expires_at"] < _Iso(T):
        return False
    if Row["last_seen_at"] < _Iso(T - timedelta(minutes=TouchMinutes)):
        Ctx.Db.Execute("UPDATE admin_sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
                       (_Iso(T), _Iso(T + timedelta(days=SessionDays)), Row["id"]))
    return True


def Revoke(Ctx: Context, Token: str | None) -> bool:
    if not Token:
        return False
    return Ctx.Db.Execute("UPDATE admin_sessions SET revoked_at = ? WHERE session_hash = ? AND revoked_at IS NULL",
                          (Now(), _Hash(Token))) > 0


def RevokeAll(Ctx: Context) -> int:
    """Server-side kill switch: every remembered browser must sign in again."""
    return Ctx.Db.Execute("UPDATE admin_sessions SET revoked_at = ? WHERE revoked_at IS NULL", (Now(),))


def Secure(Req: Request) -> bool:
    return Req.url.scheme == "https" or (Req.headers.get("x-forwarded-proto") or "").lower() == "https"


def SetCookie(Resp: JSONResponse, Req: Request, BasePath: str, Token: str | None) -> None:
    """Set (or clear, Token=None) the session cookie: HttpOnly, SameSite=Lax, scoped to the app's path."""
    Kw = dict(key=CookieName, path=(BasePath or "") + "/", httponly=True, samesite="lax", secure=Secure(Req))
    if Token:
        Resp.set_cookie(value=Token, max_age=SessionDays * 86400, **Kw)
    else:
        Resp.delete_cookie(key=CookieName, path=Kw["path"], httponly=True, samesite="lax", secure=Kw["secure"])


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
    """A valid session cookie on /api/admin/* (and /api/dev/*) becomes the Bearer admin key for that
    request, so RequireAdmin() / RequireDeveloper() need not know about cookies."""

    @App_.middleware("http")
    async def _CookieToBearer(Req: Request, CallNext):
        Path = Req.url.path
        Rel = Path[len(Ctx.Settings.BasePath):] if Ctx.Settings.BasePath and Path.startswith(Ctx.Settings.BasePath) else Path
        if (Rel.startswith("/api/admin/") or Rel.startswith("/api/dev/")) and not Rel.endswith("/login"):
            Auth = (Req.headers.get("authorization") or "").removeprefix("Bearer ").strip()
            if not Auth and Ctx.Settings.AdminKey and any(Validate(Ctx, T) for T in Tokens(Req)):
                Headers = [(K, V) for K, V in Req.scope["headers"] if K != b"authorization"]
                Headers.append((b"authorization", ("Bearer " + Ctx.Settings.AdminKey).encode("latin-1")))
                Req.scope["headers"] = Headers
        return await CallNext(Req)
