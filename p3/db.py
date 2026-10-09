"""SQLite persistence for Pipeline 3 (its own database file under P3_DATA_DIR).

Unlike Pipeline 2's in-memory job registry, every batch, candidate, movie and
mesh job is a durable row carrying its provider request id, so a restarted
server can reconcile remote work instead of losing it (spec section 9).

This file holds Pipeline 3 APPLICATION data only. Accounts, tokens and usage
live behind p3.accounts (its own accounts.db); application rows reference the
owner only by owner_account_id, a namespaced provider-issued id.
"""

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SchemaVersion = 1

DesignsTable = """
CREATE TABLE IF NOT EXISTS designs (
    id                     TEXT PRIMARY KEY,
    owner_account_id       TEXT NOT NULL,
    title                  TEXT NOT NULL,
    prompt                 TEXT NOT NULL,
    selected_candidate_id  TEXT,
    client_request_id      TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    UNIQUE (owner_account_id, client_request_id)
);
"""

def BagLinesDdl(Name: str) -> str:
    """A bag line is a quote snapshot of one product: a ring carries its US size, a charm its size in mm
    (the CHECK keeps that true for every row, as the former NOT NULL ring size did for rings)."""
    return f"""
CREATE TABLE IF NOT EXISTS {Name} (
    id                TEXT PRIMARY KEY,
    owner_account_id  TEXT NOT NULL,
    design_id         TEXT NOT NULL REFERENCES designs(id),
    candidate_id      TEXT NOT NULL REFERENCES candidates(id),
    customization_id  TEXT NOT NULL REFERENCES customizations(id),
    product_type      TEXT NOT NULL DEFAULT 'ring' CHECK (product_type IN ('ring', 'charm')),
    material_id       TEXT NOT NULL,
    ring_size         REAL,                      -- rings: the US size
    charm_size        REAL,                      -- charms: the size in mm (p3/products.py: body height, loop excluded)
    quantity          INTEGER NOT NULL,
    unit_price        REAL NOT NULL,
    currency          TEXT NOT NULL,
    pricing_version   TEXT NOT NULL,
    quote_json        TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    CHECK ((product_type = 'ring' AND ring_size IS NOT NULL) OR (product_type = 'charm' AND charm_size IS NOT NULL))
);
"""


BagLinesTable = BagLinesDdl("bag_lines")

