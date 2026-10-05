"""Shared item IDs — one short name for one exact ring or charm, used when people talk about a design.

  R-1042        a ring design (one design journey, numbered in creation order from 1001)
  R-1042-B      option B of the first four designs (options A–D = slots 1–4)
  R-1042-R1B    option B of refinement 1 (refinements numbered in creation order)
  C-1003        a charm design — charms have their own sequence (C-1001, C-1002 …), never shared with rings
  C-1003-R1B    and the same option names

The design number is stored (designs.ring_no for rings, designs.charm_no for charms), assigned by a
database trigger on insert, so every way of creating a design gets one; the option part is derived
from the candidate's batch and slot. A number is never reused: when a legacy gallery copy is merged
into its master, its number is retired (retired_rings / retired_charms) and the trigger counts past it.
"""

from p3.products import Charm, Prefixes, Ring

First = 1001

RetiredTable = """CREATE TABLE IF NOT EXISTS retired_rings (
    ring_no      INTEGER PRIMARY KEY,
    design_id    TEXT NOT NULL,
    merged_into  TEXT NOT NULL,
    title        TEXT NOT NULL,
    retired_at   TEXT NOT NULL
)"""

RetiredCharmsTable = """CREATE TABLE IF NOT EXISTS retired_charms (
    charm_no     INTEGER PRIMARY KEY,
    design_id    TEXT NOT NULL,
    merged_into  TEXT NOT NULL,
    title        TEXT NOT NULL,
    retired_at   TEXT NOT NULL
)"""


def Install(Conn) -> None:
    """Number designs: rings in designs.ring_no (as before), charms in designs.charm_no — two separate
    sequences, each assigned by its own trigger for its own product only."""
    Cols = {R[1] for R in Conn.execute("PRAGMA table_info(designs)")}
    if "ring_no" not in Cols:
        Conn.execute("ALTER TABLE designs ADD COLUMN ring_no INTEGER")
    if "charm_no" not in Cols:
        Conn.execute("ALTER TABLE designs ADD COLUMN charm_no INTEGER")
    Missing = Conn.execute("SELECT id FROM designs WHERE ring_no IS NULL AND product_type = 'ring' "
                           "ORDER BY created_at, id").fetchall()
    if Missing:
        Next = (Conn.execute("SELECT MAX(ring_no) FROM designs").fetchone()[0] or First - 1) + 1
        for N, (Did,) in enumerate(Missing):
            Conn.execute("UPDATE designs SET ring_no = ? WHERE id = ?", (Next + N, Did))
    Conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS designs_ring_no ON designs(ring_no)")
    Conn.execute(RetiredTable)
    Conn.execute("DROP TRIGGER IF EXISTS designs_ring_no_assign")
    Conn.execute(f"""CREATE TRIGGER designs_ring_no_assign AFTER INSERT ON designs
                     WHEN NEW.ring_no IS NULL AND NEW.product_type = 'ring' BEGIN
                       UPDATE designs SET ring_no = (SELECT MAX(n) + 1 FROM (
                           SELECT COALESCE(MAX(ring_no), {First - 1}) AS n FROM designs
                           UNION ALL SELECT COALESCE(MAX(ring_no), {First - 1}) FROM retired_rings))
                       WHERE id = NEW.id;
                     END""")
    # Charms: their own numbers from C-1001
    Missing = Conn.execute("SELECT id FROM designs WHERE charm_no IS NULL AND product_type = 'charm' "
                           "ORDER BY created_at, id").fetchall()
    if Missing:
        Next = (Conn.execute("SELECT MAX(charm_no) FROM designs").fetchone()[0] or First - 1) + 1
        for N, (Did,) in enumerate(Missing):
            Conn.execute("UPDATE designs SET charm_no = ? WHERE id = ?", (Next + N, Did))
    Conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS designs_charm_no ON designs(charm_no)")
    Conn.execute(RetiredCharmsTable)
    Conn.execute("DROP TRIGGER IF EXISTS designs_charm_no_assign")
    Conn.execute(f"""CREATE TRIGGER designs_charm_no_assign AFTER INSERT ON designs
                     WHEN NEW.charm_no IS NULL AND NEW.product_type = 'charm' BEGIN
                       UPDATE designs SET charm_no = (SELECT MAX(n) + 1 FROM (
                           SELECT COALESCE(MAX(charm_no), {First - 1}) AS n FROM designs
                           UNION ALL SELECT COALESCE(MAX(charm_no), {First - 1}) FROM retired_charms))
                       WHERE id = NEW.id;
                     END""")


