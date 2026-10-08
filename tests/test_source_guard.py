"""Guards against the trace of an edit applied twice: a block of code repeated right after itself.

On 2026-10-07 a repeated migration block (ADD COLUMN credit_ref, twice) stopped the service once on an existing
database; the other copies (a doubled assignment, log line or dict key) were harmless but hid the same mistake."""
import json
import sqlite3
from pathlib import Path

from p3.db import Database

Root = Path(__file__).resolve().parents[1]
Trivial = {"", "}", ")", "]", "});", "})", "},", "),", "];", "{", "(", "[", "pass", "return", "else:", "try:", "*/", "/*"}


def _RepeatedBlocks(Lines: list[str]) -> list[tuple[int, int, str]]:
    """(line, size, first line) of every block of 1..60 lines that is immediately followed by itself."""
    Out, I, N = [], 0, len(Lines)
    while I < N:
        for Size in range(min(60, (N - I) // 2), 0, -1):
            Block = Lines[I:I + Size]
            if Block == Lines[I + Size:I + 2 * Size] and any(L.strip() not in Trivial and len(L.strip()) > 3 for L in Block):
                Out.append((I + 1, Size, Block[0].strip()))
                I += 2 * Size
                break
        else:
            I += 1
    return Out


def test_no_block_of_code_is_repeated_right_after_itself():
    Files = [*Root.glob("p3/**/*.py"), *Root.glob("web/*.js"), *Root.glob("scripts/*.sh"), *Root.glob("config/*.json")]
    Found = []
    for P in Files:
        if ".min." in P.name or "tailwind" in P.name:
            continue
        Lines = P.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
        Found += [f"{P.relative_to(Root)}:{Line} ({Size} line(s)): {First[:80]}" for Line, Size, First in _RepeatedBlocks(Lines)]
    assert Found == []


def test_a_database_from_before_credit_actions_and_quote_offers_is_migrated(tmp_path):
    """A database without candidates.credit_ref and without the quote offer columns and history starts cleanly:
    every existing image slot belongs to its own batch's credit action, and the quote columns and table appear."""
    Path_ = tmp_path / "old.db"
    Database(Path_)                                                   # today's schema …
    with sqlite3.connect(Path_) as Conn:                              # … taken back to before 2026-10-07
        Conn.executescript("""
            ALTER TABLE candidates DROP COLUMN credit_ref;
            DROP INDEX quote_requests_token; DROP TABLE quote_events;
            ALTER TABLE quote_requests DROP COLUMN offer_json; ALTER TABLE quote_requests DROP COLUMN link_hash;
            ALTER TABLE quote_requests DROP COLUMN decided_at; ALTER TABLE quote_requests DROP COLUMN decision_note;
            ALTER TABLE quote_requests DROP COLUMN order_id;
        """)
        Conn.execute("PRAGMA foreign_keys=OFF")
        Conn.execute("INSERT INTO designs (id, owner_account_id, title, prompt, created_at, updated_at, ring_no) VALUES "
                     "('dsg_a', 'acc_1', 'Fil Twist', 'p', '2026-10-01', '2026-10-01', 1001)")
        Conn.execute("INSERT INTO batches (id, design_id, kind, user_text, effective_prompt, endpoint, desired_count, "
                     "config_version, created_at) VALUES ('bat_1', 'dsg_a', 'initial', 'p', 'p', 'mock', 4, 'v1', '2026-10-01')")
        Conn.execute("INSERT INTO candidates (id, batch_id, slot, status, seed, created_at, updated_at) VALUES "
                     "('cand_1', 'bat_1', 0, 'ready', 7, '2026-10-01', '2026-10-01')")
        Conn.execute("INSERT INTO quote_requests (id, owner_account_id, design_id, candidate_id, title, material_id, "
                     "material_label, ring_size, quantity, customer_json, message, status, created_at, updated_at) VALUES "
                     "('qr_1', 'acc_1', 'dsg_a', 'cand_1', 'Fil Twist', 'gold18y', '18K Yellow Gold', 7.5, 2, '{}', "
                     "'In rose gold?', 'new', '2026-10-03T10:00:00Z', '2026-10-03T10:00:00Z')")
    Db = Database(Path_)                                              # the migration runs on start, once
    assert Db.One("SELECT credit_ref FROM candidates WHERE id = 'cand_1'")["credit_ref"] == "bat_1"
    Cols = {R["name"] for R in Db.All("PRAGMA table_info(quote_requests)")}
    assert {"offer_json", "link_hash", "decided_at", "decision_note", "order_id"} <= Cols
    # The request made before the history existed starts it: its request, at its own time
    Events = Db.All("SELECT request_id, kind, data_json, by, created_at FROM quote_events")
    assert [(E["request_id"], E["kind"], E["by"], E["created_at"]) for E in Events] == [
        ("qr_1", "created", "customer", "2026-10-03T10:00:00Z")]
    assert json.loads(Events[0]["data_json"]) == {"message": "In rose gold?", "quantity": 2, "material_id": "gold18y",
                                                "size": 7.5, "earlier": 1}
    Database(Path_)                                                   # and again: nothing left to migrate, no second line
    assert Db.One("SELECT COUNT(*) AS n FROM quote_events")["n"] == 1
