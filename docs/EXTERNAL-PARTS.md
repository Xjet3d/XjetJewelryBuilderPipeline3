# External parts and 3D versions — design (2026-10-12)

How an STL made outside Atelier (Blender, Rhino, CAD, …) enters Pipeline 3: as a **new 3D version of an existing
part/design**, or as a **new design** of an existing session — and how orders keep the exact geometry they were
produced from. It extends the existing 3D pipeline (one master per version, measure once, sizes by arithmetic,
temporary exports, signed downloads); there is no second pipeline.

Status: approved by the user on 2026-10-12 with the final decisions of §12; built on proto (main), then promoted to
Atelier.

---

## 1. What existed before this change

| Concept | Where | Before |
|---|---|---|
| Session | `p3/sessions.py` | **One design journey** = one design (`designs.id`), or a customer on a shared gallery design (`gallery_uses`, `use_…`). Not a container. |
| Design | `designs` | Name, prompt, product (ring / charm), Ring / Charm number (`R-1013`, `C-1003`). |
| Options | `batches` → `candidates` | Four AI images per request (refinements add more); an option's item ID is `R-1013-A` …; orders reference the option (`candidate_id`). |
| 3D model | `meshes` (+ `raw_geometry`) | A Hi3D model of one option: the master STL (up to ~250 MB, 5M faces), **measured once**. Several rows may exist; the code always took the **newest ready one** — "active" was implicit. |
| 3D result | `session_3d` (+ `geometry_results`, `price_calculations`) | One model at one size and material: numbers by arithmetic from the one measurement. |
| Sized STL | `geometry_jobs` (export) | Exported on demand to a temporary file (deleted after 1 h), downloaded via a 10-minute signed link, named `<Design>_<Item ID>_<ORD-…>_<Material>_<size>.stl`. |
| Order line → 3D | `p3/orders.py` `_ThreeDForLine` | **Derived on every read**: the newest result of the design with the same size and material. Nothing was stored on the line. |

Consequences: no explicit version in use, no history, no way to bring an outside STL in, and an order line's geometry
could silently change if a newer result for the same size and material appeared.

---

## 2. The model

```
Session (S-1001, started on the website)  sessions (when it holds more than one design)
 ├── Design A  "Éclat Heart" C-1029       designs.session_id
 │    ├── option C-1029-A                 candidates                   — unchanged
 │    ├── v1  Hi3D            (earlier)          meshes.version_no = 1, source = hi3d
 │    └── v2  External · Rhino  ◀ IN USE         meshes.version_no = 2, source = external, parent = v1
 │          └── results (size × material)        session_3d → geometry_results, price_calculations
 └── Design B  (external part)  C-1031
      └── v1  External · CAD  ◀ IN USE

Order ORD-10003
 └── line 1 → pinned 3D version (when ordered) → pinned 3D result (when prepared)   order_lines.mesh_id, three_d_id
```

* **A 3D version is a `meshes` row** — Hi3D or external — numbered per design (`version_no` 1, 2, 3 …), with its
  provenance (source, tool, file name, who, when, the version it revises). The file and its single measurement
  (`raw_geometry`) are exactly as before. An attached STL is a **permanent asset of its part**: nothing in this feature
  deletes a version or its file.
* **LIFO.** The newest usable version is the one in use by default — each new version (uploaded or Hi3D) becomes the
  design's version in use (`designs.active_mesh_id`). The Admin can choose any other existing version at any time
  (*Use this version*); it stays in use until another is chosen or a newer one is added. Every version is kept:
  *earlier* ones, or *archived* (hidden from the choices; the file stays). A version whose file cannot be measured is
  never in use (the newest usable one takes its place).
* **An order line is pinned** to the version in use when it is ordered (`order_lines.mesh_id`, the normal order flow),
  then to the 3D result it is produced from (`order_lines.three_d_id`). A later version never changes it.
* **Sessions start on the website** (a customer's first prompt); the Admin never makes one. A session may have no
  finished part yet. An external part is attached to an existing session: as a version of one of its designs, or as a
  new design of it — the session then becomes an explicit one (`sessions`, S-1001 …) holding several designs.
* **An external design is an ordinary part of its session**: its owner sees it in My Designs and can order it like any
  design (its picture is a render of the part); the STL, like every 3D file, is for the Admin only. It is not offered
  in the Inspiration Gallery.

---

## 3. Flows

**Upload (all flows).** The browser sends the STL (up to **300 MB**) in 8 MB pieces (`POST /api/admin/uploads`,
`PUT …?offset=`, `POST …/complete`): each piece is written straight to disk (never held whole in memory; within the
20 MB request limit of proto's and Atelier's nginx). On completion the file is checked (binary or ASCII STL) and
hashed. Attaching it **moves** the file into place — no copy — and from then on it is the part's file. Only an upload
that was never attached (abandoned, or its dialog cancelled) is removed (after 24 h, or at once on cancel).

**Measuring (all flows).** Exactly the existing path: the version's `raw_geometry` row → one *measure* job in the
memory-capped geometry worker (an ASCII STL is converted to binary there — the same triangles) → the light preview →
the integrity check. An external design also gets a picture: a shaded render of the light preview (`render` job,
numpy + PIL, no AI call), used as its option's image.

| Case | Action | Result |
|---|---|---|
| **A** Session with no finished part | Session page → *Add External Part* → *Add as a new Design* | A new external design (one option `…-A`, v1, in use) in that session. |
| **B** Session with designs | Same, *Add as a new Design* | Another independent design; existing designs untouched. |
| **C** Revision | Session page → *Add External Part* → *Update an existing Design* | v(n+1) on the same design and option, `parent` = the version it revises; it is the version in use (LIFO). Earlier versions kept. |
| Order line | Order → line → *Upload revised STL* | Case C for the line's design and option, then the line is pinned to it (it is also the design's version in use). |