# ── Sessions (Admin analytics) ──────────────────────────────────────────────
# A session is one design journey: it starts when the customer submits the first prompt of a New
# Design (designs.created_at). Most stage times already live in the job tables (batches,
# candidates, customizations, movies); session_events adds what they do not keep — choice
# history with the fixed price shown, bag adds/removes, Bag viewed, Checkout clicked, design
# reopened — plus admin actions. Append-only, so funnels can be rebuilt at any time.
SessionTables = """
CREATE TABLE IF NOT EXISTS session_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    design_id         TEXT,                       -- NULL only for new_design_clicked
    owner_account_id  TEXT NOT NULL,
    kind              TEXT NOT NULL,
    data_json         TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS session_events_design ON session_events(design_id, created_at);
CREATE INDEX IF NOT EXISTS session_events_owner ON session_events(owner_account_id, created_at);

-- Admin-requested 3D production geometry for one session (never started automatically).
CREATE TABLE IF NOT EXISTS session_3d (
    id                 TEXT PRIMARY KEY,
    design_id          TEXT NOT NULL REFERENCES designs(id),
    candidate_id       TEXT NOT NULL REFERENCES candidates(id),
    mesh_id            TEXT REFERENCES meshes(id),
    customer_size      REAL,                      -- what the customer chose (NULL = none)
    production_size    REAL NOT NULL,             -- size the geometry is scaled to
    size_source        TEXT NOT NULL CHECK (size_source IN ('customer', 'default', 'admin_override')),
    customer_material  TEXT,
    material_id        TEXT NOT NULL,
    material_source    TEXT NOT NULL CHECK (material_source IN ('customer', 'default', 'admin_override')),
    status             TEXT NOT NULL,             -- requested | generating | measuring | measured | needs_review | failed
    requested_by       TEXT NOT NULL,
    error              TEXT,
    accepted_at        TEXT,                      -- the Admin accepted a flagged result for production as measured
    accepted_by        TEXT,
    accepted_note      TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS session_3d_design ON session_3d(design_id, created_at);

-- The raw Hi3D STL is the master geometry: measured once (exact), values for any size follow by scaling.
CREATE TABLE IF NOT EXISTS raw_geometry (
    mesh_id           TEXT PRIMARY KEY REFERENCES meshes(id),
    stl_path          TEXT NOT NULL,
    sha256            TEXT,
    bytes             INTEGER,
    faces             INTEGER,
    status            TEXT NOT NULL,              -- downloaded | measured | failed
    measurement_json  TEXT,                       -- raw ID, X/Y/Z, volume, area, frame, bore … (model units)
    method_version    TEXT,
    measured_at       TEXT,
    preview_path      TEXT,                       -- visual-only light preview (never used for numbers)
    thumbnail_path    TEXT,                       -- Hi3D's own thumbnail image
    integrity         TEXT NOT NULL DEFAULT 'pending',   -- pending | closed | open | unknown (background)
    integrity_json    TEXT,
    timings_json      TEXT NOT NULL DEFAULT '{}',
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- Persistent queue for heavy local STL work: only one job runs at a time; survives restarts.
CREATE TABLE IF NOT EXISTS geometry_jobs (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,                 -- measure | preview | integrity | export
    mesh_id        TEXT NOT NULL,
    session_3d_id  TEXT,
    priority       INTEGER NOT NULL,              -- lower runs first
    status         TEXT NOT NULL,                 -- queued | running | done | failed | cancelled
    attempts       INTEGER NOT NULL DEFAULT 0,
    params_json    TEXT NOT NULL DEFAULT '{}',
    result_json    TEXT,
    error          TEXT,
    output_path    TEXT,
    expires_at     TEXT,
    created_at     TEXT NOT NULL,
    started_at     TEXT,
    finished_at    TEXT
);
CREATE INDEX IF NOT EXISTS geometry_jobs_queue ON geometry_jobs(status, priority, created_at);

-- Inspiration Gallery: curated XJet designs shown on the customer site (the design's chosen image).
CREATE TABLE IF NOT EXISTS gallery_items (
    id            TEXT PRIMARY KEY,
    design_id     TEXT NOT NULL UNIQUE REFERENCES designs(id),
    candidate_id  TEXT NOT NULL REFERENCES candidates(id),
    position      INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    owner_kind    TEXT NOT NULL DEFAULT 'xjet',   -- xjet | customer: whose design is shown (publication needs the right to show it)
    consent_note  TEXT,                           -- a customer's consent to publication: who agreed, when, how
    consent_at    TEXT,
    consent_by    TEXT
);

-- A customer on a shared XJet master design (from the gallery): one row per customer and design.
-- The design itself is never copied; the customer's selection lives here, their Customize choices in
-- customizations (per owner), their bag lines in bag_lines.
CREATE TABLE IF NOT EXISTS gallery_uses (
    id                     TEXT PRIMARY KEY,
    design_id              TEXT NOT NULL REFERENCES designs(id),
    owner_account_id       TEXT NOT NULL,
    gallery_item_id        TEXT,
    source_candidate_id    TEXT REFERENCES candidates(id),    -- the tile's image when they started
    selected_candidate_id  TEXT REFERENCES candidates(id),    -- their own pick within the design
    started_at             TEXT NOT NULL,
    last_active_at         TEXT NOT NULL,
    removed_at             TEXT,                               -- removed from the customer's My Designs (journey kept)
    UNIQUE (design_id, owner_account_id)
);
CREATE INDEX IF NOT EXISTS gallery_uses_owner ON gallery_uses(owner_account_id, last_active_at);

-- ♥ Favorites: a customer's saved reference to a shared XJet master design. Never a copy, never a My Design;
-- the same master whoever saves it. Removing the design from the gallery hides the favorite, re-adding shows it.
CREATE TABLE IF NOT EXISTS gallery_favorites (
    owner_account_id  TEXT NOT NULL,
    design_id         TEXT NOT NULL REFERENCES designs(id),
    created_at        TEXT NOT NULL,
    PRIMARY KEY (owner_account_id, design_id)
);

-- Gallery sync (p3/gallerysync.py): every row an import from another site wrote, so a later import updates its own
-- rows only and never one this site made, and takes off the gallery the synced tiles the source no longer publishes.
CREATE TABLE IF NOT EXISTS gallery_sync_items (
    kind      TEXT NOT NULL,                  -- designs | batches | candidates | movies | gallery_items
    id        TEXT NOT NULL,
    source    TEXT NOT NULL,                  -- the site it came from ("proto")
    run_id    TEXT NOT NULL,                  -- the latest import that wrote or confirmed it
    first_at  TEXT NOT NULL,
    last_at   TEXT NOT NULL,
    PRIMARY KEY (kind, id)
);
-- Every applied import: when, from which site and bundle, what it changed, or why it failed.
CREATE TABLE IF NOT EXISTS gallery_sync_runs (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    bundle_ref    TEXT,                       -- the upload it came in (an Admin API push), or a name given to `import --ref`
    content_id    TEXT,
    exported_at   TEXT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,              -- ok | failed
    summary_json  TEXT NOT NULL DEFAULT '{}',
    error         TEXT,
    by            TEXT NOT NULL
);

-- The prompt check before paid image requests (p3/promptcheck.py): one row per check, whatever it decided.
CREATE TABLE IF NOT EXISTS prompt_checks (
    id                   TEXT PRIMARY KEY,
    owner_account_id     TEXT NOT NULL,
    kind                 TEXT NOT NULL,              -- design | refinement
    product_type         TEXT NOT NULL,
    text                 TEXT NOT NULL,              -- the customer's words
    checked_text         TEXT NOT NULL,              -- what the LLM read (with the application's context lines)
    source_design_id     TEXT,                       -- a refinement: the design it refines
    design_id            TEXT,                       -- the design it let through (set once that exists)
    batch_id             TEXT,                       -- the batch it let through
    decision             TEXT NOT NULL,              -- accepted | rejected | undecided (no decision: the request went ahead)
    reason               TEXT,                       -- a rejection: what the customer read
    refined_prompt       TEXT,                       -- the LLM's rewrite (kept for the Admin; never sent to the image model)
    config_version       TEXT NOT NULL,
    provider_request_id  TEXT,
    seconds              REAL,
    error                TEXT,                       -- no decision: why
    ai_mode              TEXT NOT NULL,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS prompt_checks_owner ON prompt_checks(owner_account_id, created_at);
CREATE INDEX IF NOT EXISTS prompt_checks_batch ON prompt_checks(batch_id);
CREATE INDEX IF NOT EXISTS prompt_checks_design ON prompt_checks(design_id);

-- Real processing stages with start/end times (Hi3D, download, queue, geometry, ready).
CREATE TABLE IF NOT EXISTS stage_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    subject      TEXT NOT NULL,
    stage        TEXT NOT NULL,
    detail_json  TEXT NOT NULL DEFAULT '{}',
    started_at   TEXT NOT NULL,
    ended_at     TEXT
);
CREATE INDEX IF NOT EXISTS stage_log_subject ON stage_log(subject, id);

-- Measured geometry: one row per stage (raw = as returned, production = repaired + scaled).
CREATE TABLE IF NOT EXISTS geometry_results (
    id                 TEXT PRIMARY KEY,
    session_3d_id      TEXT NOT NULL REFERENCES session_3d(id),
    stage              TEXT NOT NULL CHECK (stage IN ('raw', 'production')),
    size_x_mm          REAL, size_y_mm REAL, size_z_mm REAL,
    inner_diameter_mm  REAL,
    volume_mm3         REAL,
    surface_area_mm2   REAL,
    watertight         INTEGER NOT NULL,
    scale_factor       REAL,
    stl_path           TEXT,
    method_version     TEXT NOT NULL,
    checks_json        TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL
);

-- Weight / cost / price from a production geometry. Kept separate from the fixed customer price,
-- which is never changed by these numbers.
CREATE TABLE IF NOT EXISTS price_calculations (
    id                    TEXT PRIMARY KEY,
    session_3d_id         TEXT NOT NULL REFERENCES session_3d(id),
    geometry_id           TEXT NOT NULL REFERENCES geometry_results(id),
    material_id           TEXT NOT NULL,
    density_g_cm3         REAL NOT NULL,
    weight_g              REAL,
    production_cost       REAL,
    calculated_price      REAL,
    currency              TEXT,
    cost_model_version    TEXT,
    breakdown_json        TEXT NOT NULL DEFAULT '{}',
    fixed_price           REAL,
    fixed_price_version   TEXT,
    status                TEXT NOT NULL,           -- calculated | cost_model_not_configured | needs_review
    created_at            TEXT NOT NULL
);
"""



