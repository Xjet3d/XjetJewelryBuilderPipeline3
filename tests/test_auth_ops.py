"""AJW-39: migration tooling, monitoring and the production configuration guard."""

from pathlib import Path

import pytest

from p3.accounts import Principal
from p3.cli import Main as Cli
from p3.settings import LoadSettings, Settings, ValidateProduction
from tests.conftest import Harness
from tests.test_auth import Browser, SignedUp

Key = "ops-admin-key-of-24-plus-characters"


@pytest.fixture
async def M(tmp_path):
    Obj = Harness(tmp_path, AuthMode="modern", AdminKey=Key)
    yield Obj
    await Obj.Close()


async def test_the_migration_report_counts_accounts_without_changing_anything(M):
    Acc = M.Ctx.Accounts
    Acc.IssueToken("A", DisplayName="A", Email="a@example.com")
    Acc.IssueToken("B", DisplayName="B")                                         # no email: only its code can claim it
    Acc.IssueToken("C", DisplayName="C", Email="dup@example.com")
    Acc.IssueToken("D", DisplayName="D", Email="DUP@example.com")                # two accounts, one address: flagged, never auto-merged
    await SignedUp(M, Email="new@example.com", Name="New")
    R = Acc.Identity.LegacyReport()
    assert R["with_login"] == 1 and R["claimed"] == 0
    assert R["legacy_unclaimed"] == 5 and R["legacy_unclaimed_without_email"] == 2        # B and the harness' own "test" account
    assert R["duplicate_emails"] == [{"email": "dup@example.com", "accounts": 2}]
    assert Acc.Identity.LegacyReport() == R                                      # read-only


async def test_retiring_unclaimed_codes_keeps_accounts_and_credits(M):
    Acc = M.Ctx.Accounts
    Token, Who = Acc.IssueToken("Old", DisplayName="Old", Email="old@example.com", MaxGenerations=33)
    Claimed, _ = Acc.IssueToken("Claimed", DisplayName="Claimed", Email="c@example.com")
    Ticket = (await Browser(M).post("/api/auth/claim", json={"token": Claimed})).json()["ticket"]
    assert Ticket
    N = Acc.Identity.RetireUnclaimedCodes(DryRun=True)
    assert N >= 2 and Acc.Identity.LegacyReport()["legacy_unclaimed"] >= 2        # a dry run changes nothing
    Done = Acc.Identity.RetireUnclaimedCodes(DryRun=False)
    assert Done == N and Acc.Identity.LegacyReport()["legacy_unclaimed"] == 0
    assert (await Browser(M).post("/api/auth/claim", json={"token": Token})).status_code == 404     # the code is retired …
    assert Acc.Profile(Principal(Who.AccountId, "Old", Acc.Issuer))["max"] == 33   # … the account and credits are not
    # the owner recovers by email, without any code
    await Browser(M).post("/api/auth/forgot", json={"email": "old@example.com"})
    assert any("Set your password" in X["subject"] for X in M.App.state.Mailer.List())


async def test_the_cli_reports_and_retires(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("P3_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.chdir(tmp_path)
    assert Cli(["create-token", "--name", "Lou", "--email", "lou@example.com"]) == 0
    capsys.readouterr()
    assert Cli(["legacy-report"]) == 0
    assert "legacy_unclaimed: 1" in capsys.readouterr().out
    assert Cli(["retire-legacy-codes"]) == 0 and "would deactivate 1" in capsys.readouterr().out
    assert Cli(["retire-legacy-codes", "--yes"]) == 0 and "deactivated 1" in capsys.readouterr().out
    assert Cli(["legacy-report"]) == 0 and "legacy_unclaimed: 0" in capsys.readouterr().out


async def test_auth_stats_are_for_administrators_and_count_by_method(M):
    await SignedUp(M)
    assert (await Browser(M).get("/api/admin/auth-stats")).status_code in (401, 403)
    R = await Browser(M).get("/api/admin/auth-stats", headers={"Authorization": f"Bearer {Key}"})
    assert R.status_code == 200
    D = R.json()
    assert D["auth_mode"] == "modern" and D["live_sessions"] == 1 and D["sign_ins_24h"] == {"password": 1}
    assert D["links_issued_24h"] == {"verify_email": 1} and "migration" in D
    assert "dana@example.com" not in R.text and "token" not in " ".join(D).lower()


def _Prod(**Over):
    Base = dict(Env="production", Provider="fal", FalKey="k", PublicBaseUrl="https://xjetatelier.xjet3d.com", AdminHost="admin.xjetatelier.xjet3d.com",
                AdminKey="a" * 30, SigningSecret="b" * 30, MailMode="smtp", DailyAiSpendCapUsd=50.0, AuthMode="modern", AdminIdentityEnforced=True)
    return Settings(**{**dict(DataDir=Path("/srv/atelier/data"), PricingProfilePath=Path("x"), AllowUnapprovedPricing=False,
                              MockLatencyS=0, PollIntervalS=1, MaxTransientPollErrors=1), **Base, **Over})


def test_production_refuses_legacy_mode_and_an_unenforced_modern_mode():
    assert ValidateProduction(_Prod()) == []
    assert any("P3_AUTH_MODE" in P for P in ValidateProduction(_Prod(AuthMode="legacy")))
    assert any("P3_ADMIN_IDENTITY_ENFORCED" in P for P in ValidateProduction(_Prod(AdminIdentityEnforced=False)))
    assert ValidateProduction(_Prod(AuthMode="dual", AdminIdentityEnforced=False)) == []       # the transition state


def test_an_unknown_auth_mode_is_refused(monkeypatch):
    monkeypatch.setenv("P3_AUTH_MODE", "sometimes")
    with pytest.raises(ValueError, match="P3_AUTH_MODE"):
        LoadSettings()


def test_access_log_lines_do_not_carry_the_providers_code_and_state():
    import logging
    from p3.app import _RedactQuery
    Rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "GET %s HTTP/1.1" 303', ("1.2.3.4", "/api/auth/google/start?purpose=claim&ticket=tIcK3t&code=4/0AbC&state=sTaTe123"), None)
    _RedactQuery().filter(Rec)
    Text = Rec.getMessage()
    assert "4/0AbC" not in Text and "sTaTe123" not in Text and "tIcK3t" not in Text and "code=[redacted]" in Text
