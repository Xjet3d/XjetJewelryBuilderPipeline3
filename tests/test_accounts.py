"""Account/auth boundary: separation of identity data, provider abstraction, migration."""

import re
import sqlite3
from pathlib import Path

import pytest

from p3.accounts import InsufficientCredits, Principal
from p3.accounts.local import HashToken, LocalAccountProvider
from p3.db import OrderTables, Schema, SessionTables, CustomizationsDdl
from p3.providers import endpoints
from tests.conftest import Harness

RepoP3 = Path(__file__).resolve().parent.parent / "p3"


def _Tables(DbPath) -> set:
    with sqlite3.connect(DbPath) as Conn:
        return {R[0] for R in Conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_identity_lives_in_its_own_database(H):
    App = _Tables(H.Settings.DbPath)
    Acc = _Tables(H.Settings.DataDir / "accounts.db")
    assert not App & {"access_tokens", "usage_events", "accounts"}
    assert {"accounts", "access_tokens", "usage_events"} <= Acc
    assert not Acc & {"designs", "batches", "candidates", "bag_lines"}


def test_account_ids_are_namespaced_and_tokens_are_looked_up_by_hash(H):
    assert re.fullmatch(r"p3local:acct_[0-9a-f]{32}", H.Who.AccountId)
    assert re.fullmatch(r"[A-Z]{6}", H.Token)                               # P2-style access token
    with sqlite3.connect(H.Settings.DataDir / "accounts.db") as Conn:
        # Authentication is by hash; the plaintext lives only in accounts.delivery_token, which the
        # admin table shows and registration re-sends (as P2, which stores tokens in plaintext).
        assert Conn.execute("SELECT COUNT(*) FROM access_tokens WHERE token_hash = ?",
                            (HashToken(H.Token),)).fetchone()[0] == 1
        assert H.Token not in [R[0] for R in Conn.execute("SELECT token_hint FROM access_tokens")]
        assert Conn.execute("SELECT delivery_token FROM accounts WHERE account_id = ?",
                            (H.Who.AccountId,)).fetchone()[0] == H.Token


async def test_application_rows_store_account_id_not_token(H):
    Batch = await H.NewDesign("Plain band")
    with sqlite3.connect(H.Settings.DbPath) as Conn:
        Owner, = Conn.execute("SELECT owner_account_id FROM designs WHERE id = ?", (Batch["design_id"],)).fetchone()
        Cols = [R[1] for R in Conn.execute("PRAGMA table_info(designs)")]
    assert Owner == H.Who.AccountId and "token" not in Cols


async def test_usage_is_reported_to_the_provider(HDevPricing):
    H = HDevPricing
    Batch = await H.NewDesign("Plain band")
    await H.Proceed(Batch["design_id"], Batch["candidates"][0]["id"])
    await H.Idle()
    assert H.Ctx.Accounts.UsageSummary(H.Who.AccountId) == {"image": 4, "movie": 1}
    Profile = (await H.Client.get("/api/session")).json()
    assert Profile["usage"] == {"image_requests": 4, "movie_requests": 1}
    assert Profile["account_id"] == H.Who.AccountId
    assert (Profile["used"], Profile["max"], Profile["remaining"]) == (2, 100, 98)   # one design request and one finished movie


async def test_deactivated_token_is_rejected(H):
    assert H.Ctx.Accounts.DeactivateToken(H.Token)
    R = await H.Client.get("/api/designs")
    assert R.status_code == 401 and R.json()["error"]["code"] == "invalid_token"
    assert R.json()["error"]["message"] == "Access token not recognised or deactivated. Please re-register."


class _NoCreditProvider(LocalAccountProvider):
    """Stands in for a future shared provider with a balance policy."""

    def AuthorizeSpend(self, Who: Principal, Kind: str, Units: int) -> None:
        raise InsufficientCredits("No generations left on this account.")


async def test_credit_policy_is_enforced_through_the_provider(tmp_path):
    H = Harness(tmp_path)
    try:
        H.Ctx.Accounts.__class__ = _NoCreditProvider
        R = await H.Client.post("/api/designs", data={"prompt": "Plain band"})
        assert R.status_code == 402 and R.json()["error"]["code"] == "quota_exhausted"
        assert H.Provider.Submissions == []                 # nothing paid was started
        with sqlite3.connect(H.Settings.DbPath) as Conn:
            assert Conn.execute("SELECT COUNT(*) FROM designs").fetchone()[0] == 0
    finally:
        await H.Close()


def test_only_the_accounts_package_touches_identity_storage():
    """Architecture guard: token/account tables are referenced only by p3/accounts and the migration."""
    Allowed = {RepoP3 / "migrations.py"}
    Offenders = []
    for Py in RepoP3.rglob("*.py"):
        if (RepoP3 / "accounts") in Py.parents or Py in Allowed:
            continue
        Text = Py.read_text(encoding="utf-8")
        for Needle in ("access_tokens", "usage_events", "token_hash", "FROM accounts"):
            if Needle in Text:
                Offenders.append(f"{Py.name}: {Needle}")
    assert Offenders == []


# ── migration of a pre-accounts (version 0) database ────────────────────────

LegacyTables = """
CREATE TABLE access_tokens (token TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
                            created_at TEXT NOT NULL);
CREATE TABLE usage_events (id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL, kind TEXT NOT NULL,
                           ref_id TEXT NOT NULL, units INTEGER NOT NULL, created_at TEXT NOT NULL);
"""


def _LegacySchema() -> str:
    S = Schema.replace(SessionTables, "").replace(OrderTables, "")   # v0 had no session or order tables
    # v0: Customize choices were per design + option only (no owner column)
    S = S.replace(CustomizationsDdl("customizations"), """
CREATE TABLE IF NOT EXISTS customizations (
    id            TEXT PRIMARY KEY,
    design_id     TEXT NOT NULL REFERENCES designs(id),
    candidate_id  TEXT NOT NULL REFERENCES candidates(id),
    material_id   TEXT NOT NULL,
    ring_size     REAL,
    quantity      INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (design_id, candidate_id)
);
""")
    S = S.replace("owner_account_id       TEXT NOT NULL,", "token TEXT NOT NULL,")
    S = S.replace("UNIQUE (owner_account_id, client_request_id)", "UNIQUE (token, client_request_id)")
    S = S.replace("owner_account_id  TEXT NOT NULL,", "token TEXT NOT NULL,")
    S = re.sub(r"CREATE INDEX IF NOT EXISTS (designs_owner|bag_lines_owner)[^;]*;", "", S)
    return LegacyTables + S


async def test_pre_accounts_database_is_migrated(tmp_path):
    Var = tmp_path / "var"; Var.mkdir()
    Db = Var / "pipeline3.db"
    with sqlite3.connect(Db) as Conn:
        Conn.executescript(_LegacySchema())
        Conn.execute("INSERT INTO access_tokens VALUES ('p3_legacy_token_value', 'Old user', 1, '2026-09-29')")
        Conn.execute("INSERT INTO designs (id, token, title, prompt, created_at, updated_at) "
                     "VALUES ('dsg_old', 'p3_legacy_token_value', 'The Old Ring', 'old', '2026-09-29', '2026-09-29')")
        Conn.execute("INSERT INTO usage_events (token, kind, ref_id, units, created_at) "
                     "VALUES ('p3_legacy_token_value', 'image', 'cand_x', 6, '2026-09-29')")

    H = Harness(tmp_path)
    try:
        assert list(Var.glob("pipeline3.pre-accounts-*.db")), "backup must be written before migrating"
        assert not _Tables(Db) & {"access_tokens", "usage_events"}
        with sqlite3.connect(Db) as Conn:
            assert Conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert "designs_v0" not in Conn.execute("SELECT group_concat(sql) FROM sqlite_master").fetchone()[0]
        Old = {"X-Access-Token": "p3_legacy_token_value"}
        Listed = (await H.Client.get("/api/designs", headers=Old)).json()["designs"]
        assert [D["title"] for D in Listed] == ["The Old Ring"]           # the old token still works
        Who = H.Ctx.Accounts.Authenticate("p3_legacy_token_value")
        assert H.Ctx.Accounts.UsageSummary(Who.AccountId) == {"image": 6}
        assert (await H.Client.get("/api/designs")).json()["designs"] == []   # other accounts can't see it
    finally:
        await H.Close()
    H2 = Harness(tmp_path)                                                    # re-running is a no-op
    try:
        assert len(list(Var.glob("pipeline3.pre-accounts-*.db"))) == 1
    finally:
        await H2.Close()


def test_last_used_is_written_at_most_once_a_minute(tmp_path):
    """Every request is authenticated: "last used" is written at most once a minute and "activated" once."""
    P = LocalAccountProvider(tmp_path / "accounts.db")
    Token, Who = P.IssueToken("t", MaxGenerations=1)
    Writes = []
    Real = P.Db.Execute
    P.Db.Execute = lambda Sql, *A, **K: (Writes.append(Sql), Real(Sql, *A, **K))[1]
    for _ in range(5):
        P.Authenticate(Token)
    assert sum("last_used_at" in W for W in Writes) <= 1 and sum("activated_at" in W for W in Writes) <= 1
    Writes.clear()
    P.Db.Execute = Real
    P.Db.Execute("UPDATE access_tokens SET last_used_at = '2000-01-01T00:00:00.000+00:00'")
    P.Db.Execute = lambda Sql, *A, **K: (Writes.append(Sql), Real(Sql, *A, **K))[1]
    P.Authenticate(Token)
    assert sum("last_used_at" in W for W in Writes) == 1 and not any("activated_at" in W for W in Writes)   # a minute later: once
