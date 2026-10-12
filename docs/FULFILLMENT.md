# Atelier ↔ Fulfillment integration (Pipeline 3 side)

XJet Atelier Fulfillment is the internal order-fulfillment and production-management application for XJet
Atelier. It is a **separate application and service with its own database**, developed in its own repository
(`Xjet3d/xjet-atelier-fulfillment`, private). This document describes what Pipeline 3 contributes to the integration
and the rules that apply on this side. The decisions, the discovery of this codebase and the specification live in
the Fulfillment repository (`docs/decisions.md`, `docs/discovery.md`, `docs/implementation-spec.md`); the Pipeline 3
changes are made on the branch `feature/atelier-fulfillment`.

## Ownership

| Atelier (this application) owns | Fulfillment owns |
|---|---|
| Customers, accounts, quotes, orders, lines, prices, currencies, promo codes | Operational review and its decisions after enrollment |
| Payment status and every payment decision (`paid` is the only release basis in Phase A) | The approved production revision and its history |
| The shipping address and its changes | Production profiles, the queue, attempts, builds, QC |
| Commercial cancellation requests | Packing and shipping execution, tracking, delivery |
| Customer-facing status and every customer email | Events that drive that status |

Fulfillment never writes to `pipeline3.db`; Pipeline 3 never writes to Fulfillment's database. Pipeline 3 keeps a
**projection** of Fulfillment progress for enrolled orders, not a competing state machine.

## What Pipeline 3 gains (all behind `P3_FULFILLMENT_INTEGRATION`, off by default)

- **Enrollment.** An order is handed to Fulfillment by the Admin's explicit *Send to Fulfillment* action, never
  automatically (manual ingestion during the pilot, D-15). **Quotations are never enrolled** (D-10): a quote is
  managed here until the customer approves it and it becomes an order; that order is sent, and its snapshot carries
  the quote as a reference (`order.quote`, `lines[].quote_request_id`). From then on the order's process owner is
  Fulfillment: the legacy production / qc / shipped / completed status changes on that order are refused with an
  explanation, and those statuses are written by the projection instead. Payment, notes and cancellation stay with
  the Admin.
- **The integration API** `GET|POST /JewelryB2C3/api/integrations/fulfillment/v1/…` (identity, changes, a request's
  snapshot, commands, events, release checks, shipping checks, restricted asset access), authenticated with a dedicated
  service credential (`P3_FULFILLMENT_SERVICE_KEY`, never the Admin key) and answering only in the internal Admin
  context. Fulfillment initiates every connection; Pipeline 3 never calls into the internal network.
- **Change capture.** Additive tables (`fulfillment_enrollments`, `fulfillment_outbox`, `fulfillment_commands`,
  `fulfillment_inbox`, `fulfillment_projection`, `notification_intents`); the outbox is filled by database triggers on
  changes to enrolled orders and lines, so a change and its event are one transaction.
- **Admin.** Orders and Quotes gain a Fulfillment state (the customer stage, the review decision with its reason and
  customer-facing message, the first blocker, pending requests, commercial action pending, awaiting synchronisation),
  a Fulfillment filter, *Send to Fulfillment*, and cancellation requests for a line through commands that show
  `Pending`, `Applied` or `Rejected` with the reason — never shown as done before Fulfillment answered. The order
  page's Fulfillment card shows the parts (stage, review, file, quantities, blockers) and the requests; QC, holds and
  shipments join it in later batches. Review actions themselves live in Fulfillment (no second interface).
- **My Account.** A customer-facing `fulfillment_stage` and timeline derived from events, never internal notes.
- **Notifications.** Customer emails for review approved, changes required, cannot produce, production started,
  shipped and delivered are created and sent only by Pipeline 3 from `notification_intents`, deduplicated by order +
  milestone + revision/shipment, and only when `P3_FULFILLMENT_CUSTOMER_NOTIFICATIONS` is on (off by default; a test
  recipient first). Fulfillment sends no email.