# ── Orders ───────────────────────────────────────────────────────────────────
def OrderLinesDdl(Name: str) -> str:
    """One ordered product. Everything is a snapshot taken at the moment of ordering: name, item ID, product,
    material, size, price — and purchase_json, the purchased configuration as it was understood then (the size
    label and, for a charm, what the size measured), so a historical order stays clear whatever changes later."""
    return f"""
CREATE TABLE IF NOT EXISTS {Name} (
    id                TEXT PRIMARY KEY,
    order_id          TEXT NOT NULL REFERENCES orders(id),
    position          INTEGER NOT NULL,
    design_id         TEXT NOT NULL REFERENCES designs(id),
    candidate_id      TEXT NOT NULL REFERENCES candidates(id),
    bag_line_id       TEXT,
    customization_id  TEXT,
    title             TEXT NOT NULL,               -- the design's unique name at order time
    ring_id           TEXT,                        -- the item ID: R-1013-A for a ring, C-1003-A for a charm
    product_type      TEXT NOT NULL DEFAULT 'ring' CHECK (product_type IN ('ring', 'charm')),
    material_id       TEXT NOT NULL,
    material_label    TEXT NOT NULL,
    ring_size         REAL,                        -- rings: the US size
    charm_size        REAL,                        -- charms: the size in mm
    quantity          INTEGER NOT NULL,
    unit_price        REAL NOT NULL,
    line_total        REAL NOT NULL,
    currency          TEXT NOT NULL,
    pricing_version   TEXT NOT NULL,
    image_path        TEXT,
    purchase_json     TEXT NOT NULL DEFAULT '{{}}',
    CHECK ((product_type = 'ring' AND ring_size IS NOT NULL) OR (product_type = 'charm' AND charm_size IS NOT NULL))
);
"""



