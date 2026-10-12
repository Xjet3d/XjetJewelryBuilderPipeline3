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

- **Enrollment.** An order (or a quote request) is handed to Fulfillment by the Admin's explicit *Send to
  Fulfillment* action, never automatically. From then on the record's process owner is Fulfillment: the legacy
  production / qc / shipped / completed status changes on that order are refused with an explanation, and those
  statuses are written by the projection instead. Payment, notes and cancellation stay with the Admin.
- **The integration API** `GET|POST /JewelryB2C3/api/integrations/fulfillment/v1/…` (identity, changes, a request's
  snapshot, commands, events, release checks, shipping checks, restricted asset access), authenticated with a dedicated
  service credential (`P3_FULFILLMENT_SERVICE_KEY`, never the Admin key) and answering only in the internal Admin
  context. Fulfillment initiates every connection; Pipeline 3 never calls into the internal network.
- **Change capture.** Additive tables (`fulfillment_enrollments`, `fulfillment_outbox`, `fulfillment_commands`,
  `fulfillment_inbox`, `fulfillment_projection`, `notification_intents`); the outbox is filled by database triggers on
  changes to enrolled orders and lines, so a change and its event are one transaction.
- **Admin.** Orders and Quotes gain a Fulfillment state, review status, blockers, quantities, last synchronisation,
  filters, an internal link, *Send to Fulfillment*, and cancellation / change requests through commands that show
  `Pending`, `Applied` or `Rejected`. The Fulfillment tab of an order shows parts, revision, quantities, QC, holds,
  shipments and history. Review actions themselves live in Fulfillment.
- **My Account.** A customer-facing `fulfillment_stage` and timeline derived from events, never internal notes.
- **Notifications.** Customer emails for review approved, changes required, cannot produce, production started,
  shipped and delivered are created and sent only by Pipeline 3 from `notification_intents`, deduplicated by order +
  milestone + revision/shipment, and only when `P3_FULFILLMENT_CUSTOMER_NOTIFICATIONS` is on (off by default; a test
  recipient first). Fulfillment sends no email.

The contract (schemas and examples) is maintained in the Fulfillment repository under
`contracts/atelier-integration/v1/`; this repository keeps fixture copies under `tests/fixtures/fulfillment/` and the
contract version string both sides check.

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
| 3 | Bridge tables, triggers, identity / changes / requests / assets / events endpoints, the service key, the flag, a minimal *Send to Fulfillment* |
| 4 | Release checks, commands with their Admin state, the ownership guard, Orders / Quotes / order-detail changes, the status projection, `fulfillment_stage` in My Account |
| 7 | Shipping checks (the authoritative address and its version) |
| 8 | Notification intents, templates, deduplication, outbox-first dispatch, the notifications flag |
| 9 | Regression of the whole suite with the flag off and on |
