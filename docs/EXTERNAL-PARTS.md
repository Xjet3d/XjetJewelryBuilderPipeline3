# External parts and 3D versions — design (2026-10-12)

How an STL made outside Atelier (Blender, Rhino, CAD, …) enters Pipeline 3: as a **new 3D version of an existing design**,
or as a **new design** inside a session — and how orders keep the exact geometry they were produced from. It extends
the existing 3D pipeline (one master per design, measure once, sizes by arithmetic, temporary exports, signed
downloads); there is no second pipeline.

Status: built on proto (main) on 2026-10-12; **not** on Atelier until approved.

---

## 1. What exists today (before this change)

| Concept | Where | Today |
|---|---|---|
| Session | `p3/sessions.py` | **One design journey** = one design (`designs.id`), or a customer on a shared gallery design (`gallery_uses`, `use_…`). Not a container. |
| Design | `designs` | Name, prompt, product (ring / charm), Ring / Charm number (`R-1013`, `C-1003`). |
| Options | `batches` → `candidates` | Four AI images per request (refinements add more); an option's item ID is `R-1013-A` …; orders reference the option (`candidate_id`). |
| 3D model | `meshes` (+ `raw_geometry`) | A Hi3D model of one option: the master STL (up to ~250 MB, 5M faces), **measured once**. Several rows may exist; the code always takes the **newest ready one** — "active" is implicit. |
| 3D result | `session_3d` (+ `geometry_results`, `price_calculations`) | One model at one size and material: numbers by arithmetic from the one measurement. |
| Sized STL | `geometry_jobs` (export) | Exported on demand to a temporary file (deleted after 1 h), downloaded via a 10-minute signed link, named `<Design>_<Item ID>_<ORD-…>_<Material>_<size>.stl`. |
| Order line → 3D | `p3/orders.py` `_ThreeDForLine` | **Derived on every read**: the newest result of the design with the same size and material. Nothing is stored on the line. |

Consequences: no explicit active version, no history UI, no way to bring an outside STL in, and an order line's
geometry could silently change if a newer result for the same size and material appeared.

---

## 2. The model (proposed and built)

```
Session (S-1001)                      sessions                     — new: an explicit container
 ├── Design A  "Éclat Heart" C-1029   designs.session_id           — 0..n designs per session
 │    ├── option C-1029-A             candidates                   — unchanged
 │    ├── v1  Hi3D            (superseded)   meshes.version_no = 1, source = hi3d
 │    └── v2  External · Rhino  ◀ ACTIVE    meshes.version_no = 2, source = external, parent = v1
 │          └── results (size × material)   session_3d → geometry_results, price_calculations
 └── Design B  (external)  C-1031
      └── v1  External · CAD  ◀ ACTIVE

Order ORD-10003
 └── line 1 → pinned 3D result + pinned 3D version   order_lines.three_d_id, order_lines.mesh_id
```

* **A 3D version is a `meshes` row** — Hi3D or external — numbered per design (`version_no` 1, 2, 3 …), with its
  provenance. The file and its single measurement (`raw_geometry`) are exactly as before.
* **The design points at its active version** (`designs.active_mesh_id`). Other ready versions are *superseded*;
  an Admin may *archive* one (hidden from the list; the file is kept — nothing is ever deleted by this feature).
* **An order line is pinned** to the 3D result it was prepared from (`order_lines.three_d_id`) and so to the exact
  version (`order_lines.mesh_id`). Changing the design's active version later never changes a pinned line.
* **A session is an explicit container** (`sessions`, numbered S-1001 …) holding zero, one or many designs. Designs
  made before this change have no `session_id` and stay what they were: a session of one design. The first time a
  design is added next to one of them, that implicit session becomes an explicit one.

