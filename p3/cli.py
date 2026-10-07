"""Operator CLI (acts on Pipeline 3's own data only; tokens go through the account provider).

  python -m p3.cli create-token --label "QA tester" [--name "Dana" --email d@x.com --max-generations 10]
  python -m p3.cli set-quota <token> --max-generations 20 [--reset-usage]
  python -m p3.cli list-tokens
  python -m p3.cli deactivate-token <token>
  python -m p3.cli check-provider     # verify FAL_KEY with a free storage upload (no generation)
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
    Args = Parser.parse_args(Argv)
    if Args.cmd == "check-provider":
        return CheckProvider()
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
    elif Args.cmd == "list-tokens":
        for R in Accounts.ListAccounts():
            State = "active" if R["active"] else "inactive"
            Gen = f"{R['generations_used']}/{R['max_generations']}"
            Name = R["display_name"] or R["label"] or ""
            print(f"{R['token_hint']}...\t{State}\t{Gen}\t{R['source']}\t{R['account_id']}\t{Name}\t{R['email'] or ''}")
    elif Args.cmd == "deactivate-token":
        print("deactivated" if Accounts.DeactivateToken(Args.token) else "not found")
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