def Retire(Conn, RingNo, DesignId: str, MergedInto: str, Title: str, At: str) -> None:
    """Retire a ring design's number when the design is merged away; the number is never given out again."""
    if RingNo is not None:
        Conn.execute("INSERT OR REPLACE INTO retired_rings (ring_no, design_id, merged_into, title, retired_at) VALUES (?,?,?,?,?)",
                     (RingNo, DesignId, MergedInto, Title, At))


def RetireDesign(Conn, Design: dict, MergedInto: str, At: str) -> None:
    """Retire the number of a design of either product (its own sequence)."""
    if (Design.get("product_type") or Ring) == Charm:
        if Design.get("charm_no") is not None:
            Conn.execute("INSERT OR REPLACE INTO retired_charms (charm_no, design_id, merged_into, title, retired_at) VALUES (?,?,?,?,?)",
                         (Design["charm_no"], Design["id"], MergedInto, Design["title"], At))
        return
    Retire(Conn, Design.get("ring_no"), Design["id"], MergedInto, Design["title"], At)


def Retired(Db) -> list[dict]:
    """Retired numbers with where they went: R-1015 (Fil Line) → Fil Twist R-1012."""
    Out = [{"ring_id": DesignRef(R["ring_no"]), "title": R["title"], "design_id": R["design_id"], "retired_at": R["retired_at"],
            "merged_into": {"design_id": R["merged_into"], "ring_id": DesignRef(R["master_ring_no"]), "title": R["master_title"]}}
           for R in Db.All("SELECT r.*, d.title AS master_title, d.ring_no AS master_ring_no FROM retired_rings r "
                           "LEFT JOIN designs d ON d.id = r.merged_into ORDER BY r.ring_no")]
    Out += [{"ring_id": CharmRef(R["charm_no"]), "title": R["title"], "design_id": R["design_id"], "retired_at": R["retired_at"],
             "product_type": Charm,
             "merged_into": {"design_id": R["merged_into"], "ring_id": CharmRef(R["master_charm_no"]), "title": R["master_title"]}}
            for R in Db.All("SELECT r.*, d.title AS master_title, d.charm_no AS master_charm_no FROM retired_charms r "
                            "LEFT JOIN designs d ON d.id = r.merged_into ORDER BY r.charm_no")]
    return Out


def DesignRef(RingNo) -> str | None:
    return f"R-{RingNo}" if RingNo is not None else None


def CharmRef(CharmNo) -> str | None:
    return f"{Prefixes[Charm]}-{CharmNo}" if CharmNo is not None else None


def Ref(Design: dict | None) -> str | None:
    """The design's ID for its product: R-1042 for a ring, C-1003 for a charm (rows without a product are rings)."""
    if not Design:
        return None
    if (Design.get("product_type") or Ring) == Charm:
        return CharmRef(Design.get("charm_no"))
    return DesignRef(Design.get("ring_no"))


def CandidateRefs(Db, DesignIds: list[str]) -> dict[str, str]:
    """candidate id → item ID (R-1042-B / C-1003-B) for every candidate of the given designs."""
    if not DesignIds:
        return {}
    Q = ",".join("?" * len(DesignIds))
    Rows = Db.All(f"SELECT c.id, c.slot, b.id AS batch_id, b.kind, b.created_at, b.design_id, d.ring_no, d.charm_no, d.product_type "
                  f"FROM candidates c JOIN batches b ON b.id = c.batch_id JOIN designs d ON d.id = b.design_id "
                  f"WHERE b.design_id IN ({Q}) ORDER BY b.design_id, b.created_at, b.id", DesignIds)
    Refine, Out = {}, {}
    for R in Rows:
        Key = (R["design_id"], R["batch_id"])
        if R["kind"] == "refine" and Key not in Refine:
            Refine[Key] = 1 + sum(1 for K in Refine if K[0] == R["design_id"])
        Batch = f"R{Refine[Key]}" if R["kind"] == "refine" else ""
        Out[R["id"]] = f"{Ref(R)}-{Batch}{chr(ord('A') + int(R['slot']))}"
    return Out


def CandidateRef(Db, CandidateId: str) -> str | None:
    Row = Db.One("SELECT b.design_id FROM candidates c JOIN batches b ON b.id = c.batch_id WHERE c.id = ?", (CandidateId,))
    return CandidateRefs(Db, [Row["design_id"]]).get(CandidateId) if Row else None
