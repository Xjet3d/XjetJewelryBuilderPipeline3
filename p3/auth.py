"""HTTP access boundaries — thin adapters over the account provider.

Customer: header X-Access-Token → Ctx.Accounts.Authenticate → Principal. Nothing
in Pipeline 3 reads token storage except the provider (p3/accounts/).

Developer: the P3_ADMIN_KEY bearer secret, checked server-side on every /api/dev
route. When P3_ADMIN_KEY is unset, developer routes are disabled entirely.
"""

import hmac

from p3.accounts import AuthError, Principal
from p3.accounts.identity import SessionCookieMark
from p3.adminauth import InternalPrefix
from p3.context import Context, HttpError


# Pipeline 2 _EnforceTokenQuota message for an unknown or deactivated token on a protected route.
InvalidTokenMessage = "Access token not recognised or deactivated. Please re-register."


def RequirePrincipal(Ctx: Context, Token: str | None) -> Principal:
    """A customer session (the cookie the HTTP layer turned into "s1.<id>") or, while the server is not in "modern"
    mode, a legacy six-letter token."""
    if Token and Token.startswith(SessionCookieMark):
        Session = Ctx.Accounts.Identity.ResolveSession(Token[len(SessionCookieMark):])
        if Session is None:
            raise HttpError(401, "login_required", "Your session has ended. Please sign in again.")
        return Ctx.Accounts.Identity.PrincipalFor(Session)
    if Ctx.Settings.AuthMode == "modern":
        raise HttpError(401, "login_required", "Please sign in to continue.")
    try:
        return Ctx.Accounts.Authenticate(Token)
    except AuthError as E:
        if E.Code == "token_required":
            raise HttpError(401, "token_required", E.Message) from E
        raise HttpError(401, "invalid_token", InvalidTokenMessage) from E


def RequireDeveloper(Ctx: Context, Authorization: str | None) -> str | None:
    """Admin / developer authorization. Returns the administrator's id for an identity-signed request, else None (the key).

    An identity session reaches here as the process-local credential the middleware attached (p3/adminauth.py); a client
    can never present it. With P3_ADMIN_IDENTITY_ENFORCED the shared key is refused on the web: break-glass use is the
    operator's CLI only."""
    Supplied = (Authorization or "").removeprefix("Bearer ").strip()
    if Supplied.startswith(InternalPrefix):
        Secret, _, IdentityId = Supplied[len(InternalPrefix):].partition(".")
        if hmac.compare_digest(Secret.encode(), Ctx.AdminInternal.encode()) and IdentityId:
            return IdentityId
        raise HttpError(403, "developer_auth_required", "Developer authorization required.")
    if Ctx.Settings.AdminIdentityEnforced:
        raise HttpError(403, "admin_identity_required", "Sign in with your administrator account.")
    Expected = Ctx.Settings.AdminKey
    if not Expected:
        raise HttpError(503, "developer_tools_disabled", "Developer tools are disabled (P3_ADMIN_KEY is not set).")
    if not Supplied or not hmac.compare_digest(Supplied.encode(), Expected.encode()):
        raise HttpError(403, "developer_auth_required", "Developer authorization required.")
    return None