### 2.1 Zero, one or many designs in a session
* An explicit session may be created **empty** (Admin → Sessions → *New session*), owned by an existing account (a
  customer, or XJet's own team account).
* Designs join a session by `designs.session_id`. A legacy design (no session) is shown as a session of one; adding
  a design next to it creates the explicit session (owner = the design's owner) and puts both designs in it.
* Gallery journeys (`use_…`, a customer on XJet's shared design) are not containers: a part is added to the
  customer's own session instead.

---

## 3. Flows

**Upload (all flows).** The browser sends the STL in 8 MB pieces (`POST /api/admin/uploads`, `PUT …?offset=`,
`POST …/complete`): each piece is written straight to disk (never held whole in memory; within the 20 MB request
limit of proto's and Atelier's nginx). On completion the file is checked (binary or ASCII STL), hashed, and kept
until it is used (unused uploads are deleted after 24 h). Using it **moves** the file into place — no copy.

**Measuring (all flows).** Exactly the existing path: the version's `raw_geometry` row → one *measure* job in the
memory-capped geometry worker (an ASCII STL is converted to binary there) → the light preview → the integrity check.
An external design also gets a picture: a shaded render of the light preview (`render` job, numpy + PIL, no AI call),
used as its option's image.

| Case | Action | Result |
|---|---|---|
| **A** Empty session | Session page → *Add External Part* → *Add as a new Design* | New external design (one option `…-A`, v1 external, active) in the session. |
| **B** Session with designs | Same, *Add as a new Design* | Another independent design; existing designs untouched. |
| **C** Revision | Design's 3D panel → *Upload new version*; or *Add External Part* → *Update an existing Design* | v(n+1) external on the same design and option, `parent` = the version it revises; *activate* (default on). Previous versions kept. |
| **D** No session | Sessions → *New session* with an STL | New session (owner chosen) + new external design + v1. |
| Order line | Order → line → *Upload revised STL* | Case C for the line's design and option, then the line is **pinned** to it. By default it is this order's own revision (the design keeps its active version); *Also make it the design's active version* makes it the design's. |

---

## 4. UI

* **Sessions list** — *New session* (owner, title, optional STL → Case D). Explicit sessions show their number and
  design count; an empty session shows *Add External Part*.
* **Session page** — header shows the session (S-1001, its other designs with links). *Add External Part* opens one
  dialog: file, product (ring / charm), name, source tool, scaling (*Use exactly as uploaded* default / *Use as
  scalable master*), and **Add as a new Design** or **Update an existing Design** (with a design picker; not offered
  when the session has no design).
* **3D panel (per design)** — *3D versions*: v1, v2 … with source, tool, file name, who / when, measured X / Y / Z,
  volume, inner diameter (rings), scaling mode, state (active / superseded / archived); actions *Make active*,
  *Archive*, *Upload new version*.
* **Order page (per line)** — the pinned version (e.g. "v2 · External · Rhino"), *Upload revised STL*, and
  *Use another version…* (confirmation; logged; warns when the order is already in production).

---

## 5. Exact vs scalable master

| | Exact (default for external) | Scalable master (default for Hi3D) |
|---|---|---|
| Meaning | The STL is the production geometry, in **mm**. | The STL is a shape; Atelier sizes it. |
| Sizes | One size: the file's own. A result for any size and material uses scale 1. | Any size by arithmetic (ring: inner diameter; charm: total height incl. loop). |
| Checks | Ring: the measured inner diameter vs the ordered US size (±0.15 mm) → *review* if different. Charm: the total height vs the ordered size (±0.5 mm) → *review*. The Admin can accept. | As today. |
| Download | The master file itself, under the usual production name — no export job. | Temporary scaled export (as today). |
| Units | STL has no units; external files are read as mm. The panel shows the measured X / Y / Z and warns when they look wrong for jewelry (largest side < 3 mm or > 200 mm). | Hi3D's arbitrary units, scaled. |

---

## 6. Switching and rollback

*Make active* sets `designs.active_mesh_id` (logged: from, to, who, when). New 3D results (session page, order
preparation) use the active version; existing results and pinned order lines keep theirs. Rolling back = making an
older version active (its measurement is kept, so nothing is paid or recomputed beyond arithmetic). A newly ready Hi3D
model becomes active, unless the active version is external (a deliberate choice is never displaced automatically).

---

## 7. Orders reference exact geometry

* **Design → active version** (changes over time). **Order line → pinned 3D result → pinned version** (never changes
  by itself).
* *Prepare STL* on a line pins it to the result it creates (event `3d_prepared` with the version).
* *Use another version…* re-pins explicitly (event `3d_version_changed`, from / to / who; confirmation; a warning when
  the order is in production, QC, shipped or completed).
* Existing lines were backfilled with what the page showed on 2026-10-12 (the derived result), so nothing changes
  for them — only a result of the line's own option: a line is never pinned to another option's model (it stays
  unpinned and is pinned when it is prepared). A line without a result is pinned when it is prepared.
* A line made on the session page (not prepared from the order) follows its design until the order is released: moving
  it to *3D ready* or any later status pins every such line (event `3d_pinned`, "released").

---

## 8. Download and resizing keep working

Scalable versions: unchanged (measure once → arithmetic → temporary export → signed link). Exact versions: the result
reports a finished "export" immediately and the signed link serves the master file under the production name. File
names still come from the design's name (Admin *Rename* updates future names), item ID, order number, material, size.

---

## 9. Schema changes (additive, idempotent, in `p3/db.py`)

| Table | Change |
|---|---|
| `sessions` (new) | `id` (`ses_…`), `session_no` (S-1001 …, trigger), `owner_account_id`, `title`, `origin` (`admin`), `created_by`, `created_at`, `updated_at` |
| `uploads` (new) | `id`, `filename`, `bytes`, `received`, `path`, `status` (receiving / complete / used / expired / cancelled), `sha256`, `format` (binary / ascii), `faces`, `used_by`, `created_by`, `created_at`, `updated_at` |
| `schema_marks` (new) | one-time data steps done on this database (`versions-backfill`) |
| `designs` | `session_id`, `origin` (`ai` / `external`), `active_mesh_id` |
| `meshes` | `version_no`, `source` (`hi3d` / `external`), `scaling` (`scalable` / `exact`), `parent_mesh_id`, `source_tool`, `original_filename`, `note`, `created_by`, `archived_at`, `archived_by` |
| `order_lines` | `three_d_id`, `mesh_id` (also in the table's DDL, so a later rebuild keeps them) |

Backfill (once per database, `schema_marks`): Hi3D versions numbered per design by creation; every design's
`active_mesh_id` = its newest ready model (today's implicit choice); every order line's pin = today's derived result of
its own option. Old code ignores the new columns, so a rollback of the code keeps working on the new database.

## 10. Storage

One master per version; nothing per size; temporary exports and downloads expire as before. Uploads stream to disk in
8 MB pieces and are *moved* into place. An ASCII STL is converted to binary at measurement and only the binary is kept
(about 5× smaller; the uploaded file's SHA-256 and size stay in the version's provenance). Superseded and archived
versions keep their files (history and no new paid Hi3D call) — the price of history is one file per version. No
compression subsystem (not needed at today's ~5 GB on proto). An external design's picture is a render of its active
version (`render` job: the light preview, numpy + PIL in the worker, ~1 s; no AI call), redone when another version
becomes active.

## 11. Fulfillment

`Customer → Session → Design → 3D version → Order → Order line (pinned result + version) → Manufacturing part → Build
→ QC → Shipment`. A future *Manufacturing part* references the order line, its pinned version (`mesh_id`, the master's
SHA-256) and the exported file (size, material, its own hash) — the same for AI, Hi3D and external geometry.

## 12. Decisions taken for proto (change any before Atelier)

1. External designs are **Admin-only** for now: hidden from the customer's *My Designs* and not publishable to the
   Inspiration Gallery, until we decide how a customer sees them.
2. A new session's owner is an existing account (a customer, or XJet's team account) — no anonymous owner.
3. A new Hi3D model becomes active unless an external version is active.
4. Re-pinning an order line is allowed in any status, with confirmation and a log entry (warning from *production*).
5. External STL files are read as mm; exact-mode tolerances ring ±0.15 mm (inner diameter), charm ±0.5 mm (height).
6. Uploads up to 600 MB (`P3_MAX_UPLOAD_MB`); unused uploads deleted after 24 h (a cancelled dialog deletes its upload
   at once).
7. Nothing is ever deleted by this feature (archive hides; files stay) — except an ASCII upload's text, kept as the
   equivalent binary STL.
8. An STL uploaded on an order line is that order's revision by default; the design's active version changes only when
   asked (the checkbox, or *Make active* on the session page).
9. A part used exactly as uploaded makes its results at its own size when nobody chose one (a ring: the US size nearest
   its inner diameter; a charm: its total height).

## 13. API (Admin, as built)

| Call | What it does |
|---|---|
| `POST /api/admin/uploads` `{filename, bytes}` | Starts an upload (an `.stl`, ≤ `P3_MAX_UPLOAD_MB`); returns its id and the piece size (8 MB). |
| `PUT /api/admin/uploads/{id}?offset=N` | The next piece, raw; `409 offset_mismatch` says where to continue. |
| `POST /api/admin/uploads/{id}/complete` | Checks it is an STL (binary or ASCII) and hashes it. |
| `DELETE /api/admin/uploads/{id}` | Cancels it; the partial file is deleted. |
| `GET` / `POST /api/admin/designs/{id}/versions` | The design's versions / a new version from an upload (`scaling`, `source_tool`, `note`, `activate`, `candidate_id`, `parent_mesh_id`). |
| `POST /api/admin/versions/{mesh}/activate` · `archive` · `restore` | Switch, roll back, hide, bring back. |
| `POST /api/admin/versions/{mesh}/download-link` | A 10-minute signed link to the version's own file (`<Design>_<Option ID>_v<n>.stl`). |
| `GET /api/admin/versions/{mesh}/preview` | Its light preview, for the 3D view before any sized result. |
| `POST /api/admin/sessions` `{owner_account_id, title}` | An empty session (S-1001 …). |
| `GET /api/admin/sessions/{ses_…}` | The session and its designs. |
| `POST /api/admin/sessions/{ses_… or design id}/external-designs` | A new external design from an upload (`product_type`, `title`, `scaling`, `source_tool`, `note`, `force`). |
| `POST /api/admin/orders/{order}/lines/{line}/3d-version` `{mesh_id, note}` | Produce the line from another version of its option (a note is required from *In production* on). |
| `POST /api/admin/orders/{order}/lines/{line}/3d-upload` | A revised STL for the line: a new version, then the line uses it (`activate` = also the design's). |
