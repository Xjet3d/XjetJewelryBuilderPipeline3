# Atelier ↔ Fulfillment integration (Pipeline 3 side)

XJet Atelier Fulfillment is the internal order-fulfillment and production-management application for XJet
Atelier. It is a **separate application and service with its own database**, developed in its own repository
(`Xjet3d/xjet-atelier-fulfillment`, private). This document describes what Pipeline 3 contributes to the integration
and the rules that apply on this side. The decisions, the discovery of this codebase and the specification live in
the Fulfillment repository (`docs/decisions.md`, `docs/discovery.md`, `docs/implementation-spec.md`, and — for the
Order → Production stage of 2026-10-13 — `docs/order-to-production-review.md`); the Pipeline 3 changes are made on
the branch `feature/atelier-fulfillment`, reconciled with `main` (3D versions, external parts, pinned order lines)
on 2026-10-13.

## Ownership

| Atelier (this application) owns | Fulfillment owns |
|---|---|
| Customers, accounts, quotes, orders, lines, prices, currencies, promo codes | Manufacturing parts and their identifiers (`MP-<order>-<line>`, builds `B-nnnn`) |
| Designs, 3D versions, the pin of each order line and the production file handed over | The production revisions, their immutable files and their history after the hand-over |
| Payment status and every payment decision (whether it gates production is Atelier's rule, `P3_FULFILLMENT_PAYMENT_GATE`) | Manufacturing review, profiles, the pool, builds, operations, QC, holds, rework, reprints |
| The shipping address and its changes | Packing and shipping execution, tracking, delivery |
| Commercial cancellation requests | Operations' decision on them |
| Customer-facing status and every customer email | Events that drive that status; internal notes and attention requests for the Admin |

Fulfillment never writes to `pipeline3.db`; Pipeline 3 never writes to Fulfillment's database. Pipeline 3 keeps a
**projection** of Fulfillment progress for enrolled orders, not a competing state machine.

## What Pipeline 3 gains (all behind `P3_FULFILLMENT_INTEGRATION`, off by default)

- **Send to Production — per order line, by hand, never automatically.** On the order page each line has *Send to
  Production*, enabled only when the line is pinned to a 3D version with a complete sized result for the ordered
  size and material (measured, closed or accepted) — otherwise the button is disabled with *Print file required
  before sending to Production* and the exact reason (no model, not prepared, flagged, another size …); the order
  card offers *Send selected lines to Production* with the eligible lines ticked and the others named with their
  reason. The server checks every line again (`409 print_file_required` with the blocked lines; `409 already_sent`).
  The first line sent enrolls the order (`fulfillment_enrollments`); each sent line gets a row in
  `fulfillment_line_production` with the exact pin: the 3D version (number, source, scaling, the master's SHA-256),
  the sized result, the measurement method and the production file's SHA-256 (recorded as soon as its export
  finished — a change with reason `production_file`). Nothing is ever sent by itself when a file appears.
  **Quotations are never enrolled** (D-10); the order a quote becomes carries the quote as a reference.
  **There is no un-send** (K7): a wrong file is replaced through an explicit revision, a line leaves Production only
  through the cancellation request Operations decide. From the first line on, the legacy production / qc / shipped /
  completed status changes of that order are refused (`owned_by_fulfillment`); those statuses are written by the
  projection instead, and only once every line of the order is in Production (K1). Payment, notes and cancellation
  stay with the Admin.
- **The integration API** `GET|POST /JewelryB2C3/api/integrations/fulfillment/v1/…` (identity, changes, a request's
  snapshot, commands, events, release checks, shipping checks, restricted asset access), authenticated with a dedicated
  service credential (`P3_FULFILLMENT_SERVICE_KEY`, never the Admin key) and answering only in the internal Admin
  context. Fulfillment initiates every connection; Pipeline 3 never calls into the internal network. The production
  file route serves the export of the result pinned by a **sent** line and nothing else — a line not sent answers
  404 whatever its model; a result no sent line pins answers 404 (the credential alone opens nothing); an export that
  no longer reproduces the pinned file answers `409 file_changed` and is never served.
- **Change capture.** Additive tables (`fulfillment_enrollments`, `fulfillment_line_production`, `fulfillment_outbox`,
  `fulfillment_commands`, `fulfillment_inbox`, `fulfillment_inbox_conflicts`, `fulfillment_projection`,
  `fulfillment_sync`, `fulfillment_notification_intents`); the outbox is filled by database triggers on `orders`
  (payment, address, customer, totals, the Admin's own statuses) and on `session_3d` (3D results of enrolled orders'
  designs), and by the hand-over itself, so a change and its outbox row are one transaction.
- **Admin.** Orders and Quotes gain a Fulfillment state (the customer stage, the worst review state, the first
  blocker, pending requests, commercial action pending, awaiting synchronisation, lines sent of lines total), a
  Fulfillment filter, the per-line hand-over, and cancellation requests for a sent line through commands that show
  `Pending`, `Applied` or `Rejected` with the reason — never shown as done before Fulfillment answered. The order
  page's card shows the parts (stage, review decision with its reason and customer-facing message, file, quantities,
  blockers), the parcels, the email intents and the requests. The **Production page** (`#/orders/production`) is the
  operational view of every line sent: order, customer, part, MP number, material, size, print version, stage, build,
  attention, latest internal note, last update — with a detail page (the pin, the quantities, the build, the notes,
  the timeline, the holds and attention requests, the revision and cancellation actions). Build planning, production
  execution, QC and manufacturing operations stay in Fulfillment (no second interface).