(The requirements' Case D — a new session made in the Admin — is not offered: sessions start on the website.)

---

## 4. UI

* **Sessions list** — sessions holding several designs (S-1001 …) are listed above the journeys; a design's row
  carries its session's number and an *External part* chip.
* **Session page** — header shows the session (S-1001, its other designs). *Add External Part* (in the 3D section, and
  in the header of a session with no finished part) opens one dialog: file (up to 300 MB), **Add as a new Design**
  (name, product) or **Update an existing Design** (design picker; option), made with, scaling (*Use exactly as
  uploaded* default / *Use as scalable master*), note. The newest version becomes the one in use.
* **3D versions panel (per design)** — v1, v2 … with source, tool, file name, who / when, the version it revises,
  measured X / Y / Z, volume, inner diameter (≈ US size) or charm height, scaling, state (*In use*, *Earlier*,
  *Archived*, *Not usable*), *Newest*, and the results and orders using it; actions *View 3D*, *Download file*,
  *Use this version*, *Archive*, *Restore*.
* **Order page (per line)** — the pinned version (e.g. "3D version: v2 · External · Rhino · pinned"), *Use another
  version…* (logged; a reason is required from *In production* on) and *Upload revised STL…*.

---

## 5. Exact vs scalable master

| | Exact (default for external) | Scalable master (default for Hi3D) |
|---|---|---|
| Meaning | The STL is the production geometry, in **mm**. | The STL is a shape; Atelier sizes it. |
| Sizes | One size: the file's own (the default size of its results). A result for any size and material uses scale 1. | Any size by arithmetic (ring: inner diameter; charm: total height incl. loop). |
| Checks | Ring: the measured inner diameter vs the ordered US size (±0.15 mm). Charm: the total height vs the ordered size (±0.5 mm). Outside → *review required* (never silently accepted); the Admin can accept. | As before. |
| Download | The master file itself, under the usual production name — no export job. | Temporary scaled export (as before). |
| Units | External files are read as mm. The panel warns when the sizes do not look like jewelry in mm (largest side < 3 mm or > 200 mm). | Hi3D's arbitrary units, scaled. |

---

## 6. Switching and rollback

Each new version becomes the version in use (LIFO). *Use this version* sets `designs.active_mesh_id` to another one
(logged: from, to, who, when) — rolling back is choosing an older version (its measurement is kept, so nothing is paid or
recomputed beyond arithmetic). New 3D results and new orders use the version in use; existing results and pinned order
lines keep theirs. A version whose file cannot be measured is skipped (logged, by *system*).

---

## 7. Orders reference exact geometry

* **Design → version in use** (changes over time). **Order line → pinned version → pinned 3D result** (never changes
  by itself).
* Placing an order pins each line to the version in use for its option (when the design has a 3D model).
* *Prepare STL* on a line pins it to the result it creates on that version (event `3d_prepared` with the version).
* *Use another version…* re-pins explicitly (event `3d_version_changed`, from / to / who / note); from *In production*
  on a reason is required.
* Orders placed before this change: lines were backfilled with what their page showed on 2026-10-12 (the derived result
  of their own option). A line ordered before its design had a 3D model follows the design until it is prepared, or
  until the order is released (*3D ready* or later pins it, event `3d_pinned`).

---

## 8. Download and resizing keep working

Scalable versions: unchanged (measure once → arithmetic → temporary export → signed link). Exact versions: the result
reports a finished "export" immediately and the signed link serves the master file under the production name. File
names still come from the design's name (Admin *Rename* updates future names), item ID, order number, material, size.
A version's own file downloads as `<Design>_<Option ID>_v<n>.stl`.

---

## 9. Schema changes (additive, idempotent, in `p3/db.py`)

| Table | Change |
|---|---|
| `sessions` (new) | `id` (`ses_…`), `session_no` (S-1001 …, trigger), `owner_account_id`, `title`, `origin` (`design`: a website session that got a second design), `created_by`, `created_at`, `updated_at` |
| `uploads` (new) | `id`, `filename`, `bytes`, `received`, `path`, `status` (receiving / complete / used / expired / cancelled), `sha256`, `format` (binary / ascii), `faces`, `used_by`, `created_by`, `created_at`, `updated_at` |
| `schema_marks` (new) | one-time data steps done on this database (`versions-backfill`) |
| `designs` | `session_id`, `origin` (`ai` / `external`), `active_mesh_id` |
| `meshes` | `version_no`, `source` (`hi3d` / `external`), `scaling` (`scalable` / `exact`), `parent_mesh_id`, `source_tool`, `original_filename`, `note`, `created_by`, `archived_at`, `archived_by` |
| `order_lines` | `three_d_id`, `mesh_id` (also in the table's DDL, so a later rebuild keeps them) |

Backfill (once per database, `schema_marks`): versions numbered per design by creation; every design's version in use =
its newest ready model (the old implicit choice — LIFO); every order line's pin = the derived result of its own option.
Old code ignores the new columns, so a rollback of the code keeps working on the new database.

## 10. Storage

One master per version; nothing per size; temporary exports and downloads expire as before. Uploads (≤ 300 MB) stream
to disk in 8 MB pieces and are *moved* into place; an attached STL is permanent. An ASCII STL is converted to binary at
measurement and only the binary is kept — the same triangles (each coordinate as the float32 a binary STL stores),
about 5× smaller; the uploaded file's SHA-256 and size stay in the version's provenance. Earlier and archived versions
keep their files (history, and no new paid Hi3D call). An external design's picture is a render of its version in use,
redone when another version is in use.

## 11. Fulfillment

`Customer → Session → Design → 3D version → Order → Order line (pinned version + result) → Manufacturing part → Build
→ QC → Shipment`. A future *Manufacturing part* references the order line, its pinned version (`mesh_id`, the master's
SHA-256) and the exported file (size, material, its own hash) — the same for AI, Hi3D and external geometry.

## 12. Decisions (approved 2026-10-12)

1. **Ownership and visibility.** An external STL is another asset of the part/design, in its existing session. Customers
   never get STL files (no download, no management). When the part is ordered, the order line is pinned to the 3D
   version as part of the normal order flow.
2. **Sessions** start on the website; the Admin does not create sessions. A session may have no finished part yet; the
   external part is attached to that existing session.
3. **Several versions, LIFO.** The newest valid version is the default (the one in use); the Admin can choose any other
   existing version; history is kept, nothing is overwritten.
4. **Units and checks** (approved as built): millimetres; ring inner diameter ±0.15 mm; charm height ±0.5 mm; outside the
   tolerance → review, never silently accepted.
5. **Upload size and retention.** At most 300 MB per STL. A completed STL attached to a part is a persistent asset of
   the session's part (like its image and movie) — never deleted automatically. Only uploads that were never attached
   are removed.
6. **ASCII STL** is converted to binary, preserving the geometry.

Also (as built): re-pinning an order line from *In production* on needs a reason; an exact part's results default to its
own size; the Inspiration Gallery does not offer external designs.

## 13. API (Admin, as built)

| Call | What it does |
|---|---|
| `POST /api/admin/uploads` `{filename, bytes}` | Starts an upload (an `.stl`, ≤ 300 MB, `P3_MAX_UPLOAD_MB`); returns its id and the piece size (8 MB). |
| `PUT /api/admin/uploads/{id}?offset=N` | The next piece, raw; `409 offset_mismatch` says where to continue. |
| `POST /api/admin/uploads/{id}/complete` | Checks it is an STL (binary or ASCII) and hashes it. |
| `DELETE /api/admin/uploads/{id}` | Cancels an upload not attached yet; the partial file is deleted. |
| `GET` / `POST /api/admin/designs/{id}/versions` | The design's versions / a new version from an upload (`scaling`, `source_tool`, `note`, `candidate_id`, `parent_mesh_id`; `activate`, default true = LIFO). |
| `POST /api/admin/versions/{mesh}/activate` · `archive` · `restore` | Use this version (switch, roll back), hide, bring back. |
| `POST /api/admin/versions/{mesh}/download-link` | A 10-minute signed link to the version's own file (`<Design>_<Option ID>_v<n>.stl`). |
| `GET /api/admin/versions/{mesh}/preview` | Its light preview, for the 3D view before any sized result. |
| `GET /api/admin/sessions/{ses_…}` | A session holding several designs, and its designs. |
| `POST /api/admin/sessions/{ses_… or design id}/external-designs` | A new external design of an existing session from an upload (`product_type`, `title`, `scaling`, `source_tool`, `note`, `force`). |
| `POST /api/admin/orders/{order}/lines/{line}/3d-version` `{mesh_id, note}` | Produce the line from another version of its option. |
| `POST /api/admin/orders/{order}/lines/{line}/3d-upload` | A revised STL for the line: a new version (in use, LIFO), then the line uses it. |
