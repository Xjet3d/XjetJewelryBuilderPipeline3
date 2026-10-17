"""Operator CLI (acts on Pipeline 3's own data only; tokens go through the account provider).

  python -m p3.cli create-token --label "QA tester" [--name "Dana" --email d@x.com --max-generations 10]
  python -m p3.cli set-quota <token> --max-generations 20 [--reset-usage]
  python -m p3.cli list-tokens
  python -m p3.cli deactivate-token <token>
  python -m p3.cli set-username-login ory [--name "Ory"]   # a username + password sign-in with no email; password is prompted for
  python -m p3.cli check-provider     # verify FAL_KEY with a free storage upload (no generation)

Administrator access (AJW-39). The Admin opens only for enrolled Google / Apple identities; these commands are the only way to
change who they are, and they need the break-glass key (P3_ADMIN_KEY) because they run on the server, not on the web:
  python -m p3.cli admin-candidates                  # provider sign-ins that were refused: the identities waiting to be enrolled
  python -m p3.cli admin-enroll --name Dov --provider google --subject <id> --email dov@x.com --admin-key …
  python -m p3.cli admin-list
  python -m p3.cli admin-disable <id> | admin-enable <id> --admin-key …
  python -m p3.cli admin-revoke-sessions --admin-key …   # every signed-in administrator must sign in again

Legacy six-letter codes (AJW-39):
  python -m p3.cli legacy-report                     # read-only: how many accounts still use a code, duplicates, accounts without email
  python -m p3.cli retire-legacy-codes [--yes]       # end of the transition: deactivate codes never exchanged (dry run without --yes)
"""

import argparse
import asyncio
import io

from p3.accounts import BuildProvider
from p3.migrations import MigrateToAccounts
from p3.settings import LoadSettings


def Main(Argv=None) -> int:
    Parser = argparse.ArgumentParser(prog="p3.cli")
    Sub = Parser.add_subparsers(dest="cmd", required=True)
    Create = Sub.add_parser("create-token")
    Create.add_argument("--label", default="")
    Create.add_argument("--name", default=None)
    Create.add_argument("--email", default=None)
    Create.add_argument("--max-generations", type=int, default=10)
    Quota = Sub.add_parser("set-quota")
    Quota.add_argument("token")
    Quota.add_argument("--max-generations", type=int, default=None)
    Quota.add_argument("--reset-usage", action="store_true")
    Sub.add_parser("list-tokens")
    Deact = Sub.add_parser("deactivate-token"); Deact.add_argument("token")
    Sub.add_parser("check-provider")
    Username = Sub.add_parser("set-username-login", help="create or reset a username + password login (no email)")
    Username.add_argument("username")
    Username.add_argument("--name", default="")
    Sub.add_parser("legacy-report")
    Retire = Sub.add_parser("retire-legacy-codes")
    Retire.add_argument("--yes", action="store_true", help="really deactivate (without it only the count is shown)")
    Sub.add_parser("admin-candidates")
    Sub.add_parser("admin-list")
    Enrol = Sub.add_parser("admin-enroll")
    Enrol.add_argument("--name", required=True)
    Enrol.add_argument("--provider", required=True, choices=("google", "apple"))
    Enrol.add_argument("--subject", required=True)
    Enrol.add_argument("--email", required=True)
    for Name in ("admin-enroll", "admin-disable", "admin-enable", "admin-revoke-sessions"):
        P = Sub.choices[Name] if Name in Sub.choices else Sub.add_parser(Name)
        P.add_argument("--admin-key", default=None, help="the break-glass key (P3_ADMIN_KEY); prompted for when omitted")
        if Name in ("admin-disable", "admin-enable"):
            P.add_argument("admin_id")
    Args = Parser.parse_args(Argv)
    if Args.cmd == "check-provider":
        return CheckProvider()
    if Args.cmd.startswith("admin-"):
        return AdminCommand(Args)
    S = LoadSettings()
    Accounts = BuildProvider(S.AccountProvider, S.DataDir)
    MigrateToAccounts(S.DbPath, Accounts)     # keeps pre-accounts tokens working even before the server starts
    print(f"# account provider: {S.AccountProvider} ({S.DataDir})")
    if Args.cmd == "create-token":
        Token, Who = Accounts.IssueToken(Args.label or (Args.name or ""), DisplayName=Args.name, Email=Args.email,
                                         MaxGenerations=Args.max_generations)
        print(Token)
        print(f"# account: {Who.AccountId}, {Args.max_generations} credits  (the token is shown only once)")
    elif Args.cmd == "set-quota":
        Who = Accounts.Authenticate(Args.token)
        Accounts.SetQuota(Who.AccountId, Args.max_generations, Args.reset_usage)
        P = Accounts.Profile(Who)
        print(f"{P['remaining']} of {P['max']} credits remaining ({Who.AccountId})")
    elif Args.cmd == "set-username-login":
        import getpass
        from p3.accounts.identity import IdentityError
        Password = getpass.getpass(f"Password for {Args.username}: ")
        if Password != getpass.getpass("Again: "):
            print("The passwords differ.")
            return 2
        try:
            AccountId, Created = Accounts.Identity.SetUsernamePassword(Args.username, Password, Args.name)
        except IdentityError as E:
            print(E.Message)
            return 2
        print(f"{'created' if Created else 'password reset for'} {Args.username.strip().lower()} (account {AccountId})")
    elif Args.cmd == "legacy-report":
        R = Accounts.Identity.LegacyReport()
        for K, V in R.items():
            print(f"{K}: {V}")
    elif Args.cmd == "retire-legacy-codes":
        N = Accounts.Identity.RetireUnclaimedCodes(DryRun=not Args.yes)
        print(f"{'deactivated' if Args.yes else 'would deactivate'} {N} unclaimed sign-in codes (accounts, credits and history are untouched)")
    elif Args.cmd == "list-tokens":
        for R in Accounts.ListAccounts():
            State = "active" if R["active"] else "inactive"
            Gen = f"{R['generations_used']}/{R['max_generations']}"
            Name = R["display_name"] or R["label"] or ""
            print(f"{R['token_hint']}...\t{State}\t{Gen}\t{R['source']}\t{R['account_id']}\t{Name}\t{R['email'] or ''}")
    elif Args.cmd == "deactivate-token":
        print("deactivated" if Accounts.DeactivateToken(Args.token) else "not found")
    return 0