# An order is a snapshot: every price, name, ring ID, address and promo value is copied in at the
# moment of ordering and never recomputed. The order number (ORD-10001 …) is assigned by a trigger,
# so every way of creating an order gets one — it is never random and never reused.
OrderTables = """
CREATE TABLE IF NOT EXISTS orders (
    id                  TEXT PRIMARY KEY,
    order_no            INTEGER UNIQUE,
    owner_account_id    TEXT NOT NULL,
    status              TEXT NOT NULL,            -- new | payment_confirmed | three_d_ready | production | qc | shipped | completed | cancelled
    payment_status      TEXT NOT NULL,            -- pending | paid | failed | refunded | cancelled
    payment_provider    TEXT,                     -- NULL until a provider is connected (adapter name)
    payment_ref         TEXT,                     -- the provider's reference (intent / charge id)
    payment_json        TEXT NOT NULL DEFAULT '{}',
    customer_json       TEXT NOT NULL,            -- first_name, last_name, email, phone
    shipping_json       TEXT NOT NULL,            -- recipient, line1, line2, city, region, postal_code, country (ISO) + validation
    address_validation  TEXT NOT NULL,            -- unverified | verified | corrected | failed
    shipping_method     TEXT NOT NULL,            -- standard | express
    currency            TEXT NOT NULL,
    subtotal            REAL NOT NULL,
    discount            REAL NOT NULL,
    shipping            REAL NOT NULL,
    total               REAL NOT NULL,
    promo_code          TEXT,
    promo_json          TEXT,                     -- snapshot: code, kind, value, original_amount, discount, final_amount
    terms_version       TEXT,
    terms_accepted_at   TEXT,
    client_request_id   TEXT,                     -- idempotency: one order per click
    notes               TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (owner_account_id, client_request_id)
);
CREATE INDEX IF NOT EXISTS orders_owner ON orders(owner_account_id, created_at);
CREATE INDEX IF NOT EXISTS orders_status ON orders(status, created_at);
CREATE TRIGGER IF NOT EXISTS orders_no_assign AFTER INSERT ON orders
WHEN NEW.order_no IS NULL BEGIN
  UPDATE orders SET order_no = (SELECT COALESCE(MAX(order_no), 10000) + 1 FROM orders) WHERE id = NEW.id;
END;

""" + OrderLinesDdl("order_lines") + """
CREATE INDEX IF NOT EXISTS order_lines_order ON order_lines(order_id, position);
CREATE INDEX IF NOT EXISTS order_lines_design ON order_lines(design_id);

CREATE TABLE IF NOT EXISTS order_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    TEXT NOT NULL REFERENCES orders(id),
    kind        TEXT NOT NULL,                     -- placed | status | payment | note | email
    data_json   TEXT NOT NULL DEFAULT '{}',
    by          TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS order_events_order ON order_events(order_id, id);

CREATE TABLE IF NOT EXISTS promo_codes (
    id              TEXT PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,          -- stored upper-case
    active          INTEGER NOT NULL DEFAULT 1,
    kind            TEXT NOT NULL CHECK (kind IN ('percent', 'fixed')),
    value           REAL NOT NULL,                 -- percent (0–100) or a fixed amount in the order currency
    starts_at       TEXT,
    ends_at         TEXT,
    usage_limit     INTEGER,
    usage_count     INTEGER NOT NULL DEFAULT 0,
    materials_json  TEXT,                          -- JSON list of material ids it applies to; NULL = every material
    min_subtotal    REAL,
    note            TEXT,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Gold (luxury) is not on the fixed-price path yet: the customer asks for a quote instead.
CREATE TABLE IF NOT EXISTS quote_requests (
    id                TEXT PRIMARY KEY,
    request_no        INTEGER UNIQUE,
    owner_account_id  TEXT NOT NULL,
    design_id         TEXT NOT NULL REFERENCES designs(id),
    candidate_id      TEXT NOT NULL REFERENCES candidates(id),
    title             TEXT NOT NULL,
    ring_id           TEXT,
    material_id       TEXT NOT NULL,
    material_label    TEXT NOT NULL,
    ring_size         REAL,
    quantity          INTEGER NOT NULL,
    customer_json     TEXT NOT NULL,
    message           TEXT,
    status            TEXT NOT NULL,               -- new | quoted | answered | approved | rejected | closed (p3/quotes.py)
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    offer_json        TEXT,                        -- the Admin's current quote (p3/quotes.py SendOffer)
    link_hash         TEXT,                        -- SHA-256 of the current quote's customer link
    decided_at        TEXT,                        -- the customer approved or declined
    decision_note     TEXT,
    order_id          TEXT                         -- the order an approved quote became
);
CREATE TRIGGER IF NOT EXISTS quote_requests_no_assign AFTER INSERT ON quote_requests
WHEN NEW.request_no IS NULL BEGIN
  UPDATE quote_requests SET request_no = (SELECT COALESCE(MAX(request_no), 5000) + 1 FROM quote_requests) WHERE id = NEW.id;
END;

-- A quote request's history: the request, its emails, notes, each quote sent, the customer's decision and note
CREATE TABLE IF NOT EXISTS quote_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    data_json   TEXT NOT NULL DEFAULT '{}',
    by          TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quote_events_request ON quote_events(request_id, id);
"""