- **My Account.** A customer-facing `fulfillment_stage` and timeline derived from the public payload, never internal
  notes; nothing of the staff-only `admin_payload` reaches a customer.
- **Notifications.** Customer emails for review approved, changes required, cannot produce, production started,
  shipped and delivered are created and sent only by Pipeline 3 from `fulfillment_notification_intents`, deduplicated
  by order + milestone + revision/shipment, and only when `P3_FULFILLMENT_CUSTOMER_NOTIFICATIONS` is on (off by
  default; a test recipient first). Fulfillment sends no email.

The contract (endpoints, snapshot, events, errors) is maintained in the Fulfillment repository under
`contracts/atelier-integration/v1/` (revised in place on 2026-10-13, before the first activation — K10). Both sides
check the contract version string (`atelier-fulfillment/v1`); `tests/test_fulfillment_bridge.py` here asserts the
shapes this side serves and accepts; and the Fulfillment repository's `tests/test_contract_p3.py` runs the real
Pipeline 3 application against the real Fulfillment application in one process (mock AI provider, no network) — it
needs this checkout (`FULFILLMENT_P3_PATH`) and this repository's virtual environment.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `P3_FULFILLMENT_INTEGRATION` | `false` | Turns the integration on: the service API answers, the Admin's *Send to Production* works, sent lines are owned by Fulfillment. Off: the integration routes answer 404, no bridge table is written, nothing changes for anyone. |
| `P3_FULFILLMENT_SERVICE_KEY` | unset | The service credential Fulfillment presents (`Authorization: Bearer …`): 16+ characters in development, 24+ in production, no spaces, different from the Admin key and the signing secret. Required when the integration is on; never the Admin key. |
| `P3_FULFILLMENT_PAYMENT_GATE` | `true` | The commercial rule advertised to Fulfillment as `identity.payment_gate`. `true`: an unpaid order's lines are not released (the live checks say `PAYMENT_PENDING`). `false`: payment is shown, never gating — the Admin decides what enters Production (complimentary samples, internal evaluations; the reason goes in the order's notes). Switched to `false` at the integration's activation (K8), not before. |
| `P3_FULFILLMENT_URL` | unset | The Fulfillment application's URL (e.g. `https://tron/atelier-fulfillment`) for the Production page's *Open in Fulfillment* links to the part. Unset: no links. |
| `P3_FULFILLMENT_ORDER_ALLOWLIST` | unset | The controlled phase (the owner's authorization of 2026-10-13, §6): while set, only the listed orders (ids or references, comma-separated) can be sent to Production — the server refuses every other order (`409 not_eligible`) and the order page says so; an order already in Production stays allowed. Fulfillment keeps its own list (`FULFILLMENT_ENROLLMENT_ALLOWLIST`) at ingestion. Unset: every eligible order. |
| `P3_FULFILLMENT_CUSTOMER_NOTIFICATIONS`, `P3_FULFILLMENT_NOTIFICATION_RECIPIENT`, `P3_FULFILLMENT_NOTIFICATION_WINDOW_S` | off / unset / 120 | Customer notifications (Batch 8): off by default; every message to the test recipient while one is set; parts of one order reaching a milestone together make one email. |

The environment id Fulfillment pins (`EXPECTED_ATELIER_ENVIRONMENT_ID` there) is generated once per data directory
(`product_settings.fulfillment_environment_id`, `atelier-` + 10 hex characters) and shown by `GET …/v1/identity`.

## Rules on this branch

- Nothing here is deployed, merged into `main` or pushed to `atelier-production` without the owner's approval
  (`atelier-production` deploys to production automatically). The proto/tron instance runs live fal.ai: no test ever
  creates designs there.
- Existing behaviour for orders that are **not** sent to Production is unchanged, and the whole test suite stays
  green (two tests assert a `0o600` file mode and can only pass on Linux).
- Payment: no fake payment confirmation, no change to checkout or payment records; whether payment gates production
  is the one flag above (D-5 superseded by the owner's decision of 2026-10-13: production is a manual Admin decision).
- Migrations are additive; no Atelier table is renamed or dropped.

## Batches

| Batch | On this side |
|---|---|
| 0 | This document and the README link |
| 3 | **Done 2026-10-12.** `p3/fulfillment_bridge.py`: the bridge tables and triggers (commercial changes and 3D results of enrolled records fill the outbox in the same transaction), the identity / changes / requests / assets (line image, production STL with its SHA-256) / events endpoints, the service key and the flag, the Admin's hand-over with the order's Fulfillment card and projection, the ownership guard (production / qc / shipped / completed refused on enrolled orders; the projection's own writes create no change). *Amended 2026-10-13 (D-10): the quote-request enrollment, snapshot and trigger of this batch were removed — quotations stay in the Admin; the order keeps the quote reference* |
| 4 | **Done 2026-10-13.** `POST …/v1/release-checks` (payment, cancellation, quantity read live for the exact lines and quantities); commands (`fulfillment_commands`, the Admin's *Request cancellation* on a line → `cancel_line` in the feed with its payload → Pending until Fulfillment's `command_applied` / `command_rejected` event, the first answer stands, a repeated click is the same request); the projection writes Fulfillment's `order_status` into the order (production → qc → shipped → completed, forward only, never on a cancelled or completed order, no change goes back out); the Orders list's *Fulfillment* column and filter, the quote inbox's column, the order page's parts table and requests, the quote page's Fulfillment card |
| 8 | **Done 2026-10-13.** My Account (`GET /api/orders`, `/api/orders/{id}`): an enrolled order carries `fulfillment` (`stage`, each piece's stage, the parcels with carrier and tracking link, a timeline of the customer milestones with fixed labels, the workshop's customer-facing message when a change is needed) and `fulfillment_stage`; orders that are not enrolled are untouched. Customer notifications (`p3/fulfillment_notifications.py`): the milestone events become **notification intents** in the transaction that projects them, deduplicated by order + milestone + revision or parcel; the dispatcher sends one email per order and milestone and records the outcome (`sent`, a known non-delivery retried then `failed`, `unknown` flagged and never retried by itself). **Sending is off by default**; with a recipient set every message goes there instead of to the customer |
| 5 | Nothing new on this side: the release checks of Batch 4 are what Fulfillment calls at release; `production_started` / `production_completed` move the order status to production / qc through the projection |
| 6 | Nothing new in code: the new events (`production_failed`, `production_withdrawn`, `qc_recorded`, `qc_corrected`, `reprint_planned`, `cancellation_accepted` / `cancellation_rejected`) carry the same parts summary and are projected like every other |
| 7 | **Done 2026-10-13.** `POST …/v1/shipping-checks`; the projection keeps the order's `shipments` and the order page's card lists the parcels; `shipment_shipped` / `shipment_delivered` move the order status to shipped / completed through the projection, forward only, and only once every part is there |
| 9 | **Done 2026-10-13.** The complete suite on this branch at `4411fa2`: 413 tests, 411 passed + the 2 Windows-only `0o600` file-mode tests; the cross-application test in the Fulfillment repository walks the whole flow against this code in one process |
| Order → Production A–B | **Done 2026-10-13** (the owner's decisions K1–K12). `main` merged into this branch (3D versions, external parts, pinned order lines). The per-line hand-over: `fulfillment_line_production`, `LineEligibility`, `SendToProduction` (`POST /api/admin/orders/{id}/production {line_ids, note}` — the order-level *Send to Fulfillment* is gone), the snapshot's `lines[].production` with the exact pin and `model.version`, the production file routes serving the pinned export only (`pinned_sha256`, `409 file_changed`, 404 for anything not pinned by a sent line), the file's hash recorded when the export finishes (`production_file`), the K1 rule in the projection, `P3_FULFILLMENT_PAYMENT_GATE` advertised as `identity.payment_gate` and honoured by the two live checks. Defects fixed: the Admin page's undefined `toast` after a hand-over or a command (`notify`), the production file route's authorization (enrollment and pin, not the credential alone), the dormant un-enroll path removed (no un-send). The order page: per-line *Send to Production* with the disabled state and its reason, *Send selected lines to Production*, *Request cancellation* on sent lines only |
| Order → Production C–D | **Done 2026-10-13.** The **Production page** (`#/orders/production`, `GET /api/admin/production` with stage / attention / material / product / build / order / date filters, search and paging, grouped by order; `GET /api/admin/production/{line_id}` for one line): every line sent, with Atelier's facts (order, customer, line, the pin, the hand-over note) and Fulfillment's staff-only side from the event payloads (`admin_payload.parts[]`: stage, build, holds, the open attention request, the latest internal note, the revision with its hand-over number, the quantities) — the row's stage is *sent* until Fulfillment reports the part, then Fulfillment's; the line's timeline (K9: the order's own events filtered by `line_id` / `about_parts`, Atelier's and Fulfillment's in time order, each with its source); the *Open in Fulfillment* link (`P3_FULFILLMENT_URL`). The customer's view never carries any of it. The **revised-file workflow** (§B.6): for a line in Production the plain *Use another version* / upload is refused (`409 line_in_production`, `Orders.LineGuard`); the Production page's detail offers *Submit as the revised file* on another version or an upload, with a mandatory reason and the attention request it answers — `POST /api/admin/orders/{id}/lines/{line}/production-revision` re-pins the line (`UseVersion` / `UploadVersion` with `ForProduction`), advances `handoff_no`, records the new file when its export finishes, and tells Fulfillment (`production_revision`); the former result is no longer served. Attention requests and internal notes from Fulfillment are shown on the row, the detail and the order card (K4, K5); a cancellation request can be made from the detail (sent lines only, K7). `tests/test_production_page.py`, the per-line and payment-gate tests in `tests/test_fulfillment_bridge.py` |
| Order → Production E | **Done 2026-10-13.** The documentation of both sides (this file, `docs/ADMIN.md`, the contract), the cross-application test of the Fulfillment repository rewritten for the per-line flow (the refusal without a print file, the hand-over with the pin, the file verified against it, the attention request of a review decision, an internal note, the revised file after the hand-over and its second pin, the request answered and closed, the cancellation request, the build, QC, shipping and delivery), the full suites of both repositories, the browser verification of the Production page — results in the Fulfillment repository's `docs/order-to-production-report.md`. Not deployed: the integration stays off on tron until the owner's word |
| Controlled deployment | **2026-10-13, the owner's authorization.** `55f1a19` deployed on tron's Pipeline 3 (the checkout moved from `main` `36d6613` to this branch; nothing merged into `main` or `atelier-production`); first with the integration off, then on with `P3_FULFILLMENT_PAYMENT_GATE=false` and `P3_FULFILLMENT_ORDER_ALLOWLIST` naming only the synthetic test orders; the service key generated on tron and kept in the `.env` files only. The scenarios (standard order, multiple lines, revision after the hand-over, attention request, negative and recovery cases), the security checks and the rollback drill were run against Fulfillment `288ebbc` on tron — the report is the Fulfillment repository's `docs/controlled-deployment-report.md`. The real-order pilot waits for the owner's separate approval |