def AdminCommand(Args) -> int:
    """Who may open the Admin. Writes need the break-glass key: possession of the server's secret, on the server."""
    import getpass
    import hmac
    from types import SimpleNamespace
    from p3 import adminauth
    from p3.db import Database, Now
    S = LoadSettings()
    Db = Database(S.DbPath)
    adminauth.Install(SimpleNamespace(Db=Db))
    if Args.cmd == "admin-candidates":
        for C in adminauth.Candidates(Db):
            print(f"{C['provider']}\t{C['subject']}\t{C['email'] or ''}\t{'verified' if C['email_verified'] else 'UNVERIFIED'}\t"
                  f"{C['name'] or ''}\t{C['outcome']}\t{C['last_seen']}")
        return 0
    if Args.cmd == "admin-list":
        for A in adminauth.Administrators(Db):
            print(f"{A['id']}\t{'enabled' if A['enabled'] else 'DISABLED'}\t{A['provider']}\t{A['name']}\t{A['email']}\t{A['added_at']}")
        return 0
    if not S.AdminKey:
        print("Break-glass is disabled: P3_ADMIN_KEY is not set on this server.")
        return 2
    Key = Args.admin_key if Args.admin_key is not None else getpass.getpass("Break-glass key (P3_ADMIN_KEY): ")
    if not hmac.compare_digest(Key.encode(), S.AdminKey.encode()):
        print("The key was not accepted.")
        return 2
    By = "cli"
    if Args.cmd == "admin-enroll":
        A = adminauth.Enroll(Db, Args.name, Args.provider, Args.subject, Args.email, By)
        print(f"enrolled {A['id']}: {A['name']} <{A['email']}> via {A['provider']} (subject {A['subject']})")
    elif Args.cmd in ("admin-disable", "admin-enable"):
        print("done" if adminauth.SetEnabled(Db, Args.admin_id, Args.cmd == "admin-enable", By) else "not found")
    elif Args.cmd == "admin-revoke-sessions":
        N = Db.Execute("UPDATE admin_sessions SET revoked_at = ? WHERE revoked_at IS NULL", (Now(),))
        adminauth.Audit(SimpleNamespace(Db=Db), By, "revoke all admin sessions")
        print(f"{N} administrator sessions revoked")
    return 0


def CheckProvider() -> int:
    """Confirm the configured provider is reachable and the key is accepted.

    Live mode uploads a 1x1 PNG to fal storage, which exercises authentication
    without running (or paying for) any model.
    """
    try:
        S = LoadSettings()
    except ValueError as E:
        print(f"Configuration error: {E}")
        return 2
    if S.Provider == "mock":
        print("Provider: MOCK - no AI provider is called. Set P3_PROVIDER=fal and FAL_KEY for live mode.")
        return 0
    from PIL import Image
    from p3.providers.fal import FalProvider
    Buf = io.BytesIO()
    Image.new("RGB", (1, 1), (255, 255, 255)).save(Buf, format="PNG")
    try:
        Url = asyncio.run(FalProvider(S.FalKey).Upload(Buf.getvalue(), "image/png"))
    except Exception as E:
        print(f"Provider: LIVE (fal) - key check FAILED: {E}")
        return 1
    print(f"Provider: LIVE (fal) - key accepted (test upload: {Url}). No model was run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(Main())