def CustomizationsDdl(Name: str) -> str:
    """Customize choices (material / size / quantity) are per customer AND per design + option: on a
    shared gallery design every customer keeps their own choices."""
    return f"""
CREATE TABLE IF NOT EXISTS {Name} (
    id                TEXT PRIMARY KEY,
    owner_account_id  TEXT NOT NULL,
    design_id         TEXT NOT NULL REFERENCES designs(id),
    candidate_id      TEXT NOT NULL REFERENCES candidates(id),
    material_id       TEXT NOT NULL,
    ring_size         REAL,
    charm_size        REAL,                      -- charms only: the size in mm
    quantity          INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (owner_account_id, design_id, candidate_id)
);
"""

Schema = DesignsTable + """
CREATE TABLE IF NOT EXISTS batches (
    id                   TEXT PRIMARY KEY,
    design_id            TEXT NOT NULL REFERENCES designs(id),
    kind                 TEXT NOT NULL CHECK (kind IN ('initial', 'refine')),
    parent_candidate_id  TEXT REFERENCES candidates(id),
    user_text            TEXT NOT NULL,
    effective_prompt     TEXT NOT NULL,
    endpoint             TEXT NOT NULL,
    reference_asset      TEXT,
    reference_upload_url TEXT,
    desired_count        INTEGER NOT NULL,
    config_version       TEXT NOT NULL,
    client_request_id    TEXT,
    created_at           TEXT NOT NULL,
    UNIQUE (design_id, client_request_id)
);

CREATE TABLE IF NOT EXISTS candidates (
    id                   TEXT PRIMARY KEY,
    batch_id             TEXT NOT NULL REFERENCES batches(id),
    slot                 INTEGER NOT NULL,
    status               TEXT NOT NULL CHECK (status IN ('pending', 'generating', 'ready', 'failed')),
    seed                 INTEGER NOT NULL,
    attempts             INTEGER NOT NULL DEFAULT 0,
    duplicate_retries    INTEGER NOT NULL DEFAULT 0,
    provider_request_id  TEXT,
    asset_path           TEXT,
    content_sha256       TEXT,
    error                TEXT,
    error_code           TEXT,
    movie_id             TEXT,                     -- the movie the Admin chose to show for this image (else its newest)
    credit_ref           TEXT,                     -- the credit action this slot belongs to: its batch, or a retry action (p3/credits.py)
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    UNIQUE (batch_id, slot)
);

""" + CustomizationsDdl("customizations") + """

CREATE TABLE IF NOT EXISTS movies (
    id                   TEXT PRIMARY KEY,
    candidate_id         TEXT NOT NULL REFERENCES candidates(id),
    config_version       TEXT NOT NULL,
    endpoint             TEXT NOT NULL,
    status               TEXT NOT NULL CHECK (status IN ('queued', 'running', 'ready', 'failed', 'interrupted')),
    provider_request_id  TEXT,
    asset_path           TEXT,
    error                TEXT,
    error_code           TEXT,
    made_by_admin        TEXT,                    -- the Admin asked for this movie ("Make a new movie"): no allowance charge
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
-- At most one live (queued/running/ready) customer movie per candidate + config: dedupes repeated Proceed clicks.
CREATE UNIQUE INDEX IF NOT EXISTS movies_one_live
    ON movies(candidate_id, config_version) WHERE status IN ('queued', 'running', 'ready') AND made_by_admin IS NULL;

CREATE TABLE IF NOT EXISTS meshes (
    id                   TEXT PRIMARY KEY,
    candidate_id         TEXT NOT NULL REFERENCES candidates(id),
    endpoint             TEXT NOT NULL,
    settings_json        TEXT NOT NULL,
    config_version       TEXT NOT NULL,
    status               TEXT NOT NULL CHECK (status IN ('queued', 'running', 'ready', 'failed', 'interrupted')),
    provider_request_id  TEXT,
    original_path        TEXT,
    original_format      TEXT,
    stl_path             TEXT,
    provenance_json      TEXT NOT NULL,
    error                TEXT,
    error_code           TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

""" + BagLinesTable + SessionTables + OrderTables + """
CREATE INDEX IF NOT EXISTS designs_owner ON designs(owner_account_id, updated_at);
CREATE INDEX IF NOT EXISTS bag_lines_owner ON bag_lines(owner_account_id, created_at);
"""


