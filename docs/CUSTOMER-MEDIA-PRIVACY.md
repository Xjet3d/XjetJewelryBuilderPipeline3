# Customer media: who can see it, who may cache it (audit SEC-05, 2 Nov 2026)

## What the site does since B2

| Media | Address | Who can open it | Caching |
|---|---|---|---|
| A customer's uploaded reference photo (`designs/<id>/references/<batch>.png`) | `/api/designs/<id>/references/<batch>` (owner), `/api/admin/designs/<id>/references/<batch>` (Admin) | The design's owner; the Admin | `private, no-store` |
| The same photo at a media address (`/assets/…`, `/thumb/…`, `/assets/_derived/…`) | — | Nobody: 404 | `no-store` |
| Generated images and movies of a **gallery** design, or of the design a gallery variation came from | `/assets/…`, `/thumb/…`, `/poster/…`, `/clip/…` | Anyone with the address | `public, max-age=31536000, immutable` (Cloudflare may keep it) |
| Generated images and movies of **any other** design | the same media addresses (random ids) | Anyone with the address, as order emails, the bag, order history, Fulfillment and the Admin need | `private, max-age=31536000, immutable` (the visitor's browser only, never Cloudflare) |

The random ids (uuid4) make these addresses unguessable; they are sent only to the design's owner (pages, emails) and to
XJet (Admin, Fulfillment). Fulfillment and the gallery sync read files from disk and are not affected.

## Not changed: removing a design

"Remove" in My Designs hides a design (soft delete). Its files stay and are still served, because an order, a bag line,
an order email or the Admin can still show the design. No file is deleted by B2.

## Proposal for later approval: deleting files

Nothing below is implemented. Each step needs the owner's approval and the retention periods of PRV-02.

1. **When.** A design's files may be deleted only when the design is removed by its owner **and** is not in the gallery,
   not in any bag, order or quote, and not the source of a gallery design. Orders keep their images for the order
   retention period (PRV-02: proposed 7 years).
2. **What.** The design's folder under `assets/designs/<id>/` and its `_derived/` thumbnails, clips and posters. Database
   rows stay (the Admin's history), with the file paths cleared.
3. **Shared caches.** A design that was in the gallery may be in Cloudflare's cache: after deleting, purge its
   addresses (Cloudflare API "purge by URL", by IT). Private media is never in Cloudflare's cache.
4. **Backups.** Deleted files remain in backups until those expire (30 days today).
5. **How.** An Admin action or a scheduled job that lists what it would delete (dry run), and deletes only after a
   confirmation, writing an audit event per design.