The contract (endpoints, snapshot, events, errors) is maintained in the Fulfillment repository under
`contracts/atelier-integration/v1/`. Both sides check the contract version string (`atelier-fulfillment/v1`);
`tests/test_fulfillment_bridge.py` here asserts the shapes this side serves and accepts; and the Fulfillment
repository's `tests/test_contract_p3.py` runs the real Pipeline 3 application against the real Fulfillment application
in one process (mock AI provider, no network) — it needs this checkout (`FULFILLMENT_P3_PATH`) and this repository's
virtual environment.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `P3_FULFILLMENT_INTEGRATION` | `false` | Turns the integration on: the service API answers, the Admin's *Send to Fulfillment* works, enrolled orders are owned by Fulfillment. Off: the integration routes answer 404, no bridge table is written, nothing changes for anyone. |
| `P3_FULFILLMENT_SERVICE_KEY` | unset | The service credential Fulfillment presents (`Authorization: Bearer …`): 16+ characters in development, 24+ in production, no spaces, different from the Admin key and the signing secret. Required when the integration is on; never the Admin key. |

The environment id Fulfillment pins (`EXPECTED_ATELIER_ENVIRONMENT_ID` there) is generated once per data directory
(`product_settings.fulfillment_environment_id`, `atelier-` + 10 hex characters) and shown by `GET …/v1/identity`.

## Rules on this branch

- Nothing here is deployed, merged into `main` or pushed to `atelier-production` without the owner's approval
  (`atelier-production` deploys to production automatically). The proto/tron instance runs live fal.ai: no test ever
  creates designs there.
- Existing behaviour for orders that are **not** enrolled is unchanged, and the whole test suite stays green
  (395 tests; two of them assert a `0o600` file mode and can only pass on Linux).
- No "payment not required", no manual payment override and no alternative release path (decision D-5).
- Migrations are additive; no Atelier table is renamed or dropped.

## Batches

| Batch | On this side |
|---|---|
| 0 | This document and the README link |
| 3 | **Done 2026-10-12.** `p3/fulfillment_bridge.py`: the bridge tables and triggers (commercial changes and 3D results of enrolled records fill the outbox in the same transaction), the identity / changes / requests / assets (line image, production STL with its SHA-256) / events endpoints, the service key and the flag, the Admin's *Send to Fulfillment* with the order's Fulfillment card and projection, the ownership guard (production / qc / shipped / completed refused on enrolled orders; the projection's own writes create no change). *Amended 2026-10-13 (D-10): the quote-request enrollment, snapshot and trigger of this batch were removed — quotations stay in the Admin; the order keeps the quote reference* |
| 4 | **Done 2026-10-13.** `POST …/v1/release-checks` (payment, cancellation, quantity read live for the exact lines and quantities); commands (`fulfillment_commands`, the Admin's *Request cancellation* on a line of an enrolled order → `cancel_line` in the feed with its payload → Pending until Fulfillment's `command_applied` / `command_rejected` event, the first answer stands, a repeated click is the same request); the projection writes Fulfillment's `order_status` into the order (production → qc → shipped → completed, forward only, never on a cancelled or completed order, no change goes back out); the Orders list's *Fulfillment* column and filter (`?fulfillment=enrolled|review|changes|blocked|in_progress|ready_to_ship|shipped|commercial_action|awaiting_sync`), the quote inbox's column, the order page's parts table (stage, review decision with its reason and customer-facing message, file, quantities, blockers) and requests, the quote page's Fulfillment card with *Send to Fulfillment* |
| 8 | (also) `fulfillment_stage` and the timeline in My Account |
| 5 | Nothing new on this side: the release checks of Batch 4 are what Fulfillment calls at release; `production_started` / `production_completed` move the order status to production / qc through the projection |
| 6 | Nothing new in code: the new events (`production_failed`, `production_withdrawn`, `qc_recorded`, `qc_corrected`, `reprint_planned`, `cancellation_accepted` / `cancellation_rejected`) carry the same parts summary and are projected like every other — the order card shows each part's quantities ("2/3 passed QC; 1 reprint pending") and the order's history gets one line per event; a cancellation the Admin requested, or an order cancelled here, is decided by Operations in Fulfillment and the decision arrives as an event (the contract README in the Fulfillment repository lists the payloads) |
| 7 | Shipping checks (the authoritative address and its version) |
| 8 | Notification intents, templates, deduplication, outbox-first dispatch, the notifications flag |
| 9 | Regression of the whole suite with the flag off and on |