ProductTriggers = """
CREATE TRIGGER IF NOT EXISTS designs_product_valid BEFORE INSERT ON designs
WHEN NEW.product_type IS NULL OR NEW.product_type NOT IN ('ring', 'charm') BEGIN
  SELECT RAISE(ABORT, 'product_type must be ring or charm');
END;
CREATE TRIGGER IF NOT EXISTS designs_product_immutable BEFORE UPDATE OF product_type ON designs
WHEN NEW.product_type IS NOT OLD.product_type BEGIN
  SELECT RAISE(ABORT, 'product_type is set when a design is created and never changes');
END;
"""


def _Columns(Conn, Table: str) -> dict:
    return {R[1]: R for R in Conn.execute(f"PRAGMA table_info({Table})")}


def _Rebuild(Conn, Table: str, Ddl, Indexes: list[str]) -> None:
    """Rebuild a table into a new shape, keeping every row, id and value (SQLite cannot drop NOT NULL in place).
    Atomic: a failure (e.g. a row the new CHECK refuses) rolls back and leaves the old table untouched."""
    Old = list(_Columns(Conn, Table))
    Fk = Conn.execute("PRAGMA foreign_keys").fetchone()[0]
    Conn.execute("PRAGMA foreign_keys=OFF")      # other rows keep pointing at the same ids; enforcement off while swapping
    Conn.execute("BEGIN")
    try:
        Conn.execute(f"DROP TABLE IF EXISTS {Table}_new")
        Conn.execute(Ddl(f"{Table}_new"))
        Keep = [C for C in Old if C in _Columns(Conn, f"{Table}_new")]
        Cols = ", ".join(Keep)
        Conn.execute(f"INSERT INTO {Table}_new ({Cols}) SELECT {Cols} FROM {Table}")
        Before = Conn.execute(f"SELECT COUNT(*) FROM {Table}").fetchone()[0]
        After = Conn.execute(f"SELECT COUNT(*) FROM {Table}_new").fetchone()[0]
        if Before != After:
            raise RuntimeError(f"{Table}: {Before} rows before the rebuild, {After} after")
        Conn.execute(f"DROP TABLE {Table}")
        Conn.execute(f"ALTER TABLE {Table}_new RENAME TO {Table}")
        for Sql in Indexes:
            Conn.execute(Sql)
        Conn.execute("COMMIT")
    except Exception:
        Conn.execute("ROLLBACK")
        raise
    finally:
        Conn.execute(f"PRAGMA foreign_keys={'ON' if Fk else 'OFF'}")


def _InstallProducts(Conn) -> None:
    """Rings and charms (p3/products.py). Everything that already exists is a ring: the new columns default to
    'ring', and the bag / order tables are rebuilt only to let a charm line carry a charm size instead of a ring
    size (their rows, ids and values are copied unchanged)."""
    if "product_type" not in _Columns(Conn, "designs"):
        Conn.execute("ALTER TABLE designs ADD COLUMN product_type TEXT NOT NULL DEFAULT 'ring'")
    Conn.executescript(ProductTriggers)
    if "charm_size" not in _Columns(Conn, "customizations"):
        Conn.execute("ALTER TABLE customizations ADD COLUMN charm_size REAL")
    Q = _Columns(Conn, "quote_requests")
    if "product_type" not in Q:
        Conn.execute("ALTER TABLE quote_requests ADD COLUMN product_type TEXT NOT NULL DEFAULT 'ring'")
    if "charm_size" not in Q:
        Conn.execute("ALTER TABLE quote_requests ADD COLUMN charm_size REAL")
    B = _Columns(Conn, "bag_lines")
    if "product_type" not in B or B["ring_size"][3]:              # [3] = notnull
        _Rebuild(Conn, "bag_lines", BagLinesDdl,
                 ["CREATE INDEX IF NOT EXISTS bag_lines_owner ON bag_lines(owner_account_id, created_at)"])
    O = _Columns(Conn, "order_lines")
    if "product_type" not in O or O["ring_size"][3]:
        _Rebuild(Conn, "order_lines", OrderLinesDdl,
                 ["CREATE INDEX IF NOT EXISTS order_lines_order ON order_lines(order_id, position)",
                  "CREATE INDEX IF NOT EXISTS order_lines_design ON order_lines(design_id)"])


def Now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def NewId(Prefix: str) -> str:
    return f"{Prefix}_{uuid.uuid4().hex}"


class Database:
    """Thin sqlite3 wrapper. One connection per call; writes serialized by a lock."""

    def __init__(self, DbPath: Path, SchemaSql: str | None = None):
        self.DbPath = Path(DbPath)
        self.DbPath.parent.mkdir(parents=True, exist_ok=True)
        self._WriteLock = threading.RLock()
        with self.Connect() as Conn:
            Conn.execute("PRAGMA journal_mode=WAL")
            Conn.executescript(Schema if SchemaSql is None else SchemaSql)
            if SchemaSql is None:
                Cols = {R[1] for R in Conn.execute("PRAGMA table_info(designs)")}
                if "owner_account_id" in Cols and "ai_mode" not in Cols:
                    # AI mode the session was created in ('mock' | 'fal'); mock sessions stay out of the Admin.
                    Conn.execute("ALTER TABLE designs ADD COLUMN ai_mode TEXT")
                if "owner_account_id" in Cols and "source_design_id" not in Cols:
                    # Designs started from the Inspiration Gallery: a copy of an XJet design's batch.
                    Conn.execute("ALTER TABLE designs ADD COLUMN source_design_id TEXT")
                    Conn.execute("ALTER TABLE designs ADD COLUMN source_candidate_id TEXT")
                CCols = {R[1] for R in Conn.execute("PRAGMA table_info(customizations)")}
                if CCols and "owner_account_id" not in CCols:
                    # Customize choices became per customer (shared gallery designs): rebuild with the owner.
                    # Older databases may hold duplicate rows per design + option (no UNIQUE then): the
                    # latest one wins. Atomic, so a failure leaves the old table untouched.
                    Conn.execute("DROP TABLE IF EXISTS customizations_new")
                    # bag_lines reference customizations(id); the ids are kept, so the references stay valid —
                    # but enforcement must be off while the old table is dropped (a PRAGMA outside the transaction).
                    Fk = Conn.execute("PRAGMA foreign_keys").fetchone()[0]
                    Conn.execute("PRAGMA foreign_keys=OFF")
                    Conn.execute("BEGIN")
                    try:
                        Conn.execute(CustomizationsDdl("customizations_new"))
                        Conn.execute("INSERT OR REPLACE INTO customizations_new (id, owner_account_id, design_id, candidate_id, "
                                     "material_id, ring_size, quantity, created_at, updated_at) SELECT c.id, d.owner_account_id, "
                                     "c.design_id, c.candidate_id, c.material_id, c.ring_size, c.quantity, c.created_at, c.updated_at "
                                     "FROM customizations c JOIN designs d ON d.id = c.design_id ORDER BY c.updated_at, c.rowid")
                        Conn.execute("DROP TABLE customizations")
                        Conn.execute("ALTER TABLE customizations_new RENAME TO customizations")
                        Conn.execute("COMMIT")
                    except Exception:
                        Conn.execute("ROLLBACK")
                        raise
                    finally:
                        Conn.execute(f"PRAGMA foreign_keys={'ON' if Fk else 'OFF'}")
                MCols = {R[1] for R in Conn.execute("PRAGMA table_info(movies)")}
                if MCols and "requested_by" not in MCols:
                    Conn.execute("ALTER TABLE movies ADD COLUMN requested_by TEXT")   # who pays for a movie on a shared design
                CandCols = {R[1] for R in Conn.execute("PRAGMA table_info(candidates)")}
                if CandCols and "movie_id" not in CandCols:
                    Conn.execute("ALTER TABLE candidates ADD COLUMN movie_id TEXT")   # the movie the Admin chose to show for an image
                if CandCols and "credit_ref" not in CandCols:
                    # Credits per action (2026-10-07): every slot belongs to its batch's request unless retried later
                    Conn.execute("ALTER TABLE candidates ADD COLUMN credit_ref TEXT")
                    Conn.execute("UPDATE candidates SET credit_ref = batch_id WHERE credit_ref IS NULL")
                if MCols and "made_by_admin" not in MCols:
                    # A movie the Admin asked for ("Make a new movie"): outside the one-live-movie rule, no allowance charge
                    Conn.execute("ALTER TABLE movies ADD COLUMN made_by_admin TEXT")
                    Conn.execute("DROP INDEX IF EXISTS movies_one_live")
                    Conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS movies_one_live ON movies(candidate_id, config_version) "
                                 "WHERE status IN ('queued', 'running', 'ready') AND made_by_admin IS NULL")
                S3Cols = {R[1] for R in Conn.execute("PRAGMA table_info(session_3d)")}
                if S3Cols and "accepted_at" not in S3Cols:            # the Admin accepted a flagged 3D result for production
                    for Col in ("accepted_at TEXT", "accepted_by TEXT", "accepted_note TEXT"):
                        Conn.execute(f"ALTER TABLE session_3d ADD COLUMN {Col}")
                if "owner_account_id" in Cols and "removed_at" not in Cols:
                    Conn.execute("ALTER TABLE designs ADD COLUMN removed_at TEXT")   # removed from My Designs (journey kept)
                UCols = {R[1] for R in Conn.execute("PRAGMA table_info(gallery_uses)")}
                if UCols and "removed_at" not in UCols:
                    Conn.execute("ALTER TABLE gallery_uses ADD COLUMN removed_at TEXT")
                if "owner_account_id" in Cols and "share_slug" not in Cols:
                    # Customer share link by design name (/design/aurora-twist): assigned once, stable through renames
                    Conn.execute("ALTER TABLE designs ADD COLUMN share_slug TEXT")
                QCols = {R[1] for R in Conn.execute("PRAGMA table_info(quote_requests)")}
                if QCols and "offer_json" not in QCols:
                    # The Admin's quote and the customer's decision (p3/quotes.py)
                    for Col in ("offer_json TEXT", "link_hash TEXT", "decided_at TEXT", "decision_note TEXT", "order_id TEXT"):
                        Conn.execute(f"ALTER TABLE quote_requests ADD COLUMN {Col}")
                    # Requests made before the history existed start it with their request (their emails were not recorded)
                    Size = "COALESCE(charm_size, ring_size)" if "charm_size" in QCols else "ring_size"
                    Conn.execute("INSERT INTO quote_events (request_id, kind, data_json, by, created_at) "
                                 "SELECT id, 'created', json_object('message', substr(COALESCE(message, ''), 1, 1000), "
                                 f"'quantity', quantity, 'material_id', material_id, 'size', {Size}, 'earlier', 1), "
                                 "'customer', created_at FROM quote_requests "
                                 "WHERE id NOT IN (SELECT request_id FROM quote_events)")
                if QCols:
                    Conn.execute("CREATE INDEX IF NOT EXISTS quote_requests_token ON quote_requests(link_hash)")
                GCols = {R[1] for R in Conn.execute("PRAGMA table_info(gallery_items)")}
                if GCols and "owner_kind" not in GCols:
                    # Publication provenance: whose design a gallery item shows and the customer's consent (privacy)
                    for Col in ("owner_kind TEXT NOT NULL DEFAULT 'xjet'", "consent_note TEXT", "consent_at TEXT", "consent_by TEXT"):
                        Conn.execute(f"ALTER TABLE gallery_items ADD COLUMN {Col}")
                if "owner_account_id" in Cols:
                    _InstallProducts(Conn)                 # product type on designs (rings by default), charm sizes
                    from p3 import ringids
                    ringids.Install(Conn)                 # item IDs: R-1042 … for rings, C-1001 … for charms
            if SchemaSql is None and Conn.execute("PRAGMA user_version").fetchone()[0] < SchemaVersion:
                Conn.execute(f"PRAGMA user_version = {SchemaVersion}")

    @contextmanager
    def Connect(self):
        Conn = sqlite3.connect(self.DbPath, timeout=30, isolation_level=None)
        Conn.row_factory = sqlite3.Row
        Conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield Conn
        finally:
            Conn.close()

    @contextmanager
    def Transaction(self):
        with self._WriteLock, self.Connect() as Conn:
            Conn.execute("BEGIN IMMEDIATE")
            try:
                yield Conn
                Conn.execute("COMMIT")
            except BaseException:
                Conn.execute("ROLLBACK")
                raise

    def One(self, Sql: str, Params=()) -> dict | None:
        with self.Connect() as Conn:
            Row = Conn.execute(Sql, Params).fetchone()
            return dict(Row) if Row else None

    def All(self, Sql: str, Params=()) -> list[dict]:
        with self.Connect() as Conn:
            return [dict(R) for R in Conn.execute(Sql, Params).fetchall()]

    def Execute(self, Sql: str, Params=()) -> int:
        with self.Transaction() as Conn:
            return Conn.execute(Sql, Params).rowcount

    def Update(self, Table: str, RowId: str, **Fields) -> None:
        Fields["updated_at"] = Now()
        Cols = ", ".join(f"{K} = ?" for K in Fields)
        self.Execute(f"UPDATE {Table} SET {Cols} WHERE id = ?", (*Fields.values(), RowId))


def Dumps(Obj) -> str:
    return json.dumps(Obj, sort_keys=True)
